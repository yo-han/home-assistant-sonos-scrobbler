"""Independent regression checks for bounded metadata numbers."""

from datetime import UTC, datetime

import pytest

from custom_components.personal_sonos_scrobbler.normalize import normalize_state


@pytest.mark.parametrize(
    ("attribute", "normalized"),
    [("media_duration", "duration_seconds"), ("media_position", "position_seconds")],
)
def test_unbounded_integer_is_unknown_not_an_exception(
    attribute: str, normalized: str
) -> None:
    """Reject out-of-range integers before any conversion to a C double."""
    sample = normalize_state(
        "playing",
        {"source": "Spotify Connect", attribute: 10**1000},
        datetime(2026, 10, 31, 12, 0, tzinfo=UTC),
    )
    assert getattr(sample, normalized) is None
