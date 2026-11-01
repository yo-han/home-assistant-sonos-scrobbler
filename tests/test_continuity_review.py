"""Do not join potentially separate plays across an unobserved interval."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from custom_components.personal_sonos_scrobbler.normalize import (
    PlaybackSample,
    normalize_state,
)
from custom_components.personal_sonos_scrobbler.observation import ObservationEngine


def _sample(state: str = "playing") -> PlaybackSample:
    return normalize_state(
        state,
        {
            "source": "Spotify Connect",
            "media_artist": "Synthetic Artist",
            "media_title": "Synthetic Track",
            "media_duration": 120,
        },
        datetime(2026, 10, 31, 12, 0, tzinfo=UTC),
    )


def _almost_qualified() -> ObservationEngine:
    engine = ObservationEngine()
    engine.observe(_sample(), 0)
    for now in (15, 30, 45):
        engine.tick(now)
    assert engine.snapshot.candidate_count == 0
    return engine


def test_recovery_without_play_identity_cannot_join_two_short_plays() -> None:
    engine = _almost_qualified()
    engine.observe(_sample("unavailable"), 45)
    engine.observe(_sample(), 150)
    engine.tick(165)
    # A new occurrence of the same song could have started while it was unavailable.
    assert engine.snapshot.candidate_count == 0


def test_a_discarded_clock_gap_cannot_silently_resume_old_play_credit() -> None:
    engine = _almost_qualified()
    engine.tick(150)
    engine.tick(165)
    assert engine.snapshot.clock_gap_count == 1
    assert engine.snapshot.candidate_count == 0


@pytest.mark.parametrize("position", [None, 50.0])
def test_long_metadata_gap_cannot_join_two_short_plays(position: float | None) -> None:
    engine = _almost_qualified()
    missing = normalize_state(
        "playing", {"source": "Spotify Connect"}, datetime.now(UTC)
    )
    engine.observe(missing, 45)
    engine.observe(missing, 345)
    engine.observe(replace(_sample(), position_seconds=position), 645)
    engine.tick(660)
    assert engine.snapshot.candidate_count == 0
    assert engine.snapshot.ambiguous_restart_count == 1
    assert engine.seconds_until_next_tick() == 15


def test_short_metadata_gap_keeps_credit_without_counting_the_gap() -> None:
    engine = _almost_qualified()
    missing = normalize_state(
        "playing", {"source": "Spotify Connect"}, datetime.now(UTC)
    )
    engine.observe(missing, 45)
    engine.observe(_sample(), 75)
    assert engine.snapshot.observed_seconds == 45
    engine.tick(90)
    assert engine.snapshot.candidate_count == 1
