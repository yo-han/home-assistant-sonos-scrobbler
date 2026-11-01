"""Status sensors and the local current track; Recorder stores all they expose.

The two diagnostic sensors carry fixed states and counts. Only the current-track
sensor shows music: it reads the runtime's settled track, and restores nothing after a
restart. The current-speaker sensor names the room of
that same track and carries no music.
"""

from typing import Any, override

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_FRIENDLY_NAME,
    MAX_LENGTH_STATE_STATE,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PilotConfigEntry
from .const import (
    CONF_ENTITY_REGISTRY_IDS,
    CONF_LASTFM,
    DOMAIN,
    MODE_DELIVERY_ENABLED,
    MODE_OBSERVATION_ONLY,
    SIGNAL_LASTFM_LINK_CHANGED,
    TITLE,
)
from .controls import any_enabled
from .delivery import DeliveryService, DeliveryStatus, delivery_service
from .observation import Stage, status_attributes


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PilotConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the two status sensors, the current track and its speaker."""
    service = delivery_service(hass, entry.entry_id)
    assert service is not None
    async_add_entities(
        [
            ObservationStatusSensor(entry, service),
            DeliveryStatusSensor(entry, service),
            CurrentTrackSensor(entry),
            CurrentSpeakerSensor(entry),
        ]
    )


def entry_device_info(entry: PilotConfigEntry) -> DeviceInfo:
    """Return the one device every entity of the entry shares."""
    return DeviceInfo(
        entry_type=DeviceEntryType.SERVICE,
        identifiers={(DOMAIN, entry.entry_id)},
        name=TITLE,
    )


def track_state(artist: str, title: str) -> str:
    """Format a bounded display state; full bounded fields remain attributes."""
    return f"{artist} — {title}"[:MAX_LENGTH_STATE_STATE]


def delivery_attributes(service: DeliveryService) -> dict[str, Any]:
    """Return the delivery allowlist shared by the sensor and diagnostics."""
    snapshot = service.status
    return {
        "pending_count": snapshot.pending_count,
        "held_count": snapshot.held_count,
        "uncertain_count": snapshot.uncertain_count,
        "sent_total": snapshot.sent_total,
        "skipped_total": snapshot.skipped_total,
        "ignored_total": snapshot.ignored_total,
        "oldest_queue_age_minutes": snapshot.oldest_queue_age_minutes,
        "error_code": snapshot.error_code,
    }


class ObservationStatusSensor(SensorEntity):
    """Report the observation stage with bounded, metadata-free attributes."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "status"

    def __init__(self, entry: PilotConfigEntry, service: DeliveryService) -> None:
        """Bind the sensor to the runtime and the speakers of the config entry."""
        self._entry = entry
        self._runtime = entry.runtime_data
        self._speakers = service.speakers
        self._attr_options = [stage.value for stage in Stage]
        self._attr_unique_id = f"{entry.entry_id}_status"
        self._attr_device_info = entry_device_info(entry)

    @property
    @override
    def native_value(self) -> str:
        """Return the current stage."""
        return self._runtime.snapshot.stage.value

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the allowlisted attributes."""
        enabled = any_enabled(self._entry.data)
        return {
            **status_attributes(
                self._runtime.snapshot,
                mode=MODE_DELIVERY_ENABLED if enabled else MODE_OBSERVATION_ONLY,
                selected_speakers=len(self._entry.data[CONF_ENTITY_REGISTRY_IDS]),
                active_groups=self._runtime.active_groups,
            ),
            "lastfm_linked": CONF_LASTFM in self._entry.data,
            "speakers_left_out": self._speakers.left_out,
        }

    @override
    async def async_added_to_hass(self) -> None:
        """Write a new state after every observation, catalog or link change."""
        self.async_on_remove(
            self._runtime.async_add_listener(self.async_write_ha_state)
        )
        self.async_on_remove(
            self._speakers.async_add_listener(self.async_write_ha_state)
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_LASTFM_LINK_CHANGED.format(self._entry.entry_id),
                self.async_write_ha_state,
            )
        )


class DeliveryStatusSensor(SensorEntity):
    """Report what delivery is doing, in counts and fixed codes only."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "delivery_status"

    def __init__(self, entry: PilotConfigEntry, service: DeliveryService) -> None:
        """Bind the sensor to the delivery service of the config entry."""
        self._service = service
        self._attr_options = [status.value for status in DeliveryStatus]
        self._attr_unique_id = f"{entry.entry_id}_delivery"
        self._attr_device_info = entry_device_info(entry)

    @property
    @override
    def native_value(self) -> str:
        """Return the current delivery status."""
        return self._service.status.status.value

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the allowlisted attributes."""
        return delivery_attributes(self._service)

    @override
    async def async_added_to_hass(self) -> None:
        """Write a new state whenever the service publishes a change."""
        self.async_on_remove(
            self._service.async_add_listener(self.async_write_ha_state)
        )


class CurrentTrackSensor(SensorEntity):
    """Show what plays right now, without any Last.fm link or switch."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:music"
    _attr_should_poll = False
    _attr_translation_key = "current_track"

    def __init__(self, entry: PilotConfigEntry) -> None:
        """Bind the sensor to the runtime of the config entry."""
        self._runtime = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_current_track"
        self._attr_device_info = entry_device_info(entry)

    @property
    @override
    def native_value(self) -> str | None:
        """Return artist and title, or nothing while no track is proven."""
        track = self._runtime.current_track
        if track is None:
            return None
        return track_state(track.metadata.artist, track.metadata.title)

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the bounded fields of the track, and no position or URI."""
        track = self._runtime.current_track
        if track is None:
            return None
        duration = track.duration_seconds
        return {
            "artist": track.metadata.artist,
            "title": track.metadata.title,
            "album": track.metadata.album,
            "duration": None if duration is None else round(duration),
            "speaker": track.speaker,
        }

    @override
    async def async_added_to_hass(self) -> None:
        """Offer a state after every update; an unchanged one writes nothing."""
        self.async_on_remove(
            self._runtime.async_add_listener(self.async_write_ha_state)
        )


class CurrentSpeakerSensor(SensorEntity):
    """Name the speaker of the current track, without any music or position."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:speaker"
    _attr_should_poll = False
    _attr_translation_key = "current_speaker"

    def __init__(self, entry: PilotConfigEntry) -> None:
        """Bind the sensor to the runtime of the config entry."""
        self._runtime = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_current_speaker"
        self._attr_device_info = entry_device_info(entry)

    @property
    def _speaker(self) -> str | None:
        track = self._runtime.current_track
        return None if track is None else track.speaker

    @property
    @override
    def native_value(self) -> str | None:
        """Return the speaker's name, or nothing while no track is proven."""
        speaker = self._speaker
        if speaker is None:
            return None
        state = self.hass.states.get(speaker)
        name = None if state is None else state.attributes.get(ATTR_FRIENDLY_NAME)
        if not isinstance(name, str) or not name.strip():
            name = speaker
        return name[:MAX_LENGTH_STATE_STATE]

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the canonical entity of the speaker."""
        speaker = self._speaker
        return None if speaker is None else {ATTR_ENTITY_ID: speaker}

    @override
    async def async_added_to_hass(self) -> None:
        """Offer a state after every update; an unchanged one writes nothing."""
        self.async_on_remove(
            self._runtime.async_add_listener(self.async_write_ha_state)
        )
