"""Synthetic plays and real-file helpers; faults enter at the sqlite3 layer only."""

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from custom_components.personal_sonos_scrobbler.outbox import (
    EnqueueResult,
    OutboxStore,
)
from custom_components.personal_sonos_scrobbler.playback import (
    QualifiedPlay,
    ResumeHint,
    TrackMetadata,
)

REAL_CONNECT = sqlite3.connect
T0 = datetime(2031, 3, 4, 12, 0, 0, tzinfo=UTC)
ACCOUNT = b"\x01" * 16
OTHER_ACCOUNT = b"\x02" * 16
KEY_A = b"a" * 16
KEY_B = b"b" * 16
EPOCH = 1
META = TrackMetadata(artist="canary-artist", title="canary-title", album="canary-album")
PLAY_COLUMNS = (
    "id, account_hash, track_key, started_at, duration, artist, title, album, state, "
    "reason, epoch, attempts, read_attempts, created_at, not_before, deadline, "
    "updated_at"
)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def qualified(
    occurrence_id: str = "occ",
    *,
    start: float = 0.0,
    key: bytes = KEY_A,
    duration: float = 200.0,
    metadata: TrackMetadata = META,
) -> QualifiedPlay:
    return QualifiedPlay(
        occurrence_id=occurrence_id,
        track_key=key,
        metadata=metadata,
        started_at=at(start),
        qualified_at=at(start + duration / 2),
        duration_seconds=duration,
    )


def add(
    store: OutboxStore,
    occurrence_id: str = "occ",
    *,
    start: float = 0.0,
    key: bytes = KEY_A,
    duration: float = 200.0,
    metadata: TrackMetadata = META,
    account: bytes = ACCOUNT,
    epoch: int = EPOCH,
    now: float = 300.0,
    ready: float | None = None,
) -> EnqueueResult:
    """Enqueue a synthetic play, by default ready at the moment it arrives."""
    return store.enqueue(
        qualified(
            occurrence_id, start=start, key=key, duration=duration, metadata=metadata
        ),
        account,
        epoch,
        at(now),
        at(now if ready is None else ready),
    )


def hint(
    lineage: str = "registry-a",
    occurrence_id: str = "occ",
    *,
    prior: bool = False,
    key: bytes = KEY_A,
    start: float = 0.25,
) -> ResumeHint:
    return ResumeHint(
        occurrence_id=occurrence_id,
        track_key=key,
        started_at=at(start),
        lineage=lineage,
        prior=prior,
    )


def query(path: Path, sql: str, *params: object) -> list[tuple[Any, ...]]:
    """Read the file directly, past every fault a test installed."""
    connection = REAL_CONNECT(path)
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def write(path: Path, sql: str, *params: object) -> None:
    connection = REAL_CONNECT(path)
    try:
        with connection:
            connection.execute(sql, params)
    finally:
        connection.close()


def fill_plays(path: Path, count: int, *, state: str = "pending") -> None:
    """Insert unrelated rows directly: the bound tests need many, cheaply."""
    terminal = state not in {"pending", "held", "in_flight", "uncertain"}
    rows = [
        (
            f"filler-{index}",
            ACCOUNT,
            index.to_bytes(16, "big"),
            0,
            200,
            "" if terminal else "filler-artist",
            "" if terminal else "filler-title",
            state,
            "",
            EPOCH,
            0,
            0,
            index,
            0,
            10**10,
            index,
        )
        for index in range(count)
    ]
    connection = REAL_CONNECT(path)
    try:
        with connection:
            connection.executemany(
                "INSERT INTO plays (id, account_hash, track_key, started_at, duration, "
                "artist, title, state, reason, epoch, attempts, read_attempts, "
                "created_at, not_before, deadline, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
    finally:
        connection.close()


def fill_aliases(path: Path, count: int, play_id: str) -> None:
    """Insert aliases directly, oldest first, for the bound tests."""
    connection = REAL_CONNECT(path)
    try:
        with connection:
            connection.executemany(
                "INSERT INTO aliases (alias_id, play_id, created_at) VALUES (?, ?, ?)",
                ((f"alias-{index}", play_id, index) for index in range(count)),
            )
    finally:
        connection.close()


def install_connection(
    patch: pytest.MonkeyPatch, connection_class: type[sqlite3.Connection]
) -> None:
    """Make the code under test open its connections with this subclass."""
    patch.setattr(sqlite3, "connect", partial(REAL_CONNECT, factory=connection_class))


def fail_on(
    patch: pytest.MonkeyPatch,
    statement: str,
    *,
    after: bool = False,
    message: str = "injected failure",
) -> None:
    """Raise a real sqlite3 error on a statement, before or after it took effect."""

    class Failing(sqlite3.Connection):
        def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
            matches = sql.startswith(statement)
            if matches and not after:
                raise sqlite3.OperationalError(message)
            cursor = super().execute(sql, *args)
            if matches:
                raise sqlite3.OperationalError(message)
            return cursor

    install_connection(patch, Failing)


def on_statement(
    patch: pytest.MonkeyPatch, statement: str, action: Callable[[], None]
) -> None:
    """Run an action inside the open transaction, just before a statement."""

    class Observing(sqlite3.Connection):
        def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
            if sql.startswith(statement):
                action()
            return super().execute(sql, *args)

    install_connection(patch, Observing)
