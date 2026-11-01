"""Runtime behavior on real Home Assistant events and timers."""

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.personal_sonos_scrobbler.normalize import SourceCategory
from custom_components.personal_sonos_scrobbler.observation import Reason, Stage
from custom_components.personal_sonos_scrobbler.runtime import ObservationRuntime
from tests.helpers import Advance, active_timers, playing

ENTRY_ID = "pilot-entry"
STATE: dict[str, Any] = {
    "source": "Spotify Connect",
    "media_artist": "Artist",
    "media_title": "Title",
    "media_duration": 200,
}


@pytest.fixture
def sonos_entity(entity_registry: er.EntityRegistry) -> er.RegistryEntry:
    return entity_registry.async_get_or_create("media_player", "sonos", "RINCON_1")


@pytest.fixture
async def runtime(
    hass: HomeAssistant, sonos_entity: er.RegistryEntry
) -> AsyncIterator[ObservationRuntime]:
    instance = ObservationRuntime(hass, ENTRY_ID, [sonos_entity.id])
    yield instance
    instance.async_stop()


async def test_timer_crosses_threshold_without_state_change(
    hass: HomeAssistant,
    runtime: ObservationRuntime,
    sonos_entity: er.RegistryEntry,
    advance: Advance,
) -> None:
    hass.states.async_set(
        sonos_entity.entity_id, "playing", playing(sonos_entity.entity_id, **STATE)
    )
    runtime.async_start()

    for _ in range(6):
        await advance(15)
    assert runtime.snapshot.candidate_count == 0
    await advance(10)

    assert runtime.snapshot.candidate_count == 1
    assert runtime.snapshot.stage is Stage.QUALIFIED


async def test_no_timer_while_paused(
    hass: HomeAssistant,
    runtime: ObservationRuntime,
    sonos_entity: er.RegistryEntry,
) -> None:
    baseline = active_timers(hass)
    hass.states.async_set(
        sonos_entity.entity_id, "paused", playing(sonos_entity.entity_id, **STATE)
    )

    runtime.async_start()

    assert active_timers(hass) == baseline


async def test_state_update_reschedules_single_timer(
    hass: HomeAssistant,
    runtime: ObservationRuntime,
    sonos_entity: er.RegistryEntry,
    advance: Advance,
) -> None:
    baseline = active_timers(hass)
    hass.states.async_set(
        sonos_entity.entity_id, "playing", playing(sonos_entity.entity_id, **STATE)
    )
    runtime.async_start()

    for volume in range(5):
        hass.states.async_set(
            sonos_entity.entity_id,
            "playing",
            playing(sonos_entity.entity_id, **STATE, volume_level=volume),
        )
    await hass.async_block_till_done()
    assert active_timers(hass) == baseline + 1

    for _ in range(6):
        await advance(15)
    await advance(10)
    await advance(15)

    assert runtime.snapshot.candidate_count == 1
    assert active_timers(hass) == baseline + 1


async def test_listeners_are_notified_and_removable(
    hass: HomeAssistant,
    runtime: ObservationRuntime,
    sonos_entity: er.RegistryEntry,
) -> None:
    listener = MagicMock()
    remove = runtime.async_add_listener(listener)
    runtime.async_start()
    listener.assert_called_once()

    remove()
    hass.states.async_set(
        sonos_entity.entity_id, "playing", playing(sonos_entity.entity_id, **STATE)
    )
    await hass.async_block_till_done()

    listener.assert_called_once()


async def test_stop_cancels_timer_and_listeners(
    hass: HomeAssistant,
    runtime: ObservationRuntime,
    sonos_entity: er.RegistryEntry,
    advance: Advance,
) -> None:
    baseline = active_timers(hass)
    hass.states.async_set(
        sonos_entity.entity_id, "playing", playing(sonos_entity.entity_id, **STATE)
    )
    runtime.async_start()
    await advance(15)
    before = runtime.snapshot

    runtime.async_stop()

    assert active_timers(hass) == baseline
    await advance(15)
    hass.states.async_set(
        sonos_entity.entity_id, "paused", playing(sonos_entity.entity_id, **STATE)
    )
    await hass.async_block_till_done()
    assert runtime.snapshot == before


async def test_starts_when_state_is_absent_then_recovers(
    hass: HomeAssistant,
    runtime: ObservationRuntime,
    sonos_entity: er.RegistryEntry,
) -> None:
    runtime.async_start()
    assert runtime.snapshot.reason is Reason.ENTITY_MISSING
    assert runtime.entity_present is True

    hass.states.async_set(
        sonos_entity.entity_id, "playing", playing(sonos_entity.entity_id, **STATE)
    )
    await hass.async_block_till_done()

    assert runtime.snapshot.stage is Stage.OBSERVING
    assert runtime.snapshot.reason is Reason.COUNTING


async def test_starts_when_state_is_unavailable_then_recovers(
    hass: HomeAssistant,
    runtime: ObservationRuntime,
    sonos_entity: er.RegistryEntry,
) -> None:
    hass.states.async_set(sonos_entity.entity_id, "unavailable", {})
    runtime.async_start()
    assert runtime.snapshot.reason is Reason.UNAVAILABLE

    hass.states.async_set(
        sonos_entity.entity_id, "playing", playing(sonos_entity.entity_id, **STATE)
    )
    await hass.async_block_till_done()

    assert runtime.snapshot.stage is Stage.OBSERVING


async def test_unknown_registry_id_starts_without_crashing(
    hass: HomeAssistant,
) -> None:
    runtime = ObservationRuntime(hass, ENTRY_ID, ["a" * 32])

    runtime.async_start()

    assert runtime.entity_present is False
    assert runtime.snapshot.reason is Reason.ENTITY_MISSING
    runtime.async_stop()


async def test_entity_removed_reports_entity_missing(
    hass: HomeAssistant,
    runtime: ObservationRuntime,
    sonos_entity: er.RegistryEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    baseline = active_timers(hass)
    hass.states.async_set(
        sonos_entity.entity_id, "playing", playing(sonos_entity.entity_id, **STATE)
    )
    runtime.async_start()

    entity_registry.async_remove(sonos_entity.entity_id)
    await hass.async_block_till_done()

    assert runtime.entity_present is False
    assert runtime.snapshot.reason is Reason.ENTITY_MISSING
    assert active_timers(hass) == baseline


async def test_rename_schedules_reload(
    hass: HomeAssistant,
    runtime: ObservationRuntime,
    sonos_entity: er.RegistryEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    runtime.async_start()

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        entity_registry.async_update_entity(
            sonos_entity.entity_id, new_entity_id="media_player.kitchen"
        )
        await hass.async_block_till_done()

    reload.assert_called_once_with(ENTRY_ID)


async def test_unrelated_registry_update_does_not_reload(
    hass: HomeAssistant,
    runtime: ObservationRuntime,
    sonos_entity: er.RegistryEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    runtime.async_start()

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        entity_registry.async_update_entity(sonos_entity.entity_id, name="Kitchen")
        await hass.async_block_till_done()

    reload.assert_not_called()


async def test_delayed_timer_gap_is_discarded(
    hass: HomeAssistant,
    runtime: ObservationRuntime,
    sonos_entity: er.RegistryEntry,
    advance: Advance,
) -> None:
    hass.states.async_set(
        sonos_entity.entity_id, "playing", playing(sonos_entity.entity_id, **STATE)
    )
    runtime.async_start()
    await advance(15)

    await advance(60)

    assert runtime.snapshot.observed_seconds == 15
    assert runtime.snapshot.clock_gap_count == 1
    assert runtime.snapshot.candidate_count == 0


async def test_stale_position_updated_at_keeps_counting(
    hass: HomeAssistant,
    runtime: ObservationRuntime,
    sonos_entity: er.RegistryEntry,
    advance: Advance,
) -> None:
    anchor = dt_util.utcnow() - timedelta(hours=2)
    attributes = {
        **STATE,
        "media_duration": 20_000,
        "media_position": 10,
        "media_position_updated_at": anchor,
    }
    hass.states.async_set(
        sonos_entity.entity_id, "playing", playing(sonos_entity.entity_id, **attributes)
    )
    runtime.async_start()
    await advance(15)

    hass.states.async_set(
        sonos_entity.entity_id,
        "playing",
        playing(sonos_entity.entity_id, **attributes, volume_level=0.2),
    )
    await hass.async_block_till_done()
    await advance(15)

    assert runtime.snapshot.observed_seconds == 30
    assert runtime.snapshot.seek_count == 0


async def test_spotify_connect_without_queue_position_and_with_zero_position(
    hass: HomeAssistant,
    runtime: ObservationRuntime,
    sonos_entity: er.RegistryEntry,
    advance: Advance,
) -> None:
    attributes = {
        **STATE,
        "media_content_type": "music",
        "media_position": 0,
        "media_position_updated_at": dt_util.utcnow() - timedelta(seconds=120),
        "group_members": [sonos_entity.entity_id],
    }
    hass.states.async_set(
        sonos_entity.entity_id, "playing", playing(sonos_entity.entity_id, **attributes)
    )
    runtime.async_start()

    for volume in range(6):
        await advance(15)
        hass.states.async_set(
            sonos_entity.entity_id,
            "playing",
            playing(sonos_entity.entity_id, **attributes, volume_level=volume),
        )
        await hass.async_block_till_done()
    await advance(10)

    snapshot = runtime.snapshot
    assert snapshot.source_category is SourceCategory.SPOTIFY_CONFIRMED
    assert snapshot.presence.queue_position is False
    assert snapshot.presence.position is True
    assert snapshot.seek_count == 0
    assert snapshot.ambiguous_restart_count == 0
    assert snapshot.candidate_count == 1
