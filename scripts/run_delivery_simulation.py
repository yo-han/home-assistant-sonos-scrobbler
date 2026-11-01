"""Run the real delivery worker against the local simulation and check every outcome.

The worker, notice sender, client and SQLite ledger are the real ones; the provider
is a synthetic mock. This shows how the client behaves against that mock, never that
the real API agrees: see
postman/simulations/lastfm-delivery/lastfm-delivery.context.yaml.

Two things differ from production and are named in the evidence: the pacing gate
does not wait, it moves the runner's clock, and the request deadline is shortened
so a timeout takes a second. Now Playing cases start at normalize_state and go
through the real streams, board and sender, without the Home Assistant runtime.
"""

import asyncio
import json
import random
import sqlite3
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import IO, Any
from unittest.mock import patch

import aiohttp

from custom_components.personal_sonos_scrobbler import lastfm
from custom_components.personal_sonos_scrobbler.delivery import (
    DeliveryCore,
    Fence,
    NoticeSender,
    RequestPacer,
    account_hash,
)
from custom_components.personal_sonos_scrobbler.lastfm import LastFmCredentials
from custom_components.personal_sonos_scrobbler.lastfm_delivery import (
    LastFmDeliveryClient,
    NowPlayingOutcome,
    NowPlayingPayload,
    NowPlayingResult,
)
from custom_components.personal_sonos_scrobbler.normalize import normalize_state
from custom_components.personal_sonos_scrobbler.now_playing import NowPlayingBoard
from custom_components.personal_sonos_scrobbler.outbox import (
    AsyncOutbox,
    DeliveryState,
    OutboxStore,
)
from custom_components.personal_sonos_scrobbler.playback import (
    QualifiedPlay,
    TrackMetadata,
)
from custom_components.personal_sonos_scrobbler.streams import StreamSet
from custom_components.personal_sonos_scrobbler.topology import (
    CanonicalGroup,
    Topology,
)
from scripts.run_lastfm_simulation import (
    ACCOUNT_NAME,
    EVENT_LINE_LIMIT,
    ROOT,
    SHARED_SECRET,
    START_TIMEOUT_SECONDS,
    SimulationError,
    _async_listeners,
    _async_port_is_closed,
    _async_read_events,
    _async_stop,
)

SIMULATION = "postman/simulations/lastfm-delivery/lastfm-delivery.sim.yaml"
MOCK_MEMBER = "lastfm"
SESSION_KEY = "synthetic-session-key"
CLIENT_DEADLINE_SECONDS = 1.0
# Past the mock's WRITE_DELAY_MS, so delayed answers are logged before it stops.
LATE_ANSWER_SECONDS = 2.5
MOCK_POLL_SECONDS = 0.01
MOCK_POLLS = 500
STARTED_AT = datetime(2031, 3, 4, 12, 0, 0, tzinfo=UTC)
DURATION_SECONDS = 200.0
FIRST_MOMENT = STARTED_AT + timedelta(seconds=300)
EPOCH = 1
LINK_REVISION = 1
FENCE = Fence(EPOCH, LINK_REVISION, account_hash(ACCOUNT_NAME), 0)
PLAY = QualifiedPlay(
    occurrence_id="synthetic-occurrence",
    track_key=b"k" * 16,
    metadata=TrackMetadata("Synthetic Artist", "Synthetic Title", "Synthetic Album"),
    started_at=STARTED_AT,
    qualified_at=STARTED_AT + timedelta(seconds=DURATION_SECONDS / 2),
    duration_seconds=DURATION_SECONDS,
)
FINISHED = frozenset(
    {
        DeliveryState.SENT,
        DeliveryState.DUPLICATE,
        DeliveryState.IGNORED,
        DeliveryState.REJECTED,
    }
)
LISTENERS = 2
SPEAKER = "media_player.synthetic"
SOLO = Topology(
    (CanonicalGroup(SPEAKER, frozenset({SPEAKER})),),
    frozenset(),
)
HEARTBEAT_SECONDS = 15.0
REPEATED_UPDATES = 4


class Fault(StrEnum):
    """What ends the first process of a scenario, if anything does."""

    NONE = "none"
    CANCEL_IN_FLIGHT = "cancel_in_flight"
    LOST_COMMIT = "lost_commit"


@dataclass(frozen=True, slots=True)
class Scenario:
    """One play through the worker, and what the mock and ledger must show after."""

    name: str
    api_key: str
    posts: int
    reads: int
    state: str
    reason: str
    stored: int = 0
    steps: int = 1
    fault: Fault = Fault.NONE

    def expected(self) -> dict[str, Any]:
        """Return the fields a result must match exactly."""
        fields = asdict(self)
        return {
            key: fields[key] for key in ("posts", "reads", "state", "reason", "stored")
        }


def _delivery_key(suffix: str) -> str:
    return suffix.rjust(32, "d")


def _write_error(
    code: int, state: str, reason: str, steps: int, reads: int
) -> Scenario:
    return Scenario(
        f"http200_api_error_write_{code}",
        f"{'a' * 30}{code:02d}",
        1,
        reads,
        state,
        reason,
        steps=steps,
    )


def _read_error(code: int, reason: str) -> Scenario:
    return Scenario(
        f"http200_api_error_read_{code}",
        f"{'b' * 30}{code:02d}",
        0,
        1,
        "held",
        reason,
    )


AMBIGUOUS = frozenset(
    {
        "accepted_then_response_lost",
        "restart_in_flight",
        "local_result_commit_failure",
        "timeout_after_write",
    }
)
SCENARIOS = (
    Scenario("success", _delivery_key("01"), 1, 1, "sent", "accepted", stored=1),
    Scenario(
        "existing_spotify_history",
        _delivery_key("02"),
        0,
        1,
        "duplicate",
        "history_match",
    ),
    Scenario(
        "explicit_retry_11",
        _delivery_key("03"),
        2,
        2,
        "sent",
        "accepted",
        stored=1,
        steps=4,
    ),
    Scenario(
        "explicit_retry_16",
        _delivery_key("04"),
        2,
        2,
        "sent",
        "accepted",
        stored=1,
        steps=4,
    ),
    Scenario(
        "rate_limit_29",
        _delivery_key("05"),
        2,
        2,
        "sent",
        "accepted",
        stored=1,
        steps=4,
    ),
    *(_write_error(code, "rejected", "rejected", 1, 1) for code in (2, 3, 5, 6, 7)),
    *(_write_error(code, "pending", "auth_error", 3, 1) for code in (4, 9, 10, 13, 26)),
    *(_write_error(code, "uncertain", "history_absent", 2, 2) for code in (8, 99)),
    _read_error(8, "api_error"),
    _read_error(10, "auth_error"),
    _read_error(26, "auth_error"),
    _read_error(29, "rate_limited"),
    Scenario("ignored_entry", _delivery_key("06"), 1, 1, "ignored", "ignored"),
    Scenario(
        "invalid_session",
        _delivery_key("07"),
        1,
        1,
        "pending",
        "auth_error",
        steps=3,
    ),
    Scenario(
        "malformed_body",
        _delivery_key("08"),
        1,
        2,
        "uncertain",
        "history_absent",
        steps=2,
    ),
    Scenario(
        "oversized_body",
        _delivery_key("09"),
        1,
        2,
        "uncertain",
        "history_absent",
        steps=2,
    ),
    Scenario(
        "history_failure", _delivery_key("0a"), 0, 2, "held", "http_status", steps=2
    ),
    Scenario(
        "incomplete_history_page",
        _delivery_key("0b"),
        0,
        4,
        "held",
        "row_shape",
        steps=2,
    ),
    Scenario(
        "accepted_then_response_lost",
        _delivery_key("0c"),
        1,
        2,
        "sent",
        "history_exact",
        stored=1,
        steps=2,
    ),
    Scenario(
        "restart_in_flight",
        _delivery_key("0d"),
        1,
        2,
        "sent",
        "history_exact",
        stored=1,
        fault=Fault.CANCEL_IN_FLIGHT,
    ),
    Scenario(
        "local_result_commit_failure",
        _delivery_key("0e"),
        1,
        2,
        "sent",
        "history_exact",
        stored=1,
        fault=Fault.LOST_COMMIT,
    ),
    Scenario(
        "timeout_after_write",
        _delivery_key("0f"),
        1,
        2,
        "sent",
        "history_exact",
        stored=1,
        steps=2,
    ),
)


@dataclass(frozen=True, slots=True)
class NoticeScenario:
    """One track through the sender, and what the mock and ledger must show after."""

    name: str
    api_key: str
    outcome: NowPlayingOutcome
    reason: str
    notices: int = 1
    gate: str | None = None

    def expected(self) -> dict[str, Any]:
        """Return the fields a result must match exactly."""
        return {
            "outcome": self.outcome.value,
            "reason": self.reason,
            "notices": self.notices,
            "gate": self.gate,
            "posts": 0,
            "reads": 0,
            "stored": 0,
            "ledger_rows": 0,
        }


def _notice_error(code: int, outcome: NowPlayingOutcome, reason: str) -> NoticeScenario:
    return NoticeScenario(
        f"notice_api_error_{code}",
        f"{'c' * 30}{code:02d}",
        outcome,
        reason,
        gate={9: "auth_required", 29: "rate_limited_until"}.get(code),
    )


NOTICE_SCENARIOS = (
    NoticeScenario(
        "notice_repeated_updates_then_new_track",
        _delivery_key("13"),
        NowPlayingOutcome.ACCEPTED,
        "accepted",
        notices=2,
    ),
    NoticeScenario(
        "notice_ignored",
        _delivery_key("11"),
        NowPlayingOutcome.IGNORED,
        "ignored",
    ),
    NoticeScenario(
        "notice_malformed_body",
        _delivery_key("12"),
        NowPlayingOutcome.FAILED,
        "body_not_object",
    ),
    NoticeScenario(
        "notice_timeout",
        _delivery_key("10"),
        NowPlayingOutcome.FAILED,
        "transport_failed",
    ),
    _notice_error(11, NowPlayingOutcome.FAILED, "api_error"),
    _notice_error(9, NowPlayingOutcome.AUTH_REQUIRED, "auth_error"),
    _notice_error(29, NowPlayingOutcome.FAILED, "rate_limited"),
)


class RecordingClient(LastFmDeliveryClient):
    """Keep the fixed results of the real client for the evidence."""

    results: list[NowPlayingResult]

    async def async_now_playing(self, payload: NowPlayingPayload) -> NowPlayingResult:
        """Make the real attempt and note how it ended."""
        result = await super().async_now_playing(payload)
        self.results.append(result)
        return result


class LostCommitStore(OutboxStore):
    """Run real finalization, but reject its COMMIT after the result UPDATE."""

    failed_commits = 0

    def finalize(self, item_id: str, state: DeliveryState, reason: str) -> None:
        """Inject a SQLite error; closing the real transaction rolls it back."""
        store = self
        connect = sqlite3.connect

        class RejectResultCommit(sqlite3.Connection):
            armed = False

            def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
                if sql.startswith("UPDATE plays SET state = ?"):
                    self.armed = True
                if sql == "COMMIT" and self.armed:
                    store.failed_commits += 1
                    raise sqlite3.OperationalError("synthetic result commit failure")
                return super().execute(sql, *args)

        with patch("sqlite3.connect", partial(connect, factory=RejectResultCommit)):
            super().finalize(item_id, state, reason)


def _verify_failed_commit(path: Path, store: OutboxStore) -> None:
    """Require an actual failed COMMIT and preserved data before reopening."""
    if not isinstance(store, LostCommitStore) or store.failed_commits != 1:
        raise SimulationError("the result COMMIT fault did not run exactly once")
    connection = sqlite3.connect(path)
    try:
        saved = connection.execute(
            "SELECT state, artist, title, album FROM plays"
        ).fetchall()
    finally:
        connection.close()
    metadata = PLAY.metadata
    if saved != [("in_flight", metadata.artist, metadata.title, metadata.album)]:
        raise SimulationError("the failed result COMMIT did not roll back its changes")


@dataclass(slots=True)
class Clock:
    """The time the worker sees; the pacing gate moves it instead of waiting."""

    now: datetime = FIRST_MOMENT
    pacing_waits: int = 0

    async def async_skip_to(self, moment: datetime) -> None:
        """Stand in for the real wait, so that a run does not take minutes."""
        self.pacing_waits += 1
        self.now = max(self.now, moment)

    def advance_to(self, moment: datetime) -> None:
        """Move past the moment, and at least past the spacing between calls."""
        self.now = max(self.now + timedelta(seconds=1), moment)


def _run_blocking(function: Callable[..., Any], *args: Any) -> Awaitable[Any]:
    return asyncio.get_running_loop().run_in_executor(None, function, *args)


def _row(path: Path) -> tuple[str, str, int, int, int]:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT state, reason, attempts, read_attempts, not_before FROM plays"
        ).fetchall()
    finally:
        connection.close()
    if len(rows) != 1:
        raise SimulationError(f"expected one ledger row, got {len(rows)}")
    state, reason, attempts, read_attempts, not_before = rows[0]
    return state, reason, attempts, read_attempts, not_before


def _core(
    outbox: AsyncOutbox,
    session: aiohttp.ClientSession,
    api_key: str,
    clock: Clock,
    pacer: RequestPacer,
) -> DeliveryCore:
    client = LastFmDeliveryClient(
        session, LastFmCredentials(api_key, SHARED_SECRET), SESSION_KEY, ACCOUNT_NAME
    )
    return DeliveryCore(
        outbox,
        lambda _fence: client,
        lambda fence: fence == FENCE,
        lambda: clock.now,
        random.Random(7),
        pacer,
    )


async def _async_ledger(session: aiohttp.ClientSession, api_key: str) -> dict[str, int]:
    """Ask the mock what it saw for this key; only the mock knows this method."""
    form = {"method": "simulation.ledger", "api_key": api_key, "format": "json"}
    async with session.post(lastfm.API_ROOT, data=form) as response:
        ledger: dict[str, int] = await response.json()
    return ledger


async def _async_until_stored(session: aiohttp.ClientSession, api_key: str) -> None:
    for _ in range(MOCK_POLLS):
        if (await _async_ledger(session, api_key))["stored"]:
            return
        await asyncio.sleep(MOCK_POLL_SECONDS)
    raise SimulationError("the mock did not store the play in time")


async def _async_first_process(
    core: DeliveryCore, session: aiohttp.ClientSession, scenario: Scenario
) -> None:
    """Run until the fault ends this process, with the mock already holding the play."""
    if scenario.fault is Fault.LOST_COMMIT:
        await core.async_step(FENCE)
        return
    step = asyncio.create_task(core.async_step(FENCE))
    await _async_until_stored(session, scenario.api_key)
    step.cancel()
    await asyncio.gather(step, return_exceptions=True)


async def _async_run_scenario(
    session: aiohttp.ClientSession, scenario: Scenario, directory: Path
) -> dict[str, Any]:
    path = directory / f"{scenario.name}.sqlite3"
    clock = Clock()
    pacer = RequestPacer(lambda: clock.now, clock.async_skip_to)
    store_type = LostCommitStore if scenario.fault is Fault.LOST_COMMIT else OutboxStore
    store = store_type.create(path)
    store.set_epoch(EPOCH)
    outbox = AsyncOutbox(store, _run_blocking)
    await outbox.async_enqueue(PLAY, FENCE.account_hash, EPOCH, clock.now, clock.now)
    core = _core(outbox, session, scenario.api_key, clock, pacer)
    after_fault: str | None = None
    steps = scenario.steps
    try:
        if scenario.fault is not Fault.NONE:
            await _async_first_process(core, session, scenario)
            after_fault = _row(path)[0]
            if after_fault != "in_flight":
                raise SimulationError("the interrupted write was not in_flight")
            if scenario.fault is Fault.LOST_COMMIT:
                _verify_failed_commit(path, store)
            await outbox.async_close()
            # A restart: a new store on the same file, and nothing else survives.
            outbox = AsyncOutbox(OutboxStore.open(path), _run_blocking)
            core = _core(outbox, session, scenario.api_key, clock, pacer)
        for _ in range(steps):
            await core.async_step(FENCE)
            state, _reason, _attempts, _reads, not_before = _row(path)
            if DeliveryState(state) in FINISHED:
                break
            stop = core.last_stop
            resume_at = None if stop is None else stop.resume_at
            clock.advance_to(resume_at or datetime.fromtimestamp(not_before, UTC))
    finally:
        await outbox.async_close()
    state, reason, attempts, read_attempts, _not_before = _row(path)
    ledger = await _async_ledger(session, scenario.api_key)
    return {
        "scenario": scenario.name,
        "api_key": scenario.api_key,
        "posts": ledger["posts"],
        "reads": ledger["reads"],
        "stored": ledger["stored"],
        "state": state,
        "reason": reason,
        "attempts": attempts,
        "read_attempts": read_attempts,
        "state_after_fault": after_fault,
        "last_stop": None if core.last_stop is None else core.last_stop.code,
        "pacing_waits": clock.pacing_waits,
    }


def _spotify_sample(title: str, position: float, moment: datetime) -> Any:
    """Normalize what a Sonos entity publishes for a synthetic Spotify track."""
    return normalize_state(
        "playing",
        {
            "source": "Spotify Connect",
            "media_artist": "Synthetic Artist",
            "media_title": title,
            "media_album_name": "Synthetic Album",
            "media_duration": DURATION_SECONDS,
            "media_position": position,
            "media_position_updated_at": moment,
            "volume_level": position / DURATION_SECONDS,
        },
        moment,
    )


async def _async_run_notice_scenario(
    session: aiohttp.ClientSession, scenario: NoticeScenario, directory: Path
) -> dict[str, Any]:
    path = directory / f"{scenario.name}.sqlite3"
    clock = Clock()
    store = OutboxStore.create(path)
    store.set_epoch(EPOCH)
    outbox = AsyncOutbox(store, _run_blocking)
    client = RecordingClient(
        session,
        LastFmCredentials(scenario.api_key, SHARED_SECRET),
        SESSION_KEY,
        ACCOUNT_NAME,
    )
    client.results = []
    streams = StreamSet()
    board = NowPlayingBoard()
    sender = NoticeSender(
        outbox,
        lambda _fence: client,
        lambda fence: fence == FENCE,
        lambda: clock.now,
        random.Random(7),
        RequestPacer(lambda: clock.now, clock.async_skip_to),
        board,
        asyncio.Lock(),
        lambda: None,
    )

    async def update(title: str, position: float) -> None:
        clock.advance_to(clock.now)
        now = (clock.now - FIRST_MOMENT).total_seconds()
        sample = _spotify_sample(title, position, clock.now)
        streams.apply(SOLO, {SPEAKER: sample}, now)
        board.update(streams.current_playback)
        await sender.async_run(FENCE)

    try:
        for update_number in range(REPEATED_UPDATES):
            await update("Synthetic Title", update_number * HEARTBEAT_SECONDS)
        if scenario.notices > 1:
            await update("Another Synthetic Title", 0.0)
        limited = await outbox.async_meta_get("rate_limited_until")
        auth = await outbox.async_meta_get("auth_required")
    finally:
        await outbox.async_close()
    ledger = await _async_ledger(session, scenario.api_key)
    result = client.results[0]
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
    finally:
        connection.close()
    return {
        "scenario": scenario.name,
        "outcome": result.outcome.value,
        "reason": result.reason,
        "notices": ledger["notices"],
        "gate": "rate_limited_until" if limited else "auth_required" if auth else None,
        "posts": ledger["posts"],
        "reads": ledger["reads"],
        "stored": ledger["stored"],
        "ledger_rows": rows,
    }


async def _async_run_scenarios(
    address: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    lastfm.API_ROOT = f"{address}/"
    lastfm.REQUEST_DEADLINE_SECONDS = CLIENT_DEADLINE_SECONDS
    with tempfile.TemporaryDirectory() as directory:
        async with aiohttp.ClientSession() as session:
            results = [
                await _async_run_scenario(session, scenario, Path(directory))
                for scenario in SCENARIOS
            ]
            notices = [
                await _async_run_notice_scenario(session, scenario, Path(directory))
                for scenario in NOTICE_SCENARIOS
            ]
            return results, notices


def _request_evidence(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the routing fields; raw events hold headers and bodies."""
    return [
        {key: event.get(key) for key in ("routeKey", "method", "path", "statusCode")}
        for event in events
        if event.get("event") == "request"
    ]


def _count_statuses(requests: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in requests:
        key = f"{item['routeKey']} {item['method']} {item['path']} {item['statusCode']}"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _check(
    results: list[dict[str, Any]],
    requests: list[dict[str, Any]],
    listeners: list[str],
    port_closed: bool,
) -> None:
    """Pass only on the evidence itself, never on a flag a scenario set."""
    repeated = [
        result["scenario"]
        for result in results
        if result["scenario"] in AMBIGUOUS and result["posts"] != 1
    ]
    if repeated:
        raise SimulationError(f"a second POST in ambiguity: {repeated}")
    actual = {result["scenario"]: result for result in results}
    differing = [
        scenario.name
        for scenario in SCENARIOS
        if {key: actual.get(scenario.name, {}).get(key) for key in scenario.expected()}
        != scenario.expected()
    ]
    if differing or len(results) != len(SCENARIOS):
        raise SimulationError(f"scenario outcomes differ: {differing}")
    if not requests:
        raise SimulationError("the started mock saw no requests")
    if {(item["routeKey"], item["method"], item["path"]) for item in requests} != {
        (MOCK_MEMBER, "POST", "/")
    }:
        raise SimulationError("a request took an unexpected route")
    if len(listeners) != LISTENERS or not all(
        listener.startswith("127.0.0.1:") for listener in listeners
    ):
        raise SimulationError(f"expected loopback listeners only, got {listeners}")
    if not port_closed:
        raise SimulationError("the simulation port is still open after stopping")


def _check_notices(results: list[dict[str, Any]]) -> None:
    """Pass only on what the mock and the ledger saw for each notice."""
    actual = {result["scenario"]: result for result in results}
    differing = [
        scenario.name
        for scenario in NOTICE_SCENARIOS
        if {key: actual.get(scenario.name, {}).get(key) for key in scenario.expected()}
        != scenario.expected()
    ]
    if differing or len(results) != len(NOTICE_SCENARIOS):
        raise SimulationError(f"notice outcomes differ: {differing}")


async def _async_simulate(stderr: IO[bytes]) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        "postman",
        "simulation",
        "run",
        SIMULATION,
        "--port",
        "auto",
        "--no-history",
        "--output",
        "ndjson",
        cwd=ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=stderr,
        limit=EVENT_LINE_LIMIT,
        start_new_session=True,
    )
    assert process.stdout is not None
    events: list[dict[str, Any]] = []
    ready = asyncio.Event()
    reader = asyncio.create_task(_async_read_events(process.stdout, events, ready))
    try:
        try:
            async with asyncio.timeout(START_TIMEOUT_SECONDS):
                await ready.wait()
        except TimeoutError:
            raise SimulationError("simulation startup deadline exceeded") from None
        listening = next(
            (event for event in events if event.get("event") == "listening"), None
        )
        if listening is None:
            raise SimulationError("the simulation exited before listening")
        addresses = {
            member["routeKey"]: member["address"] for member in listening["mocks"]
        }
        listeners = await _async_listeners(process.pid)
        results, notices = await _async_run_scenarios(addresses[MOCK_MEMBER])
        await asyncio.sleep(LATE_ANSWER_SECONDS)
    finally:
        try:
            await _async_stop(process)
        finally:
            try:
                async with asyncio.timeout(5.0):
                    await reader
            except TimeoutError:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                raise SimulationError("simulation event reader did not stop") from None

    requests = _request_evidence(events)
    _check(results, requests, listeners, await _async_port_is_closed(listening["port"]))
    _check_notices(notices)
    return {
        "simulation": listening["simulation"],
        "pacing": "no-wait gate injected by the runner: it moves the clock",
        "client_deadline_seconds": CLIENT_DEADLINE_SECONDS,
        "scenarios": [
            {key: value for key, value in result.items() if key != "api_key"}
            for result in results
        ],
        "now_playing": notices,
        "mock_requests": len(requests),
        "route_status_counts": _count_statuses(requests),
        "summary": next(
            (event for event in events if event.get("event") == "summary"), None
        ),
        "listeners": listeners,
        "port_closed_after_stop": True,
    }


def main() -> None:
    """Print the evidence, or fail with what is missing."""
    with tempfile.TemporaryFile() as stderr:
        try:
            evidence = asyncio.run(_async_simulate(stderr))
        except SimulationError:
            stderr.seek(0)
            sys.stderr.buffer.write(stderr.read())
            raise
    sys.stdout.write(f"{json.dumps(evidence, indent=2)}\n")


if __name__ == "__main__":
    main()
