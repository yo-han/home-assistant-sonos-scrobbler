"""Now Playing through real states, the real service, SQLite and a socket server."""

import asyncio
import logging
from collections.abc import Iterable
from itertools import pairwise
from typing import Any

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.personal_sonos_scrobbler.const import (
    CONF_DELIVERY_ENABLED,
    CONF_ENTITY_REGISTRY_IDS,
    CONF_HISTORICAL_DELIVERY,
    CONF_NOW_PLAYING,
    CONF_NOW_PLAYING_EPOCH,
    CONF_SCROBBLING,
    CONF_SCROBBLING_SINCE,
)
from custom_components.personal_sonos_scrobbler.controls import Feature
from custom_components.personal_sonos_scrobbler.delivery import (
    BACKOFF_BASE_SECONDS,
    MIN_CALL_SPACING_SECONDS,
    DeliveryCore,
    DeliveryStatus,
    Fence,
    NoticeSender,
    delivery_service,
)
from custom_components.personal_sonos_scrobbler.outbox import OutboxError
from custom_components.personal_sonos_scrobbler.streams import TOPOLOGY_SETTLE_SECONDS
from tests.delivery_harness import (
    RECENT,
    SCROBBLE,
    Pilot,
    accepted,
    held_commit,
    history,
)
from tests.helpers import choose, link_account, start_options
from tests.lastfm_server import (
    API_KEY,
    SESSION_KEY,
    Reply,
    json_reply,
    now_playing_reply,
)
from tests.outbox_support import fail_on, qualified, write

NOW = "track.updateNowPlaying"
SPACING = MIN_CALL_SPACING_SECONDS
SETTLE = TOPOLOGY_SETTLE_SECONDS + 0.1
HEARTBEAT = 15
QUALIFYING_HEARTBEATS = 7  # a 200 s track needs 100 s of credit
RATE_LIMITED = "rate_limited_until"
STORE_META = "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)"


class Room:
    """A Sonos entity that publishes what a real speaker writes."""

    def __init__(self, pilot: Pilot, player: er.RegistryEntry) -> None:
        self._pilot = pilot
        self.player = player

    def play(
        self,
        title: str = "Title",
        state: str = "playing",
        *,
        members: Iterable[Room] | None = None,
        position: float = 0.0,
        **attributes: Any,
    ) -> None:
        self._pilot.hass.states.async_set(
            self.player.entity_id,
            state,
            {
                "source": "Spotify Connect",
                "media_artist": "Synthetic Artist",
                "media_album_name": "Synthetic Album",
                "media_title": title,
                "media_duration": 200,
                "media_position": position,
                "media_position_updated_at": dt_util.utcnow(),
                "group_members": [
                    room.player.entity_id for room in (members or (self,))
                ],
                **attributes,
            },
        )


@pytest.fixture
def room(pilot: Pilot, sonos_player: er.RegistryEntry) -> Room:
    return Room(pilot, sonos_player)


@pytest.fixture
def hall(pilot: Pilot, entity_registry: er.EntityRegistry) -> Room:
    """Add a second selected speaker, before the pilot starts."""
    player = entity_registry.async_get_or_create(
        "media_player", "sonos", "RINCON_2", suggested_object_id="hall"
    )
    pilot.hass.config_entries.async_update_entry(
        pilot.entry,
        data={
            **pilot.entry.data,
            CONF_ENTITY_REGISTRY_IDS: [
                *pilot.entry.data[CONF_ENTITY_REGISTRY_IDS],
                player.id,
            ],
        },
    )
    return Room(pilot, player)


def titles(pilot: Pilot) -> list[str]:
    return [call.form["track"] for call in pilot.server.calls(NOW)]


async def sent(pilot: Pilot, *expected: str) -> None:
    """Reach this list of notices, without moving the clock, and stay there."""
    await pilot.reach(lambda: len(titles(pilot)) >= len(expected))
    await pilot.quiet()
    assert titles(pilot) == list(expected)


async def past_spacing(pilot: Pilot) -> None:
    await pilot.advance(SPACING)
    await pilot.quiet()


def no_trace(pilot: Pilot) -> None:
    """Assert that a notice left nothing in history or the ledger."""
    assert pilot.posts() == 0
    assert pilot.reads() == 0
    assert pilot.server.history == []
    assert pilot.rows("SELECT COUNT(*) FROM plays") == [(0,)]


def pending_timers(pilot: Pilot) -> int:
    """Count Home Assistant timers; the loopback connection keeps its own."""
    timers = pilot.hass.loop._scheduled  # type: ignore[attr-defined]
    return sum(
        not timer.cancelled() and "_run_async_call_action" in repr(timer)
        for timer in timers
    )


# Sending


async def test_a_playing_track_is_announced_before_any_listening_credit(
    pilot: Pilot, room: Room
) -> None:
    await pilot.linked()

    room.play()

    await sent(pilot, "Title")
    form = pilot.server.calls(NOW)[0].form
    assert form["artist"] == "Synthetic Artist"
    assert form["album"] == "Synthetic Album"
    assert form["duration"] == "200"
    assert form["api_key"] == API_KEY
    assert form["sk"] == SESSION_KEY
    assert "timestamp" not in form
    no_trace(pilot)


async def test_a_notice_changes_nothing_about_the_later_scrobble(
    pilot: Pilot, room: Room
) -> None:
    await pilot.linked()
    await pilot.advance(1)
    room.play()
    await sent(pilot, "Title")
    no_trace(pilot)

    for _ in range(QUALIFYING_HEARTBEATS):
        await pilot.advance(HEARTBEAT)
    await pilot.quiet()
    assert pilot.rows("SELECT state FROM plays") == [("pending",)]
    pilot.server.queue(RECENT, history())
    pilot.server.queue(SCROBBLE, accepted())
    await pilot.past_grace()
    await pilot.reach(lambda: pilot.posts() == 1, tick=1)

    assert titles(pilot) == ["Title"]


async def test_ticks_volume_and_position_updates_stay_one_notice(
    pilot: Pilot, room: Room
) -> None:
    await pilot.linked()
    room.play()
    await sent(pilot, "Title")

    for step in range(1, 5):
        await pilot.advance(HEARTBEAT)
        room.play(position=HEARTBEAT * step, volume_level=step / 10)
    await pilot.quiet()

    assert titles(pilot) == ["Title"]


async def test_track_change_and_resume_are_announced(pilot: Pilot, room: Room) -> None:
    await pilot.linked()
    room.play("One")
    await sent(pilot, "One")

    await pilot.advance(SPACING)
    room.play("Two")
    await sent(pilot, "One", "Two")

    await pilot.advance(HEARTBEAT)
    room.play("Two", "paused", position=15)
    await pilot.quiet()
    await pilot.advance(HEARTBEAT)
    assert titles(pilot) == ["One", "Two"]

    room.play("Two", position=15)
    await sent(pilot, "One", "Two", "Two")


@pytest.mark.parametrize(
    "attributes",
    [
        {"source": "Other"},
        {"source": "TV"},
        {"source": "Radio", "media_content_id": "x-rincon-mp3radio://synthetic"},
        {"media_artist": None},
        {"media_title": None},
        {"state": "paused"},
        {"state": "idle"},
    ],
    ids=["unknown", "tv", "radio", "no-artist", "no-title", "paused", "idle"],
)
async def test_nothing_is_announced_without_proven_spotify_playback(
    pilot: Pilot, room: Room, attributes: dict[str, Any]
) -> None:
    await pilot.linked()
    settings = dict(attributes)

    room.play(state=settings.pop("state", "playing"), **settings)
    await pilot.quiet()
    await pilot.advance(HEARTBEAT)
    await pilot.quiet()

    assert titles(pilot) == []


async def test_a_notice_carries_no_listening_data_into_the_logs(
    pilot: Pilot, room: Room, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    await pilot.linked()

    room.play("Unmistakable Canary Title")
    await sent(pilot, "Unmistakable Canary Title")

    assert "Canary" not in caplog.text
    assert "Synthetic Artist" not in caplog.text


# Gates


async def test_linked_but_disabled_sends_nothing(pilot: Pilot, room: Room) -> None:
    await pilot.setup()
    await link_account(pilot.hass, pilot.entry)
    calls = len(pilot.server.requests)

    room.play()
    await pilot.quiet()
    await pilot.advance(HEARTBEAT)
    await pilot.quiet()

    assert len(pilot.server.requests) == calls


async def test_enabling_announces_what_is_playing_but_backfills_no_scrobble(
    pilot: Pilot, room: Room
) -> None:
    await pilot.setup()
    await link_account(pilot.hass, pilot.entry)
    room.play(position=20)
    await pilot.quiet()

    await pilot.enable()

    await sent(pilot, "Title")
    for _ in range(QUALIFYING_HEARTBEATS):
        await pilot.advance(HEARTBEAT)
    await pilot.quiet()
    assert titles(pilot) == ["Title"]
    no_trace(pilot)


async def test_pausing_and_enabling_announces_the_same_track_again(
    pilot: Pilot, room: Room
) -> None:
    await pilot.linked()
    room.play()
    await sent(pilot, "Title")

    await pilot.pause()
    await past_spacing(pilot)
    assert titles(pilot) == ["Title"]

    await pilot.enable()
    await sent(pilot, "Title", "Title")


async def test_a_reload_announces_the_current_track_once(
    pilot: Pilot, room: Room
) -> None:
    await pilot.linked()
    room.play()
    await sent(pilot, "Title")

    await pilot.advance(SPACING)
    await pilot.restart()

    await sent(pilot, "Title", "Title")


async def test_a_storage_failure_sends_nothing(pilot: Pilot, room: Room) -> None:
    await pilot.linked()
    pilot.service._async_fail("storage_error")

    room.play()
    await pilot.quiet()

    assert titles(pilot) == []


@pytest.mark.parametrize("wake_during_notice", [False, True])
async def test_an_unreadable_gate_holds_the_notice_and_leaves_delivery_alone(
    pilot: Pilot,
    room: Room,
    monkeypatch: pytest.MonkeyPatch,
    wake_during_notice: bool,
) -> None:
    """Fault only the notice's real SQL reads, while the worker stays enabled."""
    original_notice_gate = NoticeSender._async_gates_open
    original_worker_gate = DeliveryCore._async_check_gates
    failed_notice = asyncio.Event()
    worker_checks = 0
    worker_checks_before_fault = 0

    async def count_worker_gate(core: DeliveryCore, fence: Fence) -> None:
        nonlocal worker_checks
        await original_worker_gate(core, fence)
        worker_checks += 1

    async def unreadable_notice_gate(sender: NoticeSender, fence: Fence) -> bool:
        nonlocal worker_checks_before_fault
        worker_checks_before_fault = worker_checks
        # The real sender holds admission here. The global fault must not cover
        # an unrelated worker step, which is required to stop on the same error.
        with monkeypatch.context() as fault:
            fail_on(fault, "SELECT value FROM meta")
            if wake_during_notice:
                pilot.service._wake.set()
            try:
                return await original_notice_gate(sender, fence)
            except OutboxError:
                failed_notice.set()
                raise

    monkeypatch.setattr(DeliveryCore, "_async_check_gates", count_worker_gate)
    monkeypatch.setattr(NoticeSender, "_async_gates_open", unreadable_notice_gate)
    await pilot.linked()
    await pilot.reach(lambda: worker_checks > 0)

    room.play()
    await pilot.reach(failed_notice.is_set)
    notice = pilot.service._notices
    assert notice is not None
    await pilot.reach(notice.done)
    notice.result()

    # Also force a real gate read after the notice failed. A healthy status
    # before that read would not prove that the fault was isolated.
    pilot.service._wake.set()
    await pilot.reach(lambda: worker_checks > worker_checks_before_fault)
    assert titles(pilot) == []
    assert pilot.service.status.status is DeliveryStatus.READY
    assert pilot.service.status.error_code is None
    assert pilot.service._worker is not None
    assert not pilot.service._worker.done()
    no_trace(pilot)


async def test_a_shared_unreadable_gate_stops_the_scrobbling_worker(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same real SQL failure must still fail closed if the worker reads it."""
    entered = asyncio.Event()
    release = asyncio.Event()
    original_gate = DeliveryCore._async_check_gates

    async def held_gate(core: DeliveryCore, fence: Fence) -> None:
        entered.set()
        await release.wait()
        await original_gate(core, fence)

    monkeypatch.setattr(DeliveryCore, "_async_check_gates", held_gate)
    await pilot.linked()
    try:
        await pilot.reach(entered.is_set)
        with monkeypatch.context() as fault:
            fail_on(fault, "SELECT value FROM meta")
            release.set()
            await pilot.reach(
                lambda: pilot.service.status.status is DeliveryStatus.STORAGE_ERROR
            )
        assert pilot.service.status.error_code == "storage_error"
        assert titles(pilot) == []
        no_trace(pilot)
    finally:
        release.set()


async def test_a_stored_cooldown_stops_a_notice_without_a_later_send(
    pilot: Pilot, room: Room
) -> None:
    await pilot.linked()
    until = int(dt_util.utcnow().timestamp()) + 60
    write(pilot.path, STORE_META, RATE_LIMITED, str(until))

    room.play()
    await pilot.quiet()
    await pilot.advance(90)
    await pilot.quiet()
    assert titles(pilot) == []

    room.play("Next")
    await sent(pilot, "Next")


async def test_a_notice_rate_limit_stops_every_request_and_is_not_retried(
    pilot: Pilot, room: Room
) -> None:
    await pilot.linked()
    await pilot.accept_now()
    pilot.server.queue(NOW, json_reply({"error": 29, "message": "Provider text"}))

    room.play()
    await sent(pilot, "Title")
    stored = pilot.rows("SELECT value FROM meta WHERE key = ?", RATE_LIMITED)[0][0]
    assert int(stored) >= dt_util.utcnow().timestamp() + 0.8 * BACKOFF_BASE_SECONDS - 1

    await pilot.advance(SPACING)
    room.play("Blocked")
    await pilot.quiet()
    await pilot.past_grace(30)

    assert titles(pilot) == ["Title"]
    assert pilot.reads() == 0


async def test_a_notice_rate_limit_cannot_shorten_a_longer_cooldown(
    pilot: Pilot, room: Room
) -> None:
    await pilot.linked()
    far = str(int(dt_util.utcnow().timestamp()) + 3000)
    pilot.server.hold(NOW)
    pilot.server.queue(NOW, json_reply({"error": 29, "message": "Provider text"}))
    room.play()
    await pilot.reach(pilot.server.held.is_set)

    write(pilot.path, STORE_META, RATE_LIMITED, far)
    pilot.server.release(NOW)
    await pilot.quiet()

    assert pilot.rows("SELECT value FROM meta WHERE key = ?", RATE_LIMITED) == [(far,)]


async def test_a_notice_auth_error_stops_requests_on_this_link(
    pilot: Pilot, room: Room
) -> None:
    await pilot.linked()
    await pilot.quiet()
    assert pilot.service.status.status is DeliveryStatus.READY
    pilot.server.queue(NOW, json_reply({"error": 9, "message": "Provider text"}))

    room.play()
    await sent(pilot, "Title")
    await pilot.reach(
        lambda: pilot.service.status.status is DeliveryStatus.AUTH_REQUIRED, within=500
    )

    room.play("Blocked")
    await pilot.quiet()
    await pilot.advance(HEARTBEAT)
    await pilot.quiet()

    assert titles(pilot) == ["Title"]


@pytest.mark.parametrize(
    "reply",
    [
        Reply(drop=True),
        Reply(status=500),
        Reply(body=b"{ not json"),
        json_reply({"error": 11, "message": "Provider text"}),
        json_reply({"error": 16, "message": "Provider text"}),
        now_playing_reply("1"),
    ],
    ids=["dropped", "http-500", "malformed", "error-11", "error-16", "ignored"],
)
async def test_a_failed_notice_is_never_repeated(
    pilot: Pilot, room: Room, reply: Reply
) -> None:
    await pilot.linked()
    pilot.server.queue(NOW, reply)

    room.play()
    await sent(pilot, "Title")
    for step in range(1, 5):
        await pilot.advance(HEARTBEAT)
        room.play(position=HEARTBEAT * step)
    await pilot.quiet()

    assert titles(pilot) == ["Title"]
    no_trace(pilot)
    assert pilot.service.status.status is DeliveryStatus.READY


# Latest only


async def test_only_the_newest_track_leaves_after_a_paced_wait(
    pilot: Pilot, room: Room
) -> None:
    await pilot.linked()
    room.play("One")
    await sent(pilot, "One")

    room.play("Stale")
    await pilot.quiet()
    room.play("Newest")
    await pilot.quiet()
    assert titles(pilot) == ["One"]

    await pilot.advance(SPACING)
    await sent(pilot, "One", "Newest")


async def test_a_pause_during_the_wait_voids_the_notice(
    pilot: Pilot, room: Room
) -> None:
    await pilot.linked()
    room.play("One")
    await sent(pilot, "One")

    room.play("Two")
    await pilot.quiet()
    room.play("Two", "paused")
    await pilot.quiet()
    await pilot.advance(2 * SPACING)
    await pilot.quiet()

    assert titles(pilot) == ["One"]


async def test_notices_go_one_at_a_time(pilot: Pilot, room: Room) -> None:
    await pilot.linked()
    pilot.server.hold(NOW)
    room.play("One")
    await pilot.reach(pilot.server.held.is_set)

    room.play("Two")
    room.play("Three")
    await pilot.quiet()
    await pilot.advance(5 * SPACING)
    await pilot.quiet()
    assert titles(pilot) == ["One"]

    pilot.server.release(NOW)
    await sent(pilot, "One", "Three")


async def test_notices_and_delivery_share_one_request_budget(
    pilot: Pilot, room: Room
) -> None:
    await pilot.linked()
    await pilot.accept_now()
    pilot.server.queue(RECENT, history())
    pilot.server.queue(SCROBBLE, accepted())
    room.play("One")
    await sent(pilot, "One")

    await pilot.past_grace(690)
    for step in range(60):
        if step % 3 == 0:
            room.play(f"Track {step}")
        await pilot.advance(SPACING)
        await pilot.linger()
        if pilot.posts():
            break
    await pilot.quiet()

    moments = sorted(
        call.received_at
        for call in pilot.server.requests
        if call.form["method"] in {NOW, RECENT, SCROBBLE}
    )
    assert pilot.posts() == 1
    assert len(titles(pilot)) > 2
    assert all(later - earlier >= SPACING for earlier, later in pairwise(moments))


async def test_a_slow_claim_and_a_notice_keep_their_distance(
    pilot: Pilot, room: Room, monkeypatch: pytest.MonkeyPatch
) -> None:
    await pilot.linked()
    await pilot.accept_now()
    pilot.server.queue(RECENT, history())
    pilot.server.queue(SCROBBLE, accepted())

    with held_commit(monkeypatch, "UPDATE plays SET state = 'in_flight'") as hold:
        await pilot.advance(800)
        await pilot.reach(hold.reached.is_set, tick=1)
        await pilot.advance(2 * SPACING)
        room.play()
        await pilot.linger()
        hold.release.set()
        await pilot.reach(lambda: pilot.posts() == 1)
        await pilot.quiet()
        assert titles(pilot) == []
        await pilot.advance(SPACING)
        await sent(pilot, "Title")

    calls = pilot.server.calls(SCROBBLE) + pilot.server.calls(NOW)
    assert calls[1].received_at - calls[0].received_at >= SPACING


# Lifecycle


async def test_pausing_during_the_wait_sends_nothing(pilot: Pilot, room: Room) -> None:
    await pilot.linked()
    room.play("One")
    await sent(pilot, "One")

    room.play("Two")
    await pilot.quiet()
    await pilot.pause()
    await pilot.advance(2 * SPACING)
    await pilot.quiet()

    assert titles(pilot) == ["One"]


async def test_unlinking_during_the_wait_sends_nothing(
    pilot: Pilot, room: Room
) -> None:
    await pilot.linked()
    room.play("One")
    await sent(pilot, "One")

    room.play("Two")
    await pilot.quiet()
    form = await choose(
        pilot.hass, await start_options(pilot.hass, pilot.entry), "disconnect"
    )
    await pilot.hass.config_entries.options.async_configure(form["flow_id"], {})
    await pilot.advance(2 * SPACING)
    await pilot.quiet()
    room.play("Three")
    await pilot.quiet()

    assert titles(pilot) == ["One"]


async def test_unloading_during_the_wait_sends_nothing_and_leaves_no_timer(
    pilot: Pilot, room: Room
) -> None:
    await pilot.linked()
    room.play("One")
    await sent(pilot, "One")

    room.play("Two")
    await pilot.quiet()
    assert pending_timers(pilot) > 0
    await pilot.unload()

    assert pending_timers(pilot) == 0
    await pilot.advance(2 * SPACING)
    await pilot.quiet()
    assert titles(pilot) == ["One"]


async def test_a_notice_on_the_wire_does_not_hold_up_a_stop(
    pilot: Pilot, room: Room
) -> None:
    await pilot.linked()
    pilot.server.hold(NOW)
    room.play()
    await pilot.reach(pilot.server.held.is_set)

    pilot.hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await pilot.reach(
        lambda: delivery_service(pilot.hass, pilot.entry.entry_id) is None
    )
    pilot.server.release(NOW)

    assert titles(pilot) == ["Title"]


# Rooms and groups


async def test_the_newest_room_wins_and_a_static_room_cannot_flip_it_back(
    pilot: Pilot, room: Room, hall: Room
) -> None:
    await pilot.linked()
    room.play("Kitchen")
    await sent(pilot, "Kitchen")

    await pilot.advance(SPACING)
    hall.play("Hall")
    await sent(pilot, "Kitchen", "Hall")

    await pilot.advance(SPACING)
    room.play("Kitchen", position=3, volume_level=0.3)
    await pilot.advance(HEARTBEAT)
    await pilot.quiet()

    assert titles(pilot) == ["Kitchen", "Hall"]


async def test_one_group_is_one_notice(pilot: Pilot, room: Room, hall: Room) -> None:
    await pilot.linked()
    members = (room, hall)

    room.play(members=members)
    hall.play(members=members)
    await pilot.quiet()
    await pilot.advance(SETTLE)

    await sent(pilot, "Title")


async def test_a_selected_follower_announces_its_unselected_coordinator(
    pilot: Pilot, room: Room, entity_registry: er.EntityRegistry
) -> None:
    coordinator = Room(
        pilot,
        entity_registry.async_get_or_create(
            "media_player", "sonos", "RINCON_3", suggested_object_id="den"
        ),
    )
    await pilot.linked()
    await pilot.quiet()
    members = (coordinator, room)

    coordinator.play("Leader", members=members)
    room.play("Stale follower text", members=members)
    await pilot.quiet()
    assert titles(pilot) == []

    await pilot.advance(SETTLE)
    await sent(pilot, "Leader")


async def test_regrouping_a_playing_track_is_not_a_new_notice(
    pilot: Pilot, room: Room, hall: Room
) -> None:
    await pilot.linked()
    hall.play(state="idle", media_title=None, media_artist=None)
    await pilot.quiet()
    await pilot.advance(SETTLE)
    room.play()
    await sent(pilot, "Title")

    members = (room, hall)
    room.play(members=members)
    hall.play(members=members)
    await pilot.quiet()
    await pilot.advance(SETTLE)
    await pilot.quiet()

    assert titles(pilot) == ["Title"]


async def test_a_track_change_during_a_regroup_waits_for_the_settled_group(
    pilot: Pilot, room: Room, hall: Room
) -> None:
    await pilot.linked()
    hall.play(state="idle", media_title=None, media_artist=None)
    await pilot.quiet()
    await pilot.advance(SETTLE)
    room.play("One")
    await sent(pilot, "One")

    members = (room, hall)
    room.play("Two", members=members)
    hall.play("Two", members=members)
    await pilot.quiet()
    assert titles(pilot) == ["One"]

    await pilot.advance(SETTLE)
    await sent(pilot, "One", "Two")


# Now Playing only


@pytest.fixture
def only_now_playing(pilot: Pilot) -> None:
    """Leave scrobbling off: Spotify owns the history."""
    pilot.scrobbling = False


def as_0_4(pilot: Pilot, historical: object = False) -> None:
    """Put the entry back in what 0.4 stored, with a chosen old mode value."""
    data = {
        key: value
        for key, value in pilot.entry.data.items()
        if key
        not in (
            CONF_NOW_PLAYING,
            CONF_NOW_PLAYING_EPOCH,
            CONF_SCROBBLING,
            CONF_SCROBBLING_SINCE,
        )
    }
    pilot.hass.config_entries.async_update_entry(
        pilot.entry,
        data={
            **data,
            CONF_DELIVERY_ENABLED: True,
            **({} if historical is None else {CONF_HISTORICAL_DELIVERY: historical}),
        },
        minor_version=1,
    )
    write(pilot.path, "DELETE FROM meta WHERE key = 'now_playing_epoch'")


def add_stuck_rows(pilot: Pilot) -> None:
    """Leave one pending, one uncertain and one in-flight play behind, all overdue."""
    write(pilot.path, "UPDATE plays SET state = 'uncertain' WHERE id = 'late'")
    write(pilot.path, "UPDATE plays SET state = 'in_flight' WHERE id = 'lost'")


async def queue_as_historical(pilot: Pilot) -> None:
    """Reach the ledger a 0.3 install leaves: rows of every waiting kind."""
    await pilot.linked()
    for occurrence_id, start in (("occ", 0), ("late", 1), ("lost", 2)):
        await pilot.accept_now(occurrence_id, start=start, key=bytes([start]) * 16)
    await pilot.quiet()


@pytest.mark.parametrize("historical", [False, True, None, "true", 1])
async def test_upgrading_silences_every_history_row_across_restarts(
    pilot: Pilot, room: Room, historical: object
) -> None:
    await queue_as_historical(pilot)
    await pilot.unload()
    add_stuck_rows(pilot)
    before = pilot.rows("SELECT id, state FROM plays ORDER BY id")
    as_0_4(pilot, historical)

    await pilot.setup()
    assert (pilot.is_on(Feature.NOW_PLAYING), pilot.is_on(Feature.SCROBBLING)) == (
        True,
        False,
    )
    await pilot.past_grace(1200)
    await pilot.restart()
    await pilot.past_grace(1200)
    room.play()
    await pilot.reach(lambda: len(titles(pilot)) == 1)
    await pilot.past_grace(1200)

    assert pilot.reads() == 0
    assert pilot.posts() == 0
    assert titles(pilot) == ["Title"]
    assert [row[0] for row in pilot.rows("SELECT id FROM plays ORDER BY id")] == [
        row[0] for row in before
    ]


@pytest.mark.usefixtures("only_now_playing")
async def test_now_playing_only_queues_no_new_play(pilot: Pilot, room: Room) -> None:
    await pilot.linked()
    await pilot.advance(1)

    room.play()
    await sent(pilot, "Title")
    for _ in range(QUALIFYING_HEARTBEATS):
        await pilot.advance(HEARTBEAT)
    await pilot.quiet()
    pilot.accept(qualified("direct", start=2))
    await pilot.past_grace(1200)

    no_trace(pilot)
    assert titles(pilot) == ["Title"]


@pytest.mark.usefixtures("only_now_playing")
async def test_the_switch_controls_now_playing_and_never_selects_history(
    pilot: Pilot, room: Room
) -> None:
    await pilot.linked()
    room.play()
    await sent(pilot, "Title")

    await pilot.pause()
    await pilot.advance(SPACING)
    room.play("Paused")
    await pilot.quiet()
    assert titles(pilot) == ["Title"]

    await pilot.enable()
    await sent(pilot, "Title", "Paused")
    assert not pilot.is_on(Feature.SCROBBLING)


async def test_switching_scrobbling_off_stops_a_step_in_progress(
    pilot: Pilot, room: Room
) -> None:
    await pilot.linked()
    await pilot.accept_now()
    pilot.server.queue(RECENT, history())
    pilot.server.queue(SCROBBLE, accepted())
    pilot.server.hold(RECENT)
    await pilot.past_grace()
    await pilot.reach(pilot.server.held.is_set, tick=1)

    await pilot.press(Feature.SCROBBLING, "turn_off")
    await pilot.reach(lambda: pilot.service.status.status is DeliveryStatus.READY)
    pilot.server.release(RECENT)
    await pilot.past_grace(1200)

    assert pilot.reads() == 1
    assert pilot.posts() == 0
    assert pilot.state() == "pending"
    room.play()
    await sent(pilot, "Title")


async def test_uncertain_rows_do_not_paint_the_status_in_now_playing_only(
    pilot: Pilot,
) -> None:
    await queue_as_historical(pilot)
    await pilot.unload()
    add_stuck_rows(pilot)
    as_0_4(pilot)

    await pilot.setup()
    await pilot.quiet()

    status = pilot.service.status
    assert status.status is DeliveryStatus.READY
    assert status.uncertain_count == 2


@pytest.mark.usefixtures("only_now_playing")
async def test_a_notice_auth_error_shows_without_any_worker(
    pilot: Pilot, room: Room
) -> None:
    await pilot.linked()
    pilot.server.queue(NOW, json_reply({"error": 9, "message": "Provider text"}))

    room.play()
    await sent(pilot, "Title")
    await pilot.reach(
        lambda: pilot.service.status.status is DeliveryStatus.AUTH_REQUIRED, within=500
    )

    await pilot.restart()
    room.play("Blocked")
    await pilot.reach(
        lambda: pilot.service.status.status is DeliveryStatus.AUTH_REQUIRED, within=500
    )
    assert titles(pilot) == ["Title"]


@pytest.mark.parametrize("gone", ["unavailable", "missing"])
async def test_an_older_room_back_from_an_outage_takes_nothing_back(
    pilot: Pilot, room: Room, hall: Room, gone: str
) -> None:
    await pilot.linked()
    room.play("Kitchen")
    await sent(pilot, "Kitchen")
    await pilot.advance(SPACING)
    hall.play("Hall")
    await sent(pilot, "Kitchen", "Hall")
    await pilot.advance(SPACING)

    if gone == "unavailable":
        pilot.hass.states.async_set(room.player.entity_id, "unavailable", {})
    else:
        pilot.hass.states.async_remove(room.player.entity_id)
    await pilot.quiet()
    await pilot.advance(SETTLE)
    room.play("Kitchen", position=20)
    await pilot.quiet()
    await pilot.advance(SETTLE)
    await pilot.advance(HEARTBEAT)
    await pilot.quiet()

    assert titles(pilot) == ["Kitchen", "Hall"]
    assert pilot.entry.runtime_data.current_track is not None
    assert pilot.entry.runtime_data.current_track.speaker == hall.player.entity_id


async def test_a_new_track_after_an_outage_is_a_new_arrival(
    pilot: Pilot, room: Room, hall: Room
) -> None:
    await pilot.linked()
    room.play("Kitchen")
    await sent(pilot, "Kitchen")
    await pilot.advance(SPACING)
    hall.play("Hall")
    await sent(pilot, "Kitchen", "Hall")
    await pilot.advance(SPACING)

    pilot.hass.states.async_set(room.player.entity_id, "unavailable", {})
    await pilot.quiet()
    await pilot.advance(SETTLE)
    room.play("Next")
    await pilot.quiet()
    await pilot.advance(SETTLE)
    await sent(pilot, "Kitchen", "Hall", "Next")

    assert pilot.entry.runtime_data.current_track is not None
    assert pilot.entry.runtime_data.current_track.speaker == room.player.entity_id


# Status after a restart


async def stopped_with(pilot: Pilot, key: str, value: str) -> None:
    await pilot.linked()
    await pilot.unload()
    write(pilot.path, STORE_META, key, value)
    await pilot.setup()
    await pilot.quiet()


@pytest.mark.usefixtures("only_now_playing")
async def test_a_stored_auth_stop_shows_before_any_notice(
    pilot: Pilot, room: Room
) -> None:
    await stopped_with(pilot, "auth_required", "1")

    status = pilot.service.status
    assert status.status is DeliveryStatus.AUTH_REQUIRED
    assert status.error_code == "auth_required"

    room.play()
    await pilot.quiet()
    assert titles(pilot) == []


@pytest.mark.usefixtures("only_now_playing")
async def test_a_stored_cooldown_shows_until_it_ends(pilot: Pilot, room: Room) -> None:
    until = int(dt_util.utcnow().timestamp()) + 60
    await stopped_with(pilot, RATE_LIMITED, str(until))

    status = pilot.service.status
    assert status.status is DeliveryStatus.RETRYING
    assert status.error_code == "rate_limited"
    room.play()
    await pilot.quiet()
    assert titles(pilot) == []

    await pilot.advance(61)
    await pilot.quiet()
    assert pilot.service.status.status is DeliveryStatus.READY
    assert pilot.service.status.error_code is None


@pytest.mark.usefixtures("only_now_playing")
async def test_an_unreadable_stored_gate_shows_and_sends_nothing(
    pilot: Pilot, room: Room
) -> None:
    await stopped_with(pilot, RATE_LIMITED, "not-a-time")

    status = pilot.service.status
    assert status.status is DeliveryStatus.STORAGE_ERROR
    assert status.error_code == "invalid_data"

    room.play()
    await pilot.quiet()
    assert titles(pilot) == []


@pytest.mark.usefixtures("only_now_playing")
async def test_a_clock_behind_the_stored_high_water_shows(pilot: Pilot) -> None:
    ahead = int(dt_util.utcnow().timestamp()) + 60
    await stopped_with(pilot, "clock_high_water", str(ahead))

    status = pilot.service.status
    assert status.status is DeliveryStatus.RETRYING
    assert status.error_code == "clock_regressed"


@pytest.mark.usefixtures("only_now_playing")
async def test_a_stop_during_a_gate_read_leaves_no_timer(
    pilot: Pilot, room: Room, monkeypatch: pytest.MonkeyPatch
) -> None:
    await pilot.linked()
    await pilot.quiet()
    until = int(dt_util.utcnow().timestamp()) + 60
    write(pilot.path, STORE_META, RATE_LIMITED, str(until))
    service = pilot.service
    outbox = service._outbox
    assert outbox is not None
    read = outbox.async_meta_get
    reached = asyncio.Event()
    release = asyncio.Event()

    async def held_read(key: str) -> str | None:
        if key == RATE_LIMITED:
            reached.set()
            await release.wait()
        return await read(key)

    monkeypatch.setattr(outbox, "async_meta_get", held_read)
    service._async_reconcile()
    await pilot.reach(reached.is_set)
    unloading = pilot.hass.async_create_task(
        pilot.hass.config_entries.async_unload(pilot.entry.entry_id)
    )
    await pilot.reach(lambda: service._stopping)

    release.set()
    assert await unloading
    await pilot.quiet()

    assert service.closed
    assert service._cancel_gate_wake is None
    timers = pilot.hass.loop._scheduled  # type: ignore[attr-defined]
    assert not [timer for timer in timers if "delivery gates" in repr(timer)]
    await pilot.advance(3700)
    await pilot.quiet()
    assert titles(pilot) == []


@pytest.mark.usefixtures("only_now_playing")
async def test_a_clear_shows_a_cooldown_it_finds_until_that_ends(
    pilot: Pilot,
) -> None:
    await pilot.linked()
    await pilot.quiet()
    until = int(dt_util.utcnow().timestamp()) + 60
    write(pilot.path, STORE_META, RATE_LIMITED, str(until))
    assert pilot.service.status.status is DeliveryStatus.READY

    result = await pilot.option("clear_queue")

    assert result["reason"] == "queue_cleared"
    status = pilot.service.status
    assert status.status is DeliveryStatus.RETRYING
    assert status.error_code == "rate_limited"
    await pilot.advance(61)
    await pilot.quiet()
    assert pilot.service.status.status is DeliveryStatus.READY


@pytest.mark.usefixtures("only_now_playing")
async def test_a_clear_keeps_showing_a_live_auth_stop(pilot: Pilot, room: Room) -> None:
    await pilot.linked()
    pilot.server.queue(NOW, json_reply({"error": 9, "message": "Provider text"}))
    room.play()
    await sent(pilot, "Title")
    await pilot.reach(
        lambda: pilot.service.status.status is DeliveryStatus.AUTH_REQUIRED, within=500
    )

    result = await pilot.option("clear_queue")

    assert result["reason"] == "queue_cleared"
    assert pilot.service.status.status is DeliveryStatus.AUTH_REQUIRED
    assert pilot.service.status.error_code == "auth_required"


# The sender's reference


async def test_a_sender_started_during_a_reap_keeps_its_reference(
    pilot: Pilot, room: Room
) -> None:
    await pilot.linked()
    room.play("First")
    await sent(pilot, "First")
    await pilot.reach(lambda: pilot.service._notices is not None)
    service = pilot.service
    finished = service._notices
    assert finished is not None
    await pilot.reach(finished.done)

    # The reconcile stops in its wait for the finished sender, and a track arrives.
    service._async_reconcile()
    room.play("Second")
    sender = service._notices
    assert sender is not None
    assert sender is not finished
    await pilot.linger()

    assert service._notices is sender
    await pilot.advance(SPACING)
    await sent(pilot, "First", "Second")
