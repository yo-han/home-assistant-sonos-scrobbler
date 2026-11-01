"""Build media_player attributes from the real Home Assistant Sonos code."""

from typing import Any
from unittest.mock import MagicMock

from homeassistant.components.media_player import (
    MediaPlayerEntityStateAttribute as Attr,
)
from homeassistant.components.media_player import MediaType
from homeassistant.components.sonos.const import ATTR_QUEUE_POSITION
from homeassistant.components.sonos.media import SonosMedia
from homeassistant.core import HomeAssistant
from soco.core import SoCo

SPOTIFY_CONNECT_URI = "x-sonos-vli:RINCON_ABC:2,spotify:6ed3f0"
SPOTIFY_QUEUE_URI = (
    "x-sonos-spotify:spotify%3atrack%3a4uLU6hMCjMI75M1A2tKUQC?sid=12&flags=8232&sn=2"
)
SPOTIFY_EPISODE_URI = "x-sonos-spotify:spotify%3aepisode%3aabc123?sid=12"
RADIO_URI = "x-sonosapi-stream:s1234?sid=254&flags=8224"
TV_URI = "x-sonos-htastream:RINCON_ABC:spdif"
LINE_IN_URI = "x-rincon-stream:RINCON_ABC"
AIRPLAY_URI = "x-sonos-vli:RINCON_ABC:1,airplay:xyz"


def sonos_media(hass: HomeAssistant, uri: str) -> SonosMedia:
    """Run the real metadata parser against a mocked speaker."""
    speaker = MagicMock()
    speaker.music_source_from_uri = SoCo.music_source_from_uri
    speaker.get_current_track_info.return_value = {
        "uri": uri,
        "artist": "Canary Artist",
        "album": "Canary Album",
        "title": "Canary Title",
        "album_art": "",
        "playlist_position": "3",
        "duration": "0:03:30",
        "position": "0:00:10",
    }
    media = SonosMedia(hass, speaker)
    media.playback_status = "PLAYING"
    media.set_basic_track_info(update_position=True)
    return media


def media_player_attributes(media: SonosMedia) -> dict[str, Any]:
    """Expose media as SonosMediaPlayerEntity publishes it in the state machine."""
    attributes: dict[str, Any] = {
        Attr.MEDIA_CONTENT_ID: media.uri,
        Attr.MEDIA_CONTENT_TYPE: MediaType.MUSIC,
        Attr.MEDIA_DURATION: int(media.duration) if media.duration else None,
        Attr.MEDIA_POSITION: media.position,
        Attr.MEDIA_POSITION_UPDATED_AT: media.position_updated_at,
        Attr.MEDIA_TITLE: media.title or None,
        Attr.MEDIA_ARTIST: media.artist or None,
        Attr.MEDIA_ALBUM_NAME: media.album_name or None,
        Attr.MEDIA_CHANNEL: media.channel or None,
        Attr.INPUT_SOURCE: media.source_name or None,
        ATTR_QUEUE_POSITION: media.queue_position,
    }
    return {str(name): value for name, value in attributes.items() if value is not None}
