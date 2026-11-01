"""The outbox ledger on real SQLite files: dedupe, transitions, bounds, markers."""

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from freezegun.api import FrozenDateTimeFactory

from custom_components.personal_sonos_scrobbler import outbox as outbox_module
from custom_components.personal_sonos_scrobbler.const import MAX_SELECTED_PLAYERS
from custom_components.personal_sonos_scrobbler.normalize import (
    PlaybackSample,
    normalize_state,
)
from custom_components.personal_sonos_scrobbler.outbox import (
    ACTIVE_RETENTION_SECONDS,
    MAX_ACTIVE_PLAYS,
    MAX_TOMBSTONES,
    TOMBSTONE_RETENTION_SECONDS,
    DeliveryState,
    EnqueueResult,
    OutboxError,
    OutboxItem,
    OutboxStatus,
    OutboxStore,
)
from custom_components.personal_sonos_scrobbler.playback import (
    MarkerDelta,
    QualifiedPlay,
    ResumeHint,
    TrackMetadata,
)
from custom_components.personal_sonos_scrobbler.streams import StreamSet
from custom_components.personal_sonos_scrobbler.topology import (
    CanonicalGroup,
    Topology,
)
from tests.outbox_support import (
    ACCOUNT,
    EPOCH,
    KEY_A,
    KEY_B,
    META,
    OTHER_ACCOUNT,
    PLAY_COLUMNS,
    T0,
    add,
    at,
    fail_on,
    fill_aliases,
    fill_plays,
    hint,
    query,
    write,
)

TERMINAL_STATES = (
    DeliveryState.SENT,
    DeliveryState.DUPLICATE,
    DeliveryState.IGNORED,
    DeliveryState.REJECTED,
    DeliveryState.EXPIRED,
    DeliveryState.DISCARDED,
)
QUEUED_STATES = (DeliveryState.PENDING, DeliveryState.HELD)
NEVER_CLAIMABLE = (DeliveryState.UNCERTAIN, DeliveryState.IN_FLIGHT, *TERMINAL_STATES)
DAY = 86400
# Filler rows carry updated_at = their index: maintaining at this moment leaves the
# age limit out of it, so only the count limit can remove them.
FILLER_ERA = datetime(1970, 1, 1, tzinfo=UTC)


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "outbox.sqlite3"


@pytest.fixture
def store(path: Path) -> OutboxStore:
    created = OutboxStore.create(path)
    created.set_epoch(EPOCH)
    return created


@pytest.fixture
def frozen(freezer: FrozenDateTimeFactory) -> FrozenDateTimeFactory:
    """Pin the store's own clock to the synthetic timeline of these tests."""
    freezer.move_to(T0)
    return freezer


def state_of(path: Path, occurrence_id: str = "occ") -> str:
    return query(path, "SELECT state FROM plays WHERE id = ?", occurrence_id)[0][0]


def attempts_of(path: Path, occurrence_id: str = "occ") -> tuple[int, int]:
    rows = query(
        path,
        "SELECT attempts, read_attempts FROM plays WHERE id = ?",
        occurrence_id,
    )
    return rows[0]


def uncertain(
    store: OutboxStore, occurrence_id: str = "occ", *, start: float = 0.0
) -> None:
    assert add(store, occurrence_id, start=start) is EnqueueResult.ENQUEUED
    assert store.claim_in_flight(occurrence_id, DeliveryState.PENDING, EPOCH)
    store.mark_uncertain(occurrence_id, "response_lost")


def checkpoint_rows(path: Path) -> list[tuple[object, ...]]:
    return query(
        path,
        "SELECT lineage, slot, occurrence_id, track_key, started_at FROM checkpoints "
        "ORDER BY lineage, slot",
    )


# Schema


def test_create_writes_the_version_and_seeds_the_meta_table(path: Path) -> None:
    OutboxStore.create(path)

    assert dict(query(path, "SELECT key, value FROM meta")) == {
        "schema_version": "1",
        "epoch": "0",
        "sent_total": "0",
        "skipped_total": "0",
        "ignored_total": "0",
    }


def test_create_refuses_an_existing_file_and_leaves_it_alone(path: Path) -> None:
    path.write_bytes(b"precious")
    path.chmod(0o600)

    with pytest.raises(OutboxError) as caught:
        OutboxStore.create(path)

    assert caught.value.code == "storage_exists"
    assert path.read_bytes() == b"precious"


def test_schema_refuses_readable_metadata_on_a_terminal_row(
    store: OutboxStore, path: Path
) -> None:
    add(store)

    with pytest.raises(sqlite3.IntegrityError):
        write(path, "UPDATE plays SET state = 'sent' WHERE id = 'occ'")


def test_schema_refuses_an_unknown_state(store: OutboxStore, path: Path) -> None:
    add(store)

    with pytest.raises(sqlite3.IntegrityError):
        write(path, "UPDATE plays SET state = 'resend' WHERE id = 'occ'")


# Enqueue and dedupe


def test_enqueue_is_atomic_with_checkpoint_and_dedupe(
    store: OutboxStore, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.apply_marker_delta(MarkerDelta(markers_set=(hint(),)), at(10))

    def updated_at() -> list[tuple[int]]:
        return query(path, "SELECT updated_at FROM checkpoints")

    before, touched_before = checkpoint_rows(path), updated_at()
    with monkeypatch.context() as patch:
        fail_on(patch, "COMMIT")
        with pytest.raises(OutboxError) as caught:
            add(store, now=300)
        assert caught.value.code == "storage_error"

    assert query(path, "SELECT COUNT(*) FROM plays") == [(0,)]
    assert checkpoint_rows(path) == before
    assert updated_at() == touched_before

    assert add(store, now=300) is EnqueueResult.ENQUEUED

    assert query(path, "SELECT COUNT(*) FROM plays") == [(1,)]
    assert checkpoint_rows(path) == before
    assert updated_at() == [(int(at(300).timestamp()),)]


def test_enqueue_never_inserts_a_marker(store: OutboxStore, path: Path) -> None:
    add(store)

    assert checkpoint_rows(path) == []


def test_reordered_producer_keeps_the_marker_on_the_later_track(
    store: OutboxStore, path: Path
) -> None:
    store.apply_marker_delta(MarkerDelta(markers_set=(hint(occurrence_id="a"),)), at(1))
    moved = hint(occurrence_id="b", key=KEY_B, start=200)
    store.apply_marker_delta(MarkerDelta(markers_set=(moved,)), at(201))

    assert add(store, "a") is EnqueueResult.ENQUEUED

    assert store.resume_state() == ((moved,), frozenset())


def test_active_id_is_a_no_op_that_keeps_the_first_payload(
    store: OutboxStore, path: Path
) -> None:
    add(store, start=0)
    before = query(path, f"SELECT {PLAY_COLUMNS} FROM plays")
    other = TrackMetadata(artist="other-artist", title="other-title")

    result = add(store, start=5000, key=KEY_B, metadata=other, now=9000)

    assert result is EnqueueResult.DUPLICATE_OCCURRENCE
    assert query(path, f"SELECT {PLAY_COLUMNS} FROM plays") == before


@pytest.mark.parametrize("state", TERMINAL_STATES)
def test_finalized_id_stays_known_after_its_metadata_is_gone(
    store: OutboxStore, path: Path, state: DeliveryState
) -> None:
    add(store)
    store.finalize("occ", state, "done")

    assert add(store) is EnqueueResult.DUPLICATE_OCCURRENCE
    assert add(store, start=5000, key=KEY_B) is EnqueueResult.DUPLICATE_OCCURRENCE

    assert query(path, "SELECT id, state, artist FROM plays") == [
        ("occ", state.value, "")
    ]


def test_expired_and_discarded_paths_also_keep_the_id_known(
    store: OutboxStore, path: Path, frozen: FrozenDateTimeFactory
) -> None:
    add(store, "expires", now=0)
    store.maintain(at(ACTIVE_RETENTION_SECONDS))
    add(store, "cleared", start=5000, key=KEY_B, now=5000)
    store.clear_queue()

    assert add(store, "expires", now=6000) is EnqueueResult.DUPLICATE_OCCURRENCE
    assert add(store, "cleared", now=6000) is EnqueueResult.DUPLICATE_OCCURRENCE


@pytest.mark.parametrize(
    ("offset", "duration", "expected"),
    [
        (-31, 39, EnqueueResult.ENQUEUED),
        (-30, 39, EnqueueResult.OVERLAPPING),
        (0, 200, EnqueueResult.OVERLAPPING),
        (190, 200, EnqueueResult.OVERLAPPING),
        (191, 200, EnqueueResult.ENQUEUED),
        (200, 200, EnqueueResult.ENQUEUED),
    ],
)
def test_overlap_suppressed_genuine_repeat_allowed(
    store: OutboxStore,
    path: Path,
    offset: int,
    duration: int,
    expected: EnqueueResult,
) -> None:
    add(store, "first", start=1000, duration=200)

    assert add(store, "second", start=1000 + offset, duration=duration) is expected

    rows = query(path, "SELECT COUNT(*) FROM plays")[0][0]
    assert rows == (2 if expected is EnqueueResult.ENQUEUED else 1)


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (-31, EnqueueResult.OVERLAPPING),
        (-190, EnqueueResult.OVERLAPPING),
        (-191, EnqueueResult.ENQUEUED),
        (-200, EnqueueResult.ENQUEUED),
    ],
)
def test_overlap_judges_a_later_arrival_with_an_earlier_start_by_its_own_span(
    store: OutboxStore, path: Path, offset: int, expected: EnqueueResult
) -> None:
    add(store, "first", start=1000, duration=200)

    assert add(store, "second", start=1000 + offset, duration=200) is expected


@pytest.mark.parametrize("starts", [(0, 100), (100, 0)])
def test_overlapping_groups_coalesce_in_either_arrival_order(
    store: OutboxStore, path: Path, starts: tuple[int, int]
) -> None:
    first, second = starts

    assert add(store, "first", start=first, duration=400) is EnqueueResult.ENQUEUED
    assert add(store, "second", start=second, duration=400) is (
        EnqueueResult.OVERLAPPING
    )

    assert query(path, "SELECT COUNT(*) FROM plays") == [(1,)]


@pytest.mark.parametrize("starts", [(0, 400), (400, 0)])
def test_consecutive_repeats_survive_in_either_arrival_order(
    store: OutboxStore, path: Path, starts: tuple[int, int]
) -> None:
    first, second = starts

    assert add(store, "first", start=first, duration=400) is EnqueueResult.ENQUEUED
    assert add(store, "second", start=second, duration=400) is EnqueueResult.ENQUEUED


def test_overlap_is_per_account_and_per_track(store: OutboxStore) -> None:
    add(store, "first", start=1000)

    assert add(store, "track", start=1000, key=KEY_B) is EnqueueResult.ENQUEUED
    assert add(store, "acct", start=1000, account=OTHER_ACCOUNT) is (
        EnqueueResult.ENQUEUED
    )


@pytest.mark.parametrize("state", TERMINAL_STATES)
def test_overlap_holds_against_a_tombstone(
    store: OutboxStore, state: DeliveryState
) -> None:
    add(store, "first", start=1000)
    store.finalize("first", state, "done")

    assert add(store, "second", start=1050) is EnqueueResult.OVERLAPPING


def test_overlapping_id_stays_known_as_an_alias_of_the_canonical_play(
    store: OutboxStore, path: Path
) -> None:
    add(store, "first", start=1000)

    assert add(store, "second", start=1000) is EnqueueResult.OVERLAPPING

    assert query(path, "SELECT id FROM plays") == [("first",)]
    assert query(path, "SELECT alias_id, play_id FROM aliases") == [("second", "first")]
    assert add(store, "second", start=1000) is EnqueueResult.DUPLICATE_OCCURRENCE
    assert add(store, "second", start=9000) is EnqueueResult.DUPLICATE_OCCURRENCE
    assert query(path, "SELECT COUNT(*) FROM aliases") == [(1,)]


@pytest.mark.parametrize("state", TERMINAL_STATES)
def test_alias_stays_known_after_its_canonical_metadata_is_gone(
    store: OutboxStore, state: DeliveryState
) -> None:
    add(store, "first", start=1000)
    add(store, "second", start=1000)

    store.finalize("first", state, "done")

    assert add(store, "second", start=1000) is EnqueueResult.DUPLICATE_OCCURRENCE


def test_alias_table_holds_identity_only(store: OutboxStore, path: Path) -> None:
    columns = {row[1] for row in query(path, "PRAGMA table_info(aliases)")}

    assert columns == {"alias_id", "play_id", "created_at"}


def test_overlapping_enqueue_neither_writes_nor_touches_a_marker(
    store: OutboxStore, path: Path
) -> None:
    add(store, "first", start=1000)
    store.apply_marker_delta(
        MarkerDelta(markers_set=(hint(occurrence_id="second"),)), at(1)
    )
    before = query(path, "SELECT * FROM checkpoints")

    assert add(store, "second", start=1000, now=900) is EnqueueResult.OVERLAPPING
    assert add(store, "third", start=1000, now=900) is EnqueueResult.OVERLAPPING

    assert query(path, "SELECT * FROM checkpoints") == before


def test_genuine_repeat_passes_after_an_alias_was_recorded(
    store: OutboxStore,
) -> None:
    add(store, "first", start=1000)
    add(store, "second", start=1000)

    assert add(store, "repeat", start=1200) is EnqueueResult.ENQUEUED


def test_short_track_resume_cannot_requalify(store: OutboxStore) -> None:
    # d = 40 needs 20 s of credit; the end margin of 10 s lies below that.
    add(store, "first", start=1000, duration=40)
    store.finalize("first", DeliveryState.SENT, "accepted")

    for index, resumed_start in enumerate((1000, 1015, 1030)):
        assert add(store, f"resume-{index}", start=resumed_start, duration=40) is (
            EnqueueResult.OVERLAPPING
        )
    assert add(store, "repeat", start=1040, duration=40) is EnqueueResult.ENQUEUED


def test_duration_is_stored_rounded_and_overlap_uses_it(
    store: OutboxStore, path: Path
) -> None:
    add(store, "first", start=1000, duration=199.6)

    assert query(path, "SELECT duration FROM plays") == [(200,)]
    assert add(store, "inside", start=1190) is EnqueueResult.OVERLAPPING
    assert add(store, "outside", start=1191) is EnqueueResult.ENQUEUED


A, B, D = "media_player.a", "media_player.b", "media_player.d"


def sonos(title: str, seconds: float, position: float) -> PlaybackSample:
    moment = at(seconds)
    return normalize_state(
        "playing",
        {
            "source": "Spotify Connect",
            "media_artist": "Artist",
            "media_title": title,
            "media_duration": 600,
            "queue_position": 1,
            "media_position": position,
            "media_position_updated_at": moment,
        },
        moment,
    )


def groups(*members: tuple[str, ...]) -> Topology:
    return Topology(
        tuple(CanonicalGroup(group[0], frozenset(group)) for group in members),
        frozenset(),
    )


def test_handover_through_an_unselected_coordinator_keeps_one_row(
    store: OutboxStore, path: Path
) -> None:
    streams = StreamSet(iter(("x-1", "own", "x-2", "z", "y", "w")).__next__)
    streams.set_lineages({A: "registry-a", B: "registry-b"})
    streams.seed_resume_hints((), ())
    emitted: list[QualifiedPlay] = []
    for moment in range(0, 270, 15):
        emitted += streams.apply(
            groups((B, D), (A,)),
            {B: sonos("X", moment, moment), A: sonos("Other", moment, moment)},
            float(moment),
        )
    for moment in range(270, 540, 15):
        emitted += streams.apply(
            groups((D, A), (B,)),
            {D: sonos("X", moment, moment), B: sonos("Own", moment, moment - 270)},
            float(moment),
        )
    titled_x = [play for play in emitted if play.metadata.title == "X"]
    assert len({play.occurrence_id for play in titled_x}) == 2
    assert len({play.started_at for play in titled_x}) == 1

    results = [
        store.enqueue(play, ACCOUNT, EPOCH, at(600), at(600)) for play in titled_x
    ]

    assert results == [EnqueueResult.ENQUEUED, EnqueueResult.OVERLAPPING]
    assert query(
        path, "SELECT COUNT(*) FROM plays WHERE track_key = ?", titled_x[0].track_key
    ) == [(1,)]


# Queue bound


def test_queue_full_rejects_visibly_and_evicts_nothing(
    store: OutboxStore, path: Path
) -> None:
    fill_plays(path, MAX_ACTIVE_PLAYS - 4)
    add(store, "pending", key=b"1" * 16)
    add(store, "held", key=b"2" * 16)
    store.hold("held", at(9000), "history_unknown")
    add(store, "flying", key=b"3" * 16)
    assert store.claim_in_flight("flying", DeliveryState.PENDING, EPOCH)
    uncertain(store, "unresolved")
    before = query(path, f"SELECT {PLAY_COLUMNS} FROM plays ORDER BY id")
    assert len(before) == MAX_ACTIVE_PLAYS

    result = add(store, "overflow", key=KEY_B)

    assert result is EnqueueResult.QUEUE_FULL
    assert query(path, f"SELECT {PLAY_COLUMNS} FROM plays ORDER BY id") == before


def test_full_queue_accepts_again_after_a_row_turns_terminal(
    store: OutboxStore, path: Path
) -> None:
    fill_plays(path, MAX_ACTIVE_PLAYS)
    assert add(store, "overflow", key=KEY_B) is EnqueueResult.QUEUE_FULL

    store.finalize("filler-0", DeliveryState.SENT, "accepted")

    assert add(store, "overflow", key=KEY_B) is EnqueueResult.ENQUEUED


def test_full_queue_still_answers_a_known_id_as_known(
    store: OutboxStore, path: Path
) -> None:
    fill_plays(path, MAX_ACTIVE_PLAYS)

    assert add(store, "filler-0", key=KEY_B) is EnqueueResult.DUPLICATE_OCCURRENCE


# Compare and swap


def test_first_claim_persists_and_counts_the_attempt(
    store: OutboxStore, path: Path
) -> None:
    add(store)

    assert store.claim_in_flight("occ", DeliveryState.PENDING, EPOCH) is True

    assert state_of(path) == "in_flight"
    assert attempts_of(path) == (1, 0)


def test_second_claim_loses_compare_and_swap(store: OutboxStore, path: Path) -> None:
    add(store)
    assert store.claim_in_flight("occ", DeliveryState.PENDING, EPOCH)

    assert store.claim_in_flight("occ", DeliveryState.PENDING, EPOCH) is False

    assert attempts_of(path) == (1, 0)


def test_claim_of_an_unknown_id_fails(store: OutboxStore) -> None:
    assert store.claim_in_flight("nobody", DeliveryState.PENDING, EPOCH) is False


def test_stale_epoch_claim_fails(store: OutboxStore, path: Path) -> None:
    add(store)
    store.set_epoch(EPOCH + 1)

    assert store.claim_in_flight("occ", DeliveryState.PENDING, EPOCH) is False
    assert store.claim_in_flight("occ", DeliveryState.PENDING, EPOCH + 1) is False

    assert state_of(path) == "pending"
    assert attempts_of(path) == (0, 0)


def test_rebind_reaches_rows_of_older_epoch(store: OutboxStore, path: Path) -> None:
    add(store, "queued", start=1000)
    add(store, "waiting", start=2000)
    store.hold("waiting", at(400), "history_unknown")
    add(store, "foreign", start=3000, account=OTHER_ACCOUNT)
    add(store, "done", start=4000)
    store.finalize("done", DeliveryState.SENT, "accepted")

    store.rebind(5, ACCOUNT)

    assert dict(query(path, "SELECT id, epoch FROM plays")) == {
        "queued": 5,
        "waiting": 5,
        "foreign": EPOCH,
        "done": EPOCH,
    }
    assert store.meta_get("epoch") == "5"
    assert store.claim_in_flight("queued", DeliveryState.PENDING, 5) is True
    assert store.claim_in_flight("foreign", DeliveryState.PENDING, 5) is False


def test_rebind_refuses_to_lower_the_epoch(store: OutboxStore, path: Path) -> None:
    add(store)
    store.set_epoch(9)

    with pytest.raises(OutboxError) as caught:
        store.rebind(8, ACCOUNT)

    assert caught.value.code == "stale_epoch"
    assert store.meta_get("epoch") == "9"
    assert dict(query(path, "SELECT id, epoch FROM plays")) == {"occ": EPOCH}


def test_set_epoch_is_monotonic(store: OutboxStore) -> None:
    store.set_epoch(7)
    store.set_epoch(3)

    assert store.meta_get("epoch") == "7"

    store.set_epoch(8)

    assert store.meta_get("epoch") == "8"


def test_a_lowered_epoch_cannot_reopen_a_fenced_claim(store: OutboxStore) -> None:
    add(store)
    store.set_epoch(EPOCH + 1)
    store.set_epoch(EPOCH)

    assert store.claim_in_flight("occ", DeliveryState.PENDING, EPOCH) is False


@pytest.mark.parametrize("expected", NEVER_CLAIMABLE)
def test_claim_refuses_every_state_but_queued_ones(
    store: OutboxStore, path: Path, expected: DeliveryState
) -> None:
    add(store)

    with pytest.raises(OutboxError) as caught:
        store.claim_in_flight("occ", expected, EPOCH)

    assert caught.value.code == "illegal_transition"
    assert attempts_of(path) == (0, 0)


def test_uncertain_row_cannot_be_claimed_even_by_its_own_state(
    store: OutboxStore, path: Path
) -> None:
    uncertain(store)

    with pytest.raises(OutboxError) as caught:
        store.claim_in_flight("occ", DeliveryState.UNCERTAIN, EPOCH)
    assert caught.value.code == "illegal_transition"

    assert store.claim_in_flight("occ", DeliveryState.PENDING, EPOCH) is False
    assert store.claim_in_flight("occ", DeliveryState.HELD, EPOCH) is False
    assert state_of(path) == "uncertain"
    assert attempts_of(path) == (1, 0)


# Transitions


def test_uncertain_row_has_no_way_back_to_a_sendable_state(
    store: OutboxStore, path: Path
) -> None:
    uncertain(store)

    for transition in (
        lambda: store.hold("occ", at(900), "history_unknown"),
        lambda: store.retry_later("occ", at(900), "rate_limited"),
        lambda: store.mark_uncertain("occ", "response_lost"),
    ):
        with pytest.raises(OutboxError) as caught:
            transition()
        assert caught.value.code == "illegal_transition"

    assert state_of(path) == "uncertain"
    assert store.recover_in_flight() == 0
    assert state_of(path) == "uncertain"


def test_reschedule_uncertain_counts_a_read_and_keeps_the_state(
    store: OutboxStore, path: Path
) -> None:
    uncertain(store)

    store.reschedule_uncertain("occ", at(900), "history_unknown")

    assert state_of(path) == "uncertain"
    assert attempts_of(path) == (1, 1)
    assert store.next_ready(at(899)) is None
    ready = store.next_ready(at(900))
    assert ready is not None
    assert ready.state is DeliveryState.UNCERTAIN
    assert query(path, "SELECT reason FROM plays") == [("history_unknown",)]


def test_reschedule_refuses_any_row_that_is_not_uncertain(store: OutboxStore) -> None:
    add(store)

    with pytest.raises(OutboxError) as caught:
        store.reschedule_uncertain("occ", at(900), "history_unknown")

    assert caught.value.code == "illegal_transition"


def test_retry_later_requires_an_attempt_in_flight(
    store: OutboxStore, path: Path
) -> None:
    add(store)

    with pytest.raises(OutboxError) as caught:
        store.retry_later("occ", at(900), "rate_limited")

    assert caught.value.code == "illegal_transition"
    assert state_of(path) == "pending"


def test_retry_later_returns_the_claim_to_pending_for_a_new_preflight(
    store: OutboxStore, path: Path
) -> None:
    add(store)
    store.claim_in_flight("occ", DeliveryState.PENDING, EPOCH)

    store.retry_later("occ", at(900), "rate_limited")

    assert state_of(path) == "pending"
    assert store.next_ready(at(899)) is None
    assert store.claim_in_flight("occ", DeliveryState.PENDING, EPOCH) is True
    assert attempts_of(path) == (2, 0)


def test_hold_refuses_an_attempt_in_flight(store: OutboxStore, path: Path) -> None:
    add(store)
    store.claim_in_flight("occ", DeliveryState.PENDING, EPOCH)

    with pytest.raises(OutboxError) as caught:
        store.hold("occ", at(900), "history_unknown")

    assert caught.value.code == "illegal_transition"
    assert state_of(path) == "in_flight"


def test_hold_counts_a_read_and_stays_claimable(store: OutboxStore, path: Path) -> None:
    add(store)

    store.hold("occ", at(900), "history_unknown")
    store.hold("occ", at(1000), "history_unknown")

    assert state_of(path) == "held"
    assert attempts_of(path) == (0, 2)
    assert store.next_ready(at(999)) is None
    assert store.claim_in_flight("occ", DeliveryState.HELD, EPOCH) is True


def test_mark_uncertain_requires_an_attempt_in_flight(store: OutboxStore) -> None:
    add(store)

    with pytest.raises(OutboxError) as caught:
        store.mark_uncertain("occ", "fenced_before_send")

    assert caught.value.code == "illegal_transition"


@pytest.mark.parametrize(
    "state", [DeliveryState.PENDING, DeliveryState.IN_FLIGHT, DeliveryState.HELD]
)
def test_finalize_refuses_a_state_that_is_not_terminal(
    store: OutboxStore, path: Path, state: DeliveryState
) -> None:
    add(store)

    with pytest.raises(OutboxError) as caught:
        store.finalize("occ", state, "done")

    assert caught.value.code == "illegal_transition"
    assert state_of(path) == "pending"


@pytest.mark.parametrize("second", TERMINAL_STATES)
def test_terminal_row_stays_where_it_is_and_counts_once(
    store: OutboxStore, path: Path, second: DeliveryState
) -> None:
    add(store)
    store.finalize("occ", DeliveryState.SENT, "accepted")

    with pytest.raises(OutboxError) as caught:
        store.finalize("occ", second, "again")

    assert caught.value.code == "illegal_transition"
    assert state_of(path) == "sent"
    assert store.meta_get("sent_total") == "1"


def test_transition_of_an_unknown_id_is_refused(store: OutboxStore) -> None:
    with pytest.raises(OutboxError) as caught:
        store.finalize("nobody", DeliveryState.SENT, "accepted")

    assert caught.value.code == "illegal_transition"


def test_reason_must_be_a_fixed_code(store: OutboxStore, path: Path) -> None:
    add(store)

    with pytest.raises(OutboxError) as caught:
        store.hold("occ", at(900), "provider said: canary-title")

    assert caught.value.code == "invalid_reason"
    assert state_of(path) == "pending"


def test_naive_time_is_refused(store: OutboxStore) -> None:
    with pytest.raises(OutboxError) as caught:
        store.next_ready(at(0).replace(tzinfo=None))

    assert caught.value.code == "naive_time"


def test_payload_is_unchanged_across_retry(store: OutboxStore) -> None:
    add(store, start=1000.75)
    first = store.next_ready(at(300))
    assert first is not None

    for wake in (400, 500, 600):
        assert store.claim_in_flight("occ", DeliveryState.PENDING, EPOCH)
        store.retry_later("occ", at(wake), "rate_limited")
    store.hold("occ", at(700), "history_unknown")

    retried = store.next_ready(at(700))
    assert retried is not None
    assert retried.metadata == first.metadata == META
    assert (retried.id, retried.track_key, retried.account_hash) == (
        first.id,
        first.track_key,
        first.account_hash,
    )
    assert (retried.started_at, retried.duration) == (first.started_at, first.duration)
    assert (retried.attempts, retried.read_attempts) == (3, 1)
    assert retried.deadline == first.deadline


def test_item_carries_the_values_a_provider_call_needs(store: OutboxStore) -> None:
    add(store, start=10.5, duration=199.6, now=300, ready=600)

    item = store.next_ready(at(600))

    assert item == OutboxItem(
        id="occ",
        account_hash=ACCOUNT,
        track_key=KEY_A,
        started_at=int(T0.timestamp()) + 10,
        duration=200,
        attempts=0,
        read_attempts=0,
        state=DeliveryState.PENDING,
        not_before=at(600),
        deadline=at(300 + ACTIVE_RETENTION_SECONDS),
        metadata=META,
    )


def test_missing_album_comes_back_as_none(store: OutboxStore) -> None:
    add(store, metadata=TrackMetadata(artist="a", title="t"))

    item = store.next_ready(at(300))

    assert item is not None
    assert item.metadata.album is None


@pytest.mark.parametrize("state", TERMINAL_STATES)
def test_terminal_strips_metadata_keeps_tombstone(
    store: OutboxStore, path: Path, state: DeliveryState
) -> None:
    add(store, start=1000)
    assert b"canary-artist" in path.read_bytes()

    store.finalize("occ", state, "done")

    assert query(
        path,
        "SELECT id, account_hash, track_key, started_at, duration, artist, title, "
        "album, state, reason FROM plays",
    ) == [
        (
            "occ",
            ACCOUNT,
            KEY_A,
            int(T0.timestamp()) + 1000,
            200,
            "",
            "",
            None,
            state.value,
            "done",
        )
    ]
    content = path.read_bytes()
    for canary in (b"canary-artist", b"canary-title", b"canary-album"):
        assert canary not in content
    assert store.next_ready(at(10**7)) is None


def test_counters_rise_only_for_their_outcome(store: OutboxStore) -> None:
    outcomes = (
        DeliveryState.SENT,
        DeliveryState.SENT,
        DeliveryState.DUPLICATE,
        DeliveryState.IGNORED,
        DeliveryState.REJECTED,
        DeliveryState.EXPIRED,
        DeliveryState.DISCARDED,
    )
    for index, state in enumerate(outcomes):
        add(store, f"occ-{index}", start=1000 * index)
        store.finalize(f"occ-{index}", state, "done")

    totals = [
        store.meta_get(f"{name}_total") for name in ("sent", "skipped", "ignored")
    ]
    assert totals == ["2", "1", "1"]


# Reading the queue


def test_next_ready_serves_the_oldest_start_first(store: OutboxStore) -> None:
    add(store, "late", start=2000, now=100)
    add(store, "early", start=1000, now=200)
    add(store, "future", start=500, now=200, ready=900)
    add(store, "newest", start=1500, now=250)

    first = store.next_ready(at(300))

    assert first is not None
    assert first.id == "early"
    store.finalize("early", DeliveryState.SENT, "accepted")
    second = store.next_ready(at(300))
    assert second is not None
    assert second.id == "newest"


def test_next_ready_skips_a_row_in_flight(store: OutboxStore) -> None:
    add(store)
    store.claim_in_flight("occ", DeliveryState.PENDING, EPOCH)

    assert store.next_ready(at(10**7)) is None


def test_next_wake_is_the_earliest_waiting_row(store: OutboxStore) -> None:
    assert store.next_wake() is None

    add(store, "later", start=0, now=300, ready=900)
    add(store, "sooner", start=1000, now=300, ready=600)
    add(store, "flying", start=2000, now=300, ready=100)
    store.claim_in_flight("flying", DeliveryState.PENDING, EPOCH)

    assert store.next_wake() == at(600)

    store.finalize("sooner", DeliveryState.SENT, "accepted")

    assert store.next_wake() == at(900)


def test_status_counts_without_metadata(store: OutboxStore, path: Path) -> None:
    empty = store.status(at(0))
    assert empty == OutboxStatus(
        pending_count=0,
        held_count=0,
        in_flight_count=0,
        uncertain_count=0,
        sent_total=0,
        skipped_total=0,
        ignored_total=0,
        oldest_queue_age_seconds=None,
    )

    add(store, "oldest", start=0, now=100)
    add(store, "held", start=1000, now=200)
    store.hold("held", at(900), "history_unknown")
    add(store, "flying", start=2000, now=300)
    store.claim_in_flight("flying", DeliveryState.PENDING, EPOCH)
    uncertain(store, "lost", start=3000)
    add(store, "done", start=4000, now=50)
    store.finalize("done", DeliveryState.SENT, "accepted")

    assert store.status(at(160)) == OutboxStatus(
        pending_count=1,
        held_count=1,
        in_flight_count=1,
        uncertain_count=1,
        reconcilable_count=1,
        sent_total=1,
        skipped_total=0,
        ignored_total=0,
        oldest_queue_age_seconds=60,
    )


def test_status_age_never_goes_negative_after_a_clock_jump(store: OutboxStore) -> None:
    add(store, now=300)

    assert store.status(at(0)).oldest_queue_age_seconds == 0


# Retention


def test_expiry_runs_without_new_plays(
    store: OutboxStore, path: Path, frozen: FrozenDateTimeFactory
) -> None:
    add(store, "queued", start=1000, now=100)
    add(store, "held", start=2000, now=100)
    store.hold("held", at(900), "history_unknown")
    uncertain(store, "lost")
    deadline = 100 + ACTIVE_RETENTION_SECONDS

    store.maintain(at(deadline - 1))

    assert state_of(path, "queued") == "pending"

    store.maintain(at(deadline + 200))

    assert dict(query(path, "SELECT id, state FROM plays")) == {
        "queued": "expired",
        "held": "expired",
        "lost": "expired",
    }
    assert query(path, "SELECT DISTINCT artist, title, album FROM plays") == [
        ("", "", None)
    ]
    assert b"canary-artist" not in path.read_bytes()


def test_expiry_leaves_an_attempt_in_flight_to_its_worker(
    store: OutboxStore, path: Path, frozen: FrozenDateTimeFactory
) -> None:
    add(store, now=100)
    store.claim_in_flight("occ", DeliveryState.PENDING, EPOCH)

    store.maintain(at(100 + ACTIVE_RETENTION_SECONDS + 1))

    assert state_of(path) == "in_flight"


def test_tombstone_age_limit_applies_from_its_last_change(
    store: OutboxStore, path: Path, frozen: FrozenDateTimeFactory
) -> None:
    add(store, "old", start=1000)
    store.finalize("old", DeliveryState.SENT, "accepted")
    frozen.tick(timedelta(days=1))
    add(store, "young", start=2000)
    store.finalize("young", DeliveryState.SENT, "accepted")

    store.maintain(at(TOMBSTONE_RETENTION_SECONDS - 1))
    assert len(query(path, "SELECT id FROM plays")) == 2

    store.maintain(at(TOMBSTONE_RETENTION_SECONDS))
    assert query(path, "SELECT id FROM plays") == [("young",)]


def test_tombstone_bounds_enforced(store: OutboxStore, path: Path) -> None:
    fill_plays(path, MAX_TOMBSTONES + 5, state="sent")
    store.apply_marker_delta(MarkerDelta(guards_set=("registry-a",)), at(0))

    store.maintain(FILLER_ERA)

    assert query(path, "SELECT COUNT(*) FROM plays") == [(MAX_TOMBSTONES,)]
    assert query(path, "SELECT MIN(updated_at) FROM plays") == [(5,)]


def test_tombstone_cap_never_touches_active_rows(
    store: OutboxStore, path: Path
) -> None:
    fill_plays(path, MAX_TOMBSTONES + 1, state="sent")
    add(store, key=KEY_B)

    store.maintain(FILLER_ERA)

    assert state_of(path) == "pending"


def test_tombstone_stays_known_while_a_marker_references_it(
    store: OutboxStore, path: Path, frozen: FrozenDateTimeFactory
) -> None:
    add(store, "resumable", start=1000)
    add(store, "behind-prior", start=2000)
    add(store, "unreferenced", start=3000)
    for occurrence_id in ("resumable", "behind-prior", "unreferenced"):
        store.finalize(occurrence_id, DeliveryState.SENT, "accepted")
    store.apply_marker_delta(
        MarkerDelta(
            markers_set=(
                hint(occurrence_id="resumable"),
                hint(occurrence_id="behind-prior", prior=True),
            ),
            guards_set=("registry-b",),
        ),
        at(0),
    )

    far = TOMBSTONE_RETENTION_SECONDS + 30 * DAY
    store.maintain(at(far))

    assert sorted(query(path, "SELECT id FROM plays")) == [
        ("behind-prior",),
        ("resumable",),
    ]
    assert add(store, "resumable", now=far) is EnqueueResult.DUPLICATE_OCCURRENCE

    store.apply_marker_delta(MarkerDelta(lineages_cleared=("registry-a",)), at(far))
    store.maintain(at(far))

    assert query(path, "SELECT COUNT(*) FROM plays") == [(0,)]


def test_tombstone_cap_spares_a_row_a_marker_references(
    store: OutboxStore, path: Path
) -> None:
    fill_plays(path, MAX_TOMBSTONES + 2, state="sent")
    store.apply_marker_delta(
        MarkerDelta(markers_set=(hint(occurrence_id="filler-0"),)), at(0)
    )

    store.maintain(FILLER_ERA)

    assert query(path, "SELECT COUNT(*) FROM plays") == [(MAX_TOMBSTONES,)]
    assert query(path, "SELECT id FROM plays WHERE id = 'filler-0'") == [("filler-0",)]
    assert query(path, "SELECT id FROM plays WHERE id = 'filler-1'") == []
    assert query(path, "SELECT id FROM plays WHERE id = 'filler-2'") == []


def coalesced(store: OutboxStore, alias: str = "alias") -> None:
    """Leave a finished canonical play whose only marker names its alias."""
    add(store, "canonical", start=1000)
    add(store, alias, start=1000)
    store.finalize("canonical", DeliveryState.SENT, "accepted")
    store.apply_marker_delta(
        MarkerDelta(
            markers_set=(hint(occurrence_id=alias),), guards_set=("registry-b",)
        ),
        at(0),
    )


def test_marker_on_an_alias_pins_it_and_its_play_past_the_age_limit(
    store: OutboxStore, path: Path, frozen: FrozenDateTimeFactory
) -> None:
    coalesced(store)
    far = TOMBSTONE_RETENTION_SECONDS + 30 * DAY

    store.maintain(at(far))

    assert query(path, "SELECT id FROM plays") == [("canonical",)]
    assert add(store, "alias", start=9000, now=far) is (
        EnqueueResult.DUPLICATE_OCCURRENCE
    )

    store.apply_marker_delta(MarkerDelta(lineages_cleared=("registry-a",)), at(far))
    store.maintain(at(far))

    assert query(path, "SELECT COUNT(*) FROM plays") == [(0,)]
    assert query(path, "SELECT COUNT(*) FROM aliases") == [(0,)]


def test_marker_in_the_prior_slot_pins_an_alias_too(
    store: OutboxStore, path: Path, frozen: FrozenDateTimeFactory
) -> None:
    add(store, "canonical", start=1000)
    add(store, "alias", start=1000)
    store.finalize("canonical", DeliveryState.SENT, "accepted")
    store.apply_marker_delta(
        MarkerDelta(markers_set=(hint(occurrence_id="alias", prior=True),)), at(0)
    )

    store.maintain(at(TOMBSTONE_RETENTION_SECONDS + DAY))

    assert query(path, "SELECT alias_id, play_id FROM aliases") == [
        ("alias", "canonical")
    ]


def test_marker_on_an_alias_pins_its_play_against_the_count_limit(
    store: OutboxStore, path: Path
) -> None:
    fill_plays(path, MAX_TOMBSTONES + 1, state="sent")
    write(
        path,
        "UPDATE plays SET updated_at = updated_at + 10 WHERE id LIKE 'filler-%'",
    )
    write(
        path,
        "INSERT INTO plays SELECT 'canonical', account_hash, X'00', started_at, "
        "duration, artist, title, album, state, reason, epoch, attempts, "
        "read_attempts, created_at, not_before, deadline, 0 FROM plays "
        "WHERE id = 'filler-0'",
    )
    write(path, "INSERT INTO aliases VALUES ('alias', 'canonical', 0)")
    store.apply_marker_delta(
        MarkerDelta(markers_set=(hint(occurrence_id="alias"),)), at(0)
    )

    store.maintain(FILLER_ERA)

    assert query(path, "SELECT COUNT(*) FROM plays") == [(MAX_TOMBSTONES,)]
    assert query(path, "SELECT id FROM plays WHERE id = 'canonical'") == [
        ("canonical",)
    ]
    assert query(path, "SELECT alias_id FROM aliases") == [("alias",)]


def test_alias_without_a_marker_is_pruned_with_its_play(
    store: OutboxStore, path: Path, frozen: FrozenDateTimeFactory
) -> None:
    add(store, "canonical", start=1000)
    add(store, "alias", start=1000)
    add(store, "other", start=5000, key=KEY_B)
    add(store, "other-alias", start=5000, key=KEY_B)
    store.finalize("canonical", DeliveryState.SENT, "accepted")

    store.maintain(at(TOMBSTONE_RETENTION_SECONDS))

    assert query(path, "SELECT id FROM plays") == [("other",)]
    assert query(path, "SELECT alias_id, play_id FROM aliases") == [
        ("other-alias", "other")
    ]


def test_protection_wins_when_markers_leave_the_count_limit_unreachable(
    store: OutboxStore, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(outbox_module, "MAX_TOMBSTONES", 1)
    for index in range(3):
        add(store, f"pinned-{index}", start=index * 1000)
        add(store, f"loose-{index}", start=10000 + index * 1000)
    add(store, "pinned-alias", start=2000)
    for index in range(3):
        store.finalize(f"pinned-{index}", DeliveryState.SENT, "accepted")
        store.finalize(f"loose-{index}", DeliveryState.SENT, "accepted")
    store.apply_marker_delta(
        MarkerDelta(
            markers_set=(
                hint("registry-a", "pinned-0"),
                hint("registry-b", "pinned-1"),
                hint("registry-c", "pinned-alias"),
            )
        ),
        at(0),
    )

    store.maintain(at(0))

    assert sorted(query(path, "SELECT id FROM plays")) == [
        ("pinned-0",),
        ("pinned-1",),
        ("pinned-2",),
    ]


def test_alias_table_stays_bounded_and_spares_a_referenced_alias(
    store: OutboxStore, path: Path
) -> None:
    add(store, "canonical", start=1000)
    fill_aliases(path, outbox_module.MAX_ALIASES, "canonical")
    store.apply_marker_delta(
        MarkerDelta(markers_set=(hint(occurrence_id="alias-0"),)), at(0)
    )

    assert add(store, "newest", start=1000) is EnqueueResult.OVERLAPPING

    assert query(path, "SELECT COUNT(*) FROM aliases") == [(outbox_module.MAX_ALIASES,)]
    assert query(path, "SELECT 1 FROM aliases WHERE alias_id = 'alias-0'") == [(1,)]
    assert query(path, "SELECT 1 FROM aliases WHERE alias_id = 'alias-1'") == []
    assert add(store, "newest", start=1000) is EnqueueResult.DUPLICATE_OCCURRENCE


def test_persisted_pause_over_a_day_keeps_the_marker_while_metadata_expires(
    store: OutboxStore, path: Path, frozen: FrozenDateTimeFactory
) -> None:
    add(store, now=100)
    marker = hint()
    store.apply_marker_delta(MarkerDelta(markers_set=(marker,)), at(100))
    guard = MarkerDelta(guards_set=("registry-b",))
    store.apply_marker_delta(guard, at(100))

    store.maintain(at(DAY + 200))

    assert state_of(path) == "pending"
    assert OutboxStore.open(path).resume_state() == ((marker,), {"registry-b"})

    long_after = 100 + ACTIVE_RETENTION_SECONDS + 200 * DAY
    store.maintain(at(long_after))

    assert state_of(path) == "expired"
    assert query(path, "SELECT artist FROM plays") == [("",)]
    reopened = OutboxStore.open(path)
    assert reopened.resume_state() == ((marker,), {"registry-b"})
    assert add(reopened, now=long_after) is EnqueueResult.DUPLICATE_OCCURRENCE


# Own receipts


def test_other_sent_timestamps_returns_proven_other_occurrences(
    store: OutboxStore,
) -> None:
    base = int(T0.timestamp())
    add(store, "earlier", start=1000)
    add(store, "current", start=1300)
    add(store, "later", start=1600)
    add(store, "unsent", start=1900)
    store.finalize("earlier", DeliveryState.SENT, "accepted")
    store.finalize("later", DeliveryState.SENT, "accepted")

    found = store.other_sent_timestamps("current", KEY_A, base, base + 5000)

    assert found == {base + 1000, base + 1600}


def test_other_sent_timestamps_never_hides_the_rows_own_receipt(
    store: OutboxStore,
) -> None:
    base = int(T0.timestamp())
    add(store, "current", start=1000)
    store.finalize("current", DeliveryState.SENT, "accepted")

    assert store.other_sent_timestamps("current", KEY_A, base, base + 5000) == set()


def test_restored_pending_copy_keeps_its_receipt_visible(
    store: OutboxStore, path: Path
) -> None:
    # A restored backup: the row is queued again, the provider already has it.
    base = int(T0.timestamp())
    add(store, "restored", start=1000)
    add(store, "twin", start=5000)
    store.finalize("twin", DeliveryState.SENT, "accepted")
    write(path, "UPDATE plays SET started_at = ? WHERE id = 'twin'", base + 1000)

    found = store.other_sent_timestamps("restored", KEY_A, base, base + 5000)

    assert found == set()


def test_adjacent_genuine_repeats_exclude_each_other_only(store: OutboxStore) -> None:
    base = int(T0.timestamp())
    add(store, "first", start=1000)
    add(store, "second", start=1200)
    store.finalize("first", DeliveryState.SENT, "accepted")

    window = (KEY_A, base + 900, base + 1300)

    assert store.other_sent_timestamps("second", *window) == {base + 1000}
    assert store.other_sent_timestamps("first", *window) == set()


def test_other_sent_timestamps_stays_on_the_account_track_and_window(
    store: OutboxStore,
) -> None:
    base = int(T0.timestamp())
    add(store, "current", start=1000)
    add(store, "foreign", start=1300, account=OTHER_ACCOUNT)
    add(store, "other-track", start=1400, key=KEY_B)
    add(store, "outside", start=9000)
    for occurrence_id in ("foreign", "other-track", "outside"):
        store.finalize(occurrence_id, DeliveryState.SENT, "accepted")

    assert store.other_sent_timestamps("current", KEY_A, base, base + 5000) == set()


def test_other_sent_timestamps_of_an_unknown_row_excludes_nothing(
    store: OutboxStore,
) -> None:
    base = int(T0.timestamp())
    add(store, "sent", start=1000)
    store.finalize("sent", DeliveryState.SENT, "accepted")

    assert store.other_sent_timestamps("nobody", KEY_A, base, base + 5000) == set()


# Account and queue resets


def test_purge_discards_other_accounts_and_keeps_their_hashes(
    store: OutboxStore, path: Path
) -> None:
    add(store, "mine", start=1000)
    add(store, "foreign", start=2000, account=OTHER_ACCOUNT)
    add(store, "foreign-held", start=3000, account=OTHER_ACCOUNT)
    store.hold("foreign-held", at(900), "history_unknown")

    assert store.purge_other_accounts(ACCOUNT) == 2

    assert dict(query(path, "SELECT id, state FROM plays")) == {
        "mine": "pending",
        "foreign": "discarded",
        "foreign-held": "discarded",
    }
    assert query(
        path, "SELECT COUNT(*) FROM plays WHERE account_hash = ?", OTHER_ACCOUNT
    ) == [(2,)]
    assert b"canary-artist" in path.read_bytes()
    assert add(store, "foreign", account=ACCOUNT) is (
        EnqueueResult.DUPLICATE_OCCURRENCE
    )


def test_purge_never_routes_an_unresolved_write_to_the_new_account(
    store: OutboxStore, path: Path
) -> None:
    add(store, account=OTHER_ACCOUNT)
    store.claim_in_flight("occ", DeliveryState.PENDING, EPOCH)
    store.mark_uncertain("occ", "response_lost")

    assert store.purge_other_accounts(ACCOUNT) == 1

    assert state_of(path) == "discarded"


def test_purge_leaves_an_attempt_in_flight_to_its_worker(
    store: OutboxStore, path: Path
) -> None:
    add(store, account=OTHER_ACCOUNT)
    store.claim_in_flight("occ", DeliveryState.PENDING, EPOCH)

    assert store.purge_other_accounts(ACCOUNT) == 0

    assert state_of(path) == "in_flight"


def test_clear_queue_erases_queued_metadata_and_counts_it(
    store: OutboxStore, path: Path
) -> None:
    add(store, "queued", start=1000)
    add(store, "held", start=2000)
    store.hold("held", at(900), "history_unknown")
    uncertain(store, "lost")
    add(store, "done", start=4000)
    store.finalize("done", DeliveryState.SENT, "accepted")

    assert store.clear_queue() == 3

    assert dict(query(path, "SELECT id, state FROM plays")) == {
        "queued": "discarded",
        "held": "discarded",
        "lost": "discarded",
        "done": "sent",
    }
    assert b"canary-artist" not in path.read_bytes()


def test_clear_queue_leaves_an_attempt_in_flight_to_its_worker(
    store: OutboxStore, path: Path
) -> None:
    add(store)
    store.claim_in_flight("occ", DeliveryState.PENDING, EPOCH)

    assert store.clear_queue() == 0

    assert state_of(path) == "in_flight"
    store.finalize("occ", DeliveryState.SENT, "accepted")
    assert state_of(path) == "sent"


# Meta


def test_meta_round_trip_on_the_named_keys(store: OutboxStore) -> None:
    assert store.meta_get("rate_limited_until") is None

    store.meta_set("rate_limited_until", "2031")
    store.meta_set("auth_required", "1")
    store.meta_set("rate_limited_until", "2032")
    store.meta_set("clock_high_water", "2033")

    assert store.meta_get("rate_limited_until") == "2032"
    assert store.meta_get("auth_required") == "1"
    assert store.meta_get("clock_high_water") == "2033"


@pytest.mark.parametrize(
    "key",
    ["epoch", "schema_version", "sent_total", "skipped_total", "ignored_total", "x"],
)
def test_meta_set_cannot_reach_a_guarded_key(store: OutboxStore, key: str) -> None:
    before = store.meta_get(key)

    with pytest.raises(OutboxError) as caught:
        store.meta_set(key, "99")

    assert caught.value.code == "unknown_meta_key"
    assert store.meta_get(key) == before


# Markers


def test_marker_delta_writes_each_slot(store: OutboxStore, path: Path) -> None:
    active, prior = hint(), hint(occurrence_id="older", prior=True, key=KEY_B)

    store.apply_marker_delta(
        MarkerDelta(markers_set=(active, prior), guards_set=("registry-a",)), at(5)
    )

    assert sorted(checkpoint_rows(path), key=lambda row: str(row[1])) == [
        (
            "registry-a",
            "active",
            "occ",
            KEY_A,
            (int(T0.timestamp()) * 1_000_000) + 250_000,
        ),
        ("registry-a", "guard", None, None, None),
        (
            "registry-a",
            "prior",
            "older",
            KEY_B,
            (int(T0.timestamp()) * 1_000_000) + 250_000,
        ),
    ]


def test_marker_table_holds_hashes_only(store: OutboxStore, path: Path) -> None:
    columns = {row[1] for row in query(path, "PRAGMA table_info(checkpoints)")}

    assert columns == {
        "lineage",
        "slot",
        "occurrence_id",
        "track_key",
        "started_at",
        "updated_at",
    }


@pytest.mark.parametrize(
    ("slot", "occurrence_id", "key", "start"),
    [
        ("active", None, b"k", 1),
        ("active", "x", None, 1),
        ("prior", "x", b"k", None),
        ("guard", "x", b"k", 1),
        ("other", "x", b"k", 1),
    ],
)
def test_schema_refuses_a_malformed_marker_slot(
    store: OutboxStore,
    path: Path,
    slot: str,
    occurrence_id: str | None,
    key: bytes | None,
    start: int | None,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        write(
            path,
            "INSERT INTO checkpoints VALUES ('l', ?, ?, ?, ?, 0)",
            slot,
            occurrence_id,
            key,
            start,
        )


def test_same_occurrence_may_sit_on_several_lineages(store: OutboxStore) -> None:
    hints = (hint("registry-a"), hint("registry-b"))

    store.apply_marker_delta(MarkerDelta(markers_set=hints), at(5))

    assert store.resume_state() == (hints, frozenset())


def test_guard_set_and_cleared_in_one_delta(store: OutboxStore) -> None:
    store.apply_marker_delta(MarkerDelta(guards_set=("old",)), at(1))

    store.apply_marker_delta(
        MarkerDelta(guards_set=("new",), guards_cleared=("old",)), at(2)
    )

    assert store.resume_state() == ((), {"new"})


def test_prior_clear_leaves_the_other_slots(store: OutboxStore) -> None:
    active, prior = hint(), hint(occurrence_id="older", prior=True)
    store.apply_marker_delta(
        MarkerDelta(markers_set=(active, prior), guards_set=("registry-a",)), at(1)
    )

    store.apply_marker_delta(MarkerDelta(priors_cleared=("registry-a",)), at(2))

    assert store.resume_state() == ((active,), {"registry-a"})


def test_lineage_clear_wipes_all_three_slots_of_that_lineage_only(
    store: OutboxStore,
) -> None:
    kept = hint("registry-b")
    store.apply_marker_delta(
        MarkerDelta(
            markers_set=(hint(), hint(occurrence_id="older", prior=True), kept),
            guards_set=("registry-a", "registry-b"),
        ),
        at(1),
    )

    store.apply_marker_delta(MarkerDelta(lineages_cleared=("registry-a",)), at(2))

    assert store.resume_state() == ((kept,), {"registry-b"})


def test_clears_of_absent_rows_are_no_ops(store: OutboxStore) -> None:
    active = hint()
    store.apply_marker_delta(MarkerDelta(markers_set=(active,)), at(1))

    store.apply_marker_delta(
        MarkerDelta(
            lineages_cleared=("gone",),
            priors_cleared=("registry-a", "gone"),
            guards_cleared=("registry-a", "gone"),
        ),
        at(2),
    )

    assert store.resume_state() == ((active,), frozenset())


def test_same_delta_twice_is_safe(store: OutboxStore) -> None:
    active, stale = hint(), hint("registry-b")
    store.apply_marker_delta(MarkerDelta(markers_set=(stale,)), at(1))
    delta = MarkerDelta(
        markers_set=(active,),
        lineages_cleared=("registry-b",),
        guards_set=("registry-c",),
    )

    store.apply_marker_delta(delta, at(2))
    once = store.resume_state()
    store.apply_marker_delta(delta, at(3))

    assert store.resume_state() == once == ((active,), {"registry-c"})


def test_guard_both_set_and_cleared_stays_set(store: OutboxStore) -> None:
    store.apply_marker_delta(
        MarkerDelta(guards_set=("both",), guards_cleared=("both",)), at(1)
    )

    assert store.resume_state() == ((), {"both"})


def test_set_replaces_the_slot_it_names(store: OutboxStore) -> None:
    store.apply_marker_delta(MarkerDelta(markers_set=(hint(),)), at(1))
    newer = hint(occurrence_id="newer", key=KEY_B, start=90)

    store.apply_marker_delta(MarkerDelta(markers_set=(newer,)), at(2))

    assert store.resume_state() == ((newer,), frozenset())


def test_marker_start_survives_to_the_microsecond(store: OutboxStore) -> None:
    exact = hint(start=1234.567891)

    store.apply_marker_delta(MarkerDelta(markers_set=(exact,)), at(1))

    assert store.resume_state()[0][0].started_at == exact.started_at
    assert store.resume_state()[0][0].started_at.tzinfo is UTC


def full_markers() -> MarkerDelta:
    return MarkerDelta(
        markers_set=tuple(
            hint(f"registry-{index}") for index in range(MAX_SELECTED_PLAYERS)
        )
    )


def test_a_lineage_past_the_bound_is_refused_and_drops_nothing(
    store: OutboxStore,
) -> None:
    store.apply_marker_delta(full_markers(), at(1))
    before = store.resume_state()

    with pytest.raises(OutboxError) as caught:
        store.apply_marker_delta(MarkerDelta(guards_set=("one-too-many",)), at(2))

    assert caught.value.code == "marker_limit"
    assert store.resume_state() == before


def test_three_slots_of_every_lineage_fit_the_bound(
    store: OutboxStore, path: Path
) -> None:
    delta = full_markers()
    priors = tuple(
        hint(marker.lineage, "older", prior=True) for marker in delta.markers_set
    )
    guards = tuple(marker.lineage for marker in delta.markers_set)

    store.apply_marker_delta(
        MarkerDelta(markers_set=delta.markers_set + priors, guards_set=guards), at(1)
    )

    assert query(path, "SELECT COUNT(*) FROM checkpoints") == [
        (3 * MAX_SELECTED_PLAYERS,)
    ]


def test_a_swap_at_the_bound_fits_in_one_delta(store: OutboxStore) -> None:
    store.apply_marker_delta(full_markers(), at(1))

    store.apply_marker_delta(
        MarkerDelta(markers_set=(hint("newcomer"),), lineages_cleared=("registry-0",)),
        at(2),
    )

    lineages = {marker.lineage for marker in store.resume_state()[0]}
    assert len(lineages) == MAX_SELECTED_PLAYERS
    assert "newcomer" in lineages
    assert "registry-0" not in lineages


def test_purge_lineages_drops_deselected_speakers_in_every_slot(
    store: OutboxStore,
) -> None:
    kept = hint("kept")
    store.apply_marker_delta(
        MarkerDelta(
            markers_set=(kept, hint("gone"), hint("gone", "older", prior=True)),
            guards_set=("gone", "guard-only"),
        ),
        at(1),
    )

    assert store.purge_lineages(frozenset({"kept"})) == 4

    assert store.resume_state() == ((kept,), frozenset())


def test_purge_with_nothing_kept_empties_the_table(store: OutboxStore) -> None:
    store.apply_marker_delta(MarkerDelta(markers_set=(hint(),)), at(1))

    assert store.purge_lineages(frozenset()) == 1
    assert store.resume_state() == ((), frozenset())


# A real marker sequence

SOLO_A = groups((A,))
JOINED_B = groups((B, A))
JOINED_D = groups((D, A))


def detours(streams: StreamSet) -> tuple[StreamSet, list[QualifiedPlay], float]:
    """X on A, then two brief joins: the prior rotates and A's guard is set."""
    emitted = list(streams.apply(SOLO_A, {A: sonos("X", 0, 100)}, 0.0))
    emitted += streams.apply(JOINED_B, {B: sonos("Z", 10, 50)}, 10.0)
    emitted += streams.apply(JOINED_D, {D: sonos("W", 20, 50)}, 20.0)
    return streams, emitted, 20.0


def test_real_delta_sequence_round_trips_through_the_file(
    store: OutboxStore, path: Path
) -> None:
    streams = StreamSet(iter(("x", "z", "w", "held", "y")).__next__)
    streams.set_lineages({A: "registry-a"})
    streams.seed_resume_hints((), ())
    seen: list[MarkerDelta] = []

    def checkpoint(moment: float) -> None:
        delta = streams.take_marker_changes()
        seen.append(delta)
        store.apply_marker_delta(delta, at(moment))
        hints, guards = OutboxStore.open(path).resume_state()
        assert set(hints) == set(streams._markers.values()) | set(
            streams._priors.values()
        )
        assert guards == streams._guards

    for moment, topology, samples in (
        (0, SOLO_A, {A: sonos("X", 0, 100)}),
        (10, JOINED_B, {B: sonos("Z", 10, 50)}),
        (20, JOINED_D, {D: sonos("W", 20, 50)}),
        (30, SOLO_A, {A: sonos("X", 30, 100)}),
        (45, SOLO_A, {A: sonos("Y", 45, 1)}),
    ):
        streams.apply(topology, samples, float(moment))
        checkpoint(float(moment))

    assert any(delta.markers_set for delta in seen)
    assert any(delta.guards_set for delta in seen)
    assert any(delta.guards_cleared for delta in seen)
    assert any(marker.prior for delta in seen for marker in delta.markers_set), (
        "the sequence must exercise the prior slot"
    )


def restart_and_play_x(
    hints: tuple[ResumeHint, ...], guards: frozenset[str]
) -> tuple[StreamSet, list[QualifiedPlay]]:
    """Boot from stored state and resume X at position 100 past the threshold."""
    restarted = _lineaged(
        StreamSet(iter(("after-1", "after-2")).__next__), hints, guards
    )
    emitted: list[QualifiedPlay] = []
    for tick in range(0, 300, 15):
        emitted += restarted.apply(
            SOLO_A, {A: sonos("X", 1000 + tick, 100 + tick)}, float(1000 + tick)
        )
    return restarted, emitted


def test_reloaded_guard_keeps_a_held_track_from_earning(
    store: OutboxStore, path: Path
) -> None:
    streams, _emitted, moment = detours(
        _lineaged(StreamSet(iter(("x", "z", "w", "h")).__next__))
    )
    store.apply_marker_delta(streams.take_marker_changes(), at(moment))

    hints, guards = OutboxStore.open(path).resume_state()
    assert guards == {"registry-a"}

    assert restart_and_play_x(hints, frozenset())[1], "without the guard X earns"
    restarted, emitted = restart_and_play_x(hints, guards)

    assert emitted == []
    store.apply_marker_delta(restarted.take_marker_changes(), at(1300))
    assert OutboxStore.open(path).resume_state()[1] == {"registry-a"}


def _lineaged(
    streams: StreamSet,
    hints: tuple[ResumeHint, ...] = (),
    guards: frozenset[str] = frozenset(),
) -> StreamSet:
    streams.set_lineages({A: "registry-a"})
    streams.seed_resume_hints(hints, guards)
    return streams


# Privacy


ACCOUNT_NAME = "canary-account-name"
SESSION_KEY = "canary-session-key"
SHARED_SECRET = "canary-shared-secret"


def test_db_contains_no_credentials(store: OutboxStore, path: Path) -> None:
    account = hashlib.blake2b(ACCOUNT_NAME.encode(), digest_size=16).digest()
    add(store, "queued", start=1000, account=account)
    add(store, "done", start=2000, account=account)
    store.finalize("done", DeliveryState.SENT, "accepted")
    store.apply_marker_delta(
        MarkerDelta(markers_set=(hint(),), guards_set=("registry-b",)), at(5)
    )
    store.meta_set("auth_required", "1")
    store.meta_set("rate_limited_until", "2000000000")
    for key in (SESSION_KEY, SHARED_SECRET, "session_key", "shared_secret"):
        with pytest.raises(OutboxError) as caught:
            store.meta_set(key, SESSION_KEY)
        assert caught.value.code == "unknown_meta_key"

    content = path.read_bytes()

    assert b"canary-title" in content, "the scan must be able to see queued text"
    assert account in content, "the scan must be able to see the hashed account"
    for canary in (ACCOUNT_NAME, SESSION_KEY, SHARED_SECRET):
        assert canary.encode() not in content
    schema = " ".join(
        str(row[0]) for row in query(path, "SELECT sql FROM sqlite_master")
    ).casefold()
    for term in ("session", "secret", "password", "token", "api_key"):
        assert term not in schema


def test_repr_and_errors_hide_metadata(store: OutboxStore, path: Path) -> None:
    add(store, start=1000)
    item = store.next_ready(at(300))
    assert item is not None
    write(
        path,
        "CREATE TRIGGER canary BEFORE UPDATE ON plays BEGIN "
        "SELECT RAISE(ABORT, 'canary-title in canary-artist'); END",
    )

    with pytest.raises(OutboxError) as caught:
        store.hold("occ", at(900), "history_unknown")

    shown = "|".join(
        (
            repr(item),
            repr(store.status(at(300))),
            repr(caught.value),
            str(caught.value),
        )
    )
    for canary in ("canary-artist", "canary-title", "canary-album"):
        assert canary not in shown
    assert str(T0.year) not in shown
    assert repr(KEY_A) not in shown
    assert repr(ACCOUNT) not in shown
    assert str(int(T0.timestamp())) not in shown
    assert str(path) not in shown + repr(store)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert caught.value.code == "storage_error"


def test_the_latest_active_start_ignores_finished_rows(store: OutboxStore) -> None:
    assert store.latest_active_start() is None

    add(store, "kept", start=100)
    add(store, "flying", start=300, key=KEY_B)
    store.claim_in_flight("flying", DeliveryState.PENDING, EPOCH)
    add(store, "done", start=900, key=b"c" * 16)
    store.finalize("done", DeliveryState.SENT, "accepted")

    assert store.latest_active_start() == int(at(300).timestamp())
