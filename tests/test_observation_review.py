"""Independent checks for conservative play boundaries and bounded observation."""

from datetime import UTC, datetime, timedelta

from custom_components.personal_sonos_scrobbler.normalize import (
    PlaybackSample,
    normalize_state,
)
from custom_components.personal_sonos_scrobbler.observation import (
    ObservationEngine,
    Stage,
)

START = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def sample(position: int = 0, offset: int = 0) -> PlaybackSample:
    """Build one synthetic Spotify play without any real listening data."""
    now = START + timedelta(seconds=offset)
    return normalize_state(
        "playing",
        {
            "source": "Spotify Connect",
            "media_artist": "Synthetic Artist",
            "media_title": "Synthetic Track",
            "media_duration": 120,
            "media_position": position,
            "media_position_updated_at": now,
            "queue_position": 1,
        },
        now,
    )


def ambiguous_engine() -> ObservationEngine:
    engine = ObservationEngine()
    engine.observe(sample(), 0)
    for now in (15, 30, 45):
        engine.tick(now)
    # Same metadata and queue entry: seek-to-start and a repeat look identical.
    engine.observe(sample(position=0, offset=45), 45)
    assert engine.snapshot.stage is Stage.RESTART_AMBIGUOUS
    return engine


def test_ambiguous_restart_does_not_join_two_subthreshold_plays() -> None:
    engine = ambiguous_engine()
    engine.tick(60)
    assert engine.snapshot.candidate_count == 0
    assert engine.snapshot.stage is Stage.RESTART_AMBIGUOUS


def test_ambiguous_restart_cannot_schedule_a_zero_delay_loop() -> None:
    engine = ambiguous_engine()
    engine.tick(60)
    delay = engine.seconds_until_next_tick()
    assert delay is None or delay > 0


def test_a_stuck_play_keeps_the_displayed_observation_bounded() -> None:
    engine = ObservationEngine()
    engine.observe(sample(), 0)
    for now in range(15, 200_000, 15):
        engine.tick(now)
    assert 0 <= engine.snapshot.observed_seconds <= 86_400
    assert engine.snapshot.candidate_count == 1
