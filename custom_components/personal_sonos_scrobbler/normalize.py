"""Turn media_player state into a sample for the engine.

The sample identifies a track by hash. Readable artist, title and album travel
only in the optional, repr-hidden `metadata` snapshot and never reach
diagnostics or the sensor.
"""

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from urllib.parse import unquote

from .playback import TrackMetadata

MAX_STRING_LENGTH = 512
MAX_GROUP_MEMBERS = 32
MAX_MEDIA_SECONDS = 86_400
MAX_QUEUE_POSITION = 100_000
TRACK_KEY_BYTES = 16
# Control, surrogate and unassigned code points; every other category, including
# the ideographic space and the no-break space, is kept verbatim.
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cs", "Cn"})

SPOTIFY_CONNECT_SOURCE = "Spotify Connect"
EXCLUDED_SOURCES = frozenset({"tv", "line-in", "airplay"})
EXCLUDED_CONTENT_TYPES = frozenset(
    {"podcast", "episode", "video", "tvshow", "movie", "channel"}
)
# Radio, line-in, TV and library prefixes mirror soco.core.SOURCES in soco 0.31.2.
EXCLUDED_URI = re.compile(
    r"^(?:x-rincon-mp3radio:|x-sonosapi-stream:|x-sonosapi-radio:|x-sonosapi-hls:"
    r"|x-sonos-http:sonos|aac:|hls-radio:|x-rincon-stream:|x-sonos-htastream:"
    r"|x-file-cifs:|x-sonos-vli:.*,airplay:)"
    r"|spotify:(?:episode|show):",
    re.IGNORECASE,
)
SPOTIFY_TRACK_URI = re.compile(
    r"^(?:x-sonos-spotify:)?spotify:track:[A-Za-z0-9]+", re.IGNORECASE
)


class PlaybackStatus(StrEnum):
    """Playback state as reported by Home Assistant."""

    PLAYING = "playing"
    PAUSED = "paused"
    IDLE = "idle"
    UNAVAILABLE = "unavailable"
    MISSING = "missing"
    TOPOLOGY_UNCERTAIN = "topology_uncertain"


class SourceCategory(StrEnum):
    """What the public state proves about the audio source."""

    SPOTIFY_CONFIRMED = "spotify_confirmed"
    UNKNOWN = "unknown"
    EXCLUDED = "excluded"


@dataclass(frozen=True, slots=True)
class MetadataPresence:
    """Which usable fields the state carried, never their values."""

    artist: bool = False
    title: bool = False
    album: bool = False
    duration: bool = False
    position: bool = False
    content_id: bool = False
    queue_position: bool = False
    group_members: bool = False


@dataclass(frozen=True, slots=True)
class PlaybackSample:
    """One normalized state, identified by hash; readable metadata is optional."""

    status: PlaybackStatus
    category: SourceCategory
    track_key: bytes | None
    duration_seconds: float | None
    position_seconds: float | None
    queue_position: int | None
    presence: MetadataPresence
    metadata: TrackMetadata | None = field(default=None, repr=False)
    sampled_at: datetime | None = field(default=None, repr=False)


def normalize_state(
    state: str | None, attributes: Mapping[str, object], now_utc: datetime
) -> PlaybackSample:
    """Read a media_player state defensively."""
    status = _status(state)
    artist = _bounded_string(attributes.get("media_artist"))
    title = _bounded_string(attributes.get("media_title"))
    album = _bounded_string(attributes.get("media_album_name"))
    content_id = _bounded_string(attributes.get("media_content_id"))
    duration = _media_seconds(attributes.get("media_duration"))
    position = _media_seconds(attributes.get("media_position"))
    queue_position = _queue_position(attributes.get("queue_position"))

    return PlaybackSample(
        status=status,
        category=_source_category(attributes, content_id),
        track_key=_track_key(artist, title),
        duration_seconds=duration,
        position_seconds=_current_position(
            status,
            position,
            duration,
            attributes.get("media_position_updated_at"),
            now_utc,
        ),
        queue_position=queue_position,
        metadata=_metadata(attributes),
        sampled_at=now_utc,
        presence=MetadataPresence(
            artist=artist is not None,
            title=title is not None,
            album=album is not None,
            duration=duration is not None,
            position=position is not None,
            content_id=content_id is not None,
            queue_position=queue_position is not None,
            group_members=_has_group_members(attributes.get("group_members")),
        ),
    )


def _metadata(attributes: Mapping[str, object]) -> TrackMetadata | None:
    artist = music_text(attributes.get("media_artist"))
    title = music_text(attributes.get("media_title"))
    if artist is None or title is None:
        return None
    return TrackMetadata(
        artist=artist,
        title=title,
        album=music_text(attributes.get("media_album_name")),
    )


def music_text(value: object) -> str | None:
    """Return provider text verbatim, or None if empty, too long or unsafe."""
    if (
        not isinstance(value, str)
        or len(value) > MAX_STRING_LENGTH
        or not value.strip()
        or any(unicodedata.category(char) in _UNSAFE_CATEGORIES for char in value)
    ):
        return None
    return value


def _status(state: str | None) -> PlaybackStatus:
    match state:
        case None:
            return PlaybackStatus.MISSING
        case "playing":
            return PlaybackStatus.PLAYING
        case "paused":
            return PlaybackStatus.PAUSED
        case "unavailable" | "unknown":
            return PlaybackStatus.UNAVAILABLE
        case _:
            return PlaybackStatus.IDLE


def _source_category(
    attributes: Mapping[str, object], content_id: str | None
) -> SourceCategory:
    source = _bounded_string(attributes.get("source"))
    content_type = _bounded_string(attributes.get("media_content_type"))
    uri = unquote(content_id) if content_id else ""

    if (
        (source is not None and source.casefold() in EXCLUDED_SOURCES)
        or attributes.get("media_channel") not in (None, "")
        or (
            content_type is not None
            and content_type.casefold() in EXCLUDED_CONTENT_TYPES
        )
        or EXCLUDED_URI.search(uri) is not None
    ):
        return SourceCategory.EXCLUDED
    if source == SPOTIFY_CONNECT_SOURCE or SPOTIFY_TRACK_URI.match(uri) is not None:
        return SourceCategory.SPOTIFY_CONFIRMED
    return SourceCategory.UNKNOWN


def _track_key(artist: str | None, title: str | None) -> bytes | None:
    if artist is None or title is None:
        return None
    identity = f"{artist.casefold()}\0{title.casefold()}".encode(
        "utf-8", "surrogatepass"
    )
    return hashlib.blake2b(identity, digest_size=TRACK_KEY_BYTES).digest()


def _current_position(
    status: PlaybackStatus,
    position: float | None,
    duration: float | None,
    updated_at: object,
    now_utc: datetime,
) -> float | None:
    if position is None or status is not PlaybackStatus.PLAYING:
        return position
    extrapolated = position + _age_seconds(updated_at, now_utc)
    return extrapolated if duration is None else min(extrapolated, duration)


def _age_seconds(updated_at: object, now_utc: datetime) -> float:
    stamp = _aware_datetime(updated_at)
    if stamp is None:
        return 0.0
    age: timedelta = now_utc - stamp
    return max(age.total_seconds(), 0.0)


def _aware_datetime(value: object) -> datetime | None:
    if isinstance(value, str) and len(value) <= MAX_STRING_LENGTH:
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, datetime) and value.utcoffset() is not None:
        return value
    return None


def _bounded_string(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > MAX_STRING_LENGTH:
        return None
    return value.strip() or None


def _media_seconds(value: object) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not 0 <= value <= MAX_MEDIA_SECONDS
    ):
        return None
    return float(value)


def _queue_position(value: object) -> int | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 < value <= MAX_QUEUE_POSITION
    ):
        return None
    return value


def _has_group_members(value: object) -> bool:
    return (
        isinstance(value, list | tuple)
        and 0 < len(value) <= MAX_GROUP_MEMBERS
        and all(isinstance(member, str) for member in value)
    )
