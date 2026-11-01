"""Bind the observation streams to Home Assistant state events and local timers."""

import logging
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HassJob,
    HomeAssistant,
    callback,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)
from homeassistant.util import dt as dt_util

from .normalize import PlaybackSample, normalize_state
from .now_playing import TrackDisplay
from .observation import ObservationSnapshot
from .playback import (
    ActiveOccurrence,
    CurrentPlayback,
    MarkerDelta,
    QualifiedPlay,
    ResumeHint,
)
from .speaker_controls import enabled_sonos_players, is_media_player_update
from .streams import TOPOLOGY_SETTLE_SECONDS, StreamSet
from .topology import (
    MAX_TRACKED_PLAYERS,
    MemberView,
    Topology,
    member_view,
    resolve_topology,
)

_LOGGER = logging.getLogger(__name__)

# Registry changes that can alter which entities exist under which id.
_TRACKED_REGISTRY_CHANGES = frozenset({"entity_id", "disabled_by"})

type OnQualified = Callable[[QualifiedPlay], None]
type OnOccurrenceChange = Callable[[MarkerDelta], None]
type OnCurrentPlayback = Callable[[tuple[CurrentPlayback, ...]], None]


class ObservationRuntime:
    """Observe the selected speakers until stopped; all state lives in RAM."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        registry_ids: Iterable[str],
        on_qualified: OnQualified | None = None,
        on_occurrence_change: OnOccurrenceChange | None = None,
        on_current_playback: OnCurrentPlayback | None = None,
    ) -> None:
        """Prepare the runtime without touching Home Assistant yet.

        on_occurrence_change receives what changed in the durable marker state,
        always after on_qualified saw the plays of the same update. A marker stays
        while its speaker is offline or paused. on_current_playback receives the
        whole current state whenever it changed, and an empty one on stop.
        """
        self._hass = hass
        self._entry_id = entry_id
        self._registry_ids = tuple(registry_ids)
        self._on_qualified = on_qualified
        self._on_occurrence_change = on_occurrence_change
        self._on_current_playback = on_current_playback
        self._published_playback: tuple[CurrentPlayback, ...] = ()
        self._display = TrackDisplay()
        self._current_track: CurrentPlayback | None = None
        self._streams = StreamSet()
        self._selected: frozenset[str] = frozenset()
        self._registered: frozenset[str] = frozenset()
        self._tracked: frozenset[str] = frozenset()
        self._entity_present = False
        self._applied: Topology | None = None
        self._settling: Topology | None = None
        self._listeners: set[Callable[[], None]] = set()
        self._unsubscribe_states: CALLBACK_TYPE | None = None
        self._unsubscribe_registry: CALLBACK_TYPE | None = None
        self._cancel_timer: CALLBACK_TYPE | None = None
        self._cancel_settle: CALLBACK_TYPE | None = None
        self._tick_job = HassJob(
            self._async_handle_tick,
            "personal_sonos_scrobbler observation tick",
            cancel_on_shutdown=True,
        )
        self._settle_job = HassJob(
            self._async_handle_settle,
            "personal_sonos_scrobbler topology settle",
            cancel_on_shutdown=True,
        )

    @property
    def snapshot(self) -> ObservationSnapshot:
        """Return the observation aggregated over the selected speakers."""
        return self._streams.snapshot

    @property
    def active_groups(self) -> int:
        """Return how many speaker groups are being observed right now."""
        return len(self._streams.keys)

    @property
    def entity_present(self) -> bool:
        """Return whether all selected entities exist in the entity registry."""
        return self._entity_present

    @property
    def current_track(self) -> CurrentPlayback | None:
        """Return the settled track to show locally, if any; it is never restored."""
        return self._current_track

    @property
    def active_occurrences(self) -> tuple[ActiveOccurrence, ...]:
        """Return the occurrences the streams are following."""
        return self._streams.active_occurrences

    def seed_resume_hints(
        self, hints: Iterable[ResumeHint], guarded: Iterable[str]
    ) -> None:
        """Restore selected speakers' marker state; call before async_start."""
        self._streams.seed_resume_hints(
            (hint for hint in hints if hint.lineage in self._registry_ids),
            (lineage for lineage in guarded if lineage in self._registry_ids),
        )

    @callback
    def async_start(self) -> None:
        """Subscribe to every Sonos player and read the current states."""
        self._unsubscribe_registry = self._hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED,
            self._async_handle_registry_update,
            event_filter=is_media_player_update,
        )
        self._async_read_registry()
        self._async_evaluate(settled=True)

    @callback
    def async_stop(self) -> None:
        """Cancel every subscription and timer."""
        self._async_cancel_timer()
        self._async_cancel_settle()
        self._async_unsubscribe_states()
        if self._unsubscribe_registry is not None:
            self._unsubscribe_registry()
            self._unsubscribe_registry = None
        self._async_publish_playback(())
        self._listeners.clear()

    @callback
    def async_set_selection(self, registry_ids: Iterable[str]) -> None:
        """Follow a new selection at once; streams of excluded speakers end here.

        Keeping the runtime keeps the streams of the speakers that stay, and the
        markers of a speaker that leaves are cleared like those of a removed one.
        """
        self._registry_ids = tuple(registry_ids)
        self._async_read_registry()
        self._async_evaluate(settled=True)

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Call listener after every observation update."""
        self._listeners.add(listener)

        @callback
        def remove_listener() -> None:
            self._listeners.discard(listener)

        return remove_listener

    @callback
    def _async_read_registry(self) -> None:
        """Resolve the selection by registry id and subscribe to all players."""
        registry = er.async_get(self._hass)
        resolved = {
            registry_id: er.async_resolve_entity_id(registry, registry_id)
            for registry_id in self._registry_ids
        }
        present = frozenset(filter(None, resolved.values()))
        self._entity_present = len(present) == len(resolved)
        # A vanished selection keeps its registry id as a stream name that no
        # state can match, so a reused entity id cannot bring it back.
        selected = frozenset(
            entity_id or registry_id for registry_id, entity_id in resolved.items()
        )
        if not self._entity_present and selected != self._selected:
            _LOGGER.warning(
                "Selected Sonos entity is gone; reconfigure the integration"
            )
        self._selected = selected
        self._streams.set_lineages(
            {
                entity_id: registry_id
                for registry_id, entity_id in resolved.items()
                if entity_id is not None
            }
        )
        self._registered = frozenset(
            entry.entity_id for entry in enabled_sonos_players(registry)
        )
        others = sorted(self._registered - present)
        self._tracked = present | frozenset(
            others[: MAX_TRACKED_PLAYERS - len(present)]
        )
        self._async_unsubscribe_states()
        self._unsubscribe_states = async_track_state_change_event(
            self._hass, sorted(self._tracked), self._async_handle_state_change
        )

    @callback
    def _async_handle_state_change(self, event: Event[EventStateChangedData]) -> None:
        self._async_evaluate(event.data["entity_id"])

    @callback
    def _async_handle_registry_update(
        self, event: Event[er.EventEntityRegistryUpdatedData]
    ) -> None:
        data = event.data
        if data["action"] == "update":
            if not _TRACKED_REGISTRY_CHANGES & data["changes"].keys():
                return
            if data.get("old_entity_id") in self._selected:
                _LOGGER.info(
                    "Selected Sonos entity was renamed; reloading the integration"
                )
                self._hass.config_entries.async_schedule_reload(self._entry_id)
                return
        selected = self._selected
        self._async_read_registry()
        self._async_evaluate(settled=self._selected != selected)

    @callback
    def _async_handle_tick(self, _now: datetime) -> None:
        self._cancel_timer = None
        self._async_publish(self._streams.tick(time.monotonic()))

    @callback
    def _async_handle_settle(self, _now: datetime) -> None:
        self._cancel_settle = None
        self._async_evaluate(settled=True)

    @callback
    def _async_evaluate(
        self, changed: str | None = None, *, settled: bool = False
    ) -> None:
        """Bring the streams up to date; a changed group first waits to settle."""
        topology = resolve_topology(self._views(), self._registered, self._selected)
        target = topology if settled else self._streams.hold_changed_groups(topology)
        if target == topology:
            self._async_cancel_settle()
        elif topology != self._settling:
            self._async_cancel_settle()
            self._settling = topology
            self._cancel_settle = async_call_later(
                self._hass, TOPOLOGY_SETTLE_SECONDS, self._settle_job
            )
        names = None if changed is None or target != self._applied else {changed}
        self._applied = target
        samples = self._samples(names)
        self._async_publish(self._streams.apply(target, samples, time.monotonic()))

    def _views(self) -> dict[str, MemberView]:
        views: dict[str, MemberView] = {}
        for entity_id in self._tracked:
            state = self._hass.states.get(entity_id)
            views[entity_id] = member_view(
                entity_id,
                state is not None
                and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN),
                state.attributes.get("group_members") if state is not None else None,
            )
        return views

    def _samples(self, names: Iterable[str] | None) -> Mapping[str, PlaybackSample]:
        """Read the given entities, or everything a stream could need."""
        if names is None:
            names = self._tracked | self._selected | self._streams.keys
        now_utc = dt_util.utcnow()
        samples: dict[str, PlaybackSample] = {}
        for name in names:
            state = self._hass.states.get(name) if name in self._tracked else None
            samples[name] = normalize_state(
                state.state if state else None,
                state.attributes if state else {},
                now_utc,
            )
        return samples

    @callback
    def _async_publish(self, plays: tuple[QualifiedPlay, ...]) -> None:
        if self._on_qualified is not None:
            for play in plays:
                self._on_qualified(play)
        delta = self._streams.take_marker_changes()
        if self._on_occurrence_change is not None and delta:
            self._on_occurrence_change(delta)
        self._async_publish_playback(self._streams.current_playback)
        self._async_cancel_timer()
        delay = self._streams.seconds_until_next_tick()
        if delay is not None:
            self._cancel_timer = async_call_later(self._hass, delay, self._tick_job)
        snapshot = self._streams.snapshot
        _LOGGER.debug("Observation is %s (%s)", snapshot.stage, snapshot.reason)
        for listener in tuple(self._listeners):
            listener()

    @callback
    def _async_publish_playback(self, playback: tuple[CurrentPlayback, ...]) -> None:
        if playback != self._published_playback:
            self._published_playback = playback
            self._current_track = self._display.update(playback)
            if self._on_current_playback is not None:
                self._on_current_playback(playback)

    @callback
    def _async_unsubscribe_states(self) -> None:
        if self._unsubscribe_states is not None:
            self._unsubscribe_states()
            self._unsubscribe_states = None

    @callback
    def _async_cancel_timer(self) -> None:
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None

    @callback
    def _async_cancel_settle(self) -> None:
        self._settling = None
        if self._cancel_settle is not None:
            self._cancel_settle()
            self._cancel_settle = None
