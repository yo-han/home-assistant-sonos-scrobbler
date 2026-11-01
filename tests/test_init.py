"""Config entry lifecycle and translation consistency."""

import json
from pathlib import Path

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.personal_sonos_scrobbler.const import (
    CONFIG_MINOR_VERSION,
    CONFIG_VERSION,
    DOMAIN,
)
from custom_components.personal_sonos_scrobbler.observation import Reason, Stage
from tests.helpers import active_timers, entry_data, playing

COMPONENT = Path(__file__).parents[1] / "custom_components" / DOMAIN
PLAYING = {
    "source": "Spotify Connect",
    "media_artist": "Artist",
    "media_title": "Title",
    "media_duration": 200,
}


async def test_setup_and_unload_cancel_everything(
    hass: HomeAssistant,
    pilot_entry: MockConfigEntry,
    sonos_player: er.RegistryEntry,
) -> None:
    hass.states.async_set(
        sonos_player.entity_id, "playing", playing(sonos_player.entity_id, **PLAYING)
    )
    assert await hass.config_entries.async_setup(pilot_entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(pilot_entry.entry_id)
    await hass.async_block_till_done()
    # Home Assistant keeps the sensor platform loaded after the first unload.
    listeners_without_pilot = hass.bus.async_listeners()

    assert await hass.config_entries.async_setup(pilot_entry.entry_id)
    await hass.async_block_till_done()
    assert pilot_entry.state is ConfigEntryState.LOADED
    assert active_timers(hass) > 0
    assert hass.bus.async_listeners() != listeners_without_pilot

    assert await hass.config_entries.async_unload(pilot_entry.entry_id)
    await hass.async_block_till_done()

    assert pilot_entry.state is ConfigEntryState.NOT_LOADED
    assert active_timers(hass) == 0
    assert hass.bus.async_listeners() == listeners_without_pilot


async def test_reload_resets_counters(
    hass: HomeAssistant,
    pilot_entry: MockConfigEntry,
    sonos_player: er.RegistryEntry,
) -> None:
    hass.states.async_set(
        sonos_player.entity_id,
        "playing",
        playing(sonos_player.entity_id, **PLAYING, media_position=10),
    )
    assert await hass.config_entries.async_setup(pilot_entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set(
        sonos_player.entity_id,
        "playing",
        playing(sonos_player.entity_id, **PLAYING, media_position=150),
    )
    await hass.async_block_till_done()
    assert pilot_entry.runtime_data.snapshot.seek_count == 1

    assert await hass.config_entries.async_reload(pilot_entry.entry_id)
    await hass.async_block_till_done()

    assert pilot_entry.runtime_data.snapshot.seek_count == 0
    await hass.config_entries.async_unload(pilot_entry.entry_id)


async def test_setup_with_removed_entity_stays_loaded(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=CONFIG_VERSION,
        minor_version=CONFIG_MINOR_VERSION,
        data=entry_data("f" * 32),
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.snapshot.reason is Reason.ENTITY_MISSING


def flatten(value: object, prefix: str = "") -> set[str]:
    if isinstance(value, dict):
        return {
            path
            for key, child in value.items()
            for path in flatten(child, f"{prefix}{key}.")
        }
    return {prefix.rstrip(".")}


def test_translations_have_identical_keys() -> None:
    strings = json.loads((COMPONENT / "strings.json").read_text())
    english = json.loads((COMPONENT / "translations" / "en.json").read_text())
    dutch = json.loads((COMPONENT / "translations" / "nl.json").read_text())

    assert strings == english
    assert flatten(english) == flatten(dutch)


def test_translations_cover_every_stage() -> None:
    english = json.loads((COMPONENT / "translations" / "en.json").read_text())

    assert set(english["entity"]["sensor"]["status"]["state"]) == set(Stage)
