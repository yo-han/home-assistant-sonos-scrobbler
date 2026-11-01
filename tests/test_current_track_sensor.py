"""The current-track sensor through real states and synthetic Sonos entities."""

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
from custom_components.personal_sonos_scrobbler.diagnostics import (
    async_get_config_entry_diagnostics,
)
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

FORBIDDEN_ATTRIBUTES = (
    "media_content_id",
    "entity_picture",
    "media_image_url",
    "media_position",
    "media_position_updated_at",
    "uri",
    "credentials",
    "history",
)


@pytest.fixture
async def track_id(
    hass: HomeAssistant,
    loaded_pilot: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> AsyncIterator[str]:
    entity_id = entity_registry.async_get_entity_id(
        "sensor", DOMAIN, f"{loaded_pilot.entry_id}_current_track"
    )
    assert entity_id is not None
    yield entity_id


async def test_one_main_sensor_on_the_existing_device(
    hass: HomeAssistant,
    loaded_pilot: MockConfigEntry,
    track_id: str,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
) -> None:
    entry = entity_registry.async_get(track_id)

    assert entry is not None
    assert entry.disabled_by is None
    assert entry.entity_category is None
    assert entry.unique_id == f"{loaded_pilot.entry_id}_current_track"
    assert entry.translation_key == "current_track"
    devices = dr.async_entries_for_config_entry(device_registry, loaded_pilot.entry_id)
    assert [device.id for device in devices] == [entry.device_id]
    assert {
        item.device_id
        for item in er.async_entries_for_config_entry(
            entity_registry, loaded_pilot.entry_id
        )
    } == {entry.device_id}


async def test_shows_the_track_with_both_switches_off_and_no_link(
    hass: HomeAssistant,
    track_id: str,
    room: Room,
    lastfm_server: LastFmServer,
) -> None:
    assert is_clear(hass, track_id)

    room.play()
    await hass.async_block_till_done()

    assert shown(hass, track_id) == (
        "Synthetic Artist — Title",
        {
            "artist": "Synthetic Artist",
            "title": "Title",
            "album": "Synthetic Album",
            "duration": 200,
            "speaker": room.entity_id,
        },
    )
    assert lastfm_server.requests == []


async def test_attributes_carry_nothing_but_the_allowlist(
    hass: HomeAssistant, track_id: str, room: Room
) -> None:
    room.play(
        media_content_id="x-sonos-spotify:spotify%3atrack%3acanaryuri?sid=12",
        entity_picture="/api/media_player_proxy/canary-token",
        media_image_url="http://192.0.2.1/canary-image.jpg",
    )
    await hass.async_block_till_done()

    _state, attributes = shown(hass, track_id)

    assert set(attributes) == {"artist", "title", "album", "duration", "speaker"}
    assert not set(FORBIDDEN_ATTRIBUTES) & set(attributes)
    assert "canary" not in json.dumps(attributes)


async def test_a_track_change_replaces_it_and_album_and_duration_may_be_missing(
    hass: HomeAssistant, track_id: str, room: Room
) -> None:
    room.play("One")
    await hass.async_block_till_done()

    room.play("Two", media_album_name=None, media_duration=None)
    await hass.async_block_till_done()

    state, attributes = shown(hass, track_id)
    assert state == "Synthetic Artist — Two"
    assert attributes["album"] is None
    assert attributes["duration"] is None


@pytest.mark.parametrize(
    ("state", "attributes"),
    [
        ("paused", {}),
        ("idle", {}),
        (STATE_UNAVAILABLE, {}),
        ("playing", {"source": "TV", "media_title": "TV"}),
        ("playing", {"source": None, "media_content_id": None}),
    ],
    ids=["paused", "idle", "unavailable", "source_lost", "source_unknown"],
)
async def test_nothing_stale_stays_on_show(
    hass: HomeAssistant,
    track_id: str,
    room: Room,
    state: str,
    attributes: dict[str, Any],
) -> None:
    room.play()
    await hass.async_block_till_done()
    assert not is_clear(hass, track_id)

    room.play("Title", state, **attributes)
    await hass.async_block_till_done()

    assert is_clear(hass, track_id)


async def test_a_missing_title_clears_it_and_the_track_comes_back(
    hass: HomeAssistant, track_id: str, room: Room
) -> None:
    room.play()
    await hass.async_block_till_done()

    room.play(media_title=None)
    await hass.async_block_till_done()
    assert is_clear(hass, track_id)

    room.play()
    await hass.async_block_till_done()
    assert shown(hass, track_id)[0] == "Synthetic Artist — Title"


async def test_a_resume_shows_the_track_again(
    hass: HomeAssistant, track_id: str, room: Room
) -> None:
    room.play()
    await hass.async_block_till_done()
    room.play(state="paused", position=20)
    await hass.async_block_till_done()
    assert is_clear(hass, track_id)

    room.play(position=20)
    await hass.async_block_till_done()

    assert shown(hass, track_id)[0] == "Synthetic Artist — Title"


async def test_unknown_source_never_shows(
    hass: HomeAssistant, track_id: str, room: Room
) -> None:
    room.play(source=None)
    await hass.async_block_till_done()

    assert is_clear(hass, track_id)


async def test_long_unicode_is_bounded_in_state_and_whole_in_attributes(
    hass: HomeAssistant,
    track_id: str,
    room: Room,
    caplog: pytest.LogCaptureFixture,
) -> None:
    artist = "Ünï🎵" * 100
    title = "標題" * 200
    room.play(title, media_artist=artist)
    await hass.async_block_till_done()

    state, attributes = shown(hass, track_id)

    assert len(state) == 255
    assert state == f"{artist} — {title}"[:255]
    assert attributes["artist"] == artist
    assert attributes["title"] == title
    assert "exceeds the maximum" not in caplog.text


async def test_metadata_past_the_bound_never_shows(
    hass: HomeAssistant, track_id: str, room: Room
) -> None:
    room.play("x" * 513)
    await hass.async_block_till_done()

    assert is_clear(hass, track_id)


async def test_only_meaningful_changes_write_a_state(
    hass: HomeAssistant, track_id: str, room: Room, advance: Advance
) -> None:
    room.play()
    await hass.async_block_till_done()
    written: list[Event[Any]] = []

    @callback
    def record(event: Event[Any]) -> None:
        if event.data["entity_id"] == track_id:
            written.append(event)

    remove = hass.bus.async_listen("state_changed", record)
    for tick in range(1, 6):
        room.play(position=tick * HEARTBEAT, volume_level=0.1 * tick)
        await advance(HEARTBEAT)
    remove()

    assert written == []


async def test_one_group_is_one_track_from_its_coordinator(
    hass: HomeAssistant, hall: Room, track_id: str, room: Room, advance: Advance
) -> None:
    members = (room, hall)
    room.play(members=members)
    hall.play(members=members)
    await hass.async_block_till_done()
    await advance(SETTLE)
    await hass.async_block_till_done()

    state, attributes = shown(hass, track_id)

    assert state == "Synthetic Artist — Title"
    assert attributes["speaker"] == room.entity_id


async def test_a_selected_follower_shows_its_unselected_coordinator(
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
    track = entity_registry.async_get_entity_id(
        "sensor", DOMAIN, f"{pilot_entry.entry_id}_current_track"
    )
    assert track is not None

    coordinator.play(members=members)
    follower.play(members=members)
    await hass.async_block_till_done()
    await advance(SETTLE)
    await hass.async_block_till_done()

    assert shown(hass, track)[1]["speaker"] == coordinator.entity_id
    await hass.config_entries.async_unload(pilot_entry.entry_id)
    await hass.async_block_till_done()


async def test_the_newest_room_wins_and_static_updates_cannot_take_it_back(
    hass: HomeAssistant, hall: Room, track_id: str, room: Room, advance: Advance
) -> None:
    room.play("Old room")
    await hass.async_block_till_done()
    await advance(SETTLE)
    hall.play("New room")
    await hass.async_block_till_done()
    await advance(SETTLE)
    assert shown(hass, track_id)[1]["speaker"] == hall.entity_id

    room.play("Old room", position=20, volume_level=0.2)
    await hass.async_block_till_done()
    await advance(HEARTBEAT)

    assert shown(hass, track_id)[0] == "Synthetic Artist — New room"


async def test_a_track_change_in_the_older_room_takes_the_display(
    hass: HomeAssistant, hall: Room, track_id: str, room: Room, advance: Advance
) -> None:
    room.play("Old room")
    await hass.async_block_till_done()
    await advance(SETTLE)
    hall.play("New room")
    await hass.async_block_till_done()
    await advance(SETTLE)

    room.play("Newest track")
    await hass.async_block_till_done()

    state, attributes = shown(hass, track_id)
    assert state == "Synthetic Artist — Newest track"
    assert attributes["speaker"] == room.entity_id


async def test_the_other_room_shows_when_the_newest_one_stops(
    hass: HomeAssistant, hall: Room, track_id: str, room: Room, advance: Advance
) -> None:
    room.play("Old room")
    await hass.async_block_till_done()
    await advance(SETTLE)
    hall.play("New room")
    await hass.async_block_till_done()
    await advance(SETTLE)

    hall.play("New room", "paused")
    await hass.async_block_till_done()

    assert shown(hass, track_id)[0] == "Synthetic Artist — Old room"

    room.play("Old room", "paused")
    await hass.async_block_till_done()

    assert is_clear(hass, track_id)


async def test_a_reload_restores_nothing_and_leaves_no_listener_or_timer(
    hass: HomeAssistant,
    loaded_pilot: MockConfigEntry,
    track_id: str,
    room: Room,
) -> None:
    room.play()
    await hass.async_block_till_done()
    runtime = loaded_pilot.runtime_data
    assert runtime.current_track is not None

    assert await hass.config_entries.async_unload(loaded_pilot.entry_id)
    await hass.async_block_till_done()

    assert runtime.current_track is None
    assert runtime._listeners == set()
    assert active_timers(hass) == 0
    state = hass.states.get(track_id)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE
    assert "artist" not in state.attributes

    room.play("Title", "paused")
    assert await hass.config_entries.async_setup(loaded_pilot.entry_id)
    await hass.async_block_till_done()

    assert is_clear(hass, track_id)


async def test_music_stays_out_of_every_diagnostic_output(
    hass: HomeAssistant,
    loaded_pilot: MockConfigEntry,
    track_id: str,
    status_entity_id: str,
    room: Room,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=f"custom_components.{DOMAIN}")
    room.play("canary-title", media_artist="canary-artist", media_album_name="canary-a")
    await hass.async_block_till_done()
    assert "canary-title" in shown(hass, track_id)[0]

    runtime = loaded_pilot.runtime_data
    status = hass.states.get(status_entity_id)
    assert status is not None
    diagnostics = await async_get_config_entry_diagnostics(hass, loaded_pilot)

    for output in (
        caplog.text,
        status.state,
        json.dumps(dict(status.attributes), default=str),
        json.dumps(diagnostics),
        repr(runtime.snapshot),
        repr(runtime.current_track),
    ):
        assert "canary" not in output
