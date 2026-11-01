"""The current-speaker sensor names the room of the current track, nothing else."""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.personal_sonos_scrobbler.const import DOMAIN
from tests.helpers import (
    HEARTBEAT,
    SETTLE,
    Advance,
    Room,
    active_timers,
    is_clear,
    shown,
)
from tests.lastfm_server import LastFmServer


@pytest.fixture
async def speaker_id(
    hass: HomeAssistant,
    loaded_pilot: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> AsyncIterator[str]:
    entity_id = entity_registry.async_get_entity_id(
        "sensor", DOMAIN, f"{loaded_pilot.entry_id}_current_speaker"
    )
    assert entity_id is not None
    yield entity_id


async def test_one_more_sensor_on_the_existing_device(
    hass: HomeAssistant,
    loaded_pilot: MockConfigEntry,
    speaker_id: str,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
) -> None:
    entry = entity_registry.async_get(speaker_id)

    assert entry is not None
    assert entry.disabled_by is None
    assert entry.entity_category is None
    assert entry.unique_id == f"{loaded_pilot.entry_id}_current_speaker"
    assert entry.translation_key == "current_speaker"
    assert entry.original_icon == "mdi:speaker"
    assert entry.original_device_class is None
    state = hass.states.get(speaker_id)
    assert state is not None
    assert state.attributes["icon"] == "mdi:speaker"
    entries = er.async_entries_for_config_entry(entity_registry, loaded_pilot.entry_id)
    assert sorted(
        item.unique_id.removeprefix(f"{loaded_pilot.entry_id}_")
        for item in entries
        if not item.unique_id.startswith(f"{loaded_pilot.entry_id}_speaker_")
    ) == [
        "current_speaker",
        "current_track",
        "delivery",
        "now_playing",
        "scrobbling",
        "status",
    ]
    assert {item.device_id for item in entries} == {entry.device_id}
    devices = dr.async_entries_for_config_entry(device_registry, loaded_pilot.entry_id)
    assert [device.id for device in devices] == [entry.device_id]


async def test_names_the_room_with_both_switches_off_and_no_link(
    hass: HomeAssistant,
    speaker_id: str,
    room: Room,
    lastfm_server: LastFmServer,
) -> None:
    assert is_clear(hass, speaker_id)

    room.play(
        friendly_name="Kitchen",
        media_content_id="x-sonos-spotify:spotify%3atrack%3acanaryuri?sid=12",
    )
    await hass.async_block_till_done()

    assert shown(hass, speaker_id) == ("Kitchen", {"entity_id": room.entity_id})
    assert lastfm_server.requests == []


@pytest.mark.parametrize("name", [None, "", "   "], ids=["none", "empty", "blank"])
async def test_falls_back_to_the_entity_id_without_a_friendly_name(
    hass: HomeAssistant, speaker_id: str, room: Room, name: str | None
) -> None:
    room.play(friendly_name=name)
    await hass.async_block_till_done()

    assert shown(hass, speaker_id) == (room.entity_id, {"entity_id": room.entity_id})


async def test_a_long_name_is_bounded_in_the_state(
    hass: HomeAssistant,
    speaker_id: str,
    room: Room,
    caplog: pytest.LogCaptureFixture,
) -> None:
    name = "Küche🎵" * 100
    room.play(friendly_name=name)
    await hass.async_block_till_done()

    state, _attributes = shown(hass, speaker_id)

    assert state == name[:255]
    assert "exceeds the maximum" not in caplog.text


async def test_a_rename_shows_without_a_new_track(
    hass: HomeAssistant, speaker_id: str, room: Room
) -> None:
    room.play(friendly_name="Kitchen")
    await hass.async_block_till_done()

    room.play(friendly_name="Renamed room")
    await hass.async_block_till_done()

    assert shown(hass, speaker_id)[0] == "Renamed room"


@pytest.mark.parametrize(
    ("state", "attributes"),
    [
        ("paused", {}),
        (STATE_UNAVAILABLE, {}),
        ("playing", {"source": "TV", "media_title": "TV"}),
        ("playing", {"media_title": None}),
    ],
    ids=["paused", "unavailable", "source_lost", "title_lost"],
)
async def test_nothing_stale_stays_on_show(
    hass: HomeAssistant,
    speaker_id: str,
    room: Room,
    state: str,
    attributes: dict[str, Any],
) -> None:
    room.play(friendly_name="Kitchen")
    await hass.async_block_till_done()
    assert not is_clear(hass, speaker_id)

    room.play("Title", state, friendly_name="Kitchen", **attributes)
    await hass.async_block_till_done()

    assert is_clear(hass, speaker_id)


async def test_only_meaningful_changes_write_a_state(
    hass: HomeAssistant, speaker_id: str, room: Room, advance: Advance
) -> None:
    room.play(friendly_name="Kitchen")
    await hass.async_block_till_done()
    written: list[Event[Any]] = []

    @callback
    def record(event: Event[Any]) -> None:
        if event.data["entity_id"] == speaker_id:
            written.append(event)

    remove = hass.bus.async_listen("state_changed", record)
    room.play("Next track", friendly_name="Kitchen")
    await hass.async_block_till_done()
    for tick in range(1, 6):
        room.play(
            "Next track",
            friendly_name="Kitchen",
            position=tick * HEARTBEAT,
            volume_level=0.1 * tick,
        )
        await advance(HEARTBEAT)
    remove()

    assert written == []


async def test_a_group_shows_its_coordinator_for_a_selected_follower(
    hass: HomeAssistant,
    pilot_entry: MockConfigEntry,
    sonos_player: er.RegistryEntry,
    entity_registry: er.EntityRegistry,
    advance: Advance,
) -> None:
    coordinator = Room(
        hass,
        entity_registry.async_get_or_create(
            "media_player", "sonos", "RINCON_0", suggested_object_id="a_lounge"
        ),
    )
    follower = Room(hass, sonos_player)
    members = (coordinator, follower)
    assert await hass.config_entries.async_setup(pilot_entry.entry_id)
    await hass.async_block_till_done()
    sensor = entity_registry.async_get_entity_id(
        "sensor", DOMAIN, f"{pilot_entry.entry_id}_current_speaker"
    )
    assert sensor is not None

    coordinator.play(members=members, friendly_name="Lounge")
    follower.play(members=members, friendly_name="Kitchen")
    await hass.async_block_till_done()
    await advance(SETTLE)
    await hass.async_block_till_done()

    assert shown(hass, sensor) == ("Lounge", {"entity_id": coordinator.entity_id})
    await hass.config_entries.async_unload(pilot_entry.entry_id)
    await hass.async_block_till_done()


async def test_it_follows_the_same_room_as_the_current_track(
    hass: HomeAssistant,
    hall: Room,
    speaker_id: str,
    room: Room,
    advance: Advance,
) -> None:
    room.play("Old room", friendly_name="Kitchen")
    await hass.async_block_till_done()
    await advance(SETTLE)
    hall.play("New room", friendly_name="Hall")
    await hass.async_block_till_done()
    await advance(SETTLE)
    assert shown(hass, speaker_id) == ("Hall", {"entity_id": hall.entity_id})

    room.play("Old room", friendly_name="Kitchen", position=20, volume_level=0.2)
    await hass.async_block_till_done()
    await advance(HEARTBEAT)
    assert shown(hass, speaker_id)[0] == "Hall"

    hall.play("New room", "paused", friendly_name="Hall")
    await hass.async_block_till_done()
    assert shown(hass, speaker_id) == ("Kitchen", {"entity_id": room.entity_id})

    room.play("Old room", "paused", friendly_name="Kitchen")
    await hass.async_block_till_done()
    assert is_clear(hass, speaker_id)


async def test_a_reload_restores_nothing_and_leaves_no_listener_or_timer(
    hass: HomeAssistant,
    loaded_pilot: MockConfigEntry,
    speaker_id: str,
    room: Room,
) -> None:
    room.play(friendly_name="Kitchen")
    await hass.async_block_till_done()
    runtime = loaded_pilot.runtime_data

    assert await hass.config_entries.async_unload(loaded_pilot.entry_id)
    await hass.async_block_till_done()

    assert runtime._listeners == set()
    assert active_timers(hass) == 0
    state = hass.states.get(speaker_id)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE
    assert "entity_id" not in state.attributes

    room.play("Title", "paused", friendly_name="Kitchen")
    assert await hass.config_entries.async_setup(loaded_pilot.entry_id)
    await hass.async_block_till_done()

    assert is_clear(hass, speaker_id)


async def test_the_room_stays_out_of_the_logs_and_no_music_reaches_it(
    hass: HomeAssistant,
    speaker_id: str,
    room: Room,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=f"custom_components.{DOMAIN}")
    room.play("canary-title", media_artist="canary-artist", friendly_name="canary-room")
    await hass.async_block_till_done()

    state = hass.states.get(speaker_id)
    assert state is not None
    assert state.state == "canary-room"
    assert "canary-title" not in json.dumps(dict(state.attributes), default=str)
    assert "canary-artist" not in json.dumps(dict(state.attributes), default=str)
    assert "canary" not in caplog.text
