"""The delivery client against real sockets, through Home Assistant's shared session."""

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.personal_sonos_scrobbler import lastfm
from custom_components.personal_sonos_scrobbler import lastfm_delivery as delivery
from custom_components.personal_sonos_scrobbler.lastfm import LastFmCredentials
from custom_components.personal_sonos_scrobbler.lastfm_delivery import (
    HistoryComplete,
    HistoryUnknown,
    LastFmDeliveryClient,
    RemoteScrobble,
    ScrobbleOutcome,
    ScrobblePayload,
    ScrobbleResult,
    history_window,
    matches_track,
)
from tests.lastfm_server import (
    ACCOUNT_NAME,
    API_KEY,
    SESSION_KEY,
    SHARED_SECRET,
    LastFmServer,
    Reply,
    json_reply,
)

SCROBBLE = "track.scrobble"
RECENT = "user.getRecentTracks"
CANARY = "canary-provider-message"
STARTED = 1_700_000_000
ARTIST = "Synthetic Artist ☃"
TITLE = "Synthetic Tïtle"
ALBUM = "Synthetic Album"
WIDE = (0, STARTED + 1_000)
type Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]
type RawServer = Callable[[Handler], Awaitable[None]]


def payload(**overrides: Any) -> ScrobblePayload:
    values: dict[str, Any] = {
        "artist": ARTIST,
        "track": TITLE,
        "timestamp": STARTED,
        "album": ALBUM,
        "duration": 215,
    }
    return ScrobblePayload(**{**values, **overrides})


def scrobble_body(
    *,
    accepted: object = 1,
    ignored: object = 0,
    timestamp: object = STARTED,
    code: object = 0,
) -> dict[str, Any]:
    """Return the documented XML-to-JSON shape of one scrobble result."""
    return {
        "scrobbles": {
            "@attr": {"accepted": accepted, "ignored": ignored},
            "scrobble": {
                "track": {"corrected": "0", "#text": TITLE},
                "artist": {"corrected": "0", "#text": ARTIST},
                "timestamp": timestamp,
                "ignoredMessage": {"code": code, "#text": ""},
            },
        }
    }


def accepted_reply(**overrides: Any) -> Reply:
    return json_reply(scrobble_body(**overrides))


def row(
    artist: str = ARTIST, title: str = TITLE, uts: object = STARTED, **extra: Any
) -> dict[str, Any]:
    return {
        "artist": {"#text": artist, "mbid": ""},
        "name": title,
        "album": {"#text": ALBUM, "mbid": ""},
        "date": {"uts": str(uts), "#text": "synthetic"},
        **extra,
    }


def recent_body(
    tracks: object,
    *,
    page: object = 1,
    total_pages: object = 1,
    user: object = ACCOUNT_NAME,
) -> dict[str, Any]:
    attr: dict[str, object] = {"user": user, "page": page, "perPage": "50"}
    if total_pages is not None:
        attr["totalPages"] = total_pages
    return {"recenttracks": {"track": tracks, "@attr": attr}}


def recent_reply(tracks: object, **kwargs: Any) -> Reply:
    return json_reply(recent_body(tracks, **kwargs))


@pytest.fixture
async def client(
    hass: HomeAssistant, lastfm_server: LastFmServer
) -> LastFmDeliveryClient:
    """Return the real delivery client on the real shared session."""
    return LastFmDeliveryClient(
        async_get_clientsession(hass),
        LastFmCredentials(api_key=API_KEY, shared_secret=SHARED_SECRET),
        SESSION_KEY,
        ACCOUNT_NAME,
    )


@pytest.fixture
async def raw_server(
    lastfm_server: LastFmServer, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[RawServer]:
    """Serve bytes aiohttp's server would refuse to produce."""
    servers: list[asyncio.Server] = []

    async def _start(handler: Handler) -> None:
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        servers.append(server)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(lastfm, "API_ROOT", f"http://127.0.0.1:{port}/")
        monkeypatch.setattr(lastfm, "REQUEST_DEADLINE_SECONDS", 0.2)

    yield _start
    for server in servers:
        server.close()
        await server.wait_closed()


def _expected_signature(form: dict[str, str]) -> str:
    """Recompute the documented signature without the production helper."""
    pairs = "".join(
        f"{name}{form[name]}"
        for name in sorted(form)
        if name not in {"api_sig", "format"}
    )
    return hashlib.md5(
        f"{pairs}{SHARED_SECRET}".encode(), usedforsecurity=False
    ).hexdigest()


async def test_signed_form_excludes_batch_and_keeps_utf8_signature(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer
) -> None:
    lastfm_server.queue(SCROBBLE, accepted_reply())

    result = await client.async_scrobble(payload())

    form = dict(lastfm_server.requests[0].form)
    assert result.outcome is ScrobbleOutcome.ACCEPTED
    assert set(form) == {
        "method",
        "artist",
        "track",
        "timestamp",
        "album",
        "duration",
        "api_key",
        "sk",
        "api_sig",
        "format",
    }
    assert not [name for name in form if "[" in name]
    assert form["method"] == SCROBBLE
    assert (form["artist"], form["track"], form["album"]) == (ARTIST, TITLE, ALBUM)
    assert (form["timestamp"], form["duration"]) == (str(STARTED), "215")
    assert (form["api_key"], form["sk"], form["format"]) == (
        API_KEY,
        SESSION_KEY,
        "json",
    )
    assert form["api_sig"] == _expected_signature(form)
    assert lastfm_server.requests[0].http_method == "POST"


async def test_optional_fields_are_omitted_and_signed_without_them(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer
) -> None:
    lastfm_server.queue(SCROBBLE, accepted_reply())

    await client.async_scrobble(payload(album=None, duration=None))

    form = dict(lastfm_server.requests[0].form)
    assert "album" not in form
    assert "duration" not in form
    assert form["api_sig"] == _expected_signature(form)


async def test_no_credentials_or_metadata_in_url(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer
) -> None:
    lastfm_server.queue(SCROBBLE, accepted_reply())
    lastfm_server.queue(RECENT, recent_reply([]))

    await client.async_scrobble(payload())
    await client.async_history(STARTED - 31, STARTED + 246)

    assert len(lastfm_server.requests) == 2
    for request in lastfm_server.requests:
        assert (request.path, request.query) == ("/", "")
        assert request.headers["Accept-Encoding"] == "identity"


@pytest.mark.parametrize(
    ("reply", "outcome", "rate_limited"),
    [
        (accepted_reply(), ScrobbleOutcome.ACCEPTED, False),
        (
            accepted_reply(accepted=0, ignored=1, code=1),
            ScrobbleOutcome.IGNORED,
            False,
        ),
        (
            accepted_reply(accepted=0, ignored=1, code=5),
            ScrobbleOutcome.IGNORED,
            False,
        ),
        (
            accepted_reply(accepted=0, ignored=1, code=777),
            ScrobbleOutcome.IGNORED,
            False,
        ),
        (json_reply({"error": 11}), ScrobbleOutcome.SAFE_RETRY, False),
        (json_reply({"error": 16}), ScrobbleOutcome.SAFE_RETRY, False),
        (json_reply({"error": 29}), ScrobbleOutcome.SAFE_RETRY, True),
        (Reply(status=429), ScrobbleOutcome.SAFE_RETRY, True),
        (json_reply({"error": 11}, status=500), ScrobbleOutcome.SAFE_RETRY, False),
        *[
            (json_reply({"error": code, "message": CANARY}), outcome, False)
            for code, outcome in [
                (4, ScrobbleOutcome.AUTH_REQUIRED),
                (9, ScrobbleOutcome.AUTH_REQUIRED),
                (10, ScrobbleOutcome.AUTH_REQUIRED),
                (13, ScrobbleOutcome.AUTH_REQUIRED),
                (26, ScrobbleOutcome.AUTH_REQUIRED),
                (2, ScrobbleOutcome.PERMANENT_REJECT),
                (3, ScrobbleOutcome.PERMANENT_REJECT),
                (5, ScrobbleOutcome.PERMANENT_REJECT),
                (6, ScrobbleOutcome.PERMANENT_REJECT),
                (7, ScrobbleOutcome.PERMANENT_REJECT),
                (8, ScrobbleOutcome.UNCERTAIN),
                (999, ScrobbleOutcome.UNCERTAIN),
            ]
        ],
        (json_reply({"error": 9}, status=403), ScrobbleOutcome.AUTH_REQUIRED, False),
        (Reply(status=500), ScrobbleOutcome.UNCERTAIN, False),
        (Reply(status=503, body=b"<html>"), ScrobbleOutcome.UNCERTAIN, False),
        (Reply(status=404), ScrobbleOutcome.UNCERTAIN, False),
        (Reply(status=400, body=b"{}"), ScrobbleOutcome.UNCERTAIN, False),
        (
            Reply(status=302, headers={"Location": "/x"}),
            ScrobbleOutcome.UNCERTAIN,
            False,
        ),
        (
            Reply(body=b"{}", headers={"Content-Encoding": "gzip"}),
            ScrobbleOutcome.UNCERTAIN,
            False,
        ),
        (Reply(body=b" " * (65 * 1024)), ScrobbleOutcome.UNCERTAIN, False),
        (Reply(body=b"not json"), ScrobbleOutcome.UNCERTAIN, False),
        (Reply(body=b"[1]"), ScrobbleOutcome.UNCERTAIN, False),
        (Reply(body=b'{"scrobbles": NaN}'), ScrobbleOutcome.UNCERTAIN, False),
        (json_reply({}), ScrobbleOutcome.UNCERTAIN, False),
        (json_reply({"error": True}), ScrobbleOutcome.UNCERTAIN, False),
        (
            accepted_reply(accepted=1, ignored=1),
            ScrobbleOutcome.UNCERTAIN,
            False,
        ),
        (
            accepted_reply(accepted=0, ignored=0),
            ScrobbleOutcome.UNCERTAIN,
            False,
        ),
        (
            accepted_reply(accepted=2, ignored=0),
            ScrobbleOutcome.UNCERTAIN,
            False,
        ),
        (
            accepted_reply(accepted=1, ignored=0, code=3),
            ScrobbleOutcome.UNCERTAIN,
            False,
        ),
        (
            accepted_reply(accepted=0, ignored=1, code=0),
            ScrobbleOutcome.UNCERTAIN,
            False,
        ),
        (accepted_reply(accepted="x"), ScrobbleOutcome.UNCERTAIN, False),
        (accepted_reply(accepted=True), ScrobbleOutcome.UNCERTAIN, False),
        (accepted_reply(code="abc"), ScrobbleOutcome.UNCERTAIN, False),
        (
            json_reply({"scrobbles": {"@attr": {"accepted": 1, "ignored": 0}}}),
            ScrobbleOutcome.UNCERTAIN,
            False,
        ),
        (
            json_reply({"scrobbles": {"scrobble": {}}}),
            ScrobbleOutcome.UNCERTAIN,
            False,
        ),
        (
            json_reply(
                {
                    "scrobbles": {
                        "@attr": {"accepted": 2, "ignored": 0},
                        "scrobble": [
                            scrobble_body()["scrobbles"]["scrobble"],
                            scrobble_body()["scrobbles"]["scrobble"],
                        ],
                    }
                }
            ),
            ScrobbleOutcome.UNCERTAIN,
            False,
        ),
    ],
)
async def test_every_observation_has_one_outcome(
    client: LastFmDeliveryClient,
    lastfm_server: LastFmServer,
    reply: Reply,
    outcome: ScrobbleOutcome,
    rate_limited: bool,
) -> None:
    lastfm_server.queue(SCROBBLE, reply)

    result = await client.async_scrobble(payload())

    assert result.outcome is outcome
    assert result.rate_limited is rate_limited
    assert result.reason
    assert len(lastfm_server.requests) == 1


async def test_a_single_element_list_is_the_documented_singleton(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer
) -> None:
    body = scrobble_body()
    body["scrobbles"]["scrobble"] = [body["scrobbles"]["scrobble"]]
    lastfm_server.queue(SCROBBLE, json_reply(body))

    assert (await client.async_scrobble(payload())).outcome is ScrobbleOutcome.ACCEPTED


@pytest.mark.parametrize(
    "reply",
    [
        accepted_reply(accepted="1", ignored="0", timestamp=str(STARTED), code="0"),
        accepted_reply(accepted=1, ignored=0, timestamp=STARTED, code=0),
        accepted_reply(accepted="1", ignored=0, timestamp=STARTED, code="0"),
    ],
)
async def test_numeric_strings_and_ints_both_accepted(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer, reply: Reply
) -> None:
    lastfm_server.queue(SCROBBLE, reply)

    assert (await client.async_scrobble(payload())).outcome is ScrobbleOutcome.ACCEPTED


async def test_http_200_error_is_not_success(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer
) -> None:
    lastfm_server.queue(SCROBBLE, json_reply({"error": 9, "message": CANARY}))
    lastfm_server.queue(SCROBBLE, json_reply({"status": "ok"}))

    first = await client.async_scrobble(payload())
    second = await client.async_scrobble(payload())

    assert first.outcome is ScrobbleOutcome.AUTH_REQUIRED
    assert second.outcome is ScrobbleOutcome.UNCERTAIN


@pytest.mark.parametrize("timestamp", [STARTED + 1, STARTED - 1, "x", None, 0])
async def test_timestamp_mismatch_is_uncertain(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer, timestamp: object
) -> None:
    lastfm_server.queue(SCROBBLE, accepted_reply(timestamp=timestamp))

    result = await client.async_scrobble(payload())

    assert result.outcome is ScrobbleOutcome.UNCERTAIN


async def test_disconnect_after_body_read_is_uncertain(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer
) -> None:
    lastfm_server.queue(SCROBBLE, Reply(drop=True))

    result = await client.async_scrobble(payload())

    assert result.outcome is ScrobbleOutcome.UNCERTAIN
    assert len(lastfm_server.requests) == 1
    # The write really happened: the server holds the scrobble the client never saw.
    assert [item["artist"] for item in lastfm_server.history] == [ARTIST]


async def test_connection_refused_is_safe_retry(
    client: LastFmDeliveryClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = await asyncio.start_server(lambda *_: None, "127.0.0.1", 0)
    port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()
    monkeypatch.setattr(lastfm, "API_ROOT", f"http://127.0.0.1:{port}/")

    result = await client.async_scrobble(payload())

    assert result.outcome is ScrobbleOutcome.SAFE_RETRY
    assert result.rate_limited is False


async def _read_request(reader: asyncio.StreamReader) -> None:
    await reader.readuntil(b"\r\n\r\n")


async def test_timeout_is_uncertain(
    client: LastFmDeliveryClient, raw_server: RawServer
) -> None:
    async def stall(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_request(reader)
        await reader.read()
        writer.close()

    await raw_server(stall)

    assert (await client.async_scrobble(payload())).outcome is ScrobbleOutcome.UNCERTAIN


@pytest.mark.parametrize("behaviour", ["reset", "garbage", "truncated_body"])
async def test_other_transport_failures_are_uncertain(
    client: LastFmDeliveryClient, raw_server: RawServer, behaviour: str
) -> None:
    async def misbehave(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await _read_request(reader)
        if behaviour == "reset":
            writer.transport.abort()
            return
        if behaviour == "garbage":
            writer.write(b"\x00\x01 not http\r\n\r\n")
        else:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\n{")
        await writer.drain()
        writer.close()

    await raw_server(misbehave)

    assert (await client.async_scrobble(payload())).outcome is ScrobbleOutcome.UNCERTAIN


async def test_delayed_reply_past_the_deadline_is_uncertain(
    client: LastFmDeliveryClient,
    lastfm_server: LastFmServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lastfm, "REQUEST_DEADLINE_SECONDS", 0.1)
    lastfm_server.queue(SCROBBLE, Reply(delay=1.0, store=True))

    result = await client.async_scrobble(payload())

    assert result.outcome is ScrobbleOutcome.UNCERTAIN
    assert len(lastfm_server.history) == 1


async def test_cancellation_propagates(
    client: LastFmDeliveryClient, raw_server: RawServer
) -> None:
    connected = asyncio.Event()

    async def stall(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_request(reader)
        connected.set()
        await reader.read()
        writer.close()

    await raw_server(stall)
    task = asyncio.create_task(client.async_scrobble(payload()))
    async with asyncio.timeout(2):
        await connected.wait()

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_results_logs_and_reprs_carry_no_listening_data(
    client: LastFmDeliveryClient,
    lastfm_server: LastFmServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    lastfm_server.queue(
        SCROBBLE,
        json_reply({"error": 8, "message": CANARY}),
        Reply(status=302, headers={"Location": CANARY}),
        Reply(body=CANARY.encode()),
        accepted_reply(accepted=0, ignored=1, code=777),
    )
    lastfm_server.queue(RECENT, json_reply({"error": 17, "message": CANARY}))
    results: list[object] = [await client.async_scrobble(payload()) for _ in range(4)]
    results.append(await client.async_history(STARTED - 31, STARTED + 246))

    text = " ".join(
        [repr(payload()), repr(client), *map(repr, results)]
        + [record.getMessage() for record in caplog.records]
    )
    for secret in (
        ARTIST,
        TITLE,
        ALBUM,
        CANARY,
        API_KEY,
        SHARED_SECRET,
        SESSION_KEY,
        ACCOUNT_NAME,
        str(STARTED),
    ):
        assert secret not in text


@pytest.mark.parametrize(
    "overrides",
    [
        {"artist": ""},
        {"track": "  "},
        {"artist": "a\x00b"},
        {"track": "x" * 513},
        {"album": "bad\x07"},
        {"timestamp": 0},
        {"timestamp": True},
        {"timestamp": 10**20},
        {"duration": 0},
        {"duration": 86_401},
    ],
)
def test_payload_refuses_unsafe_or_invalid_values(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="invalid scrobble payload") as caught:
        payload(**overrides)

    assert ARTIST not in str(caught.value)


def test_an_exchange_repr_hides_the_body() -> None:
    exchange = delivery._Exchange(200, CANARY.encode())

    assert CANARY not in repr(exchange)


def test_payload_text_is_kept_verbatim() -> None:
    value = payload(artist="  Padded  ", track="Title (Live) - 2011")

    assert (value.artist, value.track) == ("  Padded  ", "Title (Live) - 2011")


# --- history ---------------------------------------------------------------------


async def test_history_form_is_unsigned_and_uses_the_linked_account(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer
) -> None:
    lastfm_server.queue(RECENT, recent_reply([]))

    result = await client.async_history(STARTED - 31, STARTED + 246)

    assert result == HistoryComplete(())
    assert dict(lastfm_server.requests[0].form) == {
        "method": RECENT,
        "user": ACCOUNT_NAME,
        "api_key": API_KEY,
        "from": str(STARTED - 31),
        "to": str(STARTED + 246),
        "limit": "50",
        "page": "1",
        "format": "json",
    }


def test_history_window_widens_both_edges_by_the_tolerance() -> None:
    assert history_window(1000, 200) == (969, 1231)
    assert history_window(1000, 0) == (969, 1031)


async def test_history_singleton_and_list(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer
) -> None:
    lastfm_server.queue(
        RECENT,
        recent_reply(row()),
        recent_reply([row(), row(title="Other", uts=STARTED + 300)]),
    )

    single = await client.async_history(*WIDE)
    several = await client.async_history(*WIDE)

    expected = RemoteScrobble(ARTIST, TITLE, STARTED)
    assert single == HistoryComplete((expected,))
    assert several == HistoryComplete(
        (expected, RemoteScrobble(ARTIST, "Other", STARTED + 300))
    )


async def test_numeric_strings_and_ints_in_history_both_accepted(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer
) -> None:
    entry = row()
    entry["date"] = {"uts": STARTED}
    lastfm_server.queue(
        RECENT, recent_reply([entry], page="1", total_pages="1"), recent_reply([entry])
    )

    first = await client.async_history(*WIDE)
    second = await client.async_history(*WIDE)

    assert first == second == HistoryComplete((RemoteScrobble(ARTIST, TITLE, STARTED),))


async def test_nowplaying_with_date_is_not_proof(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer
) -> None:
    lastfm_server.queue(
        RECENT,
        recent_reply(
            [
                row(**{"@attr": {"nowplaying": "true"}}),
                row(title="Dated", uts=STARTED + 5),
                {
                    "artist": {"#text": "A"},
                    "name": "N",
                    "@attr": {"nowplaying": "true"},
                },
            ]
        ),
    )

    result = await client.async_history(*WIDE)

    assert result == HistoryComplete((RemoteScrobble(ARTIST, "Dated", STARTED + 5),))


@pytest.mark.parametrize("marker", ["true", True, 1, "1"])
async def test_a_truthy_nowplaying_marker_is_skipped(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer, marker: object
) -> None:
    lastfm_server.queue(
        RECENT, recent_reply([row(**{"@attr": {"nowplaying": marker}}), row()])
    )

    assert await client.async_history(*WIDE) == HistoryComplete(
        (RemoteScrobble(ARTIST, TITLE, STARTED),)
    )


@pytest.mark.parametrize("marker", ["false", "", 0, "0", None, "maybe", [], False])
async def test_any_other_nowplaying_marker_is_unknown(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer, marker: object
) -> None:
    lastfm_server.queue(
        RECENT, recent_reply([row(**{"@attr": {"nowplaying": marker}})])
    )

    result = await client.async_history(*WIDE)

    assert result == HistoryUnknown("row_shape")


async def test_rows_outside_the_requested_window_are_dropped(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer
) -> None:
    lastfm_server.queue(
        RECENT,
        recent_reply(
            [
                row(title="Before", uts=STARTED - 100),
                row(title="Edge", uts=STARTED - 31),
                row(title="Inside", uts=STARTED),
                row(title="After", uts=STARTED + 400),
            ]
        ),
    )

    result = await client.async_history(STARTED - 31, STARTED + 246)

    assert result == HistoryComplete(
        (
            RemoteScrobble(ARTIST, "Edge", STARTED - 31),
            RemoteScrobble(ARTIST, "Inside", STARTED),
        )
    )


async def test_a_malformed_row_outside_the_window_is_still_unknown(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer
) -> None:
    outside = {"artist": {"#text": "A"}, "name": "N", "date": {"uts": "x"}}
    lastfm_server.queue(RECENT, recent_reply([row(), outside]))

    result = await client.async_history(STARTED - 31, STARTED + 246)

    assert isinstance(result, HistoryUnknown)


@pytest.mark.parametrize(
    "bad_row",
    [
        {"artist": {"#text": "A"}, "name": "N"},
        {"artist": {"#text": "A"}, "name": "N", "date": {}},
        {"artist": {"#text": "A"}, "name": "N", "date": {"uts": "x"}},
        {"artist": {"#text": "A"}, "name": "N", "date": {"uts": "-5"}},
        {"artist": {"#text": "A"}, "name": "N", "date": {"uts": True}},
        {"artist": {"#text": "A"}, "name": "N", "date": "12"},
        {"artist": {"#text": ""}, "name": "N", "date": {"uts": "5"}},
        {"artist": {"#text": "A"}, "name": 7, "date": {"uts": "5"}},
        {"name": "N", "date": {"uts": "5"}},
        "not an object",
    ],
)
async def test_missing_date_is_unknown(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer, bad_row: object
) -> None:
    lastfm_server.queue(RECENT, recent_reply([row(), bad_row]))

    result = await client.async_history(*WIDE)

    assert isinstance(result, HistoryUnknown)
    assert result.reason


@pytest.mark.parametrize(
    "body",
    [
        recent_body([row()], total_pages=None),
        recent_body([row()], total_pages="x"),
        recent_body([row()], total_pages=True),
        recent_body([row()], total_pages=-1),
        recent_body([row()], total_pages=0),
        recent_body([row()], page=None),
        recent_body([row()], page=2, total_pages=2),
        recent_body([row()], user="someone_else"),
        recent_body([row()], user=None),
        {"recenttracks": {"track": [row()]}},
        {"recenttracks": {"@attr": {"user": ACCOUNT_NAME, "page": 1, "totalPages": 1}}},
        {"recenttracks": []},
        {"other": {}},
        recent_body("not rows"),
        recent_body([row()] * 51),
    ],
)
async def test_missing_total_pages_is_unknown(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer, body: object
) -> None:
    lastfm_server.queue(RECENT, json_reply(body))

    assert isinstance(await client.async_history(*WIDE), HistoryUnknown)


async def test_account_name_comparison_ignores_case(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer
) -> None:
    lastfm_server.queue(RECENT, recent_reply([row()], user=ACCOUNT_NAME.upper()))

    assert isinstance(await client.async_history(*WIDE), HistoryComplete)


async def test_pages_are_followed_and_merged(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer
) -> None:
    lastfm_server.queue(
        RECENT,
        recent_reply([row(uts=30)], page=1, total_pages=3),
        recent_reply([row(uts=20)], page=2, total_pages=3),
        recent_reply([row(uts=10)], page=3, total_pages=3),
    )

    result = await client.async_history(*WIDE)

    assert result == HistoryComplete(
        tuple(RemoteScrobble(ARTIST, TITLE, uts) for uts in (30, 20, 10))
    )
    assert [call.form["page"] for call in lastfm_server.calls(RECENT)] == [
        "1",
        "2",
        "3",
    ]


async def test_pagination_truncated_or_over_limit_is_unknown(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer
) -> None:
    # More pages than the cap: unknown without fetching them.
    lastfm_server.queue(RECENT, recent_reply([row()], page=1, total_pages=5))
    over_limit = await client.async_history(*WIDE)
    assert isinstance(over_limit, HistoryUnknown)
    assert len(lastfm_server.calls(RECENT)) == 1

    # A later page that fails, repeats the wrong page or is malformed.
    for second in (
        Reply(status=500),
        json_reply({"error": 29}),
        recent_reply([row()], page=1, total_pages=2),
        Reply(body=b"{"),
    ):
        lastfm_server.queue(
            RECENT, recent_reply([row()], page=1, total_pages=2), second
        )
        assert isinstance(await client.async_history(*WIDE), HistoryUnknown)


@pytest.mark.parametrize(
    "page_counts", [(2, 1), (2, 3, 3), (2, 0), (3, 3, 2), (3, 2, 3)]
)
async def test_changed_page_count_is_unknown(
    client: LastFmDeliveryClient,
    lastfm_server: LastFmServer,
    page_counts: tuple[int, ...],
) -> None:
    lastfm_server.queue(
        RECENT,
        *[
            recent_reply(
                [] if total == 0 else [row(uts=100 - page)],
                page=page,
                total_pages=total,
            )
            for page, total in enumerate(page_counts, 1)
        ],
    )

    result = await client.async_history(*WIDE)

    assert isinstance(result, HistoryUnknown)
    assert result.reason == "pagination_changed"


async def test_a_page_beyond_the_reported_count_is_unknown(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer
) -> None:
    lastfm_server.queue(RECENT, recent_reply([row()], page=2, total_pages=1))

    assert isinstance(await client.async_history(*WIDE), HistoryUnknown)


async def test_pages_up_to_the_cap_are_complete(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer
) -> None:
    lastfm_server.queue(
        RECENT,
        *[
            recent_reply([row(uts=page)], page=page, total_pages=4)
            for page in range(1, 5)
        ],
    )

    result = await client.async_history(*WIDE)

    assert isinstance(result, HistoryComplete)
    assert len(result.scrobbles) == 4


async def test_empty_complete_history_is_absent(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer
) -> None:
    lastfm_server.queue(RECENT, recent_reply([]), recent_reply([], total_pages="0"))

    assert await client.async_history(*WIDE) == HistoryComplete(())
    assert await client.async_history(*WIDE) == HistoryComplete(())


@pytest.mark.parametrize(
    "reply",
    [
        json_reply({"error": 17, "message": CANARY}),
        json_reply({"error": 17, "message": CANARY}, status=403),
        json_reply({"error": 6}),
        json_reply({"error": 29}, status=429),
        Reply(status=429),
        Reply(status=500),
        Reply(status=302, headers={"Location": "/"}),
        Reply(body=b" " * (257 * 1024)),
        Reply(body=b"[]"),
        Reply(drop=True),
    ],
)
async def test_private_profile_error_is_unknown(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer, reply: Reply
) -> None:
    lastfm_server.queue(RECENT, reply)

    result = await client.async_history(*WIDE)

    assert isinstance(result, HistoryUnknown)
    assert CANARY not in result.reason


@pytest.mark.parametrize(
    "reply",
    [
        json_reply({"error": 29}),
        json_reply({"error": "29"}, status=403),
        json_reply({"error": 29}, status=429),
        Reply(status=429),
    ],
)
async def test_rate_limited_history_says_so(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer, reply: Reply
) -> None:
    lastfm_server.queue(RECENT, reply)

    result = await client.async_history(*WIDE)

    assert result == HistoryUnknown("rate_limited", rate_limited=True)


@pytest.mark.parametrize(
    "reply",
    [
        json_reply({"error": 10}),
        json_reply({"error": "10"}, status=403),
        json_reply({"error": 26}),
        json_reply({"error": 26}, status=403),
    ],
)
async def test_history_says_when_the_api_key_itself_failed(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer, reply: Reply
) -> None:
    lastfm_server.queue(RECENT, reply)

    result = await client.async_history(*WIDE)

    assert result == HistoryUnknown("auth_error", auth_required=True)


@pytest.mark.parametrize(
    "reply",
    [
        json_reply({"error": 17}),
        json_reply({"error": 17}, status=403),
        json_reply({"error": 4}),
        json_reply({"error": 9}),
        Reply(status=403),
    ],
)
async def test_other_history_errors_do_not_claim_a_key_failure(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer, reply: Reply
) -> None:
    lastfm_server.queue(RECENT, reply)

    result = await client.async_history(*WIDE)

    assert isinstance(result, HistoryUnknown)
    assert result.auth_required is False


@pytest.mark.parametrize(
    "reply",
    [json_reply({"error": 17}), Reply(status=500), Reply(body=b"[]")],
)
async def test_other_history_failures_do_not_claim_a_rate_limit(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer, reply: Reply
) -> None:
    lastfm_server.queue(RECENT, reply)

    result = await client.async_history(*WIDE)

    assert isinstance(result, HistoryUnknown)
    assert result.rate_limited is False


async def test_history_failures_without_a_response_are_unknown(
    client: LastFmDeliveryClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = await asyncio.start_server(lambda *_: None, "127.0.0.1", 0)
    port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()
    monkeypatch.setattr(lastfm, "API_ROOT", f"http://127.0.0.1:{port}/")

    assert isinstance(await client.async_history(*WIDE), HistoryUnknown)


async def test_history_response_limit_is_larger_than_the_auth_limit(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer
) -> None:
    padded = recent_reply([row()]).body + b" " * (100 * 1024)
    lastfm_server.queue(RECENT, Reply(body=padded))

    assert isinstance(await client.async_history(*WIDE), HistoryComplete)
    assert delivery.MAX_HISTORY_RESPONSE_BYTES == 256 * 1024
    assert (delivery.HISTORY_PAGE_LIMIT, delivery.MAX_HISTORY_PAGES) == (50, 4)


async def test_history_cancellation_propagates(
    client: LastFmDeliveryClient, raw_server: RawServer
) -> None:
    connected = asyncio.Event()

    async def stall(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_request(reader)
        connected.set()
        await reader.read()
        writer.close()

    await raw_server(stall)
    task = asyncio.create_task(client.async_history(*WIDE))
    async with asyncio.timeout(2):
        await connected.wait()

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# --- matching ----------------------------------------------------------------------


def _remote(artist: str, title: str) -> RemoteScrobble:
    return RemoteScrobble(artist, title, STARTED)


@pytest.mark.parametrize(
    ("artist", "title", "remote_artist", "remote_title"),
    [
        ("Beyoncé", "Halo", "BEYONCÉ", "halo"),
        ("Beyoncé", "Halo", "Beyoncé", "Halo"),
        ("\uff21\uff22\uff23", "\uff33\uff4f\uff4e\uff47", "abc", "song"),
        ("Straße", "Weiß", "STRASSE", "WEISS"),
        ("Artist", "  Spaced \t Out  ", "Artist", "spaced out"),
        ("Artist", "Song (Remastered 2011)", "Artist", "Song"),
        ("Artist", "Song", "Artist", "Song [Live] (Bonus)"),
        ("Artist", "Song - Remastered 2011", "Artist", "Song"),
        ("Artist", "Song", "Artist", "Song - Radio Edit"),
        ("Artist", "Song", "Artist", "Song \u2013 Radio Edit"),
        ("Artist", "Song", "Artist", "Song \u2014 Radio Edit"),
        ("Artist", "Song (Live) [2011 Remaster]", "Artist", "Song"),
        ("Artist", "Song", "Artist", "Song (Live (2011)) [Bonus]"),
        ("Artist", "Song", "Artist", "Song (Live [Take 2])"),
        ("Artist", "(Intro)", "Artist", "(Intro)"),
    ],
)
def test_unicode_and_case_match(
    artist: str, title: str, remote_artist: str, remote_title: str
) -> None:
    assert matches_track(artist, title, _remote(remote_artist, remote_title))


@pytest.mark.parametrize(
    ("artist", "title", "remote_artist", "remote_title"),
    [
        ("Artist", "Song", "Artist", "Another Song"),
        ("Artist", "Song", "Other Artist", "Song"),
        ("Artist", "Song A", "Artist", "Song B"),
        ("Artist", "(Intro)", "Artist", "(Outro)"),
        ("Artist", "Song", "Artist", ""),
        ("", "Song", "Artist", "Song"),
        ("Artist", "Song (Live)", "Artist", "Songs"),
        ("Artist", "(Intro)", "Artist", "Song"),
        ("Artist", "Song", "Artist", "(Intro)"),
        ("Artist", "(Intro)", "Artist", "[Intro]"),
        ("Artist", "Song \u2013 Live", "Artist", "Other \u2013 Live"),
    ],
)
def test_different_song_does_not_match(
    artist: str, title: str, remote_artist: str, remote_title: str
) -> None:
    assert not matches_track(artist, title, _remote(remote_artist, remote_title))


@pytest.mark.parametrize(
    ("ours", "theirs"),
    [
        ("Main Artist", "Main Artist feat. Guest"),
        ("Main Artist feat. Guest", "Main Artist"),
        ("Main Artist, Guest", "Main Artist"),
        ("Main Artist & Guest", "Main Artist ft. Other"),
        ("Main Artist", "main artist, someone, else"),
        ("Main Artist", "Main Artist featuring Guest"),
        ("Main Artist", "Main Artist (feat. Guest)"),
        ("Main Artist", "Main Artist (ft. Guest)"),
        ("Main Artist", "Main Artist; Guest"),
        ("Main Artist", "Main Artist and Guest"),
        ("Main Artist", "Main Artist x Guest"),
        ("Main Artist", "Main Artist/Guest"),
        ("Main Artist / Guest", "Main Artist"),
    ],
)
def test_multi_artist_matches_first_artist(ours: str, theirs: str) -> None:
    assert matches_track(ours, "Song", _remote(theirs, "Song"))


def test_a_guest_alone_is_not_the_main_artist() -> None:
    assert not matches_track("Guest", "Song", _remote("Main Artist, Guest", "Song"))


def test_a_shared_first_word_is_an_accepted_false_positive() -> None:
    # Conservative on purpose: a duplicate held back beats a duplicate written.
    assert matches_track("Simon", "Song", _remote("Simon & Garfunkel", "Song"))


@pytest.mark.parametrize("remote_artist", ["Mainstream", "Main Artists", "Mainx Guest"])
def test_separators_need_their_own_boundaries(remote_artist: str) -> None:
    assert not matches_track("Main", "Song", _remote(remote_artist, "Song"))


def test_bracket_only_titles_fall_back_to_the_full_title() -> None:
    assert matches_track("A", "(Intro)", _remote("A", "(Intro)"))


def test_matching_ignores_the_timestamp_and_never_alters_outbound_text() -> None:
    value = payload(artist="Artist feat. Guest", track="Song (Live)")

    assert matches_track(value.artist, value.track, RemoteScrobble("Artist", "Song", 1))
    assert (value.artist, value.track) == ("Artist feat. Guest", "Song (Live)")


def test_results_are_immutable_values() -> None:
    result = ScrobbleResult(ScrobbleOutcome.ACCEPTED, "accepted")

    assert result.rate_limited is False
    assert {outcome.value for outcome in ScrobbleOutcome} == {
        "accepted",
        "ignored",
        "safe_retry",
        "auth_required",
        "permanent_reject",
        "uncertain",
    }
    with pytest.raises(AttributeError):
        result.reason = "x"  # type: ignore[misc]
