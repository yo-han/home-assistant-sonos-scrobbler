"""Per-speaker switches through real services, registry, SQLite and a socket server."""

import asyncio
import json
from pathlib import Path

import pytest
from homeassistant.const import ATTR_FRIENDLY_NAME, CONF_ENTITY_ID, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.personal_sonos_scrobbler import (
    async_migrate_entry,
    async_unload_entry,
)
from custom_components.personal_sonos_scrobbler.const import (
    CONF_ENTITY_REGISTRY_IDS,
    CONF_NOW_PLAYING,
    CONF_NOW_PLAYING_EPOCH,
    CONF_SCROBBLING_SINCE,
    CONFIG_MINOR_VERSION,
    DOMAIN,
    LEGACY_TITLE,
    MAX_SELECTED_PLAYERS,
    TITLE,
)
from custom_components.personal_sonos_scrobbler.controls import Feature
from custom_components.personal_sonos_scrobbler.outbox import OutboxError
from custom_components.personal_sonos_scrobbler.switch import SpeakerSwitch
from tests.delivery_harness import (
    RECENT,
    SCROBBLE,
    STARTED,
    Pilot,
    accepted,
    held_before,
    held_commit,
    history,
)
from tests.helpers import SETTLE, Room, active_timers, link_account
from tests.lastfm_server import RecordedRequest

NOW = "track.updateNowPlaying"
EPOCH_UPDATE = "UPDATE meta SET value = ? WHERE key = 'epoch'"
COMPONENT = Path(__file__).parents[1] / "custom_components/personal_sonos_scrobbler"
HEARTBEAT = 15
QUALIFYING_HEARTBEATS = 7  # a 200 s track needs 100 s of credit
SONOS = "sonos"


@pytest.fixture
def attic_player(
    entity_registry: er.EntityRegistry, pilot_entry: MockConfigEntry
) -> er.RegistryEntry:
    """Register a Sonos speaker that was never selected."""
    return entity_registry.async_get_or_create(
        "media_player", SONOS, "RINCON_3", suggested_object_id="attic"
    )


@pytest.fixture
def attic(hass: HomeAssistant, attic_player: er.RegistryEntry) -> Room:
    return Room(hass, attic_player)


def speaker_unique_id(pilot: Pilot, registry_id: str) -> str:
    return f"{pilot.entry.entry_id}_speaker_{registry_id}"


def switch_of(pilot: Pilot, player: er.RegistryEntry) -> str:
    entity_id = er.async_get(pilot.hass).async_get_entity_id(
        "switch", DOMAIN, speaker_unique_id(pilot, player.id)
    )
    assert entity_id is not None, f"no switch for {player.entity_id}"
    return entity_id


def speaker_switches(pilot: Pilot) -> list[er.RegistryEntry]:
    entries = er.async_entries_for_config_entry(
        er.async_get(pilot.hass), pilot.entry.entry_id
    )
    prefix = f"{pilot.entry.entry_id}_speaker_"
    return [entry for entry in entries if entry.unique_id.startswith(prefix)]


async def press(pilot: Pilot, player: er.RegistryEntry, service: str) -> None:
    await pilot.hass.services.async_call(
        "switch", service, {"entity_id": switch_of(pilot, player)}, blocking=True
    )


def shown(pilot: Pilot, player: er.RegistryEntry) -> str:
    state = pilot.hass.states.get(switch_of(pilot, player))
    assert state is not None
    return state.state


def selection(pilot: Pilot) -> list[str]:
    selected: list[str] = pilot.entry.data[CONF_ENTITY_REGISTRY_IDS]
    return selected


def sensor_state(pilot: Pilot, key: str) -> str:
    entity_id = er.async_get(pilot.hass).async_get_entity_id(
        "sensor", DOMAIN, f"{pilot.entry.entry_id}_{key}"
    )
    assert entity_id is not None
    state = pilot.hass.states.get(entity_id)
    assert state is not None
    return state.state


def track(pilot: Pilot) -> str:
    return sensor_state(pilot, "current_track")


def notices(pilot: Pilot) -> list[RecordedRequest]:
    return pilot.server.calls(NOW)


async def linked(pilot: Pilot, *features: Feature) -> None:
    """Set up and link, with the time for the first switches to pass."""
    await pilot.setup()
    await link_account(pilot.hass, pilot.entry)
    await pilot.advance(1)
    for feature in features:
        await pilot.press(feature, "turn_on")
    await pilot.advance(1)


async def qualify(pilot: Pilot) -> None:
    for _ in range(QUALIFYING_HEARTBEATS):
        await pilot.advance(HEARTBEAT)


# The catalog


async def test_every_sonos_speaker_gets_a_configuration_switch(
    pilot: Pilot,
    sonos_player: er.RegistryEntry,
    attic_player: er.RegistryEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    lookalike = entity_registry.async_get_or_create(
        "media_player", "cast", "cast-1", suggested_object_id="sonos_keuken"
    )

    await pilot.setup()

    entries = speaker_switches(pilot)
    assert {entry.unique_id for entry in entries} == {
        speaker_unique_id(pilot, sonos_player.id),
        speaker_unique_id(pilot, attic_player.id),
    }
    assert lookalike.id not in str([entry.unique_id for entry in entries])
    assert {entry.entity_category for entry in entries} == {EntityCategory.CONFIG}
    global_switch = entity_registry.async_get(pilot.switch_id(Feature.NOW_PLAYING))
    assert global_switch is not None
    assert global_switch.entity_category is None
    assert {entry.device_id for entry in entries} == {global_switch.device_id}
    assert shown(pilot, sonos_player) == "on"
    assert shown(pilot, attic_player) == "off"


async def test_a_switch_exposes_only_its_source_entity(
    pilot: Pilot, sonos_player: er.RegistryEntry, room: Room
) -> None:
    await pilot.setup()
    room.play()
    await pilot.quiet()

    state = pilot.hass.states.get(switch_of(pilot, sonos_player))

    assert state is not None
    assert state.attributes["speaker"] == sonos_player.entity_id
    assert "Title" not in str(state.attributes)
    assert "media_position" not in state.attributes


async def test_a_new_speaker_appears_off_without_touching_the_selection(
    pilot: Pilot, entity_registry: er.EntityRegistry, sonos_player: er.RegistryEntry
) -> None:
    await pilot.setup()

    newcomer = entity_registry.async_get_or_create(
        "media_player", SONOS, "RINCON_9", suggested_object_id="garage"
    )
    await pilot.quiet()

    assert shown(pilot, newcomer) == "off"
    assert selection(pilot) == [sonos_player.id]


async def test_a_removed_speaker_loses_its_switch_and_no_new_identity_inherits(
    pilot: Pilot, entity_registry: er.EntityRegistry, sonos_player: er.RegistryEntry
) -> None:
    await pilot.setup()
    old_switch = switch_of(pilot, sonos_player)

    entity_registry.async_remove(sonos_player.entity_id)
    await pilot.quiet()

    assert entity_registry.async_get(old_switch) is None
    assert pilot.hass.states.get(old_switch) is None
    replacement = entity_registry.async_get_or_create(
        "media_player", SONOS, "RINCON_NEW", suggested_object_id="kitchen"
    )
    await pilot.quiet()
    assert replacement.id != sonos_player.id
    assert shown(pilot, replacement) == "off"


async def test_a_disabled_speaker_cannot_contribute_and_returns_with_its_choice(
    pilot: Pilot,
    entity_registry: er.EntityRegistry,
    sonos_player: er.RegistryEntry,
    room: Room,
) -> None:
    await pilot.setup()
    room.play()
    await pilot.quiet()
    assert track(pilot) != "unknown"

    entity_registry.async_update_entity(
        sonos_player.entity_id, disabled_by=er.RegistryEntryDisabler.USER
    )
    await pilot.quiet()

    assert speaker_switches(pilot) == []
    assert track(pilot) == "unknown"

    entity_registry.async_update_entity(sonos_player.entity_id, disabled_by=None)
    await pilot.quiet()
    assert shown(pilot, sonos_player) == "on"


async def test_a_rename_keeps_the_identity_and_the_inclusion(
    pilot: Pilot, entity_registry: er.EntityRegistry, sonos_player: er.RegistryEntry
) -> None:
    await pilot.setup()
    before = switch_of(pilot, sonos_player)

    entity_registry.async_update_entity(
        sonos_player.entity_id, new_entity_id="media_player.keuken_beneden"
    )
    await pilot.quiet()

    assert switch_of(pilot, sonos_player) == before
    assert shown(pilot, sonos_player) == "on"
    state = pilot.hass.states.get(before)
    assert state is not None
    assert state.attributes["speaker"] == "media_player.keuken_beneden"
    assert selection(pilot) == [sonos_player.id]


async def test_the_display_name_follows_the_friendly_name(
    pilot: Pilot, sonos_player: er.RegistryEntry, room: Room
) -> None:
    room.play(friendly_name="Keuken")
    await pilot.setup()
    switch = switch_of(pilot, sonos_player)
    state = pilot.hass.states.get(switch)
    assert state is not None
    assert state.attributes[ATTR_FRIENDLY_NAME].endswith(" Keuken")

    room.play(friendly_name="Bijkeuken")
    await pilot.quiet()

    state = pilot.hass.states.get(switch)
    assert state is not None
    assert state.attributes[ATTR_FRIENDLY_NAME].endswith(" Bijkeuken")
    entry = er.async_get(pilot.hass).async_get(switch)
    assert entry is not None
    assert entry.original_name == "Bijkeuken"


async def test_a_speaker_without_a_state_is_named_after_its_device(
    pilot: Pilot,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
    sonos_player: er.RegistryEntry,
) -> None:
    device = device_registry.async_get_or_create(
        config_entry_id=sonos_player.config_entry_id or "",
        identifiers={(SONOS, "RINCON_1")},
        name="Woonkamer",
    )
    entity_registry.async_update_entity(sonos_player.entity_id, device_id=device.id)

    await pilot.setup()

    entry = entity_registry.async_get(switch_of(pilot, sonos_player))
    assert entry is not None
    assert entry.original_name == "Woonkamer"


async def test_a_reload_makes_no_duplicates_and_leaks_no_listener(
    pilot: Pilot, sonos_player: er.RegistryEntry, attic_player: er.RegistryEntry
) -> None:
    await pilot.setup()
    before = {entry.entity_id for entry in speaker_switches(pilot)}
    listeners = dict(pilot.hass.bus.async_listeners())

    for _ in range(3):
        await pilot.hass.config_entries.async_reload(pilot.entry.entry_id)
        await pilot.quiet()

    assert {entry.entity_id for entry in speaker_switches(pilot)} == before
    assert len(speaker_switches(pilot)) == 2
    assert dict(pilot.hass.bus.async_listeners()) == listeners


async def test_unload_stops_following_the_registry(
    pilot: Pilot, entity_registry: er.EntityRegistry
) -> None:
    await pilot.setup()
    switches = speaker_switches(pilot)
    await pilot.unload()

    entity_registry.async_get_or_create("media_player", SONOS, "RINCON_LATE")
    await pilot.quiet()

    assert active_timers(pilot.hass) == 0
    assert speaker_switches(pilot) == switches


async def test_a_speaker_gone_while_stopped_loses_its_switch_at_setup(
    pilot: Pilot, entity_registry: er.EntityRegistry, attic_player: er.RegistryEntry
) -> None:
    await pilot.setup()
    stale = switch_of(pilot, attic_player)
    await pilot.unload()

    entity_registry.async_remove(attic_player.entity_id)
    await pilot.setup()

    assert entity_registry.async_get(stale) is None


async def test_the_catalog_is_bounded_and_says_so_without_selecting_anything(
    pilot: Pilot,
    entity_registry: er.EntityRegistry,
    sonos_player: er.RegistryEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    for number in range(MAX_SELECTED_PLAYERS + 1):
        entity_registry.async_get_or_create(
            "media_player", SONOS, f"RINCON_X{number}", suggested_object_id="a"
        )

    await pilot.setup()

    assert len(speaker_switches(pilot)) == MAX_SELECTED_PLAYERS
    assert shown(pilot, sonos_player) == "on"  # a selected speaker is never left out
    assert selection(pilot) == [sonos_player.id]
    status = er.async_get(pilot.hass).async_get_entity_id(
        "sensor", DOMAIN, f"{pilot.entry.entry_id}_status"
    )
    assert status is not None
    state = pilot.hass.states.get(status)
    assert state is not None
    assert state.attributes["speakers_left_out"] == 2
    assert "2 Sonos speakers have no switch" in caplog.text


@pytest.mark.parametrize(
    "name", ["strings.json", "translations/en.json", "translations/nl.json"]
)
def test_the_switch_is_named_after_the_speaker_alone(name: str) -> None:
    translations = json.loads((COMPONENT / name).read_text())

    assert translations["entity"]["switch"]["speaker"]["name"] == "{speaker}"
    for key in (
        "speaker_unavailable",
        "too_many_speakers",
        "selection_without_scrobbling",
    ):
        assert translations["exceptions"][key]["message"]


# What a switch controls


async def test_an_excluded_room_contributes_nothing_with_every_feature_on(
    pilot: Pilot, attic: Room
) -> None:
    await linked(pilot, Feature.NOW_PLAYING, Feature.SCROBBLING)

    attic.play()
    await pilot.quiet()
    await qualify(pilot)
    await pilot.past_grace()

    assert track(pilot) == "unknown"
    assert sensor_state(pilot, "current_speaker") == "unknown"
    assert notices(pilot) == []
    assert (pilot.reads(), pilot.posts()) == (0, 0)
    assert pilot.rows("SELECT id FROM plays") == []


async def test_switches_work_unlinked_and_create_no_ledger(
    pilot: Pilot, attic_player: er.RegistryEntry, attic: Room
) -> None:
    await pilot.setup()
    attic.play(position=40)
    await pilot.quiet()
    assert track(pilot) == "unknown"

    await press(pilot, attic_player, "turn_on")
    await pilot.quiet()

    assert shown(pilot, attic_player) == "on"
    assert attic_player.id in selection(pilot)
    assert track(pilot).endswith("Title")
    assert not pilot.path.exists()
    assert pilot.server.requests == []


async def test_including_a_room_allows_the_present_but_history_only_what_is_new(
    pilot: Pilot, attic_player: er.RegistryEntry, attic: Room
) -> None:
    await linked(pilot, Feature.NOW_PLAYING, Feature.SCROBBLING)
    attic.play("Already playing", position=40)
    await pilot.quiet()
    since = pilot.entry.data[CONF_SCROBBLING_SINCE]

    await press(pilot, attic_player, "turn_on")
    await pilot.quiet()
    await pilot.reach(lambda: len(notices(pilot)) == 1)

    assert track(pilot).endswith("Already playing")
    assert notices(pilot)[0].form["track"] == "Already playing"
    assert pilot.is_on(Feature.SCROBBLING)
    assert pilot.entry.data[CONF_SCROBBLING_SINCE] > since

    await qualify(pilot)
    await pilot.past_grace()
    assert pilot.rows("SELECT id FROM plays") == []

    pilot.server.queue(RECENT, history())
    pilot.server.queue(SCROBBLE, accepted(pilot_now(pilot)))
    attic.play("Brand new")
    await pilot.quiet()
    await qualify(pilot)
    await pilot.past_grace()
    await pilot.reach(lambda: pilot.posts() == 1, tick=1)

    assert pilot.server.calls(SCROBBLE)[0].form["track"] == "Brand new"


def pilot_now(pilot: Pilot) -> int:
    return int(pilot.entry.data[CONF_SCROBBLING_SINCE])


async def test_excluding_the_active_room_clears_or_falls_back(
    pilot: Pilot,
    sonos_player: er.RegistryEntry,
    room: Room,
    hall: Room,
) -> None:
    await pilot.setup()
    room.play("Older")
    await pilot.advance(5)
    hall.play("Newest")
    await pilot.quiet()
    assert track(pilot).endswith("Newest")
    hall_player = er.async_get(pilot.hass).async_get(hall.entity_id)
    assert hall_player is not None

    await press(pilot, hall_player, "turn_off")
    await pilot.quiet()
    assert track(pilot).endswith("Older")

    await press(pilot, sonos_player, "turn_off")
    await pilot.quiet()
    assert track(pilot) == "unknown"


async def test_all_rooms_off_stays_loaded_and_quiet(
    pilot: Pilot, sonos_player: er.RegistryEntry, room: Room
) -> None:
    await linked(pilot, Feature.NOW_PLAYING)
    room.play()
    await pilot.quiet()
    await pilot.reach(lambda: len(notices(pilot)) == 1)
    assert track(pilot).endswith("Title")

    await press(pilot, sonos_player, "turn_off")
    await pilot.quiet()

    assert selection(pilot) == []
    assert pilot.loaded()
    assert track(pilot) == "unknown"
    assert sensor_state(pilot, "current_speaker") == "unknown"
    room.play("Next")
    await pilot.quiet()
    runtime = pilot.entry.runtime_data
    assert runtime._cancel_timer is None  # the loopback server keeps its own timers
    assert runtime._cancel_settle is None
    await pilot.advance(HEARTBEAT)
    assert len(notices(pilot)) == 1


async def test_a_group_with_an_included_follower_counts_once(
    pilot: Pilot, room: Room, hall: Room, sonos_player: er.RegistryEntry
) -> None:
    await linked(pilot, Feature.SCROBBLING)
    pilot.server.queue(RECENT, history())
    pilot.server.queue(SCROBBLE, accepted(pilot_now(pilot) + 1))
    await press(pilot, sonos_player, "turn_off")
    await pilot.advance(2)

    room.play(members=(room, hall))
    hall.play(members=(room, hall))
    await pilot.advance(SETTLE)
    assert track(pilot).endswith("Title")
    await qualify(pilot)
    await pilot.past_grace()
    await pilot.reach(lambda: pilot.posts() == 1, tick=1)

    await pilot.past_grace()
    assert pilot.posts() == 1
    assert pilot.rows("SELECT COUNT(*) FROM plays") == [(1,)]


async def test_a_group_without_an_included_member_contributes_nothing(
    pilot: Pilot, room: Room, attic: Room, sonos_player: er.RegistryEntry
) -> None:
    await linked(pilot, Feature.NOW_PLAYING, Feature.SCROBBLING)
    await press(pilot, sonos_player, "turn_off")

    room.play(members=(room, attic))
    attic.play(members=(room, attic))
    await pilot.advance(SETTLE)
    await qualify(pilot)
    await pilot.past_grace()

    assert track(pilot) == "unknown"
    assert notices(pilot) == []
    assert pilot.rows("SELECT id FROM plays") == []


# The new period


async def test_a_change_parks_every_queued_play_through_off_on_and_restart(
    pilot: Pilot, attic_player: er.RegistryEntry
) -> None:
    await linked(pilot, Feature.NOW_PLAYING, Feature.SCROBBLING)
    await pilot.accept_now(start=pilot_now(pilot) - STARTED + 1)
    assert pilot.state() == "pending"

    await press(pilot, attic_player, "turn_on")
    await press(pilot, attic_player, "turn_off")
    await pilot.past_grace()
    await pilot.restart()
    await pilot.past_grace()

    assert pilot.is_on(Feature.SCROBBLING)
    assert pilot.is_on(Feature.NOW_PLAYING)
    assert pilot.state() == "pending"
    assert (pilot.reads(), pilot.posts()) == (0, 0)


async def test_an_unchanged_selection_starts_no_new_period(
    pilot: Pilot, sonos_player: er.RegistryEntry, attic_player: er.RegistryEntry
) -> None:
    await linked(pilot, Feature.SCROBBLING)
    since = pilot.entry.data[CONF_SCROBBLING_SINCE]
    await pilot.advance(5)

    await press(pilot, sonos_player, "turn_on")
    await press(pilot, attic_player, "turn_off")
    await pilot.quiet()

    assert pilot.entry.data[CONF_SCROBBLING_SINCE] == since


async def test_a_change_with_scrobbling_off_leaves_it_off(
    pilot: Pilot, attic_player: er.RegistryEntry
) -> None:
    await linked(pilot, Feature.NOW_PLAYING)

    await press(pilot, attic_player, "turn_on")
    await pilot.quiet()

    assert pilot.is_on(Feature.NOW_PLAYING)
    assert not pilot.is_on(Feature.SCROBBLING)
    assert pilot.entry.data[CONF_SCROBBLING_SINCE] is None


async def test_upgrade_keeps_the_selection_on_and_new_speakers_off(
    pilot: Pilot,
    sonos_player: er.RegistryEntry,
    attic_player: er.RegistryEntry,
) -> None:
    await linked(pilot, Feature.NOW_PLAYING)
    data = dict(pilot.entry.data)

    await pilot.restart()

    assert shown(pilot, sonos_player) == "on"
    assert shown(pilot, attic_player) == "off"
    assert dict(pilot.entry.data) == data


# Races


async def test_off_binds_at_once_while_the_ledger_is_held(
    pilot: Pilot,
    sonos_player: er.RegistryEntry,
    room: Room,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await linked(pilot, Feature.NOW_PLAYING, Feature.SCROBBLING)
    room.play()
    await pilot.quiet()
    assert track(pilot).endswith("Title")

    with held_commit(monkeypatch, EPOCH_UPDATE) as hold:
        turning_off = pilot.hass.async_create_task(
            press(pilot, sonos_player, "turn_off")
        )
        await pilot.reach(hold.reached.is_set)
        assert selection(pilot) == []
        assert track(pilot) == "unknown"
        assert shown(pilot, sonos_player) == "off"
        hold.release.set()
        await turning_off

    assert pilot.is_on(Feature.SCROBBLING)


async def test_a_waiting_notice_of_an_excluded_room_never_leaves(
    pilot: Pilot, sonos_player: er.RegistryEntry, room: Room
) -> None:
    await linked(pilot, Feature.NOW_PLAYING)
    room.play("First")
    await pilot.quiet()
    await pilot.reach(lambda: len(notices(pilot)) == 1)

    room.play("Second")  # behind the pacer, which the frozen clock holds
    await pilot.quiet()
    await press(pilot, sonos_player, "turn_off")
    await pilot.advance(5)
    await pilot.quiet()

    assert len(notices(pilot)) == 1


async def test_a_later_global_off_is_not_undone_by_a_room_change(
    pilot: Pilot, attic_player: er.RegistryEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    await linked(pilot, Feature.SCROBBLING)

    with held_commit(monkeypatch, EPOCH_UPDATE) as hold:
        toggling = pilot.hass.async_create_task(press(pilot, attic_player, "turn_on"))
        await pilot.reach(hold.reached.is_set)
        off = pilot.hass.async_create_task(pilot.press(Feature.SCROBBLING, "turn_off"))
        await pilot.quiet()
        hold.release.set()
        await asyncio.gather(toggling, off)
    await pilot.quiet()

    assert attic_player.id in selection(pilot)
    assert not pilot.is_on(Feature.SCROBBLING)
    assert pilot.entry.data[CONF_SCROBBLING_SINCE] is None


async def test_an_unlink_during_a_room_change_keeps_everything_off(
    pilot: Pilot, attic_player: er.RegistryEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    await linked(pilot, Feature.NOW_PLAYING, Feature.SCROBBLING)

    with held_commit(monkeypatch, EPOCH_UPDATE) as hold:
        toggling = pilot.hass.async_create_task(press(pilot, attic_player, "turn_on"))
        await pilot.reach(hold.reached.is_set)
        unlink = pilot.hass.async_create_task(pilot.option("disconnect"))
        await pilot.quiet()
        hold.release.set()
        await asyncio.gather(toggling, unlink)
    await pilot.quiet()

    assert attic_player.id in selection(pilot)
    assert not pilot.is_on(Feature.SCROBBLING)
    assert not pilot.is_on(Feature.NOW_PLAYING)


async def test_an_unload_during_a_room_change_ends_it_and_logs_why(
    pilot: Pilot,
    attic_player: er.RegistryEntry,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await linked(pilot, Feature.SCROBBLING)

    with held_commit(monkeypatch, EPOCH_UPDATE) as hold:
        toggling = pilot.hass.async_create_task(press(pilot, attic_player, "turn_on"))
        await pilot.reach(hold.reached.is_set)
        unloading = pilot.hass.async_create_task(pilot.unload())
        await pilot.quiet()
        hold.release.set()
        await asyncio.gather(toggling, unloading)

    assert attic_player.id in selection(pilot)
    assert not pilot.is_on(Feature.SCROBBLING)
    assert "Scrobbling stopped with the integration" in caplog.text
    await pilot.setup()
    assert not pilot.is_on(Feature.SCROBBLING)


async def test_concurrent_rooms_lose_no_update_and_end_in_one_period(
    pilot: Pilot,
    sonos_player: er.RegistryEntry,
    attic_player: er.RegistryEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await linked(pilot, Feature.SCROBBLING)

    with held_commit(monkeypatch, EPOCH_UPDATE) as hold:
        first = pilot.hass.async_create_task(press(pilot, attic_player, "turn_on"))
        await pilot.reach(hold.reached.is_set)
        second = pilot.hass.async_create_task(press(pilot, sonos_player, "turn_off"))
        await pilot.quiet()
        hold.release.set()
        await asyncio.gather(first, second)
    await pilot.quiet()

    assert selection(pilot) == [attic_player.id]
    assert pilot.is_on(Feature.SCROBBLING)
    assert pilot.entry.data[CONF_SCROBBLING_SINCE] is not None


async def test_a_later_on_wins_over_an_earlier_off_of_the_same_room(
    pilot: Pilot, attic_player: er.RegistryEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    await linked(pilot, Feature.SCROBBLING)
    await press(pilot, attic_player, "turn_on")

    with held_commit(monkeypatch, EPOCH_UPDATE) as hold:
        off = pilot.hass.async_create_task(press(pilot, attic_player, "turn_off"))
        await pilot.reach(hold.reached.is_set)
        on = pilot.hass.async_create_task(press(pilot, attic_player, "turn_on"))
        await pilot.quiet()
        hold.release.set()
        await asyncio.gather(off, on)
    await pilot.quiet()

    assert attic_player.id in selection(pilot)
    assert shown(pilot, attic_player) == "on"
    assert pilot.is_on(Feature.SCROBBLING)


async def test_a_held_worker_cannot_send_after_a_change(
    pilot: Pilot, attic_player: er.RegistryEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    await linked(pilot, Feature.SCROBBLING)
    await pilot.accept_now(start=pilot_now(pilot) - STARTED + 1)
    pilot.server.queue(RECENT, history())

    with held_before(monkeypatch, "UPDATE plays SET state") as hold:
        await pilot.advance(800)
        await pilot.reach(lambda: hold.reached.is_set(), tick=1)
        await press_soon(pilot, attic_player)
        hold.release.set()
        await pilot.quiet()
    await pilot.past_grace()

    assert pilot.posts() == 0


async def press_soon(pilot: Pilot, player: er.RegistryEntry) -> None:
    """Start a press without waiting: its ledger step needs the held thread."""
    pilot.hass.async_create_task(press(pilot, player, "turn_on"))
    await pilot.linger()


async def test_reconfigure_shares_the_semantics_of_the_switches(
    pilot: Pilot,
    sonos_player: er.RegistryEntry,
    attic_player: er.RegistryEntry,
) -> None:
    await linked(pilot, Feature.NOW_PLAYING, Feature.SCROBBLING)
    since = pilot.entry.data[CONF_SCROBBLING_SINCE]
    await pilot.accept_now(start=pilot_now(pilot) - STARTED + 1)
    await pilot.advance(5)
    runtime = pilot.entry.runtime_data

    result = await pilot.entry.start_reconfigure_flow(pilot.hass)
    result = await pilot.hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ENTITY_ID: [attic_player.entity_id]}
    )
    await pilot.quiet()

    assert result["reason"] == "reconfigure_successful"
    assert pilot.entry.runtime_data is runtime
    assert selection(pilot) == [attic_player.id]
    assert shown(pilot, attic_player) == "on"
    assert shown(pilot, sonos_player) == "off"
    assert pilot.is_on(Feature.NOW_PLAYING)
    assert pilot.is_on(Feature.SCROBBLING)
    assert pilot.entry.data[CONF_SCROBBLING_SINCE] > since
    await pilot.past_grace()
    assert pilot.state() == "pending"
    assert (pilot.reads(), pilot.posts()) == (0, 0)


async def test_an_entry_without_a_selection_starts_and_opts_in_by_switch(
    pilot: Pilot, sonos_player: er.RegistryEntry, room: Room
) -> None:
    pilot.hass.config_entries.async_update_entry(
        pilot.entry, data={**pilot.entry.data, CONF_ENTITY_REGISTRY_IDS: []}
    )
    room.play(position=30)
    await linked(pilot, Feature.NOW_PLAYING)
    await pilot.restart()  # with a ledger to purge against an empty selection
    await pilot.quiet()
    assert track(pilot) == "unknown"
    assert shown(pilot, sonos_player) == "off"
    runtime = pilot.entry.runtime_data
    assert runtime.entity_present is True

    await press(pilot, sonos_player, "turn_on")
    await pilot.quiet()

    assert track(pilot).endswith("Title")


# Failures


async def test_a_storage_fault_applies_the_room_and_says_scrobbling_stopped(
    pilot: Pilot,
    sonos_player: er.RegistryEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await linked(pilot, Feature.SCROBBLING)

    async def broken() -> None:
        raise OutboxError("storage_error")

    monkeypatch.setattr(pilot.service.controls, "_prepare_ledger", broken)

    with pytest.raises(HomeAssistantError) as raised:
        await press(pilot, sonos_player, "turn_off")

    assert raised.value.translation_key == "selection_without_scrobbling"
    assert selection(pilot) == []
    assert not pilot.is_on(Feature.SCROBBLING)


async def test_a_vanished_speaker_cannot_be_included(
    pilot: Pilot, attic_player: er.RegistryEntry
) -> None:
    await pilot.setup()

    with pytest.raises(HomeAssistantError) as raised:
        await pilot.service.speakers.async_include("not-a-registry-id")

    assert raised.value.translation_key == "speaker_unavailable"
    assert attic_player.id not in selection(pilot)


async def test_the_selection_limit_is_a_fixed_error(
    pilot: Pilot, attic_player: er.RegistryEntry
) -> None:
    await pilot.setup()
    ids = [f"gone-{number}" for number in range(MAX_SELECTED_PLAYERS)]
    pilot.hass.config_entries.async_update_entry(
        pilot.entry, data={**pilot.entry.data, CONF_ENTITY_REGISTRY_IDS: ids}
    )

    with pytest.raises(HomeAssistantError) as raised:
        await press(pilot, attic_player, "turn_on")

    assert raised.value.translation_key == "too_many_speakers"


# Unload


async def test_a_catalog_change_during_a_held_stop_adds_nothing(
    pilot: Pilot, entity_registry: er.EntityRegistry, sonos_player: er.RegistryEntry
) -> None:
    await linked(pilot, Feature.NOW_PLAYING)
    service = pilot.service
    await service._lock.acquire()
    unloading = pilot.hass.async_create_task(
        pilot.hass.config_entries.async_unload(pilot.entry.entry_id)
    )
    await pilot.reach(lambda: service._stopping)

    late = entity_registry.async_get_or_create(
        "media_player", SONOS, "RINCON_LATE", suggested_object_id="late"
    )
    await pilot.linger()
    service._lock.release()
    assert await unloading
    await pilot.quiet()

    assert not pilot.loaded()
    assert speaker_unique_id(pilot, late.id) not in {
        entry.unique_id for entry in speaker_switches(pilot)
    }
    assert not [
        state
        for state in pilot.hass.states.async_all("switch")
        if state.state != "unavailable"
    ]
    await pilot.setup()
    assert {entry.unique_id for entry in speaker_switches(pilot)} == {
        speaker_unique_id(pilot, sonos_player.id),
        speaker_unique_id(pilot, late.id),
    }
    assert shown(pilot, late) == "off"


def original_name(registry: er.EntityRegistry, entity_id: str) -> str | None:
    entry = registry.async_get(entity_id)
    assert entry is not None
    return entry.original_name


async def test_a_catalog_change_during_platform_removal_adds_nothing(
    pilot: Pilot, entity_registry: er.EntityRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    await pilot.setup()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_removal(_switch: SpeakerSwitch) -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr(SpeakerSwitch, "async_will_remove_from_hass", hold_removal)
    unloading = pilot.hass.async_create_task(
        pilot.hass.config_entries.async_unload(pilot.entry.entry_id)
    )
    await pilot.reach(entered.is_set)

    late = entity_registry.async_get_or_create(
        "media_player", SONOS, "RINCON_LATE", suggested_object_id="late"
    )
    await pilot.linger()
    release.set()
    assert await unloading
    await pilot.quiet()

    assert speaker_unique_id(pilot, late.id) not in {
        entry.unique_id for entry in speaker_switches(pilot)
    }
    assert not [
        state
        for state in pilot.hass.states.async_all("switch")
        if state.state != "unavailable"
    ]


async def test_a_refused_unload_catches_up_and_keeps_following_the_catalog(
    pilot: Pilot,
    entity_registry: er.EntityRegistry,
    sonos_player: er.RegistryEntry,
    room: Room,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    room.play(friendly_name="Keuken")
    await pilot.setup()
    switch = switch_of(pilot, sonos_player)
    added: list[er.RegistryEntry] = []

    async def refuse(*_args: object) -> bool:
        added.append(
            entity_registry.async_get_or_create(
                "media_player", SONOS, "RINCON_LATE", suggested_object_id="late"
            )
        )
        room.play(friendly_name="Bijkeuken")
        await pilot.linger()
        assert len(speaker_switches(pilot)) == 1  # nothing joins a platform on its way
        return False

    with monkeypatch.context() as patch:
        patch.setattr(pilot.hass.config_entries, "async_unload_platforms", refuse)
        assert not await async_unload_entry(pilot.hass, pilot.entry)
    await pilot.quiet()

    assert pilot.loaded()
    assert shown(pilot, added[0]) == "off"
    assert original_name(entity_registry, switch) == "Bijkeuken"

    await press(pilot, added[0], "turn_on")
    assert shown(pilot, added[0]) == "on"
    later = entity_registry.async_get_or_create(
        "media_player", SONOS, "RINCON_LATER", suggested_object_id="later"
    )
    room.play(friendly_name="Keuken")
    await pilot.quiet()
    assert shown(pilot, later) == "off"
    assert original_name(entity_registry, switch) == "Keuken"


async def test_an_unload_that_raises_still_catches_up(
    pilot: Pilot, entity_registry: er.EntityRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    await pilot.setup()

    async def fail(*_args: object) -> bool:
        raise ValueError

    with monkeypatch.context() as patch:
        patch.setattr(pilot.hass.config_entries, "async_unload_platforms", fail)
        with pytest.raises(ValueError):
            await async_unload_entry(pilot.hass, pilot.entry)

    late = entity_registry.async_get_or_create(
        "media_player", SONOS, "RINCON_LATE", suggested_object_id="late"
    )
    await pilot.quiet()
    assert shown(pilot, late) == "off"


# Identity


async def test_a_restored_identity_keeps_its_choice_and_a_new_one_starts_off(
    pilot: Pilot,
    entity_registry: er.EntityRegistry,
    attic_player: er.RegistryEntry,
) -> None:
    await pilot.setup()
    await press(pilot, attic_player, "turn_on")
    switch = switch_of(pilot, attic_player)

    entity_registry.async_remove(attic_player.entity_id)
    await pilot.quiet()
    assert entity_registry.async_get(switch) is None
    assert attic_player.id in selection(pilot)

    restored = entity_registry.async_get_or_create(
        "media_player", SONOS, "RINCON_3", suggested_object_id="attic"
    )
    genuinely_new = entity_registry.async_get_or_create(
        "media_player", SONOS, "RINCON_NEW", suggested_object_id="attic"
    )
    await pilot.quiet()

    assert restored.id == attic_player.id
    assert genuinely_new.id != attic_player.id
    assert switch_of(pilot, restored) == switch
    assert shown(pilot, restored) == "on"
    assert shown(pilot, genuinely_new) == "off"


# The bound on the catalog


async def test_a_speaker_turned_off_at_the_limit_returns_through_reconfigure(
    pilot: Pilot, entity_registry: er.EntityRegistry, sonos_player: er.RegistryEntry
) -> None:
    for number in range(MAX_SELECTED_PLAYERS):
        entity_registry.async_get_or_create(
            "media_player", SONOS, f"RINCON_X{number}", suggested_object_id="a"
        )
    await pilot.setup()
    switch = switch_of(pilot, sonos_player)
    service = pilot.service

    await press(pilot, sonos_player, "turn_off")
    await pilot.quiet()

    assert selection(pilot) == []
    assert entity_registry.async_get(switch) is None
    assert pilot.hass.states.get(switch) is None
    assert service.speakers.left_out == 1
    with pytest.raises(HomeAssistantError) as raised:
        await service.speakers.async_include(sonos_player.id)
    assert raised.value.translation_key == "speaker_unavailable"

    form = await pilot.entry.start_reconfigure_flow(pilot.hass)
    result = await pilot.hass.config_entries.flow.async_configure(
        form["flow_id"], {CONF_ENTITY_ID: [sonos_player.entity_id]}
    )
    await pilot.quiet()

    assert result["reason"] == "reconfigure_successful"
    assert selection(pilot) == [sonos_player.id]
    assert switch_of(pilot, sonos_player) == switch
    assert shown(pilot, sonos_player) == "on"
    assert service.speakers.left_out == 1


# Names


@pytest.mark.parametrize(
    ("language", "track_name", "now_playing"),
    [
        ("en", "Current track", "Now Playing to Last.fm"),
        ("nl", "Huidig nummer", "Now Playing naar Last.fm"),
    ],
)
async def test_a_switch_shows_the_bare_speaker_name_in_each_language(
    pilot: Pilot,
    sonos_player: er.RegistryEntry,
    room: Room,
    language: str,
    track_name: str,
    now_playing: str,
) -> None:
    pilot.hass.config.language = language
    room.play(friendly_name="Keuken")

    await pilot.setup()

    registry = er.async_get(pilot.hass)
    speaker = registry.async_get(switch_of(pilot, sonos_player))
    feature = registry.async_get(pilot.switch_id(Feature.NOW_PLAYING))
    track = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{pilot.entry.entry_id}_current_track"
    )
    assert track is not None
    sensor = registry.async_get(track)
    assert speaker is not None
    assert feature is not None
    assert sensor is not None
    assert speaker.original_name == "Keuken"
    assert feature.original_name == now_playing
    assert sensor.original_name == track_name

    await press(pilot, sonos_player, "turn_off")
    assert shown(pilot, sonos_player) == "off"


@pytest.mark.parametrize(
    ("old_title", "new_title"),
    [
        (LEGACY_TITLE, TITLE),
        ("Personal Sonos Scrobbler", TITLE),
        ("Muziek", "Muziek"),
    ],
    ids=["pilot_default", "public_default", "user_title"],
)
@pytest.mark.parametrize("minor_version", [2, 3])
@pytest.mark.parametrize("device_name_by_user", [None, "Eigen audio"])
async def test_an_upgrade_renames_only_defaults_and_keeps_every_identity(
    pilot: Pilot,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
    sonos_player: er.RegistryEntry,
    attic_player: er.RegistryEntry,
    room: Room,
    old_title: str,
    new_title: str,
    minor_version: int,
    device_name_by_user: str | None,
) -> None:
    entry = pilot.entry
    pilot.hass.config_entries.async_update_entry(
        entry,
        title=old_title,
        minor_version=minor_version,
        data={**entry.data, CONF_NOW_PLAYING: True, CONF_NOW_PLAYING_EPOCH: 3},
    )
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=old_title,
    )
    device_registry.async_update_device(device.id, name_by_user=device_name_by_user)
    kept = entity_registry.async_get_or_create(
        "switch",
        DOMAIN,
        speaker_unique_id(pilot, sonos_player.id),
        suggested_object_id="sonos_observation_pilot_include_keuken",
        config_entry=entry,
        device_id=device.id,
        original_name="Include Keuken",
    )
    renamed = entity_registry.async_get_or_create(
        "switch",
        DOMAIN,
        speaker_unique_id(pilot, attic_player.id),
        suggested_object_id="sonos_observation_pilot_include_zolder",
        config_entry=entry,
        device_id=device.id,
        original_name="Include Zolder",
    )
    entity_registry.async_update_entity(renamed.entity_id, name="Mijn zolder")
    room.play(friendly_name="Keuken")
    stored_data = dict(entry.data)

    assert await async_migrate_entry(pilot.hass, entry)
    assert entry.data == stored_data
    await pilot.setup()

    assert entry.title == new_title
    assert entry.minor_version == CONFIG_MINOR_VERSION
    assert entry.data[CONF_NOW_PLAYING] is True  # a newer entry keeps its switches
    assert entry.data[CONF_NOW_PLAYING_EPOCH] == 3
    upgraded = device_registry.async_get(device.id)
    assert upgraded is not None
    assert upgraded.name == TITLE
    assert upgraded.name_by_user == device_name_by_user
    assert switch_of(pilot, sonos_player) == kept.entity_id
    assert switch_of(pilot, attic_player) == renamed.entity_id
    speaker = entity_registry.async_get(kept.entity_id)
    custom = entity_registry.async_get(renamed.entity_id)
    assert speaker is not None
    assert custom is not None
    assert speaker.id == kept.id
    assert speaker.original_name == "Keuken"
    assert custom.name == "Mijn zolder"
    state = pilot.hass.states.get(renamed.entity_id)
    assert state is not None
    assert state.attributes[ATTR_FRIENDLY_NAME].endswith("Mijn zolder")


async def test_a_new_entry_gets_the_public_name(
    hass: HomeAssistant, pilot: Pilot, sonos_player: er.RegistryEntry
) -> None:
    await pilot.setup()
    devices = dr.async_entries_for_config_entry(
        dr.async_get(hass), pilot.entry.entry_id
    )

    assert [device.name for device in devices] == ["Home Assistant Sonos Scrobbler"]
    assert pilot.switch_id(Feature.NOW_PLAYING).startswith(
        "switch.home_assistant_sonos_scrobbler_"
    )
