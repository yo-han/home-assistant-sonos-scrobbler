"""A private transactional ledger of qualified plays, stdlib SQLite only.

Every duplicate-protection promise rests on this file. Each operation is one
durable transaction on its own connection, and whatever failed surfaces as an
OutboxError with a fixed code, so a caller can never mistake a lost commit for a
persisted state. No operation deletes or recreates the file.
"""

import asyncio
import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import wraps
from pathlib import Path
from typing import Any

from .const import MAX_SELECTED_PLAYERS
from .playback import MarkerDelta, QualifiedPlay, ResumeHint, TrackMetadata

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MAX_ACTIVE_PLAYS = 1000
ACTIVE_RETENTION_SECONDS = 7 * 86400
MAX_TOMBSTONES = 10000
TOMBSTONE_RETENTION_SECONDS = 14 * 86400
# Markers never come near this many, so the oldest unreferenced alias always exists
# and the table cannot grow past it, however many ids coalesce onto one play.
MAX_ALIASES = MAX_TOMBSTONES
BUSY_TIMEOUT_SECONDS = 5
OVERLAP_START_MARGIN_SECONDS = 30
# Below the smallest listening threshold of 15 s, so a resume cannot earn again,
# while a genuine repeat that starts when the track ended still passes. Both margins
# apply in either arrival order: plays need not qualify in the order they started.
OVERLAP_END_MARGIN_SECONDS = 10

_PRIVATE_FILE_MODE = 0o600
_GROUP_AND_OTHER_BITS = 0o077
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_REASON = re.compile(r"[a-z0-9_]{1,64}")
_WRITABLE_META = frozenset(
    {"rate_limited_until", "auth_required", "clock_high_water", "now_playing_epoch"}
)
_ACTIVE = "('pending', 'held', 'in_flight', 'uncertain')"
# Active rows nobody is working on; an attempt in flight belongs to its worker.
_WAITING = "('pending', 'held', 'uncertain')"
_TERMINAL = "('sent', 'duplicate', 'ignored', 'rejected', 'expired', 'discarded')"
_STRIPPED = "artist = '', title = '', album = NULL"
_MARKER_IDS = "SELECT occurrence_id FROM checkpoints WHERE occurrence_id IS NOT NULL"
# A marker pins the play it names, also when it names an id that coalesced onto it.
_PINNED_BY_MARKER = (
    f"{_MARKER_IDS} UNION SELECT play_id FROM aliases WHERE alias_id IN ({_MARKER_IDS})"
)
_ITEM_COLUMNS = (
    "id, account_hash, track_key, started_at, duration, attempts, read_attempts, "
    "state, not_before, deadline, artist, title, album"
)
_CONNECTION_PRAGMAS = (
    "PRAGMA synchronous = FULL",
    "PRAGMA journal_mode = DELETE",
    # Erased metadata must not linger in free pages of the file.
    "PRAGMA secure_delete = ON",
)
_SCHEMA = (
    f"""
    CREATE TABLE plays (
        id TEXT PRIMARY KEY,
        account_hash BLOB NOT NULL,
        track_key BLOB NOT NULL,
        started_at INTEGER NOT NULL,
        duration INTEGER NOT NULL,
        artist TEXT NOT NULL,
        title TEXT NOT NULL,
        album TEXT,
        state TEXT NOT NULL CHECK (state IN {_ACTIVE} OR state IN {_TERMINAL}),
        reason TEXT NOT NULL,
        epoch INTEGER NOT NULL,
        attempts INTEGER NOT NULL,
        read_attempts INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        not_before INTEGER NOT NULL,
        deadline INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        CHECK (
            state IN {_ACTIVE} OR (artist = '' AND title = '' AND album IS NULL)
        )
    )
    """,
    "CREATE INDEX plays_by_state ON plays (state, not_before)",
    "CREATE INDEX plays_by_track ON plays (account_hash, track_key, started_at)",
    # started_at is in microseconds here: a restored marker must equal the original.
    """
    CREATE TABLE checkpoints (
        lineage TEXT NOT NULL,
        slot TEXT NOT NULL CHECK (slot IN ('active', 'prior', 'guard')),
        occurrence_id TEXT,
        track_key BLOB,
        started_at INTEGER,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (lineage, slot),
        CHECK (
            (
                slot = 'guard'
                AND occurrence_id IS NULL
                AND track_key IS NULL
                AND started_at IS NULL
            )
            OR (
                slot <> 'guard'
                AND occurrence_id IS NOT NULL
                AND track_key IS NOT NULL
                AND started_at IS NOT NULL
            )
        )
    )
    """,
    # An id that coalesced onto a play stays known; it never had a row of its own.
    """
    CREATE TABLE aliases (
        alias_id TEXT PRIMARY KEY,
        play_id TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )
    """,
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)
_TABLES = frozenset({"plays", "checkpoints", "aliases", "meta"})


class DeliveryState(StrEnum):
    """Where a play stands; only the first four can still change."""

    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    UNCERTAIN = "uncertain"
    HELD = "held"
    SENT = "sent"
    DUPLICATE = "duplicate"
    IGNORED = "ignored"
    REJECTED = "rejected"
    EXPIRED = "expired"
    DISCARDED = "discarded"


class EnqueueResult(StrEnum):
    """What became of an offered play."""

    ENQUEUED = "enqueued"
    DUPLICATE_OCCURRENCE = "duplicate_occurrence"
    OVERLAPPING = "overlapping"
    QUEUE_FULL = "queue_full"


_TERMINAL_STATES = frozenset(
    {
        DeliveryState.SENT,
        DeliveryState.DUPLICATE,
        DeliveryState.IGNORED,
        DeliveryState.REJECTED,
        DeliveryState.EXPIRED,
        DeliveryState.DISCARDED,
    }
)
# An uncertain row may already be accepted, so nothing can make it claimable.
_CLAIMABLE_STATES = frozenset({DeliveryState.PENDING, DeliveryState.HELD})
_COUNTERS = {
    DeliveryState.SENT: "sent_total",
    DeliveryState.DUPLICATE: "skipped_total",
    DeliveryState.IGNORED: "ignored_total",
}
_NUMERIC_META = ("epoch", *_COUNTERS.values())


class OutboxError(Exception):
    """A storage or state failure, told by a fixed code and nothing else."""

    def __init__(self, code: str) -> None:
        """Keep the code as the whole message."""
        super().__init__(code)
        self.code = code


class OutboxClosed(OutboxError):
    """The outbox stopped taking work."""


@dataclass(frozen=True, slots=True)
class OutboxItem:
    """One queued play with its original payload, out of every repr."""

    id: str
    account_hash: bytes = field(repr=False)
    track_key: bytes = field(repr=False)
    started_at: int = field(repr=False)  # UTC seconds, as sent to a provider
    duration: int
    attempts: int
    read_attempts: int
    state: DeliveryState
    not_before: datetime = field(repr=False)
    deadline: datetime = field(repr=False)
    metadata: TrackMetadata = field(repr=False)


@dataclass(frozen=True, slots=True)
class OutboxStatus:
    """Counts and one age, nothing about any play."""

    pending_count: int
    held_count: int
    in_flight_count: int
    uncertain_count: int
    sent_total: int
    skipped_total: int
    ignored_total: int
    oldest_queue_age_seconds: int | None
    # Uncertain rows reads can still decide; the rest wait for their expiry.
    reconcilable_count: int = 0


def _since_epoch(moment: datetime, unit: timedelta) -> int:
    if moment.utcoffset() is None:
        raise OutboxError("naive_time")
    return (moment - _UNIX_EPOCH) // unit


def _seconds(moment: datetime) -> int:
    return _since_epoch(moment, timedelta(seconds=1))


def _microseconds(moment: datetime) -> int:
    return _since_epoch(moment, timedelta(microseconds=1))


def _moment(seconds: int) -> datetime:
    return _UNIX_EPOCH + timedelta(seconds=seconds)


def _wall_clock() -> int:
    """Date a change where no caller supplies a time; only retention reads it."""
    return int(time.time())


def _checked_reason(reason: str) -> str:
    """Keep provider text out of the file: a reason is a fixed code."""
    if _REASON.fullmatch(reason) is None:
        raise OutboxError("invalid_reason")
    return reason


def _placeholders(values: Iterable[object]) -> str:
    return ", ".join("?" for _value in values)


def _storage_error(error: sqlite3.Error) -> OutboxError:
    """Name the failure by its class; SQLite's text may quote stored values."""
    # Bare DatabaseError is how sqlite3 reports a damaged or foreign file.
    if type(error) is sqlite3.DatabaseError:
        return OutboxError("storage_corrupt")
    return OutboxError("storage_error")


@contextmanager
def _transaction(path: Path, mode: str = "rw") -> Iterator[sqlite3.Connection]:
    """Yield one durable transaction; leaving without COMMIT rolls it back.

    mode=rw is the guarantee that a lost file stays lost instead of coming back as
    an empty ledger. Closing a connection rolls back whatever is still open.
    """
    uri = f"{path.absolute().as_uri()}?mode={mode}"
    connection = sqlite3.connect(
        uri, uri=True, isolation_level=None, timeout=BUSY_TIMEOUT_SECONDS
    )
    try:
        for pragma in _CONNECTION_PRAGMAS:
            connection.execute(pragma).fetchall()
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.execute("COMMIT")
    finally:
        connection.close()


def _fixed_errors[**Parameters, Result](
    operation: Callable[Parameters, Result],
) -> Callable[Parameters, Result]:
    """Let nothing but a fixed code leave a store operation.

    Python's own messages quote values just as SQLite's do, and callers map storage
    failures by this one exception type.
    """

    @wraps(operation)
    def guarded(*args: Parameters.args, **kwargs: Parameters.kwargs) -> Result:
        try:
            return operation(*args, **kwargs)
        except OutboxError:
            raise
        except sqlite3.Error as error:
            raise _storage_error(error) from None
        except Exception as error:
            # Only the class: its message may quote a stored value. A programming
            # error relabelled as invalid_data must still be visible somewhere.
            _LOGGER.error(
                "Outbox operation %s failed: %s",
                operation.__name__,
                type(error).__name__,
            )
            raise OutboxError("invalid_data") from None

    return guarded


def _item(row: tuple[Any, ...]) -> OutboxItem:
    artist, title, album = row[10:13]
    return OutboxItem(
        id=row[0],
        account_hash=row[1],
        track_key=row[2],
        started_at=row[3],
        duration=row[4],
        attempts=row[5],
        read_attempts=row[6],
        state=DeliveryState(row[7]),
        not_before=_moment(row[8]),
        deadline=_moment(row[9]),
        metadata=TrackMetadata(artist=artist, title=title, album=album),
    )


def _hint(lineage: str, slot: str, row: tuple[Any, ...]) -> ResumeHint:
    occurrence_id, track_key, started_at = row
    return ResumeHint(
        occurrence_id=occurrence_id,
        track_key=track_key,
        started_at=_UNIX_EPOCH + timedelta(microseconds=started_at),
        lineage=lineage,
        prior=slot == "prior",
    )


def _read_epoch(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT value FROM meta WHERE key = 'epoch'").fetchone()
    return int(row[0])


class OutboxStore:
    """The ledger; synchronous, so it belongs on an executor thread."""

    def __init__(self, path: Path) -> None:
        """Bind to a file through create or open, which verify it."""
        self._path = path

    @classmethod
    @_fixed_errors
    def create(cls, path: Path) -> OutboxStore:
        """Start a new ledger; the only way a file ever comes to exist."""
        try:
            descriptor = os.open(
                path, os.O_CREAT | os.O_EXCL | os.O_RDWR, _PRIVATE_FILE_MODE
            )
        except FileExistsError:
            raise OutboxError("storage_exists") from None
        except OSError:
            raise OutboxError("storage_error") from None
        os.close(descriptor)
        with _transaction(path, "rwc") as connection:
            for statement in _SCHEMA:
                connection.execute(statement)
            connection.executemany(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                (
                    ("schema_version", str(SCHEMA_VERSION)),
                    ("epoch", "0"),
                    *((counter, "0") for counter in _COUNTERS.values()),
                ),
            )
        return cls(path)

    @classmethod
    @_fixed_errors
    def open(cls, path: Path) -> OutboxStore:
        """Attach to an existing ledger, or fail without touching the file."""
        try:
            mode = path.stat().st_mode
        except FileNotFoundError:
            raise OutboxError("storage_missing") from None
        except OSError:
            raise OutboxError("storage_error") from None
        if mode & _GROUP_AND_OTHER_BITS:
            raise OutboxError("storage_permissions")
        with _transaction(path) as connection:
            if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise OutboxError("storage_corrupt")
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
            if _TABLES - {row[0] for row in tables}:
                raise OutboxError("schema_mismatch")
            meta = dict(connection.execute("SELECT key, value FROM meta").fetchall())
            if meta.get("schema_version") != str(SCHEMA_VERSION) or not all(
                meta.get(key, "").isdecimal() for key in _NUMERIC_META
            ):
                raise OutboxError("schema_mismatch")
        return cls(path)

    @_fixed_errors
    def enqueue(
        self,
        play: QualifiedPlay,
        account_hash: bytes,
        epoch: int,
        now: datetime,
        not_before: datetime,
    ) -> EnqueueResult:
        """Record a play once; a marker is never created or revived here.

        An id that overlaps a play is remembered as its alias, so it answers as
        known from then on, whatever happens to the overlap ledger around it.
        """
        created_at = _seconds(now)
        started_at = _seconds(play.started_at)
        ready_at = _seconds(not_before)
        duration = round(play.duration_seconds)
        with _transaction(self._path) as connection:
            known = connection.execute(
                "SELECT 1 FROM plays WHERE id = :id UNION ALL "
                "SELECT 1 FROM aliases WHERE alias_id = :id LIMIT 1",
                {"id": play.occurrence_id},
            ).fetchone()
            if known is not None:
                return EnqueueResult.DUPLICATE_OCCURRENCE
            overlapped = connection.execute(
                "SELECT id FROM plays WHERE account_hash = :account "
                "AND track_key = :track AND ("
                ":start BETWEEN started_at - :before AND "
                "started_at + duration - :after OR "
                "started_at BETWEEN :start - :before AND "
                ":start + :duration - :after) ORDER BY started_at, id LIMIT 1",
                {
                    "account": account_hash,
                    "track": play.track_key,
                    "start": started_at,
                    "duration": duration,
                    "before": OVERLAP_START_MARGIN_SECONDS,
                    "after": OVERLAP_END_MARGIN_SECONDS,
                },
            ).fetchone()
            if overlapped is not None:
                self._record_alias(
                    connection, play.occurrence_id, overlapped[0], created_at
                )
                return EnqueueResult.OVERLAPPING
            active = connection.execute(
                f"SELECT COUNT(*) FROM plays WHERE state IN {_ACTIVE}"
            ).fetchone()[0]
            if active >= MAX_ACTIVE_PLAYS:
                return EnqueueResult.QUEUE_FULL
            connection.execute(
                "INSERT INTO plays (id, account_hash, track_key, started_at, duration, "
                "artist, title, album, state, reason, epoch, attempts, read_attempts, "
                "created_at, not_before, deadline, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', '', ?, 0, 0, ?, ?, ?, ?)",
                (
                    play.occurrence_id,
                    account_hash,
                    play.track_key,
                    started_at,
                    duration,
                    play.metadata.artist,
                    play.metadata.title,
                    play.metadata.album,
                    epoch,
                    created_at,
                    ready_at,
                    created_at + ACTIVE_RETENTION_SECONDS,
                    created_at,
                ),
            )
            connection.execute(
                "UPDATE checkpoints SET updated_at = ? WHERE occurrence_id = ?",
                (created_at, play.occurrence_id),
            )
        return EnqueueResult.ENQUEUED

    @_fixed_errors
    def next_ready(self, now: datetime, since: int = 0) -> OutboxItem | None:
        """Return the oldest waiting row of this period whose time has come.

        A row that started before since is parked: no read, no request, no wake.
        """
        moment = _seconds(now)
        with _transaction(self._path) as connection:
            row = connection.execute(
                f"SELECT {_ITEM_COLUMNS} FROM plays "
                f"WHERE state IN {_WAITING} AND not_before <= ? AND started_at >= ? "
                "ORDER BY started_at, id LIMIT 1",
                (moment, since),
            ).fetchone()
        return None if row is None else _item(row)

    @_fixed_errors
    def next_wake(self, since: int = 0) -> datetime | None:
        """Return when the earliest waiting row of this period comes due."""
        with _transaction(self._path) as connection:
            row = connection.execute(
                f"SELECT MIN(not_before) FROM plays WHERE state IN {_WAITING} "
                "AND started_at >= ?",
                (since,),
            ).fetchone()
        return None if row[0] is None else _moment(row[0])

    @_fixed_errors
    def next_wake_is_retry(self, since: int = 0) -> bool:
        """Return whether the row that comes due first has made an attempt."""
        with _transaction(self._path) as connection:
            row = connection.execute(
                f"SELECT attempts > 0 OR read_attempts > 0 FROM plays "
                f"WHERE state IN {_WAITING} AND started_at >= ? "
                "ORDER BY not_before, started_at, id LIMIT 1",
                (since,),
            ).fetchone()
        return row is not None and bool(row[0])

    @_fixed_errors
    def latest_active_start(self) -> int | None:
        """Return the newest start among the rows a worker could still touch."""
        with _transaction(self._path) as connection:
            latest: int | None = connection.execute(
                f"SELECT MAX(started_at) FROM plays WHERE state IN {_ACTIVE}"
            ).fetchone()[0]
        return latest

    @_fixed_errors
    def claim_in_flight(
        self, item_id: str, expected: DeliveryState, epoch: int
    ) -> bool:
        """Persist the attempt; only a True return permits the request.

        Both the row and the store must still be on the caller's epoch.
        """
        if expected not in _CLAIMABLE_STATES:
            raise OutboxError("illegal_transition")
        with _transaction(self._path) as connection:
            cursor = connection.execute(
                "UPDATE plays SET state = 'in_flight', attempts = attempts + 1, "
                "updated_at = ? WHERE id = ? AND state = ? AND epoch = ? AND "
                "CAST((SELECT value FROM meta WHERE key = 'epoch') AS INTEGER) = ?",
                (_wall_clock(), item_id, expected.value, epoch, epoch),
            )
            claimed = cursor.rowcount == 1
        return claimed

    @_fixed_errors
    def finalize(self, item_id: str, state: DeliveryState, reason: str) -> None:
        """End an active row: erase its metadata and count it, atomically."""
        if state not in _TERMINAL_STATES:
            raise OutboxError("illegal_transition")
        counter = _COUNTERS.get(state)
        with _transaction(self._path) as connection:
            self._move(
                connection,
                item_id,
                _ACTIVE,
                f"state = ?, reason = ?, {_STRIPPED}",
                (state.value, _checked_reason(reason)),
            )
            if counter is not None:
                connection.execute(
                    "UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = ?",
                    (counter,),
                )

    @_fixed_errors
    def retry_later(self, item_id: str, not_before: datetime, reason: str) -> None:
        """Requeue an attempt the provider explicitly did not accept."""
        self._transition(
            item_id,
            "('in_flight')",
            "state = 'pending', not_before = ?, reason = ?",
            (_seconds(not_before), _checked_reason(reason)),
        )

    @_fixed_errors
    def hold(self, item_id: str, not_before: datetime, reason: str) -> None:
        """Park a row no attempt touched yet, after a read could not decide it."""
        self._transition(
            item_id,
            "('pending', 'held')",
            "state = 'held', not_before = ?, reason = ?, "
            "read_attempts = read_attempts + 1",
            (_seconds(not_before), _checked_reason(reason)),
        )

    @_fixed_errors
    def mark_uncertain(self, item_id: str, reason: str) -> None:
        """Record that an attempt may have reached the provider."""
        self._transition(
            item_id,
            "('in_flight')",
            "state = 'uncertain', reason = ?",
            (_checked_reason(reason),),
        )

    @_fixed_errors
    def reschedule_uncertain(
        self, item_id: str, not_before: datetime, reason: str
    ) -> None:
        """Plan the next read of an uncertain row, which stays uncertain."""
        self._transition(
            item_id,
            "('uncertain')",
            "not_before = ?, reason = ?, read_attempts = read_attempts + 1",
            (_seconds(not_before), _checked_reason(reason)),
        )

    @_fixed_errors
    def recover_in_flight(self) -> int:
        """Treat every attempt left by a previous run as possibly accepted."""
        with _transaction(self._path) as connection:
            cursor = connection.execute(
                "UPDATE plays SET state = 'uncertain', "
                "reason = 'recovered_in_flight', updated_at = ? "
                "WHERE state = 'in_flight'",
                (_wall_clock(),),
            )
            recovered = cursor.rowcount
        return recovered

    @_fixed_errors
    def rebind(self, epoch: int, account_hash: bytes) -> None:
        """Move the store and this account's active rows to a newer epoch."""
        with _transaction(self._path) as connection:
            if epoch < _read_epoch(connection):
                raise OutboxError("stale_epoch")
            connection.execute(
                "UPDATE meta SET value = ? WHERE key = 'epoch'", (str(epoch),)
            )
            connection.execute(
                "UPDATE plays SET epoch = ?, updated_at = ? "
                f"WHERE account_hash = ? AND state IN {_ACTIVE}",
                (epoch, _wall_clock(), account_hash),
            )

    @_fixed_errors
    def set_epoch(self, epoch: int) -> None:
        """Raise the epoch; a stale writer can never lower it and reopen sending."""
        with _transaction(self._path) as connection:
            connection.execute(
                "UPDATE meta SET value = ? "
                "WHERE key = 'epoch' AND CAST(value AS INTEGER) < ?",
                (str(epoch), epoch),
            )

    @_fixed_errors
    def purge_other_accounts(self, account_hash: bytes, since: int = 0) -> int:
        """Discard what this period queued for another account, keeping its hashes."""
        with _transaction(self._path) as connection:
            cursor = connection.execute(
                "UPDATE plays SET state = 'discarded', "
                f"reason = 'account_changed', {_STRIPPED}, updated_at = ? "
                f"WHERE account_hash <> ? AND state IN {_WAITING} "
                "AND started_at >= ?",
                (_wall_clock(), account_hash, since),
            )
            discarded = cursor.rowcount
        return discarded

    @_fixed_errors
    def clear_queue(self) -> int:
        """Erase queued metadata; the count is returned only after COMMIT."""
        with _transaction(self._path) as connection:
            cursor = connection.execute(
                "UPDATE plays SET state = 'discarded', "
                f"reason = 'queue_cleared', {_STRIPPED}, updated_at = ? "
                f"WHERE state IN {_WAITING}",
                (_wall_clock(),),
            )
            discarded = cursor.rowcount
        return discarded

    @_fixed_errors
    def maintain(self, now: datetime) -> None:
        """Enforce every retention bound; needs no playback to run.

        A tombstone a marker pins survives both limits: dropping it would make that
        known id enqueueable again. Protection wins over the count limit, which is
        then exceeded by at most the number of markers. Aliases leave with their
        play, and markers themselves are not touched here.
        """
        moment = _seconds(now)
        tombstones = (
            f"SELECT id FROM plays WHERE state IN {_TERMINAL} "
            f"AND id NOT IN ({_PINNED_BY_MARKER})"
        )
        with _transaction(self._path) as connection:
            connection.execute(
                "UPDATE plays SET state = 'expired', "
                f"reason = 'retention', {_STRIPPED}, updated_at = ? "
                f"WHERE state IN {_WAITING} AND deadline <= ?",
                (moment, moment),
            )
            connection.execute(
                f"DELETE FROM plays WHERE id IN ({tombstones} AND updated_at <= ?)",
                (moment - TOMBSTONE_RETENTION_SECONDS,),
            )
            connection.execute(
                f"DELETE FROM plays WHERE id IN ({tombstones} "
                "ORDER BY updated_at, id LIMIT MAX(0, "
                f"(SELECT COUNT(*) FROM plays WHERE state IN {_TERMINAL}) - ?))",
                (MAX_TOMBSTONES,),
            )
            connection.execute(
                "DELETE FROM aliases WHERE play_id NOT IN (SELECT id FROM plays)"
            )

    @_fixed_errors
    def status(self, now: datetime, since: int = 0) -> OutboxStatus:
        """Return the counts, the oldest active age and what this period can decide."""
        moment = _seconds(now)
        with _transaction(self._path) as connection:
            counts = dict(
                connection.execute(
                    f"SELECT state, COUNT(*) FROM plays WHERE state IN {_ACTIVE} "
                    "GROUP BY state"
                ).fetchall()
            )
            oldest = connection.execute(
                f"SELECT MIN(created_at) FROM plays WHERE state IN {_ACTIVE}"
            ).fetchone()[0]
            reconcilable = connection.execute(
                "SELECT COUNT(*) FROM plays WHERE state = 'uncertain' "
                "AND started_at >= ?",
                (since,),
            ).fetchone()[0]
            totals = dict(
                connection.execute(
                    f"SELECT key, value FROM meta WHERE key IN "
                    f"({_placeholders(_COUNTERS)})",
                    tuple(_COUNTERS.values()),
                ).fetchall()
            )
        return OutboxStatus(
            pending_count=counts.get(DeliveryState.PENDING.value, 0),
            held_count=counts.get(DeliveryState.HELD.value, 0),
            in_flight_count=counts.get(DeliveryState.IN_FLIGHT.value, 0),
            uncertain_count=counts.get(DeliveryState.UNCERTAIN.value, 0),
            sent_total=int(totals["sent_total"]),
            skipped_total=int(totals["skipped_total"]),
            ignored_total=int(totals["ignored_total"]),
            oldest_queue_age_seconds=(
                None if oldest is None else max(moment - oldest, 0)
            ),
            reconcilable_count=reconcilable,
        )

    @_fixed_errors
    def other_sent_timestamps(
        self, item_id: str, track_key: bytes, start: int, end: int
    ) -> frozenset[int]:
        """Return starts of OTHER sent occurrences of the track on this account.

        The row's own id and own start never appear, so a receipt for the current
        occurrence or a restored copy of it cannot be explained away. An unknown
        row excludes nothing.
        """
        with _transaction(self._path) as connection:
            rows = connection.execute(
                "SELECT sent.started_at FROM plays AS sent "
                "JOIN plays AS own ON own.id = ? "
                "WHERE sent.state = 'sent' "
                "AND sent.account_hash = own.account_hash "
                "AND sent.track_key = ? AND sent.started_at BETWEEN ? AND ? "
                "AND sent.started_at <> own.started_at",
                (item_id, track_key, start, end),
            ).fetchall()
        return frozenset(row[0] for row in rows)

    @_fixed_errors
    def apply_marker_delta(self, delta: MarkerDelta, now: datetime) -> None:
        """Apply all marker changes at once; repeating a delta changes nothing.

        Clears run first, so a lineage moving out frees room for one moving in,
        and a guard both set and cleared stays set.
        """
        moment = _seconds(now)
        markers = [
            (
                marker.lineage,
                "prior" if marker.prior else "active",
                marker.occurrence_id,
                marker.track_key,
                _microseconds(marker.started_at),
                moment,
            )
            for marker in delta.markers_set
        ]
        with _transaction(self._path) as connection:
            for lineage in delta.lineages_cleared:
                connection.execute(
                    "DELETE FROM checkpoints WHERE lineage = ?", (lineage,)
                )
            for lineage in delta.priors_cleared:
                connection.execute(
                    "DELETE FROM checkpoints WHERE lineage = ? AND slot = 'prior'",
                    (lineage,),
                )
            for lineage in delta.guards_cleared:
                connection.execute(
                    "DELETE FROM checkpoints WHERE lineage = ? AND slot = 'guard'",
                    (lineage,),
                )
            connection.executemany(
                "INSERT OR REPLACE INTO checkpoints "
                "(lineage, slot, occurrence_id, track_key, started_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                markers,
            )
            connection.executemany(
                "INSERT OR REPLACE INTO checkpoints (lineage, slot, updated_at) "
                "VALUES (?, 'guard', ?)",
                [(lineage, moment) for lineage in delta.guards_set],
            )
            lineages = connection.execute(
                "SELECT COUNT(DISTINCT lineage) FROM checkpoints"
            ).fetchone()[0]
            if lineages > MAX_SELECTED_PLAYERS:
                raise OutboxError("marker_limit")

    @_fixed_errors
    def resume_state(self) -> tuple[tuple[ResumeHint, ...], frozenset[str]]:
        """Return the stored markers and the lineages under a guard."""
        with _transaction(self._path) as connection:
            rows = connection.execute(
                "SELECT lineage, slot, occurrence_id, track_key, started_at "
                "FROM checkpoints ORDER BY lineage, slot"
            ).fetchall()
        hints = tuple(
            _hint(row[0], row[1], row[2:]) for row in rows if row[1] != "guard"
        )
        return hints, frozenset(row[0] for row in rows if row[1] == "guard")

    @_fixed_errors
    def purge_lineages(self, keep: frozenset[str]) -> int:
        """Drop every slot of the lineages that are no longer selected."""
        with _transaction(self._path) as connection:
            cursor = connection.execute(
                f"DELETE FROM checkpoints WHERE lineage NOT IN ({_placeholders(keep)})",
                tuple(keep),
            )
            dropped = cursor.rowcount
        return dropped

    @_fixed_errors
    def meta_get(self, key: str) -> str | None:
        """Return a stored value, or None if it was never set."""
        with _transaction(self._path) as connection:
            row = connection.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return None if row is None else row[0]

    @_fixed_errors
    def meta_set(self, key: str, value: str) -> None:
        """Store a worker flag; epoch, version and counters have their own paths."""
        if key not in _WRITABLE_META:
            raise OutboxError("unknown_meta_key")
        with _transaction(self._path) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
            )

    @staticmethod
    def _record_alias(
        connection: sqlite3.Connection, alias_id: str, play_id: str, created_at: int
    ) -> None:
        """Remember a coalesced id, making room only among unreferenced aliases."""
        connection.execute(
            "DELETE FROM aliases WHERE alias_id IN ("
            f"SELECT alias_id FROM aliases WHERE alias_id NOT IN ({_MARKER_IDS}) "
            "ORDER BY created_at, alias_id LIMIT MAX(0, "
            "(SELECT COUNT(*) FROM aliases) + 1 - ?))",
            (MAX_ALIASES,),
        )
        connection.execute(
            "INSERT INTO aliases (alias_id, play_id, created_at) VALUES (?, ?, ?)",
            (alias_id, play_id, created_at),
        )

    def _transition(
        self,
        item_id: str,
        sources: str,
        assignments: str,
        values: tuple[object, ...],
    ) -> None:
        with _transaction(self._path) as connection:
            self._move(connection, item_id, sources, assignments, values)

    @staticmethod
    def _move(
        connection: sqlite3.Connection,
        item_id: str,
        sources: str,
        assignments: str,
        values: tuple[object, ...],
    ) -> None:
        """Change exactly one row in an allowed state, or refuse."""
        cursor = connection.execute(
            f"UPDATE plays SET {assignments}, updated_at = ? "
            f"WHERE id = ? AND state IN {sources}",
            (*values, _wall_clock(), item_id),
        )
        if cursor.rowcount != 1:
            raise OutboxError("illegal_transition")


class _ExecutorCall:
    """One store call, tracked from inside the executor function.

    The future a run_blocking hands back says nothing about the thread: cancelling
    it, as Home Assistant does before STOP, leaves a running thread committing and
    a queued one never started. Only the function itself can tell those apart, so
    finished resolves when the thread returned or when the call can no longer start.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start unclaimed: neither a thread nor close has decided yet."""
        self._loop = loop
        self._lock = threading.Lock()
        self._started = False
        self._given_up = False
        self.finished: asyncio.Future[None] = loop.create_future()

    def run[*Arguments, Result](
        self, function: Callable[[*Arguments], Result], *args: *Arguments
    ) -> Result:
        """Run on the worker thread, unless the call was given up on already."""
        with self._lock:
            if self._given_up:
                raise OutboxClosed("outbox_closed")
            self._started = True
        try:
            return function(*args)
        finally:
            self._loop.call_soon_threadsafe(self._finish)

    def give_up_unless_started(self) -> None:
        """Close the call on the loop once its wrapper ended without a thread."""
        with self._lock:
            if self._started:
                return
            self._given_up = True
        self._finish()

    def _finish(self) -> None:
        if not self.finished.done():
            self.finished.set_result(None)


class AsyncOutbox:
    """Run the store on an executor and account for every thread it started.

    A cancelled caller stops waiting, not the thread: the commit can still land.
    Each call therefore stays tracked until its thread returned, so close can wait
    for the last write instead of letting one slip in unseen.
    """

    def __init__(
        self, store: OutboxStore, run_blocking: Callable[..., Awaitable[Any]]
    ) -> None:
        """Take the store and the way to reach an executor."""
        self._store = store
        self._run_blocking = run_blocking
        self._work: set[asyncio.Future[None]] = set()
        self._closed = False

    async def _async_run[*Arguments, Result](
        self, function: Callable[[*Arguments], Result], *args: *Arguments
    ) -> Result:
        if self._closed:
            raise OutboxClosed("outbox_closed")
        call = _ExecutorCall(asyncio.get_running_loop())
        wrapper: asyncio.Future[Result] = asyncio.ensure_future(
            self._run_blocking(call.run, function, *args)
        )
        self._work.add(call.finished)
        call.finished.add_done_callback(self._work.discard)
        wrapper.add_done_callback(lambda _wrapper: call.give_up_unless_started())
        return await asyncio.shield(wrapper)

    async def async_drain(self) -> None:
        """Wait for everything already started; new work stays welcome."""
        while self._work:
            await asyncio.wait(self._work)

    async def async_close(self) -> None:
        """Refuse new work and wait for everything already started."""
        self._closed = True
        await self.async_drain()

    async def async_create(self) -> None:
        """Create the bound file; the thread stays tracked like any other call."""
        await self._async_run(OutboxStore.create, self._store._path)

    async def async_open(self) -> None:
        """Verify the bound file; the thread stays tracked like any other call."""
        await self._async_run(OutboxStore.open, self._store._path)

    async def async_enqueue(
        self,
        play: QualifiedPlay,
        account_hash: bytes,
        epoch: int,
        now: datetime,
        not_before: datetime,
    ) -> EnqueueResult:
        """Mirror OutboxStore.enqueue."""
        return await self._async_run(
            self._store.enqueue, play, account_hash, epoch, now, not_before
        )

    async def async_next_ready(
        self, now: datetime, since: int = 0
    ) -> OutboxItem | None:
        """Mirror OutboxStore.next_ready."""
        return await self._async_run(self._store.next_ready, now, since)

    async def async_next_wake(self, since: int = 0) -> datetime | None:
        """Mirror OutboxStore.next_wake."""
        return await self._async_run(self._store.next_wake, since)

    async def async_next_wake_is_retry(self, since: int = 0) -> bool:
        """Mirror OutboxStore.next_wake_is_retry."""
        return await self._async_run(self._store.next_wake_is_retry, since)

    async def async_latest_active_start(self) -> int | None:
        """Mirror OutboxStore.latest_active_start."""
        return await self._async_run(self._store.latest_active_start)

    async def async_claim_in_flight(
        self, item_id: str, expected: DeliveryState, epoch: int
    ) -> bool:
        """Mirror OutboxStore.claim_in_flight."""
        return await self._async_run(
            self._store.claim_in_flight, item_id, expected, epoch
        )

    async def async_finalize(
        self, item_id: str, state: DeliveryState, reason: str
    ) -> None:
        """Mirror OutboxStore.finalize."""
        await self._async_run(self._store.finalize, item_id, state, reason)

    async def async_retry_later(
        self, item_id: str, not_before: datetime, reason: str
    ) -> None:
        """Mirror OutboxStore.retry_later."""
        await self._async_run(self._store.retry_later, item_id, not_before, reason)

    async def async_hold(self, item_id: str, not_before: datetime, reason: str) -> None:
        """Mirror OutboxStore.hold."""
        await self._async_run(self._store.hold, item_id, not_before, reason)

    async def async_mark_uncertain(self, item_id: str, reason: str) -> None:
        """Mirror OutboxStore.mark_uncertain."""
        await self._async_run(self._store.mark_uncertain, item_id, reason)

    async def async_reschedule_uncertain(
        self, item_id: str, not_before: datetime, reason: str
    ) -> None:
        """Mirror OutboxStore.reschedule_uncertain."""
        await self._async_run(
            self._store.reschedule_uncertain, item_id, not_before, reason
        )

    async def async_recover_in_flight(self) -> int:
        """Mirror OutboxStore.recover_in_flight."""
        return await self._async_run(self._store.recover_in_flight)

    async def async_rebind(self, epoch: int, account_hash: bytes) -> None:
        """Mirror OutboxStore.rebind."""
        await self._async_run(self._store.rebind, epoch, account_hash)

    async def async_set_epoch(self, epoch: int) -> None:
        """Mirror OutboxStore.set_epoch."""
        await self._async_run(self._store.set_epoch, epoch)

    async def async_purge_other_accounts(
        self, account_hash: bytes, since: int = 0
    ) -> int:
        """Mirror OutboxStore.purge_other_accounts."""
        return await self._async_run(
            self._store.purge_other_accounts, account_hash, since
        )

    async def async_clear_queue(self) -> int:
        """Mirror OutboxStore.clear_queue."""
        return await self._async_run(self._store.clear_queue)

    async def async_maintain(self, now: datetime) -> None:
        """Mirror OutboxStore.maintain."""
        await self._async_run(self._store.maintain, now)

    async def async_status(self, now: datetime, since: int = 0) -> OutboxStatus:
        """Mirror OutboxStore.status."""
        return await self._async_run(self._store.status, now, since)

    async def async_other_sent_timestamps(
        self, item_id: str, track_key: bytes, start: int, end: int
    ) -> frozenset[int]:
        """Mirror OutboxStore.other_sent_timestamps."""
        return await self._async_run(
            self._store.other_sent_timestamps, item_id, track_key, start, end
        )

    async def async_apply_marker_delta(self, delta: MarkerDelta, now: datetime) -> None:
        """Mirror OutboxStore.apply_marker_delta."""
        await self._async_run(self._store.apply_marker_delta, delta, now)

    async def async_resume_state(
        self,
    ) -> tuple[tuple[ResumeHint, ...], frozenset[str]]:
        """Mirror OutboxStore.resume_state."""
        return await self._async_run(self._store.resume_state)

    async def async_purge_lineages(self, keep: frozenset[str]) -> int:
        """Mirror OutboxStore.purge_lineages."""
        return await self._async_run(self._store.purge_lineages, keep)

    async def async_meta_get(self, key: str) -> str | None:
        """Mirror OutboxStore.meta_get."""
        return await self._async_run(self._store.meta_get, key)

    async def async_meta_set(self, key: str, value: str) -> None:
        """Mirror OutboxStore.meta_set."""
        await self._async_run(self._store.meta_set, key, value)
