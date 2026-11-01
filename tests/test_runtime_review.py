"""Independent checks for the lifetime of the selected Sonos identity."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.personal_sonos_scrobbler.observation import Reason
from custom_components.personal_sonos_scrobbler.runtime import ObservationRuntime
from tests.helpers import playing


async def test_removed_registry_identity_cannot_resume_on_stale_entity_updates(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    sonos_player: er.RegistryEntry,
    loaded_pilot: MockConfigEntry,
) -> None:
    """A late device update or reused entity name must not resurrect observation."""
    runtime: ObservationRuntime = loaded_pilot.runtime_data
    attributes = {
        "source": "Spotify Connect",
        "media_artist": "Synthetic Artist",
        "media_title": "Synthetic Track",
        "media_duration": 120,
    }
    entity_id = sonos_player.entity_id
    hass.states.async_set(entity_id, "playing", playing(entity_id, **attributes))
    await hass.async_block_till_done()

    entity_registry.async_remove(entity_id)
    await hass.async_block_till_done()
    assert runtime.entity_present is False
    assert runtime.snapshot.reason is Reason.ENTITY_MISSING

    hass.states.async_remove(entity_id)
    await hass.async_block_till_done()
    hass.states.async_set(entity_id, "playing", playing(entity_id, **attributes))
    await hass.async_block_till_done()

    assert runtime.entity_present is False
    assert runtime.snapshot.reason is Reason.ENTITY_MISSING
    assert runtime.snapshot.observed_seconds == 0
