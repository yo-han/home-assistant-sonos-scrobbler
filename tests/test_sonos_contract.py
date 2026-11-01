"""The normalizer must classify what the real Sonos integration publishes."""

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant.components.sonos.media_player import SonosMediaPlayerEntity
from homeassistant.components.sonos.speaker import SonosSpeaker
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.personal_sonos_scrobbler.normalize import (
    PlaybackSample,
    SourceCategory,
    normalize_state,
)
from tests.fixtures.sonos_states import (
    AIRPLAY_URI,
    LINE_IN_URI,
    RADIO_URI,
    SPOTIFY_CONNECT_URI,
    SPOTIFY_EPISODE_URI,
    SPOTIFY_QUEUE_URI,
    TV_URI,
    media_player_attributes,
    sonos_media,
)


def sample_for(hass: HomeAssistant, uri: str) -> PlaybackSample:
    attributes = media_player_attributes(sonos_media(hass, uri))
    return normalize_state("playing", attributes, dt_util.utcnow())


def test_contract_spotify_connect_is_confirmed(hass: HomeAssistant) -> None:
    media = sonos_media(hass, SPOTIFY_CONNECT_URI)

    assert media.source_name == "Spotify Connect"
    assert sample_for(hass, SPOTIFY_CONNECT_URI).category is (
        SourceCategory.SPOTIFY_CONFIRMED
    )


def test_contract_queue_spotify_track_uri_is_confirmed(hass: HomeAssistant) -> None:
    media = sonos_media(hass, SPOTIFY_QUEUE_URI)

    assert media.source_name is None
    sample = sample_for(hass, SPOTIFY_QUEUE_URI)
    assert sample.category is SourceCategory.SPOTIFY_CONFIRMED
    assert sample.queue_position == 3
    assert sample.duration_seconds == 210
    assert sample.track_key is not None


def test_contract_queue_episode_is_excluded(hass: HomeAssistant) -> None:
    assert sonos_media(hass, SPOTIFY_EPISODE_URI).source_name is None
    assert sample_for(hass, SPOTIFY_EPISODE_URI).category is SourceCategory.EXCLUDED


def test_contract_radio_is_excluded(hass: HomeAssistant) -> None:
    assert sonos_media(hass, RADIO_URI).source_name is None
    assert sample_for(hass, RADIO_URI).category is SourceCategory.EXCLUDED


def test_contract_tv_and_line_in_are_excluded(hass: HomeAssistant) -> None:
    assert sonos_media(hass, TV_URI).source_name == "TV"
    assert sonos_media(hass, LINE_IN_URI).source_name == "Line-in"
    assert sample_for(hass, TV_URI).category is SourceCategory.EXCLUDED
    assert sample_for(hass, LINE_IN_URI).category is SourceCategory.EXCLUDED


def test_contract_airplay_is_excluded(hass: HomeAssistant) -> None:
    assert sonos_media(hass, AIRPLAY_URI).source_name == "AirPlay"
    assert sample_for(hass, AIRPLAY_URI).category is SourceCategory.EXCLUDED


def regroup_speakers(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, *uids: str
) -> tuple[dict[str, SonosSpeaker], list[tuple[str, list[str]]]]:
    """Build real speakers that record each state write with its published list."""
    writes: list[tuple[str, list[str]]] = []
    data = SimpleNamespace(discovered={}, topology_condition=asyncio.Condition())
    speakers: dict[str, SonosSpeaker] = {}
    for uid in uids:
        entity_id = entity_registry.async_get_or_create(
            "media_player", "sonos", uid
        ).entity_id
        speaker = SonosSpeaker.__new__(SonosSpeaker)
        speaker.hass = hass
        speaker.data = data
        speaker.soco = MagicMock(uid=uid)
        speaker.uid = uid
        speaker.zone_name = uid
        speaker.coordinator = None
        speaker.sonos_group = [speaker]
        speaker.sonos_group_entities = [entity_id]
        speaker._group_members_missing = set()

        def record(entity_id: str = entity_id, speaker: SonosSpeaker = speaker) -> None:
            writes.append((entity_id, list(speaker.sonos_group_entities)))

        speaker.async_write_entity_states = record  # type: ignore[method-assign]
        speakers[uid] = speaker
        data.discovered[uid] = speaker
    return speakers, writes


def regroup_event(*uids: str) -> Any:
    return SimpleNamespace(zone_player_uui_ds_in_group=",".join(uids))


def entity_for(speaker: SonosSpeaker) -> SonosMediaPlayerEntity:
    """Read the real properties without the setup that needs a live speaker."""
    entity = SonosMediaPlayerEntity.__new__(SonosMediaPlayerEntity)
    entity.speaker = speaker
    return entity


def test_contract_follower_media_is_coordinator_media() -> None:
    leader = SimpleNamespace(media=object(), coordinator=None)
    follower = SimpleNamespace(media=object(), coordinator=leader)

    assert entity_for(cast(SonosSpeaker, follower)).media is leader.media
    assert entity_for(cast(SonosSpeaker, leader)).media is leader.media


async def test_contract_group_members_published_coordinator_first(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> None:
    speakers, writes = regroup_speakers(hass, entity_registry, "LEAD", "SIDE")
    lead, side = (speakers[uid].sonos_group_entities[0] for uid in ("LEAD", "SIDE"))

    await speakers["SIDE"].create_update_groups_coro(regroup_event("SIDE", "LEAD"))

    assert entity_for(speakers["LEAD"]).group_members == [side, lead]
    assert writes == [(side, [side, lead]), (lead, [side, lead])]


async def test_contract_regroup_writes_leader_before_follower(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> None:
    speakers, writes = regroup_speakers(hass, entity_registry, "A", "B")
    a, b = (speakers[uid].sonos_group_entities[0] for uid in ("A", "B"))

    await speakers["A"].create_update_groups_coro(regroup_event("A", "B"))

    assert writes == [(a, [a, b]), (b, [a, b])]


async def test_contract_split_writes_the_leaver_first_as_a_soloist(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> None:
    speakers, writes = regroup_speakers(hass, entity_registry, "A", "B", "C")
    a, b, c = (speakers[uid].sonos_group_entities[0] for uid in ("A", "B", "C"))
    await speakers["A"].create_update_groups_coro(regroup_event("A", "B", "C"))
    writes.clear()

    await speakers["A"].create_update_groups_coro(regroup_event("A", "C"))

    assert writes == [(b, [b]), (a, [a, c]), (c, [a, c])]


def test_contract_grouping_feature_is_supported() -> None:
    features = entity_for(MagicMock())._attr_supported_features

    assert MediaPlayerEntityFeature.GROUPING in features
