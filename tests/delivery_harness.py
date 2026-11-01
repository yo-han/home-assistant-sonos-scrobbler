"""A pilot on a real HA event loop, real SQLite and a real socket server."""

import asyncio
import sqlite3
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState, ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.personal_sonos_scrobbler.const import DOMAIN
from custom_components.personal_sonos_scrobbler.controls import Feature, is_enabled
from custom_components.personal_sonos_scrobbler.delivery import (
    DeliveryService,
    delivery_service,
    ledger_path,
)
from custom_components.personal_sonos_scrobbler.playback import QualifiedPlay
from tests.helpers import Advance, choose, link_account, start_options
from tests.lastfm_server import ACCOUNT_NAME, LastFmServer, Reply, json_reply
from tests.outbox_support import META, REAL_CONNECT, T0, qualified, query

SCROBBLE = "track.scrobble"
RECENT = "user.getRecentTracks"
STARTED = int(T0.timestamp())


def row(
    stamp: int = STARTED,
    artist: str = META.artist,
    title: str = META.title,
) -> dict[str, Any]:
    return {
        "artist": {"#text": artist},
        "name": title,
        "date": {"uts": str(stamp), "#text": "synthetic"},
    }


def page(number: int, total: int, *rows: dict[str, Any]) -> Reply:
    """Return one page of a history that spans several."""
    attributes = {"user": ACCOUNT_NAME, "page": str(number), "totalPages": str(total)}
    return json_reply({"recenttracks": {"track": list(rows), "@attr": attributes}})


def history(*rows: dict[str, Any]) -> Reply:
    """Return one complete history page."""
    return page(1, 1, *rows)


def accepted(stamp: int = STARTED) -> Reply:
    return json_reply(
        {
            "scrobbles": {
                "@attr": {"accepted": 1, "ignored": 0},
                "scrobble": {
                    "timestamp": str(stamp),
                    "ignoredMessage": {"code": "0", "#text": ""},
                },
            }
        }
    )


@dataclass
class Hold:
    """One worker thread stopped at a statement until the test lets it go."""

    reached: threading.Event
    release: threading.Event

    def __call__(self) -> None:
        if not self.reached.is_set():
            self.reached.set()
            self.release.wait(10)


@contextmanager
def held_commit(patch: pytest.MonkeyPatch, statement: str) -> Any:
    """Hold the COMMIT of the first transaction that ran this statement."""
    hold = Hold(threading.Event(), threading.Event())

    class Holding(sqlite3.Connection):
        armed = False

        def execute(self, sql: str, *args: Any) -> Any:
            if sql.startswith(statement):
                self.armed = True
            if sql == "COMMIT" and self.armed:
                hold()
            return super().execute(sql, *args)

        def executemany(self, sql: str, *args: Any) -> Any:
            if sql.startswith(statement):
                self.armed = True
            return super().executemany(sql, *args)

    with patch.context() as local:
        local.setattr("sqlite3.connect", partial(REAL_CONNECT, factory=Holding))
        try:
            yield hold
        finally:
            hold.release.set()


@contextmanager
def held_before(patch: pytest.MonkeyPatch, statement: str) -> Any:
    """Hold the first execution of this statement, before it runs."""
    hold = Hold(threading.Event(), threading.Event())

    class Holding(sqlite3.Connection):
        def execute(self, sql: str, *args: Any) -> Any:
            if sql.startswith(statement):
                hold()
            return super().execute(sql, *args)

    with patch.context() as local:
        local.setattr("sqlite3.connect", partial(REAL_CONNECT, factory=Holding))
        try:
            yield hold
        finally:
            hold.release.set()


async def _real_pause() -> None:
    """Pause in real time for threads and sockets; freezegun leaves this alone."""
    threading.Event().wait(0.001)
    await asyncio.sleep(0)


@dataclass
class Pilot:
    """An entry and everything needed to act on it as Home Assistant would."""

    hass: HomeAssistant
    entry: MockConfigEntry
    server: LastFmServer
    advance: Advance
    # Most tests exercise both senders; a Now Playing test switches this off.
    scrobbling: bool = True

    @property
    def service(self) -> DeliveryService:
        service = delivery_service(self.hass, self.entry.entry_id)
        assert service is not None
        return service

    @property
    def path(self) -> Path:
        return ledger_path(self.hass, self.entry.entry_id)

    async def setup(self) -> None:
        assert await self.hass.config_entries.async_setup(self.entry.entry_id)
        await self.hass.async_block_till_done()

    async def unload(self) -> None:
        assert await self.hass.config_entries.async_unload(self.entry.entry_id)
        await self.hass.async_block_till_done()

    async def restart(self, data: dict[str, Any] | None = None) -> None:
        """Start again from the file, and optionally from older entry data."""
        await self.unload()
        if data is not None:
            self.hass.config_entries.async_update_entry(self.entry, data=data)
        await self.setup()

    async def option(self, step: str) -> ConfigFlowResult:
        """Choose a confirmation step and submit it, as the user would."""
        form = await choose(self.hass, await start_options(self.hass, self.entry), step)
        assert form["type"] is FlowResultType.FORM
        return await self.hass.config_entries.options.async_configure(
            form["flow_id"], {}
        )

    def switch_id(self, feature: Feature) -> str:
        """Find a switch by its stable unique id."""
        entity_id = er.async_get(self.hass).async_get_entity_id(
            "switch", DOMAIN, f"{self.entry.entry_id}_{feature}"
        )
        assert entity_id is not None, f"no {feature} switch"
        return entity_id

    async def press(self, feature: Feature, service: str) -> None:
        """Call the real switch service and wait for its completion."""
        await self.hass.services.async_call(
            "switch",
            service,
            {"entity_id": self.switch_id(feature)},
            blocking=True,
        )

    def is_on(self, feature: Feature) -> bool:
        return is_enabled(self.entry.data, feature)

    async def enable(self) -> None:
        await self.press(Feature.NOW_PLAYING, "turn_on")
        if self.scrobbling:
            await self.press(Feature.SCROBBLING, "turn_on")

    async def pause(self) -> None:
        """Switch off what enable switched on, in the same order."""
        await self.press(Feature.NOW_PLAYING, "turn_off")
        await self.press(Feature.SCROBBLING, "turn_off")

    async def linked(self) -> None:
        """Set up, link and turn delivery on through the real flow and switches."""
        await self.setup()
        await link_account(self.hass, self.entry)
        await self.enable()

    def accept(self, play: QualifiedPlay) -> None:
        self.service.async_accept(play)

    async def accept_now(self, occurrence_id: str = "occ", **options: Any) -> None:
        """Offer a play and wait until the producer chain took it."""
        self.accept(qualified(occurrence_id, **options))
        await self.hass.async_block_till_done()

    def rows(self, sql: str, *params: object) -> list[tuple[Any, ...]]:
        return query(self.path, sql, *params)

    def state(self, occurrence_id: str = "occ") -> str | None:
        found = self.rows("SELECT state FROM plays WHERE id = ?", occurrence_id)
        return found[0][0] if found else None

    def column(self, name: str, occurrence_id: str = "occ") -> Any:
        return self.rows(f"SELECT {name} FROM plays WHERE id = ?", occurrence_id)[0][0]

    def reads(self) -> int:
        return len(self.server.calls(RECENT))

    def posts(self) -> int:
        return len(self.server.calls(SCROBBLE))

    async def reach(
        self, condition: Callable[[], bool], *, tick: float = 0.0, within: int = 5000
    ) -> None:
        """Let real work happen until the condition holds; tick moves the clock.

        The event loop runs on the frozen clock here, so no asyncio timer can bound
        this: it counts real millisecond pauses instead.
        """
        for _ in range(within):
            if condition():
                return
            if tick:
                await self.advance(tick)
                # Enough for a loopback request: its deadline runs on this clock.
                await self.linger()
            await _real_pause()
        pytest.fail("the condition was not reached")

    async def linger(self) -> None:
        """Pass real time without waiting for tracked work, which may be held."""
        for _ in range(20):
            await _real_pause()

    async def quiet(self) -> None:
        """Give anything that should not happen a real chance to happen."""
        for _ in range(5):
            await self.hass.async_block_till_done()
            for _ in range(4):
                await _real_pause()

    async def past_grace(self, seconds: float = 800.0) -> None:
        """Move past the grace of a play from the first minutes."""
        await self.advance(seconds)
        await self.quiet()

    async def delivered(self, occurrence_id: str = "occ") -> None:
        await self.reach(lambda: self.state(occurrence_id) == "sent", tick=1)

    def loaded(self) -> bool:
        return self.entry.state is ConfigEntryState.LOADED
