"""The current-playback snapshot and the choice of what the account shows next."""

from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import replace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.personal_sonos_scrobbler.now_playing import (
    NowPlayingBoard,
    TrackDisplay,
)
from custom_components.personal_sonos_scrobbler.playback import (
    CurrentPlayback,
    TrackMetadata,
)
from custom_components.personal_sonos_scrobbler.runtime import ObservationRuntime
from custom_components.personal_sonos_scrobbler.streams import TOPOLOGY_SETTLE_SECONDS
from tests.helpers import Advance
from tests.outbox_support import T0, at

ENTRY_ID = "pilot-entry"
SPOTIFY = {
    "source": "Spotify Connect",
    "media_artist": "Artist",
    "media_album_name": "Album",
    "media_duration": 200,
}


def current(
    occurrence_id: str = "a", *, start: float = 0.0, settled: bool = True
) -> CurrentPlayback:
    return CurrentPlayback(
        occurrence_id=occurrence_id,
        metadata=TrackMetadata("Artist", occurrence_id),
        started_at=at(start),
        duration_seconds=200.0,
        settled=settled,
    )


def taken(board: NowPlayingBoard) -> str | None:
    notice = board.take()
    return None if notice is None else notice.occurrence_id


# The board


OLD = current("old")
NEW = current("new", start=5)


def shown(display: TrackDisplay, *items: CurrentPlayback) -> str | None:
    track = display.update(items)
    return None if track is None else track.occurrence_id


def test_the_display_keeps_the_newest_arrival_through_static_updates() -> None:
    display = TrackDisplay()

    assert shown(display, OLD) == "old"
    assert shown(display, OLD, NEW) == "new"
    assert shown(display, NEW, OLD) == "new"


def test_the_display_orders_by_arrival_not_by_start() -> None:
    display = TrackDisplay()
    shown(display, current("late", start=50))

    # A resume keeps its old start, and is still the newest thing to happen.
    assert (
        shown(display, current("late", start=50), current("resumed", start=1))
        == "resumed"
    )


def test_an_unproven_choice_yields_and_takes_the_display_back() -> None:
    display = TrackDisplay()
    shown(display, OLD)
    assert shown(display, OLD, NEW) == "new"

    assert shown(display, OLD, replace(NEW, settled=False)) == "old"
    assert shown(display, OLD, NEW) == "new"


def test_only_an_unproven_track_shows_nothing_until_it_settles() -> None:
    display = TrackDisplay()
    held = current("first", settled=False)

    assert shown(display, held) is None
    assert shown(display, replace(held, settled=True)) == "first"


def test_the_display_ends_with_the_playback() -> None:
    display = TrackDisplay()
    shown(display, OLD, NEW)

    assert shown(display, OLD) == "old"
    assert shown(display) is None
    assert display._arrivals == {}


def test_two_streams_of_one_occurrence_show_the_proven_one() -> None:
    display = TrackDisplay()
    settled = current("same")

    assert shown(display, settled, replace(settled, settled=False)) == "same"


def test_a_new_occurrence_is_ready_once() -> None:
    board = NowPlayingBoard()

    board.update((current(),))

    assert taken(board) == "a"
    assert taken(board) is None
    board.update((current(),))
    assert board.ready is None


def test_a_track_that_stops_and_returns_is_announced_again() -> None:
    board = NowPlayingBoard()
    board.update((current(),))
    board.take()

    board.update(())
    board.update((current(),))

    assert taken(board) == "a"


def test_stopping_voids_a_queued_notice() -> None:
    board = NowPlayingBoard()
    board.update((current(),))

    board.update(())

    assert board.ready is None
    assert board.take() is None


def test_a_new_track_replaces_a_queued_one_and_the_old_does_not_return() -> None:
    board = NowPlayingBoard()
    board.update((current("old"),))

    board.update((current("old"), current("new", start=-50.0)))
    assert taken(board) == "new"

    board.update((current("old"), current("new", start=-50.0)))
    assert board.ready is None


def test_the_newest_start_wins_among_tracks_found_together() -> None:
    board = NowPlayingBoard()

    board.update((current("older", start=1.0), current("newest", start=2.0)))

    assert taken(board) == "newest"
    board.update((current("older", start=1.0), current("newest", start=2.0)))
    assert board.ready is None


def test_equal_starts_are_decided_the_same_in_any_order() -> None:
    first, second = NowPlayingBoard(), NowPlayingBoard()

    first.update((current("x"), current("y")))
    second.update((current("y"), current("x")))

    assert taken(first) == taken(second) == "y"


def test_an_unsettled_track_waits_and_keeps_its_place() -> None:
    board = NowPlayingBoard()
    board.update((current(),))

    board.update((current(settled=False),))
    assert board.ready is None
    assert board.take() is None

    board.update((current(),))
    assert taken(board) == "a"


def test_an_unsettled_stretch_does_not_announce_a_shown_track_again() -> None:
    board = NowPlayingBoard()
    board.update((current(),))
    board.take()

    board.update((current(settled=False),))
    board.update((current(),))

    assert board.ready is None


def test_a_track_first_seen_unsettled_is_announced_once_it_settles() -> None:
    board = NowPlayingBoard()

    board.update((current(settled=False),))
    assert board.ready is None

    board.update((current(),))
    assert taken(board) == "a"


def test_two_streams_of_one_occurrence_are_one_notice() -> None:
    board = NowPlayingBoard()

    board.update((current(settled=False), current()))

    assert taken(board) == "a"
    assert board.ready is None


def test_reset_announces_what_is_playing_anew() -> None:
    board = NowPlayingBoard()
    board.update((current("older", start=1.0), current("newest", start=2.0)))
    board.take()

    board.reset()

    assert taken(board) == "newest"


def test_a_snapshot_keeps_track_text_out_of_its_repr() -> None:
    text = repr(current("occurrence"))

    assert "Artist" not in text
    assert "metadata" not in text
    assert str(T0.year) not in text


# The runtime


class Recorder:
    """A runtime with every snapshot it published."""

    def __init__(self) -> None:
        self.published: list[tuple[CurrentPlayback, ...]] = []
        self.runtime: ObservationRuntime

    @property
    def titles(self) -> list[str]:
        return [item.metadata.title for item in self.published[-1]]

    @property
    def settled(self) -> list[bool]:
        return [item.settled for item in self.published[-1]]


Start = Callable[..., Recorder]


@pytest.fixture
def register(entity_registry: er.EntityRegistry) -> Callable[[str], er.RegistryEntry]:
    def _register(name: str) -> er.RegistryEntry:
        return entity_registry.async_get_or_create(
            "media_player", "sonos", f"RINCON_{name}", suggested_object_id=name
        )

    return _register


@pytest.fixture
async def start(hass: HomeAssistant) -> AsyncIterator[Start]:
    built: list[Recorder] = []

    def _start(*selected: er.RegistryEntry) -> Recorder:
        recorder = Recorder()
        recorder.runtime = ObservationRuntime(
            hass,
            ENTRY_ID,
            [entry.id for entry in selected],
            on_current_playback=recorder.published.append,
        )
        recorder.runtime.async_start()
        built.append(recorder)
        return recorder

    yield _start
    for recorder in built:
        recorder.runtime.async_stop()


def publish(
    hass: HomeAssistant,
    player: er.RegistryEntry,
    title: str = "Title",
    state: str = "playing",
    *,
    members: Sequence[er.RegistryEntry] | None = None,
    position: float = 1.0,
    **attributes: Any,
) -> None:
    """Publish what a Sonos entity writes for a synthetic Spotify track."""
    hass.states.async_set(
        player.entity_id,
        state,
        {
            **SPOTIFY,
            "media_title": title,
            "media_position": position,
            "media_position_updated_at": dt_util.utcnow(),
            "group_members": [entry.entity_id for entry in members or (player,)],
            **attributes,
        },
    )


async def test_runtime_publishes_a_playing_track_before_any_credit(
    hass: HomeAssistant,
    start: Start,
    register: Callable[[str], er.RegistryEntry],
) -> None:
    kitchen = register("kitchen")
    recorder = start(kitchen)

    publish(hass, kitchen)
    await hass.async_block_till_done()

    item = recorder.published[-1][0]
    assert recorder.titles == ["Title"]
    assert recorder.settled == [True]
    assert item.metadata == TrackMetadata("Artist", "Title", "Album")
    assert item.duration_seconds == 200.0
    assert recorder.runtime.snapshot.candidate_count == 0


@pytest.mark.parametrize(
    "stopped",
    [
        {"state": "paused"},
        {"state": "idle"},
        {"source": "TV"},
        {"source": None, "media_content_id": "x-rincon-stream:RINCON_1"},
        {"source": "Other"},
    ],
    ids=["paused", "idle", "excluded", "line-in", "unknown-source"],
)
async def test_runtime_clears_the_snapshot_when_playback_stops_or_loses_its_source(
    hass: HomeAssistant,
    start: Start,
    register: Callable[[str], er.RegistryEntry],
    stopped: dict[str, Any],
) -> None:
    kitchen = register("kitchen")
    recorder = start(kitchen)
    publish(hass, kitchen)
    await hass.async_block_till_done()
    assert recorder.titles == ["Title"]

    publish(hass, kitchen, **stopped)
    await hass.async_block_till_done()

    assert recorder.titles == []


@pytest.mark.parametrize(
    "attributes",
    [
        {"source": "Other"},
        {"media_artist": None},
        {"media_title": " "},
        {"media_title": "bad\x00text"},
        {"source": "AirPlay"},
    ],
    ids=["unknown", "no-artist", "blank", "unsafe", "airplay"],
)
async def test_runtime_never_publishes_what_cannot_be_sent(
    hass: HomeAssistant,
    start: Start,
    register: Callable[[str], er.RegistryEntry],
    attributes: dict[str, Any],
) -> None:
    kitchen = register("kitchen")
    recorder = start(kitchen)

    publish(hass, kitchen, **attributes)
    await hass.async_block_till_done()

    assert not recorder.published or recorder.titles == []


async def test_snapshot_is_republished_only_when_it_changes(
    hass: HomeAssistant,
    start: Start,
    register: Callable[[str], er.RegistryEntry],
    advance: Advance,
) -> None:
    kitchen = register("kitchen")
    recorder = start(kitchen)
    publish(hass, kitchen)
    await hass.async_block_till_done()
    published = len(recorder.published)

    publish(hass, kitchen, position=2.0, volume_level=0.4)
    await advance(15)
    await advance(15)

    assert len(recorder.published) == published


async def test_an_event_of_another_stream_keeps_this_stream_in_the_snapshot(
    hass: HomeAssistant,
    start: Start,
    register: Callable[[str], er.RegistryEntry],
) -> None:
    kitchen, hall = register("kitchen"), register("hall")
    recorder = start(kitchen, hall)
    publish(hass, kitchen, "Kitchen")
    publish(hass, hall, "Hall")
    await hass.async_block_till_done()

    publish(hass, hall, "Hall", position=3.0, volume_level=0.2)
    await hass.async_block_till_done()

    assert sorted(recorder.titles) == ["Hall", "Kitchen"]


async def test_a_metadata_gap_holds_the_occurrence_without_announcing_it(
    hass: HomeAssistant,
    start: Start,
    register: Callable[[str], er.RegistryEntry],
) -> None:
    kitchen = register("kitchen")
    recorder = start(kitchen)
    publish(hass, kitchen)
    await hass.async_block_till_done()
    occurrence = recorder.published[-1][0].occurrence_id

    publish(hass, kitchen, source=None, media_title=None, media_artist=None)
    await hass.async_block_till_done()
    assert recorder.settled == [False]

    publish(hass, kitchen)
    await hass.async_block_till_done()
    assert recorder.settled == [True]
    assert recorder.published[-1][0].occurrence_id == occurrence


async def test_selected_follower_reads_its_unselected_coordinator(
    hass: HomeAssistant,
    start: Start,
    register: Callable[[str], er.RegistryEntry],
    advance: Advance,
) -> None:
    kitchen, hall = register("kitchen"), register("hall")
    recorder = start(hall)
    members = (kitchen, hall)

    publish(hass, kitchen, "Leader", members=members)
    publish(hass, hall, "Stale follower text", members=members)
    await hass.async_block_till_done()
    await advance(TOPOLOGY_SETTLE_SECONDS + 0.1)

    assert recorder.titles == ["Leader"]
    assert recorder.settled == [True]


async def test_a_group_is_one_entry_and_a_regroup_keeps_its_occurrence_held(
    hass: HomeAssistant,
    start: Start,
    register: Callable[[str], er.RegistryEntry],
    advance: Advance,
) -> None:
    kitchen, hall = register("kitchen"), register("hall")
    recorder = start(kitchen, hall)
    publish(hass, kitchen)
    publish(hass, hall, state="idle", media_title=None, media_artist=None)
    await hass.async_block_till_done()
    await advance(TOPOLOGY_SETTLE_SECONDS + 0.1)
    occurrence = recorder.published[-1][0].occurrence_id

    members = (kitchen, hall)
    publish(hass, kitchen, members=members)
    publish(hass, hall, members=members)
    await hass.async_block_till_done()
    assert recorder.settled == [False]

    await advance(TOPOLOGY_SETTLE_SECONDS + 0.1)
    assert recorder.titles == ["Title"]
    assert recorder.settled == [True]
    assert recorder.published[-1][0].occurrence_id == occurrence


async def test_a_split_keeps_the_occurrence_through_a_successor_stream(
    hass: HomeAssistant,
    start: Start,
    register: Callable[[str], er.RegistryEntry],
    advance: Advance,
) -> None:
    kitchen, hall = register("kitchen"), register("hall")
    recorder = start(kitchen, hall)
    members = (kitchen, hall)
    publish(hass, kitchen, members=members)
    publish(hass, hall, members=members)
    await hass.async_block_till_done()
    await advance(TOPOLOGY_SETTLE_SECONDS + 0.1)
    occurrence = recorder.published[-1][0].occurrence_id

    publish(hass, hall)
    publish(hass, kitchen)
    await hass.async_block_till_done()
    await advance(TOPOLOGY_SETTLE_SECONDS + 0.1)

    assert {item.occurrence_id for item in recorder.published[-1]} == {occurrence}
    for snapshot in recorder.published:
        assert snapshot


async def test_stopping_the_runtime_publishes_an_empty_snapshot(
    hass: HomeAssistant,
    start: Start,
    register: Callable[[str], er.RegistryEntry],
) -> None:
    kitchen = register("kitchen")
    recorder = start(kitchen)
    publish(hass, kitchen)
    await hass.async_block_till_done()

    recorder.runtime.async_stop()

    assert recorder.published[-1] == ()
