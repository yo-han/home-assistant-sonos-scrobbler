"""Redacted diagnostics: the sensor allowlists, without entity ids or entry data."""

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from . import PilotConfigEntry
from .const import (
    CONF_ENTITY_REGISTRY_IDS,
    CONF_LASTFM,
    DOMAIN,
    MODE_DELIVERY_ENABLED,
    MODE_OBSERVATION_ONLY,
)
from .controls import any_enabled
from .delivery import delivery_service
from .observation import status_attributes
from .sensor import delivery_attributes


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PilotConfigEntry
) -> dict[str, Any]:
    """Return the observation and delivery status of the config entry."""
    runtime = entry.runtime_data
    snapshot = runtime.snapshot
    integration = await async_get_integration(hass, DOMAIN)
    enabled = any_enabled(entry.data)
    service = delivery_service(hass, entry.entry_id)
    return {
        **status_attributes(
            snapshot,
            mode=MODE_DELIVERY_ENABLED if enabled else MODE_OBSERVATION_ONLY,
            selected_speakers=len(entry.data[CONF_ENTITY_REGISTRY_IDS]),
            active_groups=runtime.active_groups,
        ),
        **({} if service is None else delivery_attributes(service)),
        "delivery_status": "unavailable"
        if service is None
        else service.status.status.value,
        "stage": snapshot.stage.value,
        "entity_present": runtime.entity_present,
        "version": str(integration.version),
        "lastfm_linked": CONF_LASTFM in entry.data,
        "speakers_left_out": 0 if service is None else service.speakers.left_out,
    }
