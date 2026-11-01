"""The delivery worker: one Home Assistant-free step, and the service that runs it.

A step handles one row. Everything before the POST may stop at any moment; from the
claim on, the row is IN_FLIGHT and a lost outcome can only become UNCERTAIN, which
reconciles through reads and never reaches a claim again. Favouring a missed play
over a duplicate is the point: Last.fm has no idempotency key, so history can only
make a duplicate unlikely, and an independent writer can still land after any read.

DeliveryService owns everything around that step for one config entry: the ledger
file, the producer chain, timers and the fence, which it reads live from the entry.
NoticeSender is the best-effort side road for Now Playing: it shares the pacer and
the stored gates, keeps nothing on disk beyond those gates and never retries.

A worker runs only while scrobbling is on, and only for plays of the current period:
a row that started before the switch was turned on stays parked until its expiry.
Now Playing and scrobbling are fenced apart, so a toggle of one leaves the other
running. The worker's requests and each notice attempt take one admission lock,
because a claim sits between the pacer and a POST and can outlast the gap.
"""

import asyncio
import hashlib
import logging
import random
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Event, HassJob, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ACCOUNT_NAME,
    CONF_ENTITY_REGISTRY_IDS,
    CONF_LASTFM,
    CONF_LASTFM_LINK_REVISION,
    CONF_OUTBOX_CREATED,
    CONF_SCROBBLING_SINCE,
    DOMAIN,
    SIGNAL_LASTFM_LINK_CHANGED,
)
from .controls import (
    Feature,
    FeatureControl,
    any_enabled,
    feature_epoch,
    feature_off,
    is_enabled,
)
from .lastfm_delivery import (
    HistoryComplete,
    HistoryUnknown,
    LastFmDeliveryClient,
    NowPlayingOutcome,
    NowPlayingPayload,
    NowPlayingResult,
    ScrobbleOutcome,
    ScrobblePayload,
    ScrobbleResult,
    delivery_client,
    history_window,
    matches_track,
)
from .now_playing import NowPlayingBoard
from .outbox import (
    MAX_ACTIVE_PLAYS,
    AsyncOutbox,
    DeliveryState,
    EnqueueResult,
    OutboxClosed,
    OutboxError,
    OutboxItem,
    OutboxStatus,
    OutboxStore,
)
from .playback import CurrentPlayback, MarkerDelta, QualifiedPlay, ResumeHint
from .speaker_controls import SpeakerControl

_LOGGER = logging.getLogger(__name__)

SPOTIFY_GRACE_SECONDS = 600
POST_END_MARGIN_SECONDS = 60
BACKOFF_BASE_SECONDS = 60
BACKOFF_CAP_SECONDS = 3600
BACKOFF_JITTER = 0.2
MIN_CALL_SPACING_SECONDS = 1.0
MAINTENANCE_INTERVAL = timedelta(hours=1)

ACCOUNT_HASH_BYTES = 16

_ACCOUNT_HASH_PERSON = b"sonos-account"
# Past the cap anyway; keeps an old row's attempt count from reaching float range.
_MAX_DOUBLINGS = 7
_MAX_STORED_DIGITS = 12
_MAX_STORED_SECONDS = int(datetime.max.replace(tzinfo=UTC).timestamp())
_AUTH_REQUIRED_KEY = "auth_required"
_RATE_LIMITED_KEY = "rate_limited_until"
_EPOCH_KEY = "epoch"
_NOTICE_EPOCH_KEY = "now_playing_epoch"
_CLOCK_KEY = "clock_high_water"
_STATE_REFUSALS = frozenset({"illegal_transition", "stale_epoch", "marker_limit"})
# The row left the state the step read; younger rows are not concerned.
_ROW_REFUSALS = frozenset({"illegal_transition"})
_TERMINAL_OUTCOMES = {
    ScrobbleOutcome.ACCEPTED: DeliveryState.SENT,
    ScrobbleOutcome.IGNORED: DeliveryState.IGNORED,
    ScrobbleOutcome.PERMANENT_REJECT: DeliveryState.REJECTED,
}


class DeliveryStatus(StrEnum):
    """What delivery is doing, in fixed terms."""

    DISABLED = "disabled"
    READY = "ready"
    WAITING_GRACE = "waiting_grace"
    RETRYING = "retrying"
    AUTH_REQUIRED = "auth_required"
    STORAGE_ERROR = "storage_error"
    UNCERTAIN = "uncertain"


class StepResult(StrEnum):
    """How a step ended; a STOPPED step says why in DeliveryCore.last_stop."""

    IDLE = "idle"
    WORKED = "worked"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class Fence:
    """The link, epoch and period a step works for; any change ends its right to act.

    since is the first second of the scrobbling period; older rows stay parked.
    """

    epoch: int
    link_revision: int
    account_hash: bytes = field(repr=False)
    since: int  # Required: 0 opens the whole ledger, so it must be someone's choice.


@dataclass(frozen=True, slots=True)
class StepStop:
    """Why a step stopped: a fixed code, and when trying again makes sense.

    status None means no fault to show: the worker was fenced, closed or refused.
    Without resume_at only a new fence or a new worker can change the answer.
    """

    code: str
    status: DeliveryStatus | None = None
    resume_at: datetime | None = field(repr=False, default=None)


class RequestPacer:
    """Keep every outbound delivery request a fixed time after the previous one.

    It outlives a worker, so a pause and resume cannot shorten the gap. Marking is
    separate from waiting because a claim sits between the wait and a POST. Its
    users take turns through the service's admission lock.
    """

    def __init__(
        self,
        clock: Callable[[], datetime],
        wait_until: Callable[[datetime], Awaitable[None]],
    ) -> None:
        """Take the clock and how to wait on it; never asyncio.sleep."""
        self._clock = clock
        self._wait_until = wait_until
        self._next_at: datetime | None = None

    async def async_wait(self) -> None:
        """Return once a request may start."""
        while self._next_at is not None and self._clock() < self._next_at:
            await self._wait_until(self._next_at)

    def mark(self) -> None:
        """Start the gap; call with no await before the request itself."""
        spacing = timedelta(seconds=MIN_CALL_SPACING_SECONDS)
        self._next_at = self._clock() + spacing


class _Stopped(Exception):
    """End this step; never leaves the core."""

    def __init__(self, stop: StepStop) -> None:
        """Carry the reason to async_step."""
        super().__init__(stop.code)
        self.stop = stop


def account_hash(account_name: str) -> bytes:
    """Return the identity of a linked account, which is all the ledger stores."""
    if not account_name:
        raise ValueError("invalid account name")
    return hashlib.blake2b(
        account_name.casefold().encode(),
        digest_size=ACCOUNT_HASH_BYTES,
        person=_ACCOUNT_HASH_PERSON,
    ).digest()


def grace_deadline(play: QualifiedPlay) -> datetime:
    """Return when a direct Spotify scrobble of this play should have landed."""
    return max(
        play.qualified_at + timedelta(seconds=SPOTIFY_GRACE_SECONDS),
        play.started_at
        + timedelta(seconds=play.duration_seconds + POST_END_MARGIN_SECONDS),
    )


def backoff_seconds(attempt: int, rng: random.Random) -> float:
    """Return the wait after this failed attempt, counted from one."""
    doubled = BACKOFF_BASE_SECONDS << min(attempt - 1, _MAX_DOUBLINGS)
    jitter = rng.uniform(1 - BACKOFF_JITTER, 1 + BACKOFF_JITTER)
    return min(min(doubled, BACKOFF_CAP_SECONDS) * jitter, BACKOFF_CAP_SECONDS)


def _epoch_seconds(moment: datetime) -> int:
    return int(moment.timestamp())


def _moment(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def _stored_seconds(value: str | None) -> int | None:
    """Return a stored time, 0 if unset, or None if it cannot be trusted."""
    if value is None:
        return 0
    if not value.isdecimal() or len(value) > _MAX_STORED_DIGITS:
        return None
    seconds = int(value)
    return None if seconds > _MAX_STORED_SECONDS else seconds


def _payload(item: OutboxItem) -> ScrobblePayload | None:
    """Return the stored play exactly as it goes out, or None if it cannot."""
    try:
        return ScrobblePayload(
            artist=item.metadata.artist,
            track=item.metadata.title,
            timestamp=item.started_at,
            album=item.metadata.album,
            duration=item.duration,
        )
    except ValueError:
        return None


class DeliveryCore:
    """The single serial worker step; one instance is one worker start."""

    def __init__(
        self,
        outbox: AsyncOutbox,
        client_for: Callable[[Fence], LastFmDeliveryClient],
        is_current: Callable[[Fence], bool],
        clock: Callable[[], datetime],
        rng: random.Random,
        pacer: RequestPacer,
    ) -> None:
        """Take what a step needs; credentials stay behind client_for."""
        self._outbox = outbox
        self._client_for = client_for
        self._is_current = is_current
        self._clock = clock
        self._rng = rng
        self._pacer = pacer
        self._recovered = False
        self._bound_to: Fence | None = None
        self._high_water = 0
        self._halted: StepStop | None = None
        self._last_stop: StepStop | None = None

    @property
    def last_stop(self) -> StepStop | None:
        """Return why the last step stopped, or None if it did not."""
        return self._last_stop

    async def async_step(self, fence: Fence) -> StepResult:
        """Handle at most one row for this fence, with at most one POST."""
        self._last_stop = self._halted
        if self._halted is not None:
            return StepResult.STOPPED
        try:
            return await self._async_step(fence)
        except _Stopped as stopped:
            self._last_stop = stopped.stop
            if stopped.stop.code in _ROW_REFUSALS:
                return StepResult.WORKED
            return StepResult.STOPPED

    async def _async_step(self, fence: Fence) -> StepResult:
        self._require_current(fence)
        await self._async_start(fence)
        await self._async_check_gates(fence)
        now = self._clock()
        item = await self._store(fence, self._outbox.async_next_ready(now, fence.since))
        if item is None:
            return StepResult.IDLE
        if item.account_hash != fence.account_hash:
            await self._async_purge_other_accounts(fence)
            return StepResult.WORKED
        if item.state is not DeliveryState.UNCERTAIN and now >= item.deadline:
            await self._async_expire(fence, item)
            return StepResult.WORKED

        client = self._client_for(fence)
        window = history_window(item.started_at, item.duration)
        history = await client.async_history(*window, lambda: self._async_page(fence))
        self._require_current(fence)
        self._require_clock()
        if isinstance(history, HistoryUnknown):
            if history.rate_limited:
                await self._async_limit_calls(fence, item.read_attempts + 1)
            elif history.auth_required:
                await self._async_require_auth(fence)
        receipts = await self._async_receipts(fence, item, history, window)

        if item.state is DeliveryState.UNCERTAIN:
            await self._async_reconcile(fence, item, history, receipts)
        elif isinstance(history, HistoryUnknown):
            await self._store(
                fence,
                self._outbox.async_hold(
                    item.id, self._next_read_at(item), history.reason
                ),
            )
        elif receipts:
            await self._store(
                fence,
                self._outbox.async_finalize(
                    item.id, DeliveryState.DUPLICATE, "history_match"
                ),
            )
        else:
            await self._async_send(fence, client, item)
        return StepResult.WORKED

    async def _async_start(self, fence: Fence) -> None:
        """Settle what a previous run left, then adopt the fence."""
        if not self._recovered:
            await self._settled(self._outbox.async_recover_in_flight())
            self._recovered = True
            self._require_current(fence)
        if fence != self._bound_to:
            await self._store(
                fence, self._outbox.async_rebind(fence.epoch, fence.account_hash)
            )
            await self._async_purge_other_accounts(fence)
            self._bound_to = fence

    async def _async_purge_other_accounts(self, fence: Fence) -> None:
        await self._store(
            fence,
            self._outbox.async_purge_other_accounts(fence.account_hash, fence.since),
        )

    async def _async_check_gates(self, fence: Fence) -> None:
        """Refuse the step while the clock, the link or the limit forbids calls."""
        now = _epoch_seconds(self._clock())
        self._high_water = await self._async_stored_seconds(fence, _CLOCK_KEY)
        self._require_clock()
        if now > self._high_water:
            await self._store(fence, self._outbox.async_meta_set(_CLOCK_KEY, str(now)))
            self._high_water = now
        # The marker names the link that failed, so any relink ends it by itself.
        marker = await self._store(
            fence, self._outbox.async_meta_get(_AUTH_REQUIRED_KEY)
        )
        if marker == str(fence.link_revision):
            raise _Stopped(StepStop("auth_required", DeliveryStatus.AUTH_REQUIRED))
        limited_until = await self._async_stored_seconds(fence, _RATE_LIMITED_KEY)
        if now < limited_until:
            raise _Stopped(
                StepStop(
                    "rate_limited", DeliveryStatus.RETRYING, _moment(limited_until)
                )
            )

    async def _async_stored_seconds(self, fence: Fence, key: str) -> int:
        """Read a stored time; an unreadable one must not open a gate."""
        seconds = _stored_seconds(
            await self._store(fence, self._outbox.async_meta_get(key))
        )
        if seconds is None:
            raise self._halt("invalid_data")
        return seconds

    async def _async_receipts(
        self,
        fence: Fence,
        item: OutboxItem,
        history: HistoryComplete | HistoryUnknown,
        window: tuple[int, int],
    ) -> frozenset[int]:
        """Return dated matches, minus receipts proven to be other plays."""
        if isinstance(history, HistoryUnknown):
            return frozenset()
        others = await self._store(
            fence,
            self._outbox.async_other_sent_timestamps(item.id, item.track_key, *window),
        )
        metadata = item.metadata
        return frozenset(
            remote.timestamp
            for remote in history.scrobbles
            if matches_track(metadata.artist, metadata.title, remote)
        ).difference(others)

    async def _async_reconcile(
        self,
        fence: Fence,
        item: OutboxItem,
        history: HistoryComplete | HistoryUnknown,
        receipts: frozenset[int],
    ) -> None:
        """Decide an uncertain row from history alone; only a read touches it."""
        if item.started_at in receipts:
            finalize = self._outbox.async_finalize(
                item.id, DeliveryState.SENT, "history_exact"
            )
        elif receipts:
            finalize = self._outbox.async_finalize(
                item.id, DeliveryState.DUPLICATE, "history_match"
            )
        elif self._clock() >= item.deadline:
            finalize = self._outbox.async_finalize(
                item.id, DeliveryState.EXPIRED, "unresolved"
            )
        else:
            reason = (
                history.reason
                if isinstance(history, HistoryUnknown)
                else "history_absent"
            )
            await self._store(
                fence,
                self._outbox.async_reschedule_uncertain(
                    item.id, min(self._next_read_at(item), item.deadline), reason
                ),
            )
            return
        await self._store(fence, finalize)

    async def _async_send(
        self, fence: Fence, client: LastFmDeliveryClient, item: OutboxItem
    ) -> None:
        """Persist the attempt, recheck the gates, POST once, record the result."""
        payload = _payload(item)
        if payload is None:
            await self._store(
                fence,
                self._outbox.async_finalize(
                    item.id, DeliveryState.REJECTED, "invalid_payload"
                ),
            )
            return
        # Before the claim: a row must not sit in flight through a wait.
        await self._async_take_turn(fence)
        if self._clock() >= item.deadline:
            await self._async_expire(fence, item)
            return
        # No fence check of its own: a row claimed here must end up uncertain.
        claimed = await self._settled(
            self._outbox.async_claim_in_flight(item.id, item.state, fence.epoch)
        )
        if not claimed:
            # The row moved or lags the fence. Rebinding fixes a lag, and the next
            # step decides the row anew; a store ahead of the fence refuses here.
            self._require_current(fence)
            await self._store(
                fence, self._outbox.async_rebind(fence.epoch, fence.account_hash)
            )
            return
        if not self._is_current(fence):
            await self._settled(
                self._outbox.async_mark_uncertain(item.id, "fenced_before_send")
            )
            raise _Stopped(StepStop("fenced"))
        if self._clock_is_behind():
            await self._settled(
                self._outbox.async_mark_uncertain(item.id, "clock_behind_before_send")
            )
            raise self._clock_stop()
        if self._clock() >= item.deadline:
            await self._settled(
                self._outbox.async_mark_uncertain(item.id, "deadline_before_send")
            )
            raise _Stopped(StepStop("deadline_before_send", resume_at=self._clock()))
        self._pacer.mark()
        result = await client.async_scrobble(payload)
        await self._async_record(fence, item, result)

    async def _async_record(
        self, fence: Fence, item: OutboxItem, result: ScrobbleResult
    ) -> None:
        """Write what the POST proved; a stale step still closes its own row."""
        terminal = _TERMINAL_OUTCOMES.get(result.outcome)
        if terminal is not None:
            await self._store(
                fence, self._outbox.async_finalize(item.id, terminal, result.reason)
            )
        elif result.outcome is ScrobbleOutcome.UNCERTAIN:
            await self._store(
                fence, self._outbox.async_mark_uncertain(item.id, result.reason)
            )
        else:
            attempt = item.attempts + 1
            await self._store(
                fence,
                self._outbox.async_retry_later(
                    item.id, self._after_backoff(attempt), result.reason
                ),
            )
            if result.outcome is ScrobbleOutcome.AUTH_REQUIRED:
                await self._async_require_auth(fence)
            elif result.rate_limited:
                await self._async_limit_calls(fence, attempt)

    async def _async_expire(self, fence: Fence, item: OutboxItem) -> None:
        """End a queued row past retention; its metadata must not leave anymore."""
        await self._store(
            fence,
            self._outbox.async_finalize(item.id, DeliveryState.EXPIRED, "retention"),
        )

    async def _async_require_auth(self, fence: Fence) -> None:
        """Stop all calls on this link; the marker names it, so a relink ends it."""
        await self._store(
            fence,
            self._outbox.async_meta_set(_AUTH_REQUIRED_KEY, str(fence.link_revision)),
        )

    async def _async_limit_calls(self, fence: Fence, attempt: int) -> None:
        until = _epoch_seconds(self._after_backoff(attempt))
        await self._store(
            fence, self._outbox.async_meta_set(_RATE_LIMITED_KEY, str(until))
        )

    def _next_read_at(self, item: OutboxItem) -> datetime:
        return self._after_backoff(item.read_attempts + 1)

    def _after_backoff(self, attempt: int) -> datetime:
        delay = backoff_seconds(attempt, self._rng)
        return self._clock() + timedelta(seconds=delay)

    async def _async_take_turn(self, fence: Fence) -> None:
        """Wait for the next request slot; the wait may have outlived the fence."""
        await self._pacer.async_wait()
        self._require_current(fence)

    async def _async_page(self, fence: Fence) -> None:
        await self._async_take_turn(fence)
        self._pacer.mark()

    def _clock_is_behind(self) -> bool:
        return _epoch_seconds(self._clock()) < self._high_water

    def _require_clock(self) -> None:
        """Stop while the clock is below the highest time this worker relied on."""
        if self._clock_is_behind():
            raise self._clock_stop()

    def _clock_stop(self) -> _Stopped:
        return _Stopped(
            StepStop(
                "clock_regressed", DeliveryStatus.RETRYING, _moment(self._high_water)
            )
        )

    def _require_current(self, fence: Fence) -> None:
        if not self._is_current(fence):
            raise _Stopped(StepStop("fenced"))

    async def _store[Result](self, fence: Fence, call: Awaitable[Result]) -> Result:
        """Await a store call and stop if the fence moved meanwhile."""
        result = await self._settled(call)
        self._require_current(fence)
        return result

    async def _settled[Result](self, call: Awaitable[Result]) -> Result:
        """Await a store call; whatever it cannot promise stops the step."""
        try:
            return await call
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            # Only the executor wrapper was cancelled: the write may have landed
            # or been given up on, and a next write could overtake it.
            raise self._halt("outcome_unknown") from None
        except OutboxClosed as error:
            raise _Stopped(StepStop(error.code)) from None
        except OutboxError as error:
            if error.code in _STATE_REFUSALS:
                raise _Stopped(StepStop(error.code)) from None
            raise self._halt(error.code) from None
        except RuntimeError:
            # The store boundary lets only fixed codes out, so this is the
            # executor refusing work during shutdown.
            raise self._halt("executor_gone") from None

    def _halt(self, code: str) -> _Stopped:
        """End this worker: nothing may be sent until storage is verified anew."""
        self._halted = StepStop(code, DeliveryStatus.STORAGE_ERROR)
        return _Stopped(self._halted)


class NoticeSender:
    """Send the board's notices one at a time for one fence; never a retry.

    Each attempt holds the admission lock from its wait to its recorded result, so it
    sees what the worker's last step stored. Nothing awaits between reading the board
    and the POST, so a notice cannot outlive its track in a wait. A request on the
    wire cannot be recalled. A closed gate drops the notice instead of keeping it for
    later, because by then it would announce the past.
    """

    def __init__(
        self,
        outbox: AsyncOutbox,
        client_for: Callable[[Fence], LastFmDeliveryClient],
        is_current: Callable[[Fence], bool],
        clock: Callable[[], datetime],
        rng: random.Random,
        pacer: RequestPacer,
        board: NowPlayingBoard,
        admission: asyncio.Lock,
        on_auth_required: Callable[[], None],
    ) -> None:
        """Take what a run needs, like a worker; credentials stay behind client_for."""
        self._outbox = outbox
        self._client_for = client_for
        self._is_current = is_current
        self._clock = clock
        self._rng = rng
        self._pacer = pacer
        self._board = board
        self._admission = admission
        self._on_auth_required = on_auth_required

    async def async_run(self, fence: Fence) -> None:
        """Send until the board has nothing ready or the fence ends."""
        while self._is_current(fence) and self._board.ready is not None:
            async with self._admission:
                await self._async_attempt(fence)

    async def _async_attempt(self, fence: Fence) -> None:
        """Take a turn and send what is ready by then, if anything may leave."""
        await self._pacer.async_wait()
        gates_open = await self._async_gates_open(fence)
        if not self._is_current(fence):
            return
        notice = self._board.take()
        if notice is None or not gates_open:
            return
        client = self._client_for(fence)
        payload = _notice_payload(notice)
        self._pacer.mark()
        await self._async_record(fence, await client.async_now_playing(payload))

    async def _async_gates_open(self, fence: Fence) -> bool:
        """Read the worker's stored gates; a value it would refuse stays closed."""
        marker = await self._outbox.async_meta_get(_AUTH_REQUIRED_KEY)
        limited_until = _stored_seconds(
            await self._outbox.async_meta_get(_RATE_LIMITED_KEY)
        )
        high_water = _stored_seconds(await self._outbox.async_meta_get(_CLOCK_KEY))
        now = _epoch_seconds(self._clock())
        if marker == str(fence.link_revision):
            self._on_auth_required()
            return False
        return (
            limited_until is not None
            and high_water is not None
            and limited_until <= now
            and high_water <= now
        )

    async def _async_record(self, fence: Fence, result: NowPlayingResult) -> None:
        """Keep only what stops every request; the notice itself leaves no trace."""
        if result.outcome is NowPlayingOutcome.AUTH_REQUIRED:
            await self._outbox.async_meta_set(
                _AUTH_REQUIRED_KEY, str(fence.link_revision)
            )
            self._on_auth_required()
        elif result.rate_limited:
            await self._async_extend_cooldown()

    async def _async_extend_cooldown(self) -> None:
        """Start the first backoff step, unless a longer cooldown already runs."""
        delay = timedelta(seconds=backoff_seconds(1, self._rng))
        until = _epoch_seconds(self._clock() + delay)
        stored = await self._outbox.async_meta_get(_RATE_LIMITED_KEY)
        limited_until = _stored_seconds(stored)
        if limited_until is not None and limited_until < until:
            await self._outbox.async_meta_set(_RATE_LIMITED_KEY, str(until))


def _notice_payload(notice: CurrentPlayback) -> NowPlayingPayload:
    duration = round(notice.duration_seconds or 0)
    metadata = notice.metadata
    return NowPlayingPayload(
        artist=metadata.artist,
        track=metadata.title,
        album=metadata.album,
        duration=duration if duration > 0 else None,
    )


# Highest first: a stored fault outranks the switch, which outranks what the
# worker is doing; an uncertain play outranks a wait.
_STATUS_PRIORITY = (
    DeliveryStatus.STORAGE_ERROR,
    DeliveryStatus.DISABLED,
    DeliveryStatus.AUTH_REQUIRED,
    DeliveryStatus.UNCERTAIN,
    DeliveryStatus.RETRYING,
    DeliveryStatus.WAITING_GRACE,
    DeliveryStatus.READY,
)
# Everything the status may name; any other code reads as an internal error.
PUBLISHED_ERROR_CODES = frozenset(
    {
        "queue_full",
        "clock_regressed",
        "rate_limited",
        "auth_required",
        "storage_error",
        "storage_corrupt",
        "storage_missing",
        "storage_exists",
        "storage_permissions",
        "schema_mismatch",
        "invalid_data",
        "outcome_unknown",
        "executor_gone",
        "worker_failed",
        "stale_epoch",
    }
)


def resolve_status(
    *, failed: bool, enabled: bool, activity: DeliveryStatus, uncertain: bool
) -> DeliveryStatus:
    """Return the highest-priority status that applies right now."""
    applicable = {activity}
    if failed:
        applicable.add(DeliveryStatus.STORAGE_ERROR)
    if not enabled:
        applicable.add(DeliveryStatus.DISABLED)
    if uncertain:
        applicable.add(DeliveryStatus.UNCERTAIN)
    return min(applicable, key=_STATUS_PRIORITY.index)


@dataclass(frozen=True, slots=True)
class DeliveryStatusSnapshot:
    """What delivery is doing: a status, counts and one fixed code, no play."""

    status: DeliveryStatus
    error_code: str | None = None
    pending_count: int = 0
    held_count: int = 0
    uncertain_count: int = 0
    sent_total: int = 0
    skipped_total: int = 0
    ignored_total: int = 0
    oldest_queue_age_minutes: int | None = None


def ledger_path(hass: HomeAssistant, entry_id: str) -> Path:
    """Return the private file of this entry; nothing else ever lives in it."""
    return Path(hass.config.path(".storage", f"{DOMAIN}_{entry_id}.sqlite3"))


def delivery_service(hass: HomeAssistant, entry_id: str) -> DeliveryService | None:
    """Return the service of an entry until its ledger is closed."""
    services: dict[str, DeliveryService] = hass.data.get(DOMAIN, {})
    return services.get(entry_id)


def _delete_ledger(path: Path) -> None:
    path.unlink(missing_ok=True)
    path.with_name(f"{path.name}-journal").unlink(missing_ok=True)


async def async_remove_ledger(hass: HomeAssistant, entry_id: str) -> None:
    """Delete the ledger once nothing can write it anymore.

    A cancelled stop raises here, so the delete is never reached: a file that may
    still be written is not free, and deleting it could let it come back.
    """
    if (service := delivery_service(hass, entry_id)) is not None:
        await service.async_stop()
    await hass.async_add_executor_job(_delete_ledger, ledger_path(hass, entry_id))


class DeliveryService:
    """Run delivery for one entry; an instance lives from setup to its close.

    One worker runs at a time and every fence change ends it, so a stopped core is
    never reused: it would not recover the row a cancelled POST left in flight.
    Lifecycle steps take the lock and fence before they queue for it. Producers run
    in one FIFO chain with maintenance, because a write that overtakes another can
    revive an ended marker or lose an alias.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
        """Prepare the service; no file is touched before async_start."""
        self._hass = hass
        self._entry = entry
        self._path = ledger_path(hass, entry.entry_id)
        self._pacer = RequestPacer(dt_util.utcnow, self._async_wait_until)
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._outbox: AsyncOutbox | None = None
        self._worker: asyncio.Task[None] | None = None
        self._worker_fence: Fence | None = None
        self._admission = asyncio.Lock()
        self._board = NowPlayingBoard()
        self._notice_fence: Fence | None = None
        self._notices: asyncio.Task[None] | None = None
        self._notices_fence: Fence | None = None
        self._jobs: deque[Callable[[AsyncOutbox], Awaitable[None]]] = deque()
        self._chain: asyncio.Task[None] | None = None
        self._reconciling: asyncio.Task[None] | None = None
        self._stopper: asyncio.Task[None] | None = None
        self._cancel_maintenance: CALLBACK_TYPE | None = None
        self._unsubscribe_change: CALLBACK_TYPE | None = None
        self._unsubscribe_stop: CALLBACK_TYPE | None = None
        self._listeners: set[Callable[[], None]] = set()
        self._resume: tuple[tuple[ResumeHint, ...], frozenset[str]] = ((), frozenset())
        self._committed_epoch: int | None = None
        self._committed_notice_epoch: int | None = None
        self._clearing = 0
        self._stopping = False
        self._closed = False
        self._ledger_ready = False
        self._failure: str | None = None
        self._activity = DeliveryStatusSnapshot(DeliveryStatus.READY)
        self._notice_gates = DeliveryStatusSnapshot(DeliveryStatus.READY)
        self._cancel_gate_wake: CALLBACK_TYPE | None = None
        self._counts: OutboxStatus | None = None
        self._queue_full = False
        self._published = self.status
        self.controls = FeatureControl(
            hass,
            entry,
            self.async_prepare_ledger,
            self.async_settle,
            self._async_latest_queued_start,
            lambda: not self._stopping,
        )
        self.speakers = SpeakerControl(hass, entry, self.controls)
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = self

    @property
    def status(self) -> DeliveryStatusSnapshot:
        """Return the current status."""
        counts = self._counts
        fence = self._entry_fence()
        enabled = fence is not None or self._entry_notice_fence() is not None
        status = resolve_status(
            failed=self._failure is not None,
            enabled=enabled,
            activity=self._activity.status,
            # Nothing reconciles a parked row, so it says nothing about now.
            uncertain=(
                fence is not None
                and counts is not None
                and counts.reconcilable_count > 0
            ),
        )
        code = self._failure or (
            "queue_full"
            if self._queue_full
            else (self._activity.error_code if enabled else None)
        )
        return DeliveryStatusSnapshot(
            status,
            None
            if code is None
            else (code if code in PUBLISHED_ERROR_CODES else "storage_error"),
            *(
                ()
                if counts is None
                else (
                    counts.pending_count,
                    counts.held_count,
                    counts.uncertain_count,
                    counts.sent_total,
                    counts.skipped_total,
                    counts.ignored_total,
                    None
                    if counts.oldest_queue_age_seconds is None
                    else counts.oldest_queue_age_seconds // 60,
                )
            ),
        )

    async def _async_refresh_counts(self, outbox: AsyncOutbox) -> None:
        """Read the counts and publish; called after worker steps and writes."""
        fence = self._entry_fence()
        self._counts = await self._async_settled(
            outbox.async_status(dt_util.utcnow(), 0 if fence is None else fence.since)
        )
        self._queue_full = self._queue_full and (
            self._counts.pending_count
            + self._counts.held_count
            + self._counts.uncertain_count
            >= MAX_ACTIVE_PLAYS
        )
        self._async_publish()

    @property
    def resume_state(self) -> tuple[tuple[ResumeHint, ...], frozenset[str]]:
        """Return the stored markers; read after async_start, before the runtime."""
        return self._resume

    @property
    def closed(self) -> bool:
        """Return whether the ledger was closed, so that no thread can write it."""
        return self._closed

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Call listener after every status change."""
        self._listeners.add(listener)

        @callback
        def remove_listener() -> None:
            self._listeners.discard(listener)

        return remove_listener

    async def async_start(self) -> None:
        """Verify the ledger and run what it needs; a fault becomes a status.

        Without a ledger nothing is created: only enabling does that.
        """
        self._unsubscribe_change = async_dispatcher_connect(
            self._hass,
            SIGNAL_LASTFM_LINK_CHANGED.format(self._entry.entry_id),
            self._async_handle_change,
        )
        self._unsubscribe_stop = self._hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._async_handle_stop
        )
        if not self._entry.data.get(CONF_OUTBOX_CREATED):
            if any_enabled(self._entry.data):
                self._async_fail("storage_missing")
            return
        async with self._lock:
            outbox = self._async_attach()
            try:
                await self._async_settled(outbox.async_open())
                self._ledger_ready = True
                await self._async_settled(
                    outbox.async_purge_lineages(
                        frozenset(self._entry.data[CONF_ENTITY_REGISTRY_IDS])
                    )
                )
                self._resume = await self._async_settled(outbox.async_resume_state())
                await self._async_settled(outbox.async_maintain(dt_util.utcnow()))
                await self._async_commit_fence(outbox)
                await self._async_refresh_counts(outbox)
                await self._async_read_notice_gates(outbox)
            except OutboxClosed:
                return
            except OutboxError as error:
                self._async_fail(error.code)
                return
            self._async_start_maintenance()
            self._async_start_worker()

    async def async_stop(self) -> None:
        """Stop for unload, removal and STOP alike, in one order; safe to repeat."""
        if self._closed:
            return
        if self._stopper is None or self._stopper.done():
            self._stopper = self._hass.async_create_task(
                self._async_shut_down(), "personal_sonos_scrobbler delivery stop"
            )
        await asyncio.shield(self._stopper)

    async def async_prepare_ledger(self) -> None:
        """Create or verify the ledger before an enable; raises a fixed code."""
        async with self._lock:
            if self._stopping:
                raise OutboxClosed("outbox_closed")
            if self._failure is not None:
                raise OutboxError(self._failure)
            if self._outbox is not None:
                return
            outbox = self._async_attach()
            try:
                try:
                    await self._async_settled(outbox.async_create())
                except OutboxError as error:
                    # A crash after create can lose the flag: a whole ledger is
                    # kept, anything else fails the check and stays as it is.
                    if error.code != "storage_exists":
                        raise
                    await self._async_settled(outbox.async_open())
            except OutboxError as error:
                self._async_fail(error.code)
                raise
            self._ledger_ready = True
            self._hass.config_entries.async_update_entry(
                self._entry, data={**self._entry.data, CONF_OUTBOX_CREATED: True}
            )
            self._async_start_maintenance()

    async def async_settle(self) -> None:
        """Return once both live epochs are durable; raises a fixed code otherwise."""
        while self._reconciling is not None and not self._reconciling.done():
            await asyncio.wait({self._reconciling})
        if not self._entry.data.get(CONF_OUTBOX_CREATED):
            return
        if (
            self._committed_epoch != self._entry_epoch()
            or self._committed_notice_epoch != self._entry_notice_epoch()
        ):
            raise OutboxError(self._failure or "storage_error")

    async def _async_latest_queued_start(self) -> int | None:
        """Return the newest start among the active rows; raises a fixed code."""
        if self._outbox is None:
            raise OutboxError(self._failure or "storage_error")
        return await self._async_settled(self._outbox.async_latest_active_start())

    async def async_clear_queue(self) -> None:
        """Erase queued metadata; a return means the erasure committed."""
        self._clearing += 1
        self._async_cancel_worker()
        try:
            async with self._lock:
                outbox = self._outbox
                if self._stopping or self._failure is not None or outbox is None:
                    raise OutboxError(self._failure or "storage_error")
                try:
                    await self._async_reap_worker()
                    await self._async_drain_chain()
                    await outbox.async_drain()
                    await self._async_settled(outbox.async_recover_in_flight())
                    await self._async_settled(outbox.async_clear_queue())
                    self._queue_full = False
                    await self._async_refresh_counts(outbox)
                    await self._async_read_notice_gates(outbox)
                except OutboxError as error:
                    self._async_fail(error.code)
                    raise
        finally:
            self._clearing -= 1
        self._async_start_worker()

    @callback
    def async_accept(self, play: QualifiedPlay) -> None:
        """Take a play from the runtime; never raises into it."""
        fence = self._live_fence()
        if (
            fence is None
            or play.started_at.timestamp() < self._entry.data[CONF_SCROBBLING_SINCE]
        ):
            return
        now = dt_util.utcnow()

        async def enqueue(outbox: AsyncOutbox) -> None:
            await self._async_enqueue(outbox, play, fence, now)

        self._async_submit(enqueue)

    @callback
    def async_on_occurrence_change(self, delta: MarkerDelta) -> None:
        """Take a marker change from the runtime; never raises into it."""
        now = dt_util.utcnow()

        async def apply(outbox: AsyncOutbox) -> None:
            await self._async_settled(outbox.async_apply_marker_delta(delta, now))

        self._async_submit(apply)

    @callback
    def async_on_current_playback(self, playback: tuple[CurrentPlayback, ...]) -> None:
        """Take the current state from the runtime; never raises into it."""
        self._board.update(playback)
        self._async_pump_notices()

    def _entry_fence(self) -> Fence | None:
        """Return the scrobbling fence the entry describes, read live."""
        data: Mapping[str, Any] = self._entry.data
        since = data.get(CONF_SCROBBLING_SINCE)
        if not is_enabled(data, Feature.SCROBBLING) or since is None:
            return None
        return self._fence(Feature.SCROBBLING, int(since))

    def _entry_notice_fence(self) -> Fence | None:
        """Return the Now Playing fence the entry describes, read live."""
        if not is_enabled(self._entry.data, Feature.NOW_PLAYING):
            return None
        return self._fence(Feature.NOW_PLAYING, 0)

    def _fence(self, feature: Feature, since: int) -> Fence | None:
        data: Mapping[str, Any] = self._entry.data
        link = data.get(CONF_LASTFM)
        if link is None:
            return None
        return Fence(
            feature_epoch(data, feature),
            data.get(CONF_LASTFM_LINK_REVISION, 0),
            account_hash(link[CONF_ACCOUNT_NAME]),
            since,
        )

    def _entry_epoch(self) -> int:
        return feature_epoch(self._entry.data, Feature.SCROBBLING)

    def _entry_notice_epoch(self) -> int:
        return feature_epoch(self._entry.data, Feature.NOW_PLAYING)

    def _may_work(self) -> bool:
        return not (self._stopping or self._clearing or self._failure is not None)

    def _live_fence(self) -> Fence | None:
        """Return the fence the worker may run under right now, if any."""
        return self._entry_fence() if self._may_work() else None

    def _live_notice_fence(self) -> Fence | None:
        """Return the fence notices may run under right now, if any."""
        return self._entry_notice_fence() if self._may_work() else None

    def _is_current(self, fence: Fence) -> bool:
        return fence == self._live_fence()

    def _is_notice_current(self, fence: Fence) -> bool:
        return fence == self._live_notice_fence()

    def _client_for(self, _fence: Fence) -> LastFmDeliveryClient:
        """Build the client; a step only asks while its fence and link hold."""
        return delivery_client(
            async_get_clientsession(self._hass), self._entry.data[CONF_LASTFM]
        )

    @callback
    def _async_attach(self) -> AsyncOutbox:
        """Bind to the file; held on failure too, so that stop closes its thread."""
        outbox = AsyncOutbox(OutboxStore(self._path), self._hass.async_add_executor_job)
        self._outbox = outbox
        return outbox

    async def _async_settled[Result](self, call: Awaitable[Result]) -> Result:
        """Await a store call; what it cannot promise becomes a fixed code."""
        try:
            return await call
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            # Only the executor wrapper was cancelled: the write may have landed
            # or been given up on. Recovery is for the next start to read.
            raise OutboxError("outcome_unknown") from None
        except OutboxError:
            raise
        except RuntimeError:
            raise OutboxError("executor_gone") from None

    async def _async_commit_fence(self, outbox: AsyncOutbox) -> None:
        """Make both live epochs durable; a moved one found its worker stopped."""
        if self._committed_epoch != self._entry_epoch():
            await self._async_settled(outbox.async_recover_in_flight())
            stored = await self._async_settled(outbox.async_meta_get(_EPOCH_KEY))
            if stored is not None and int(stored) > self._entry_epoch():
                self._async_keep_lost_off(Feature.SCROBBLING, int(stored))
            epoch = self._entry_epoch()
            await self._async_settled(outbox.async_set_epoch(epoch))
            self._committed_epoch = epoch
        if self._committed_notice_epoch != self._entry_notice_epoch():
            await self._async_commit_notice_epoch(outbox)

    async def _async_commit_notice_epoch(self, outbox: AsyncOutbox) -> None:
        stored = _stored_seconds(
            await self._async_settled(outbox.async_meta_get(_NOTICE_EPOCH_KEY))
        )
        if stored is None:
            raise OutboxError("invalid_data")
        if stored > self._entry_notice_epoch():
            self._async_keep_lost_off(Feature.NOW_PLAYING, stored)
        epoch = self._entry_notice_epoch()
        await self._async_settled(outbox.async_meta_set(_NOTICE_EPOCH_KEY, str(epoch)))
        self._committed_notice_epoch = epoch

    @callback
    def _async_keep_lost_off(self, feature: Feature, stored: int) -> None:
        """The ledger saw an off the delayed config save lost: it stands."""
        self._hass.config_entries.async_update_entry(
            self._entry, data=feature_off(self._entry.data, feature, stored + 1)
        )

    @callback
    def _async_handle_change(self) -> None:
        """Fence what changed at once; a new worker waits for the durable commit."""
        current = asyncio.current_task()
        for task, stale in (
            (self._worker, self._worker_is_stale()),
            (self._notices, self._notices_are_stale()),
        ):
            if stale and task is not None and task is not current:
                task.cancel()
        self._async_reconcile()

    def _worker_is_stale(self) -> bool:
        return self._worker_fence != self._live_fence()

    def _notices_are_stale(self) -> bool:
        return self._notices_fence != self._live_notice_fence()

    @callback
    def _async_reconcile(self) -> None:
        self._reconciling = self._hass.async_create_task(
            self._async_run_reconcile(), "personal_sonos_scrobbler delivery reconcile"
        )

    async def _async_run_reconcile(self) -> None:
        async with self._lock:
            await self._async_reap_stale()
            outbox = self._outbox
            if outbox is None or self._stopping or self._failure is not None:
                self._async_publish()
                return
            try:
                # A worker that stays keeps its calls; nothing below touches them.
                if self._worker is None:
                    await outbox.async_drain()
                # A publication of an already durable epoch commits nothing.
                await self._async_commit_fence(outbox)
                await self._async_refresh_counts(outbox)
                await self._async_read_notice_gates(outbox)
            except OutboxClosed:
                return
            except OutboxError as error:
                self._async_fail(error.code)
                return
            self._async_start_worker()

    async def _async_read_notice_gates(self, outbox: AsyncOutbox) -> None:
        """Show the stored stops of Now Playing alone; a worker reports its own.

        Without a worker nothing else reads them, so the status would say ready until
        the next notice is refused. An unreadable value fails as it does for a worker.
        Read from the entry, because a clear that is about to end reads them too.
        """
        self._async_cancel_gate_wake()
        fence = self._entry_notice_fence()
        if (
            self._stopping
            or self._failure is not None
            or fence is None
            or self._entry_fence() is not None
        ):
            return
        marker = await self._async_settled(outbox.async_meta_get(_AUTH_REQUIRED_KEY))
        limited_until = _stored_seconds(
            await self._async_settled(outbox.async_meta_get(_RATE_LIMITED_KEY))
        )
        high_water = _stored_seconds(
            await self._async_settled(outbox.async_meta_get(_CLOCK_KEY))
        )
        if limited_until is None or high_water is None:
            raise OutboxError("invalid_data")
        now = _epoch_seconds(dt_util.utcnow())
        if marker == str(fence.link_revision):
            self._notice_gates = DeliveryStatusSnapshot(
                DeliveryStatus.AUTH_REQUIRED, "auth_required"
            )
        elif now < high_water:
            self._async_show_notice_wait("clock_regressed", high_water - now)
        elif now < limited_until:
            self._async_show_notice_wait("rate_limited", limited_until - now)
        else:
            self._notice_gates = DeliveryStatusSnapshot(DeliveryStatus.READY)

    @callback
    def _async_show_notice_wait(self, code: str, seconds: int) -> None:
        """Show a stop that ends by itself, and read the gates again when it does."""
        # A read that outlived the shutdown must not arm a timer nobody cancels.
        if self._stopping:
            return
        self._notice_gates = DeliveryStatusSnapshot(DeliveryStatus.RETRYING, code)
        self._cancel_gate_wake = async_call_later(
            self._hass,
            min(seconds, BACKOFF_CAP_SECONDS),
            HassJob(
                self._async_handle_gate_wake,
                "personal_sonos_scrobbler delivery gates",
                cancel_on_shutdown=True,
            ),
        )

    @callback
    def _async_handle_gate_wake(self, _now: datetime) -> None:
        self._cancel_gate_wake = None
        self._async_reconcile()

    @callback
    def _async_cancel_gate_wake(self) -> None:
        if self._cancel_gate_wake is not None:
            self._cancel_gate_wake()
            self._cancel_gate_wake = None

    @callback
    def _async_start_worker(self) -> None:
        self._async_pump_notices()
        fence = self._live_fence()
        outbox = self._outbox
        if fence is None and self._live_notice_fence() is not None:
            # Without a worker nothing else clears what the last one was doing.
            self._async_set_activity(
                self._notice_gates.status, self._notice_gates.error_code
            )
            return
        if fence is None or outbox is None or self._worker is not None:
            self._async_publish()
            return
        core = DeliveryCore(
            outbox,
            self._client_for,
            self._is_current,
            dt_util.utcnow,
            random.Random(),
            self._pacer,
        )
        self._activity = DeliveryStatusSnapshot(DeliveryStatus.READY)
        self._async_publish()
        self._worker_fence = fence
        self._worker = self._entry.async_create_background_task(
            self._hass,
            self._async_work(core, fence, outbox),
            "personal_sonos_scrobbler delivery worker",
            eager_start=False,
        )

    @callback
    def _async_pump_notices(self) -> None:
        """Start the sender if a notice is ready and the durable fence allows it.

        Only the entry's own fence resets the board: a new enable or account
        announces what plays now, whereas scrobbling or a clear is no new start.
        """
        entry_fence = self._entry_notice_fence()
        if entry_fence != self._notice_fence:
            self._notice_fence = entry_fence
            self._board.reset()
        fence = self._live_notice_fence()
        outbox = self._outbox
        if (
            fence is None
            or outbox is None
            or not self._ledger_ready
            or fence.epoch != self._committed_notice_epoch
            or self._board.ready is None
            or (self._notices is not None and not self._notices.done())
        ):
            return
        sender = NoticeSender(
            outbox,
            self._client_for,
            self._is_notice_current,
            dt_util.utcnow,
            random.Random(),
            self._pacer,
            self._board,
            self._admission,
            self._async_handle_notice_auth,
        )
        self._notices_fence = fence
        self._notices = self._entry.async_create_background_task(
            self._hass,
            self._async_send_notices(sender, fence),
            "personal_sonos_scrobbler now playing",
            eager_start=False,
        )

    @callback
    def _async_handle_notice_auth(self) -> None:
        """Show a dead link at once; the worker, if any, confirms it from the marker."""
        self._wake.set()
        self._async_set_activity(DeliveryStatus.AUTH_REQUIRED, "auth_required")

    async def _async_send_notices(self, sender: NoticeSender, fence: Fence) -> None:
        """Run the sender; its faults cost a notice and never stop delivery."""
        try:
            await sender.async_run(fence)
        except OutboxClosed:
            return
        except OutboxError as error:
            _LOGGER.warning("Now playing notice stopped: %s", error.code)
        except RuntimeError:
            # The store boundary lets only fixed codes out; see DeliveryCore._settled.
            _LOGGER.warning("Now playing notice stopped: executor_gone")
        except Exception as error:
            # Only the class: its message may quote listening data.
            _LOGGER.error("Now playing notice failed: %s", type(error).__name__)

    async def _async_work(
        self, core: DeliveryCore, fence: Fence, outbox: AsyncOutbox
    ) -> None:
        """Step until the fence ends; each wait ends on a timer or a commit."""
        try:
            while True:
                self._wake.clear()
                async with self._admission:
                    result = await core.async_step(fence)
                await self._async_refresh_counts(outbox)
                if result is StepResult.WORKED:
                    self._async_set_activity(DeliveryStatus.READY)
                elif result is StepResult.IDLE:
                    wake_at = await self._async_settled(
                        outbox.async_next_wake(fence.since)
                    )
                    # Grace is for rows that have made no attempt yet; a backoff
                    # or a read hold is a retry.
                    retrying = wake_at is not None and await self._async_settled(
                        outbox.async_next_wake_is_retry(fence.since)
                    )
                    self._async_set_activity(
                        DeliveryStatus.READY
                        if wake_at is None
                        else (
                            DeliveryStatus.RETRYING
                            if retrying
                            else DeliveryStatus.WAITING_GRACE
                        )
                    )
                    await self._async_wait_for_work(wake_at)
                elif not await self._async_after_stop(core):
                    return
        except OutboxClosed:
            return
        except OutboxError as error:
            self._async_fail(error.code)
        except Exception as error:
            # Only the class: its message may quote listening data.
            _LOGGER.error("Delivery worker failed: %s", type(error).__name__)
            self._async_fail("worker_failed")

    async def _async_after_stop(self, core: DeliveryCore) -> bool:
        """Answer a stopped step; False ends the worker."""
        stop = core.last_stop
        if stop is None or stop.code == "fenced":
            return False
        if stop.status is DeliveryStatus.STORAGE_ERROR:
            self._async_fail(stop.code)
            return False
        if stop.code == "stale_epoch":
            self._async_reconcile()
            return False
        if stop.status is not None:
            self._async_set_activity(stop.status, stop.code)
        if stop.resume_at is None:
            return False
        await self._async_wait_for_work(stop.resume_at)
        return True

    async def _async_wait_for_work(self, moment: datetime | None) -> None:
        """Wait for a commit, or until moment; a cancelled wait leaves no timer."""
        if moment is None:
            await self._wake.wait()
            return
        seconds = (moment - dt_util.utcnow()).total_seconds()
        # A second at least: a due row can still be behind next_ready's rounding.
        delay = min(max(seconds, 1.0), BACKOFF_CAP_SECONDS)
        cancel = async_call_later(
            self._hass,
            delay,
            HassJob(
                self._async_handle_timer,
                "personal_sonos_scrobbler delivery wake",
                cancel_on_shutdown=True,
            ),
        )
        try:
            await self._wake.wait()
        finally:
            cancel()

    @callback
    def _async_handle_timer(self, _now: datetime) -> None:
        self._wake.set()

    async def _async_wait_until(self, moment: datetime) -> None:
        """Wait on a Home Assistant timer, so that a stop or a frozen clock works."""
        reached = asyncio.Event()

        @callback
        def handle(_now: datetime) -> None:
            reached.set()

        cancel = async_call_later(
            self._hass,
            max((moment - dt_util.utcnow()).total_seconds(), 0.0),
            HassJob(
                handle,
                "personal_sonos_scrobbler delivery pace",
                cancel_on_shutdown=True,
            ),
        )
        try:
            await reached.wait()
        finally:
            cancel()

    @callback
    def _async_submit(self, job: Callable[[AsyncOutbox], Awaitable[None]]) -> None:
        """Append to the producer chain; without a ledger there is nothing to do."""
        if (
            self._outbox is None
            or not self._ledger_ready
            or self._stopping
            or self._failure is not None
        ):
            return
        self._jobs.append(job)
        if self._chain is None or self._chain.done():
            self._chain = self._hass.async_create_task(
                self._async_run_chain(self._outbox),
                "personal_sonos_scrobbler delivery producers",
                eager_start=False,
            )

    async def _async_run_chain(self, outbox: AsyncOutbox) -> None:
        """Run one write at a time; one with no answer stops all after it."""
        while self._jobs:
            job = self._jobs.popleft()
            try:
                await job(outbox)
            except OutboxClosed:
                self._jobs.clear()
            except OutboxError as error:
                if error.code in _STATE_REFUSALS:
                    _LOGGER.warning("Delivery write refused: %s", error.code)
                    continue
                self._jobs.clear()
                self._async_fail(error.code)

    async def _async_enqueue(
        self, outbox: AsyncOutbox, play: QualifiedPlay, fence: Fence, now: datetime
    ) -> None:
        if fence != self._entry_fence():
            return
        result = await self._async_settled(
            outbox.async_enqueue(
                play, fence.account_hash, fence.epoch, now, grace_deadline(play)
            )
        )
        if result is EnqueueResult.ENQUEUED:
            self._queue_full = False
            self._wake.set()
        elif result is EnqueueResult.QUEUE_FULL:
            _LOGGER.warning("Delivery queue is full; a play was not queued")
            self._queue_full = True
        await self._async_refresh_counts(outbox)

    async def _async_drain_chain(self) -> None:
        while self._chain is not None and not self._chain.done():
            await asyncio.wait({self._chain})

    @callback
    def _async_cancel_worker(self) -> None:
        current = asyncio.current_task()
        for task in (self._worker, self._notices):
            if task is not None and task is not current:
                task.cancel()

    async def _async_reap_worker(self) -> None:
        """Cancel the worker and sender, see them end; threads drain separately."""
        worker = self._worker
        for task in (worker, self._notices):
            if task is not None:
                task.cancel()
                await asyncio.wait({task})
        self._worker = None
        self._worker_fence = None
        self._notices = None
        self._notices_fence = None

    async def _async_reap_stale(self) -> None:
        """See off only what a change ended or what already stopped by itself."""
        worker = self._worker
        if worker is not None and (worker.done() or self._worker_is_stale()):
            worker.cancel()
            await asyncio.wait({worker})
            self._worker = None
            self._worker_fence = None
        notices = self._notices
        if notices is not None and (notices.done() or self._notices_are_stale()):
            notices.cancel()
            await asyncio.wait({notices})
            # A new track can start the next sender during that wait.
            if self._notices is notices:
                self._notices = None
                self._notices_fence = None

    @callback
    def _async_start_maintenance(self) -> None:
        # A step that outlived the shutdown must not arm a timer nobody cancels.
        if self._stopping:
            return
        if self._cancel_maintenance is None:
            self._cancel_maintenance = async_track_time_interval(
                self._hass,
                self._async_handle_maintenance,
                MAINTENANCE_INTERVAL,
                cancel_on_shutdown=True,
            )

    @callback
    def _async_handle_maintenance(self, now: datetime) -> None:
        async def maintain(outbox: AsyncOutbox) -> None:
            await self._async_settled(outbox.async_maintain(now))
            await self._async_refresh_counts(outbox)

        self._async_submit(maintain)

    @callback
    def _async_handle_stop(self, _event: Event) -> None:
        self._unsubscribe_stop = None
        self._hass.async_create_task(
            self.async_stop(), "personal_sonos_scrobbler delivery stop"
        )

    async def _async_shut_down(self) -> None:
        """Fence, cancel timers, stop the worker, finish the chain, close."""
        self._stopping = True
        for unsubscribe in (
            self._cancel_maintenance,
            self._unsubscribe_change,
            self._unsubscribe_stop,
        ):
            if unsubscribe is not None:
                unsubscribe()
        self._cancel_maintenance = None
        self._unsubscribe_change = None
        self._unsubscribe_stop = None
        self._async_cancel_gate_wake()
        self._async_cancel_worker()
        async with self._lock:
            await self._async_reap_worker()
            await self._async_drain_chain()
            if self._outbox is not None:
                await self._outbox.async_close()
        self._closed = True
        self._board.update(())
        self._hass.data[DOMAIN].pop(self._entry.entry_id, None)
        self._listeners.clear()

    @callback
    def _async_fail(self, code: str) -> None:
        """Fence until storage is verified again, which takes a new start."""
        _LOGGER.error("Delivery stopped: %s", code)
        self._failure = code
        self._async_cancel_worker()
        self._async_publish()

    @callback
    def _async_set_activity(
        self, status: DeliveryStatus, code: str | None = None
    ) -> None:
        self._activity = DeliveryStatusSnapshot(status, code)
        self._async_publish()

    @callback
    def _async_publish(self) -> None:
        snapshot = self.status
        if snapshot != self._published:
            self._published = snapshot
            for listener in tuple(self._listeners):
                listener()
