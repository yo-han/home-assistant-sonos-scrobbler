"""The Last.fm switches and one inclusion switch per Sonos speaker.

Everything they do happens behind FeatureControl and SpeakerControl.
"""

from contextlib import suppress
from typing import Any, override

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PilotConfigEntry
from .const import SIGNAL_LASTFM_LINK_CHANGED
from .controls import Feature, FeatureControl
from .delivery import delivery_service
from .sensor import entry_device_info
from .speaker_controls import Speaker, SpeakerControl


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PilotConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one switch per feature, and keep one per speaker on offer."""
    service = delivery_service(hass, entry.entry_id)
    assert service is not None
    async_add_entities(
        FeatureSwitch(entry, service.controls, feature) for feature in Feature
    )
    switches = _SpeakerSwitches(hass, entry, service.speakers, async_add_entities)
    entry.async_on_unload(service.speakers.async_add_listener(switches.async_sync))
    switches.async_sync()


class FeatureSwitch(SwitchEntity):
    """Show the confirmed setting; a service call returns once it is applied."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, entry: PilotConfigEntry, controls: FeatureControl, feature: Feature
    ) -> None:
        """Bind the switch to one feature of the config entry."""
        self._entry_id = entry.entry_id
        self._controls = controls
        self._feature = feature
        self._attr_translation_key = feature.value
        self._attr_unique_id = f"{entry.entry_id}_{feature.value}"
        self._attr_device_info = entry_device_info(entry)

    @property
    @override
    def is_on(self) -> bool:
        """Return the persisted setting, never an optimistic one."""
        return self._controls.is_on(self._feature)

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Switch on, or raise what stood in the way."""
        await self._controls.async_turn_on(self._feature)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Switch off."""
        await self._controls.async_turn_off(self._feature)

    @override
    async def async_added_to_hass(self) -> None:
        """Write a new state after every change, also one a link flow made."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_LASTFM_LINK_CHANGED.format(self._entry_id),
                self.async_write_ha_state,
            )
        )


class SpeakerSwitch(SwitchEntity):
    """Include one speaker in everything; the registry id is its identity."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "speaker"

    def __init__(
        self, entry: PilotConfigEntry, speakers: SpeakerControl, speaker: Speaker
    ) -> None:
        """Bind the switch to one speaker of the config entry."""
        self._entry_id = entry.entry_id
        self._speakers = speakers
        self._speaker = speaker
        self._attr_translation_placeholders = {"speaker": speaker.name}
        self._attr_unique_id = speaker_unique_id(entry.entry_id, speaker.registry_id)
        self._attr_device_info = entry_device_info(entry)

    @property
    def speaker(self) -> Speaker:
        """Return the speaker as last seen."""
        return self._speaker

    @property
    @override
    def is_on(self) -> bool:
        """Return the persisted choice, never an optimistic one."""
        return self._speakers.is_included(self._speaker.registry_id)

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the source entity for automations, and nothing about the music."""
        return {"speaker": self._speaker.entity_id}

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Include the speaker, or raise what stood in the way."""
        await self._speakers.async_include(self._speaker.registry_id)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Exclude the speaker; this took effect before anything is awaited."""
        await self._speakers.async_exclude(self._speaker.registry_id)

    @override
    async def async_added_to_hass(self) -> None:
        """Write a new state after every change, also one Reconfigure made."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_LASTFM_LINK_CHANGED.format(self._entry_id),
                self.async_write_ha_state,
            )
        )

    @callback
    def async_show(self, speaker: Speaker) -> None:
        """Follow a new name or entity id under the same identity."""
        self._speaker = speaker
        self._attr_translation_placeholders = {"speaker": speaker.name}
        # Home Assistant caches the translated name and has no other way to drop it.
        with suppress(AttributeError):
            del self.name
        if self.hass is None or self.platform_data is None:
            return
        name = self.name
        er.async_get(self.hass).async_update_entity(
            self.entity_id, original_name=name if isinstance(name, str) else None
        )
        self.async_write_ha_state()


def speaker_unique_id(entry_id: str, registry_id: str) -> str:
    """Return the stable identity of a switch; a new speaker gets a new one."""
    return f"{entry_id}_speaker_{registry_id}"


class _SpeakerSwitches:
    """Hold one switch per speaker on offer, across every catalog change."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: PilotConfigEntry,
        speakers: SpeakerControl,
        add_entities: AddConfigEntryEntitiesCallback,
    ) -> None:
        """Start empty; a registry entry from an earlier run is found by id."""
        self._hass = hass
        self._entry = entry
        self._speakers = speakers
        self._add_entities = add_entities
        self._switches: dict[str, SpeakerSwitch] = {}

    @callback
    def async_sync(self) -> None:
        """Remove what left the catalog, follow what changed, add what is new."""
        catalog = {speaker.registry_id: speaker for speaker in self._speakers.catalog}
        self._async_remove_obsolete(catalog)
        added: list[SpeakerSwitch] = []
        for registry_id, speaker in catalog.items():
            switch = self._switches.get(registry_id)
            if switch is None:
                switch = SpeakerSwitch(self._entry, self._speakers, speaker)
                self._switches[registry_id] = switch
                added.append(switch)
            elif switch.speaker != speaker:
                switch.async_show(speaker)
        if added:
            self._add_entities(added)

    @callback
    def _async_remove_obsolete(self, catalog: dict[str, Speaker]) -> None:
        wanted = {
            speaker_unique_id(self._entry.entry_id, registry_id)
            for registry_id in catalog
        }
        prefix = speaker_unique_id(self._entry.entry_id, "")
        registry = er.async_get(self._hass)
        for entry in er.async_entries_for_config_entry(registry, self._entry.entry_id):
            if entry.unique_id.startswith(prefix) and entry.unique_id not in wanted:
                registry.async_remove(entry.entity_id)
        for registry_id in self._switches.keys() - catalog.keys():
            del self._switches[registry_id]
