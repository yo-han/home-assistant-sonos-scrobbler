"""One credited stream per canonical group, on real states, registry and timers."""

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from inspect import Parameter, signature
from typing import Any
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.personal_sonos_scrobbler.normalize import (
    PlaybackSample,
    normalize_state,
)
from custom_components.personal_sonos_scrobbler.observation import (
    ObservationEngine,
    ObservationSnapshot,
    Reason,
    Stage,
)
from custom_components.personal_sonos_scrobbler.playback import (
    MarkerDelta,
    QualifiedPlay,
    ResumeHint,
)
from custom_components.personal_sonos_scrobbler.runtime import ObservationRuntime
from custom_components.personal_sonos_scrobbler.streams import (
    TOPOLOGY_SETTLE_SECONDS,
    StreamSet,
)
from custom_components.personal_sonos_scrobbler.topology import (
    MAX_TRACKED_PLAYERS,
    CanonicalGroup,
    Topology,
)
from tests.helpers import Advance, active_timers

ENTRY_ID = "pilot-entry"
HEARTBEAT = 15
QUALIFYING_HEARTBEATS = 7  # a 200 s track needs 100 s of credit


def media(title: str = "Title", position: float = 0.0) -> dict[str, Any]:
    """Return what a playing Sonos group publishes for a synthetic Spotify track."""
    return {
        "source": "Spotify Connect",
        "media_artist": "Artist",
        "media_title": title,
        "media_duration": 200,
        "media_position": position,
        "media_position_updated_at": dt_util.utcnow(),
    }


def publish(
    hass: HomeAssistant,
    player: er.RegistryEntry,
    members: Sequence[er.RegistryEntry],
    attributes: dict[str, Any],
    state: str = "playing",
) -> None:
    """Publish a state the way one Sonos entity writes it."""
    hass.states.async_set(
        player.entity_id,
        state,
        {**attributes, "group_members": [member.entity_id for member in members]},
    )


class Harness:
    """A runtime with its emitted events in reach.

    store, priors and guards play the part of the durable marker table, one row
    per lineage and slot: they survive a restart when handed to the next harness,
    and they only change through the callback.
    """

    def __init__(
        self,
        store: dict[str, ResumeHint],
        priors: dict[str, ResumeHint],
        guards: set[str],
    ) -> None:
        self.plays: list[QualifiedPlay] = []
        self.changes: list[MarkerDelta] = []
        self.store = store
        self.priors = priors
        self.guards = guards
        self.runtime: ObservationRuntime

    def on_change(self, delta: MarkerDelta) -> None:
        self.changes.append(delta)
        for marker in delta.markers_set:
            (self.priors if marker.prior else self.store)[marker.lineage] = marker
        for lineage in delta.lineages_cleared:
            self.store.pop(lineage, None)
            self.priors.pop(lineage, None)
            self.guards.discard(lineage)
        for lineage in delta.priors_cleared:
            del self.priors[lineage]
        self.guards.update(delta.guards_set)
        self.guards.difference_update(delta.guards_cleared)

    @property
    def snapshot(self) -> ObservationSnapshot:
        return self.runtime.snapshot

    @property
    def titles(self) -> list[str]:
        return [play.metadata.title for play in self.plays]


@pytest.fixture
def register(entity_registry: er.EntityRegistry) -> Callable[[str], er.RegistryEntry]:
    def _register(name: str) -> er.RegistryEntry:
        return entity_registry.async_get_or_create(
            "media_player",
            "sonos",
            f"RINCON_{name}",
            suggested_object_id=name,
        )

    return _register


@pytest.fixture
def kitchen(register: Callable[[str], er.RegistryEntry]) -> er.RegistryEntry:
    return register("kitchen")


@pytest.fixture
def hall(register: Callable[[str], er.RegistryEntry]) -> er.RegistryEntry:
    return register("hall")


@pytest.fixture
def den(register: Callable[[str], er.RegistryEntry]) -> er.RegistryEntry:
    return register("den")


@pytest.fixture
async def harness(hass: HomeAssistant) -> AsyncIterator[Callable[..., Harness]]:
    """Build started runtimes; every one of them is stopped after the test."""
    built: list[Harness] = []

    def _build(
        *selected: er.RegistryEntry,
        store: dict[str, ResumeHint] | None = None,
        priors: dict[str, ResumeHint] | None = None,
        guards: set[str] | None = None,
    ) -> Harness:
        instance = Harness(
            {} if store is None else store,
            {} if priors is None else priors,
            set() if guards is None else guards,
        )
        instance.runtime = ObservationRuntime(
            hass,
            ENTRY_ID,
            [entry.id for entry in selected],
            instance.plays.append,
            instance.on_change,
        )
        instance.runtime.seed_resume_hints(
            (*instance.store.values(), *instance.priors.values()), instance.guards
        )
        instance.runtime.async_start()
        built.append(instance)
        return instance

    yield _build
    for instance in built:
        instance.runtime.async_stop()


async def heartbeats(advance: Advance, count: int = QUALIFYING_HEARTBEATS) -> None:
    for _ in range(count):
        await advance(HEARTBEAT)


async def test_two_standalone_speakers_earn_independently(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
) -> None:
    publish(hass, kitchen, [kitchen], media("One"))
    publish(hass, hall, [hall], media("Two"))
    pilot = harness(kitchen, hall)

    await heartbeats(advance)

    assert sorted(pilot.titles) == ["One", "Two"]
    assert pilot.plays[0].occurrence_id != pilot.plays[1].occurrence_id
    assert pilot.snapshot.candidate_count == 2


async def test_different_tracks_in_two_groups_both_qualify(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    den: er.RegistryEntry,
    advance: Advance,
) -> None:
    publish(hass, kitchen, [kitchen, hall], media("One"))
    publish(hass, hall, [kitchen, hall], media("One"))
    publish(hass, den, [den], media("Two"))
    pilot = harness(kitchen, hall, den)

    await heartbeats(advance)

    assert sorted(pilot.titles) == ["One", "Two"]


async def test_leader_with_two_followers_credits_once(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    den: er.RegistryEntry,
    advance: Advance,
) -> None:
    group = [kitchen, hall, den]
    for player in group:
        publish(hass, player, group, media())
    pilot = harness(*group)

    await heartbeats(advance, 6)
    assert pilot.snapshot.observed_seconds == 90
    assert pilot.plays == []
    await heartbeats(advance, 1)

    assert pilot.titles == ["Title"]
    assert pilot.snapshot.candidate_count == 1


async def test_selected_follower_with_unselected_leader_counts_once(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
) -> None:
    group = [kitchen, hall]
    for player in group:
        publish(hass, player, group, media())
    pilot = harness(hall)

    await heartbeats(advance)

    assert pilot.titles == ["Title"]
    assert pilot.snapshot.stage is Stage.QUALIFIED


async def test_only_the_leader_state_feeds_the_engine(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
) -> None:
    group = [kitchen, hall]
    for player in group:
        publish(hass, player, group, media())
    pilot = harness(kitchen, hall)

    publish(hass, hall, group, media("Follower Only"))
    await hass.async_block_till_done()
    await heartbeats(advance)

    assert pilot.titles == ["Title"]


async def test_follower_event_does_not_observe_the_leader(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
) -> None:
    group = [kitchen, hall]
    track = media()
    for player in group:
        publish(hass, player, group, track)
    harness(kitchen, hall)

    with patch.object(ObservationEngine, "observe") as observe:
        publish(hass, hall, group, {**track, "volume_level": 0.5})
        await hass.async_block_till_done()
        observe.assert_not_called()

        publish(hass, kitchen, group, {**track, "volume_level": 0.5})
        await hass.async_block_till_done()
        observe.assert_called_once()


async def test_unselected_speaker_events_do_not_starve_the_tick(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
) -> None:
    publish(hass, kitchen, [kitchen], media())
    publish(hass, hall, [hall], {}, state="idle")
    pilot = harness(kitchen)

    for volume in range(12):
        await advance(10)
        publish(hass, hall, [hall], {"volume_level": volume / 100}, state="idle")
        await hass.async_block_till_done()

    assert pilot.snapshot.observed_seconds == 120
    assert pilot.snapshot.clock_gap_count == 0
    assert pilot.snapshot.ambiguous_restart_count == 0
    assert pilot.titles == ["Title"]


async def test_events_of_one_stream_do_not_starve_another(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
) -> None:
    other = media("Two")
    publish(hass, kitchen, [kitchen], media("One"))
    publish(hass, hall, [hall], other)
    pilot = harness(kitchen, hall)

    for volume in range(12):
        await advance(10)
        publish(hass, hall, [hall], {**other, "volume_level": volume / 100})
        await hass.async_block_till_done()

    assert sorted(pilot.titles) == ["One", "Two"]
    assert pilot.snapshot.clock_gap_count == 0


async def test_partial_regroup_order_stays_uncertain(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
) -> None:
    track = media()
    publish(hass, kitchen, [kitchen], track)
    publish(hass, hall, [hall], {}, state="idle")
    pilot = harness(kitchen, hall)
    await advance(HEARTBEAT)

    publish(hass, kitchen, [kitchen, hall], track)
    await hass.async_block_till_done()
    await heartbeats(advance)

    assert pilot.snapshot.reason is Reason.TOPOLOGY_UNCERTAIN
    assert pilot.snapshot.stage is Stage.SUSPENDED
    assert pilot.snapshot.observed_seconds == HEARTBEAT
    assert pilot.plays == []


async def test_split_order_from_clear_coordinator_stays_uncertain(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    den: er.RegistryEntry,
    advance: Advance,
) -> None:
    track = media()
    group = [kitchen, hall, den]
    for player in group:
        publish(hass, player, group, track)
    pilot = harness(*group)
    await advance(HEARTBEAT)

    publish(hass, hall, [hall], track)
    await hass.async_block_till_done()
    await advance(HEARTBEAT)
    assert pilot.snapshot.reason is Reason.TOPOLOGY_UNCERTAIN

    publish(hass, kitchen, [kitchen, den], track)
    await hass.async_block_till_done()
    await advance(HEARTBEAT)
    assert pilot.snapshot.reason is Reason.TOPOLOGY_UNCERTAIN
    assert pilot.snapshot.observed_seconds == HEARTBEAT

    publish(hass, den, [kitchen, den], track)
    await hass.async_block_till_done()
    assert pilot.snapshot.reason is Reason.TOPOLOGY_UNCERTAIN
    await advance(TOPOLOGY_SETTLE_SECONDS)

    assert pilot.snapshot.reason is Reason.COUNTING


@pytest.mark.parametrize("gone", ["unavailable", "missing"])
async def test_missing_or_unavailable_leader_fails_closed(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
    gone: str,
) -> None:
    group = [kitchen, hall]
    for player in group:
        publish(hass, player, group, media())
    pilot = harness(hall)
    await advance(HEARTBEAT)

    if gone == "unavailable":
        hass.states.async_set(kitchen.entity_id, "unavailable", {})
    else:
        hass.states.async_remove(kitchen.entity_id)
    await hass.async_block_till_done()
    await heartbeats(advance)

    assert pilot.snapshot.reason is Reason.TOPOLOGY_UNCERTAIN
    assert pilot.snapshot.observed_seconds == HEARTBEAT
    assert pilot.plays == []


async def test_unregistered_member_fails_closed(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    advance: Advance,
) -> None:
    hass.states.async_set(
        kitchen.entity_id,
        "playing",
        {**media(), "group_members": [kitchen.entity_id, "media_player.not_sonos"]},
    )
    pilot = harness(kitchen)

    await heartbeats(advance)

    assert pilot.snapshot.stage is Stage.WAITING
    assert pilot.snapshot.reason is Reason.TOPOLOGY_UNCERTAIN
    assert pilot.plays == []


async def test_join_while_playing_keeps_occurrence(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
) -> None:
    track = media()
    publish(hass, kitchen, [kitchen], track)
    publish(hass, hall, [hall], {}, state="idle")
    pilot = harness(kitchen, hall)
    await advance(HEARTBEAT)
    occurrence = pilot.runtime.active_occurrences[0].occurrence_id

    publish(hass, kitchen, [kitchen, hall], track)
    publish(hass, hall, [kitchen, hall], track)
    await hass.async_block_till_done()
    await advance(TOPOLOGY_SETTLE_SECONDS)

    assert [o.occurrence_id for o in pilot.runtime.active_occurrences] == [occurrence]
    assert pilot.snapshot.reason is Reason.COUNTING
    await heartbeats(advance, 6)
    assert [play.occurrence_id for play in pilot.plays] == [occurrence]
    assert pilot.snapshot.observed_seconds == pytest.approx(HEARTBEAT * 7)


async def test_settle_timer_is_single_and_handoff_is_judged_once(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
) -> None:
    track = media()
    publish(hass, kitchen, [kitchen], track)
    publish(hass, hall, [hall], {}, state="idle")
    pilot = harness(kitchen, hall)
    baseline = active_timers(hass)

    publish(hass, kitchen, [kitchen, hall], track)
    publish(hass, hall, [kitchen, hall], track)
    publish(hass, hall, [kitchen, hall], {**track, "volume_level": 0.5})
    await hass.async_block_till_done()

    assert pilot.snapshot.reason is Reason.TOPOLOGY_UNCERTAIN
    assert active_timers(hass) == baseline
    await advance(TOPOLOGY_SETTLE_SECONDS)
    assert pilot.snapshot.reason is Reason.COUNTING
    assert active_timers(hass) == baseline


async def test_leader_change_with_continuous_position_keeps_occurrence(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
) -> None:
    track = media()
    for player in (kitchen, hall):
        publish(hass, player, [kitchen, hall], track)
    pilot = harness(kitchen, hall)
    await advance(HEARTBEAT)
    occurrence = pilot.runtime.active_occurrences[0].occurrence_id

    publish(hass, hall, [hall, kitchen], track)
    publish(hass, kitchen, [hall, kitchen], track)
    await hass.async_block_till_done()
    await advance(TOPOLOGY_SETTLE_SECONDS)

    assert pilot.snapshot.stage is Stage.OBSERVING
    assert pilot.snapshot.ambiguous_restart_count == 0
    await heartbeats(advance, 6)
    assert [play.occurrence_id for play in pilot.plays] == [occurrence]


async def test_ambiguous_handoff_earns_nothing_new(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
) -> None:
    track = media()
    for player in (kitchen, hall):
        publish(hass, player, [kitchen, hall], track)
    pilot = harness(kitchen, hall)
    await advance(HEARTBEAT)

    jumped = media(position=120)
    publish(hass, hall, [hall, kitchen], jumped)
    publish(hass, kitchen, [hall, kitchen], jumped)
    await hass.async_block_till_done()
    await advance(TOPOLOGY_SETTLE_SECONDS)
    await heartbeats(advance, 10)

    assert pilot.snapshot.stage is Stage.RESTART_AMBIGUOUS
    assert pilot.snapshot.ambiguous_restart_count == 1
    assert pilot.plays == []


async def test_group_split_second_successor_is_suppressed_until_track_change(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
) -> None:
    track = media()
    for player in (kitchen, hall):
        publish(hass, player, [kitchen, hall], track)
    pilot = harness(kitchen, hall)
    await advance(HEARTBEAT)
    occurrence = pilot.runtime.active_occurrences[0].occurrence_id

    publish(hass, hall, [hall], track)
    publish(hass, kitchen, [kitchen], track)
    await hass.async_block_till_done()
    await advance(TOPOLOGY_SETTLE_SECONDS)
    await heartbeats(advance, 8)

    assert [play.occurrence_id for play in pilot.plays] == [occurrence]

    publish(hass, hall, [hall], media("Next"))
    await hass.async_block_till_done()
    await heartbeats(advance)

    assert pilot.titles == ["Title", "Next"]


async def test_split_engine_follows_the_old_leader_not_list_order(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    den: er.RegistryEntry,
    advance: Advance,
) -> None:
    track = media()
    group = [hall, den, kitchen]
    for player in group:
        publish(hass, player, group, track)
    pilot = harness(*group)
    await advance(HEARTBEAT)

    publish(hass, hall, [hall], track)
    publish(hass, den, [den, kitchen], track)
    publish(hass, kitchen, [den, kitchen], track)
    await hass.async_block_till_done()
    await advance(TOPOLOGY_SETTLE_SECONDS)
    await heartbeats(advance, 6)

    assert pilot.titles == ["Title"]
    assert pilot.snapshot.observed_seconds == pytest.approx(HEARTBEAT * 7)


async def test_follower_seen_as_soloist_with_leader_media_does_not_double_credit(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
) -> None:
    track = media()
    for player in (kitchen, hall):
        publish(hass, player, [kitchen, hall], track)
    pilot = harness(kitchen, hall)
    await heartbeats(advance, 5)

    publish(hass, hall, [hall], track)
    await hass.async_block_till_done()
    await heartbeats(advance, 8)
    assert pilot.plays == []

    publish(hass, kitchen, [kitchen], track)
    await hass.async_block_till_done()
    await advance(TOPOLOGY_SETTLE_SECONDS)
    await heartbeats(advance, 8)

    assert pilot.titles == ["Title"]


async def test_lost_follower_returning_alone_cannot_earn_the_group_track(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
) -> None:
    track = media()
    for player in (kitchen, hall):
        publish(hass, player, [kitchen, hall], track)
    pilot = harness(kitchen, hall)
    await advance(HEARTBEAT)

    hass.states.async_set(hall.entity_id, "unavailable", {})
    publish(hass, kitchen, [kitchen], track)
    await hass.async_block_till_done()
    await advance(TOPOLOGY_SETTLE_SECONDS)
    publish(hass, hall, [hall], track)
    await hass.async_block_till_done()
    await heartbeats(advance, 8)

    assert pilot.titles == ["Title"]


async def test_volume_and_duplicate_metadata_events_add_no_time(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
) -> None:
    group = [kitchen, hall]
    track = media()
    for player in group:
        publish(hass, player, group, track)
    pilot = harness(*group)

    for volume in range(10):
        publish(hass, hall, group, {**track, "volume_level": volume / 10})
        publish(hass, kitchen, group, {**track, "volume_level": volume / 10})
    await hass.async_block_till_done()
    assert pilot.snapshot.observed_seconds == 0
    await advance(HEARTBEAT)

    assert pilot.snapshot.observed_seconds == HEARTBEAT
    assert pilot.snapshot.seek_count == 0


async def test_rename_and_removal_resolve_by_registry_id(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    entity_registry: er.EntityRegistry,
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
) -> None:
    publish(hass, kitchen, [kitchen], media())
    publish(hass, hall, [hall], media("Two"))
    first = harness(kitchen, hall)

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        renamed = entity_registry.async_update_entity(
            kitchen.entity_id, new_entity_id="media_player.dining"
        )
        await hass.async_block_till_done()
    reload.assert_called_once_with(ENTRY_ID)
    first.runtime.async_stop()

    publish(hass, renamed, [renamed], media("Renamed"))
    second = harness(renamed, hall)
    assert second.runtime.entity_present is True
    await advance(HEARTBEAT)
    assert second.snapshot.observed_seconds == HEARTBEAT

    entity_registry.async_remove(hall.entity_id)
    await hass.async_block_till_done()
    publish(hass, hall, [hall], media("Stale"))
    await hass.async_block_till_done()
    await heartbeats(advance, 6)

    assert second.runtime.entity_present is False
    assert second.titles == ["Renamed"]


async def test_removal_of_the_only_stream_keeps_the_totals(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    entity_registry: er.EntityRegistry,
    kitchen: er.RegistryEntry,
    advance: Advance,
) -> None:
    publish(hass, kitchen, [kitchen], media())
    pilot = harness(kitchen)
    await heartbeats(advance)
    assert pilot.snapshot.candidate_count == 1

    entity_registry.async_remove(kitchen.entity_id)
    await hass.async_block_till_done()

    assert pilot.snapshot.candidate_count == 1
    assert pilot.snapshot.reason is Reason.ENTITY_MISSING


async def test_new_registration_is_tracked_and_can_join(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    register: Callable[[str], er.RegistryEntry],
    kitchen: er.RegistryEntry,
    advance: Advance,
) -> None:
    newcomer_id = "media_player.newcomer"
    track = {**media(), "group_members": [newcomer_id, kitchen.entity_id]}
    hass.states.async_set(kitchen.entity_id, "playing", track)
    pilot = harness(kitchen)
    await advance(HEARTBEAT)
    assert pilot.snapshot.reason is Reason.TOPOLOGY_UNCERTAIN

    assert register("newcomer").entity_id == newcomer_id
    await hass.async_block_till_done()
    assert pilot.snapshot.reason is Reason.TOPOLOGY_UNCERTAIN
    hass.states.async_set(newcomer_id, "playing", track)
    await hass.async_block_till_done()
    await advance(TOPOLOGY_SETTLE_SECONDS)

    assert pilot.snapshot.reason is Reason.COUNTING


async def test_disabled_registration_leaves_the_tracked_set(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    entity_registry: er.EntityRegistry,
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
) -> None:
    group = [kitchen, hall]
    for player in group:
        publish(hass, player, group, media())
    pilot = harness(kitchen)
    await advance(HEARTBEAT)
    assert pilot.snapshot.reason is Reason.COUNTING

    entity_registry.async_update_entity(
        hall.entity_id, disabled_by=er.RegistryEntryDisabler.USER
    )
    await hass.async_block_till_done()

    assert pilot.snapshot.reason is Reason.TOPOLOGY_UNCERTAIN


async def test_more_players_than_the_bound_is_uncertain(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    register: Callable[[str], er.RegistryEntry],
    kitchen: er.RegistryEntry,
) -> None:
    publish(hass, kitchen, [kitchen], media())
    for index in range(MAX_TRACKED_PLAYERS):
        register(f"filler_{index}")

    pilot = harness(kitchen)

    assert pilot.snapshot.reason is Reason.TOPOLOGY_UNCERTAIN


async def test_occurrence_change_sets_the_marker_of_the_speaker(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
) -> None:
    publish(hass, kitchen, [kitchen], media("One"))
    pilot = harness(kitchen)
    first = pilot.runtime.active_occurrences[0]
    assert [m.lineage for c in pilot.changes for m in c.markers_set] == [kitchen.id]
    assert pilot.store[kitchen.id].occurrence_id == first.occurrence_id
    assert pilot.store[kitchen.id].track_key == track_key("One")
    assert pilot.store[kitchen.id].started_at == first.started_at

    publish(hass, kitchen, [kitchen], media("Two"))
    await hass.async_block_till_done()

    second = pilot.runtime.active_occurrences[0]
    assert second.occurrence_id != first.occurrence_id
    assert pilot.store[kitchen.id].occurrence_id == second.occurrence_id
    assert len(pilot.changes) == 2


async def test_pause_is_no_occurrence_change(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
) -> None:
    track = media()
    publish(hass, kitchen, [kitchen], track)
    pilot = harness(kitchen)

    publish(hass, kitchen, [kitchen], track, state="paused")
    await hass.async_block_till_done()
    pilot.runtime.async_stop()

    assert len(pilot.changes) == 1
    assert len(pilot.runtime.active_occurrences) == 1


async def test_qualified_play_is_reported_before_its_end(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    advance: Advance,
    freezer: FrozenDateTimeFactory,
) -> None:
    publish(hass, kitchen, [kitchen], media("One"))
    pilot = harness(kitchen)
    order: list[str] = []
    pilot.runtime._on_qualified = lambda _play: order.append("qualified")
    pilot.runtime._on_occurrence_change = lambda *_changes: order.append("changed")

    await heartbeats(advance, 6)
    freezer.tick(timedelta(seconds=HEARTBEAT))
    publish(hass, kitchen, [kitchen], media("Two"))
    await hass.async_block_till_done()

    assert order == ["qualified", "changed"]


TWO_DAYS = timedelta(days=2)


def marker(player: er.RegistryEntry, title: str = "X") -> ResumeHint:
    return ResumeHint("original", track_key(title), dt_util.utcnow(), player.id)


async def earn_on(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    player: er.RegistryEntry,
    advance: Advance,
    store: dict[str, ResumeHint],
) -> ResumeHint:
    """Run one process that earns X on the player and stop it, paused on X."""
    publish(hass, player, [player], media("X"))
    pilot = harness(player, store=store)
    await heartbeats(advance)
    assert pilot.titles == ["X"]
    pilot.runtime.async_stop()
    return store[player.id]


async def test_track_on_another_speaker_cannot_touch_an_offline_marker(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
    freezer: FrozenDateTimeFactory,
) -> None:
    store: dict[str, ResumeHint] = {}
    original = await earn_on(hass, harness, kitchen, advance, store)
    freezer.tick(TWO_DAYS)

    hass.states.async_set(kitchen.entity_id, "unavailable", {})
    publish(hass, hall, [hall], media("X"))
    pilot = harness(kitchen, hall, store=store)
    await heartbeats(advance)
    assert pilot.titles == ["X"]
    assert pilot.plays[0].occurrence_id != original.occurrence_id

    publish(hass, hall, [hall], media("Y"))
    await hass.async_block_till_done()
    assert store[kitchen.id] == original
    await advance(HEARTBEAT)

    publish(hass, kitchen, [kitchen], media("X", position=100))
    await hass.async_block_till_done()
    await heartbeats(advance, 16)

    assert pilot.titles == ["X", "Y"]
    assert store[kitchen.id] == original
    resumed = [
        o
        for o in pilot.runtime.active_occurrences
        if o.occurrence_id == original.occurrence_id
    ]
    assert [o.started_at for o in resumed] == [original.started_at]


async def test_marker_survives_an_offline_speaker_across_two_restarts(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
    freezer: FrozenDateTimeFactory,
) -> None:
    store: dict[str, ResumeHint] = {}
    original = await earn_on(hass, harness, kitchen, advance, store)
    hass.states.async_set(kitchen.entity_id, "unavailable", {})
    publish(hass, hall, [hall], media("Y"))

    for _ in range(2):
        freezer.tick(TWO_DAYS)
        pilot = harness(kitchen, hall, store=store)
        await advance(HEARTBEAT)
        pilot.runtime.async_stop()

    assert store[kitchen.id] == original


async def test_own_different_track_replaces_the_marker(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    advance: Advance,
    freezer: FrozenDateTimeFactory,
) -> None:
    store: dict[str, ResumeHint] = {}
    original = await earn_on(hass, harness, kitchen, advance, store)
    freezer.tick(TWO_DAYS)

    publish(hass, kitchen, [kitchen], media("Y"))
    pilot = harness(kitchen, store=store)

    assert store[kitchen.id].occurrence_id != original.occurrence_id
    assert store[kitchen.id].track_key == track_key("Y")
    assert pilot.changes[0].lineages_cleared == ()


async def test_doubtful_resume_keeps_the_marker(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    advance: Advance,
    freezer: FrozenDateTimeFactory,
) -> None:
    store: dict[str, ResumeHint] = {}
    original = await earn_on(hass, harness, kitchen, advance, store)
    freezer.tick(TWO_DAYS)

    publish(hass, kitchen, [kitchen], media("X", position=100))
    pilot = harness(kitchen, store=store)
    await heartbeats(advance, 10)

    assert pilot.plays == []
    assert store[kitchen.id] == original
    assert pilot.changes == []


async def test_resume_within_the_window_reuses_the_identity_after_restart(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    advance: Advance,
    freezer: FrozenDateTimeFactory,
) -> None:
    store: dict[str, ResumeHint] = {}
    original = await earn_on(hass, harness, kitchen, advance, store)
    freezer.tick(timedelta(minutes=30))

    publish(hass, kitchen, [kitchen], media("X", position=120))
    pilot = harness(kitchen, store=store)
    assert pilot.snapshot.observed_seconds == 0
    await heartbeats(advance)

    assert [play.occurrence_id for play in pilot.plays] == [original.occurrence_id]
    assert pilot.plays[0].started_at == original.started_at


@pytest.mark.parametrize(
    ("state", "extra"),
    [
        ("idle", {}),
        ("playing", {"source": "TV"}),
    ],
    ids=["idle", "excluded_source"],
)
async def test_sample_without_a_certain_track_keeps_the_marker(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    state: str,
    extra: dict[str, Any],
) -> None:
    original = marker(kitchen)
    publish(hass, kitchen, [kitchen], {**media("Other"), **extra}, state=state)

    pilot = harness(kitchen, store={kitchen.id: original})

    assert pilot.store == {kitchen.id: original}
    assert pilot.changes == []


async def test_uncertain_sample_does_not_replace_the_marker(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
) -> None:
    original = marker(kitchen)
    hass.states.async_set(
        kitchen.entity_id,
        "playing",
        {**media("Other"), "group_members": [kitchen.entity_id, "media_player.nope"]},
    )

    pilot = harness(kitchen, store={kitchen.id: original})
    assert pilot.snapshot.reason is Reason.TOPOLOGY_UNCERTAIN
    assert pilot.store == {kitchen.id: original}

    publish(hass, kitchen, [kitchen], media("Other"))
    await hass.async_block_till_done()
    assert pilot.store[kitchen.id].track_key == track_key("Other")


async def test_rename_keeps_the_lineage(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    entity_registry: er.EntityRegistry,
    kitchen: er.RegistryEntry,
    advance: Advance,
) -> None:
    store: dict[str, ResumeHint] = {}
    original = await earn_on(hass, harness, kitchen, advance, store)
    renamed = entity_registry.async_update_entity(
        kitchen.entity_id, new_entity_id="media_player.dining"
    )
    await hass.async_block_till_done()

    publish(hass, renamed, [renamed], media("X", position=110))
    pilot = harness(renamed, store=store)

    assert renamed.id == kitchen.id
    assert pilot.runtime.active_occurrences[0].occurrence_id == original.occurrence_id


async def test_deselected_marker_is_ignored_by_seeding(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
) -> None:
    foreign = marker(hall)
    publish(hass, kitchen, [kitchen], media("X", position=30))

    pilot = harness(kitchen, store={hall.id: foreign})
    await heartbeats(advance)

    assert pilot.titles == ["X"]
    assert pilot.store[hall.id] == foreign


async def test_removed_speaker_clears_its_lineage(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    entity_registry: er.EntityRegistry,
    kitchen: er.RegistryEntry,
) -> None:
    publish(hass, kitchen, [kitchen], media("X"))
    pilot = harness(kitchen)
    assert kitchen.id in pilot.store

    entity_registry.async_remove(kitchen.entity_id)
    await hass.async_block_till_done()

    assert pilot.changes[-1] == MarkerDelta(lineages_cleared=(kitchen.id,))
    assert pilot.store == {}


async def test_group_members_share_one_marker_occurrence(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    den: er.RegistryEntry,
) -> None:
    group = [kitchen, hall, den]
    for player in group:
        publish(hass, player, group, media())

    pilot = harness(kitchen, hall)

    assert set(pilot.store) == {kitchen.id, hall.id}
    occurrence = pilot.runtime.active_occurrences[0].occurrence_id
    assert {m.occurrence_id for m in pilot.store.values()} == {occurrence}


async def test_join_moves_the_marker_of_the_new_member(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
) -> None:
    track = media()
    publish(hass, kitchen, [kitchen], track)
    publish(hass, hall, [hall], media("Own"))
    pilot = harness(kitchen, hall)
    occurrence = pilot.store[kitchen.id].occurrence_id

    publish(hass, kitchen, [kitchen, hall], track)
    publish(hass, hall, [kitchen, hall], track)
    await hass.async_block_till_done()
    assert pilot.store[hall.id].track_key == track_key("Own")
    await advance(TOPOLOGY_SETTLE_SECONDS)

    assert pilot.store[kitchen.id].occurrence_id == occurrence
    assert pilot.store[hall.id].occurrence_id == occurrence


async def test_join_into_a_group_without_a_track_keeps_the_marker(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
) -> None:
    publish(hass, kitchen, [kitchen], media("Theirs"))
    publish(hass, hall, [hall], media("Own"))
    pilot = harness(kitchen, hall)
    own = pilot.store[hall.id]
    publish(hass, kitchen, [kitchen], {}, state="idle")
    await hass.async_block_till_done()

    publish(hass, kitchen, [kitchen, hall], {}, state="idle")
    publish(hass, hall, [kitchen, hall], {}, state="idle")
    await hass.async_block_till_done()
    await advance(TOPOLOGY_SETTLE_SECONDS)

    assert pilot.store[hall.id] == own


async def test_same_track_after_an_excluded_source_cannot_earn_on_the_marker(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    advance: Advance,
) -> None:
    publish(hass, kitchen, [kitchen], media("X"))
    pilot = harness(kitchen)
    original = pilot.store[kitchen.id]
    await heartbeats(advance)
    assert pilot.titles == ["X"]

    publish(hass, kitchen, [kitchen], {**media("X"), "source": "TV"})
    await hass.async_block_till_done()
    publish(hass, kitchen, [kitchen], media("X"))
    await hass.async_block_till_done()
    await heartbeats(advance)

    assert pilot.titles == ["X"]
    assert pilot.store[kitchen.id] == original


async def test_merged_group_with_two_identities_earns_nothing_after_restart(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
    freezer: FrozenDateTimeFactory,
) -> None:
    older = ResumeHint("occ_old", track_key("X"), dt_util.utcnow(), hall.id)
    freezer.tick(timedelta(hours=1))
    newer = ResumeHint("occ_new", track_key("X"), dt_util.utcnow(), kitchen.id)
    store = {hall.id: older, kitchen.id: newer}
    freezer.tick(timedelta(seconds=100))
    assert hall.entity_id < kitchen.entity_id

    group = [kitchen, hall]
    for player in group:
        publish(hass, player, group, media("X", position=100))
    pilot = harness(kitchen, hall, store=store)
    await heartbeats(advance, 8)

    assert pilot.plays == []
    assert store == {hall.id: older, kitchen.id: newer}
    assert pilot.changes == []

    for player in group:
        publish(hass, player, group, media("Next"))
    await hass.async_block_till_done()
    assert {m.track_key for m in store.values()} == {track_key("Next")}


Z_TRACK = "Z"


async def join(
    hass: HomeAssistant,
    advance: Advance,
    joiner: er.RegistryEntry,
    leader: er.RegistryEntry,
    attributes: dict[str, Any],
) -> None:
    """Let the joiner follow the leader, in the order Sonos writes it."""
    for player in (leader, joiner):
        publish(hass, player, [leader, joiner], attributes)
    await hass.async_block_till_done()
    await advance(TOPOLOGY_SETTLE_SECONDS)


async def leave(
    hass: HomeAssistant,
    advance: Advance,
    leaver: er.RegistryEntry,
    leader: er.RegistryEntry,
    attributes: dict[str, Any],
) -> None:
    """Let the leaver return to its own queue, in the order Sonos writes it."""
    publish(hass, leaver, [leaver], attributes)
    publish(hass, leader, [leader], media(Z_TRACK))
    await hass.async_block_till_done()
    await advance(TOPOLOGY_SETTLE_SECONDS)


def plays_of(pilot: Harness, title: str = "X") -> list[QualifiedPlay]:
    return [play for play in pilot.plays if play.metadata.title == title]


def without_position(attributes: dict[str, Any]) -> dict[str, Any]:
    gone = {"media_position", "media_position_updated_at"}
    return {key: value for key, value in attributes.items() if key not in gone}


async def paused_on_x_beside_z(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    player: er.RegistryEntry,
    other: er.RegistryEntry,
    advance: Advance,
) -> tuple[Harness, ResumeHint]:
    """Earn X on the player, leave it paused there, with the other speaker on Z."""
    publish(hass, player, [player], media("X"))
    publish(hass, other, [other], media(Z_TRACK))
    pilot = harness(player, other)
    await heartbeats(advance)
    assert [play.metadata.title for play in plays_of(pilot)] == ["X"]
    publish(hass, player, [player], media("X", position=105), state="paused")
    await hass.async_block_till_done()
    return pilot, pilot.store[player.id]


async def test_brief_join_cannot_earn_the_paused_track_again(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
    freezer: FrozenDateTimeFactory,
) -> None:
    pilot, original = await paused_on_x_beside_z(hass, harness, kitchen, hall, advance)
    freezer.tick(TWO_DAYS)

    await join(hass, advance, kitchen, hall, media(Z_TRACK))
    assert pilot.store[kitchen.id].track_key == track_key(Z_TRACK)
    assert pilot.priors[kitchen.id] == replace(original, prior=True)

    await leave(hass, advance, kitchen, hall, media("X", position=100))
    await heartbeats(advance, 16)

    assert len(plays_of(pilot)) == 1
    resumed = [
        occurrence
        for occurrence in pilot.runtime.active_occurrences
        if occurrence.track_key == track_key("X")
    ]
    assert [(o.occurrence_id, o.started_at) for o in resumed] == [
        (original.occurrence_id, original.started_at)
    ]


async def test_brief_join_resumes_the_same_identity_inside_the_window(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
) -> None:
    pilot, original = await paused_on_x_beside_z(hass, harness, kitchen, hall, advance)

    await join(hass, advance, kitchen, hall, media(Z_TRACK))
    await leave(hass, advance, kitchen, hall, media("X", position=105))
    await heartbeats(advance, 16)

    assert {play.occurrence_id for play in plays_of(pilot)} == {original.occurrence_id}
    assert {play.started_at for play in plays_of(pilot)} == {original.started_at}


async def test_restart_inside_a_brief_join_cannot_earn_the_paused_track_again(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
    freezer: FrozenDateTimeFactory,
) -> None:
    first, original = await paused_on_x_beside_z(hass, harness, kitchen, hall, advance)
    await join(hass, advance, kitchen, hall, media(Z_TRACK))
    first.runtime.async_stop()
    freezer.tick(TWO_DAYS)

    pilot = harness(kitchen, hall, store=first.store, priors=first.priors)
    await leave(hass, advance, kitchen, hall, media("X", position=100))
    await heartbeats(advance, 16)

    assert plays_of(pilot) == []
    assert pilot.store[kitchen.id] == original
    assert pilot.priors[kitchen.id] == replace(original, prior=True)


async def test_join_during_downtime_cannot_earn_the_paused_track_again(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
    freezer: FrozenDateTimeFactory,
) -> None:
    store: dict[str, ResumeHint] = {}
    original = await earn_on(hass, harness, kitchen, advance, store)
    freezer.tick(TWO_DAYS)

    for player in (hall, kitchen):
        publish(hass, player, [hall, kitchen], media(Z_TRACK))
    pilot = harness(kitchen, hall, store=store)
    assert pilot.priors[kitchen.id] == replace(original, prior=True)

    await leave(hass, advance, kitchen, hall, media("X", position=100))
    await heartbeats(advance, 16)

    assert plays_of(pilot) == []


async def test_genuine_repeat_after_a_join_earns_from_a_proven_start(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
    freezer: FrozenDateTimeFactory,
) -> None:
    pilot, original = await paused_on_x_beside_z(hass, harness, kitchen, hall, advance)
    freezer.tick(TWO_DAYS)
    await join(hass, advance, kitchen, hall, media(Z_TRACK))

    await leave(hass, advance, kitchen, hall, media("X", position=2))
    await heartbeats(advance, 8)

    repeat = plays_of(pilot)[-1]
    assert len(plays_of(pilot)) == 2
    assert repeat.occurrence_id != original.occurrence_id
    assert repeat.started_at > original.started_at
    assert pilot.priors == {}
    assert any(kitchen.id in change.priors_cleared for change in pilot.changes)


async def test_prior_survives_a_track_change_in_the_group(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
    freezer: FrozenDateTimeFactory,
) -> None:
    pilot, original = await paused_on_x_beside_z(hass, harness, kitchen, hall, advance)
    freezer.tick(TWO_DAYS)
    await join(hass, advance, kitchen, hall, media(Z_TRACK))

    for player in (hall, kitchen):
        publish(hass, player, [hall, kitchen], media("W"))
    await hass.async_block_till_done()

    assert pilot.store[kitchen.id].track_key == track_key("W")
    assert pilot.priors[kitchen.id] == replace(original, prior=True)

    await leave(hass, advance, kitchen, hall, media("X", position=100))
    await heartbeats(advance, 16)

    assert len(plays_of(pilot)) == 1


async def test_unknown_position_after_a_join_is_suppressed(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
    freezer: FrozenDateTimeFactory,
) -> None:
    pilot, original = await paused_on_x_beside_z(hass, harness, kitchen, hall, advance)
    freezer.tick(TWO_DAYS)
    await join(hass, advance, kitchen, hall, media(Z_TRACK))

    await leave(hass, advance, kitchen, hall, without_position(media("X")))
    await heartbeats(advance, 16)

    assert len(plays_of(pilot)) == 1
    assert pilot.priors[kitchen.id] == replace(original, prior=True)


async def test_join_of_an_unselected_leader_keeps_the_marker_as_prior(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
    freezer: FrozenDateTimeFactory,
) -> None:
    publish(hass, hall, [hall], media(Z_TRACK))
    store: dict[str, ResumeHint] = {}
    original = await earn_on(hass, harness, kitchen, advance, store)
    pilot = harness(kitchen, store=store)
    freezer.tick(TWO_DAYS)

    await join(hass, advance, kitchen, hall, media(Z_TRACK))
    await leave(hass, advance, kitchen, hall, media("X", position=100))
    await heartbeats(advance, 16)

    assert plays_of(pilot) == []
    assert pilot.store[kitchen.id] == original


async def test_deselected_prior_is_ignored_by_seeding(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
) -> None:
    foreign = replace(marker(hall), prior=True)
    publish(hass, kitchen, [kitchen], media("X", position=30))

    pilot = harness(kitchen, priors={hall.id: foreign})
    await heartbeats(advance)

    assert pilot.titles == ["X"]
    assert pilot.priors[hall.id] == foreign


async def test_two_detours_cannot_earn_the_held_track_across_a_restart(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    den: er.RegistryEntry,
    advance: Advance,
    freezer: FrozenDateTimeFactory,
) -> None:
    store: dict[str, ResumeHint] = {}
    await earn_on(hass, harness, kitchen, advance, store)
    publish(hass, kitchen, [kitchen], media("X", position=105), state="paused")
    publish(hass, hall, [hall], media(Z_TRACK, position=50))
    publish(hass, den, [den], media("W", position=50))
    pilot = harness(kitchen, store=store)
    freezer.tick(TWO_DAYS)

    await join(hass, advance, kitchen, hall, media(Z_TRACK, position=60))
    publish(hass, hall, [hall], media(Z_TRACK, position=60))
    await join(hass, advance, kitchen, den, media("W", position=60))
    assert pilot.guards == {kitchen.id}
    publish(hass, den, [den], media("W", position=70))
    publish(hass, kitchen, [kitchen], media("X", position=100))
    await hass.async_block_till_done()
    await advance(TOPOLOGY_SETTLE_SECONDS)
    pilot.runtime.async_stop()

    second = harness(
        kitchen, store=pilot.store, priors=pilot.priors, guards=pilot.guards
    )
    await heartbeats(advance, 16)

    assert plays_of(pilot) == plays_of(second) == []
    assert second.guards == {kitchen.id}


async def test_deselected_guard_is_ignored_by_seeding(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
) -> None:
    publish(hass, kitchen, [kitchen], media("X", position=30))

    pilot = harness(kitchen, guards={hall.id})
    await heartbeats(advance)

    assert pilot.titles == ["X"]


async def test_removed_speaker_clears_both_slots(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    entity_registry: er.EntityRegistry,
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
) -> None:
    pilot, _original = await paused_on_x_beside_z(hass, harness, kitchen, hall, advance)
    await join(hass, advance, kitchen, hall, media(Z_TRACK))
    assert kitchen.id in pilot.priors

    entity_registry.async_remove(kitchen.entity_id)
    await hass.async_block_till_done()

    assert kitchen.id not in pilot.store
    assert pilot.priors == {}


async def test_leader_change_keeps_every_marker(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
) -> None:
    track = media()
    for player in (kitchen, hall):
        publish(hass, player, [kitchen, hall], track)
    pilot = harness(kitchen, hall)
    before = dict(pilot.store)

    publish(hass, hall, [hall, kitchen], track)
    publish(hass, kitchen, [hall, kitchen], track)
    await hass.async_block_till_done()
    await advance(TOPOLOGY_SETTLE_SECONDS)

    assert pilot.store == before


async def test_split_keeps_the_marker_per_member(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
) -> None:
    track = media()
    for player in (kitchen, hall):
        publish(hass, player, [kitchen, hall], track)
    pilot = harness(kitchen, hall)
    occurrence = pilot.store[kitchen.id].occurrence_id

    publish(hass, hall, [hall], track)
    publish(hass, kitchen, [kitchen], track)
    await hass.async_block_till_done()
    await advance(TOPOLOGY_SETTLE_SECONDS)
    assert {m.occurrence_id for m in pilot.store.values()} == {occurrence}

    publish(hass, kitchen, [kitchen], media("Next"))
    await hass.async_block_till_done()

    assert pilot.store[hall.id].occurrence_id == occurrence
    assert pilot.store[kitchen.id].track_key == track_key("Next")


async def test_split_successor_restarted_on_the_track_cannot_earn_it(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
    freezer: FrozenDateTimeFactory,
) -> None:
    track = media()
    for player in (kitchen, hall):
        publish(hass, player, [kitchen, hall], track)
    pilot = harness(kitchen, hall)
    await heartbeats(advance)
    occurrence = pilot.plays[0].occurrence_id
    publish(hass, hall, [hall], track)
    publish(hass, kitchen, [kitchen], track)
    await hass.async_block_till_done()
    await advance(TOPOLOGY_SETTLE_SECONDS)
    pilot.runtime.async_stop()

    second = harness(hall, store={hall.id: pilot.store[hall.id]})
    await heartbeats(advance)

    assert {play.occurrence_id for play in second.plays} <= {occurrence}


async def test_follower_volume_events_keep_one_credit_for_the_group(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    advance: Advance,
) -> None:
    group = [kitchen, hall]
    track = media()
    for player in group:
        publish(hass, player, group, track)
    pilot = harness(*group)

    for number in range(24):
        await advance(5)
        publish(hass, hall, group, {**track, "volume_level": number / 24})
        await hass.async_block_till_done()

    assert pilot.titles == ["Title"]
    assert pilot.snapshot.observed_seconds == 120
    assert pilot.snapshot.candidate_count == 1
    assert pilot.snapshot.clock_gap_count == 0


async def test_follower_volume_events_keep_credit_in_two_independent_groups(
    hass: HomeAssistant,
    harness: Callable[..., Harness],
    kitchen: er.RegistryEntry,
    hall: er.RegistryEntry,
    den: er.RegistryEntry,
    register: Callable[[str], er.RegistryEntry],
    advance: Advance,
) -> None:
    attic = register("attic")
    first, second = [kitchen, hall], [den, attic]
    one, two = media("One"), media("Two")
    for player in first:
        publish(hass, player, first, one)
    for player in second:
        publish(hass, player, second, two)
    pilot = harness(*first, *second)

    for number in range(24):
        await advance(5)
        publish(hass, hall, first, {**one, "volume_level": number / 24})
        publish(hass, attic, second, {**two, "volume_level": number / 24})
        await hass.async_block_till_done()

    assert sorted(pilot.titles) == ["One", "Two"]
    assert pilot.snapshot.candidate_count == 2
    assert pilot.snapshot.clock_gap_count == 0


# StreamSet without Home Assistant: aggregation and resume decisions.

T0 = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
KEYS = ("media_player.a", "media_player.b")
LINEAGES = {KEYS[0]: "registry-a", KEYS[1]: "registry-b"}


def solo(*keys: str) -> Topology:
    return Topology(
        tuple(CanonicalGroup(key, frozenset({key})) for key in sorted(keys)),
        frozenset(),
    )


def sample(
    title: str = "Title",
    position: float | None = None,
    at: datetime = T0,
    state: str = "playing",
) -> PlaybackSample:
    attributes: dict[str, object] = {
        "source": "Spotify Connect",
        "media_artist": "Artist",
        "media_title": title,
        "media_duration": 200,
    }
    if position is not None:
        attributes |= {"media_position": position, "media_position_updated_at": at}
    return normalize_state(state, attributes, at)


def tick_until_qualified(streams: StreamSet, now: float) -> list[QualifiedPlay]:
    plays: list[QualifiedPlay] = []
    for step in range(1, 40):
        plays += streams.tick(now + step * HEARTBEAT)
    return plays


def hint_for(key: bytes, lineage: str = LINEAGES[KEYS[0]]) -> ResumeHint:
    return ResumeHint("hinted", key, T0, lineage)


def track_key(title: str = "Title") -> bytes:
    key = sample(title).track_key
    assert key is not None
    return key


def seeded(*hints: ResumeHint, guarded: Sequence[str] = (), **kwargs: Any) -> StreamSet:
    streams = StreamSet(**kwargs)
    streams.set_lineages(LINEAGES)
    streams.seed_resume_hints(hints, guarded)
    return streams


@pytest.mark.parametrize(
    "offset",
    [
        timedelta(seconds=-3),
        timedelta(seconds=-1),
        timedelta(0),
        timedelta(hours=1),
        timedelta(hours=6),
    ],
    ids=["tolerance_edge", "inside_tolerance", "same_moment", "inside", "edge"],
)
def test_resume_reuses_identity_and_credits_from_zero(offset: timedelta) -> None:
    streams = seeded(hint_for(track_key()))
    position = 60.0
    at = T0 + offset + timedelta(seconds=position)

    streams.apply(solo(KEYS[0]), {KEYS[0]: sample(position=position, at=at)}, 0.0)

    assert streams.snapshot.observed_seconds == 0
    occurrence = streams.active_occurrences[0]
    assert occurrence.occurrence_id == "hinted"
    assert occurrence.started_at == T0
    assert streams.tick(HEARTBEAT) == ()
    plays = tick_until_qualified(streams, HEARTBEAT)
    assert [(play.occurrence_id, play.started_at) for play in plays] == [("hinted", T0)]


@pytest.mark.parametrize(
    ("position", "offset"),
    [
        (None, timedelta(hours=1)),
        (5.0, timedelta(hours=1)),
        (60.0, timedelta(seconds=-3, milliseconds=-1)),
        (60.0, timedelta(hours=6, seconds=1)),
        (60.0, timedelta(hours=24, seconds=1)),
        (60.0, timedelta(days=30)),
    ],
    ids=["no_position", "at_restart", "before", "past_window", "past_24h", "past_30d"],
)
def test_doubtful_resume_never_earns_until_a_transition(
    position: float | None, offset: timedelta
) -> None:
    streams = seeded(hint_for(track_key()), new_occurrence_id=lambda: "fresh")
    at = T0 + offset + timedelta(seconds=position or 0.0)

    streams.apply(solo(KEYS[0]), {KEYS[0]: sample(position=position, at=at)}, 0.0)

    assert tick_until_qualified(streams, 0.0) == []
    assert streams.active_occurrences[0].occurrence_id == "hinted"
    assert streams.active_occurrences[0].started_at == T0
    assert streams.take_marker_changes() == MarkerDelta()

    later = 40.0 * HEARTBEAT
    streams.apply(solo(KEYS[0]), {KEYS[0]: sample("Next", at=at)}, later)
    assert [play.metadata.title for play in tick_until_qualified(streams, later)] == [
        "Next"
    ]


def test_paused_song_resumed_past_24_hours_earns_no_event() -> None:
    streams = seeded(hint_for(track_key()))
    resumed = T0 + timedelta(hours=25)

    streams.apply(solo(KEYS[0]), {KEYS[0]: sample(position=90, at=resumed)}, 0.0)
    plays = tick_until_qualified(streams, 0.0)

    assert plays == []
    assert streams.snapshot.candidate_count == 1


def test_another_stream_without_a_marker_earns_the_same_track_normally() -> None:
    streams = seeded(hint_for(track_key()), new_occurrence_id=lambda: "fresh")
    at = T0 + timedelta(seconds=60)

    streams.apply(
        solo(*KEYS),
        {
            KEYS[0]: normalize_state("unavailable", {}, at),
            KEYS[1]: sample(position=60, at=at),
        },
        0.0,
    )
    plays = tick_until_qualified(streams, 0.0)

    assert [play.occurrence_id for play in plays] == ["fresh"]
    delta = streams.take_marker_changes()
    assert [marker.lineage for marker in delta.markers_set] == [LINEAGES[KEYS[1]]]
    assert delta.lineages_cleared == ()


def test_marker_cannot_be_consumed_by_another_stream_on_the_same_track() -> None:
    streams = seeded(hint_for(track_key("X")), new_occurrence_id=lambda: "fresh")
    today = T0 + timedelta(days=2)
    missing = normalize_state("unavailable", {}, today)

    streams.apply(
        solo(*KEYS), {KEYS[0]: missing, KEYS[1]: sample("X", position=0, at=today)}, 0.0
    )
    streams.apply(solo(*KEYS), {KEYS[1]: sample("Y", at=today)}, 15.0)
    streams.apply(solo(*KEYS), {KEYS[0]: sample("X", position=100, at=today)}, 30.0)
    plays = tick_until_qualified(streams, 30.0)

    assert [play for play in plays if play.metadata.title == "X"] == []
    resumed = streams.active_occurrences[0]
    assert (resumed.occurrence_id, resumed.started_at) == ("hinted", T0)


def test_each_stream_reads_only_its_own_marker() -> None:
    first = ResumeHint("first", track_key(), T0, LINEAGES[KEYS[0]])
    second = ResumeHint("second", track_key(), T0, LINEAGES[KEYS[1]])
    streams = seeded(first, second)
    at = T0 + timedelta(seconds=60)

    streams.apply(solo(*KEYS), {name: sample(position=60, at=at) for name in KEYS}, 0.0)

    assert [o.occurrence_id for o in streams.active_occurrences] == ["first", "second"]


def test_marker_waits_for_a_playing_or_paused_sample() -> None:
    streams = seeded(hint_for(track_key()))
    at = T0 + timedelta(seconds=60)

    streams.apply(solo(KEYS[0]), {KEYS[0]: sample(position=60, at=at, state="idle")}, 0)
    assert streams.active_occurrences == ()

    streams.apply(
        solo(KEYS[0]), {KEYS[0]: sample(position=60, at=at, state="paused")}, 1.0
    )
    assert streams.active_occurrences[0].occurrence_id == "hinted"


def test_marker_of_a_group_member_is_read_by_the_stream_of_its_group() -> None:
    group = Topology((CanonicalGroup(KEYS[0], frozenset(KEYS)),), frozenset())
    streams = seeded(hint_for(track_key(), LINEAGES[KEYS[1]]))
    at = T0 + timedelta(seconds=60)

    streams.apply(group, {KEYS[0]: sample(position=60, at=at)}, 0.0)

    assert streams.active_occurrences[0].occurrence_id == "hinted"


def test_unselected_leader_gets_no_marker() -> None:
    group = Topology(
        (CanonicalGroup("media_player.c", frozenset({"media_player.c", KEYS[0]})),),
        frozenset(),
    )
    streams = seeded()

    streams.apply(group, {"media_player.c": sample()}, 0.0)

    set_markers = streams.take_marker_changes().markers_set
    assert [marker.lineage for marker in set_markers] == [LINEAGES[KEYS[0]]]


def test_excluded_source_on_the_marker_track_decides_nothing() -> None:
    streams = seeded(hint_for(track_key()))
    too_early = T0 + timedelta(seconds=-60)
    excluded = {
        "source": "TV",
        "media_artist": "Artist",
        "media_title": "Title",
        "media_duration": 200,
    }

    streams.apply(
        solo(KEYS[0]), {KEYS[0]: normalize_state("playing", excluded, too_early)}, 0.0
    )
    at = T0 + timedelta(seconds=60)
    streams.apply(solo(KEYS[0]), {KEYS[0]: sample(position=60, at=at)}, 1.0)

    assert [play.occurrence_id for play in tick_until_qualified(streams, 1.0)] == [
        "hinted"
    ]


def test_uncertainty_erases_what_a_stream_proved() -> None:
    streams = seeded()
    streams.apply(solo(KEYS[0]), {KEYS[0]: sample()}, 0.0)
    streams.take_marker_changes()
    uncertain = Topology((), frozenset({KEYS[0]}))
    streams.apply(uncertain, {KEYS[0]: sample()}, 1.0)

    joined = Topology((CanonicalGroup(KEYS[0], frozenset(KEYS)),), frozenset())
    streams.apply(joined, {}, 2.0)

    assert streams.take_marker_changes() == MarkerDelta()


MERGED = Topology((CanonicalGroup(KEYS[0], frozenset(KEYS)),), frozenset())


def contested_markers(*ids: str) -> tuple[ResumeHint, ResumeHint]:
    """Give the two members of a merged group a marker of one track each."""
    first, second = (
        ResumeHint(occurrence_id, track_key(), T0 + hours, LINEAGES[key])
        for occurrence_id, hours, key in zip(
            ids, (timedelta(0), timedelta(hours=1)), KEYS, strict=True
        )
    )
    return first, second


@pytest.mark.parametrize(
    "ids",
    [("older", "newer"), ("newer", "older")],
    ids=["older_on_first_member", "newer_on_first_member"],
)
@pytest.mark.parametrize(
    "resumed_at",
    [timedelta(hours=1, seconds=100), timedelta(days=2)],
    ids=["both_resumable", "neither_resumable"],
)
def test_contested_markers_suppress_and_stay_untouched(
    ids: tuple[str, str], resumed_at: timedelta
) -> None:
    streams = seeded(*contested_markers(*ids))
    at = T0 + resumed_at

    streams.apply(MERGED, {KEYS[0]: sample(position=100, at=at)}, 0.0)
    plays = tick_until_qualified(streams, 0.0)

    assert plays == []
    assert streams.take_marker_changes() == MarkerDelta()

    later = 40.0 * HEARTBEAT
    streams.apply(MERGED, {KEYS[0]: sample("Next", at=at)}, later)
    set_markers = streams.take_marker_changes().markers_set
    active = [marker for marker in set_markers if not marker.prior]
    assert {marker.lineage for marker in active} == set(LINEAGES.values())
    assert {marker.track_key for marker in active} == {track_key("Next")}
    # The follower may have joined while nobody watched, so it keeps its identity.
    priors = [marker for marker in set_markers if marker.prior]
    assert [(m.lineage, m.track_key) for m in priors] == [
        (LINEAGES[KEYS[1]], track_key())
    ]


def test_contested_track_earns_again_after_a_proven_restart() -> None:
    streams = seeded(*contested_markers("older", "newer"))
    at = T0 + timedelta(hours=1, seconds=100)
    queued = {
        "source": "Spotify Connect",
        "media_artist": "Artist",
        "media_title": "Title",
        "media_duration": 200,
        "queue_position": 1,
        "media_position_updated_at": at,
    }
    first = normalize_state("playing", {**queued, "media_position": 100}, at)
    streams.apply(MERGED, {KEYS[0]: first}, 0.0)
    assert streams.tick(HEARTBEAT) == ()
    streams.take_marker_changes()

    restart = at + timedelta(seconds=HEARTBEAT)
    second = normalize_state(
        "playing",
        {
            **queued,
            "queue_position": 2,
            "media_position": 0,
            "media_position_updated_at": restart,
        },
        restart,
    )
    streams.apply(MERGED, {KEYS[0]: second}, HEARTBEAT)

    set_markers = streams.take_marker_changes().markers_set
    assert {marker.lineage for marker in set_markers} == set(LINEAGES.values())
    assert len(tick_until_qualified(streams, HEARTBEAT)) == 1


def test_one_identity_on_several_members_resumes_normally() -> None:
    shared = (
        ResumeHint("shared", track_key(), T0, LINEAGES[KEYS[0]]),
        ResumeHint("shared", track_key(), T0, LINEAGES[KEYS[1]]),
    )
    streams = seeded(*shared)
    at = T0 + timedelta(seconds=100)

    streams.apply(MERGED, {KEYS[0]: sample(position=100, at=at)}, 0.0)
    plays = tick_until_qualified(streams, 0.0)

    assert [(play.occurrence_id, play.started_at) for play in plays] == [("shared", T0)]
    assert streams.take_marker_changes() == MarkerDelta()


def test_resumable_marker_wins_over_one_outside_the_window() -> None:
    far = T0 - timedelta(days=2)
    streams = seeded(
        ResumeHint("stale", track_key(), far, LINEAGES[KEYS[0]]),
        ResumeHint("current", track_key(), T0, LINEAGES[KEYS[1]]),
    )
    at = T0 + timedelta(seconds=100)

    streams.apply(MERGED, {KEYS[0]: sample(position=100, at=at)}, 0.0)

    assert [play.occurrence_id for play in tick_until_qualified(streams, 0.0)] == [
        "current"
    ]


JOINED = Topology((CanonicalGroup(KEYS[1], frozenset(KEYS)),), frozenset())
A_LINEAGE = LINEAGES[KEYS[0]]
B_LINEAGE = LINEAGES[KEYS[1]]
FAR = T0 + timedelta(days=2)


def priors_after_join(*hints: ResumeHint) -> StreamSet:
    """Join A to B playing Z two days after A's marker was set."""
    streams = seeded(*hints)
    streams.apply(JOINED, {KEYS[1]: sample("Z", position=30, at=FAR)}, 0.0)
    return streams


def prior_of(streams: StreamSet, lineage: str = A_LINEAGE) -> list[ResumeHint]:
    set_markers = streams.take_marker_changes().markers_set
    return [m for m in set_markers if m.prior and m.lineage == lineage]


def test_join_moves_the_marker_to_the_prior_slot() -> None:
    held = hint_for(track_key("X"))
    streams = priors_after_join(held)

    delta = streams.take_marker_changes()

    assert replace(held, prior=True) in delta.markers_set
    assert {(m.lineage, m.prior) for m in delta.markers_set} == {
        (A_LINEAGE, False),
        (A_LINEAGE, True),
        (B_LINEAGE, False),
    }
    assert replace(delta, markers_set=()) == MarkerDelta()


def test_own_track_change_makes_no_prior() -> None:
    streams = seeded(hint_for(track_key("X")))

    streams.apply(solo(KEYS[0]), {KEYS[0]: sample("Y", position=0, at=FAR)}, 0.0)

    assert prior_of(streams) == []


def test_leader_of_a_join_makes_no_prior() -> None:
    held = ResumeHint("leader", track_key("V"), T0, B_LINEAGE)
    streams = seeded(held)
    streams.apply(JOINED, {KEYS[1]: sample("Z", position=0, at=FAR)}, 0.0)

    assert prior_of(streams, B_LINEAGE) == []


def test_split_makes_no_prior() -> None:
    streams = seeded(
        ResumeHint("shared", track_key("Z"), T0, A_LINEAGE),
        ResumeHint("shared", track_key("Z"), T0, B_LINEAGE),
    )
    streams.apply(JOINED, {KEYS[1]: sample("Z", at=T0)}, 0.0)
    streams.take_marker_changes()

    streams.apply(
        solo(*KEYS),
        {KEYS[0]: sample("Own", position=0, at=T0), KEYS[1]: sample("Z", at=T0)},
        1.0,
    )

    assert prior_of(streams) == []


@pytest.mark.parametrize(
    "position",
    [None, 5.001, 100.0],
    ids=["no_position", "just_past_the_restart_bound", "mid_track"],
)
def test_mid_track_start_of_the_prior_never_earns(position: float | None) -> None:
    streams = priors_after_join(hint_for(track_key("X")))
    streams.take_marker_changes()
    later = FAR + timedelta(days=1)

    streams.apply(
        solo(*KEYS),
        {
            KEYS[0]: sample("X", position=position, at=later),
            KEYS[1]: sample("Z", at=later),
        },
        15.0,
    )
    plays = tick_until_qualified(streams, 15.0)

    assert [play for play in plays if play.metadata.title == "X"] == []
    resumed = streams.active_occurrences[0]
    assert (resumed.occurrence_id, resumed.started_at) == ("hinted", T0)
    assert prior_of(streams) == []


def test_mid_track_start_of_the_prior_in_the_window_keeps_its_identity() -> None:
    streams = seeded(hint_for(track_key("X")))
    at = T0 + timedelta(seconds=120)
    streams.apply(JOINED, {KEYS[1]: sample("Z", position=30, at=at)}, 0.0)

    streams.apply(
        solo(*KEYS),
        {KEYS[0]: sample("X", position=100, at=at), KEYS[1]: sample("Z", at=at)},
        15.0,
    )
    plays = tick_until_qualified(streams, 15.0)

    assert [
        (play.occurrence_id, play.started_at)
        for play in plays
        if play.metadata.title == "X"
    ] == [("hinted", T0)]


@pytest.mark.parametrize("position", [0.0, 5.0], ids=["start", "restart_bound"])
def test_proven_fresh_start_earns_and_clears_only_the_prior(position: float) -> None:
    streams = priors_after_join(hint_for(track_key("X")))
    streams.take_marker_changes()
    later = FAR + timedelta(days=1)

    streams.apply(
        solo(*KEYS),
        {
            KEYS[0]: sample("X", position=position, at=later),
            KEYS[1]: sample("Z", at=later),
        },
        15.0,
    )

    delta = streams.take_marker_changes()
    assert delta.priors_cleared == (A_LINEAGE,)
    assert delta.lineages_cleared == ()
    plays = [
        play
        for play in tick_until_qualified(streams, 15.0)
        if play.metadata.title == "X"
    ]
    assert len(plays) == 1
    assert plays[0].occurrence_id != "hinted"


def test_prior_on_the_stream_of_another_speaker_decides_nothing() -> None:
    streams = priors_after_join(hint_for(track_key("X")))
    streams.apply(solo(*KEYS), {KEYS[1]: sample("Y", at=FAR)}, 15.0)
    streams.take_marker_changes()
    later = FAR + timedelta(days=1)

    streams.apply(
        solo(*KEYS),
        {
            KEYS[0]: sample("Own", position=0, at=later),
            KEYS[1]: sample("X", position=100, at=later),
        },
        30.0,
    )
    plays = tick_until_qualified(streams, 30.0)

    assert [play.metadata.title for play in plays].count("X") == 1


def test_seeded_prior_decides_after_a_restart() -> None:
    first = priors_after_join(hint_for(track_key("X")))
    streams = seeded(*first.take_marker_changes().markers_set)
    later = FAR + timedelta(days=1)

    streams.apply(solo(KEYS[0]), {KEYS[0]: sample("X", position=100, at=later)}, 0.0)

    assert tick_until_qualified(streams, 0.0) == []
    assert streams.active_occurrences[0].occurrence_id == "hinted"
    assert streams.active_occurrences[0].started_at == T0


def test_each_lineage_keeps_one_prior_and_the_newer_wins() -> None:
    streams = priors_after_join(hint_for(track_key("X")))
    streams.apply(solo(*KEYS), {KEYS[0]: sample("Y", at=FAR)}, 1.0)
    streams.take_marker_changes()

    streams.apply(JOINED, {KEYS[1]: sample("W", at=FAR)}, 2.0)

    moved = prior_of(streams)
    assert [marker.track_key for marker in moved] == [track_key("Y")]
    assert list(streams._priors) == [A_LINEAGE]


def test_seeding_keeps_one_prior_per_lineage() -> None:
    older = ResumeHint("older", track_key("X"), T0, A_LINEAGE, prior=True)
    newer = ResumeHint("newer", track_key("Y"), T0, A_LINEAGE, prior=True)

    streams = seeded(older, newer)

    assert streams._priors == {A_LINEAGE: newer}


def test_join_of_an_idle_stream_moves_the_marker_once_a_track_shows() -> None:
    held = hint_for(track_key("X"))
    streams = seeded(held)
    streams.apply(JOINED, {KEYS[1]: normalize_state("idle", {}, FAR)}, 0.0)
    assert streams.take_marker_changes() == MarkerDelta()

    streams.apply(JOINED, {KEYS[1]: sample("Z", at=FAR)}, 1.0)

    assert prior_of(streams) == [replace(held, prior=True)]


def test_leaving_before_any_track_showed_makes_no_prior() -> None:
    streams = seeded(hint_for(track_key("X")))
    streams.apply(JOINED, {KEYS[1]: normalize_state("idle", {}, FAR)}, 0.0)

    streams.apply(
        solo(*KEYS),
        {
            KEYS[0]: sample("Own", position=0, at=FAR),
            KEYS[1]: normalize_state("idle", {}, FAR),
        },
        1.0,
    )

    assert prior_of(streams) == []


def test_split_into_a_smaller_group_makes_no_prior() -> None:
    third = "media_player.c"
    trio = Topology((CanonicalGroup(KEYS[1], frozenset({*KEYS, third})),), frozenset())
    rest = Topology(
        (
            CanonicalGroup(KEYS[0], frozenset({KEYS[0], third})),
            CanonicalGroup(KEYS[1], frozenset({KEYS[1]})),
        ),
        frozenset(),
    )
    streams = StreamSet()
    streams.set_lineages({**LINEAGES, third: "registry-c"})
    streams.apply(trio, {KEYS[1]: sample("Z", position=30, at=T0)}, 0.0)
    streams.take_marker_changes()

    streams.apply(
        rest,
        {KEYS[0]: sample("Own", position=0, at=T0), KEYS[1]: sample("Z", at=T0)},
        1.0,
    )

    assert prior_of(streams) == []


def test_removal_forgets_the_prior_of_a_speaker_that_comes_back() -> None:
    streams = priors_after_join(hint_for(track_key("X")))
    streams.set_lineages({KEYS[1]: B_LINEAGE})
    streams.apply(JOINED, {KEYS[1]: sample("Z", at=FAR)}, 1.0)
    streams.set_lineages(LINEAGES)
    later = FAR + timedelta(days=1)

    streams.apply(
        solo(*KEYS),
        {KEYS[0]: sample("X", position=100, at=later), KEYS[1]: sample("Z", at=later)},
        2.0,
    )

    plays = tick_until_qualified(streams, 2.0)

    assert [play.metadata.title for play in plays].count("X") == 1


def test_removal_clears_a_lineage_that_only_holds_a_prior() -> None:
    streams = seeded(ResumeHint("left", track_key("X"), T0, A_LINEAGE, prior=True))
    streams.set_lineages({KEYS[1]: B_LINEAGE})

    streams.apply(solo(KEYS[1]), {KEYS[1]: normalize_state("idle", {}, FAR)}, 0.0)

    assert streams.take_marker_changes() == MarkerDelta(lineages_cleared=(A_LINEAGE,))


def test_join_on_the_same_occurrence_keeps_the_marker_for_the_next_track() -> None:
    shared = (
        ResumeHint("shared", track_key("Z"), T0, A_LINEAGE),
        ResumeHint("shared", track_key("Z"), T0, B_LINEAGE),
    )
    streams = seeded(*shared)
    streams.apply(JOINED, {KEYS[1]: sample("Z", position=30, at=T0)}, 0.0)
    assert prior_of(streams) == []

    streams.apply(JOINED, {KEYS[1]: sample("W", at=T0)}, 1.0)

    set_markers = streams.take_marker_changes().markers_set
    assert [m for m in set_markers if m.prior] == [replace(shared[0], prior=True)]


def sent_on_both() -> StreamSet:
    """Both speakers hold the marker of X, submitted two days ago."""
    return seeded(
        *(ResumeHint("sent-x", track_key("X"), T0, name) for name in LINEAGES.values())
    )


def resume_x_on_a_after_group_moved_on(streams: StreamSet) -> None:
    a, b = KEYS
    streams.apply(JOINED, {b: sample("Z", position=50, at=after(20))}, 20.0)
    streams.apply(
        solo(a, b),
        {a: sample("X", position=100, at=after(30)), b: sample("Z", at=after(30))},
        30.0,
    )
    plays = tick_until_qualified(streams, 30.0)

    assert [play for play in plays if play.metadata.title == "X"] == []
    resumed = streams.active_occurrences[0]
    assert (resumed.occurrence_id, resumed.started_at) == ("sent-x", T0)


def test_marker_moving_on_a_foreign_stream_of_the_same_occurrence_is_kept() -> None:
    a, b = KEYS
    paused = sample("X", position=100, at=FAR, state="paused")
    streams = sent_on_both()
    streams.apply(solo(a, b), {a: paused, b: paused}, 0.0)
    streams.apply(JOINED, {b: paused}, 10.0)

    resume_x_on_a_after_group_moved_on(streams)


def test_marker_moving_after_a_coordinator_swap_is_kept() -> None:
    a, b = KEYS
    paused = sample("X", position=100, at=FAR, state="paused")
    streams = sent_on_both()
    streams.apply(MERGED, {a: paused}, 0.0)
    streams.apply(JOINED, {b: paused}, 10.0)

    resume_x_on_a_after_group_moved_on(streams)


def test_follower_gaining_the_lead_on_the_same_occurrence_makes_no_prior() -> None:
    a, b = KEYS
    streams = sent_on_both()
    streams.apply(MERGED, {a: sample("X", position=100, at=FAR)}, 0.0)
    streams.apply(JOINED, {b: sample("X", position=105, at=after(5))}, 5.0)
    streams.take_marker_changes()

    streams.apply(JOINED, {b: sample("Z", position=50, at=after(10))}, 10.0)

    assert prior_of(streams, B_LINEAGE) == []


def test_fresh_start_clears_the_prior_of_every_member() -> None:
    streams = seeded(
        ResumeHint("first", track_key("X"), T0, A_LINEAGE, prior=True),
        ResumeHint("second", track_key("X"), T0, B_LINEAGE, prior=True),
    )

    streams.apply(MERGED, {KEYS[0]: sample("X", position=0, at=FAR)}, 0.0)

    delta = streams.take_marker_changes()
    assert delta.priors_cleared == (A_LINEAGE, B_LINEAGE)
    assert delta.lineages_cleared == ()
    assert len(tick_until_qualified(streams, 0.0)) == 1


def test_fresh_start_of_another_track_keeps_the_prior() -> None:
    held = ResumeHint("left", track_key("X"), T0, A_LINEAGE, prior=True)
    streams = seeded(held)

    streams.apply(solo(KEYS[0]), {KEYS[0]: sample("Y", position=0, at=FAR)}, 0.0)

    assert replace(streams.take_marker_changes(), markers_set=()) == MarkerDelta()


def test_prior_of_another_occurrence_contests_the_track() -> None:
    streams = seeded(
        ResumeHint("active", track_key("X"), T0, A_LINEAGE),
        ResumeHint("left", track_key("X"), T0, A_LINEAGE, prior=True),
    )
    at = T0 + timedelta(seconds=100)

    streams.apply(solo(KEYS[0]), {KEYS[0]: sample("X", position=100, at=at)}, 0.0)

    assert tick_until_qualified(streams, 0.0) == []
    assert streams.take_marker_changes() == MarkerDelta()


def test_priors_of_two_members_contest_a_mid_track_start() -> None:
    streams = seeded(
        ResumeHint("first", track_key("X"), T0, A_LINEAGE, prior=True),
        ResumeHint("second", track_key("X"), T0, B_LINEAGE, prior=True),
    )
    at = T0 + timedelta(seconds=100)

    streams.apply(MERGED, {KEYS[0]: sample("X", position=100, at=at)}, 0.0)

    assert tick_until_qualified(streams, 0.0) == []


def test_active_marker_outranks_a_fresh_start_of_the_same_track() -> None:
    held = ResumeHint("left", track_key("X"), T0, A_LINEAGE, prior=True)
    streams = seeded(ResumeHint("active", track_key("X"), T0, A_LINEAGE), held)

    streams.apply(solo(KEYS[0]), {KEYS[0]: sample("X", position=0, at=FAR)}, 0.0)

    assert tick_until_qualified(streams, 0.0) == []
    assert streams._priors == {A_LINEAGE: held}


def test_marker_of_one_identity_in_both_slots_is_no_contest() -> None:
    streams = seeded(
        ResumeHint("same", track_key("X"), T0, A_LINEAGE),
        ResumeHint("same", track_key("X"), T0, A_LINEAGE, prior=True),
    )
    at = T0 + timedelta(seconds=100)

    streams.apply(solo(KEYS[0]), {KEYS[0]: sample("X", position=100, at=at)}, 0.0)

    assert [play.occurrence_id for play in tick_until_qualified(streams, 0.0)] == [
        "same"
    ]


A_KEY = KEYS[0]
ONLY_A = solo(A_KEY)
HELD_POSITION = 100.0


class SavedMarkers:
    """The durable marker table: one row per lineage and slot."""

    def __init__(self, *hints: ResumeHint) -> None:
        self.rows: dict[tuple[str, str], ResumeHint | None] = {
            (hint.lineage, "prior" if hint.prior else "active"): hint for hint in hints
        }

    def save(self, streams: StreamSet) -> None:
        delta = streams.take_marker_changes()
        for marker in delta.markers_set:
            slot = "prior" if marker.prior else "active"
            self.rows[marker.lineage, slot] = marker
        for lineage in delta.lineages_cleared:
            self.rows = {
                key: row for key, row in self.rows.items() if key[0] != lineage
            }
        for lineage in delta.priors_cleared:
            del self.rows[lineage, "prior"]
        for lineage in delta.guards_set:
            self.rows[lineage, "guard"] = None
        for lineage in delta.guards_cleared:
            del self.rows[lineage, "guard"]

    @property
    def occurrence_ids(self) -> set[str]:
        return {row.occurrence_id for row in self.rows.values() if row is not None}

    @property
    def guarded(self) -> set[str]:
        return {lineage for lineage, slot in self.rows if slot == "guard"}

    def restore(self, lineages: Mapping[str, str] | None = None) -> StreamSet:
        streams = StreamSet()
        streams.set_lineages({A_KEY: A_LINEAGE} if lineages is None else lineages)
        hints = [row for row in self.rows.values() if row is not None]
        streams.seed_resume_hints(hints, self.guarded)
        return streams


def foreign(number: int) -> tuple[str, Topology]:
    leader = f"media_player.foreign_{number}"
    group = CanonicalGroup(leader, frozenset({A_KEY, leader}))
    return leader, Topology((group,), frozenset())


def after(seconds: float) -> datetime:
    return FAR + timedelta(seconds=seconds)


def take_detours(streams: StreamSet, count: int) -> float:
    """Hold X paused, pass through short foreign streams, return the next moment."""
    streams.apply(
        ONLY_A,
        {A_KEY: sample("X", position=HELD_POSITION, at=FAR, state="paused")},
        0.0,
    )
    for number in range(1, count + 1):
        now = number * 10.0
        leader, topology = foreign(number)
        streams.apply(
            topology, {leader: sample(f"Z-{number}", position=50, at=after(now))}, now
        )
    return (count + 1) * 10.0


def play_on_a(
    streams: StreamSet, now: float, title: str = "X", position: float | None = 100.0
) -> list[QualifiedPlay]:
    """Start a track on A's own queue and cross its threshold."""
    streams.apply(ONLY_A, {A_KEY: sample(title, position=position, at=after(now))}, now)
    plays = tick_until_qualified(streams, now)
    return [play for play in plays if play.metadata.title == title]


def detoured(count: int) -> tuple[StreamSet, float]:
    streams = StreamSet()
    streams.set_lineages({A_KEY: A_LINEAGE})
    streams.seed_resume_hints((hint_for(track_key("X")),), ())
    return streams, take_detours(streams, count)


def test_offline_room_marker_is_not_consumed_by_an_independent_stream() -> None:
    streams = seeded(hint_for(track_key("X")))
    a, b = KEYS
    only_b = Topology((CanonicalGroup(b, frozenset({b})),), frozenset({a}))
    missing = normalize_state("unavailable", {}, FAR)

    streams.apply(only_b, {a: missing, b: sample("X", position=0, at=FAR)}, 0.0)
    streams.apply(only_b, {a: missing, b: sample("Y", at=after(10))}, 10.0)
    streams.apply(
        solo(a, b),
        {
            a: sample("X", position=100, at=after(20)),
            b: sample("Y", position=10, at=after(20)),
        },
        20.0,
    )
    plays = tick_until_qualified(streams, 20.0)

    assert [play for play in plays if play.metadata.title == "X"] == []
    assert any(o.occurrence_id == "hinted" for o in streams.active_occurrences)


def test_detour_through_an_unselected_leader_keeps_the_identity() -> None:
    """Supervisor 17:37: Z was heard for ten seconds and never qualified."""
    streams = StreamSet()
    streams.set_lineages({A_KEY: A_LINEAGE})
    streams.seed_resume_hints((hint_for(track_key("X")),), ())

    streams.apply(
        ONLY_A,
        {A_KEY: sample("X", position=100, at=FAR, state="paused")},
        0.0,
    )
    leader, joined = foreign(1)
    streams.apply(joined, {leader: sample("Z", position=50, at=after(10))}, 10.0)
    streams.apply(ONLY_A, {A_KEY: sample("X", position=100, at=after(20))}, 20.0)
    plays: list[QualifiedPlay] = []
    for now in range(30, 121, 10):
        plays += streams.tick(float(now))

    assert plays == []
    resumed = streams.active_occurrences[0]
    assert (resumed.occurrence_id, resumed.started_at) == ("hinted", T0)


@pytest.mark.parametrize("count", [1, 2, 3], ids=["one", "two", "three"])
def test_foreign_detours_cannot_earn_the_held_track_again(count: int) -> None:
    streams, now = detoured(count)

    assert play_on_a(streams, now) == []


@pytest.mark.parametrize("count", [2, 3], ids=["two", "three"])
def test_overwritten_prior_sets_the_guard(count: int) -> None:
    streams, _now = detoured(count)

    assert streams.take_marker_changes().guards_set == (A_LINEAGE,)


def test_one_detour_sets_no_guard() -> None:
    streams, _now = detoured(1)

    assert streams.take_marker_changes().guards_set == ()


def test_prior_of_the_same_occurrence_sets_no_guard() -> None:
    streams = seeded(
        ResumeHint("same", track_key("X"), T0, A_LINEAGE),
        ResumeHint("same", track_key("X"), T0, A_LINEAGE, prior=True),
    )

    streams.apply(JOINED, {KEYS[1]: sample("Z", position=30, at=FAR)}, 0.0)

    assert streams.take_marker_changes().guards_set == ()


@pytest.mark.parametrize("restarts", [1, 2], ids=["one_restart", "two_restarts"])
def test_guard_keeps_the_held_track_unsendable_across_restarts(restarts: int) -> None:
    """Supervisor 17:48: recreate from saved state while X is held mid-track."""
    saved = SavedMarkers(hint_for(track_key("X")))
    streams, now = detoured(2)
    assert play_on_a(streams, now) == []
    saved.save(streams)
    held = streams.active_occurrences[0].occurrence_id

    for restart in range(1, restarts + 1):
        assert held not in saved.occurrence_ids
        streams = saved.restore()
        assert play_on_a(streams, restart * 1000.0) == []
        saved.save(streams)
        held = streams.active_occurrences[0].occurrence_id

    assert saved.guarded == {A_LINEAGE}
    assert held not in saved.occurrence_ids


@pytest.mark.parametrize(
    ("title", "position"),
    [("X", 100.0), ("X", None), ("Other", 100.0), ("Other", None), ("X", 5.001)],
    ids=["held", "held_no_position", "other", "other_no_position", "past_the_bound"],
)
def test_guard_suppresses_every_doubtful_start_and_writes_no_marker(
    title: str, position: float | None
) -> None:
    streams, now = detoured(2)
    streams.take_marker_changes()

    assert play_on_a(streams, now, title, position) == []
    assert streams.take_marker_changes() == MarkerDelta()


def test_guard_suppresses_a_resumable_active_marker_too() -> None:
    streams = seeded(hint_for(track_key("X")), guarded=(A_LINEAGE,))
    at = T0 + timedelta(seconds=100)

    streams.apply(solo(KEYS[0]), {KEYS[0]: sample("X", position=100, at=at)}, 0.0)

    assert tick_until_qualified(streams, 0.0) == []


@pytest.mark.parametrize(
    ("lineage", "other"),
    [(A_LINEAGE, B_LINEAGE), (B_LINEAGE, A_LINEAGE)],
    ids=["leader", "follower"],
)
def test_guard_of_one_member_holds_its_group_stream(lineage: str, other: str) -> None:
    streams = seeded(guarded=(lineage,))

    streams.apply(MERGED, {KEYS[0]: sample("X", position=100, at=T0)}, 0.0)

    assert tick_until_qualified(streams, 0.0) == []
    assert streams.take_marker_changes() == MarkerDelta(guards_set=(other,))


def test_held_track_cannot_escape_to_a_member_that_joined_across_a_restart() -> None:
    a, b = KEYS
    saved = SavedMarkers()
    saved.rows[A_LINEAGE, "guard"] = None
    streams = saved.restore(LINEAGES)
    streams.apply(solo(a), {a: sample("X", position=100, at=T0)}, 0.0)
    streams.apply(MERGED, {a: sample("X", position=115, at=T0)}, 15.0)
    saved.save(streams)
    assert saved.guarded == {A_LINEAGE, B_LINEAGE}

    streams.apply(
        solo(a, b),
        {a: sample("Other", position=0, at=T0), b: sample("X", position=130, at=T0)},
        30.0,
    )
    saved.save(streams)
    assert saved.guarded == {B_LINEAGE}

    streams = saved.restore(LINEAGES)
    streams.apply(
        solo(a, b),
        {a: sample("Other", at=FAR), b: sample("X", position=160, at=FAR)},
        0.0,
    )
    plays = tick_until_qualified(streams, 0.0)

    assert [play for play in plays if play.metadata.title == "X"] == []


def test_member_joining_an_idle_held_stream_gets_the_guard() -> None:
    a = KEYS[0]
    streams = seeded(guarded=(A_LINEAGE,))
    streams.apply(solo(a), {a: sample("X", position=100, at=T0)}, 0.0)
    streams.take_marker_changes()

    streams.apply(MERGED, {a: normalize_state("idle", {}, T0)}, 15.0)

    assert streams.take_marker_changes().guards_set == (B_LINEAGE,)


def test_contested_stream_sets_no_guard() -> None:
    streams = seeded(*contested_markers("older", "newer"))
    at = T0 + timedelta(hours=1, seconds=100)

    streams.apply(MERGED, {KEYS[0]: sample(position=100, at=at)}, 0.0)

    assert streams.take_marker_changes() == MarkerDelta()


@pytest.mark.parametrize("owner", [StreamSet, ObservationRuntime])
def test_guarded_lineages_cannot_be_left_out_of_seeding(owner: type) -> None:
    parameter = signature(owner.seed_resume_hints).parameters["guarded"]

    assert parameter.default is Parameter.empty


def test_held_occurrence_does_not_reach_a_marker_through_a_split() -> None:
    streams = seeded(guarded=(LINEAGES[KEYS[1]],))
    at = T0 + timedelta(seconds=100)
    streams.apply(MERGED, {KEYS[0]: sample("X", position=100, at=at)}, 0.0)
    streams.take_marker_changes()

    streams.apply(
        solo(*KEYS),
        {
            KEYS[0]: sample("Own", position=0, at=at),
            KEYS[1]: sample("X", position=2, at=at),
        },
        1.0,
    )

    set_markers = streams.take_marker_changes().markers_set
    assert [(m.lineage, m.track_key) for m in set_markers] == [
        (A_LINEAGE, track_key("Own"))
    ]


def test_fresh_track_in_a_foreign_group_earns_and_keeps_the_guard() -> None:
    streams, now = detoured(2)
    streams.take_marker_changes()
    leader, joined = foreign(3)

    streams.apply(joined, {leader: sample("W", position=0, at=after(now))}, now)
    plays = tick_until_qualified(streams, now)

    assert [play.metadata.title for play in plays] == ["W"]
    assert streams.take_marker_changes().guards_cleared == ()
    later = now + 1000.0
    assert play_on_a(streams, later) == []


def test_fresh_track_on_the_own_queue_clears_the_guard_and_earns() -> None:
    streams, now = detoured(2)
    saved = SavedMarkers()
    saved.save(streams)
    assert saved.guarded == {A_LINEAGE}

    assert [p.metadata.title for p in play_on_a(streams, now, "Y", 0.0)] == ["Y"]

    assert streams.take_marker_changes().guards_cleared == (A_LINEAGE,)


def test_fresh_track_led_by_the_lineage_in_a_group_clears_the_guard() -> None:
    streams = seeded(guarded=(A_LINEAGE,))

    streams.apply(MERGED, {KEYS[0]: sample("Y", position=0, at=T0)}, 0.0)

    assert streams.take_marker_changes().guards_cleared == (A_LINEAGE,)


def test_fresh_track_led_by_another_member_keeps_the_guard_of_a_follower() -> None:
    streams = seeded(guarded=(LINEAGES[KEYS[1]],))

    streams.apply(MERGED, {KEYS[0]: sample("Y", position=0, at=T0)}, 0.0)

    assert streams.take_marker_changes().guards_cleared == ()


def test_fresh_start_of_the_held_track_on_the_own_queue_earns_and_clears() -> None:
    streams, now = detoured(2)
    streams.take_marker_changes()

    plays = play_on_a(streams, now, "X", 0.0)

    assert len(plays) == 1
    assert plays[0].occurrence_id != "hinted"
    assert plays[0].started_at == after(now)
    assert streams.take_marker_changes().guards_cleared == (A_LINEAGE,)


def test_cleared_guard_lets_a_later_track_earn_normally() -> None:
    streams, now = detoured(2)
    assert play_on_a(streams, now, "Y", 0.0) != []

    later = now + 1000.0
    assert play_on_a(streams, later, "V", 100.0) != []


def test_genuine_plays_keep_earning_while_guarded() -> None:
    streams, now = detoured(2)
    streams.take_marker_changes()
    earned: list[str] = []

    for number, title in enumerate(("X", "Z-1", "X"), start=3):
        leader, joined = foreign(number)
        streams.apply(joined, {leader: sample(title, position=0, at=after(now))}, now)
        earned += [p.metadata.title for p in tick_until_qualified(streams, now)]
        now += 1000.0

    assert earned == ["X", "Z-1", "X"]


@pytest.mark.parametrize("count", [1, 2, 3, 12], ids=str)
def test_detours_stay_within_three_rows_per_lineage(count: int) -> None:
    saved = SavedMarkers()
    streams, _now = detoured(count)
    saved.save(streams)

    slots = sorted(slot for lineage, slot in saved.rows if lineage == A_LINEAGE)
    assert len(slots) == len(set(slots)) <= 3
    assert len(streams._priors) == len(streams._markers) == 1
    assert len(streams._guards) <= 1


def test_removal_clears_a_lineage_that_only_holds_a_guard() -> None:
    streams = seeded(guarded=(A_LINEAGE,))
    streams.set_lineages({KEYS[1]: B_LINEAGE})

    streams.apply(solo(KEYS[1]), {KEYS[1]: normalize_state("idle", {}, FAR)}, 0.0)

    assert streams.take_marker_changes() == MarkerDelta(lineages_cleared=(A_LINEAGE,))
    assert not streams._guards


def test_guard_set_and_removal_in_one_report_is_only_the_removal() -> None:
    streams, _now = detoured(2)
    streams.set_lineages({})

    streams.apply(ONLY_A, {A_KEY: normalize_state("idle", {}, FAR)}, 100.0)

    delta = streams.take_marker_changes()
    assert delta.guards_set == ()
    assert delta.lineages_cleared == (A_LINEAGE,)


def test_marker_delta_hides_lineages_and_reports_emptiness() -> None:
    changed = MarkerDelta(guards_set=("registry-secret",))

    assert changed
    assert not MarkerDelta()
    assert "registry-secret" not in repr(changed)


def test_marker_changes_are_reported_once() -> None:
    streams = seeded()
    streams.apply(solo(KEYS[0]), {KEYS[0]: sample()}, 0.0)

    assert len(streams.take_marker_changes().markers_set) == 1
    streams.apply(solo(KEYS[0]), {KEYS[0]: sample()}, 1.0)
    assert streams.take_marker_changes() == MarkerDelta()


def test_stage_aggregates_by_priority_and_counters_are_summed() -> None:
    streams = StreamSet()
    streams.apply(
        solo(*KEYS),
        {KEYS[0]: sample("One"), KEYS[1]: sample("Two", state="paused")},
        0.0,
    )
    assert streams.snapshot.stage is Stage.OBSERVING

    tick_until_qualified(streams, 0.0)
    snapshot = streams.snapshot

    assert snapshot.stage is Stage.QUALIFIED
    assert snapshot.candidate_count == 1


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        (None, "idle", Reason.ENTITY_MISSING),
        ("idle", None, Reason.NO_MEDIA),
    ],
)
def test_stage_tie_uses_the_sorted_key(
    first: str | None, second: str | None, expected: Reason
) -> None:
    streams = StreamSet()
    streams.apply(
        solo(*KEYS),
        {
            KEYS[0]: normalize_state(first, {}, T0),
            KEYS[1]: normalize_state(second, {}, T0),
        },
        0.0,
    )

    assert streams.snapshot.stage is Stage.WAITING
    assert streams.snapshot.reason is expected


def test_empty_stream_set_reports_nothing_yet() -> None:
    snapshot = StreamSet().snapshot

    assert snapshot.stage is Stage.WAITING
    assert snapshot.reason is Reason.NO_MEDIA
    assert snapshot.candidate_count == 0
    assert StreamSet().seconds_until_next_tick() is None


def test_merged_stream_counters_do_not_fall() -> None:
    streams = StreamSet()
    streams.apply(solo(*KEYS), {KEYS[0]: sample("One"), KEYS[1]: sample("Two")}, 0.0)
    tick_until_qualified(streams, 0.0)
    assert streams.snapshot.candidate_count == 2

    merged = Topology((CanonicalGroup(KEYS[0], frozenset(KEYS)),), frozenset())
    streams.apply(merged, {KEYS[0]: sample("One")}, 1000.0)

    assert streams.snapshot.candidate_count == 2
    assert len(streams.active_occurrences) == 1
