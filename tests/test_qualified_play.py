"""The pure engine emits one immutable qualified play per qualifying occurrence."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import count

import pytest

from custom_components.personal_sonos_scrobbler.normalize import (
    MetadataPresence,
    PlaybackSample,
    PlaybackStatus,
    SourceCategory,
    normalize_state,
)
from custom_components.personal_sonos_scrobbler.observation import ObservationEngine
from custom_components.personal_sonos_scrobbler.playback import (
    QualifiedPlay,
    TrackMetadata,
)

KEY_A = b"a" * 16
KEY_B = b"b" * 16
T0 = datetime(2031, 3, 4, 12, 0, 0, tzinfo=UTC)
META_A = TrackMetadata(artist="canary-artist-a", title="canary-title-a", album="alb-a")
META_B = TrackMetadata(artist="canary-artist-b", title="canary-title-b")


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def sample(
    key: bytes | None = KEY_A,
    metadata: TrackMetadata | None = META_A,
    *,
    when: float = 0.0,
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
        metadata=metadata,
        sampled_at=at(when),
    )


@pytest.fixture
def engine() -> ObservationEngine:
    ids = count(1)
    return ObservationEngine(new_occurrence_id=lambda: f"occ-{next(ids)}")


def run(engine: ObservationEngine, start: float, stop: float) -> list[QualifiedPlay]:
    """Advance through heartbeats and collect every emitted event."""
    events: list[QualifiedPlay] = []
    now = start
    while now < stop:
        now = min(now + 15, stop)
        events.extend(engine.tick(now))
    return events


def test_a_qualifying_during_accrual_before_b_keeps_a_metadata(
    engine: ObservationEngine,
) -> None:
    assert engine.observe(sample(position=20), 0) == ()
    assert run(engine, 0, 90) == []

    events = engine.observe(sample(KEY_B, META_B, when=105), 105)

    assert len(events) == 1
    event = events[0]
    assert event.metadata == META_A
    assert event.track_key == KEY_A
    assert event.started_at == at(-20)
    assert event.qualified_at == at(100)
    assert event.duration_seconds == 200
    assert event.occurrence_id == "occ-1"
    assert engine.active_occurrence is not None
    assert engine.active_occurrence.occurrence_id == "occ-2"
    assert engine.active_occurrence.track_key == KEY_B


def test_threshold_on_timer_emits_once(engine: ObservationEngine) -> None:
    engine.observe(sample(), 0)

    assert run(engine, 0, 99) == []
    events = engine.tick(100)

    assert len(events) == 1
    assert events[0].qualified_at == at(100)
    assert run(engine, 100, 400) == []
    assert engine.snapshot.candidate_count == 1


def test_repeated_update_emits_once(engine: ObservationEngine) -> None:
    engine.observe(sample(), 0)
    run(engine, 0, 99)

    first = engine.observe(sample(when=100), 100)
    again = [engine.observe(sample(when=100 + n), 100 + n) for n in range(1, 6)]

    assert len(first) == 1
    assert all(events == () for events in again)


def test_pause_resume_emits_once(engine: ObservationEngine) -> None:
    engine.observe(sample(), 0)
    run(engine, 0, 60)
    assert engine.observe(sample(when=60, status=PlaybackStatus.PAUSED), 60) == ()
    assert run(engine, 60, 600) == []

    assert engine.observe(sample(when=600), 600) == ()
    events = run(engine, 600, 700)

    assert len(events) == 1
    assert events[0].qualified_at == at(640)
    assert events[0].occurrence_id == "occ-1"
    assert run(engine, 700, 900) == []


def test_source_change_keeps_original_snapshot(engine: ObservationEngine) -> None:
    engine.observe(sample(), 0)
    run(engine, 0, 45)
    changed = replace(
        META_A, artist="Canary-Artist-A", title="Other", album="other-album"
    )
    engine.observe(
        sample(metadata=changed, when=45, category=SourceCategory.UNKNOWN), 45
    )
    run(engine, 45, 60)
    engine.observe(sample(metadata=changed, when=60), 60)

    events = run(engine, 60, 100)

    assert len(events) == 1
    assert events[0].metadata == META_A


def test_seek_never_adds_credit(engine: ObservationEngine) -> None:
    engine.observe(sample(position=0), 0)
    run(engine, 0, 10)

    assert engine.observe(sample(when=10, position=150), 10) == ()
    assert engine.snapshot.observed_seconds == 10
    assert engine.snapshot.seek_count == 1

    assert run(engine, 10, 99) == []
    events = engine.tick(100)
    assert len(events) == 1
    assert events[0].started_at == at(0)


def test_start_anchor_survives_pause(engine: ObservationEngine) -> None:
    engine.observe(sample(position=20), 0)
    before = engine.active_occurrence
    engine.observe(sample(when=30, status=PlaybackStatus.PAUSED, position=50), 30)
    engine.observe(sample(when=600, position=50), 600)

    after = engine.active_occurrence

    assert before is not None
    assert after == before
    assert after.started_at == at(-20)


def test_start_is_sampled_at_minus_position_on_attach(
    engine: ObservationEngine,
) -> None:
    engine.observe(sample(when=500, position=150), 0)
    started = engine.active_occurrence
    assert started is not None
    assert started.started_at == at(350)

    engine.observe(sample(KEY_B, META_B, when=520), 20)
    assert engine.active_occurrence is not None
    assert engine.active_occurrence.started_at == at(520)


def test_album_is_filled_in_only_while_missing_and_before_emission(
    engine: ObservationEngine,
) -> None:
    bare = TrackMetadata(artist=META_A.artist, title=META_A.title)
    engine.observe(sample(metadata=bare), 0)
    engine.observe(sample(metadata=META_A, when=5), 5)
    engine.observe(sample(metadata=replace(META_A, album="later"), when=10), 10)
    run(engine, 10, 99)

    events = engine.tick(100)

    assert events[0].metadata.album == "alb-a"
    engine.observe(sample(metadata=replace(META_A, album="after"), when=101), 101)
    assert engine.active_occurrence is not None


def test_missing_metadata_counts_a_candidate_without_an_event(
    engine: ObservationEngine,
) -> None:
    engine.observe(replace(sample(), metadata=None, sampled_at=None), 0)

    assert run(engine, 0, 150) == []
    assert engine.snapshot.candidate_count == 1


def test_suppress_until_transition_blocks_only_the_current_track(
    engine: ObservationEngine,
) -> None:
    engine.suppress_until_transition(KEY_A)
    engine.observe(sample(), 0)

    assert run(engine, 0, 150) == []
    assert engine.observe(sample(KEY_B, META_B, when=150), 150) == ()
    assert len(run(engine, 150, 260)) == 1


def test_proven_restart_of_a_suppressed_track_emits_a_new_occurrence(
    engine: ObservationEngine,
) -> None:
    engine.suppress_until_transition(KEY_A)
    engine.observe(sample(position=0, queue=1), 0)
    first = engine.active_occurrence
    run(engine, 0, 30)

    assert engine.observe(sample(when=30, position=1, queue=2), 30) == ()
    restarted = engine.active_occurrence
    events = run(engine, 30, 200)

    assert first is not None
    assert restarted is not None
    assert restarted.occurrence_id != first.occurrence_id
    assert len(events) == 1
    assert events[0].occurrence_id == restarted.occurrence_id


def test_suppression_survives_reattach_after_end_play(
    engine: ObservationEngine,
) -> None:
    engine.suppress_until_transition(KEY_A)
    engine.observe(sample(), 0)
    engine.end_play(10)

    engine.observe(sample(when=20), 20)

    assert run(engine, 20, 200) == []


def test_suppression_survives_reattach_after_excluded_source(
    engine: ObservationEngine,
) -> None:
    engine.suppress_until_transition(KEY_A)
    engine.observe(sample(), 0)
    engine.observe(sample(when=10, category=SourceCategory.EXCLUDED), 10)

    engine.observe(sample(when=20), 20)

    assert run(engine, 20, 200) == []


def test_suppression_survives_unproven_position_restart(
    engine: ObservationEngine,
) -> None:
    engine.suppress_until_transition(KEY_A)
    engine.observe(sample(position=0), 0)
    run(engine, 0, 30)

    engine.observe(sample(when=30, position=1), 30)

    assert run(engine, 30, 250) == []


def test_end_play_returns_events_and_clears_the_occurrence(
    engine: ObservationEngine,
) -> None:
    engine.observe(sample(), 0)
    run(engine, 0, 90)

    events = engine.end_play(105)

    assert len(events) == 1
    assert engine.active_occurrence is None


def test_ideographic_space_and_nbsp_are_preserved_verbatim() -> None:
    state = normalize_state(
        "playing",
        {
            "media_artist": "Artist\u3000Name\u00a0",
            "media_title": "\u00a0Title\u3000",
            "media_album_name": "Album\u3000",
        },
        T0,
    )

    assert state.metadata == TrackMetadata(
        artist="Artist\u3000Name\u00a0", title="\u00a0Title\u3000", album="Album\u3000"
    )
    assert state.sampled_at == T0


UNSAFE = ["bad\ud800", "line\nbreak", "tab\there", "nul\x00", "\u0378", "", "  "]


@pytest.mark.parametrize("bad", [*UNSAFE, "x" * 513])
def test_unsafe_required_values_yield_no_metadata(bad: str) -> None:
    attributes = {"media_artist": "Artist", "media_title": "Title"}

    for name in ("media_artist", "media_title"):
        state = normalize_state("playing", {**attributes, name: bad}, T0)
        assert state.metadata is None


def test_lone_surrogate_yields_no_metadata() -> None:
    state = normalize_state(
        "playing", {"media_artist": "Artist", "media_title": "bad\ud800"}, T0
    )

    assert state.metadata is None
    assert state.track_key is not None


def test_unsafe_album_is_dropped_but_keeps_the_track() -> None:
    state = normalize_state(
        "playing",
        {"media_artist": "A", "media_title": "T", "media_album_name": "x\ud800"},
        T0,
    )

    assert state.metadata == TrackMetadata(artist="A", title="T")


def test_snapshot_repr_holds_no_metadata(engine: ObservationEngine) -> None:
    engine.observe(sample(position=20), 0)
    events = run(engine, 0, 100)
    outputs = [
        repr(engine.snapshot),
        repr(engine.active_occurrence),
        repr(engine._play),
        repr(META_A),
        repr(events[0]),
    ]

    for text in outputs:
        for fragment in ("canary-artist", "canary-title", "alb-a", "2031", "aaaa"):
            assert fragment not in text
    for fragment in ("canary-artist", "canary-title", "alb-a", "2031"):
        assert fragment not in repr(sample())
