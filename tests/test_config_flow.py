"""The flow accepts exactly one enabled media_player of the sonos integration."""

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.personal_sonos_scrobbler.const import (
    CONF_ENTITY_REGISTRY_IDS,
    CONF_LASTFM,
    CONF_LASTFM_LINK_REVISION,
    DOMAIN,
)
from custom_components.personal_sonos_scrobbler.delivery import delivery_service
from tests.helpers import entry_data


async def start_flow(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    return result["flow_id"]


async def test_user_flow_stores_registry_id_not_entity_id(
    hass: HomeAssistant, sonos_player: er.RegistryEntry
) -> None:
    flow_id = await start_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_ENTITY_ID: [sonos_player.entity_id]}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == entry_data(sonos_player.id)


async def test_rejects_non_sonos_platform_with_same_looking_name(
    hass: HomeAssistant,
    sonos_player: er.RegistryEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    lookalike = entity_registry.async_get_or_create(
        "media_player", "cast", "cast-1", suggested_object_id="sonos_keuken"
    )
    assert lookalike.entity_id == "media_player.sonos_keuken"
    flow_id = await start_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_ENTITY_ID: [lookalike.entity_id]}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_ENTITY_ID: "not_sonos_media_player"}


async def test_rejects_unregistered_entity(
    hass: HomeAssistant, sonos_player: er.RegistryEntry
) -> None:
    flow_id = await start_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_ENTITY_ID: ["media_player.sonos_living_room"]}
    )

    assert result["errors"] == {CONF_ENTITY_ID: "not_sonos_media_player"}


async def test_rejects_disabled_entity(
    hass: HomeAssistant,
    sonos_player: er.RegistryEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    disabled = entity_registry.async_get_or_create(
        "media_player",
        "sonos",
        "RINCON_2",
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    flow_id = await start_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_ENTITY_ID: [disabled.entity_id]}
    )

    assert result["errors"] == {CONF_ENTITY_ID: "entity_disabled"}


async def test_rejects_sensor_domain(
    hass: HomeAssistant,
    sonos_player: er.RegistryEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    sensor = entity_registry.async_get_or_create("sensor", "sonos", "RINCON_1-audio")
    flow_id = await start_flow(hass)

    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            flow_id, {CONF_ENTITY_ID: [sensor.entity_id]}
        )


async def test_aborts_without_sonos_players(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_sonos_media_players"


async def test_second_entry_is_blocked(
    hass: HomeAssistant, pilot_entry: MockConfigEntry
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_a_fresh_entry_selects_nothing_unless_asked(
    hass: HomeAssistant,
    sonos_player: er.RegistryEntry,
    entity_registry: er.EntityRegistry,
    private_config_dir: None,
) -> None:
    entity_registry.async_get_or_create("media_player", "sonos", "RINCON_2")
    flow_id = await start_flow(hass)

    result = await hass.config_entries.flow.async_configure(flow_id, {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == entry_data()
    entry = result["result"]
    assert entry.state is ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_reconfigure_outside_a_running_integration_writes_nothing(
    hass: HomeAssistant,
    pilot_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    other = entity_registry.async_get_or_create("media_player", "sonos", "RINCON_2")
    data = dict(pilot_entry.data)

    result = await pilot_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ENTITY_ID: [other.entity_id]}
    )

    assert result["reason"] == "not_running"
    assert dict(pilot_entry.data) == data


async def test_reconfigure_says_when_scrobbling_stopped(
    hass: HomeAssistant,
    pilot_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = entity_registry.async_get_or_create("media_player", "sonos", "RINCON_2")
    await hass.config_entries.async_setup(pilot_entry.entry_id)
    await hass.async_block_till_done()

    async def fail(_ids: list[str]) -> None:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="selection_without_scrobbling",
        )

    service = delivery_service(hass, pilot_entry.entry_id)
    assert service is not None
    monkeypatch.setattr(service.speakers, "async_set_selection", fail)

    result = await pilot_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ENTITY_ID: [other.entity_id]}
    )

    assert result["reason"] == "selection_without_scrobbling"
    await hass.config_entries.async_unload(pilot_entry.entry_id)
    await hass.async_block_till_done()


async def test_reconfigure_applies_the_selection_without_a_reload(
    hass: HomeAssistant,
    pilot_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    await hass.config_entries.async_setup(pilot_entry.entry_id)
    await hass.async_block_till_done()
    other = entity_registry.async_get_or_create("media_player", "sonos", "RINCON_2")
    runtime = pilot_entry.runtime_data

    result = await pilot_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ENTITY_ID: [other.entity_id]}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert pilot_entry.data == entry_data(other.id)
    assert pilot_entry.runtime_data is runtime
    await hass.config_entries.async_unload(pilot_entry.entry_id)
    await hass.async_block_till_done()


async def test_reconfigure_recovers_from_removed_entity(
    hass: HomeAssistant,
    pilot_entry: MockConfigEntry,
    sonos_player: er.RegistryEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    await hass.config_entries.async_setup(pilot_entry.entry_id)
    await hass.async_block_till_done()
    replacement = entity_registry.async_get_or_create(
        "media_player", "sonos", "RINCON_2"
    )
    entity_registry.async_remove(sonos_player.entity_id)
    await hass.async_block_till_done()
    assert pilot_entry.runtime_data.entity_present is False

    result = await pilot_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ENTITY_ID: [replacement.entity_id]}
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    assert pilot_entry.runtime_data.entity_present is True


async def test_reconfigure_rejects_invalid_entity(
    hass: HomeAssistant,
    pilot_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    lookalike = entity_registry.async_get_or_create("media_player", "cast", "cast-1")
    await hass.config_entries.async_setup(pilot_entry.entry_id)
    await hass.async_block_till_done()

    result = await pilot_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ENTITY_ID: [lookalike.entity_id]}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_ENTITY_ID: "not_sonos_media_player"}


async def test_reconfigure_preserves_the_lastfm_link(
    hass: HomeAssistant,
    pilot_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    link = {"api_key": "a", "shared_secret": "b", "session_key": "c"}
    hass.config_entries.async_update_entry(
        pilot_entry,
        data={
            **pilot_entry.data,
            CONF_LASTFM: link,
            CONF_LASTFM_LINK_REVISION: 3,
        },
    )
    other = entity_registry.async_get_or_create("media_player", "sonos", "RINCON_2")
    await hass.config_entries.async_setup(pilot_entry.entry_id)
    await hass.async_block_till_done()

    result = await pilot_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ENTITY_ID: [other.entity_id]}
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    assert pilot_entry.data == {
        **entry_data(other.id),
        CONF_LASTFM: link,
        CONF_LASTFM_LINK_REVISION: 3,
    }


@pytest.mark.parametrize(
    "cleared", [{CONF_ENTITY_ID: []}, {}], ids=["empty_list", "omitted"]
)
async def test_reconfigure_clears_the_selection_however_the_form_says_so(
    hass: HomeAssistant,
    loaded_pilot: MockConfigEntry,
    sonos_player: er.RegistryEntry,
    cleared: dict[str, list[str]],
) -> None:
    form = await loaded_pilot.start_reconfigure_flow(hass)
    assert form["type"] is FlowResultType.FORM
    field = next(iter(form["data_schema"].schema))
    assert field.description == {"suggested_value": [sonos_player.entity_id]}

    result = await hass.config_entries.flow.async_configure(form["flow_id"], cleared)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert loaded_pilot.data[CONF_ENTITY_REGISTRY_IDS] == []


async def test_a_refused_reconfigure_shows_the_selection_again(
    hass: HomeAssistant, loaded_pilot: MockConfigEntry, sonos_player: er.RegistryEntry
) -> None:
    form = await loaded_pilot.start_reconfigure_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        form["flow_id"], {CONF_ENTITY_ID: ["media_player.not_there"]}
    )

    assert result["errors"] == {CONF_ENTITY_ID: "not_sonos_media_player"}
    field = next(iter(result["data_schema"].schema))
    assert field.description == {"suggested_value": [sonos_player.entity_id]}
    assert loaded_pilot.data[CONF_ENTITY_REGISTRY_IDS] == [sonos_player.id]
