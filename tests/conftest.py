"""Shared fixtures of the test suite."""

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.personal_sonos_scrobbler import lastfm
from custom_components.personal_sonos_scrobbler.const import (
    CONF_ENTITY_REGISTRY_IDS,
    CONFIG_MINOR_VERSION,
    CONFIG_VERSION,
    DOMAIN,
)
from tests.delivery_harness import Pilot
from tests.helpers import Advance, Room, entry_data
from tests.lastfm_server import LastFmServer
from tests.outbox_support import T0


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load the integration from custom_components."""


@pytest.fixture
async def lastfm_server(
    hass: HomeAssistant, socket_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[LastFmServer]:
    """Point the client at a loopback server; production has no such input."""
    server = LastFmServer()
    await server.start()
    monkeypatch.setattr(lastfm, "API_ROOT", server.url)
    yield server
    await server.stop()


@pytest.fixture
def sonos_player(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> er.RegistryEntry:
    """Register a media_player the way the built-in Sonos integration does."""
    sonos_entry = MockConfigEntry(domain="sonos")
    sonos_entry.add_to_hass(hass)
    return entity_registry.async_get_or_create(
        "media_player",
        "sonos",
        "RINCON_1",
        suggested_object_id="kitchen",
        config_entry=sonos_entry,
    )


@pytest.fixture
def private_config_dir(hass: HomeAssistant, tmp_path: Path) -> None:
    """Keep a ledger out of the shared test configuration directory."""
    (tmp_path / ".storage").mkdir()
    hass.config.config_dir = str(tmp_path)


@pytest.fixture
def pilot_entry(
    hass: HomeAssistant, sonos_player: er.RegistryEntry, private_config_dir: None
) -> MockConfigEntry:
    """Add a pilot config entry that observes sonos_player."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=CONFIG_VERSION,
        minor_version=CONFIG_MINOR_VERSION,
        data=entry_data(sonos_player.id),
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def room(hass: HomeAssistant, sonos_player: er.RegistryEntry) -> Room:
    return Room(hass, sonos_player)


@pytest.fixture
def hall(
    hass: HomeAssistant,
    pilot_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> Room:
    """Add a second selected speaker, before the pilot starts."""
    player = entity_registry.async_get_or_create(
        "media_player", "sonos", "RINCON_2", suggested_object_id="hall"
    )
    hass.config_entries.async_update_entry(
        pilot_entry,
        data={
            **pilot_entry.data,
            CONF_ENTITY_REGISTRY_IDS: [
                *pilot_entry.data[CONF_ENTITY_REGISTRY_IDS],
                player.id,
            ],
        },
    )
    return Room(hass, player)


@pytest.fixture
async def loaded_pilot(
    hass: HomeAssistant, pilot_entry: MockConfigEntry
) -> AsyncIterator[MockConfigEntry]:
    """Set up the pilot and unload it, so no timer outlives the test."""
    assert await hass.config_entries.async_setup(pilot_entry.entry_id)
    await hass.async_block_till_done()
    yield pilot_entry
    await hass.config_entries.async_unload(pilot_entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
def status_entity_id(
    loaded_pilot: MockConfigEntry, entity_registry: er.EntityRegistry
) -> str:
    """Return the status sensor's entity id through its unique id."""
    entity_id = entity_registry.async_get_entity_id(
        "sensor", DOMAIN, f"{loaded_pilot.entry_id}_status"
    )
    assert entity_id is not None
    return entity_id


@pytest.fixture
def advance(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> Advance:
    """Move the clock and fire the timers that are due."""

    async def _advance(seconds: float) -> None:
        freezer.tick(timedelta(seconds=seconds))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    return _advance


@pytest.fixture
async def pilot(
    hass: HomeAssistant,
    pilot_entry: MockConfigEntry,
    lastfm_server: LastFmServer,
    freezer: FrozenDateTimeFactory,
    advance: Advance,
) -> AsyncIterator[Pilot]:
    """Give a test the pilot at T0; whatever still runs is unloaded afterwards."""
    freezer.move_to(T0)
    built = Pilot(hass, pilot_entry, lastfm_server, advance)
    yield built
    if built.loaded():
        await built.unload()
