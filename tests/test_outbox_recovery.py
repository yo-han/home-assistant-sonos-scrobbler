"""The outbox under injected faults, restarts, damaged files and cancellation."""

import asyncio
import os
import sqlite3
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.personal_sonos_scrobbler.outbox import (
    BUSY_TIMEOUT_SECONDS,
    AsyncOutbox,
    DeliveryState,
    EnqueueResult,
    OutboxClosed,
    OutboxError,
    OutboxStore,
)
from custom_components.personal_sonos_scrobbler.playback import (
    MarkerDelta,
    TrackMetadata,
)
from tests.outbox_support import (
    ACCOUNT,
    EPOCH,
    META,
    PLAY_COLUMNS,
    REAL_CONNECT,
    add,
    at,
    fail_on,
    hint,
    install_connection,
    on_statement,
    qualified,
    query,
    write,
)


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "outbox.sqlite3"


@pytest.fixture
def store(path: Path) -> OutboxStore:
    created = OutboxStore.create(path)
    created.set_epoch(EPOCH)
    return created


def state_of(path: Path, occurrence_id: str = "occ") -> str:
    return query(path, "SELECT state FROM plays WHERE id = ?", occurrence_id)[0][0]


def plays_snapshot(path: Path) -> list[tuple[Any, ...]]:
    return query(path, f"SELECT {PLAY_COLUMNS} FROM plays ORDER BY id")


# Enqueue around its commit


def test_failure_before_and_after_enqueue_commit(
    store: OutboxStore, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        fail_on(patch, "COMMIT")
        with pytest.raises(OutboxError) as before_commit:
            add(store)

    assert before_commit.value.code == "storage_error"
    assert query(path, "SELECT COUNT(*) FROM plays") == [(0,)]

    with monkeypatch.context() as patch:
        fail_on(patch, "COMMIT", after=True)
        with pytest.raises(OutboxError) as caught:
            add(store)
        assert caught.value.code == "storage_error"

    # The commit landed though the caller saw a failure: a retry must not add a row.
    assert query(path, "SELECT id FROM plays") == [("occ",)]
    assert add(store) is EnqueueResult.DUPLICATE_OCCURRENCE
    assert query(path, "SELECT id FROM plays") == [("occ",)]


def test_failed_commit_records_no_alias(
    store: OutboxStore, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(store, "first", start=1000)

    with monkeypatch.context() as patch:
        fail_on(patch, "COMMIT")
        with pytest.raises(OutboxError) as caught:
            add(store, "second", start=1000)
        assert caught.value.code == "storage_error"

    assert query(path, "SELECT COUNT(*) FROM aliases") == [(0,)]
    assert add(store, "second", start=1000) is EnqueueResult.OVERLAPPING


def test_failed_insert_leaves_nothing_behind(
    store: OutboxStore, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        fail_on(patch, "INSERT INTO plays")
        with pytest.raises(OutboxError) as caught:
            add(store)
        assert caught.value.code == "storage_error"

    assert query(path, "SELECT COUNT(*) FROM plays") == [(0,)]
    assert add(store) is EnqueueResult.ENQUEUED


def test_real_full_database_refuses_enqueue_and_keeps_the_rows(
    store: OutboxStore, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Capped(sqlite3.Connection):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            pages = self.execute("PRAGMA page_count").fetchone()[0]
            self.execute(f"PRAGMA max_page_count = {pages}").fetchall()

    add(store, "first")
    long_title = TrackMetadata(artist="a" * 1024, title="t" * 1024)
    results: list[EnqueueResult] = []
    with monkeypatch.context() as patch:
        install_connection(patch, Capped)
        with pytest.raises(OutboxError) as caught:
            for index in range(20):
                results.append(
                    add(
                        store,
                        f"big-{index}",
                        start=(index + 1) * 1000,
                        metadata=long_title,
                    )
                )

    assert caught.value.code == "storage_error"
    assert query(path, "SELECT COUNT(*) FROM plays") == [(1 + len(results),)]
    assert state_of(path, "first") == "pending"


# Claim around its commit


def test_failed_inflight_commit_returns_no_claim(
    store: OutboxStore, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(store)
    before = plays_snapshot(path)

    with monkeypatch.context() as patch:
        fail_on(patch, "COMMIT")
        with pytest.raises(OutboxError) as caught:
            store.claim_in_flight("occ", DeliveryState.PENDING, EPOCH)
        assert caught.value.code == "storage_error"

    assert plays_snapshot(path) == before


def test_commit_lost_after_landing_returns_no_claim_and_recovers_uncertain(
    store: OutboxStore, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(store)

    with monkeypatch.context() as patch:
        fail_on(patch, "COMMIT", after=True)
        with pytest.raises(OutboxError) as caught:
            store.claim_in_flight("occ", DeliveryState.PENDING, EPOCH)
        assert caught.value.code == "storage_error"

    assert state_of(path) == "in_flight"
    assert store.recover_in_flight() == 1
    assert state_of(path) == "uncertain"


def test_failed_claim_update_returns_no_claim(
    store: OutboxStore, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(store)

    with monkeypatch.context() as patch:
        fail_on(patch, "UPDATE plays")
        with pytest.raises(OutboxError) as caught:
            store.claim_in_flight("occ", DeliveryState.PENDING, EPOCH)
        assert caught.value.code == "storage_error"

    assert state_of(path) == "pending"


def test_full_disk_error_blocks_claim(
    store: OutboxStore, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(store)
    before = plays_snapshot(path)

    with monkeypatch.context() as patch:
        fail_on(patch, "UPDATE plays", message="database or disk is full")
        with pytest.raises(OutboxError) as caught:
            store.claim_in_flight("occ", DeliveryState.PENDING, EPOCH)

    assert caught.value.code == "storage_error"
    assert "full" not in str(caught.value)
    assert plays_snapshot(path) == before


def test_claim_is_only_true_once_the_commit_returned(
    store: OutboxStore, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(store)
    states_at_commit: list[str] = []

    with monkeypatch.context() as patch:
        on_statement(patch, "COMMIT", lambda: states_at_commit.append("committing"))
        assert store.claim_in_flight("occ", DeliveryState.PENDING, EPOCH) is True

    assert states_at_commit == ["committing"]
    assert state_of(path) == "in_flight"


# Recovery


def test_recovery_moves_inflight_to_uncertain_never_pending(
    store: OutboxStore, path: Path
) -> None:
    add(store, "flying", start=1000)
    add(store, "queued", start=2000)
    add(store, "held", start=3000)
    store.hold("held", at(900), "history_unknown")
    store.claim_in_flight("flying", DeliveryState.PENDING, EPOCH)

    restarted = OutboxStore.open(path)

    assert state_of(path, "flying") == "in_flight"
    assert restarted.recover_in_flight() == 1
    assert dict(query(path, "SELECT id, state FROM plays")) == {
        "flying": "uncertain",
        "queued": "pending",
        "held": "held",
    }
    assert query(path, "SELECT attempts, reason FROM plays WHERE id = 'flying'") == [
        (1, "recovered_in_flight")
    ]
    assert restarted.recover_in_flight() == 0


def test_recovered_row_reaches_reads_but_no_claim(
    store: OutboxStore, path: Path
) -> None:
    add(store)
    store.claim_in_flight("occ", DeliveryState.PENDING, EPOCH)
    restarted = OutboxStore.open(path)
    restarted.recover_in_flight()

    item = restarted.next_ready(at(300))

    assert item is not None
    assert item.state is DeliveryState.UNCERTAIN
    assert item.metadata == META
    with pytest.raises(OutboxError) as caught:
        restarted.claim_in_flight("occ", item.state, EPOCH)
    assert caught.value.code == "illegal_transition"


def test_remote_accept_then_failed_result_commit_cannot_resend(
    store: OutboxStore, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(store)
    store.claim_in_flight("occ", DeliveryState.PENDING, EPOCH)

    with monkeypatch.context() as patch:
        fail_on(patch, "COMMIT")
        with pytest.raises(OutboxError) as caught:
            store.finalize("occ", DeliveryState.SENT, "accepted")
        assert caught.value.code == "storage_error"

    assert state_of(path) == "in_flight"
    assert store.meta_get("sent_total") == "0"
    assert query(path, "SELECT artist FROM plays") == [("canary-artist",)]
    store.recover_in_flight()
    assert state_of(path) == "uncertain"
    assert store.claim_in_flight("occ", DeliveryState.PENDING, EPOCH) is False

    store.finalize("occ", DeliveryState.SENT, "reconciled")

    assert store.meta_get("sent_total") == "1"


# Failed writes that callers must see


def test_failed_clear_raises_and_keeps_the_metadata(
    store: OutboxStore, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(store)
    before = plays_snapshot(path)

    with monkeypatch.context() as patch:
        fail_on(patch, "COMMIT")
        with pytest.raises(OutboxError) as caught:
            store.clear_queue()
        assert caught.value.code == "storage_error"

    assert plays_snapshot(path) == before
    assert query(path, "SELECT artist FROM plays") == [("canary-artist",)]


def test_clear_returns_only_after_its_commit(
    store: OutboxStore, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(store)
    seen: list[list[tuple[Any, ...]]] = []

    with monkeypatch.context() as patch:
        on_statement(
            patch,
            "COMMIT",
            lambda: seen.append(query(path, "SELECT state FROM plays")),
        )
        assert store.clear_queue() == 1

    # Another connection still reads the old row while the commit is pending.
    assert seen == [[("pending",)]]
    assert query(path, "SELECT state, artist FROM plays") == [("discarded", "")]


def test_failed_set_epoch_raises_and_keeps_the_old_epoch(
    store: OutboxStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        fail_on(patch, "COMMIT")
        with pytest.raises(OutboxError) as caught:
            store.set_epoch(9)
        assert caught.value.code == "storage_error"

    assert store.meta_get("epoch") == str(EPOCH)


def test_failed_rebind_moves_neither_the_rows_nor_the_epoch(
    store: OutboxStore, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(store)

    with monkeypatch.context() as patch:
        fail_on(patch, "COMMIT")
        with pytest.raises(OutboxError) as caught:
            store.rebind(9, ACCOUNT)
        assert caught.value.code == "storage_error"

    assert store.meta_get("epoch") == str(EPOCH)
    assert query(path, "SELECT epoch FROM plays") == [(EPOCH,)]


def test_failed_finalize_counts_nothing(store: OutboxStore, path: Path) -> None:
    add(store)
    write(
        path,
        "CREATE TRIGGER broken BEFORE UPDATE ON meta WHEN NEW.key = 'skipped_total' "
        "BEGIN SELECT RAISE(ABORT, 'injected'); END",
    )

    with pytest.raises(OutboxError) as caught:
        store.finalize("occ", DeliveryState.DUPLICATE, "history_match")
    assert caught.value.code == "storage_error"

    assert state_of(path) == "pending"
    assert query(path, "SELECT artist FROM plays") == [("canary-artist",)]
    assert store.meta_get("skipped_total") == "0"


def test_failure_mid_delta_leaves_every_slot_untouched(
    store: OutboxStore, path: Path
) -> None:
    kept = hint("kept")
    prior = hint("kept", "older", prior=True)
    store.apply_marker_delta(
        MarkerDelta(markers_set=(kept, prior, hint("cleared")), guards_set=("old",)),
        at(1),
    )
    before = query(path, "SELECT * FROM checkpoints ORDER BY lineage, slot")
    write(
        path,
        "CREATE TRIGGER broken BEFORE INSERT ON checkpoints WHEN NEW.slot = 'guard' "
        "BEGIN SELECT RAISE(ABORT, 'injected'); END",
    )

    with pytest.raises(OutboxError) as caught:
        store.apply_marker_delta(
            MarkerDelta(
                markers_set=(hint("new"),),
                lineages_cleared=("cleared",),
                priors_cleared=("kept",),
                guards_set=("new",),
                guards_cleared=("old",),
            ),
            at(2),
        )
    assert caught.value.code == "storage_error"

    assert query(path, "SELECT * FROM checkpoints ORDER BY lineage, slot") == before


def test_failed_lineage_purge_keeps_every_row(
    store: OutboxStore, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.apply_marker_delta(MarkerDelta(markers_set=(hint(),)), at(1))

    with monkeypatch.context() as patch:
        fail_on(patch, "COMMIT")
        with pytest.raises(OutboxError) as caught:
            store.purge_lineages(frozenset())
        assert caught.value.code == "storage_error"

    assert len(store.resume_state()[0]) == 1


def test_failed_maintenance_rolls_back_expiry_and_pruning(
    store: OutboxStore, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(store)
    before = plays_snapshot(path)

    with monkeypatch.context() as patch:
        fail_on(patch, "COMMIT")
        with pytest.raises(OutboxError) as caught:
            store.maintain(at(10**8))
        assert caught.value.code == "storage_error"

    assert plays_snapshot(path) == before


# How every operation talks to SQLite


def test_every_operation_is_one_durable_transaction_on_one_connection(
    store: OutboxStore, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statements: list[str] = []
    opened: list[sqlite3.Connection] = []
    closed: list[sqlite3.Connection] = []
    settings: list[dict[str, Any]] = []

    class Recording(sqlite3.Connection):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            settings.append({"args": args, **kwargs})
            opened.append(self)

        def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
            statements.append(sql)
            return super().execute(sql, *args)

        def close(self) -> None:
            closed.append(self)
            super().close()

    with monkeypatch.context() as patch:
        install_connection(patch, Recording)
        add(store)

    assert len(opened) == len(closed) == 1
    assert settings[0]["isolation_level"] is None
    assert settings[0]["timeout"] == BUSY_TIMEOUT_SECONDS
    assert settings[0]["args"][0].endswith("?mode=rw")
    assert settings[0]["uri"] is True
    assert statements[:4] == [
        "PRAGMA synchronous = FULL",
        "PRAGMA journal_mode = DELETE",
        "PRAGMA secure_delete = ON",
        "BEGIN IMMEDIATE",
    ]
    assert statements[-1] == "COMMIT"


def test_connection_closes_when_the_operation_fails(
    store: OutboxStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []

    class Failing(sqlite3.Connection):
        def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
            if sql == "COMMIT":
                raise sqlite3.OperationalError("injected")
            return super().execute(sql, *args)

        def close(self) -> None:
            closed.append(True)
            super().close()

    with monkeypatch.context() as patch:
        install_connection(patch, Failing)
        with pytest.raises(OutboxError) as caught:
            store.set_epoch(9)
        assert caught.value.code == "storage_error"

    assert closed == [True]


def test_path_with_uri_characters_opens_the_right_file(tmp_path: Path) -> None:
    folder = tmp_path / "config dir?x=1#y%20"
    folder.mkdir()
    path = folder / "outbox.sqlite3"

    created = OutboxStore.create(path)

    assert created.meta_get("schema_version") == "1"
    assert OutboxStore.open(path).meta_get("schema_version") == "1"
    assert sorted(item.name for item in folder.iterdir()) == ["outbox.sqlite3"]


# Files


def test_file_mode_is_private(
    store: OutboxStore, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = path.with_name(f"{path.name}-journal")
    modes: list[int] = []

    def record_journal_mode() -> None:
        modes.append(journal.stat().st_mode & 0o777)

    assert path.stat().st_mode & 0o777 == 0o600
    with monkeypatch.context() as patch:
        on_statement(patch, "COMMIT", record_journal_mode)
        add(store)

    assert modes == [0o600]
    assert not journal.exists()


def test_create_stays_private_under_an_open_umask(tmp_path: Path) -> None:
    previous = os.umask(0)
    try:
        OutboxStore.create(tmp_path / "open.sqlite3")
    finally:
        os.umask(previous)

    assert (tmp_path / "open.sqlite3").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660])
def test_open_refuses_a_file_others_can_reach(
    store: OutboxStore, path: Path, mode: int
) -> None:
    path.chmod(mode)

    with pytest.raises(OutboxError) as caught:
        OutboxStore.open(path)

    assert caught.value.code == "storage_permissions"


def test_missing_file_on_open_raises(path: Path) -> None:
    with pytest.raises(OutboxError) as caught:
        OutboxStore.open(path)

    assert caught.value.code == "storage_missing"
    assert not path.exists()


def test_corrupt_file_raises_and_is_not_recreated(
    store: OutboxStore, path: Path
) -> None:
    add(store)
    garbage = b"this is not a database at all " * 200
    path.write_bytes(garbage)

    with pytest.raises(OutboxError) as opening:
        OutboxStore.open(path)
    with pytest.raises(OutboxError) as using:
        add(store, "next")

    assert opening.value.code == "storage_corrupt"
    assert using.value.code == "storage_corrupt"
    assert path.read_bytes() == garbage

    # A file that disappears under a live store comes back only through create.
    path.unlink()
    with pytest.raises(OutboxError) as caught:
        add(store, "next")
    assert caught.value.code == "storage_error"

    assert not path.exists()


def test_damaged_pages_raise_and_the_file_stays_as_it_was(
    store: OutboxStore, path: Path
) -> None:
    for index in range(30):
        add(store, f"occ-{index}", start=index * 1000)
    content = bytearray(path.read_bytes())
    page_size = int.from_bytes(content[16:18], "big")
    content[page_size : page_size * 3] = b"\xff" * (page_size * 2)
    path.write_bytes(content)

    with pytest.raises(OutboxError) as caught:
        OutboxStore.open(path)

    assert caught.value.code == "storage_corrupt"
    assert path.read_bytes() == bytes(content)


def test_empty_file_is_not_treated_as_a_fresh_ledger(path: Path) -> None:
    path.touch(mode=0o600)

    with pytest.raises(OutboxError) as caught:
        OutboxStore.open(path)

    assert caught.value.code == "schema_mismatch"
    assert path.read_bytes() == b""


def test_foreign_schema_version_is_refused(store: OutboxStore, path: Path) -> None:
    write(path, "UPDATE meta SET value = '2' WHERE key = 'schema_version'")

    with pytest.raises(OutboxError) as caught:
        OutboxStore.open(path)

    assert caught.value.code == "schema_mismatch"


@pytest.mark.parametrize("table", ["plays", "checkpoints", "aliases", "meta"])
def test_open_refuses_a_ledger_without_a_required_table(
    store: OutboxStore, path: Path, table: str
) -> None:
    write(path, f"DROP TABLE {table}")

    with pytest.raises(OutboxError) as caught:
        OutboxStore.open(path)

    assert caught.value.code == "schema_mismatch"


@pytest.mark.parametrize(
    "key", ["schema_version", "epoch", "sent_total", "skipped_total", "ignored_total"]
)
def test_open_refuses_a_ledger_without_a_required_meta_key(
    store: OutboxStore, path: Path, key: str
) -> None:
    write(path, "DELETE FROM meta WHERE key = ?", key)

    with pytest.raises(OutboxError) as caught:
        OutboxStore.open(path)

    assert caught.value.code == "schema_mismatch"


@pytest.mark.parametrize("key", ["epoch", "sent_total"])
def test_open_refuses_a_meta_number_that_is_no_number(
    store: OutboxStore, path: Path, key: str
) -> None:
    write(path, "UPDATE meta SET value = 'canary' WHERE key = ?", key)

    with pytest.raises(OutboxError) as caught:
        OutboxStore.open(path)

    assert caught.value.code == "schema_mismatch"


NOT_A_TIME: Any = "canary"


def assert_only_the_code_shows(error: OutboxError, code: str) -> None:
    printed = "".join(traceback.format_exception(error))
    assert error.code == code
    assert str(error) == code
    assert "canary" not in printed
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


def test_text_sqlite_cannot_encode_fails_with_a_code_and_writes_nothing(
    store: OutboxStore, path: Path
) -> None:
    broken = TrackMetadata(artist="canary\ud800", title="canary-title")

    with pytest.raises(OutboxError) as caught:
        add(store, metadata=broken)

    assert_only_the_code_shows(caught.value, "invalid_data")
    assert query(path, "SELECT COUNT(*) FROM plays") == [(0,)]


def test_duration_that_is_no_number_fails_with_a_code(store: OutboxStore) -> None:
    with pytest.raises(OutboxError) as caught:
        store.enqueue(
            replace(qualified(), duration_seconds=float("nan")),
            ACCOUNT,
            EPOCH,
            at(300),
            at(300),
        )

    assert_only_the_code_shows(caught.value, "invalid_data")


def test_counter_lost_after_open_fails_with_a_code(
    store: OutboxStore, path: Path
) -> None:
    write(path, "DELETE FROM meta WHERE key = 'sent_total'")

    with pytest.raises(OutboxError) as caught:
        store.status(at(0))

    assert_only_the_code_shows(caught.value, "invalid_data")


def test_epoch_lost_after_open_fails_with_a_code(
    store: OutboxStore, path: Path
) -> None:
    write(path, "UPDATE meta SET value = 'canary' WHERE key = 'epoch'")

    with pytest.raises(OutboxError) as caught:
        store.rebind(5, ACCOUNT)

    assert_only_the_code_shows(caught.value, "invalid_data")


def test_stored_time_out_of_range_fails_with_a_code(
    store: OutboxStore, path: Path
) -> None:
    add(store)
    write(path, "UPDATE plays SET deadline = ?", 10**15)

    with pytest.raises(OutboxError) as caught:
        store.next_ready(at(300))

    assert_only_the_code_shows(caught.value, "invalid_data")


def test_stored_marker_it_cannot_read_fails_with_a_code(
    store: OutboxStore, path: Path
) -> None:
    write(
        path,
        "INSERT INTO checkpoints VALUES ('l', 'active', 'x', X'00', 'canary', 0)",
    )

    with pytest.raises(OutboxError) as caught:
        store.resume_state()

    assert_only_the_code_shows(caught.value, "invalid_data")


def test_every_public_store_operation_goes_through_the_error_boundary() -> None:
    public = {
        name: member
        for name, member in vars(OutboxStore).items()
        if not name.startswith("_")
    }

    # A classmethod object always has __wrapped__; the guard sits on its function.
    functions = {
        name: getattr(member, "__func__", member) for name, member in public.items()
    }

    assert len(public) >= 20
    assert {"create", "open"} <= functions.keys()
    assert [
        name for name, op in functions.items() if not hasattr(op, "__wrapped__")
    ] == []


def test_error_boundary_logs_the_operation_and_class_without_values(
    store: OutboxStore, caplog: pytest.LogCaptureFixture
) -> None:
    with pytest.raises(OutboxError) as caught:
        store.next_ready(NOT_A_TIME)

    assert caught.value.code == "invalid_data"
    assert [record.getMessage() for record in caplog.records] == [
        "Outbox operation next_ready failed: AttributeError"
    ]


def test_error_boundary_logs_no_value_from_the_failure(
    store: OutboxStore, caplog: pytest.LogCaptureFixture
) -> None:
    class Hostile:
        def utcoffset(self) -> None:
            raise ValueError("canary-title")

    with pytest.raises(OutboxError) as caught:
        store.next_ready(Hostile())  # type: ignore[arg-type]

    assert_only_the_code_shows(caught.value, "invalid_data")
    assert "ValueError" in caplog.text
    assert "canary" not in caplog.text


def test_argument_of_the_wrong_type_fails_with_a_code(store: OutboxStore) -> None:
    with pytest.raises(OutboxError) as caught:
        store.next_ready(NOT_A_TIME)

    assert_only_the_code_shows(caught.value, "invalid_data")


def test_open_of_a_healthy_file_keeps_every_row(store: OutboxStore, path: Path) -> None:
    add(store)
    store.apply_marker_delta(MarkerDelta(markers_set=(hint(),)), at(1))

    reopened = OutboxStore.open(path)

    item = reopened.next_ready(at(300))
    assert item is not None
    assert item.id == "occ"
    assert reopened.resume_state() == ((hint(),), frozenset())


# Async wrapper


async def test_async_outbox_runs_the_store_on_the_executor(
    hass: HomeAssistant, store: OutboxStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    threads: list[int] = []
    outbox = AsyncOutbox(store, hass.async_add_executor_job)

    on_statement(monkeypatch, "COMMIT", lambda: threads.append(threading.get_ident()))
    result = await outbox.async_enqueue(qualified(), ACCOUNT, EPOCH, at(300), at(300))
    claimed = await outbox.async_claim_in_flight("occ", DeliveryState.PENDING, EPOCH)
    status = await outbox.async_status(at(300))
    await outbox.async_close()

    assert len(threads) == 3
    assert threading.get_ident() not in threads
    assert result is EnqueueResult.ENQUEUED
    assert claimed is True
    assert status.in_flight_count == 1


async def test_async_outbox_mirrors_every_public_store_method(
    hass: HomeAssistant, store: OutboxStore
) -> None:
    outbox = AsyncOutbox(store, hass.async_add_executor_job)
    public = {
        name
        for name in vars(OutboxStore)
        if not name.startswith("_") and name not in {"create", "open"}
    }

    missing = {name for name in public if not hasattr(outbox, f"async_{name}")}

    assert public
    assert missing == set()


async def test_async_outbox_passes_a_store_failure_to_the_caller(
    hass: HomeAssistant, store: OutboxStore
) -> None:
    outbox = AsyncOutbox(store, hass.async_add_executor_job)

    with pytest.raises(OutboxError) as caught:
        await outbox.async_finalize("nobody", DeliveryState.SENT, "accepted")

    assert caught.value.code == "illegal_transition"
    await outbox.async_close()


async def test_cancelled_caller_is_awaited_by_close(
    hass: HomeAssistant,
    store: OutboxStore,
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add(store)
    committing = threading.Event()
    release = threading.Event()

    def hold_the_commit() -> None:
        committing.set()
        assert release.wait(10)

    outbox = AsyncOutbox(store, hass.async_add_executor_job)
    on_statement(monkeypatch, "COMMIT", hold_the_commit)

    caller = asyncio.create_task(
        outbox.async_claim_in_flight("occ", DeliveryState.PENDING, EPOCH)
    )
    await hass.async_add_executor_job(committing.wait, 10)
    assert committing.is_set()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    closing = asyncio.create_task(outbox.async_close())
    await asyncio.sleep(0)
    await asyncio.sleep(0.01)
    assert not closing.done()
    with pytest.raises(OutboxClosed):
        await outbox.async_status(at(300))

    release.set()
    await closing

    assert state_of(path) == "in_flight"


async def test_close_waits_for_a_thread_whose_wrapper_was_cancelled(
    store: OutboxStore, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # HA cancels its executor Future before STOP while the thread still commits.
    add(store)
    loop = asyncio.get_running_loop()
    entered = threading.Event()
    release = threading.Event()
    wrappers: list[asyncio.Future[Any]] = []

    def run_blocking(function: Any, *args: Any) -> asyncio.Future[Any]:
        wrappers.append(loop.run_in_executor(None, function, *args))
        return wrappers[-1]

    def hold_commit() -> None:
        entered.set()
        assert release.wait(10)

    outbox = AsyncOutbox(store, run_blocking)
    with monkeypatch.context() as patch:
        on_statement(patch, "COMMIT", hold_commit)
        caller = asyncio.create_task(
            outbox.async_claim_in_flight("occ", DeliveryState.PENDING, EPOCH)
        )
        await asyncio.to_thread(entered.wait, 10)
        wrappers[0].cancel()
        closing = asyncio.create_task(outbox.async_close())
        await asyncio.sleep(0.02)
        assert not closing.done()

        release.set()
        await closing

    assert state_of(path) == "in_flight"
    with pytest.raises(asyncio.CancelledError):
        await caller


async def test_close_waits_for_a_thread_held_before_its_connection_opens(
    store: OutboxStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = asyncio.get_running_loop()
    entered = threading.Event()
    release = threading.Event()
    wrappers: list[asyncio.Future[Any]] = []

    def run_blocking(function: Any, *args: Any) -> asyncio.Future[Any]:
        wrappers.append(loop.run_in_executor(None, function, *args))
        return wrappers[-1]

    def held_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        entered.set()
        assert release.wait(10)
        return REAL_CONNECT(*args, **kwargs)

    outbox = AsyncOutbox(store, run_blocking)
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", held_connect)
        caller = asyncio.create_task(outbox.async_set_epoch(9))
        await asyncio.to_thread(entered.wait, 10)
        wrappers[0].cancel()
        closing = asyncio.create_task(outbox.async_close())
        await asyncio.sleep(0.02)
        assert not closing.done()

        release.set()
        await closing

    assert store.meta_get("epoch") == "9"
    with pytest.raises(asyncio.CancelledError):
        await caller


async def test_close_is_not_stuck_on_work_cancelled_before_a_thread_took_it(
    store: OutboxStore,
) -> None:
    loop = asyncio.get_running_loop()
    release = threading.Event()
    wrappers: list[asyncio.Future[Any]] = []

    with ThreadPoolExecutor(max_workers=1) as executor:

        def run_blocking(function: Any, *args: Any) -> asyncio.Future[Any]:
            wrappers.append(loop.run_in_executor(executor, function, *args))
            return wrappers[-1]

        occupied = loop.run_in_executor(executor, release.wait, 10)
        outbox = AsyncOutbox(store, run_blocking)
        caller = asyncio.create_task(outbox.async_set_epoch(9))
        await asyncio.sleep(0)
        wrappers[0].cancel()

        await asyncio.wait_for(outbox.async_close(), 5)

        release.set()
        await occupied

    assert store.meta_get("epoch") == str(EPOCH)
    with pytest.raises(asyncio.CancelledError):
        await caller


async def test_work_a_thread_takes_after_close_gave_up_on_it_never_runs(
    store: OutboxStore,
) -> None:
    # A wrapper can be cancelled when the thread is about to take the call anyway.
    loop = asyncio.get_running_loop()
    taken: list[Any] = []
    wrapper: asyncio.Future[Any] = loop.create_future()

    def run_blocking(function: Any, *args: Any) -> asyncio.Future[Any]:
        taken.append((function, args))
        return wrapper

    outbox = AsyncOutbox(store, run_blocking)
    caller = asyncio.create_task(outbox.async_set_epoch(9))
    await asyncio.sleep(0)
    wrapper.cancel()
    await asyncio.wait_for(outbox.async_close(), 5)

    function, args = taken[0]
    with pytest.raises(OutboxClosed):
        await asyncio.to_thread(function, *args)

    assert store.meta_get("epoch") == str(EPOCH)
    with pytest.raises(asyncio.CancelledError):
        await caller


async def test_drain_waits_for_a_cancelled_wrapper_and_keeps_taking_work(
    store: OutboxStore, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(store)
    loop = asyncio.get_running_loop()
    entered = threading.Event()
    release = threading.Event()
    wrappers: list[asyncio.Future[Any]] = []

    def run_blocking(function: Any, *args: Any) -> asyncio.Future[Any]:
        wrappers.append(loop.run_in_executor(None, function, *args))
        return wrappers[-1]

    def hold_commit() -> None:
        entered.set()
        assert release.wait(10)

    outbox = AsyncOutbox(store, run_blocking)
    with monkeypatch.context() as patch:
        on_statement(patch, "COMMIT", hold_commit)
        caller = asyncio.create_task(
            outbox.async_claim_in_flight("occ", DeliveryState.PENDING, EPOCH)
        )
        await asyncio.to_thread(entered.wait, 10)
        wrappers[0].cancel()
        draining = asyncio.create_task(outbox.async_drain())
        await asyncio.sleep(0.02)
        assert not draining.done()

        release.set()
        await draining

    assert state_of(path) == "in_flight"
    assert await outbox.async_meta_get("epoch") == str(EPOCH)
    with pytest.raises(asyncio.CancelledError):
        await caller
    await outbox.async_close()


async def test_drain_with_nothing_running_returns_and_leaves_the_outbox_open(
    hass: HomeAssistant, store: OutboxStore
) -> None:
    outbox = AsyncOutbox(store, hass.async_add_executor_job)

    await outbox.async_drain()

    assert await outbox.async_meta_get("epoch") == str(EPOCH)
    await outbox.async_close()


async def test_close_is_repeatable_and_refuses_new_work(
    hass: HomeAssistant, store: OutboxStore
) -> None:
    outbox = AsyncOutbox(store, hass.async_add_executor_job)

    await outbox.async_close()
    await outbox.async_close()

    with pytest.raises(OutboxClosed) as caught:
        await outbox.async_meta_get("epoch")
    assert caught.value.code == "outbox_closed"
    assert isinstance(caught.value, OutboxError)


async def test_failure_of_an_abandoned_write_still_reaches_the_log(
    store: OutboxStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = asyncio.get_running_loop()
    reported: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    committing = threading.Event()
    release = threading.Event()

    def hold_then_fail() -> None:
        committing.set()
        assert release.wait(10)
        raise sqlite3.OperationalError("canary-title")

    outbox = AsyncOutbox(store, partial(loop.run_in_executor, None))
    with monkeypatch.context() as patch:
        on_statement(patch, "COMMIT", hold_then_fail)
        caller = asyncio.create_task(outbox.async_set_epoch(9))
        await asyncio.to_thread(committing.wait, 10)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        release.set()

        await outbox.async_close()
        await asyncio.sleep(0)

    assert store.meta_get("epoch") == str(EPOCH)
    assert [str(context["exception"]) for context in reported] == ["storage_error"]
