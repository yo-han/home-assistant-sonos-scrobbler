"""Scrobble, announce and read history for the linked account, with explicit outcomes.

Three calls, one module: `track.scrobble`, `track.updateNowPlaying` and
`user.getRecentTracks`. Every call is one attempt; retry policy belongs to the
caller, and a failed Now Playing request is never retried. Results carry fixed
reasons and never provider text, listening data, credentials or URLs, and the parsers
are pure functions over the decoded body. The JSON shapes are derived from the
documented XML and stay an assumption until verified live: anything unexpected is
UNCERTAIN for a write and unknown for a read, never success and never absence. Now
Playing has no history effect, so anything unexpected there is a failed notice.
"""

import asyncio
import json
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import aiohttp
from homeassistant.const import CONF_API_KEY

from . import lastfm
from .const import CONF_ACCOUNT_NAME, CONF_SESSION_KEY, CONF_SHARED_SECRET
from .lastfm import LastFmCredentials, sign_request
from .normalize import MAX_MEDIA_SECONDS, music_text

HISTORY_PAGE_LIMIT = 50
MAX_HISTORY_PAGES = 4
MAX_HISTORY_RESPONSE_BYTES = 256 * 1024
# "from" and "to" are documented as exclusive and the provider rounds an unknown
# amount, so the window grows by this many seconds on each side.
HISTORY_TOLERANCE_SECONDS = 31

_LOGGER = logging.getLogger(__name__)

_MAX_REMOTE_TEXT = 1024
_MAX_NUMBER_TEXT = 20
_SAFE_RETRY_CODES = frozenset({11, 16})
_RATE_LIMIT_CODE = 29
_AUTH_CODES = frozenset({4, 9, 10, 13, 26})
# A read carries only the API key, so only key-level codes say anything about the link.
_HISTORY_KEY_CODES = frozenset({10, 26})
_PERMANENT_CODES = frozenset({2, 3, 5, 6, 7})
# The documented filter codes; another nonzero code is a shape this parser cannot read.
_IGNORED_CODES = frozenset({1, 2, 3, 4, 5})
# `x`, `and` and `featuring` need whitespace on both sides, so "Mainx" and "Brand"
# stay whole; "(feat." and "(ft." are covered by the optional bracket.
_ARTIST_SEPARATOR = re.compile(
    r"\s*(?:[,;/&]|\s\(?(?:feat|ft)\.|\s(?:featuring|and|x)\s)\s*"
)
_TITLE_DASH = re.compile(r"\s[-\u2013\u2014]\s")
_OPENING = {")": "(", "]": "["}
_MAX_TIMESTAMP = 10**_MAX_NUMBER_TEXT - 1


class ScrobbleOutcome(StrEnum):
    """What one scrobble attempt proved."""

    ACCEPTED = "accepted"
    IGNORED = "ignored"
    SAFE_RETRY = "safe_retry"
    AUTH_REQUIRED = "auth_required"
    PERMANENT_REJECT = "permanent_reject"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class ScrobbleResult:
    """An outcome with a fixed reason; nothing from the provider."""

    outcome: ScrobbleOutcome
    reason: str
    rate_limited: bool = False


@dataclass(frozen=True, slots=True)
class ScrobblePayload:
    """One play to send, verbatim; the text stays out of every repr."""

    artist: str = field(repr=False)
    track: str = field(repr=False)
    timestamp: int = field(repr=False)
    album: str | None = field(repr=False, default=None)
    duration: int | None = field(repr=False, default=None)

    def __post_init__(self) -> None:
        """Refuse values the form cannot carry safely, without echoing them."""
        valid = (
            music_text(self.artist) is not None
            and music_text(self.track) is not None
            and (self.album is None or music_text(self.album) is not None)
            and _is_positive_int(self.timestamp)
            and self.timestamp <= _MAX_TIMESTAMP
            and (
                self.duration is None
                or (
                    _is_positive_int(self.duration)
                    and self.duration <= MAX_MEDIA_SECONDS
                )
            )
        )
        if not valid:
            raise ValueError("invalid scrobble payload")


@dataclass(frozen=True, slots=True)
class RemoteScrobble:
    """One dated play in the account's history."""

    artist: str = field(repr=False)
    title: str = field(repr=False)
    timestamp: int = field(repr=False)


@dataclass(frozen=True, slots=True)
class HistoryComplete:
    """Every dated play in the window; absence from it is meaningful."""

    scrobbles: tuple[RemoteScrobble, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class HistoryUnknown:
    """The window could not be read completely; absence proves nothing."""

    reason: str
    rate_limited: bool = False
    auth_required: bool = False


class NowPlayingOutcome(StrEnum):
    """What one Now Playing attempt proved; none of them justifies a retry."""

    ACCEPTED = "accepted"
    IGNORED = "ignored"
    AUTH_REQUIRED = "auth_required"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class NowPlayingResult:
    """An outcome with a fixed reason; nothing from the provider."""

    outcome: NowPlayingOutcome
    reason: str
    rate_limited: bool = False


@dataclass(frozen=True, slots=True)
class NowPlayingPayload:
    """The track to show, verbatim; the text stays out of every repr."""

    artist: str = field(repr=False)
    track: str = field(repr=False)
    album: str | None = field(repr=False, default=None)
    duration: int | None = field(repr=False, default=None)

    def __post_init__(self) -> None:
        """Refuse values the form cannot carry safely, without echoing them."""
        valid = (
            music_text(self.artist) is not None
            and music_text(self.track) is not None
            and (self.album is None or music_text(self.album) is not None)
            and (
                self.duration is None
                or (
                    _is_positive_int(self.duration)
                    and self.duration <= MAX_MEDIA_SECONDS
                )
            )
        )
        if not valid:
            raise ValueError("invalid now playing payload")


class _Transport(StrEnum):
    """A failed exchange, by whether the request can have reached the provider."""

    NOT_SENT = "not_sent"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class _Exchange:
    status: int
    body: bytes | None = field(repr=False)


def history_window(started_at: int, duration: int) -> tuple[int, int]:
    """Return the exclusive bounds around a play that started at started_at."""
    return (
        started_at - HISTORY_TOLERANCE_SECONDS,
        started_at + duration + HISTORY_TOLERANCE_SECONDS,
    )


def matches_track(artist: str, title: str, remote: RemoteScrobble) -> bool:
    """Return whether the remote play may be this one.

    Conservative on purpose: a false positive holds a duplicate back, a false
    negative writes one. It compares only; what is sent is never normalized.
    """
    return _same_artist(artist, remote.artist) and _same_title(title, remote.title)


class LastFmDeliveryClient:
    """Send one scrobble or notice, or read one window, for the linked account."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        credentials: LastFmCredentials,
        session_key: str,
        account_name: str,
    ) -> None:
        """Use the shared session; the endpoint is fixed in the lastfm module."""
        if not session_key or not account_name:
            raise ValueError("invalid delivery client")
        self._session = session
        self._credentials = credentials
        self._session_key = session_key
        self._account_name = account_name

    async def async_scrobble(self, payload: ScrobblePayload) -> ScrobbleResult:
        """Make one attempt to record the play."""
        params = {
            "method": "track.scrobble",
            "artist": payload.artist,
            "track": payload.track,
            "timestamp": str(payload.timestamp),
            "api_key": self._credentials.api_key,
            "sk": self._session_key,
        }
        if payload.album is not None:
            params["album"] = payload.album
        if payload.duration is not None:
            params["duration"] = str(payload.duration)
        exchange = await self._async_post(
            self._signed(params), lastfm.MAX_RESPONSE_BYTES, "scrobble"
        )
        result = _classify_scrobble(exchange, payload.timestamp)
        _LOGGER.debug(
            "Last.fm scrobble outcome: %s (%s)", result.outcome.value, result.reason
        )
        return result

    async def async_now_playing(self, payload: NowPlayingPayload) -> NowPlayingResult:
        """Make one attempt to show the track; the caller must not repeat it."""
        params = {
            "method": "track.updateNowPlaying",
            "artist": payload.artist,
            "track": payload.track,
            "api_key": self._credentials.api_key,
            "sk": self._session_key,
        }
        if payload.album is not None:
            params["album"] = payload.album
        if payload.duration is not None:
            params["duration"] = str(payload.duration)
        exchange = await self._async_post(
            self._signed(params), lastfm.MAX_RESPONSE_BYTES, "now playing"
        )
        result = _classify_now_playing(exchange)
        _LOGGER.debug(
            "Last.fm now playing outcome: %s (%s)", result.outcome.value, result.reason
        )
        return result

    async def async_history(
        self,
        start: int,
        end: int,
        before_page: Callable[[], Awaitable[None]] | None = None,
    ) -> HistoryComplete | HistoryUnknown:
        """Read every dated play in the exclusive window, or say it is unknown.

        before_page runs ahead of each request and may stop the read by raising.
        """
        scrobbles: list[RemoteScrobble] = []
        total_pages: int | None = None
        page = 1
        while total_pages is None or page <= total_pages:
            form = {
                "method": "user.getRecentTracks",
                "user": self._account_name,
                "api_key": self._credentials.api_key,
                "from": str(start),
                "to": str(end),
                "limit": str(HISTORY_PAGE_LIMIT),
                "page": str(page),
                "format": "json",
            }
            if before_page is not None:
                await before_page()
            exchange = await self._async_post(
                form, MAX_HISTORY_RESPONSE_BYTES, "history"
            )
            parsed = _parse_history_page(
                exchange, page, self._account_name, total_pages
            )
            if isinstance(parsed, HistoryUnknown):
                _LOGGER.debug("Last.fm history unknown: %s", parsed.reason)
                return parsed
            rows, total_pages = parsed
            # Every row was validated first; a provider that ignores from/to must
            # not widen the window. The bounds are kept inclusive: a kept edge row
            # can only hold a duplicate back, a dropped one can write it.
            scrobbles.extend(row for row in rows if start <= row.timestamp <= end)
            page += 1
        return HistoryComplete(tuple(scrobbles))

    def _signed(self, params: Mapping[str, str]) -> dict[str, str]:
        return {
            **params,
            "api_sig": sign_request(params, self._credentials.shared_secret),
            "format": "json",
        }

    async def _async_post(
        self, form: Mapping[str, str], limit: int, context: str
    ) -> _Exchange | _Transport:
        """Return the status and bounded body, or how the transport failed."""
        try:
            async with (
                asyncio.timeout(lastfm.REQUEST_DEADLINE_SECONDS),
                self._session.post(
                    lastfm.API_ROOT,
                    data=form,
                    headers={aiohttp.hdrs.ACCEPT_ENCODING: "identity"},
                    allow_redirects=False,
                    auto_decompress=False,
                ) as response,
            ):
                body = await lastfm.read_bounded(response, limit, context)
                return _Exchange(response.status, body)
        except aiohttp.ClientConnectorError:
            # The connection never opened, so nothing was written.
            return _Transport.NOT_SENT
        except aiohttp.ClientError, TimeoutError:
            # The request may have been written; only a read can tell.
            return _Transport.INTERRUPTED


def delivery_client(
    session: aiohttp.ClientSession, link: Mapping[str, str]
) -> LastFmDeliveryClient:
    """Return the client for a stored link."""
    return LastFmDeliveryClient(
        session,
        LastFmCredentials(link[CONF_API_KEY], link[CONF_SHARED_SECRET]),
        link[CONF_SESSION_KEY],
        link[CONF_ACCOUNT_NAME],
    )


def _classify_scrobble(
    exchange: _Exchange | _Transport, timestamp: int
) -> ScrobbleResult:
    """Map one observed exchange to exactly one outcome."""
    if exchange is _Transport.NOT_SENT:
        return ScrobbleResult(ScrobbleOutcome.SAFE_RETRY, "connect_failed")
    if isinstance(exchange, _Transport):
        return _uncertain("transport_interrupted")
    payload = _decode_object(exchange.body)
    if payload is not None and "error" in payload:
        # The API code wins over the HTTP status, as in the auth client.
        return _classify_error(payload["error"])
    if exchange.status == 429:
        return ScrobbleResult(ScrobbleOutcome.SAFE_RETRY, "http_429", rate_limited=True)
    if exchange.status != 200:
        return _uncertain("http_status")
    if payload is None:
        return _uncertain("body_not_object")
    return _classify_success(payload, timestamp)


def _classify_now_playing(exchange: _Exchange | _Transport) -> NowPlayingResult:
    """Map one observed exchange to one outcome; provider corrections are ignored."""
    if isinstance(exchange, _Transport):
        return _failed_notice("transport_failed")
    payload = _decode_object(exchange.body)
    if payload is not None and "error" in payload:
        code = _to_int(payload["error"], minimum=0)
        if code is None:
            return _failed_notice("api_error_shape")
        if code == _RATE_LIMIT_CODE:
            return _failed_notice("rate_limited", rate_limited=True)
        if code in _AUTH_CODES:
            return NowPlayingResult(NowPlayingOutcome.AUTH_REQUIRED, "auth_error")
        return _failed_notice("api_error")
    if exchange.status == 429:
        return _failed_notice("http_429", rate_limited=True)
    if exchange.status != 200:
        return _failed_notice("http_status")
    if payload is None:
        return _failed_notice("body_not_object")
    notice = payload.get("nowplaying")
    if not isinstance(notice, dict):
        return _failed_notice("nowplaying_shape")
    message = notice.get("ignoredMessage")
    code = (
        _to_int(message.get("code"), minimum=0) if isinstance(message, dict) else None
    )
    if code == 0:
        return NowPlayingResult(NowPlayingOutcome.ACCEPTED, "accepted")
    if code in _IGNORED_CODES:
        return NowPlayingResult(NowPlayingOutcome.IGNORED, "ignored")
    return _failed_notice("ignored_message_shape")


def _parse_history_page(
    exchange: _Exchange | _Transport,
    page: int,
    account_name: str,
    frozen_pages: int | None,
) -> tuple[list[RemoteScrobble], int] | HistoryUnknown:
    """Return the dated rows and the page count, or why the page cannot be trusted.

    The count of the first page is frozen: later pages must repeat it, because a
    count that moves means the window changed under the read. An empty history
    (count 0) is valid on the first page only.
    """
    if isinstance(exchange, _Transport):
        return HistoryUnknown("transport_failed")
    payload = _decode_object(exchange.body)
    code = None if payload is None else _to_int(payload.get("error"), minimum=0)
    if exchange.status == 429 or code == _RATE_LIMIT_CODE:
        return HistoryUnknown("rate_limited", rate_limited=True)
    if code in _HISTORY_KEY_CODES:
        return HistoryUnknown("auth_error", auth_required=True)
    if exchange.status != 200:
        return HistoryUnknown("http_status")
    if payload is None:
        return HistoryUnknown("body_not_object")
    if "error" in payload:
        return HistoryUnknown("api_error")
    recent = payload.get("recenttracks")
    if not isinstance(recent, dict):
        return HistoryUnknown("recenttracks_shape")
    attributes = recent.get("@attr")
    if not isinstance(attributes, dict):
        return HistoryUnknown("attributes_shape")
    user = attributes.get("user")
    if not isinstance(user, str) or user.casefold() != account_name.casefold():
        return HistoryUnknown("user_mismatch")
    total_pages = _to_int(attributes.get("totalPages"), minimum=0)
    if total_pages is None or _to_int(attributes.get("page"), minimum=1) != page:
        return HistoryUnknown("pagination_shape")
    if total_pages > MAX_HISTORY_PAGES:
        return HistoryUnknown("too_many_pages")
    if frozen_pages is not None and total_pages != frozen_pages:
        return HistoryUnknown("pagination_changed")
    if page > max(total_pages, 1):
        return HistoryUnknown("pagination_shape")
    rows = _history_rows(recent.get("track"))
    if rows is None:
        return HistoryUnknown("row_shape")
    if total_pages == 0 and rows:
        return HistoryUnknown("pagination_shape")
    return rows, max(total_pages, 1)


def _classify_error(raw_code: object) -> ScrobbleResult:
    code = _to_int(raw_code, minimum=0)
    if code is None:
        return _uncertain("api_error_shape")
    if code == _RATE_LIMIT_CODE:
        return ScrobbleResult(ScrobbleOutcome.SAFE_RETRY, "rate_limited", True)
    if code in _SAFE_RETRY_CODES:
        return ScrobbleResult(ScrobbleOutcome.SAFE_RETRY, "temporary_error")
    if code in _AUTH_CODES:
        return ScrobbleResult(ScrobbleOutcome.AUTH_REQUIRED, "auth_error")
    if code in _PERMANENT_CODES:
        return ScrobbleResult(ScrobbleOutcome.PERMANENT_REJECT, "rejected")
    return _uncertain("api_error")


def _classify_success(payload: dict[str, Any], timestamp: int) -> ScrobbleResult:
    scrobbles = payload.get("scrobbles")
    if not isinstance(scrobbles, dict):
        return _uncertain("scrobbles_shape")
    attributes = scrobbles.get("@attr")
    entries = scrobbles.get("scrobble")
    if isinstance(entries, dict):
        entries = [entries]
    if (
        not isinstance(attributes, dict)
        or not isinstance(entries, list)
        or len(entries) != 1
        or not isinstance(entries[0], dict)
    ):
        return _uncertain("scrobbles_shape")
    entry = entries[0]
    accepted = _to_int(attributes.get("accepted"), minimum=0)
    ignored = _to_int(attributes.get("ignored"), minimum=0)
    message = entry.get("ignoredMessage")
    code = (
        _to_int(message.get("code"), minimum=0) if isinstance(message, dict) else None
    )
    if code is None:
        return _uncertain("ignored_message_shape")
    if (accepted, ignored) == (1, 0):
        if code != 0:
            return _uncertain("inconsistent")
        if _to_int(entry.get("timestamp"), minimum=1) != timestamp:
            return _uncertain("timestamp_mismatch")
        return ScrobbleResult(ScrobbleOutcome.ACCEPTED, "accepted")
    if (accepted, ignored) == (0, 1) and code != 0:
        return ScrobbleResult(ScrobbleOutcome.IGNORED, "ignored")
    return _uncertain("inconsistent")


def _history_rows(raw: object) -> list[RemoteScrobble] | None:
    rows = [raw] if isinstance(raw, dict) else raw
    if not isinstance(rows, list) or len(rows) > HISTORY_PAGE_LIMIT:
        return None
    scrobbles: list[RemoteScrobble] = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        attributes = row.get("@attr")
        if isinstance(attributes, dict) and "nowplaying" in attributes:
            # A now-playing row is not history, even if it carries a date. Any
            # other marker value is a shape this parser does not know.
            if _is_truthy_marker(attributes["nowplaying"]):
                continue
            return None
        date = row.get("date")
        uts = _to_int(date.get("uts"), minimum=1) if isinstance(date, dict) else None
        artist = _remote_text(row.get("artist"))
        title = _remote_text(row.get("name"))
        if uts is None or artist is None or title is None:
            return None
        scrobbles.append(RemoteScrobble(artist, title, uts))
    return scrobbles


def _is_truthy_marker(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return isinstance(value, str) and value in {"true", "1"}


def _remote_text(value: object) -> str | None:
    if isinstance(value, dict):
        value = value.get("#text", value.get("name"))
    if not isinstance(value, str) or not value.strip():
        return None
    return value if len(value) <= _MAX_REMOTE_TEXT else None


def _decode_object(body: bytes | None) -> dict[str, Any] | None:
    if body is None:
        return None
    try:
        decoded = json.loads(body.decode(), parse_constant=_reject_constant)
    except ValueError, RecursionError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _reject_constant(constant: str) -> object:
    raise ValueError


def _to_int(value: object, *, minimum: int) -> int | None:
    """Accept an int or its decimal string, as the XML-to-JSON mapping may give."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not (value.isascii() and value.isdigit()) or len(value) > _MAX_NUMBER_TEXT:
            return None
        value = int(value)
    if not isinstance(value, int) or value < minimum:
        return None
    return value


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _uncertain(reason: str) -> ScrobbleResult:
    return ScrobbleResult(ScrobbleOutcome.UNCERTAIN, reason)


def _failed_notice(reason: str, *, rate_limited: bool = False) -> NowPlayingResult:
    return NowPlayingResult(NowPlayingOutcome.FAILED, reason, rate_limited)


def _fold(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _same_artist(ours: str, theirs: str) -> bool:
    first_ours, first_theirs = _first_artist(ours), _first_artist(theirs)
    return _fold(ours) == _fold(theirs) or (
        bool(first_ours) and first_ours == first_theirs
    )


def _first_artist(artist: str) -> str:
    return _ARTIST_SEPARATOR.split(_fold(artist), maxsplit=1)[0]


def _same_title(ours: str, theirs: str) -> bool:
    folded_ours, folded_theirs = _fold(ours), _fold(theirs)
    return bool(folded_ours) and (
        folded_ours == folded_theirs
        or _core_title(folded_ours) == _core_title(folded_theirs)
    )


def _core_title(folded: str) -> str:
    """Drop bracketed and dash suffixes, unless that would leave nothing."""
    core = folded
    while (stripped := _strip_dash(_strip_trailing_brackets(core))) != core:
        core = stripped
    return core or folded


def _strip_dash(text: str) -> str:
    return _TITLE_DASH.split(text, maxsplit=1)[0]


def _strip_trailing_brackets(text: str) -> str:
    """Cut one trailing bracket group, nested ones included; unbalanced stays."""
    text = text.rstrip()
    if not text or text[-1] not in _OPENING:
        return text
    depth = 0
    for index in range(len(text) - 1, -1, -1):
        if text[index] in _OPENING:
            depth += 1
        elif text[index] in _OPENING.values():
            depth -= 1
            if depth == 0:
                return text[:index].rstrip()
    return text
