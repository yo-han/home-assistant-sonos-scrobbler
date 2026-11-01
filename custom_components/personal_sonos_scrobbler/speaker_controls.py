"""Which Sonos speakers may be included, and the one writer of that selection.

The catalog follows the entity registry and the friendly names, on any installation.
A new speaker is offered, never selected. A selected speaker is offered first, so the
bound on the catalog cannot hide a switch that is on.

A change happens in one step on the event loop, also for two rooms at once: the
selection is read and written without an await, so no update is lost and the order of
arrival is the order of effect. What follows it belongs to FeatureControl.
"""

import logging
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_FRIENDLY_NAME, MAX_LENGTH_STATE_STATE
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

from .const import CONF_ENTITY_REGISTRY_IDS, DOMAIN, MAX_SELECTED_PLAYERS
from .controls import FeatureControl

_LOGGER = logging.getLogger(__name__)

MEDIA_PLAYER_DOMAIN = "media_player"
MEDIA_PLAYER_PREFIX = f"{MEDIA_PLAYER_DOMAIN}."
SONOS_PLATFORM = "sonos"


@dataclass(frozen=True, slots=True)
class Speaker:
    """One speaker on offer; registry_id is its identity, the rest can change."""

    registry_id: str
    entity_id: str
    name: str


def is_sonos_media_player(entry: er.RegistryEntry) -> bool:
    """Return whether the built-in Sonos integration registered this media_player."""
    return entry.domain == MEDIA_PLAYER_DOMAIN and entry.platform == SONOS_PLATFORM


def enabled_sonos_players(registry: er.EntityRegistry) -> Iterator[er.RegistryEntry]:
    """Yield every Sonos media_player that Home Assistant has not disabled."""
    return (
        entry
        for entry in registry.entities.values()
        if is_sonos_media_player(entry) and entry.disabled_by is None
    )


@callback
def is_media_player_update(data: er.EventEntityRegistryUpdatedData) -> bool:
    """Tell whether a registry event concerns a media_player."""
    return data["entity_id"].startswith(MEDIA_PLAYER_PREFIX)


def _speaker_name(hass: HomeAssistant, player: er.RegistryEntry) -> str:
    """Return the friendly name, or what it comes from while no state exists yet."""
    state = hass.states.get(player.entity_id)
    device = (
        dr.async_get(hass).async_get(player.device_id) if player.device_id else None
    )
    candidates = (
        None if state is None else state.attributes.get(ATTR_FRIENDLY_NAME),
        player.name,
        player.original_name,
        None if device is None else device.name_by_user or device.name,
    )
    for name in candidates:
        if isinstance(name, str) and name.strip():
            return name[:MAX_LENGTH_STATE_STATE]
    return player.entity_id


def _failure(translation_key: str) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN, translation_key=translation_key
    )


class SpeakerControl:
    """Keep the catalog current and apply every change of the selection."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry[Any], controls: FeatureControl
    ) -> None:
        """Bind to one entry; nothing is read before async_start."""
        self._hass = hass
        self._entry = entry
        self._controls = controls
        self._catalog: tuple[Speaker, ...] = ()
        self._left_out = 0
        self._paused = False
        self._listeners: set[Callable[[], None]] = set()
        self._unsubscribe_registry: CALLBACK_TYPE | None = None
        self._unsubscribe_states: CALLBACK_TYPE | None = None

    @property
    def catalog(self) -> tuple[Speaker, ...]:
        """Return the speakers on offer, at most the supported number."""
        return self._catalog

    @property
    def left_out(self) -> int:
        """Return how many enabled speakers did not fit in the catalog."""
        return self._left_out

    @property
    def _selection(self) -> list[str]:
        selection: list[str] = self._entry.data[CONF_ENTITY_REGISTRY_IDS]
        return selection

    def is_included(self, registry_id: str) -> bool:
        """Return the confirmed choice for this speaker."""
        return registry_id in self._selection

    @callback
    def async_start(self) -> None:
        """Read the catalog and follow it."""
        self._unsubscribe_registry = self._hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED,
            self._async_handle_registry_update,
            event_filter=is_media_player_update,
        )
        self._async_refresh()

    @callback
    def async_stop(self) -> None:
        """Cancel every subscription."""
        if self._unsubscribe_registry is not None:
            self._unsubscribe_registry()
            self._unsubscribe_registry = None
        self._async_unsubscribe_states()
        self._listeners.clear()

    @callback
    def async_pause(self) -> None:
        """Hold the catalog as it is; every subscription and listener stays."""
        self._paused = True

    @callback
    def async_resume(self) -> None:
        """Follow again, starting with whatever changed during the pause."""
        self._paused = False
        self._async_refresh()

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Call listener after every change of the catalog."""
        self._listeners.add(listener)

        @callback
        def remove_listener() -> None:
            self._listeners.discard(listener)

        return remove_listener

    async def async_include(self, registry_id: str) -> None:
        """Include a speaker on offer; a click twice is one change."""
        selection = self._selection
        if registry_id in selection:
            return
        if all(speaker.registry_id != registry_id for speaker in self._catalog):
            raise _failure("speaker_unavailable")
        if len(selection) >= MAX_SELECTED_PLAYERS:
            raise _failure("too_many_speakers")
        await self.async_set_selection([*selection, registry_id])

    async def async_exclude(self, registry_id: str) -> None:
        """Exclude a speaker, also one that is no longer on offer."""
        await self.async_set_selection(
            [selected for selected in self._selection if selected != registry_id]
        )

    async def async_set_selection(self, registry_ids: Iterable[str]) -> None:
        """Replace the selection; the same speakers again change nothing."""
        selection = list(registry_ids)
        if set(selection) != set(self._selection):
            await self._controls.async_change_selection(selection, self._async_rebind)

    @callback
    def _async_rebind(self, registry_ids: list[str]) -> None:
        self._entry.runtime_data.async_set_selection(registry_ids)
        self._async_refresh()

    @callback
    def _async_handle_registry_update(
        self, _event: Event[er.EventEntityRegistryUpdatedData]
    ) -> None:
        self._async_refresh()

    @callback
    def _async_handle_state_change(self, event: Event[EventStateChangedData]) -> None:
        """Read only the one speaker: this runs for every position update."""
        entity_id = event.data["entity_id"]
        speaker = next(
            (item for item in self._catalog if item.entity_id == entity_id), None
        )
        player = er.async_get(self._hass).async_get(entity_id)
        if (
            speaker is not None
            and player is not None
            and _speaker_name(self._hass, player) != speaker.name
        ):
            self._async_refresh()

    @callback
    def _async_refresh(self) -> None:
        """Rebuild the catalog, and report what changed."""
        if self._paused:
            return
        selection = self._selection
        players = sorted(
            enabled_sonos_players(er.async_get(self._hass)),
            key=lambda player: (player.id not in selection, player.entity_id),
        )
        offered = players[:MAX_SELECTED_PLAYERS]
        catalog = tuple(
            Speaker(player.id, player.entity_id, _speaker_name(self._hass, player))
            for player in offered
        )
        left_out = len(players) - len(offered)
        if left_out and left_out != self._left_out:
            _LOGGER.warning(
                "%d Sonos speakers have no switch: at most %d are supported",
                left_out,
                MAX_SELECTED_PLAYERS,
            )
        if catalog == self._catalog and left_out == self._left_out:
            return
        tracked = {speaker.entity_id for speaker in self._catalog}
        self._catalog = catalog
        self._left_out = left_out
        if tracked != {speaker.entity_id for speaker in catalog}:
            self._async_follow_states()
        for listener in tuple(self._listeners):
            listener()

    @callback
    def _async_follow_states(self) -> None:
        self._async_unsubscribe_states()
        self._unsubscribe_states = async_track_state_change_event(
            self._hass,
            [speaker.entity_id for speaker in self._catalog],
            self._async_handle_state_change,
        )

    @callback
    def _async_unsubscribe_states(self) -> None:
        if self._unsubscribe_states is not None:
            self._unsubscribe_states()
            self._unsubscribe_states = None
