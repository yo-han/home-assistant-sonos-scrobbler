"""One worker step against real SQLite and a real socket server."""

import asyncio
import hashlib
import json
import random
import sqlite3
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.personal_sonos_scrobbler import lastfm
from custom_components.personal_sonos_scrobbler.const import (
    CONF_DELIVERY_EPOCH,
    CONF_NOW_PLAYING,
    CONF_OUTBOX_CREATED,
    CONF_SCROBBLING,
)
from custom_components.personal_sonos_scrobbler.controls import Feature
from custom_components.personal_sonos_scrobbler.delivery import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_CAP_SECONDS,
    MAINTENANCE_INTERVAL,
    MIN_CALL_SPACING_SECONDS,
    POST_END_MARGIN_SECONDS,
    SPOTIFY_GRACE_SECONDS,
    DeliveryCore,
    DeliveryStatus,
    Fence,
    RequestPacer,
    StepResult,
    account_hash,
    backoff_seconds,
    grace_deadline,
)
from custom_components.personal_sonos_scrobbler.lastfm import LastFmCredentials
from custom_components.personal_sonos_scrobbler.lastfm_delivery import (
    LastFmDeliveryClient,
)
from custom_components.personal_sonos_scrobbler.outbox import (
    ACTIVE_RETENTION_SECONDS,
    AsyncOutbox,
    DeliveryState,
    OutboxStore,
)
from custom_components.personal_sonos_scrobbler.playback import (
    MarkerDelta,
    QualifiedPlay,
)
from tests.delivery_harness import (
    RECENT,
    SCROBBLE,
    STARTED,
    Pilot,
    accepted,
    held_commit,
    history,
    page,
    row,
)
from tests.helpers import link_account, start_options
from tests.lastfm_server import (
    ACCOUNT_NAME,
    API_KEY,
    SESSION_KEY,
    SHARED_SECRET,
    LastFmServer,
    Reply,
    json_reply,
)
from tests.outbox_support import (
    ACCOUNT,
    EPOCH,
    KEY_A,
    KEY_B,
    META,
    OTHER_ACCOUNT,
    T0,
    add,
    at,
    hint,
    install_connection,
    on_statement,
    qualified,
    query,
    write,
)

READY = 300.0
FIRST_MOMENT = at(READY)
FENCE = Fence(EPOCH, 1, ACCOUNT, 0)


def ignored() -> Reply:
    return json_reply(
        {
            "scrobbles": {
                "@attr": {"accepted": 0, "ignored": 1},
                "scrobble": {
                    "timestamp": str(STARTED),
                    "ignoredMessage": {"code": "1", "#text": "canary-provider-text"},
                },
            }
        }
    )


@dataclass
class Rig:
    """A file, an outbox, a server and a hand-driven clock and fence."""

    hass: HomeAssistant
    server: LastFmServer
    path: Path
    store: OutboxStore
    outbox: AsyncOutbox
    now: datetime = FIRST_MOMENT
    live: Fence | None = FENCE
    fence_checks: int = 0
    fence_fails_at: int | None = None
    clients: list[Fence] = field(default_factory=list)
    waits: list[tuple[datetime, int]] = field(default_factory=list)
    wait_gate: asyncio.Event | None = None
    waiting: asyncio.Event = field(default_factory=asyncio.Event)
    pacer: RequestPacer = field(init=False)

    def __post_init__(self) -> None:
        self.pacer = RequestPacer(lambda: self.now, self.wait_until)

    async def wait_until(self, moment: datetime) -> None:
        """Note what the wait saw; a held wait lets a test act inside it."""
        self.waits.append((moment, len(self.server.requests)))
        if self.wait_gate is not None:
            self.waiting.set()
            await self.wait_gate.wait()
        self.now = max(self.now, moment)

    def client_for(self, fence: Fence) -> LastFmDeliveryClient:
        self.clients.append(fence)
        return LastFmDeliveryClient(
            async_get_clientsession(self.hass),
            LastFmCredentials(api_key=API_KEY, shared_secret=SHARED_SECRET),
            SESSION_KEY,
            ACCOUNT_NAME,
        )

    def is_current(self, fence: Fence) -> bool:
        self.fence_checks += 1
        if self.fence_checks == self.fence_fails_at:
            self.live = None
        return fence == self.live

    def core(self, seed: int = 7) -> DeliveryCore:
        return DeliveryCore(
            self.outbox,
            self.client_for,
            self.is_current,
            lambda: self.now,
            random.Random(seed),
            self.pacer,
        )

    async def restart(self) -> DeliveryCore:
        """Start over from the file alone, as a new process would."""
        await self.outbox.async_close()
        self.store = OutboxStore.open(self.path)
        self.outbox = AsyncOutbox(self.store, self.hass.async_add_executor_job)
        return self.core()

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    def until_due(self, occurrence_id: str = "occ") -> None:
        """Move to the row's next attempt, past the spacing between calls."""
        due = T0 + timedelta(seconds=self.column("not_before", occurrence_id) - STARTED)
        self.now = max(due, self.now + timedelta(seconds=MIN_CALL_SPACING_SECONDS))

    def add(self, occurrence_id: str = "occ", **options: Any) -> None:
        add(self.store, occurrence_id, now=READY, ready=READY, **options)

    def state(self, occurrence_id: str = "occ") -> str:
        return str(self.column("state", occurrence_id))

    def column(self, name: str, occurrence_id: str = "occ") -> Any:
        rows = query(self.path, f"SELECT {name} FROM plays WHERE id = ?", occurrence_id)
        return rows[0][0]

    def sent(self) -> list[dict[str, str]]:
        return [dict(call.form) for call in self.server.calls(SCROBBLE)]

    def reads(self) -> int:
        return len(self.server.calls(RECENT))

    async def until_held(self, step: asyncio.Task[StepResult]) -> None:
        """Wait for the held request, and fail rather than hang if none arrives."""
        held = asyncio.ensure_future(self.server.held.wait())
        await asyncio.wait({step, held}, return_when=asyncio.FIRST_COMPLETED)
        if step.done():
            held.cancel()
            await step
            pytest.fail("the step ended without reaching the held request")


def build_rig(hass: HomeAssistant, server: LastFmServer, path: Path) -> Rig:
    store = OutboxStore.create(path)
    store.set_epoch(EPOCH)
    return Rig(
        hass, server, path, store, AsyncOutbox(store, hass.async_add_executor_job)
    )


@pytest.fixture
async def rig(
    hass: HomeAssistant, lastfm_server: LastFmServer, tmp_path: Path
) -> AsyncIterator[Rig]:
    built = build_rig(hass, lastfm_server, tmp_path / "outbox.sqlite3")
    yield built
    await built.outbox.async_close()


def fail_commit_after(patch: pytest.MonkeyPatch, statement: str) -> None:
    """Lose the COMMIT of the transaction that ran this statement."""

    class Failing(sqlite3.Connection):
        armed = False

        def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
            if sql.startswith(statement):
                self.armed = True
            if sql == "COMMIT" and self.armed:
                raise sqlite3.OperationalError("injected failure")
            return super().execute(sql, *args)

    install_connection(patch, Failing)


FINALIZE = "UPDATE plays SET state = ?"
CLAIM = "UPDATE plays SET state = 'in_flight'"


# Pure helpers


def test_account_hash_ignores_case_and_differs_per_account() -> None:
    assert account_hash("Listener") == account_hash("listener")
    assert account_hash("Listener") != account_hash("other")
    assert len(account_hash("Listener")) == 16


def test_account_hash_has_its_own_domain() -> None:
    plain = hashlib.blake2b(b"listener", digest_size=16).digest()

    assert account_hash("listener") != plain


def test_fence_repr_hides_the_account_hash() -> None:
    assert ACCOUNT.hex() not in repr(FENCE)
    assert str(ACCOUNT) not in repr(FENCE)


def test_grace_uses_later_of_qualification_and_track_end() -> None:
    long_track = QualifiedPlay(
        occurrence_id="long",
        track_key=KEY_A,
        metadata=META,
        started_at=at(0),
        qualified_at=at(240),
        duration_seconds=1000.0,
    )
    short_track = QualifiedPlay(
        occurrence_id="short",
        track_key=KEY_A,
        metadata=META,
        started_at=at(0),
        qualified_at=at(100),
        duration_seconds=200.0,
    )

    assert SPOTIFY_GRACE_SECONDS == 600
    assert POST_END_MARGIN_SECONDS == 60
    assert grace_deadline(long_track) == at(1060)
    assert grace_deadline(short_track) == at(700)


@pytest.mark.parametrize(
    ("attempt", "base"),
    [(1, 60), (2, 120), (3, 240), (6, 1920)],
)
def test_backoff_doubles_within_twenty_percent(attempt: int, base: int) -> None:
    delays = [backoff_seconds(attempt, random.Random(seed)) for seed in range(50)]

    assert min(delays) >= base * 0.8
    assert max(delays) <= base * 1.2
    assert min(delays) != max(delays)


@pytest.mark.parametrize("attempt", [7, 8, 100, 10**6])
def test_backoff_never_passes_the_cap(attempt: int) -> None:
    delays = [backoff_seconds(attempt, random.Random(seed)) for seed in range(50)]

    assert BACKOFF_BASE_SECONDS == 60
    assert BACKOFF_CAP_SECONDS == 3600
    assert max(delays) <= 3600
    assert min(delays) >= 3600 * 0.8


# Preflight


async def test_every_first_dispatch_reads_history(rig: Rig) -> None:
    rig.add()
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, accepted())

    assert await rig.core().async_step(FENCE) is StepResult.WORKED

    methods = [call.form["method"] for call in rig.server.requests]
    assert methods == [RECENT, SCROBBLE]
    read = rig.server.calls(RECENT)[0].form
    assert read["from"] == str(STARTED - 31)
    assert read["to"] == str(STARTED + 200 + 31)
    assert read["user"] == ACCOUNT_NAME
    assert rig.state() == "sent"


async def test_restored_queue_is_preflighted(rig: Rig) -> None:
    rig.add()
    core = await rig.restart()
    rig.server.queue(RECENT, history(row(STARTED)))

    assert await core.async_step(FENCE) is StepResult.WORKED

    assert rig.reads() == 1
    assert rig.sent() == []
    assert rig.state() == "duplicate"


async def test_convincing_match_skips_post(rig: Rig) -> None:
    rig.add()
    rig.server.queue(RECENT, history(row(STARTED + 4, title=META.title.upper())))

    assert await rig.core().async_step(FENCE) is StepResult.WORKED

    assert rig.sent() == []
    assert rig.state() == "duplicate"
    assert rig.column("reason") == "history_match"
    assert rig.column("title") == ""
    assert rig.store.status(rig.now).skipped_total == 1


async def test_a_different_song_in_the_window_is_no_match(rig: Rig) -> None:
    rig.add()
    rig.server.queue(RECENT, history(row(STARTED, title="something else")))
    rig.server.queue(SCROBBLE, accepted())

    await rig.core().async_step(FENCE)

    assert len(rig.sent()) == 1


async def test_unknown_history_holds_without_post(rig: Rig) -> None:
    rig.add()
    rig.server.queue(RECENT, Reply(status=500))
    core = rig.core()

    assert await core.async_step(FENCE) is StepResult.WORKED

    assert rig.sent() == []
    assert rig.state() == "held"
    assert rig.column("reason") == "http_status"
    assert rig.column("read_attempts") == 1
    waited = rig.column("not_before") - int(rig.now.timestamp())
    assert BACKOFF_BASE_SECONDS * 0.8 - 1 <= waited <= BACKOFF_BASE_SECONDS * 1.2

    # Not due yet: nothing happens at all.
    rig.advance(5)
    assert await core.async_step(FENCE) is StepResult.IDLE
    assert rig.reads() == 1

    # Due again: only a complete read lets it through, from the held state.
    rig.advance(waited)
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, accepted())
    assert await core.async_step(FENCE) is StepResult.WORKED
    assert rig.reads() == 2
    assert rig.state() == "sent"


async def test_a_read_that_keeps_failing_backs_off_further(rig: Rig) -> None:
    rig.add()
    core = rig.core()
    waits = []
    for _ in range(4):
        rig.server.queue(RECENT, Reply(status=500))
        await core.async_step(FENCE)
        waits.append(rig.column("not_before") - int(rig.now.timestamp()))
        rig.advance(waits[-1])

    assert rig.column("read_attempts") == 4
    assert waits[3] > waits[0] * 2
    assert rig.sent() == []


# Attempts


async def test_accepted_post_sends_the_original_payload_and_finalizes(
    rig: Rig,
) -> None:
    rig.add()
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, accepted())

    await rig.core().async_step(FENCE)

    form = rig.sent()[0]
    assert form["artist"] == META.artist
    assert form["track"] == META.title
    assert form["album"] == META.album
    assert form["timestamp"] == str(STARTED)
    assert form["duration"] == "200"
    assert rig.state() == "sent"
    assert rig.column("artist") == ""
    assert rig.column("attempts") == 1
    assert rig.store.status(rig.now).sent_total == 1


@pytest.mark.parametrize("code", [11, 16])
async def test_api_11_16_retry_exact_payload_with_new_preflight(
    rig: Rig, code: int
) -> None:
    rig.add()
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, json_reply({"error": code}))
    core = rig.core()

    assert await core.async_step(FENCE) is StepResult.WORKED
    assert rig.state() == "pending"
    assert rig.column("reason") == "temporary_error"
    assert await core.async_step(FENCE) is StepResult.IDLE

    rig.until_due()
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, accepted())
    assert await core.async_step(FENCE) is StepResult.WORKED

    first, second = rig.sent()
    assert first == second
    assert rig.reads() == 2
    assert rig.state() == "sent"
    assert rig.column("attempts") == 2


async def test_retry_that_finds_a_receipt_on_preflight_does_not_post(rig: Rig) -> None:
    rig.add()
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, json_reply({"error": 16}))
    core = rig.core()
    await core.async_step(FENCE)

    rig.until_due()
    rig.server.queue(RECENT, history(row(STARTED + 2)))
    await core.async_step(FENCE)

    assert len(rig.sent()) == 1
    assert rig.state() == "duplicate"


async def test_connection_failure_before_sending_retries(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.add()
    rig.server.queue(RECENT, history())
    client = rig.client_for

    def refusing_after_read(fence: Fence) -> LastFmDeliveryClient:
        built = client(fence)
        scrobble = built.async_scrobble

        async def refused(payload: Any) -> Any:
            monkeypatch.setattr(lastfm, "API_ROOT", "http://127.0.0.1:1/")
            return await scrobble(payload)

        built.async_scrobble = refused  # type: ignore[method-assign]
        return built

    core = DeliveryCore(
        rig.outbox,
        refusing_after_read,
        rig.is_current,
        lambda: rig.now,
        random.Random(1),
        rig.pacer,
    )

    assert await core.async_step(FENCE) is StepResult.WORKED

    assert rig.state() == "pending"
    assert rig.column("reason") == "connect_failed"


async def test_http_200_alone_is_not_delivered(rig: Rig) -> None:
    rig.add()
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, Reply(status=200, body=b"{}"))

    await rig.core().async_step(FENCE)

    assert rig.state() == "uncertain"
    assert rig.column("reason") == "scrobbles_shape"
    assert rig.store.status(rig.now).sent_total == 0


async def test_ignored_is_terminal_with_a_fixed_reason(rig: Rig) -> None:
    rig.add()
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, ignored())

    await rig.core().async_step(FENCE)

    assert rig.state() == "ignored"
    assert rig.column("reason") == "ignored"


async def test_permanent_reject_is_terminal(rig: Rig) -> None:
    rig.add()
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, json_reply({"error": 6}))

    await rig.core().async_step(FENCE)

    assert rig.state() == "rejected"
    assert rig.column("title") == ""


async def test_one_attempt_per_step_oldest_start_first(rig: Rig) -> None:
    rig.add("later", start=1000)
    rig.add("earlier", start=0, key=KEY_B)
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, accepted())

    assert await rig.core().async_step(FENCE) is StepResult.WORKED

    assert len(rig.sent()) == 1
    assert rig.sent()[0]["timestamp"] == str(STARTED)
    assert rig.state("earlier") == "sent"
    assert rig.state("later") == "pending"


async def test_nothing_ready_is_idle_without_a_provider_call(rig: Rig) -> None:
    assert await rig.core().async_step(FENCE) is StepResult.IDLE

    assert rig.server.requests == []
    assert rig.clients == []


async def test_a_stored_payload_it_cannot_send_is_rejected_without_a_post(
    rig: Rig,
) -> None:
    rig.add()
    write(rig.path, "UPDATE plays SET title = ' ' WHERE id = 'occ'")
    rig.server.queue(RECENT, history())

    assert await rig.core().async_step(FENCE) is StepResult.WORKED

    assert rig.sent() == []
    assert rig.state() == "rejected"
    assert rig.column("reason") == "invalid_payload"


@pytest.mark.parametrize("state", ["pending", "held"])
async def test_queued_row_past_its_deadline_expires_without_a_call(
    rig: Rig, state: str
) -> None:
    rig.add()
    if state == "held":
        rig.store.hold("occ", at(READY), "history_absent")
    rig.now = at(READY + ACTIVE_RETENTION_SECONDS)

    assert await rig.core().async_step(FENCE) is StepResult.WORKED

    assert rig.server.requests == []
    assert rig.state() == "expired"
    assert rig.column("reason") == "retention"
    assert rig.column("title") == ""


async def test_queued_row_with_time_for_its_post_still_goes(rig: Rig) -> None:
    rig.add()
    rig.now = at(READY + ACTIVE_RETENTION_SECONDS - 2)
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, accepted())

    await rig.core().async_step(FENCE)

    assert rig.state() == "sent"


async def test_deadline_passing_during_the_claim_never_posts(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.add()
    rig.server.queue(RECENT, history())
    core = rig.core()

    def reach_the_deadline() -> None:
        rig.now = at(READY + ACTIVE_RETENTION_SECONDS)

    with monkeypatch.context() as patch:
        on_statement(patch, CLAIM, reach_the_deadline)
        assert await core.async_step(FENCE) is StepResult.STOPPED

    assert core.last_stop is not None
    assert core.last_stop.code == "deadline_before_send"
    assert rig.sent() == []
    assert rig.state() == "uncertain"
    assert rig.column("reason") == "deadline_before_send"


async def test_deadline_passing_during_the_read_stops_the_post(rig: Rig) -> None:
    rig.add()
    rig.now = at(READY + ACTIVE_RETENTION_SECONDS - 1)
    rig.server.queue(RECENT, history())
    rig.server.hold(RECENT)

    step = asyncio.create_task(rig.core().async_step(FENCE))
    await rig.until_held(step)
    rig.advance(1)
    rig.server.release(RECENT)

    assert await step is StepResult.WORKED
    assert rig.sent() == []
    assert rig.state() == "expired"


# Overlap protection


async def _send_the_repeat_while_the_first_play_is_held(rig: Rig) -> DeliveryCore:
    """Leave "second" sent and "first" due: its window covers that receipt.

    A window reaches 31 s back and the whole track forward, so only a LATER play
    can fall inside it, and that takes an earlier row held back past its repeat.
    """
    rig.add("first", start=0)
    rig.add("second", start=200)
    core = rig.core()
    rig.server.queue(RECENT, Reply(status=500))
    await core.async_step(FENCE)
    assert rig.state("first") == "held"

    rig.advance(MIN_CALL_SPACING_SECONDS)
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, accepted(STARTED + 200))
    await core.async_step(FENCE)
    assert rig.state("second") == "sent"

    rig.until_due("first")
    return core


async def test_own_sent_entry_does_not_suppress_genuine_repeat(rig: Rig) -> None:
    core = await _send_the_repeat_while_the_first_play_is_held(rig)
    rig.server.queue(RECENT, history(row(STARTED + 200)))
    rig.server.queue(SCROBBLE, accepted(STARTED))

    await core.async_step(FENCE)

    assert [form["timestamp"] for form in rig.sent()] == [
        str(STARTED + 200),
        str(STARTED),
    ]
    assert rig.state("first") == "sent"


async def test_exact_current_receipt_is_found_on_a_pending_row(rig: Rig) -> None:
    rig.add()
    rig.server.queue(RECENT, history(row(STARTED)))

    await rig.core().async_step(FENCE)

    assert rig.sent() == []
    assert rig.state() == "duplicate"


async def test_restored_pending_snapshot_with_its_own_receipt_is_not_sent(
    rig: Rig,
) -> None:
    # A backup holds the row as pending, the provider already has it, and a sent
    # copy of the same start sits in the ledger: that copy proves no OTHER play.
    rig.add("restored")
    write(
        rig.path,
        "INSERT INTO plays SELECT 'sent-copy', account_hash, track_key, started_at, "
        "duration, '', '', NULL, 'sent', 'accepted', epoch, 1, 0, created_at, "
        "not_before, deadline, updated_at FROM plays WHERE id = 'restored'",
    )
    core = await rig.restart()
    rig.server.queue(RECENT, history(row(STARTED)))

    await core.async_step(FENCE)

    assert rig.sent() == []
    assert rig.state("restored") == "duplicate"


async def test_adjacent_repeats_with_both_receipts_present_stay_apart(
    rig: Rig,
) -> None:
    rig.add("one", start=0)
    rig.add("two", start=200)
    rig.add("three", start=400)
    core = rig.core()
    rig.server.queue(RECENT, Reply(status=500), Reply(status=500), history())
    rig.server.queue(SCROBBLE, accepted(STARTED + 400))
    for _ in range(3):
        await core.async_step(FENCE)
        rig.advance(MIN_CALL_SPACING_SECONDS)
    assert rig.state("three") == "sent"

    # Later plays go first, so each window holds the receipt of the next one.
    write(rig.path, "UPDATE plays SET not_before = 9999999999 WHERE id = 'one'")
    rig.until_due("two")
    rig.server.queue(RECENT, history(row(STARTED + 400)))
    rig.server.queue(SCROBBLE, accepted(STARTED + 200))
    await core.async_step(FENCE)
    assert rig.state("two") == "sent"

    write(rig.path, "UPDATE plays SET not_before = 0 WHERE id = 'one'")
    rig.advance(MIN_CALL_SPACING_SECONDS)
    rig.server.queue(RECENT, history(row(STARTED + 200)))
    rig.server.queue(SCROBBLE, accepted(STARTED))
    await core.async_step(FENCE)

    assert [rig.state(name) for name in ("one", "two", "three")] == ["sent"] * 3
    assert len(rig.sent()) == 3


async def test_independent_spotify_receipt_suppresses_despite_own_sent_rows(
    rig: Rig,
) -> None:
    core = await _send_the_repeat_while_the_first_play_is_held(rig)
    rig.server.queue(RECENT, history(row(STARTED + 3), row(STARTED + 200)))

    await core.async_step(FENCE)

    assert len(rig.sent()) == 1
    assert rig.state("first") == "duplicate"


# Uncertain rows


async def test_uncertain_never_posts_again_even_after_restart(rig: Rig) -> None:
    rig.add()
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, Reply(drop=True))
    core = rig.core()
    await core.async_step(FENCE)
    assert rig.state() == "uncertain"
    assert rig.server.history, "the write must really have happened"

    for restart in (False, True, False, True, True):
        if restart:
            core = await rig.restart()
        rig.until_due()
        rig.server.queue(RECENT, history())
        assert await core.async_step(FENCE) is StepResult.WORKED
        assert rig.state() == "uncertain"

    assert len(rig.sent()) == 1
    assert rig.column("attempts") == 1
    assert rig.reads() == 6


async def test_uncertain_backs_off_through_reads_only(rig: Rig) -> None:
    rig.add()
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, Reply(drop=True))
    core = rig.core()
    await core.async_step(FENCE)

    rig.server.queue(RECENT, Reply(status=500))
    assert await core.async_step(FENCE) is StepResult.WORKED

    assert rig.state() == "uncertain"
    assert rig.column("reason") == "http_status"
    assert rig.column("read_attempts") == 1
    assert rig.column("not_before") > int(rig.now.timestamp())
    assert await core.async_step(FENCE) is StepResult.IDLE


@pytest.mark.parametrize(
    ("offset", "state", "reason", "sent_total", "skipped_total"),
    [
        (0, "sent", "history_exact", 1, 0),
        (5, "duplicate", "history_match", 0, 1),
    ],
)
async def test_positive_read_finalizes_uncertain(
    rig: Rig,
    offset: int,
    state: str,
    reason: str,
    sent_total: int,
    skipped_total: int,
) -> None:
    rig.add()
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, Reply(drop=True))
    core = rig.core()
    await core.async_step(FENCE)

    rig.advance(2)
    rig.server.queue(RECENT, history(row(STARTED + offset)))
    assert await core.async_step(FENCE) is StepResult.WORKED

    assert rig.state() == state
    assert rig.column("reason") == reason
    assert rig.column("title") == ""
    status = rig.store.status(rig.now)
    assert (status.sent_total, status.skipped_total) == (sent_total, skipped_total)
    assert len(rig.sent()) == 1


async def test_uncertain_ignores_the_receipt_of_a_proven_other_play(rig: Rig) -> None:
    rig.add("first", start=0)
    rig.add("second", start=200)
    core = rig.core()
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, Reply(drop=True))
    await core.async_step(FENCE)
    assert rig.state("first") == "uncertain"

    rig.advance(MIN_CALL_SPACING_SECONDS)
    rig.server.queue(RECENT, history())
    await core.async_step(FENCE)

    rig.advance(MIN_CALL_SPACING_SECONDS)
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, accepted(STARTED + 200))
    await core.async_step(FENCE)
    assert rig.state("second") == "sent"

    rig.until_due("first")
    rig.server.queue(RECENT, history(row(STARTED + 200)))
    await core.async_step(FENCE)

    assert rig.state("first") == "uncertain"
    assert rig.column("reason", "first") == "history_absent"
    assert rig.column("read_attempts", "first") == 2


async def test_absent_read_expires_uncertain(rig: Rig) -> None:
    rig.add()
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, Reply(drop=True))
    core = rig.core()
    await core.async_step(FENCE)

    deadline = int(rig.now.timestamp()) + 30
    write(rig.path, "UPDATE plays SET deadline = ? WHERE id = 'occ'", deadline)
    rig.advance(2)
    rig.server.queue(RECENT, history())
    assert await core.async_step(FENCE) is StepResult.WORKED

    # The next read lands on the deadline, not behind it.
    assert rig.state() == "uncertain"
    assert rig.column("not_before") == deadline

    rig.now = T0 + timedelta(seconds=deadline - STARTED)
    rig.server.queue(RECENT, history())
    assert await core.async_step(FENCE) is StepResult.WORKED

    assert rig.state() == "expired"
    assert rig.column("reason") == "unresolved"
    assert rig.column("title") == ""
    assert len(rig.sent()) == 1


async def test_unknown_read_at_the_deadline_also_expires(rig: Rig) -> None:
    rig.add()
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, Reply(drop=True))
    core = rig.core()
    await core.async_step(FENCE)

    rig.advance(ACTIVE_RETENTION_SECONDS)
    rig.server.queue(RECENT, Reply(status=500))
    await core.async_step(FENCE)

    assert rig.state() == "expired"
    assert len(rig.sent()) == 1


async def test_positive_read_past_the_deadline_still_finalizes(rig: Rig) -> None:
    rig.add()
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, Reply(drop=True))
    core = rig.core()
    await core.async_step(FENCE)

    rig.advance(ACTIVE_RETENTION_SECONDS)
    rig.server.queue(RECENT, history(row(STARTED)))
    await core.async_step(FENCE)

    assert rig.state() == "sent"


async def test_remote_accept_then_local_commit_failure_has_one_post(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.add()
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, accepted())
    core = rig.core()

    with monkeypatch.context() as patch:
        fail_commit_after(patch, FINALIZE)
        assert await core.async_step(FENCE) is StepResult.STOPPED

    stop = core.last_stop
    assert stop is not None
    assert stop.status is DeliveryStatus.STORAGE_ERROR
    assert rig.state() == "in_flight"

    # The stopped worker does nothing more, also with storage healthy again.
    rig.advance(2)
    assert await core.async_step(FENCE) is StepResult.STOPPED
    assert len(rig.server.requests) == 2

    restarted = await rig.restart()
    rig.server.queue(RECENT, history(row(STARTED)))
    assert await restarted.async_step(FENCE) is StepResult.WORKED

    assert len(rig.sent()) == 1
    assert rig.state() == "sent"
    assert rig.store.status(rig.now).sent_total == 1


# Auth


@pytest.mark.parametrize("restart", [False, True])
async def test_auth_required_stops_all_calls_until_relink(
    rig: Rig, restart: bool
) -> None:
    rig.add()
    rig.add("other", start=1000)
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, json_reply({"error": 9}))
    core = rig.core()

    assert await core.async_step(FENCE) is StepResult.WORKED
    assert rig.state() == "pending"
    assert rig.store.meta_get("auth_required") == "1"

    if restart:
        core = await rig.restart()
    calls = len(rig.server.requests)
    rig.advance(BACKOFF_CAP_SECONDS * 3)
    assert await core.async_step(FENCE) is StepResult.STOPPED
    assert core.last_stop is not None
    assert core.last_stop.status is DeliveryStatus.AUTH_REQUIRED
    assert len(rig.server.requests) == calls

    relinked = Fence(EPOCH + 1, 2, ACCOUNT, 0)
    rig.live = relinked
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, accepted())
    assert await core.async_step(relinked) is StepResult.WORKED
    assert rig.state() == "sent"
    assert len(rig.sent()) == 2


async def test_auth_failure_of_a_stale_post_leaves_no_marker(rig: Rig) -> None:
    rig.add()
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, json_reply({"error": 9}))
    rig.server.hold(SCROBBLE)
    core = rig.core()

    step = asyncio.create_task(core.async_step(FENCE))
    await rig.until_held(step)
    rig.live = None
    rig.server.release(SCROBBLE)

    assert await step is StepResult.STOPPED
    assert rig.state() == "pending"
    assert rig.store.meta_get("auth_required") is None


# Global limits


@pytest.mark.parametrize("code", [10, 26])
@pytest.mark.parametrize("state", ["pending", "uncertain"])
async def test_key_failure_on_a_read_stops_all_calls_until_relink(
    rig: Rig, code: int, state: str
) -> None:
    rig.add()
    if state == "uncertain":
        rig.store.claim_in_flight("occ", DeliveryState.PENDING, EPOCH)
        rig.store.mark_uncertain("occ", "transport_interrupted")
    rig.server.queue(RECENT, json_reply({"error": code}, status=403))
    core = rig.core()

    assert await core.async_step(FENCE) is StepResult.WORKED

    assert rig.sent() == []
    assert rig.store.meta_get("auth_required") == str(FENCE.link_revision)
    calls = len(rig.server.requests)
    rig.until_due()
    assert await core.async_step(FENCE) is StepResult.STOPPED
    assert core.last_stop is not None
    assert core.last_stop.status is DeliveryStatus.AUTH_REQUIRED
    assert len(rig.server.requests) == calls


async def test_private_profile_on_a_read_is_no_key_failure(rig: Rig) -> None:
    rig.add()
    rig.server.queue(RECENT, json_reply({"error": 17}))

    await rig.core().async_step(FENCE)

    assert rig.store.meta_get("auth_required") is None
    assert rig.state() == "held"


async def test_rate_limit_from_a_post_stops_every_call_until_it_ends(
    rig: Rig,
) -> None:
    rig.add()
    rig.add("other", start=1000, key=KEY_B)
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, json_reply({"error": 29}))
    core = rig.core()

    assert await core.async_step(FENCE) is StepResult.WORKED
    until = int(rig.store.meta_get("rate_limited_until") or 0)
    assert until > int(rig.now.timestamp())
    assert rig.state() == "pending"
    calls = len(rig.server.requests)

    # The other row is ready, and a restart does not lift the limit either.
    rig.advance(2)
    assert await core.async_step(FENCE) is StepResult.STOPPED
    assert core.last_stop is not None
    assert core.last_stop.status is DeliveryStatus.RETRYING
    assert core.last_stop.code == "rate_limited"
    assert core.last_stop.resume_at == T0 + timedelta(seconds=until - STARTED)
    restarted = await rig.restart()
    assert await restarted.async_step(FENCE) is StepResult.STOPPED
    assert len(rig.server.requests) == calls

    rig.now = T0 + timedelta(seconds=until - STARTED)
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, accepted(STARTED + 1000))
    assert await restarted.async_step(FENCE) is StepResult.WORKED
    assert len(rig.server.requests) == calls + 2


async def test_rate_limit_from_a_read_stops_the_other_rows_too(rig: Rig) -> None:
    rig.add()
    rig.add("other", start=1000, key=KEY_B)
    rig.server.queue(RECENT, json_reply({"error": 29}))
    core = rig.core()

    assert await core.async_step(FENCE) is StepResult.WORKED

    assert rig.state() == "held"
    assert rig.store.meta_get("rate_limited_until") is not None
    rig.advance(2)
    assert await core.async_step(FENCE) is StepResult.STOPPED
    assert rig.reads() == 1


async def test_every_request_waits_its_turn_also_inside_a_step(rig: Rig) -> None:
    rig.add("one")
    rig.add("two", start=1000, key=KEY_B)
    rig.server.queue(RECENT, page(1, 2), page(2, 2), history())
    rig.server.queue(SCROBBLE, accepted(), accepted(STARTED + 1000))
    core = rig.core()
    begin = rig.now

    assert await core.async_step(FENCE) is StepResult.WORKED
    assert await core.async_step(FENCE) is StepResult.WORKED

    assert MIN_CALL_SPACING_SECONDS == 1.0
    assert [item.form["method"] for item in rig.server.requests] == [
        RECENT,
        RECENT,
        SCROBBLE,
        RECENT,
        SCROBBLE,
    ]
    # Each wait came before its own request, one second after the one before it.
    assert rig.waits == [
        (begin + timedelta(seconds=1), 1),
        (begin + timedelta(seconds=2), 2),
        (begin + timedelta(seconds=3), 3),
        (begin + timedelta(seconds=4), 4),
    ]
    assert rig.state("two") == "sent"


async def test_a_new_worker_keeps_the_gap_of_the_last(rig: Rig) -> None:
    rig.add()
    rig.server.queue(RECENT, history(row(STARTED)))
    await rig.core().async_step(FENCE)
    rig.add("two", start=1000, key=KEY_B)
    rig.server.queue(RECENT, history(row(STARTED + 1000)))

    await rig.core().async_step(FENCE)

    assert len(rig.waits) == 1


async def test_no_wait_once_the_gap_has_passed(rig: Rig) -> None:
    rig.add()
    rig.server.queue(RECENT, history(row(STARTED)))
    await rig.core().async_step(FENCE)
    rig.add("two", start=1000, key=KEY_B)
    rig.server.queue(RECENT, history(row(STARTED + 1000)))
    rig.advance(MIN_CALL_SPACING_SECONDS)

    await rig.core().async_step(FENCE)

    assert rig.waits == []


async def _until_waiting(rig: Rig, step: asyncio.Task[StepResult]) -> None:
    waiting = asyncio.ensure_future(rig.waiting.wait())
    await asyncio.wait({step, waiting}, return_when=asyncio.FIRST_COMPLETED)
    if step.done():
        waiting.cancel()
        await step
        pytest.fail("the step ended without waiting its turn")


async def test_pause_during_the_wait_for_the_post_releases_nothing(rig: Rig) -> None:
    rig.add()
    rig.server.queue(RECENT, history())
    rig.wait_gate = asyncio.Event()
    core = rig.core()

    step = asyncio.create_task(core.async_step(FENCE))
    await _until_waiting(rig, step)
    rig.live = None
    rig.wait_gate.set()

    assert await step is StepResult.STOPPED
    assert core.last_stop is not None
    assert core.last_stop.code == "fenced"
    assert rig.sent() == []
    assert rig.state() == "pending"
    assert rig.column("attempts") == 0


async def test_pause_during_the_wait_for_a_page_reads_no_further(rig: Rig) -> None:
    rig.add()
    rig.server.queue(RECENT, page(1, 2), page(2, 2))
    rig.wait_gate = asyncio.Event()
    core = rig.core()

    step = asyncio.create_task(core.async_step(FENCE))
    await _until_waiting(rig, step)
    rig.live = None
    rig.wait_gate.set()

    assert await step is StepResult.STOPPED
    assert rig.reads() == 1
    assert rig.state() == "pending"


async def test_cancelling_the_wait_leaves_the_row_unclaimed(rig: Rig) -> None:
    rig.add()
    rig.server.queue(RECENT, history())
    rig.wait_gate = asyncio.Event()

    step = asyncio.create_task(rig.core().async_step(FENCE))
    await _until_waiting(rig, step)
    step.cancel()

    with pytest.raises(asyncio.CancelledError):
        await step
    assert rig.sent() == []
    assert rig.state() == "pending"


async def test_deadline_reached_during_the_wait_expires_the_row(rig: Rig) -> None:
    rig.add()
    rig.now = at(READY + ACTIVE_RETENTION_SECONDS - 1)
    rig.server.queue(RECENT, history())

    await rig.core().async_step(FENCE)

    assert rig.sent() == []
    assert rig.state() == "expired"


async def test_clock_behind_the_high_water_mark_does_nothing_until_caught_up(
    rig: Rig,
) -> None:
    rig.add("one")
    rig.add("two", start=1000, key=KEY_B)
    rig.server.queue(RECENT, history(row(STARTED)))
    core = rig.core()
    await core.async_step(FENCE)
    high = rig.now
    calls = len(rig.server.requests)

    rig.now = high - timedelta(hours=1)
    assert await core.async_step(FENCE) is StepResult.STOPPED
    assert core.last_stop is not None
    assert core.last_stop.code == "clock_regressed"
    assert core.last_stop.resume_at == high

    # The mark is durable: a restart with the wrong clock waits as well.
    restarted = await rig.restart()
    assert await restarted.async_step(FENCE) is StepResult.STOPPED
    assert len(rig.server.requests) == calls
    assert rig.state("two") == "pending"
    assert int(rig.store.meta_get("clock_high_water") or 0) == int(high.timestamp())

    rig.now = high + timedelta(seconds=2)
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, accepted(STARTED + 1000))
    assert await restarted.async_step(FENCE) is StepResult.WORKED
    assert rig.state("two") == "sent"


async def test_clock_falling_back_during_the_read_stops_before_the_claim(
    rig: Rig,
) -> None:
    rig.add()
    rig.server.queue(RECENT, history())
    rig.server.hold(RECENT)
    core = rig.core()

    step = asyncio.create_task(core.async_step(FENCE))
    await rig.until_held(step)
    rig.advance(-10)
    rig.server.release(RECENT)

    assert await step is StepResult.STOPPED
    assert core.last_stop is not None
    assert core.last_stop.code == "clock_regressed"
    assert rig.sent() == []
    assert rig.state() == "pending"
    assert rig.column("attempts") == 0


async def test_clock_falling_back_during_the_claim_stops_before_the_post(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.add()
    rig.server.queue(RECENT, history())
    core = rig.core()

    with monkeypatch.context() as patch:
        on_statement(patch, CLAIM, lambda: rig.advance(-10))
        assert await core.async_step(FENCE) is StepResult.STOPPED

    assert core.last_stop is not None
    assert core.last_stop.code == "clock_regressed"
    assert rig.sent() == []
    assert rig.state() == "uncertain"
    assert rig.column("reason") == "clock_behind_before_send"


async def test_clock_recovering_while_marking_uncertain_still_never_posts(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.add()
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, accepted())
    core = rig.core()

    class ClockThatComesBack(sqlite3.Connection):
        def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
            if sql.startswith(CLAIM):
                rig.advance(-10)
            elif sql.startswith("UPDATE plays SET state = 'uncertain'"):
                rig.advance(10)
            return super().execute(sql, *args)

    with monkeypatch.context() as patch:
        install_connection(patch, ClockThatComesBack)
        assert await core.async_step(FENCE) is StepResult.STOPPED

    assert rig.sent() == []
    assert rig.state() == "uncertain"


@pytest.mark.parametrize("key", ["rate_limited_until", "clock_high_water"])
async def test_stored_time_past_the_calendar_is_a_storage_error(
    rig: Rig, key: str
) -> None:
    rig.add()
    rig.store.meta_set(key, "999999999999")
    core = rig.core()

    assert await core.async_step(FENCE) is StepResult.STOPPED

    assert core.last_stop is not None
    assert core.last_stop.code == "invalid_data"
    assert core.last_stop.status is DeliveryStatus.STORAGE_ERROR
    assert rig.server.requests == []


async def test_unreadable_high_water_mark_is_a_storage_error(rig: Rig) -> None:
    rig.add()
    rig.store.meta_set("clock_high_water", "not-a-number")
    core = rig.core()

    assert await core.async_step(FENCE) is StepResult.STOPPED

    assert core.last_stop is not None
    assert core.last_stop.status is DeliveryStatus.STORAGE_ERROR
    assert rig.server.requests == []


# Worker start


async def test_recovery_runs_once_per_worker_start_and_first(rig: Rig) -> None:
    rig.add()
    rig.store.claim_in_flight("occ", DeliveryState.PENDING, EPOCH)
    rig.add("late", start=1000, key=KEY_B)
    core = rig.core()
    rig.server.queue(RECENT, history())

    # The left-over attempt is due at once and reconciles; it never reposts.
    assert await core.async_step(FENCE) is StepResult.WORKED
    assert rig.state() == "uncertain"
    assert rig.sent() == []

    # A row claimed later in the life of this worker is not swept up.
    rig.store.claim_in_flight("late", DeliveryState.PENDING, EPOCH)
    rig.advance(2)
    await core.async_step(FENCE)
    assert rig.state("late") == "in_flight"

    restarted = await rig.restart()
    await restarted.async_step(FENCE)
    assert rig.state("late") == "uncertain"


async def test_start_rebinds_to_the_fence_and_purges_other_accounts(rig: Rig) -> None:
    rig.add()
    add(rig.store, "foreign", start=1000, key=KEY_B, account=OTHER_ACCOUNT, now=READY)
    rig.store.claim_in_flight("foreign", DeliveryState.PENDING, EPOCH)
    fence = Fence(EPOCH + 3, 1, ACCOUNT, 0)
    rig.live = fence
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, accepted())

    await rig.core().async_step(fence)

    assert rig.store.meta_get("epoch") == str(EPOCH + 3)
    assert rig.state("foreign") == "discarded"
    assert rig.column("title", "foreign") == ""
    assert rig.state() == "sent"
    assert len(rig.sent()) == 1


async def test_lost_pause_stops_the_worker_without_a_call(rig: Rig) -> None:
    rig.add()
    rig.store.set_epoch(EPOCH + 5)
    core = rig.core()

    assert await core.async_step(FENCE) is StepResult.STOPPED

    assert core.last_stop is not None
    assert core.last_stop.code == "stale_epoch"
    assert core.last_stop.status is None
    assert rig.server.requests == []
    assert rig.state() == "pending"


async def test_a_row_of_another_account_is_discarded_and_blocks_nobody(
    rig: Rig,
) -> None:
    core = rig.core()
    assert await core.async_step(FENCE) is StepResult.IDLE
    add(rig.store, "foreign", account=OTHER_ACCOUNT, now=READY)
    rig.add("younger", start=1000, key=KEY_B)

    assert await core.async_step(FENCE) is StepResult.WORKED

    assert rig.state("foreign") == "discarded"
    assert rig.column("title", "foreign") == ""
    assert rig.server.requests == []
    assert rig.clients == []

    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, accepted(STARTED + 1000))
    assert await core.async_step(FENCE) is StepResult.WORKED
    assert rig.state("younger") == "sent"


# Fence


async def test_stale_fence_does_nothing_at_all(rig: Rig) -> None:
    rig.add()
    write(rig.path, "UPDATE plays SET state = 'in_flight' WHERE id = 'occ'")
    rig.live = None
    core = rig.core()

    assert await core.async_step(FENCE) is StepResult.STOPPED

    assert core.last_stop is not None
    assert core.last_stop.code == "fenced"
    assert rig.state() == "in_flight"
    assert rig.store.meta_get("clock_high_water") is None
    assert rig.server.requests == []


@pytest.mark.parametrize(
    "reply",
    [history(row(STARTED)), Reply(status=500), json_reply({"error": 29})],
    ids=["match", "unknown", "rate_limited"],
)
async def test_fence_lost_during_the_read_writes_nothing(
    rig: Rig, reply: Reply
) -> None:
    rig.add()
    rig.server.queue(RECENT, reply)
    rig.server.hold(RECENT)

    step = asyncio.create_task(rig.core().async_step(FENCE))
    await rig.until_held(step)
    rig.live = None
    rig.server.release(RECENT)

    assert await step is StepResult.STOPPED
    assert rig.state() == "pending"
    assert rig.column("read_attempts") == 0
    assert rig.store.meta_get("rate_limited_until") is None
    assert rig.sent() == []


async def test_fence_is_rechecked_with_no_await_before_the_post(
    hass: HomeAssistant, lastfm_server: LastFmServer, tmp_path: Path
) -> None:
    """Fail each check in turn: until the last one before the POST, nothing posts."""
    baseline = build_rig(hass, lastfm_server, tmp_path / "baseline.sqlite3")
    baseline.add()
    lastfm_server.queue(RECENT, history())
    lastfm_server.queue(SCROBBLE, accepted())
    lastfm_server.hold(SCROBBLE)
    core = baseline.core()
    step = asyncio.create_task(core.async_step(FENCE))
    await baseline.until_held(step)
    checks = baseline.fence_checks
    lastfm_server.release(SCROBBLE)
    await step
    await baseline.outbox.async_close()
    lastfm_server.requests.clear()

    assert checks >= 8
    for failing_check in range(1, checks + 1):
        sweep = build_rig(hass, lastfm_server, tmp_path / f"{failing_check}.sqlite3")
        sweep.add()
        sweep.fence_fails_at = failing_check
        lastfm_server.queue(RECENT, history())

        result = await sweep.core().async_step(FENCE)
        await sweep.outbox.async_close()

        assert result is StepResult.STOPPED, failing_check
        assert sweep.sent() == [], failing_check
        expected = "uncertain" if failing_check == checks else "pending"
        assert sweep.state() == expected, failing_check
        if failing_check == checks:
            assert sweep.column("reason") == "fenced_before_send"
            assert sweep.column("attempts") == 1
        lastfm_server.requests.clear()
        lastfm_server._replies.clear()


async def test_post_on_the_wire_may_close_its_own_row_after_the_fence_moved(
    rig: Rig,
) -> None:
    rig.add()
    rig.add("next", start=1000, key=KEY_B)
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, accepted())
    rig.server.hold(SCROBBLE)

    step = asyncio.create_task(rig.core().async_step(FENCE))
    await rig.until_held(step)
    rig.live = Fence(EPOCH + 1, 2, OTHER_ACCOUNT, 0)
    rig.server.release(SCROBBLE)

    assert await step is StepResult.STOPPED
    assert rig.state() == "sent"
    assert rig.state("next") == "pending"
    assert len(rig.sent()) == 1


async def test_a_store_ahead_of_the_fence_stops_before_the_post(rig: Rig) -> None:
    rig.add()
    rig.server.queue(RECENT, history())
    rig.server.hold(RECENT)
    core = rig.core()

    step = asyncio.create_task(core.async_step(FENCE))
    await rig.until_held(step)
    write(rig.path, "UPDATE meta SET value = '99' WHERE key = 'epoch'")
    rig.server.release(RECENT)

    assert await step is StepResult.STOPPED
    assert core.last_stop is not None
    assert core.last_stop.code == "stale_epoch"
    assert core.last_stop.status is None
    assert rig.sent() == []
    assert rig.state() == "pending"


async def test_a_row_on_a_stale_epoch_is_rebound_without_a_post_in_that_step(
    rig: Rig,
) -> None:
    core = rig.core()
    assert await core.async_step(FENCE) is StepResult.IDLE
    rig.add(epoch=EPOCH - 1)
    rig.server.queue(RECENT, history(), history())
    rig.server.queue(SCROBBLE, accepted())

    assert await core.async_step(FENCE) is StepResult.WORKED

    assert rig.sent() == []
    assert rig.state() == "pending"
    assert rig.column("epoch") == EPOCH

    rig.advance(MIN_CALL_SPACING_SECONDS)
    assert await core.async_step(FENCE) is StepResult.WORKED
    assert rig.reads() == 2
    assert rig.state() == "sent"


# Storage


async def test_storage_failure_stops_the_worker_with_a_fixed_code(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.add()
    rig.server.queue(RECENT, history())
    core = rig.core()

    with monkeypatch.context() as patch:
        fail_commit_after(patch, CLAIM)
        assert await core.async_step(FENCE) is StepResult.STOPPED

    assert core.last_stop is not None
    assert core.last_stop.status is DeliveryStatus.STORAGE_ERROR
    assert core.last_stop.code == "storage_error"
    assert rig.sent() == []
    assert rig.state() == "pending"

    calls = len(rig.server.requests)
    rig.advance(2)
    assert await core.async_step(FENCE) is StepResult.STOPPED
    assert len(rig.server.requests) == calls
    assert rig.path.exists()


async def test_refused_transition_blocks_no_younger_row(rig: Rig) -> None:
    rig.add()
    rig.add("younger", start=1000, key=KEY_B)
    rig.server.queue(RECENT, history(row(STARTED)))
    rig.server.hold(RECENT)
    core = rig.core()

    step = asyncio.create_task(core.async_step(FENCE))
    await rig.until_held(step)
    write(
        rig.path,
        "UPDATE plays SET state = 'sent', artist = '', title = '', album = NULL "
        "WHERE id = 'occ'",
    )
    rig.server.release(RECENT)

    assert await step is StepResult.WORKED
    assert core.last_stop is not None
    assert core.last_stop.code == "illegal_transition"
    assert core.last_stop.status is None
    assert rig.sent() == []

    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, accepted(STARTED + 1000))
    rig.advance(MIN_CALL_SPACING_SECONDS)
    assert await core.async_step(FENCE) is StepResult.WORKED
    assert core.last_stop is None
    assert rig.state("younger") == "sent"


async def test_closed_outbox_stops_without_a_status(rig: Rig) -> None:
    rig.add()
    await rig.outbox.async_close()
    core = rig.core()

    assert await core.async_step(FENCE) is StepResult.STOPPED

    assert core.last_stop is not None
    assert core.last_stop.code == "outbox_closed"
    assert core.last_stop.status is None


async def test_executor_gone_is_a_storage_stop(rig: Rig) -> None:
    def refusing(*_args: Any) -> Any:
        raise RuntimeError("cannot schedule new futures after shutdown")

    rig.outbox = AsyncOutbox(rig.store, refusing)
    core = rig.core()

    assert await core.async_step(FENCE) is StepResult.STOPPED

    assert core.last_stop is not None
    assert core.last_stop.status is DeliveryStatus.STORAGE_ERROR
    assert core.last_stop.code == "executor_gone"


@pytest.mark.parametrize(
    ("statement", "state_after"),
    [(CLAIM, "in_flight"), ("UPDATE plays SET state = 'pending'", "pending")],
)
async def test_cancelled_wrapper_means_outcome_unknown(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    statement: str,
    state_after: str,
) -> None:
    """HA cancels the executor wrapper, not the task: the write may still land."""
    wrappers: list[asyncio.Future[Any]] = []
    entered = threading.Event()
    release = threading.Event()

    def run_blocking(function: Any, *args: Any) -> asyncio.Future[Any]:
        wrappers.append(rig.hass.async_add_executor_job(function, *args))
        return wrappers[-1]

    def hold() -> None:
        entered.set()
        assert release.wait(10)

    rig.add()
    rig.outbox = AsyncOutbox(rig.store, run_blocking)
    core = rig.core()
    rig.server.queue(RECENT, history())
    if statement != CLAIM:
        rig.server.queue(SCROBBLE, json_reply({"error": 16}))

    with monkeypatch.context() as patch:
        on_statement(patch, statement, hold)
        step = asyncio.create_task(core.async_step(FENCE))
        await asyncio.to_thread(entered.wait, 10)
        wrappers[-1].cancel()

        assert await step is StepResult.STOPPED
        assert step.cancelling() == 0
        assert core.last_stop is not None
        assert core.last_stop.code == "outcome_unknown"
        assert core.last_stop.status is DeliveryStatus.STORAGE_ERROR

        # The thread still holds its write: no conclusion, no next call.
        calls = len(rig.server.requests)
        rig.advance(2)
        assert await core.async_step(FENCE) is StepResult.STOPPED
        assert len(rig.server.requests) == calls

        release.set()
        await rig.outbox.async_drain()

    assert rig.state() == state_after
    posts = len(rig.sent())

    restarted = await rig.restart()
    rig.server.queue(RECENT, history())
    await restarted.async_step(FENCE)
    assert len(rig.sent()) == posts


async def test_real_cancellation_during_a_store_call_propagates(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def hold() -> None:
        entered.set()
        assert release.wait(10)

    rig.add()
    rig.server.queue(RECENT, history())
    core = rig.core()

    with monkeypatch.context() as patch:
        on_statement(patch, CLAIM, hold)
        step = asyncio.create_task(core.async_step(FENCE))
        await asyncio.to_thread(entered.wait, 10)
        step.cancel()
        with pytest.raises(asyncio.CancelledError):
            await step
        release.set()
        await rig.outbox.async_drain()

    assert rig.sent() == []
    assert rig.state() == "in_flight"


async def test_cancelled_post_leaves_the_attempt_for_recovery(rig: Rig) -> None:
    rig.add()
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, accepted())
    rig.server.hold(SCROBBLE)
    core = rig.core()

    step = asyncio.create_task(core.async_step(FENCE))
    await rig.until_held(step)
    step.cancel()
    with pytest.raises(asyncio.CancelledError):
        await step
    await rig.outbox.async_drain()

    assert rig.state() == "in_flight"
    restarted = await rig.restart()
    rig.server.release(SCROBBLE)
    rig.server.queue(RECENT, history())
    await restarted.async_step(FENCE)

    assert rig.state() == "uncertain"
    assert len(rig.sent()) == 1


# Privacy


async def test_no_stop_or_log_carries_listening_data(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.add()
    rig.server.queue(RECENT, history())
    rig.server.queue(SCROBBLE, json_reply({"error": 9}))
    core = rig.core()
    await core.async_step(FENCE)
    rig.advance(2)
    await core.async_step(FENCE)

    printed = repr(core.last_stop) + caplog.text + json.dumps(repr(FENCE))

    for canary in ("canary-artist", "canary-title", ACCOUNT_NAME, SESSION_KEY):
        assert canary not in printed
    assert str(STARTED) not in printed


async def test_store_errors_reach_the_stop_as_codes_only(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.add()
    core = rig.core()

    def boom(*_args: Any, **_kwargs: Any) -> sqlite3.Connection:
        raise sqlite3.OperationalError("canary-title")

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", boom)
        assert await core.async_step(FENCE) is StepResult.STOPPED

    assert "canary" not in repr(core.last_stop)


# Service: what the worker never sees


async def test_event_committed_before_worker_wakes(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    await pilot.linked()
    pilot.server.queue(RECENT, history())
    pilot.server.queue(SCROBBLE, accepted())
    await pilot.advance(2000)

    # Far past its grace, so only the wake can start the worker: no clock moves.
    with held_commit(monkeypatch, "INSERT INTO plays") as hold:
        pilot.accept(qualified("occ", start=1))
        await pilot.reach(hold.reached.is_set)
        await pilot.linger()
        assert pilot.reads() == 0
        assert pilot.state() is None
        hold.release.set()
        await pilot.reach(lambda: pilot.reads() == 1)

    assert pilot.state() is not None


async def test_upgrade_never_creates_db_or_enqueues_while_disabled(
    pilot: Pilot,
) -> None:
    await pilot.setup()
    await link_account(pilot.hass, pilot.entry)

    await pilot.accept_now()
    pilot.service.async_on_occurrence_change(MarkerDelta(markers_set=(hint(),)))
    await pilot.quiet()
    await pilot.advance(MAINTENANCE_INTERVAL.total_seconds() + 1)

    assert not pilot.path.exists()
    assert pilot.entry.data[CONF_OUTBOX_CREATED] is False
    assert pilot.service.status.status is DeliveryStatus.DISABLED


async def test_enabling_creates_the_ledger_and_needs_a_link(pilot: Pilot) -> None:
    await pilot.setup()
    assert not pilot.path.exists()

    await link_account(pilot.hass, pilot.entry)
    assert not pilot.path.exists()
    menu = await start_options(pilot.hass, pilot.entry)
    assert "clear_queue" not in menu["menu_options"]
    await pilot.enable()

    assert pilot.path.exists()
    assert pilot.entry.data[CONF_OUTBOX_CREATED] is True
    menu = await start_options(pilot.hass, pilot.entry)
    assert "clear_queue" in menu["menu_options"]


async def test_play_started_before_enable_is_not_credited(pilot: Pilot) -> None:
    await pilot.setup()
    await link_account(pilot.hass, pilot.entry)
    await pilot.advance(100)
    await pilot.enable()

    await pilot.accept_now("before", start=99)
    await pilot.accept_now("after", start=100)

    assert pilot.state("before") is None
    assert pilot.state("after") == "pending"


async def test_pause_lost_by_crash_stays_paused(pilot: Pilot) -> None:
    await pilot.linked()
    await pilot.accept_now()
    await pilot.unload()
    # The ledger saw a pause the delayed config save never got to write.
    write(pilot.path, "UPDATE meta SET value = '50' WHERE key = 'epoch'")

    await pilot.setup()
    await pilot.past_grace()

    assert pilot.entry.data[CONF_SCROBBLING] is False
    assert pilot.entry.data[CONF_NOW_PLAYING] is True
    assert pilot.entry.data[CONF_DELIVERY_EPOCH] == 51
    assert pilot.rows("SELECT value FROM meta WHERE key = 'epoch'") == [("51",)]
    assert pilot.reads() == 0
    assert pilot.posts() == 0
    assert pilot.state() == "pending"


@pytest.mark.parametrize("damage", ["missing", "corrupt", "empty"])
async def test_damaged_ledger_is_a_storage_error_and_stays_as_it_is(
    pilot: Pilot, damage: str
) -> None:
    await pilot.linked()
    await pilot.unload()
    if damage == "missing":
        pilot.path.unlink()
    else:
        pilot.path.write_bytes(b"not a database" if damage == "corrupt" else b"")
    before = pilot.path.read_bytes() if damage != "missing" else None

    await pilot.setup()
    await pilot.accept_now()
    await pilot.past_grace()

    snapshot = pilot.service.status
    assert snapshot.status is DeliveryStatus.STORAGE_ERROR
    assert snapshot.error_code is not None
    assert pilot.server.calls(RECENT) == []
    if before is None:
        assert not pilot.path.exists()
    else:
        assert pilot.path.read_bytes() == before


async def test_crash_after_create_is_adopted_only_if_the_ledger_is_whole(
    pilot: Pilot,
) -> None:
    await pilot.setup()
    await link_account(pilot.hass, pilot.entry)
    pilot.path.write_bytes(b"")

    with pytest.raises(HomeAssistantError):
        await pilot.press(Feature.SCROBBLING, "turn_on")

    assert pilot.entry.data[CONF_SCROBBLING] is False
    assert pilot.entry.data[CONF_OUTBOX_CREATED] is False
    assert pilot.path.read_bytes() == b""


async def test_status_listener_hears_a_change_and_can_leave(pilot: Pilot) -> None:
    await pilot.setup()
    await link_account(pilot.hass, pilot.entry)
    heard: list[DeliveryStatus] = []
    remove = pilot.service.async_add_listener(
        lambda: heard.append(pilot.service.status.status)
    )

    await pilot.enable()
    await pilot.quiet()
    assert heard[-1] is DeliveryStatus.READY
    remove()
    count = len(heard)
    await pilot.pause()

    assert len(heard) == count


def test_a_fence_never_defaults_to_the_whole_history() -> None:
    with pytest.raises(TypeError):
        Fence(EPOCH, 1, ACCOUNT)  # type: ignore[call-arg]
