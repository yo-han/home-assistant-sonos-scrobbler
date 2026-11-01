"""Defensive reading of media_player state, without retaining metadata."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from custom_components.personal_sonos_scrobbler.normalize import (
    PlaybackStatus,
    SourceCategory,
    normalize_state,
)

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
TRACK: dict[str, Any] = {
    "media_artist": "Artist",
    "media_title": "Title",
    "media_album_name": "Album",
    "media_duration": 200,
    "media_position": 20,
    "media_position_updated_at": NOW,
}


@pytest.mark.parametrize(
    "overrides",
    [
        {"source": "TV"},
        {"source": "line-in"},
        {"source": "AirPlay"},
        {"media_channel": "Station"},
        {"media_content_type": "podcast"},
        {"media_content_id": "spotify:episode:abc"},
        {"media_content_id": "x-sonos-spotify:spotify:show:abc"},
        {"media_content_id": "x-file-cifs://nas/track.flac"},
    ],
)
def test_explicit_exclusion_beats_spotify_source(overrides: dict[str, Any]) -> None:
    attributes = {**TRACK, "source": "Spotify Connect", **overrides}

    sample = normalize_state("playing", attributes, NOW)

    assert sample.category is SourceCategory.EXCLUDED


def test_percent_encoded_episode_uri_is_excluded() -> None:
    attributes = {**TRACK, "media_content_id": "spotify%3Aepisode%3Aabc"}

    assert normalize_state("playing", attributes, NOW).category is (
        SourceCategory.EXCLUDED
    )


def test_bare_spotify_track_uri_is_confirmed() -> None:
    attributes = {**TRACK, "media_content_id": "spotify:track:4uLU6hMCjMI75M1A2tKUQC"}

    assert normalize_state("playing", attributes, NOW).category is (
        SourceCategory.SPOTIFY_CONFIRMED
    )


@pytest.mark.parametrize(
    "uri", ["spotify:track:", "x-sonos-spotify:spotify:playlist:abc", "https://a/b"]
)
def test_other_uris_are_not_spotify_track_evidence(uri: str) -> None:
    attributes = {**TRACK, "media_content_id": uri}

    assert normalize_state("playing", attributes, NOW).category is (
        SourceCategory.UNKNOWN
    )


def test_content_type_music_is_not_spotify_evidence() -> None:
    attributes = {**TRACK, "media_content_type": "music"}

    assert normalize_state("playing", attributes, NOW).category is (
        SourceCategory.UNKNOWN
    )


def test_missing_source_and_uri_is_unknown() -> None:
    sample = normalize_state("playing", TRACK, NOW)

    assert sample.category is SourceCategory.UNKNOWN
    assert sample.presence.content_id is False


@pytest.mark.parametrize(
    ("state", "status"),
    [
        (None, PlaybackStatus.MISSING),
        ("playing", PlaybackStatus.PLAYING),
        ("paused", PlaybackStatus.PAUSED),
        ("idle", PlaybackStatus.IDLE),
        ("unavailable", PlaybackStatus.UNAVAILABLE),
        ("unknown", PlaybackStatus.UNAVAILABLE),
    ],
)
def test_state_maps_to_status(state: str | None, status: PlaybackStatus) -> None:
    assert normalize_state(state, {}, NOW).status is status


@pytest.mark.parametrize(
    "bad",
    [float("nan"), float("inf"), True, -1, 10**9, [1], {"a": 1}, "x" * 513],
)
def test_malformed_values_are_absent(bad: Any) -> None:
    attributes = {
        "media_artist": bad,
        "media_title": "Title",
        "media_album_name": bad,
        "media_duration": bad,
        "media_position": bad,
        "media_content_id": bad,
        "queue_position": bad,
        "media_position_updated_at": bad,
    }

    sample = normalize_state("playing", attributes, NOW)

    assert sample.track_key is None
    assert sample.duration_seconds is None
    assert sample.position_seconds is None
    assert sample.queue_position is None
    assert sample.presence.artist is False
    assert sample.presence.album is False
    assert sample.presence.duration is False
    assert sample.presence.position is False
    assert sample.presence.content_id is False
    assert sample.presence.queue_position is False


@pytest.mark.parametrize(
    ("members", "present"),
    [
        (["media_player.a"], True),
        (["media_player.a"] * 32, True),
        (["media_player.a"] * 33, False),
        ([1, 2], False),
        ("media_player.a", False),
        ([], False),
    ],
)
def test_group_members_are_bounded(members: Any, present: bool) -> None:
    sample = normalize_state("playing", {"group_members": members}, NOW)

    assert sample.presence.group_members is present


def test_stale_position_timestamp_extrapolates_while_playing() -> None:
    attributes = {
        **TRACK,
        "media_position_updated_at": NOW - timedelta(seconds=90),
    }

    assert normalize_state("playing", attributes, NOW).position_seconds == 110


def test_extrapolation_is_capped_at_duration() -> None:
    attributes = {**TRACK, "media_position_updated_at": NOW - timedelta(hours=2)}

    assert normalize_state("playing", attributes, NOW).position_seconds == 200


def test_future_timestamp_does_not_rewind_position() -> None:
    attributes = {**TRACK, "media_position_updated_at": NOW + timedelta(seconds=30)}

    assert normalize_state("playing", attributes, NOW).position_seconds == 20


@pytest.mark.parametrize(
    "stamp",
    [datetime(2026, 10, 31, 11, 0), "not-a-date", None],
)
def test_unusable_timestamp_keeps_position_without_extrapolation(stamp: Any) -> None:
    attributes = {**TRACK, "media_position_updated_at": stamp}

    assert normalize_state("playing", attributes, NOW).position_seconds == 20


def test_iso_timestamp_string_is_accepted() -> None:
    stamp = (NOW - timedelta(seconds=5)).isoformat()
    attributes = {**TRACK, "media_position_updated_at": stamp}

    assert normalize_state("playing", attributes, NOW).position_seconds == 25


def test_position_is_not_extrapolated_while_paused() -> None:
    attributes = {**TRACK, "media_position_updated_at": NOW - timedelta(seconds=90)}

    assert normalize_state("paused", attributes, NOW).position_seconds == 20


def test_identical_track_key_ignores_case_and_album() -> None:
    first = normalize_state("playing", TRACK, NOW)
    second = normalize_state(
        "playing",
        {
            **TRACK,
            "media_artist": "ARTIST",
            "media_title": "title",
            "media_album_name": "Other Album",
        },
        NOW,
    )

    assert first.track_key is not None
    assert first.track_key == second.track_key


def test_track_key_separates_artist_from_title() -> None:
    first = normalize_state("playing", {"media_artist": "ab", "media_title": "c"}, NOW)
    second = normalize_state("playing", {"media_artist": "a", "media_title": "bc"}, NOW)

    assert first.track_key != second.track_key


@pytest.mark.parametrize("missing", ["media_artist", "media_title"])
def test_track_key_is_none_without_artist_or_title(missing: str) -> None:
    attributes = {key: value for key, value in TRACK.items() if key != missing}

    assert normalize_state("playing", attributes, NOW).track_key is None


def test_lone_surrogate_in_metadata_does_not_raise() -> None:
    sample = normalize_state("playing", {**TRACK, "media_title": "bad\ud800"}, NOW)

    assert sample.track_key is not None
