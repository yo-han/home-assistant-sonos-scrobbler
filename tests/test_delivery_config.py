"""Version 2 config: several players, delivery off by default, fenced by an epoch."""

from typing import Any

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_ENTITY_ID
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.personal_sonos_scrobbler import async_migrate_entry
from custom_components.personal_sonos_scrobbler.const import (
    CONF_DELIVERY_ENABLED,
    CONF_DELIVERY_ENABLED_AT,
    CONF_DELIVERY_EPOCH,
    CONF_ENTITY_REGISTRY_ID,
    CONF_ENTITY_REGISTRY_IDS,
    CONF_HISTORICAL_DELIVERY,
    CONF_LASTFM,
    CONF_LASTFM_LINK_REVISION,
    CONF_NOW_PLAYING,
    CONF_NOW_PLAYING_EPOCH,
    CONF_OUTBOX_CREATED,
    CONF_SCROBBLING,
    CONF_SCROBBLING_SINCE,
    CONFIG_MINOR_VERSION,
    DOMAIN,
    MAX_SELECTED_PLAYERS,
    SIGNAL_LASTFM_LINK_CHANGED,
)
from custom_components.personal_sonos_scrobbler.controls import Feature
from custom_components.personal_sonos_scrobbler.delivery import delivery_service
from tests.helpers import choose, entry_data, link_account, start_options

LINK = {
    "api_key": "a",
    "shared_secret": "b",
    "session_key": "c",
    "account_name": "d",
}


def link_pilot(hass: HomeAssistant, entry: MockConfigEntry, **extra: Any) -> None:
    """Give the entry a confirmed link, the way a finished link flow leaves it."""
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_LASTFM: dict(LINK),
            CONF_LASTFM_LINK_REVISION: 1,
            CONF_DELIVERY_EPOCH: 1,
            **extra,
        },
    )


def v1_entry(registry_id: str, **extra: Any) -> MockConfigEntry:
    """The format this project shipped before delivery existed."""
    return MockConfigEntry(
        domain=DOMAIN, version=1, data={CONF_ENTITY_REGISTRY_ID: registry_id, **extra}
    )


async def test_v1_entry_migrates_keeping_link_and_revision(
    hass: HomeAssistant, sonos_player: er.RegistryEntry
) -> None:
    entry = v1_entry(
        sonos_player.id, **{CONF_LASTFM: dict(LINK), CONF_LASTFM_LINK_REVISION: 3}
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.version == 2
    assert entry.data == {
        **entry_data(sonos_player.id),
        CONF_LASTFM: LINK,
        CONF_LASTFM_LINK_REVISION: 3,
    }
    await hass.config_entries.async_unload(entry.entry_id)


async def test_migration_defaults_delivery_off(
    hass: HomeAssistant, sonos_player: er.RegistryEntry
) -> None:
    entry = v1_entry(sonos_player.id)
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.data[CONF_ENTITY_REGISTRY_IDS] == [sonos_player.id]
    assert CONF_ENTITY_REGISTRY_ID not in entry.data
    assert entry.data[CONF_NOW_PLAYING] is False
    assert entry.data[CONF_SCROBBLING] is False
    assert entry.data[CONF_DELIVERY_EPOCH] == 0
    assert entry.data[CONF_OUTBOX_CREATED] is False
    await hass.config_entries.async_unload(entry.entry_id)


def v2_entry(registry_id: str, **extra: Any) -> MockConfigEntry:
    """What 0.4 stored, with its master flag and mode."""
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=1,
        data={
            CONF_ENTITY_REGISTRY_IDS: [registry_id],
            CONF_DELIVERY_ENABLED: False,
            CONF_DELIVERY_EPOCH: 7,
            CONF_DELIVERY_ENABLED_AT: 1,
            CONF_OUTBOX_CREATED: False,
            CONF_HISTORICAL_DELIVERY: False,
            **extra,
        },
    )


@pytest.mark.parametrize("historical", [False, True, "yes"])
@pytest.mark.parametrize(
    ("delivery", "link", "now_playing"),
    [
        (True, {CONF_LASTFM: LINK}, True),
        (False, {CONF_LASTFM: LINK}, False),
        (True, {}, False),
        ("yes", {CONF_LASTFM: LINK}, False),
    ],
)
async def test_the_0_4_entry_keeps_now_playing_and_never_gains_scrobbling(
    hass: HomeAssistant,
    historical: object,
    delivery: object,
    link: dict[str, Any],
    now_playing: bool,
) -> None:
    entry = v2_entry(
        "f" * 32,
        **{
            CONF_DELIVERY_ENABLED: delivery,
            CONF_HISTORICAL_DELIVERY: historical,
            CONF_LASTFM_LINK_REVISION: 3,
            **link,
        },
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    assert (entry.version, entry.minor_version) == (2, CONFIG_MINOR_VERSION)
    assert entry.data == {
        CONF_ENTITY_REGISTRY_IDS: ["f" * 32],
        CONF_DELIVERY_EPOCH: 7,
        CONF_OUTBOX_CREATED: False,
        CONF_LASTFM_LINK_REVISION: 3,
        **link,
        CONF_NOW_PLAYING: now_playing,
        CONF_NOW_PLAYING_EPOCH: 0,
        CONF_SCROBBLING: False,
        CONF_SCROBBLING_SINCE: None,
    }


async def test_the_migration_runs_once(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=CONFIG_MINOR_VERSION,
        data={CONF_SCROBBLING: True, CONF_DELIVERY_ENABLED: True},
    )
    entry.add_to_hass(hass)
    before = dict(entry.data)

    assert await async_migrate_entry(hass, entry)

    assert entry.data == before


@pytest.mark.parametrize("data", [{}, {CONF_ENTITY_REGISTRY_ID: ""}])
async def test_migration_without_registry_id_fails_setup(
    hass: HomeAssistant, data: dict[str, Any]
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, version=1, data=data)
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)

    assert entry.state is ConfigEntryState.MIGRATION_ERROR
    assert entry.version == 1
    assert entry.data == data


async def test_entry_from_a_newer_version_is_not_touched(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, version=3, data={"future": True})
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)

    assert entry.state is ConfigEntryState.MIGRATION_ERROR
    assert entry.data == {"future": True}


@pytest.mark.parametrize(
    ("case", "error"),
    [
        ("duplicate", "selection_duplicate"),
        ("foreign", "not_sonos_media_player"),
        ("disabled", "entity_disabled"),
        ("65_players", "too_many_players"),
    ],
)
async def test_rejects_duplicate_foreign_disabled_or_65_players(
    hass: HomeAssistant,
    sonos_player: er.RegistryEntry,
    entity_registry: er.EntityRegistry,
    case: str,
    error: str,
) -> None:
    selections = {
        "duplicate": [sonos_player.entity_id, sonos_player.entity_id],
        "foreign": [
            sonos_player.entity_id,
            entity_registry.async_get_or_create(
                "media_player", "cast", "c-1"
            ).entity_id,
        ],
        "disabled": [
            sonos_player.entity_id,
            entity_registry.async_get_or_create(
                "media_player",
                "sonos",
                "RINCON_OFF",
                disabled_by=er.RegistryEntryDisabler.USER,
            ).entity_id,
        ],
        "65_players": [
            entity_registry.async_get_or_create(
                "media_player", "sonos", f"RINCON_N{index}"
            ).entity_id
            for index in range(MAX_SELECTED_PLAYERS + 1)
        ],
    }
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ENTITY_ID: selections[case]}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_ENTITY_ID: error}


async def test_exactly_64_players_are_accepted(
    hass: HomeAssistant,
    sonos_player: er.RegistryEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    entity_ids = [
        entity_registry.async_get_or_create(
            "media_player", "sonos", f"RINCON_N{index}"
        ).entity_id
        for index in range(MAX_SELECTED_PLAYERS)
    ]
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ENTITY_ID: entity_ids}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(result["data"][CONF_ENTITY_REGISTRY_IDS]) == MAX_SELECTED_PLAYERS
    assert result["data"][CONF_NOW_PLAYING] is False
    assert result["data"][CONF_SCROBBLING] is False


async def test_second_entry_still_blocked(
    hass: HomeAssistant, pilot_entry: MockConfigEntry
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_reconfigure_preserves_link_and_runtime(
    hass: HomeAssistant,
    pilot_entry: MockConfigEntry,
    sonos_player: er.RegistryEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    link_pilot(hass, pilot_entry, **{CONF_NOW_PLAYING: True})
    other = entity_registry.async_get_or_create("media_player", "sonos", "RINCON_2")
    await hass.config_entries.async_setup(pilot_entry.entry_id)
    await hass.async_block_till_done()
    runtime = pilot_entry.runtime_data

    result = await pilot_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ENTITY_ID: [sonos_player.entity_id, other.entity_id]}
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    assert pilot_entry.runtime_data is runtime
    assert pilot_entry.data[CONF_ENTITY_REGISTRY_IDS] == [sonos_player.id, other.id]
    assert pilot_entry.data[CONF_LASTFM] == LINK
    assert pilot_entry.data[CONF_LASTFM_LINK_REVISION] == 1
    assert pilot_entry.data[CONF_NOW_PLAYING] is True
    # Scrobbling was off, so there is no worker to fence and no period to end.
    assert pilot_entry.data[CONF_DELIVERY_EPOCH] == 1
    await hass.config_entries.async_unload(pilot_entry.entry_id)


async def test_new_speaker_is_selectable_but_not_selected(
    hass: HomeAssistant,
    pilot_entry: MockConfigEntry,
    sonos_player: er.RegistryEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    await hass.config_entries.async_setup(pilot_entry.entry_id)
    await hass.async_block_till_done()
    newcomer = entity_registry.async_get_or_create("media_player", "sonos", "RINCON_9")
    await hass.async_block_till_done()

    assert pilot_entry.data[CONF_ENTITY_REGISTRY_IDS] == [sonos_player.id]
    result = await pilot_entry.start_reconfigure_flow(hass)
    schema = result["data_schema"]
    assert schema is not None
    field = next(iter(schema.schema))
    assert field.description == {"suggested_value": [sonos_player.entity_id]}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ENTITY_ID: [sonos_player.entity_id, newcomer.entity_id]},
    )
    await hass.async_block_till_done()

    assert pilot_entry.data[CONF_ENTITY_REGISTRY_IDS] == [sonos_player.id, newcomer.id]
    await hass.config_entries.async_unload(pilot_entry.entry_id)


async def _switch(
    hass: HomeAssistant, entry: MockConfigEntry, feature: Feature, on: bool
) -> None:
    service = delivery_service(hass, entry.entry_id)
    assert service is not None
    if on:
        await service.controls.async_turn_on(feature)
    else:
        await service.controls.async_turn_off(feature)


async def test_the_menu_offers_the_account_and_no_delivery_control(
    hass: HomeAssistant, loaded_pilot: MockConfigEntry
) -> None:
    menu = await start_options(hass, loaded_pilot)
    assert menu["menu_options"] == ["connect"]

    link_pilot(hass, loaded_pilot)
    menu = await start_options(hass, loaded_pilot)
    assert menu["menu_options"] == ["replace", "disconnect"]


async def test_an_enable_records_when_the_scrobbling_period_began(
    hass: HomeAssistant, loaded_pilot: MockConfigEntry
) -> None:
    link_pilot(hass, loaded_pilot)
    before = dt_util.utcnow().timestamp()

    await _switch(hass, loaded_pilot, Feature.SCROBBLING, True)

    assert before <= loaded_pilot.data[CONF_SCROBBLING_SINCE]
    assert loaded_pilot.data[CONF_SCROBBLING_SINCE] <= dt_util.utcnow().timestamp()
    assert loaded_pilot.data[CONF_SCROBBLING] is True
    assert loaded_pilot.data[CONF_NOW_PLAYING] is False
    assert loaded_pilot.data[CONF_DELIVERY_EPOCH] == 2
    assert loaded_pilot.data[CONF_NOW_PLAYING_EPOCH] == 0


async def _enable(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    for feature in Feature:
        await _switch(hass, entry, feature, True)
    assert entry.data[CONF_NOW_PLAYING] is True
    assert entry.data[CONF_SCROBBLING] is True


def assert_all_off(entry: MockConfigEntry) -> None:
    assert entry.data[CONF_NOW_PLAYING] is False
    assert entry.data[CONF_SCROBBLING] is False
    assert entry.data[CONF_SCROBBLING_SINCE] is None


@pytest.mark.usefixtures("lastfm_server")
async def test_first_connect_leaves_delivery_off(
    hass: HomeAssistant, loaded_pilot: MockConfigEntry
) -> None:
    await link_account(hass, loaded_pilot)

    assert_all_off(loaded_pilot)
    assert loaded_pilot.data[CONF_DELIVERY_EPOCH] == 1


@pytest.mark.usefixtures("lastfm_server")
async def test_unlink_then_relink_stays_off(
    hass: HomeAssistant, loaded_pilot: MockConfigEntry
) -> None:
    await link_account(hass, loaded_pilot)
    await _enable(hass, loaded_pilot)
    epoch = loaded_pilot.data[CONF_DELIVERY_EPOCH]

    form = await choose(hass, await start_options(hass, loaded_pilot), "disconnect")
    await hass.config_entries.options.async_configure(form["flow_id"], {})

    assert_all_off(loaded_pilot)
    assert loaded_pilot.data[CONF_DELIVERY_EPOCH] == epoch + 1

    await link_account(hass, loaded_pilot)

    assert_all_off(loaded_pilot)


@pytest.mark.usefixtures("lastfm_server")
async def test_replace_switches_delivery_off_and_needs_fresh_enable(
    hass: HomeAssistant, loaded_pilot: MockConfigEntry
) -> None:
    await link_account(hass, loaded_pilot)
    await _enable(hass, loaded_pilot)
    epoch = loaded_pilot.data[CONF_DELIVERY_EPOCH]

    await link_account(hass, loaded_pilot, "replace")

    assert_all_off(loaded_pilot)
    assert loaded_pilot.data[CONF_DELIVERY_EPOCH] == epoch + 1
    await _enable(hass, loaded_pilot)


@pytest.mark.parametrize("enable", [True, False])
async def test_a_switch_commit_signals_the_new_state(
    hass: HomeAssistant, loaded_pilot: MockConfigEntry, enable: bool
) -> None:
    link_pilot(hass, loaded_pilot)
    if not enable:
        await _switch(hass, loaded_pilot, Feature.SCROBBLING, True)
    base = loaded_pilot.data[CONF_DELIVERY_EPOCH]
    seen: list[tuple[bool, int]] = []

    # A plain function would run later in the executor, and read later data.
    @callback
    def record() -> None:
        seen.append(
            (
                loaded_pilot.data[CONF_SCROBBLING],
                loaded_pilot.data[CONF_DELIVERY_EPOCH],
            )
        )

    unsubscribe = async_dispatcher_connect(
        hass, SIGNAL_LASTFM_LINK_CHANGED.format(loaded_pilot.entry_id), record
    )
    await _switch(hass, loaded_pilot, Feature.SCROBBLING, enable)
    unsubscribe()

    # An enable stages the epoch off and publishes True at that same epoch.
    staged = base + 1
    assert seen == ([(False, staged), (True, staged)] if enable else [(False, staged)])
