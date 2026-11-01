"""Both switches through real HA services, SQLite and a socket server."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.personal_sonos_scrobbler.const import (
    CONF_DELIVERY_EPOCH,
    CONF_LASTFM,
    CONF_LASTFM_LINK_REVISION,
    CONF_NOW_PLAYING_EPOCH,
    DOMAIN,
)
from custom_components.personal_sonos_scrobbler.controls import Feature
from custom_components.personal_sonos_scrobbler.delivery import (
    MIN_CALL_SPACING_SECONDS,
    DeliveryStatus,
)
from custom_components.personal_sonos_scrobbler.outbox import (
    ACTIVE_RETENTION_SECONDS,
    OutboxError,
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
)
from tests.helpers import link_account
from tests.outbox_support import write

NOW = "track.updateNowPlaying"
NOW_PLAYING_KEY = "now_playing_enabled"
SCROBBLING_KEY = "scrobbling_enabled"
SINCE_KEY = "scrobbling_since"
EPOCH_UPDATE = "UPDATE meta SET value = ? WHERE key = 'epoch'"
NOTICE_EPOCH_UPDATE = "INSERT OR REPLACE INTO meta"
NOW_PLAYING_EN = "Now Playing to Last.fm"
SCROBBLING_EN = "Scrobbling to Last.fm"
COMPONENT = Path(__file__).parents[1] / "custom_components/personal_sonos_scrobbler"
HEARTBEAT = 15
QUALIFYING_HEARTBEATS = 7  # a 200 s track needs 100 s of credit


def switch_id(pilot: Pilot, feature: str) -> str:
    """Find a switch by its stable unique id."""
    entity_id = er.async_get(pilot.hass).async_get_entity_id(
        "switch", DOMAIN, f"{pilot.entry.entry_id}_{feature}"
    )
    assert entity_id is not None, f"no {feature} switch"
    return entity_id


async def press(pilot: Pilot, feature: str, service: str) -> None:
    """Call the real service and wait for its completion."""
    await pilot.hass.services.async_call(
        "switch",
        service,
        {"entity_id": switch_id(pilot, feature)},
        blocking=True,
    )


def shown(pilot: Pilot, feature: str) -> str:
    state = pilot.hass.states.get(switch_id(pilot, feature))
    assert state is not None
    return state.state


def flags(pilot: Pilot) -> tuple[bool, bool]:
    return (pilot.entry.data[NOW_PLAYING_KEY], pilot.entry.data[SCROBBLING_KEY])


async def linked_off(pilot: Pilot) -> None:
    await pilot.setup()
    await link_account(pilot.hass, pilot.entry)
    await pilot.hass.async_block_till_done()


def play(pilot: Pilot, room: er.RegistryEntry, title: str = "Title") -> None:
    pilot.hass.states.async_set(
        room.entity_id,
        "playing",
        {
            "source": "Spotify Connect",
            "media_artist": "Synthetic Artist",
            "media_title": title,
            "media_duration": 200,
            "media_position": 0,
            "media_position_updated_at": dt_util.utcnow(),
            "group_members": [room.entity_id],
        },
    )


@pytest.mark.parametrize(
    ("name", "now_playing", "scrobbling", "sensor"),
    [
        ("strings.json", NOW_PLAYING_EN, SCROBBLING_EN, "Current track"),
        ("translations/en.json", NOW_PLAYING_EN, SCROBBLING_EN, "Current track"),
        (
            "translations/nl.json",
            "Now Playing naar Last.fm",
            "Scrobbelen naar Last.fm",
            "Huidig nummer",
        ),
    ],
)
def test_the_entities_carry_the_requested_names(
    name: str, now_playing: str, scrobbling: str, sensor: str
) -> None:
    entity = json.loads((COMPONENT / name).read_text())["entity"]

    assert entity["switch"]["now_playing"]["name"] == now_playing
    assert entity["switch"]["scrobbling"]["name"] == scrobbling
    assert entity["sensor"]["current_track"]["name"] == sensor


async def test_both_switches_exist_and_start_off(pilot: Pilot) -> None:
    await linked_off(pilot)

    assert shown(pilot, "now_playing") == "off"
    assert shown(pilot, "scrobbling") == "off"
    assert flags(pilot) == (False, False)


@pytest.mark.parametrize(
    ("now_playing", "scrobbling"),
    [(False, False), (True, False), (False, True), (True, True)],
)
async def test_each_combination_sends_only_what_is_on(
    pilot: Pilot,
    sonos_player: er.RegistryEntry,
    now_playing: bool,
    scrobbling: bool,
) -> None:
    await linked_off(pilot)
    pilot.server.queue(RECENT, history())
    pilot.server.queue(SCROBBLE, accepted())
    await pilot.advance(1)
    if now_playing:
        await press(pilot, "now_playing", "turn_on")
    if scrobbling:
        await press(pilot, "scrobbling", "turn_on")
    await pilot.advance(1)

    play(pilot, sonos_player)
    await pilot.quiet()
    if now_playing:
        # The request's deadline runs on the frozen clock moved below.
        await pilot.reach(lambda: len(pilot.server.calls(NOW)) == 1)
        await pilot.quiet()
    for _ in range(QUALIFYING_HEARTBEATS):
        await pilot.advance(HEARTBEAT)
    await pilot.past_grace()
    if scrobbling:
        await pilot.reach(lambda: pilot.posts() == 1, tick=1)

    assert flags(pilot) == (now_playing, scrobbling)
    assert len(pilot.server.calls(NOW)) == int(now_playing)
    assert pilot.posts() == int(scrobbling)
    assert pilot.reads() == int(scrobbling)


async def test_switching_on_without_a_link_fails_and_sends_nothing(
    pilot: Pilot,
) -> None:
    await pilot.setup()

    for feature in ("now_playing", "scrobbling"):
        with pytest.raises(HomeAssistantError):
            await press(pilot, feature, "turn_on")
        assert shown(pilot, feature) == "off"

    assert pilot.server.requests == []
    assert not pilot.path.exists()


async def test_turning_one_on_leaves_the_other_alone(pilot: Pilot) -> None:
    await linked_off(pilot)

    await press(pilot, "now_playing", "turn_on")
    assert flags(pilot) == (True, False)
    await press(pilot, "scrobbling", "turn_on")
    assert flags(pilot) == (True, True)
    await press(pilot, "now_playing", "turn_off")
    assert flags(pilot) == (False, True)
    assert shown(pilot, "scrobbling") == "on"


async def test_a_held_commit_shows_off_until_it_is_durable(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    await linked_off(pilot)
    await press(pilot, "now_playing", "turn_on")
    await press(pilot, "now_playing", "turn_off")

    with held_commit(monkeypatch, EPOCH_UPDATE) as hold:
        turning_on = pilot.hass.async_create_task(press(pilot, "scrobbling", "turn_on"))
        await pilot.reach(hold.reached.is_set)
        assert flags(pilot) == (False, False)
        assert shown(pilot, "scrobbling") == "off"
        hold.release.set()
        await turning_on

    assert flags(pilot) == (False, True)


async def test_future_only_leaves_old_rows_parked_without_requests(
    pilot: Pilot,
) -> None:
    await linked_off(pilot)
    await press(pilot, "scrobbling", "turn_on")
    await pilot.accept_now()
    await press(pilot, "scrobbling", "turn_off")
    await pilot.advance(2000)

    await press(pilot, "scrobbling", "turn_on")
    await pilot.quiet()
    await pilot.past_grace()

    assert pilot.state() == "pending"
    assert (pilot.reads(), pilot.posts()) == (0, 0)


async def test_a_busy_period_never_touches_parked_rows_or_spins(pilot: Pilot) -> None:
    await linked_off(pilot)
    await press(pilot, "scrobbling", "turn_on")
    await pilot.accept_now("old-pending")
    await pilot.accept_now("old-uncertain", start=300, key=b"u" * 16)
    await press(pilot, "scrobbling", "turn_off")
    write(pilot.path, "UPDATE plays SET state = 'uncertain' WHERE id = 'old-uncertain'")
    await pilot.past_grace(2000)
    outbox = pilot.service._outbox
    assert outbox is not None
    polls = 0
    next_ready = outbox.async_next_ready

    async def counted(*args: Any) -> Any:
        nonlocal polls
        polls += 1
        return await next_ready(*args)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(outbox, "async_next_ready", counted)
        await press(pilot, "scrobbling", "turn_on")
        for _ in range(20):
            await pilot.linger()
        await pilot.quiet()

    # One look at an empty period, and then a worker asleep without a timer.
    assert polls == 1
    assert (pilot.reads(), pilot.posts()) == (0, 0)
    assert pilot.service.status.status is DeliveryStatus.READY
    assert pilot.service.status.uncertain_count == 1

    pilot.server.queue(RECENT, history())
    pilot.server.queue(SCROBBLE, accepted(STARTED + 3000))
    await pilot.accept_now("new", start=3000, key=b"n" * 16)
    await pilot.advance(1000)
    await pilot.reach(lambda: pilot.state("new") == "sent", tick=1)
    await pilot.quiet()

    assert (pilot.reads(), pilot.posts()) == (1, 1)
    assert pilot.state("old-pending") == "pending"
    assert pilot.state("old-uncertain") == "uncertain"
    assert pilot.column("title", "old-pending") != ""


async def test_retention_still_expires_a_parked_row(pilot: Pilot) -> None:
    await linked_off(pilot)
    await press(pilot, "scrobbling", "turn_on")
    await pilot.accept_now()
    await press(pilot, "scrobbling", "turn_off")
    await pilot.advance(10)
    await press(pilot, "scrobbling", "turn_on")

    await pilot.advance(ACTIVE_RETENTION_SECONDS + 3600)
    await pilot.quiet()

    assert pilot.state() == "expired"
    assert (pilot.reads(), pilot.posts()) == (0, 0)


async def test_the_track_already_playing_is_never_backfilled(
    pilot: Pilot, sonos_player: er.RegistryEntry
) -> None:
    await linked_off(pilot)
    play(pilot, sonos_player)
    await pilot.quiet()
    await pilot.advance(HEARTBEAT)

    await press(pilot, "scrobbling", "turn_on")
    for _ in range(QUALIFYING_HEARTBEATS):
        await pilot.advance(HEARTBEAT)
    await pilot.past_grace()

    assert pilot.rows("SELECT COUNT(*) FROM plays") == [(0,)]
    assert (pilot.reads(), pilot.posts()) == (0, 0)


async def test_off_then_on_starts_a_new_period(pilot: Pilot) -> None:
    await linked_off(pilot)
    await press(pilot, "scrobbling", "turn_on")
    first = pilot.entry.data[SINCE_KEY]
    await pilot.advance(50)

    await press(pilot, "scrobbling", "turn_off")
    assert pilot.entry.data[SINCE_KEY] is None
    await press(pilot, "scrobbling", "turn_on")

    assert pilot.entry.data[SINCE_KEY] == first + 50


async def test_a_restart_keeps_both_settings_and_the_period(pilot: Pilot) -> None:
    await linked_off(pilot)
    await press(pilot, "now_playing", "turn_on")
    await press(pilot, "scrobbling", "turn_on")
    since = pilot.entry.data[SINCE_KEY]
    await pilot.accept_now()
    pilot.server.queue(RECENT, history())
    pilot.server.queue(SCROBBLE, accepted())

    await pilot.restart()
    assert flags(pilot) == (True, True)
    assert shown(pilot, "now_playing") == "on"
    assert shown(pilot, "scrobbling") == "on"
    assert pilot.entry.data[SINCE_KEY] == since
    await pilot.past_grace()
    await pilot.delivered()

    assert pilot.posts() == 1


async def test_now_playing_off_leaves_a_running_worker_and_its_read_alone(
    pilot: Pilot,
) -> None:
    await linked_off(pilot)
    await press(pilot, "now_playing", "turn_on")
    await press(pilot, "scrobbling", "turn_on")
    await pilot.accept_now()
    pilot.server.queue(RECENT, history())
    pilot.server.queue(SCROBBLE, accepted())
    pilot.server.hold(RECENT)
    await pilot.past_grace()
    await pilot.reach(pilot.server.held.is_set, tick=1)
    worker = pilot.service._worker

    await press(pilot, "now_playing", "turn_off")
    await press(pilot, "now_playing", "turn_on")
    await press(pilot, "now_playing", "turn_off")

    assert pilot.service._worker is worker
    pilot.server.release(RECENT)
    await pilot.delivered()
    assert (pilot.reads(), pilot.posts()) == (1, 1)


async def test_now_playing_off_costs_no_listening_credit(
    pilot: Pilot, sonos_player: er.RegistryEntry
) -> None:
    await linked_off(pilot)
    await press(pilot, "now_playing", "turn_on")
    await press(pilot, "scrobbling", "turn_on")
    await pilot.advance(1)
    play(pilot, sonos_player)
    await pilot.reach(lambda: len(pilot.server.calls(NOW)) == 1)
    await pilot.quiet()
    for _ in range(3):
        await pilot.advance(HEARTBEAT)

    await press(pilot, "now_playing", "turn_off")
    for _ in range(QUALIFYING_HEARTBEATS - 3):
        await pilot.advance(HEARTBEAT)
    await pilot.quiet()

    assert pilot.rows("SELECT COUNT(*) FROM plays") == [(1,)]
    assert pilot.rows("SELECT started_at FROM plays") == [(STARTED + 1,)]


async def test_scrobbling_toggles_announce_nothing_again(
    pilot: Pilot, sonos_player: er.RegistryEntry
) -> None:
    await linked_off(pilot)
    await press(pilot, "now_playing", "turn_on")
    play(pilot, sonos_player)
    await pilot.reach(lambda: len(pilot.server.calls(NOW)) == 1)
    await pilot.advance(MIN_CALL_SPACING_SECONDS)

    await press(pilot, "scrobbling", "turn_on")
    await press(pilot, "scrobbling", "turn_off")
    await pilot.quiet()

    assert len(pilot.server.calls(NOW)) == 1


async def test_turning_now_playing_back_on_announces_the_track_once(
    pilot: Pilot, sonos_player: er.RegistryEntry
) -> None:
    await linked_off(pilot)
    await press(pilot, "now_playing", "turn_on")
    play(pilot, sonos_player)
    await pilot.reach(lambda: len(pilot.server.calls(NOW)) == 1)
    await pilot.advance(MIN_CALL_SPACING_SECONDS)

    await press(pilot, "now_playing", "turn_off")
    await press(pilot, "now_playing", "turn_on")
    await pilot.reach(lambda: len(pilot.server.calls(NOW)) == 2)
    await pilot.quiet()

    assert len(pilot.server.calls(NOW)) == 2


async def test_off_while_a_notice_is_on_the_wire_sends_nothing_more(
    pilot: Pilot, sonos_player: er.RegistryEntry
) -> None:
    await linked_off(pilot)
    await press(pilot, "now_playing", "turn_on")
    pilot.server.hold(NOW)
    play(pilot, sonos_player)
    await pilot.reach(pilot.server.held.is_set)

    await press(pilot, "now_playing", "turn_off")

    assert pilot.service._notices is None
    play(pilot, sonos_player, "Later")
    await pilot.advance(MIN_CALL_SPACING_SECONDS)
    pilot.server.release(NOW)
    await pilot.quiet()
    assert len(pilot.server.calls(NOW)) == 1


async def test_off_during_a_post_keeps_the_write_uncertain(pilot: Pilot) -> None:
    await linked_off(pilot)
    await press(pilot, "scrobbling", "turn_on")
    await pilot.accept_now()
    pilot.server.queue(RECENT, history())
    pilot.server.queue(SCROBBLE, accepted())
    pilot.server.hold(SCROBBLE)
    await pilot.past_grace()
    await pilot.reach(pilot.server.held.is_set, tick=1)

    await press(pilot, "scrobbling", "turn_off")

    assert pilot.service._worker is None
    assert pilot.state() == "uncertain"
    pilot.server.release(SCROBBLE)
    await press(pilot, "scrobbling", "turn_on")
    await pilot.past_grace(1000)
    assert pilot.state() == "uncertain"
    assert (pilot.reads(), pilot.posts()) == (1, 1)


async def test_a_double_click_turns_on_once(pilot: Pilot) -> None:
    await linked_off(pilot)

    await asyncio.gather(
        press(pilot, "scrobbling", "turn_on"), press(pilot, "scrobbling", "turn_on")
    )
    await pilot.quiet()

    assert flags(pilot) == (False, True)


async def test_off_beats_a_held_enable_and_nothing_activates_late(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    await linked_off(pilot)
    await press(pilot, "now_playing", "turn_on")

    with held_commit(monkeypatch, EPOCH_UPDATE) as hold:
        turning_on = pilot.hass.async_create_task(press(pilot, "scrobbling", "turn_on"))
        await pilot.reach(hold.reached.is_set)
        turning_off = pilot.hass.async_create_task(
            press(pilot, "scrobbling", "turn_off")
        )
        await pilot.linger()
        hold.release.set()
        with pytest.raises(HomeAssistantError):
            await turning_on
        await turning_off
    await pilot.quiet()

    assert flags(pilot) == (True, False)
    assert shown(pilot, "scrobbling") == "off"
    assert pilot.service._worker is None


async def test_on_after_a_held_on_and_off_ends_on(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    await linked_off(pilot)
    await press(pilot, "now_playing", "turn_on")

    with held_commit(monkeypatch, EPOCH_UPDATE) as hold:
        first = pilot.hass.async_create_task(press(pilot, "scrobbling", "turn_on"))
        await pilot.reach(hold.reached.is_set)
        off = pilot.hass.async_create_task(press(pilot, "scrobbling", "turn_off"))
        await pilot.linger()
        last = pilot.hass.async_create_task(press(pilot, "scrobbling", "turn_on"))
        await pilot.linger()
        hold.release.set()
        with pytest.raises(HomeAssistantError):
            await first
        await off
        await last

    assert flags(pilot) == (True, True)


async def test_unlinking_during_a_held_enable_activates_nothing(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    await linked_off(pilot)
    await press(pilot, "now_playing", "turn_on")
    await press(pilot, "now_playing", "turn_off")

    with held_commit(monkeypatch, EPOCH_UPDATE) as hold:
        turning_on = pilot.hass.async_create_task(press(pilot, "scrobbling", "turn_on"))
        await pilot.reach(hold.reached.is_set)
        unlinking = pilot.hass.async_create_task(pilot.option("disconnect"))
        await pilot.linger()
        hold.release.set()
        with pytest.raises(HomeAssistantError):
            await turning_on
        await unlinking

    assert CONF_LASTFM not in pilot.entry.data
    assert flags(pilot) == (False, False)


async def test_a_link_change_fences_both_and_relinking_enables_neither(
    pilot: Pilot,
) -> None:
    await linked_off(pilot)
    await press(pilot, "now_playing", "turn_on")
    await press(pilot, "scrobbling", "turn_on")
    assert shown(pilot, "scrobbling") == "on"

    await link_account(pilot.hass, pilot.entry, "replace")

    assert flags(pilot) == (False, False)
    assert pilot.entry.data[SINCE_KEY] is None
    assert shown(pilot, "now_playing") == "off"
    assert shown(pilot, "scrobbling") == "off"

    await press(pilot, "now_playing", "turn_on")
    assert (await pilot.option("disconnect"))["reason"] == "disconnected"
    await link_account(pilot.hass, pilot.entry)

    assert flags(pilot) == (False, False)


async def test_off_works_after_a_storage_failure(pilot: Pilot) -> None:
    await linked_off(pilot)
    await press(pilot, "now_playing", "turn_on")
    await press(pilot, "scrobbling", "turn_on")
    pilot.service._async_fail("storage_corrupt")

    await press(pilot, "now_playing", "turn_off")
    await press(pilot, "scrobbling", "turn_off")

    assert flags(pilot) == (False, False)
    with pytest.raises(HomeAssistantError):
        await press(pilot, "scrobbling", "turn_on")
    assert flags(pilot) == (False, False)


async def test_a_now_playing_off_a_crash_lost_stays_off(pilot: Pilot) -> None:
    await linked_off(pilot)
    await press(pilot, "now_playing", "turn_on")
    await press(pilot, "scrobbling", "turn_on")
    epoch = pilot.entry.data[CONF_DELIVERY_EPOCH]
    await pilot.unload()
    # The ledger saw the off; the delayed config save never got to write it.
    write(pilot.path, "UPDATE meta SET value = '50' WHERE key = 'now_playing_epoch'")

    await pilot.setup()

    assert flags(pilot) == (False, True)
    assert pilot.entry.data[CONF_NOW_PLAYING_EPOCH] == 51
    assert pilot.entry.data[CONF_DELIVERY_EPOCH] == epoch
    assert pilot.rows("SELECT value FROM meta WHERE key = 'now_playing_epoch'") == [
        ("51",)
    ]


async def test_a_damaged_now_playing_epoch_is_a_storage_error(pilot: Pilot) -> None:
    await linked_off(pilot)
    await press(pilot, "now_playing", "turn_on")
    await pilot.unload()
    write(pilot.path, "UPDATE meta SET value = 'x' WHERE key = 'now_playing_epoch'")

    await pilot.setup()

    assert pilot.service.status.error_code == "invalid_data"


async def test_a_clock_regression_cannot_readmit_a_row_of_the_previous_period(
    pilot: Pilot, sonos_player: er.RegistryEntry
) -> None:
    await linked_off(pilot)
    await press(pilot, "scrobbling", "turn_on")
    await pilot.advance(1)
    play(pilot, sonos_player)
    for _ in range(QUALIFYING_HEARTBEATS):
        await pilot.advance(HEARTBEAT)
    await pilot.quiet()
    started = pilot.rows("SELECT started_at FROM plays")
    assert started == [(STARTED + 1,)]

    await press(pilot, "scrobbling", "turn_off")
    await pilot.advance(-120)
    await press(pilot, "scrobbling", "turn_on")
    pilot.server.queue(RECENT, history())
    pilot.server.queue(SCROBBLE, accepted())
    assert pilot.entry.data[SINCE_KEY] == STARTED + 2
    await pilot.past_grace(1100)
    for _ in range(4):
        await pilot.advance(1)
        await pilot.linger()
    await pilot.quiet()

    assert (pilot.reads(), pilot.posts()) == (0, 0)
    assert pilot.rows("SELECT started_at, state FROM plays") == [
        (STARTED + 1, "pending")
    ]
    assert pilot.service.status.pending_count == 1


async def test_a_listen_after_a_regressed_activation_still_scrobbles_once(
    pilot: Pilot,
) -> None:
    await linked_off(pilot)
    await press(pilot, "scrobbling", "turn_on")
    await pilot.accept_now("old", start=100)
    await press(pilot, "scrobbling", "turn_off")
    await press(pilot, "scrobbling", "turn_on")
    assert pilot.entry.data[SINCE_KEY] == STARTED + 101

    # At the boundary: the first start the new period may hold.
    pilot.server.queue(RECENT, history())
    pilot.server.queue(SCROBBLE, accepted(STARTED + 101))
    await pilot.accept_now("before", start=100.5, key=b"b" * 16)
    await pilot.accept_now("new", start=101, key=b"n" * 16)
    await pilot.past_grace(1000)
    await pilot.delivered("new")

    assert pilot.state("before") is None
    assert pilot.state("old") == "pending"
    assert (pilot.reads(), pilot.posts()) == (1, 1)


@pytest.mark.parametrize(
    ("feature", "statement"),
    [
        ("now_playing", NOTICE_EPOCH_UPDATE),
        ("scrobbling", EPOCH_UPDATE),
    ],
)
async def test_a_final_off_beats_every_on_before_it(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch, feature: str, statement: str
) -> None:
    await linked_off(pilot)
    await press(pilot, feature, "turn_on")
    await press(pilot, feature, "turn_off")

    with held_commit(monkeypatch, statement) as hold:
        first = pilot.hass.async_create_task(press(pilot, feature, "turn_on"))
        await pilot.reach(hold.reached.is_set)
        second = pilot.hass.async_create_task(press(pilot, feature, "turn_on"))
        await pilot.linger()
        off = pilot.hass.async_create_task(press(pilot, feature, "turn_off"))
        await pilot.linger()
        hold.release.set()
        results = await asyncio.gather(first, second, off, return_exceptions=True)
    await pilot.quiet()

    assert [type(result) for result in results] == [
        HomeAssistantError,
        HomeAssistantError,
        type(None),
    ]
    assert flags(pilot) == (False, False)
    assert shown(pilot, feature) == "off"


async def test_the_other_switch_keeps_its_queued_on_through_an_off(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    await linked_off(pilot)
    await press(pilot, "scrobbling", "turn_on")
    await press(pilot, "scrobbling", "turn_off")

    with held_commit(monkeypatch, EPOCH_UPDATE) as hold:
        scrobbling = pilot.hass.async_create_task(press(pilot, "scrobbling", "turn_on"))
        await pilot.reach(hold.reached.is_set)
        now_playing = pilot.hass.async_create_task(
            press(pilot, "now_playing", "turn_on")
        )
        await press(pilot, "now_playing", "turn_off")
        await pilot.linger()
        hold.release.set()
        await scrobbling
        with pytest.raises(HomeAssistantError):
            await now_playing

    assert flags(pilot) == (False, True)


async def test_a_queued_double_click_still_turns_on(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    await linked_off(pilot)
    await press(pilot, "scrobbling", "turn_on")
    await press(pilot, "scrobbling", "turn_off")

    with held_commit(monkeypatch, EPOCH_UPDATE) as hold:
        first = pilot.hass.async_create_task(press(pilot, "scrobbling", "turn_on"))
        await pilot.reach(hold.reached.is_set)
        second = pilot.hass.async_create_task(press(pilot, "scrobbling", "turn_on"))
        await pilot.linger()
        hold.release.set()
        await asyncio.gather(first, second)

    assert flags(pilot) == (False, True)


async def test_a_queued_on_does_not_cross_an_account_change(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    await linked_off(pilot)
    await press(pilot, "scrobbling", "turn_on")
    await press(pilot, "scrobbling", "turn_off")
    revision = pilot.entry.data[CONF_LASTFM_LINK_REVISION]

    with held_commit(monkeypatch, EPOCH_UPDATE) as hold:
        first = pilot.hass.async_create_task(press(pilot, "scrobbling", "turn_on"))
        await pilot.reach(hold.reached.is_set)
        second = pilot.hass.async_create_task(press(pilot, "scrobbling", "turn_on"))
        await pilot.linger()
        replacing = pilot.hass.async_create_task(
            link_account(pilot.hass, pilot.entry, "replace")
        )
        await pilot.reach(
            lambda: pilot.entry.data[CONF_LASTFM_LINK_REVISION] == revision + 1
        )
        hold.release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        await replacing
    await pilot.quiet()

    assert all(isinstance(result, HomeAssistantError) for result in results)
    assert flags(pilot) == (False, False)


async def test_an_off_in_the_gap_after_a_release_still_stops_the_queued_on(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between a release and the wake-up of the next on, the lock reads as free."""
    await linked_off(pilot)
    controls = pilot.service.controls
    entered = asyncio.Event()
    release = asyncio.Event()
    prepare = controls._prepare_ledger
    off: list[asyncio.Task[None]] = []

    def turn_off() -> None:
        off.append(
            pilot.hass.async_create_task(controls.async_turn_off(Feature.SCROBBLING))
        )

    async def failing_once() -> None:
        entered.set()
        await release.wait()
        monkeypatch.setattr(controls, "_prepare_ledger", prepare)
        asyncio.get_running_loop().call_soon(turn_off)
        raise OutboxError("storage_error")

    monkeypatch.setattr(controls, "_prepare_ledger", failing_once)
    first = pilot.hass.async_create_task(controls.async_turn_on(Feature.SCROBBLING))
    await entered.wait()
    second = pilot.hass.async_create_task(controls.async_turn_on(Feature.SCROBBLING))
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    await asyncio.gather(*off)

    assert all(isinstance(result, HomeAssistantError) for result in results)
    assert flags(pilot) == (False, False)


async def test_a_track_started_during_the_period_query_is_not_backfilled(
    pilot: Pilot,
    sonos_player: er.RegistryEntry,
    monkeypatch: pytest.MonkeyPatch,
    freezer: FrozenDateTimeFactory,
) -> None:
    await linked_off(pilot)

    with held_before(monkeypatch, "SELECT MAX(started_at) FROM plays") as hold:
        enabling = pilot.hass.async_create_task(press(pilot, "scrobbling", "turn_on"))
        await pilot.reach(hold.reached.is_set)
        freezer.tick(1)
        play(pilot, sonos_player)
        await pilot.linger()
        assert not enabling.done()
        assert flags(pilot) == (False, False)
        # Only the clock: a marker write may still wait for this same query.
        freezer.tick(1)
        hold.release.set()
        await enabling

    assert flags(pilot) == (False, True)
    assert pilot.entry.data[SINCE_KEY] == STARTED + 2
    for _ in range(QUALIFYING_HEARTBEATS + 1):
        await pilot.advance(HEARTBEAT)
    await pilot.quiet()

    assert pilot.rows("SELECT COUNT(*) FROM plays") == [(0,)]
