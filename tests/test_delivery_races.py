"""Lifecycle races through a real service: each counts on the server apart.

A request already on the wire is allowed; a new one is not.
"""

import asyncio
import sqlite3
from datetime import timedelta
from itertools import count
from typing import Any
from unittest.mock import AsyncMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.exceptions import HomeAssistantError

from custom_components.personal_sonos_scrobbler.const import (
    CONF_DELIVERY_EPOCH,
    CONF_ENTITY_REGISTRY_IDS,
    CONF_NOW_PLAYING,
    CONF_OUTBOX_CREATED,
    CONF_SCROBBLING,
    CONF_SCROBBLING_SINCE,
)
from custom_components.personal_sonos_scrobbler.controls import Feature
from custom_components.personal_sonos_scrobbler.delivery import (
    DeliveryStatus,
    async_remove_ledger,
)
from custom_components.personal_sonos_scrobbler.normalize import (
    PlaybackSample,
    normalize_state,
)
from custom_components.personal_sonos_scrobbler.outbox import (
    ACTIVE_RETENTION_SECONDS,
    MAX_ACTIVE_PLAYS,
)
from custom_components.personal_sonos_scrobbler.playback import (
    CurrentPlayback,
    MarkerDelta,
)
from custom_components.personal_sonos_scrobbler.streams import StreamSet
from custom_components.personal_sonos_scrobbler.topology import (
    CanonicalGroup,
    Topology,
)
from tests.delivery_harness import (
    RECENT,
    SCROBBLE,
    STARTED,
    Pilot,
    accepted,
    held_before,
    held_commit,
    history,
    page,
    row,
)
from tests.helpers import link_account
from tests.lastfm_server import Reply, session_reply
from tests.outbox_support import (
    KEY_B,
    META,
    T0,
    at,
    fail_on,
    fill_plays,
    hint,
    qualified,
)

OTHER_ACCOUNT_NAME = "another_listener"
EPOCH_UPDATE = "UPDATE meta SET value = ? WHERE key = 'epoch' AND CAST"
CLEAR = "UPDATE plays SET state = 'discarded'"
INSERT_PLAY = "INSERT INTO plays"
CHECKPOINT = "INSERT OR REPLACE INTO checkpoints"
MINUTE = 60.0


async def queued_and_held(pilot: Pilot, method: str = SCROBBLE) -> None:
    """Reach a request of this method on the wire, with a play in the queue."""
    await pilot.linked()
    await pilot.accept_now()
    pilot.server.queue(RECENT, history())
    pilot.server.queue(SCROBBLE, accepted())
    pilot.server.hold(method)
    await pilot.past_grace()
    await pilot.reach(pilot.server.held.is_set, tick=1)


# Pause


async def test_disable_during_history_read(pilot: Pilot) -> None:
    await queued_and_held(pilot, RECENT)

    await pilot.pause()
    pilot.server.release(RECENT)
    await pilot.quiet()
    await pilot.advance(MINUTE)
    await pilot.quiet()

    assert pilot.reads() == 1
    assert pilot.posts() == 0
    assert pilot.state() == "pending"


async def test_pause_reports_success_only_after_the_epoch_committed(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    await pilot.linked()
    await pilot.accept_now()
    old_data = dict(pilot.entry.data)

    with held_commit(monkeypatch, EPOCH_UPDATE) as hold:
        pause = pilot.hass.async_create_task(pilot.pause())
        await pilot.reach(hold.reached.is_set)
        await pilot.linger()
        assert not pause.done()
        # The fence does not wait for the commit.
        assert pilot.entry.data[CONF_SCROBBLING] is False
        hold.release.set()
        await pause

    # Crash: the delayed config save never ran, the ledger has the pause.
    await pilot.restart(old_data)
    pilot.server.queue(RECENT, history())
    await pilot.past_grace()

    assert pilot.entry.data[CONF_SCROBBLING] is False
    assert pilot.entry.data[CONF_DELIVERY_EPOCH] > old_data[CONF_DELIVERY_EPOCH]
    assert pilot.reads() == 0
    assert pilot.posts() == 0


async def test_a_failed_pause_stays_fenced_and_the_status_says_so(
    pilot: Pilot,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await pilot.linked()
    await pilot.accept_now()

    with monkeypatch.context() as patch:
        fail_on(patch, EPOCH_UPDATE, message="canary-sqlite-text")
        await pilot.pause()

    assert "canary" not in caplog.text
    assert pilot.entry.data[CONF_SCROBBLING] is False
    assert pilot.service.status.status is DeliveryStatus.STORAGE_ERROR
    await pilot.past_grace()
    assert pilot.reads() == 0


# Enable


async def _linked_unenabled(pilot: Pilot) -> int:
    """Set up and link without enabling; return the epoch the entry has."""
    await pilot.setup()
    await link_account(pilot.hass, pilot.entry)
    return int(pilot.entry.data[CONF_DELIVERY_EPOCH])


async def _assert_off_after_a_reload(pilot: Pilot, epoch: int) -> None:
    """The entry is off at the staged epoch, and a play sends nothing."""
    data = pilot.entry.data
    assert data[CONF_SCROBBLING] is False
    assert data[CONF_SCROBBLING_SINCE] is None
    assert data[CONF_DELIVERY_EPOCH] == epoch

    if pilot.loaded():
        await pilot.restart()
    else:
        await pilot.setup()
    pilot.server.queue(RECENT, history())
    pilot.server.queue(SCROBBLE, accepted())
    await pilot.accept_now()
    await pilot.past_grace(3 * MINUTE)

    assert pilot.entry.data[CONF_SCROBBLING] is False
    assert pilot.reads() == 0
    assert pilot.posts() == 0


async def test_failed_enable_leaves_delivery_off_after_a_reload(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    epoch = await _linked_unenabled(pilot)

    with monkeypatch.context() as patch:
        fail_on(patch, EPOCH_UPDATE, message="canary-sqlite-text")
        with pytest.raises(HomeAssistantError) as failure:
            await pilot.press(Feature.SCROBBLING, "turn_on")

    assert "canary" not in str(failure.value)
    await _assert_off_after_a_reload(pilot, epoch + 1)


async def test_enable_publishes_true_only_after_the_durable_commit(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Home Assistant may save the entry at any write, so none may say True early."""
    await _linked_unenabled(pilot)
    writes: list[tuple[str, Any]] = []
    update_entry = pilot.hass.config_entries.async_update_entry

    def recording_update(entry: Any, **changes: Any) -> Any:
        changed = update_entry(entry, **changes)
        writes.append(("entry", entry.data[CONF_SCROBBLING]))
        return changed

    monkeypatch.setattr(
        pilot.hass.config_entries, "async_update_entry", recording_update
    )

    with monkeypatch.context() as patch:
        fail_on(patch, EPOCH_UPDATE, message="canary-sqlite-text")
        with pytest.raises(HomeAssistantError):
            await pilot.press(Feature.SCROBBLING, "turn_on")

    assert ("entry", False) in writes
    assert ("entry", True) not in writes

    await pilot.restart()
    writes.clear()
    outbox = pilot.service._outbox
    assert outbox is not None
    set_epoch = outbox.async_set_epoch

    async def recording_set_epoch(epoch: int) -> None:
        writes.append(("epoch", epoch))
        await set_epoch(epoch)

    monkeypatch.setattr(outbox, "async_set_epoch", recording_set_epoch)

    with held_commit(monkeypatch, EPOCH_UPDATE) as hold:
        enabling = pilot.hass.async_create_task(
            pilot.press(Feature.SCROBBLING, "turn_on")
        )
        await pilot.reach(hold.reached.is_set)
        await pilot.linger()
        assert not enabling.done()
        assert pilot.entry.data[CONF_SCROBBLING] is False
        assert pilot.entry.data[CONF_SCROBBLING_SINCE] is None
        hold.release.set()
        await enabling
    await pilot.quiet()

    assert pilot.entry.data[CONF_SCROBBLING] is True
    # Nothing fallible follows the publication of True.
    assert writes[-1] == ("entry", True)
    assert [name for name, _ in writes].count("epoch") == 1


async def test_unload_during_the_create_of_an_enable_leaves_delivery_off(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    epoch = await _linked_unenabled(pilot)
    service = pilot.service

    with held_before(monkeypatch, "CREATE INDEX plays_by_state") as hold:
        enabling = pilot.hass.async_create_task(
            pilot.press(Feature.SCROBBLING, "turn_on")
        )
        await pilot.reach(hold.reached.is_set)
        unloading = pilot.hass.async_create_task(
            pilot.hass.config_entries.async_unload(pilot.entry.entry_id)
        )
        await pilot.linger()
        hold.release.set()
        assert await unloading
        with pytest.raises(HomeAssistantError):
            await enabling

    assert service.closed
    # The create finished after the shutdown passed its timer step.
    assert service._cancel_maintenance is None
    # Stopped before it staged anything.
    await _assert_off_after_a_reload(pilot, epoch)


async def test_enable_on_an_adopted_ledger_ahead_reports_the_pause(
    pilot: Pilot,
) -> None:
    await pilot.linked()
    await pilot.unload()
    ahead = int(pilot.entry.data[CONF_DELIVERY_EPOCH]) + 10
    # The ledger kept a pause whose config save, and the created flag, were lost.
    connection = sqlite3.connect(pilot.path)
    with connection:
        connection.execute(
            "UPDATE meta SET value = ? WHERE key = 'epoch'", (str(ahead),)
        )
    connection.close()
    pilot.hass.config_entries.async_update_entry(
        pilot.entry,
        data={
            **pilot.entry.data,
            CONF_OUTBOX_CREATED: False,
            CONF_NOW_PLAYING: False,
            CONF_SCROBBLING: False,
            CONF_SCROBBLING_SINCE: None,
        },
    )
    await pilot.setup()

    with pytest.raises(HomeAssistantError):
        await pilot.press(Feature.SCROBBLING, "turn_on")

    assert pilot.entry.data[CONF_SCROBBLING] is False
    assert pilot.entry.data[CONF_DELIVERY_EPOCH] == ahead + 1
    assert pilot.service.status.status is DeliveryStatus.DISABLED


# Clear


async def test_clear_reports_success_only_after_the_commit(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    await pilot.linked()
    await pilot.accept_now()

    with held_commit(monkeypatch, CLEAR) as hold:
        clear = pilot.hass.async_create_task(pilot.option("clear_queue"))
        await pilot.reach(hold.reached.is_set)
        await pilot.linger()
        assert not clear.done()
        await pilot.accept_now("during", start=500, key=KEY_B)
        hold.release.set()
        result = await clear

    assert result["reason"] == "queue_cleared"
    assert pilot.column("title") == ""
    assert pilot.state() == "discarded"
    assert pilot.state("during") is None


async def test_failed_clear_keeps_the_metadata_and_says_so(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    await pilot.linked()
    await pilot.accept_now()

    with monkeypatch.context() as patch:
        fail_on(patch, CLEAR, message="canary-sqlite-text")
        result = await pilot.option("clear_queue")

    assert result["reason"] == "storage_error"
    assert "canary" not in str(result)
    assert pilot.state() == "pending"
    assert pilot.column("title") != ""
    assert pilot.service.status.status is DeliveryStatus.STORAGE_ERROR


async def test_clear_during_a_post_leaves_nothing_and_revives_nothing(
    pilot: Pilot,
) -> None:
    await queued_and_held(pilot)

    result = await pilot.option("clear_queue")
    active = pilot.rows(
        "SELECT COUNT(*) FROM plays WHERE state IN "
        "('pending', 'held', 'in_flight', 'uncertain') OR title <> ''"
    )
    pilot.server.release(SCROBBLE)
    await pilot.quiet()
    await pilot.past_grace(3 * MINUTE)

    assert result["reason"] == "queue_cleared"
    assert active == [(0,)]
    assert pilot.state() == "discarded"
    assert pilot.posts() == 1
    assert pilot.reads() == 1


async def test_clear_waits_for_a_held_producer_commit_and_leaves_nothing(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    await pilot.linked()

    with held_commit(monkeypatch, INSERT_PLAY) as hold:
        pilot.accept(qualified("occ"))
        await pilot.reach(hold.reached.is_set)
        clear = pilot.hass.async_create_task(pilot.option("clear_queue"))
        await pilot.linger()
        assert not clear.done()
        hold.release.set()
        result = await clear

    assert result["reason"] == "queue_cleared"
    assert pilot.state() == "discarded"
    assert pilot.column("title") == ""
    assert pilot.rows("SELECT COUNT(*) FROM plays WHERE title <> ''") == [(0,)]


async def test_clear_on_a_broken_ledger_is_refused(pilot: Pilot) -> None:
    await pilot.linked()
    await pilot.unload()
    pilot.path.unlink()
    await pilot.setup()

    result = await pilot.option("clear_queue")

    assert result["reason"] == "storage_error"
    assert not pilot.path.exists()


# Link


async def test_replace_account_during_post(pilot: Pilot) -> None:
    await queued_and_held(pilot)

    pilot.server.queue("auth.getSession", session_reply(name=OTHER_ACCOUNT_NAME))
    await link_account(pilot.hass, pilot.entry, "replace")
    pilot.server.release(SCROBBLE)
    await pilot.quiet()
    assert pilot.state() == "uncertain"
    await pilot.enable()
    await pilot.past_grace(3 * MINUTE)

    # Parked, not erased, and never reconciled against the other account.
    assert pilot.state() == "uncertain"
    assert pilot.column("title") != ""
    assert pilot.posts() == 1
    assert pilot.reads() == 1


@pytest.mark.parametrize("account", ["same", "other"])
async def test_unlink_then_relink_leaves_the_old_play_parked(
    pilot: Pilot, account: str
) -> None:
    await pilot.linked()
    await pilot.accept_now()

    assert (await pilot.option("disconnect"))["reason"] == "disconnected"
    assert pilot.entry.data[CONF_SCROBBLING] is False
    if account == "other":
        pilot.server.queue("auth.getSession", session_reply(name=OTHER_ACCOUNT_NAME))
    await link_account(pilot.hass, pilot.entry)
    await pilot.past_grace()
    assert pilot.reads() == 0

    await pilot.enable()
    await pilot.past_grace(3 * MINUTE)

    assert pilot.state() == "pending"
    assert (pilot.reads(), pilot.posts()) == (0, 0)


async def test_a_marker_during_ledger_creation_is_no_storage_fault(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    await pilot.setup()
    await link_account(pilot.hass, pilot.entry)

    with held_before(monkeypatch, "PRAGMA synchronous") as hold:
        enabling = pilot.hass.async_create_task(pilot.enable())
        await pilot.reach(hold.reached.is_set)
        pilot.service.async_on_occurrence_change(MarkerDelta(markers_set=(hint(),)))
        await pilot.linger()
        hold.release.set()
        await enabling

    await pilot.quiet()
    assert pilot.service.status.status is DeliveryStatus.READY


# Retention and maintenance


async def test_paused_ledger_erases_metadata_without_a_single_call(
    pilot: Pilot,
) -> None:
    await pilot.linked()
    await pilot.accept_now()
    await pilot.pause()

    await pilot.advance(ACTIVE_RETENTION_SECONDS + 2 * MINUTE)
    await pilot.reach(lambda: pilot.state() == "expired", tick=3600)

    assert pilot.column("title") == ""
    assert pilot.reads() == 0
    assert pilot.posts() == 0


async def test_startup_expires_before_anything_is_dispatched(pilot: Pilot) -> None:
    await pilot.linked()
    await pilot.accept_now()
    await pilot.unload()
    await pilot.advance(ACTIVE_RETENTION_SECONDS + 2 * MINUTE)

    await pilot.setup()
    await pilot.quiet()

    assert pilot.state() == "expired"
    assert pilot.reads() == 0
    assert pilot.posts() == 0


# Pacing


async def test_each_request_waits_for_the_ha_clock(pilot: Pilot) -> None:
    await pilot.linked()
    await pilot.accept_now()
    pilot.server.queue(RECENT, page(1, 2), page(2, 2))
    pilot.server.queue(SCROBBLE, accepted())

    await pilot.past_grace()
    await pilot.reach(lambda: pilot.reads() == 1)
    await pilot.quiet()
    assert pilot.reads() == 1

    await pilot.advance(1)
    await pilot.reach(lambda: pilot.reads() == 2)
    await pilot.quiet()
    assert pilot.posts() == 0

    await pilot.advance(1)
    await pilot.reach(lambda: pilot.posts() == 1)
    await pilot.delivered()


async def test_pause_during_the_wait_releases_no_post(pilot: Pilot) -> None:
    await pilot.linked()
    await pilot.accept_now()
    pilot.server.queue(RECENT, history())
    pilot.server.queue(SCROBBLE, accepted())
    await pilot.past_grace()
    await pilot.reach(lambda: pilot.reads() == 1)
    await pilot.quiet()

    await pilot.pause()
    await pilot.advance(MINUTE)
    await pilot.quiet()

    assert pilot.posts() == 0
    assert pilot.state() == "pending"


# Timers across a reload


async def test_stale_retry_timer_after_reload(pilot: Pilot) -> None:
    await pilot.linked()
    await pilot.accept_now()
    await pilot.restart()
    pilot.server.queue(RECENT, history())
    pilot.server.queue(SCROBBLE, accepted())

    await pilot.past_grace()
    await pilot.delivered()
    await pilot.past_grace(MINUTE)

    assert pilot.reads() == 1
    assert pilot.posts() == 1


# Stop, unload, removal


async def test_shutdown_while_checkpointing(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    await pilot.linked()
    service = pilot.service

    with held_commit(monkeypatch, CHECKPOINT) as hold:
        service.async_on_occurrence_change(MarkerDelta(markers_set=(hint(),)))
        await pilot.reach(hold.reached.is_set)
        stopping = asyncio.ensure_future(pilot.hass.async_stop())
        await pilot.linger()
        assert not stopping.done()
        assert not service.closed
        hold.release.set()
        await stopping

    assert service.closed
    assert len(pilot.rows("SELECT lineage FROM checkpoints")) == 1


async def test_shutdown_while_in_flight(pilot: Pilot) -> None:
    await queued_and_held(pilot)
    service = pilot.service

    await pilot.hass.async_stop()

    assert service.closed
    assert pilot.state() == "in_flight"
    assert pilot.posts() == 1


async def test_restart_after_a_post_cut_off_only_reads(pilot: Pilot) -> None:
    await queued_and_held(pilot)

    await pilot.unload()
    assert pilot.state() == "in_flight"
    pilot.server.release(SCROBBLE)
    pilot.server.queue(RECENT, history(row(STARTED)))
    await pilot.setup()

    assert pilot.state() == "uncertain"
    await pilot.reach(lambda: pilot.state() != "uncertain")
    assert pilot.posts() == 1


async def test_stop_at_a_claim_commit_waits_for_the_thread(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    await pilot.linked()
    await pilot.accept_now()
    pilot.server.queue(RECENT, history())
    service = pilot.service

    with held_commit(monkeypatch, "UPDATE plays SET state = 'in_flight'") as hold:
        await pilot.past_grace()
        await pilot.reach(hold.reached.is_set, tick=1)
        stopping = asyncio.ensure_future(pilot.hass.async_stop())
        await pilot.linger()
        assert not stopping.done()
        hold.release.set()
        await stopping

    assert service.closed
    assert pilot.state() == "in_flight"
    assert pilot.posts() == 0


async def test_stop_held_before_open_waits_for_the_thread(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    await pilot.linked()
    await pilot.unload()

    with held_before(monkeypatch, "PRAGMA quick_check") as hold:
        setup = asyncio.ensure_future(
            pilot.hass.config_entries.async_setup(pilot.entry.entry_id)
        )
        await pilot.reach(hold.reached.is_set)
        service = pilot.service
        stopping = asyncio.ensure_future(pilot.hass.async_stop())
        await pilot.linger()
        assert not stopping.done()
        assert not service.closed
        hold.release.set()
        await stopping
        await asyncio.gather(setup, return_exceptions=True)

    assert service.closed
    assert pilot.reads() == 0


async def test_remove_during_sqlite_commit(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    await pilot.linked()

    with held_commit(monkeypatch, INSERT_PLAY) as hold:
        pilot.accept(qualified("occ"))
        await pilot.reach(hold.reached.is_set)
        removal = pilot.hass.async_create_task(
            pilot.hass.config_entries.async_remove(pilot.entry.entry_id)
        )
        await pilot.linger()
        assert not removal.done()
        assert pilot.path.exists()
        hold.release.set()
        await removal

    await pilot.quiet()
    assert not pilot.path.exists()
    assert not pilot.path.with_name(pilot.path.name + "-journal").exists()


async def test_remove_held_before_open_leaves_no_file(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    await pilot.linked()
    await pilot.unload()

    with held_before(monkeypatch, "PRAGMA quick_check") as hold:
        setup = pilot.hass.async_create_task(
            pilot.hass.config_entries.async_setup(pilot.entry.entry_id)
        )
        await pilot.reach(hold.reached.is_set)
        removal = pilot.hass.async_create_task(
            pilot.hass.config_entries.async_remove(pilot.entry.entry_id)
        )
        await pilot.linger()
        assert not removal.done()
        assert pilot.path.exists()
        hold.release.set()
        await setup
        await removal

    await pilot.quiet()
    assert not pilot.path.exists()


async def test_removal_after_a_failed_unload_stops_the_service_first(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    await pilot.linked()
    service = pilot.service
    with monkeypatch.context() as patch:
        patch.setattr(
            pilot.hass.config_entries,
            "async_unload_platforms",
            AsyncMock(return_value=False),
        )
        assert not await pilot.hass.config_entries.async_unload(pilot.entry.entry_id)
    assert not service.closed

    await pilot.hass.config_entries.async_remove(pilot.entry.entry_id)
    await pilot.quiet()

    assert service.closed
    assert not pilot.path.exists()


async def test_removal_with_a_cancelled_stop_keeps_the_file(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    await pilot.linked()
    service = pilot.service

    with held_commit(monkeypatch, INSERT_PLAY) as hold:
        pilot.accept(qualified("occ"))
        await pilot.reach(hold.reached.is_set)
        removal = pilot.hass.async_create_task(
            async_remove_ledger(pilot.hass, pilot.entry.entry_id)
        )
        await pilot.linger()
        assert not removal.done()
        removal.cancel()
        await asyncio.gather(removal, return_exceptions=True)
        assert removal.cancelled()
        assert pilot.path.exists()
        hold.release.set()
        await service.async_stop()

    assert service.closed
    assert pilot.path.exists()


async def test_removing_a_never_loaded_entry_deletes_nothing_else(
    pilot: Pilot,
) -> None:
    await pilot.linked()
    await pilot.unload()
    keeper = pilot.path.with_name("unrelated.sqlite3")
    keeper.write_bytes(b"kept")

    await pilot.hass.config_entries.async_remove(pilot.entry.entry_id)
    await pilot.quiet()

    assert not pilot.path.exists()
    assert keeper.read_bytes() == b"kept"


# Bounds and clock


async def test_queue_at_capacity(pilot: Pilot) -> None:
    await pilot.linked()
    await pilot.pause()
    fill_plays(pilot.path, MAX_ACTIVE_PLAYS)
    await pilot.enable()

    await pilot.accept_now("over", start=2000, key=KEY_B)
    pilot.service.async_on_occurrence_change(MarkerDelta(markers_set=(hint(),)))
    await pilot.hass.async_block_till_done()

    assert pilot.state("over") is None
    assert pilot.service.status.status is not DeliveryStatus.STORAGE_ERROR
    assert len(pilot.rows("SELECT lineage FROM checkpoints")) == 1


async def test_clock_jump_back_and_forward(
    pilot: Pilot, freezer: FrozenDateTimeFactory
) -> None:
    await pilot.linked()
    await pilot.accept_now()
    pilot.server.queue(RECENT, history())
    pilot.server.queue(SCROBBLE, accepted())
    await pilot.past_grace()
    await pilot.delivered()
    calls = pilot.reads() + pilot.posts()

    freezer.move_to(T0 + timedelta(seconds=60))
    await pilot.accept_now("second", start=1, key=KEY_B)
    await pilot.quiet()
    assert pilot.service.status.status is DeliveryStatus.RETRYING
    assert pilot.reads() + pilot.posts() == calls

    freezer.move_to(T0 + timedelta(seconds=5000))
    pilot.server.queue(RECENT, history())
    pilot.server.queue(SCROBBLE, accepted(STARTED + 1))
    await pilot.delivered("second")


# Producers


async def test_marker_of_b_lands_after_the_enqueue_of_a(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    await pilot.linked()
    lineage = pilot.entry.data[CONF_ENTITY_REGISTRY_IDS][0]
    service = pilot.service
    service.async_on_occurrence_change(MarkerDelta(markers_set=(hint(lineage, "a"),)))
    await pilot.hass.async_block_till_done()

    with held_commit(monkeypatch, INSERT_PLAY) as hold:
        pilot.accept(qualified("a"))
        service.async_on_occurrence_change(
            MarkerDelta(markers_set=(hint(lineage, "b", key=KEY_B),))
        )
        await pilot.reach(hold.reached.is_set)
        await pilot.linger()
        # B waits its turn, however the executor would have ordered it.
        assert pilot.rows("SELECT occurrence_id FROM checkpoints") == [("a",)]
        hold.release.set()
        await pilot.hass.async_block_till_done()

    assert pilot.rows("SELECT occurrence_id FROM checkpoints") == [("b",)]
    assert pilot.rows("SELECT COUNT(*) FROM plays WHERE id = 'a'") == [(1,)]
    assert pilot.state("b") is None


async def test_repeating_transitions_keep_a_paused_speakers_marker(
    pilot: Pilot,
) -> None:
    await pilot.linked()
    service = pilot.service
    paused, moving = "registry-paused", "registry-moving"
    service.async_on_occurrence_change(MarkerDelta(markers_set=(hint(paused, "p"),)))
    for number in range(20):
        service.async_on_occurrence_change(
            MarkerDelta(markers_set=(hint(moving, f"m-{number}"),))
        )
        await pilot.accept_now(f"m-{number}", start=number * 400 + 1)
    await pilot.hass.async_block_till_done()

    assert sorted(pilot.rows("SELECT lineage, occurrence_id FROM checkpoints")) == [
        (moving, "m-19"),
        (paused, "p"),
    ]


@pytest.mark.parametrize("damage", ["storage", "closed_executor", "cancelled_wrapper"])
async def test_a_failing_producer_never_raises_into_the_runtime(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    await pilot.linked()
    service = pilot.service

    def refuse(*_args: Any) -> Any:
        raise RuntimeError("Executor shutdown has been called")

    def cancelled(*_args: Any) -> Any:
        future = pilot.hass.loop.create_future()
        future.cancel()
        return future

    with monkeypatch.context() as patch:
        if damage == "storage":
            fail_on(patch, INSERT_PLAY, message="canary-sqlite-text")
        else:
            patch.setattr(
                service._outbox,
                "_run_blocking",
                refuse if damage == "closed_executor" else cancelled,
            )
        pilot.accept(qualified("a"))
        service.async_on_occurrence_change(
            MarkerDelta(markers_set=(hint("registry-a", "later"),))
        )
        await pilot.hass.async_block_till_done()

    snapshot = service.status
    assert snapshot.status is DeliveryStatus.STORAGE_ERROR
    assert snapshot.error_code in {"storage_error", "executor_gone", "outcome_unknown"}
    assert "canary" not in repr(snapshot)
    # The chain stopped there: a later write must not overtake a lost one.
    assert pilot.rows("SELECT COUNT(*) FROM checkpoints") == [(0,)]


# Supervisor scenarios: real streams, real SQLite, a real service


def sample(
    title: str | None, moment: float, position: float = 0.0, status: str = "playing"
) -> PlaybackSample:
    attributes: dict[str, Any] = (
        {}
        if title is None
        else {
            "source": "Spotify Connect",
            "media_artist": "Synthetic artist",
            "media_title": title,
            "media_duration": 200,
            "media_position": position,
            "media_position_updated_at": at(moment),
        }
    )
    return normalize_state(status, attributes, at(moment))


def solo(*names: str) -> Topology:
    return Topology(
        tuple(CanonicalGroup(name, frozenset({name})) for name in names), frozenset()
    )


class Producer:
    """StreamSet wired to the service the way the runtime wires it."""

    def __init__(self, pilot: Pilot, lineages: dict[str, str]) -> None:
        ids = count()
        self.pilot = pilot
        self.streams = StreamSet(lambda: f"occurrence-{next(ids)}-{id(self)}")
        self.streams.set_lineages(lineages)
        hints, guarded = pilot.service.resume_state
        self.streams.seed_resume_hints(hints, guarded)
        self.earned = 0

    def publish(self, plays: tuple[Any, ...]) -> None:
        self.earned += len(plays)
        for play in plays:
            self.pilot.accept(play)
        delta = self.streams.take_marker_changes()
        if delta:
            self.pilot.service.async_on_occurrence_change(delta)

    def apply(
        self, topology: Topology, samples: dict[str, PlaybackSample], moment: float
    ) -> None:
        self.publish(self.streams.apply(topology, samples, moment))

    def run(self, moment: float) -> None:
        for step in (15, 30, 45, 60, 75, 90, 100, 130):
            self.publish(self.streams.tick(moment + step))


async def test_pause_over_a_day_and_restart_earns_nothing_new(
    pilot: Pilot, freezer: FrozenDateTimeFactory
) -> None:
    await pilot.linked()
    lineage = pilot.entry.data[CONF_ENTITY_REGISTRY_IDS][0]
    producer = Producer(pilot, {"a": lineage})
    freezer.move_to(at(5))
    producer.apply(solo("a"), {"a": sample("X", 5)}, 5)
    producer.run(5)
    assert producer.earned == 1
    await pilot.hass.async_block_till_done()
    pilot.server.queue(RECENT, history())
    pilot.server.queue(SCROBBLE, accepted(STARTED + 5))
    await pilot.past_grace()
    await pilot.delivered("occurrence-0-" + str(id(producer)))
    producer.apply(solo("a"), {"a": sample("X", 140, 135, "paused")}, 140)
    await pilot.hass.async_block_till_done()

    await pilot.unload()
    moment = 26 * 3600.0
    freezer.move_to(at(moment))
    await pilot.setup()
    resumed = Producer(pilot, {"a": lineage})
    resumed.apply(solo("a"), {"a": sample("X", moment, 135)}, moment)
    resumed.run(moment)
    await pilot.past_grace()

    assert resumed.earned == 0
    assert pilot.posts() == 1
    assert pilot.reads() == 1


async def test_offline_speaker_keeps_its_identity_across_a_restart(
    pilot: Pilot, freezer: FrozenDateTimeFactory
) -> None:
    await pilot.linked()
    a_lineage = pilot.entry.data[CONF_ENTITY_REGISTRY_IDS][0]
    b_lineage = "registry-b"
    pilot.hass.config_entries.async_update_entry(
        pilot.entry,
        data={
            **pilot.entry.data,
            CONF_ENTITY_REGISTRY_IDS: [a_lineage, b_lineage],
        },
    )
    lineages = {"a": a_lineage, "b": b_lineage}
    producer = Producer(pilot, lineages)
    freezer.move_to(at(5))
    producer.apply(solo("a"), {"a": sample("X", 5)}, 5)
    producer.run(5)
    await pilot.hass.async_block_till_done()
    assert producer.earned == 1
    known = pilot.rows("SELECT id FROM plays")[0][0]
    await pilot.unload()

    freezer.move_to(at(7200))
    await pilot.setup()
    restarted = Producer(pilot, lineages)
    only_b = Topology((CanonicalGroup("b", frozenset({"b"})),), frozenset({"a"}))
    offline = sample(None, 7200, status="unavailable")
    restarted.apply(only_b, {"a": offline, "b": sample("X", 7200)}, 7200)
    restarted.apply(only_b, {"a": offline, "b": sample("Y", 7210)}, 7210)
    restarted.apply(
        solo("a", "b"), {"a": sample("X", 7220, 100), "b": sample("Y", 7220, 10)}, 7220
    )
    restarted.run(7220)
    await pilot.hass.async_block_till_done()

    assert any(
        item.occurrence_id == known for item in restarted.streams.active_occurrences
    )
    assert pilot.rows(
        "SELECT occurrence_id FROM checkpoints WHERE lineage = ?", a_lineage
    ) == [(known,)]


async def test_two_detours_then_restart_stay_unsendable(
    pilot: Pilot, freezer: FrozenDateTimeFactory
) -> None:
    await pilot.linked()
    lineage = pilot.entry.data[CONF_ENTITY_REGISTRY_IDS][0]
    lineages = {"a": lineage}
    producer = Producer(pilot, lineages)
    freezer.move_to(at(5))
    producer.apply(solo("a"), {"a": sample("X", 5)}, 5)
    producer.run(5)
    assert producer.earned == 1
    producer.apply(solo("a"), {"a": sample("X", 140, 100, "paused")}, 140)
    for number in (1, 2):
        leader = f"foreign-{number}"
        moment = 140.0 + number * 10
        producer.apply(
            Topology((CanonicalGroup(leader, frozenset({"a", leader})),), frozenset()),
            {leader: sample(f"Z-{number}", moment, 50)},
            moment,
        )
    producer.apply(solo("a"), {"a": sample("X", 170, 100, "paused")}, 170)
    await pilot.hass.async_block_till_done()
    assert pilot.rows("SELECT lineage FROM checkpoints WHERE slot = 'guard'") == [
        (lineage,)
    ]

    await pilot.unload()
    freezer.move_to(at(180))
    await pilot.setup()
    assert pilot.service.resume_state[1] == frozenset({lineage})
    restarted = Producer(pilot, lineages)
    restarted.apply(solo("a"), {"a": sample("X", 180, 100)}, 180)
    restarted.run(180)
    await pilot.hass.async_block_till_done()

    assert restarted.earned == 0
    assert pilot.rows("SELECT COUNT(*) FROM plays") == [(1,)]


# Status


async def test_a_read_hold_shows_retrying_not_grace(pilot: Pilot) -> None:
    await pilot.linked()
    await pilot.accept_now()
    pilot.server.queue(RECENT, Reply(status=500))

    # No attempt yet: this is the grace delay.
    await pilot.reach(
        lambda: pilot.service.status.status is DeliveryStatus.WAITING_GRACE
    )

    await pilot.past_grace()
    await pilot.reach(lambda: pilot.service.status.status is DeliveryStatus.RETRYING)

    assert pilot.reads() == 1
    assert pilot.state() == "held"
    assert pilot.posts() == 0


@pytest.mark.parametrize("trigger", ["clear", "stop"])
async def test_no_sender_can_start_during_a_full_reap(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch, trigger: str
) -> None:
    """Both callers close the fence first, so no second reference can appear."""
    await pilot.linked()
    await pilot.quiet()
    service = pilot.service
    reap = service._async_reap_worker
    reaped = []

    async def spy() -> None:
        service.async_on_current_playback(
            (CurrentPlayback("new", META, T0, duration_seconds=200.0, speaker="x"),)
        )
        assert service._notices is None
        await reap()
        reaped.append(trigger)

    with monkeypatch.context() as patch:
        patch.setattr(service, "_async_reap_worker", spy)
        if trigger == "clear":
            await pilot.option("clear_queue")
        else:
            await pilot.unload()

    assert reaped == [trigger]
