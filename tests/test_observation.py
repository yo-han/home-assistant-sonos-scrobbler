"""Replay scenarios against the pure observation engine."""

from dataclasses import replace

import pytest

from custom_components.personal_sonos_scrobbler import observation
from custom_components.personal_sonos_scrobbler.normalize import (
    MetadataPresence,
    PlaybackSample,
    PlaybackStatus,
    SourceCategory,
)
from custom_components.personal_sonos_scrobbler.observation import (
    ObservationEngine,
    Reason,
    Stage,
    threshold_seconds,
)

TRACK_A = b"a" * 16
TRACK_B = b"b" * 16


def track(
    key: bytes | None = TRACK_A,
    *,
    status: PlaybackStatus = PlaybackStatus.PLAYING,
    category: SourceCategory = SourceCategory.SPOTIFY_CONFIRMED,
    duration: float | None = 200.0,
    position: float | None = None,
    queue: int | None = None,
) -> PlaybackSample:
    return PlaybackSample(
        status=status,
        category=category,
        track_key=key,
        duration_seconds=duration,
        position_seconds=position,
        queue_position=queue,
        presence=MetadataPresence(title=key is not None),
    )


PAUSED = track(status=PlaybackStatus.PAUSED)
UNAVAILABLE = track(None, status=PlaybackStatus.UNAVAILABLE, duration=None)
EMPTY = track(None, duration=None)


@pytest.fixture
def engine() -> ObservationEngine:
    return ObservationEngine()


def play_for(engine: ObservationEngine, start: float, stop: float) -> None:
    """Advance through heartbeats so no delta looks like a clock gap."""
    now = start
    while now < stop:
        now = min(now + 15, stop)
        engine.tick(now)


@pytest.mark.parametrize(
    ("duration", "threshold"),
    [
        (None, None),
        (0, None),
        (30, None),
        (30.5, 15.25),
        (200, 100),
        (480, 240),
        (600, 240),
    ],
)
def test_threshold_boundaries(duration: float | None, threshold: float | None) -> None:
    assert threshold_seconds(duration) == threshold


def test_normal_track_qualifies_exactly_at_threshold(
    engine: ObservationEngine,
) -> None:
    engine.observe(track(), 0)
    play_for(engine, 0, 99)
    assert engine.snapshot.candidate_count == 0
    assert engine.snapshot.stage is Stage.OBSERVING
    assert engine.snapshot.reason is Reason.COUNTING

    engine.tick(100)

    assert engine.snapshot.candidate_count == 1
    assert engine.snapshot.stage is Stage.QUALIFIED
    assert engine.snapshot.reason is Reason.THRESHOLD_REACHED


def test_attach_mid_track_starts_at_zero(engine: ObservationEngine) -> None:
    engine.observe(track(position=150), 0)
    assert engine.snapshot.observed_seconds == 0

    play_for(engine, 0, 99)
    assert engine.snapshot.candidate_count == 0
    engine.tick(100)
    assert engine.snapshot.candidate_count == 1


@pytest.mark.parametrize(
    ("duration", "reason"),
    [(30.0, Reason.DURATION_TOO_SHORT), (None, Reason.DURATION_UNKNOWN)],
)
def test_short_and_unknown_duration_never_qualify(
    engine: ObservationEngine, duration: float | None, reason: Reason
) -> None:
    engine.observe(track(duration=duration), 0)
    play_for(engine, 0, 300)

    assert engine.snapshot.reason is reason
    assert engine.snapshot.candidate_count == 0
    assert engine.snapshot.threshold_seconds is None


def test_duration_arriving_later_keeps_elapsed(engine: ObservationEngine) -> None:
    engine.observe(track(duration=None), 0)
    play_for(engine, 0, 120)

    engine.observe(track(), 120)

    assert engine.snapshot.observed_seconds == 120
    assert engine.snapshot.candidate_count == 1


def test_duration_dropping_out_keeps_known_duration(
    engine: ObservationEngine,
) -> None:
    engine.observe(track(), 0)
    engine.observe(track(duration=None), 10)

    assert engine.snapshot.duration_seconds == 200


def test_pause_and_resume_excludes_paused_time(engine: ObservationEngine) -> None:
    engine.observe(track(), 0)
    play_for(engine, 0, 40)
    engine.observe(PAUSED, 40)
    assert engine.snapshot.stage is Stage.SUSPENDED
    assert engine.snapshot.reason is Reason.NOT_PLAYING

    engine.observe(track(), 540)
    play_for(engine, 540, 600)

    assert engine.snapshot.observed_seconds == 100
    assert engine.snapshot.clock_gap_count == 0


def test_forward_seek_adds_no_listening_time(engine: ObservationEngine) -> None:
    engine.observe(track(position=20), 0)
    engine.observe(track(position=190), 10)

    assert engine.snapshot.observed_seconds == 10
    assert engine.snapshot.seek_count == 1
    assert engine.snapshot.candidate_count == 0


def test_backward_seek_mid_track_is_not_restart(engine: ObservationEngine) -> None:
    engine.observe(track(position=150), 0)
    engine.observe(track(position=80), 10)

    assert engine.snapshot.seek_count == 1
    assert engine.snapshot.ambiguous_restart_count == 0
    assert engine.snapshot.stage is Stage.OBSERVING


def test_tolerated_position_drift_is_not_a_seek(engine: ObservationEngine) -> None:
    engine.observe(track(position=20), 0)
    engine.observe(track(position=33), 10)

    assert engine.snapshot.seek_count == 0


def test_end_of_track_position_cap_is_not_a_seek(engine: ObservationEngine) -> None:
    engine.observe(track(position=195), 0)
    engine.observe(track(position=200), 10)

    assert engine.snapshot.seek_count == 0


def test_duplicate_and_volume_updates_do_not_reset_or_reemit(
    engine: ObservationEngine,
) -> None:
    engine.observe(track(), 0)
    play_for(engine, 0, 100)

    for second in range(101, 121):
        engine.observe(track(), second)

    assert engine.snapshot.candidate_count == 1
    assert engine.snapshot.observed_seconds == 120
    assert engine.snapshot.stage is Stage.QUALIFIED


def test_track_change_starts_new_play(engine: ObservationEngine) -> None:
    engine.observe(track(), 0)
    play_for(engine, 0, 100)
    assert engine.snapshot.candidate_count == 1

    engine.observe(track(TRACK_B), 110)
    assert engine.snapshot.observed_seconds == 0
    assert engine.snapshot.stage is Stage.OBSERVING

    engine.observe(track(), 120)
    assert engine.snapshot.observed_seconds == 0
    play_for(engine, 120, 220)

    assert engine.snapshot.candidate_count == 2


def test_threshold_crossed_just_before_track_change_still_counts(
    engine: ObservationEngine,
) -> None:
    engine.observe(track(), 0)
    play_for(engine, 0, 90)

    engine.observe(track(TRACK_B), 101)

    assert engine.snapshot.candidate_count == 1


def test_same_track_restart_is_ambiguous_without_second_candidate(
    engine: ObservationEngine,
) -> None:
    engine.observe(track(position=0), 0)
    play_for(engine, 0, 120)

    engine.observe(track(position=1), 120)
    assert engine.snapshot.stage is Stage.RESTART_AMBIGUOUS
    assert engine.snapshot.ambiguous_restart_count == 1

    play_for(engine, 120, 300)
    assert engine.snapshot.candidate_count == 1


def test_restart_with_queue_change_is_new_play(engine: ObservationEngine) -> None:
    engine.observe(track(position=0, queue=3), 0)
    play_for(engine, 0, 120)

    engine.observe(track(position=0, queue=4), 120)

    assert engine.snapshot.observed_seconds == 0
    assert engine.snapshot.ambiguous_restart_count == 0
    play_for(engine, 120, 220)
    assert engine.snapshot.candidate_count == 2


def test_same_track_after_idle_reports_ambiguity(engine: ObservationEngine) -> None:
    engine.observe(track(position=0), 0)
    play_for(engine, 0, 150)
    engine.observe(
        track(None, status=PlaybackStatus.IDLE, duration=None, position=0), 150
    )

    engine.observe(track(position=0), 90_000)

    assert engine.snapshot.ambiguous_restart_count == 1


def test_short_state_with_position_does_not_hide_a_restart(
    engine: ObservationEngine,
) -> None:
    engine.observe(track(position=0), 0)
    play_for(engine, 0, 60)
    engine.observe(track(None, duration=None, position=0), 60)

    engine.observe(track(position=0), 61)
    play_for(engine, 61, 120)

    assert engine.snapshot.ambiguous_restart_count == 1
    assert engine.snapshot.candidate_count == 0


def test_recovery_with_continuous_position_keeps_play(
    engine: ObservationEngine,
) -> None:
    engine.observe(track(position=0), 0)
    play_for(engine, 0, 60)
    engine.observe(UNAVAILABLE, 60)

    engine.observe(track(position=70), 70)
    play_for(engine, 70, 110)

    assert engine.snapshot.ambiguous_restart_count == 0
    assert engine.snapshot.candidate_count == 1


def test_recovery_without_position_keeps_earlier_candidate(
    engine: ObservationEngine,
) -> None:
    engine.observe(track(), 0)
    play_for(engine, 0, 100)
    engine.observe(UNAVAILABLE, 100)

    engine.observe(track(), 200)

    assert engine.snapshot.candidate_count == 1
    assert engine.snapshot.ambiguous_restart_count == 1
    assert engine.seconds_until_next_tick() == 15


def test_new_track_after_lost_continuity_counts_normally(
    engine: ObservationEngine,
) -> None:
    engine.observe(track(), 0)
    play_for(engine, 0, 45)
    engine.tick(150)

    engine.observe(track(TRACK_B), 150)
    play_for(engine, 150, 250)

    assert engine.snapshot.candidate_count == 1


@pytest.mark.parametrize(
    "excluded",
    [
        track(category=SourceCategory.EXCLUDED),
        track(None, category=SourceCategory.EXCLUDED, duration=None),
    ],
    ids=["with_metadata", "without_artist_like_tv"],
)
def test_source_change_to_excluded_ends_play(
    engine: ObservationEngine, excluded: PlaybackSample
) -> None:
    engine.observe(track(), 0)
    play_for(engine, 0, 60)

    engine.observe(excluded, 60)
    assert engine.snapshot.stage is Stage.EXCLUDED
    assert engine.snapshot.reason is Reason.SOURCE_EXCLUDED
    assert engine.snapshot.observed_seconds == 0

    engine.tick(75)
    engine.observe(track(), 90)
    assert engine.snapshot.observed_seconds == 0
    assert engine.snapshot.candidate_count == 0


def test_unknown_source_counts_but_never_emits(engine: ObservationEngine) -> None:
    engine.observe(track(category=SourceCategory.UNKNOWN), 0)
    play_for(engine, 0, 150)

    snapshot = engine.snapshot
    assert snapshot.unknown_source_threshold_count == 1
    assert snapshot.candidate_count == 0
    assert snapshot.reason is Reason.SOURCE_UNKNOWN


def test_unknown_then_confirmed_keeps_elapsed_and_qualifies(
    engine: ObservationEngine,
) -> None:
    engine.observe(track(category=SourceCategory.UNKNOWN), 0)
    play_for(engine, 0, 60)

    engine.observe(track(), 60)
    assert engine.snapshot.observed_seconds == 60
    play_for(engine, 60, 150)

    assert engine.snapshot.candidate_count == 1
    assert engine.snapshot.unknown_source_threshold_count == 0


def test_confirmation_after_unknown_threshold_still_qualifies(
    engine: ObservationEngine,
) -> None:
    engine.observe(track(category=SourceCategory.UNKNOWN), 0)
    play_for(engine, 0, 150)
    assert engine.snapshot.unknown_source_threshold_count == 1

    engine.observe(track(), 150)

    assert engine.snapshot.candidate_count == 1


def test_unavailable_adds_no_time(engine: ObservationEngine) -> None:
    engine.observe(track(), 0)
    play_for(engine, 0, 30)

    engine.observe(UNAVAILABLE, 30)
    assert engine.snapshot.reason is Reason.UNAVAILABLE
    assert engine.snapshot.stage is Stage.SUSPENDED

    engine.observe(track(), 40)

    assert engine.snapshot.observed_seconds == 30


def test_missing_entity_is_reported(engine: ObservationEngine) -> None:
    engine.observe(track(None, status=PlaybackStatus.MISSING, duration=None), 0)

    assert engine.snapshot.reason is Reason.ENTITY_MISSING
    assert engine.snapshot.stage is Stage.WAITING


def test_clock_gap_is_discarded(engine: ObservationEngine) -> None:
    engine.observe(track(), 0)

    engine.tick(46)

    assert engine.snapshot.observed_seconds == 0
    assert engine.snapshot.clock_gap_count == 1
    assert engine.snapshot.candidate_count == 0
    assert engine.snapshot.reason is Reason.CLOCK_GAP
    assert engine.snapshot.stage is Stage.RESTART_AMBIGUOUS
    assert engine.snapshot.ambiguous_restart_count == 1


def test_backwards_clock_is_discarded(engine: ObservationEngine) -> None:
    engine.observe(track(), 100)

    engine.tick(90)
    assert engine.snapshot.observed_seconds == 0
    assert engine.snapshot.clock_gap_count == 1

    engine.tick(100)
    assert engine.snapshot.observed_seconds == 10


def test_late_tick_within_gap_counts_once(engine: ObservationEngine) -> None:
    engine.observe(track(), 0)
    play_for(engine, 0, 99)

    engine.tick(143)
    engine.tick(150)

    assert engine.snapshot.observed_seconds == 150
    assert engine.snapshot.candidate_count == 1


def test_transient_empty_metadata_keeps_play(engine: ObservationEngine) -> None:
    engine.observe(track(), 0)
    play_for(engine, 0, 60)

    engine.observe(EMPTY, 60)
    assert engine.snapshot.reason is Reason.METADATA_MISSING
    assert engine.snapshot.stage is Stage.SUSPENDED
    engine.tick(62)

    engine.observe(track(), 63)
    assert engine.snapshot.observed_seconds == 60
    play_for(engine, 63, 103)

    assert engine.snapshot.observed_seconds == 100
    assert engine.snapshot.candidate_count == 1


def test_group_join_same_track_keeps_play_other_track_starts_new(
    engine: ObservationEngine,
) -> None:
    engine.observe(track(position=10), 0)
    play_for(engine, 0, 60)

    engine.observe(track(position=120), 60)
    assert engine.snapshot.observed_seconds == 60

    engine.observe(track(TRACK_B, position=120), 70)
    assert engine.snapshot.observed_seconds == 0


def test_track_change_while_paused_starts_new_play(
    engine: ObservationEngine,
) -> None:
    engine.observe(track(), 0)
    play_for(engine, 0, 60)
    engine.observe(PAUSED, 60)

    engine.observe(track(TRACK_B, status=PlaybackStatus.PAUSED), 200)
    assert engine.snapshot.observed_seconds == 0

    engine.observe(track(TRACK_B), 900)
    play_for(engine, 900, 960)

    assert engine.snapshot.observed_seconds == 60


def test_stale_timestamp_never_stops_counting(engine: ObservationEngine) -> None:
    engine.observe(track(duration=600, position=10), 0)
    play_for(engine, 0, 90)

    engine.observe(track(duration=600, position=100), 90)
    play_for(engine, 90, 300)

    assert engine.snapshot.observed_seconds == 300
    assert engine.snapshot.seek_count == 0
    assert engine.snapshot.candidate_count == 1


def test_next_tick_is_bounded_by_heartbeat_and_threshold(
    engine: ObservationEngine,
) -> None:
    assert engine.seconds_until_next_tick() is None

    engine.observe(track(), 0)
    assert engine.seconds_until_next_tick() == 15

    play_for(engine, 0, 96)
    assert engine.seconds_until_next_tick() == 4

    engine.observe(PAUSED, 96)
    assert engine.seconds_until_next_tick() is None

    engine.observe(track(), 100)
    play_for(engine, 100, 104)
    assert engine.snapshot.candidate_count == 1
    assert engine.seconds_until_next_tick() == 15


def test_counters_saturate(
    engine: ObservationEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(observation, "MAX_COUNTER", 2)
    engine.observe(track(), 0)

    for second in (100, 200, 300, 400):
        engine.tick(second)

    assert engine.snapshot.clock_gap_count == 2


def test_snapshot_holds_no_track_identity(engine: ObservationEngine) -> None:
    sample = replace(track(), track_key=b"secret-hash-bytes")
    engine.observe(sample, 0)

    assert "secret-hash-bytes" not in repr(engine.snapshot)
