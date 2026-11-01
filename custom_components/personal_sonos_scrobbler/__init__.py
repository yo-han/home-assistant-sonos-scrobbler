"""Observe qualifying Spotify listens on Sonos with optional delivery."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_DELIVERY_EPOCH,
    CONF_ENTITY_REGISTRY_ID,
    CONF_ENTITY_REGISTRY_IDS,
    CONF_OUTBOX_CREATED,
    CONFIG_MINOR_VERSION,
    CONFIG_VERSION,
    FEATURES_MINOR_VERSION,
    LEGACY_TITLE,
    TITLE,
)
from .controls import migrated_features
from .delivery import DeliveryService, async_remove_ledger, delivery_service
from .runtime import ObservationRuntime

type PilotConfigEntry = ConfigEntry[ObservationRuntime]

PLATFORMS = [Platform.SENSOR, Platform.SWITCH]


async def async_migrate_entry(hass: HomeAssistant, entry: PilotConfigEntry) -> bool:
    """Upgrade without losing the link; only an earlier Now Playing stays on.

    Below the features' minor version the old mode is read once. After that the data
    is left alone: the switches of a newer entry hold the user's own choice. The old
    default title goes; a title the user chose stays.
    """
    if entry.version > CONFIG_VERSION:
        return False
    data = dict(entry.data)
    if entry.version == 1:
        registry_id = data.pop(CONF_ENTITY_REGISTRY_ID, None)
        if not isinstance(registry_id, str) or not registry_id:
            return False
        data |= {
            CONF_ENTITY_REGISTRY_IDS: [registry_id],
            CONF_DELIVERY_EPOCH: 0,
            CONF_OUTBOX_CREATED: False,
        }
    elif entry.minor_version >= CONFIG_MINOR_VERSION:
        return True
    if entry.version == 1 or entry.minor_version < FEATURES_MINOR_VERSION:
        data = migrated_features(data)
    hass.config_entries.async_update_entry(
        entry,
        data=data,
        title=TITLE if entry.title == LEGACY_TITLE else entry.title,
        version=CONFIG_VERSION,
        minor_version=CONFIG_MINOR_VERSION,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PilotConfigEntry) -> bool:
    """Start observing the selected entities; a missing one is reported, not fatal."""
    delivery = DeliveryService(hass, entry)
    runtime = ObservationRuntime(
        hass,
        entry.entry_id,
        entry.data[CONF_ENTITY_REGISTRY_IDS],
        delivery.async_accept,
        delivery.async_on_occurrence_change,
        delivery.async_on_current_playback,
    )
    entry.runtime_data = runtime
    entry.async_on_unload(runtime.async_stop)
    entry.async_on_unload(delivery.async_stop)
    entry.async_on_unload(delivery.speakers.async_stop)
    await delivery.async_start()
    delivery.speakers.async_start()
    runtime.seed_resume_hints(*delivery.resume_state)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    runtime.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PilotConfigEntry) -> bool:
    """Unload the platforms, then stop delivery; the runtime stops after that.

    The catalog pauses before the first await: a switch added from here on would join
    a platform on its way out and outlive it. A refused or failed unload resumes it,
    so nothing is lost. Only a finished unload stops it for good.
    """
    delivery = delivery_service(hass, entry.entry_id)
    if delivery is None:
        return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    delivery.speakers.async_pause()
    unloaded = False
    try:
        unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    finally:
        if not unloaded:
            delivery.speakers.async_resume()
    if unloaded:
        delivery.speakers.async_stop()
        await delivery.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: PilotConfigEntry) -> None:
    """Delete the entry's private ledger, once nothing can write it anymore."""
    await async_remove_ledger(hass, entry.entry_id)
