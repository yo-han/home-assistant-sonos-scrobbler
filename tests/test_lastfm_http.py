"""The client against real sockets, through Home Assistant's shared session."""

import asyncio
import gzip
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import ClientSession
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.personal_sonos_scrobbler import lastfm
from custom_components.personal_sonos_scrobbler.lastfm import (
    LastFmClient,
    LastFmCredentials,
    LastFmError,
    LastFmErrorCode,
    sign_request,
)
from tests.lastfm_server import (
    ACCOUNT_NAME,
    API_KEY,
    SESSION_KEY,
    SHARED_SECRET,
    TOKEN,
    LastFmServer,
    Reply,
    json_reply,
    session_reply,
    token_reply,
)

GET_TOKEN = "auth.getToken"
GET_SESSION = "auth.getSession"
CANARY = "canary-provider-message"
MAX_BYTES = 64 * 1024
TOKEN_BODY = b'{"token": "%s"}' % TOKEN.encode()
TOKEN_WITH_NAN = b'{"token": "%s", "other": NaN}' % TOKEN.encode()
NESTING = MAX_BYTES // 2
NESTED_BODY = b"[" * NESTING + b"]" * NESTING
type Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]
type RawServer = Callable[[Handler], Awaitable[None]]


@pytest.fixture
async def client(hass: HomeAssistant, lastfm_server: LastFmServer) -> LastFmClient:
    """Return the real client on the real shared session."""
    return LastFmClient(
        async_get_clientsession(hass),
        LastFmCredentials(api_key=API_KEY, shared_secret=SHARED_SECRET),
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


async def _error(call: Awaitable[object]) -> LastFmError:
    with pytest.raises(LastFmError) as caught:
        await call
    return caught.value


def _padded_token(size: int) -> bytes:
    """Return a valid reply of exactly size bytes: only the limit can refuse it."""
    return TOKEN_BODY + b" " * (size - len(TOKEN_BODY))


async def _read_request(reader: asyncio.StreamReader) -> None:
    await reader.readuntil(b"\r\n\r\n")


async def test_get_token_posts_signed_form_without_query(
    client: LastFmClient, lastfm_server: LastFmServer
) -> None:
    assert await client.async_create_token() == TOKEN

    request = lastfm_server.requests[0]
    signed = {"method": GET_TOKEN, "api_key": API_KEY}
    assert request.http_method == "POST"
    assert request.path == "/"
    assert request.query == ""
    assert request.form == {
        **signed,
        "format": "json",
        "api_sig": sign_request(signed, SHARED_SECRET),
    }
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert request.headers["Accept-Encoding"] == "identity"


async def test_get_session_signs_the_token(
    client: LastFmClient, lastfm_server: LastFmServer
) -> None:
    session = await client.async_create_session(TOKEN)

    assert (session.account_name, session.session_key) == (ACCOUNT_NAME, SESSION_KEY)
    signed = {"method": GET_SESSION, "api_key": API_KEY, "token": TOKEN}
    assert lastfm_server.requests[0].form == {
        **signed,
        "format": "json",
        "api_sig": sign_request(signed, SHARED_SECRET),
    }


async def test_redirect_is_not_followed(
    client: LastFmClient, lastfm_server: LastFmServer
) -> None:
    lastfm_server.queue(
        GET_TOKEN,
        Reply(status=302, headers={"Location": f"{lastfm_server.url}elsewhere"}),
    )

    error = await _error(client.async_create_token())

    assert error.code is LastFmErrorCode.UNEXPECTED_RESPONSE
    assert [request.path for request in lastfm_server.requests] == ["/"]


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (Reply(status=500), LastFmErrorCode.PROVIDER_UNAVAILABLE),
        (Reply(status=503, body=b"<html>"), LastFmErrorCode.PROVIDER_UNAVAILABLE),
        (Reply(status=429), LastFmErrorCode.RATE_LIMITED),
        (Reply(status=404), LastFmErrorCode.UNEXPECTED_RESPONSE),
        (Reply(status=204), LastFmErrorCode.UNEXPECTED_RESPONSE),
        (Reply(body=b"not json"), LastFmErrorCode.UNEXPECTED_RESPONSE),
        (Reply(body=b"[1]"), LastFmErrorCode.UNEXPECTED_RESPONSE),
        (Reply(body=NESTED_BODY), LastFmErrorCode.UNEXPECTED_RESPONSE),
        (Reply(body=TOKEN_WITH_NAN), LastFmErrorCode.UNEXPECTED_RESPONSE),
        (Reply(body=b'{"error": true}'), LastFmErrorCode.UNEXPECTED_RESPONSE),
        (Reply(body=b'{"error": "14"}'), LastFmErrorCode.UNEXPECTED_RESPONSE),
        (Reply(body=b'{"token": "\xff"}'), LastFmErrorCode.UNEXPECTED_RESPONSE),
        (json_reply({"error": {"code": 1}}, status=429), LastFmErrorCode.RATE_LIMITED),
        (
            json_reply({"error": "busy"}, status=503),
            LastFmErrorCode.PROVIDER_UNAVAILABLE,
        ),
        (
            json_reply({"error": True, "token": TOKEN}),
            LastFmErrorCode.UNEXPECTED_RESPONSE,
        ),
        (json_reply({"token": 12}), LastFmErrorCode.UNEXPECTED_RESPONSE),
        (token_reply(""), LastFmErrorCode.UNEXPECTED_RESPONSE),
        (token_reply("x" * 129), LastFmErrorCode.UNEXPECTED_RESPONSE),
        (token_reply("control\x00token"), LastFmErrorCode.UNEXPECTED_RESPONSE),
        (token_reply(f"{TOKEN}\n"), LastFmErrorCode.UNEXPECTED_RESPONSE),
    ],
)
async def test_malformed_or_failed_token_replies_map_to_fixed_codes(
    client: LastFmClient,
    lastfm_server: LastFmServer,
    reply: Reply,
    expected: LastFmErrorCode,
) -> None:
    lastfm_server.queue(GET_TOKEN, reply)

    assert (await _error(client.async_create_token())).code is expected


@pytest.mark.parametrize(
    "reply",
    [
        json_reply({"session": []}),
        json_reply({"session": {"name": ACCOUNT_NAME}}),
        session_reply(name=""),
        session_reply(key=""),
        session_reply(name="line\nbreak"),
        session_reply(name="bi‮di"),
        session_reply(name="code`span"),
        session_reply(name="x" * 129),
        session_reply(key="x" * 129),
        session_reply(key="ctrl\x07"),
    ],
)
async def test_invalid_session_shapes_are_rejected(
    client: LastFmClient, lastfm_server: LastFmServer, reply: Reply
) -> None:
    lastfm_server.queue(GET_SESSION, reply)

    error = await _error(client.async_create_session(TOKEN))

    assert error.code is LastFmErrorCode.UNEXPECTED_RESPONSE


@pytest.mark.parametrize(
    ("api_code", "expected"),
    [
        (4, LastFmErrorCode.INVALID_CREDENTIALS),
        (10, LastFmErrorCode.INVALID_CREDENTIALS),
        (13, LastFmErrorCode.INVALID_CREDENTIALS),
        (26, LastFmErrorCode.API_KEY_SUSPENDED),
        (29, LastFmErrorCode.RATE_LIMITED),
        (8, LastFmErrorCode.PROVIDER_UNAVAILABLE),
        (11, LastFmErrorCode.PROVIDER_UNAVAILABLE),
        (16, LastFmErrorCode.PROVIDER_UNAVAILABLE),
        (14, LastFmErrorCode.UNEXPECTED_RESPONSE),
        (15, LastFmErrorCode.UNEXPECTED_RESPONSE),
        (999, LastFmErrorCode.UNEXPECTED_RESPONSE),
    ],
)
async def test_get_token_api_error_codes(
    client: LastFmClient,
    lastfm_server: LastFmServer,
    api_code: int,
    expected: LastFmErrorCode,
) -> None:
    lastfm_server.queue(GET_TOKEN, json_reply({"error": api_code, "message": CANARY}))

    assert (await _error(client.async_create_token())).code is expected


@pytest.mark.parametrize(
    ("api_code", "expected"),
    [
        (14, LastFmErrorCode.TOKEN_NOT_AUTHORIZED),
        (15, LastFmErrorCode.TOKEN_INVALID),
        (4, LastFmErrorCode.TOKEN_INVALID),
        (10, LastFmErrorCode.INVALID_CREDENTIALS),
        (13, LastFmErrorCode.INVALID_CREDENTIALS),
        (26, LastFmErrorCode.API_KEY_SUSPENDED),
        (29, LastFmErrorCode.RATE_LIMITED),
        (8, LastFmErrorCode.PROVIDER_UNAVAILABLE),
        (11, LastFmErrorCode.PROVIDER_UNAVAILABLE),
        (16, LastFmErrorCode.PROVIDER_UNAVAILABLE),
        (999, LastFmErrorCode.UNEXPECTED_RESPONSE),
    ],
)
@pytest.mark.parametrize("status", [200, 403])
async def test_get_session_api_error_beats_http_status(
    client: LastFmClient,
    lastfm_server: LastFmServer,
    api_code: int,
    expected: LastFmErrorCode,
    status: int,
) -> None:
    lastfm_server.queue(
        GET_SESSION, json_reply({"error": api_code, "message": CANARY}, status=status)
    )

    assert (await _error(client.async_create_session(TOKEN))).code is expected


async def test_provider_message_reaches_no_exception_field(
    client: LastFmClient, lastfm_server: LastFmServer
) -> None:
    lastfm_server.queue(GET_SESSION, json_reply({"error": 14, "message": CANARY}))

    error = await _error(client.async_create_session(TOKEN))

    assert CANARY not in f"{error!s} {error!r} {error.args}"
    assert error.__cause__ is None
    assert error.__context__ is None


async def test_valid_body_at_the_limit_is_accepted(
    client: LastFmClient, lastfm_server: LastFmServer
) -> None:
    lastfm_server.queue(GET_TOKEN, Reply(body=_padded_token(MAX_BYTES)))

    assert await client.async_create_token() == TOKEN


async def test_valid_body_over_the_limit_is_refused(
    client: LastFmClient, lastfm_server: LastFmServer
) -> None:
    lastfm_server.queue(GET_TOKEN, Reply(body=_padded_token(MAX_BYTES + 1)))

    error = await _error(client.async_create_token())

    assert error.code is LastFmErrorCode.UNEXPECTED_RESPONSE


async def test_valid_chunked_body_over_the_limit_is_cut_off(
    client: LastFmClient, lastfm_server: LastFmServer
) -> None:
    padded = _padded_token(MAX_BYTES + 1)
    chunks = [padded[start : start + 4096] for start in range(0, len(padded), 4096)]
    lastfm_server.queue(GET_TOKEN, Reply(chunks=chunks))

    error = await _error(client.async_create_token())

    assert error.code is LastFmErrorCode.UNEXPECTED_RESPONSE


async def test_oversized_gateway_page_still_reports_the_outage(
    client: LastFmClient, lastfm_server: LastFmServer
) -> None:
    lastfm_server.queue(
        GET_TOKEN, Reply(status=502, body=b"x" * (lastfm.MAX_RESPONSE_BYTES + 1))
    )

    error = await _error(client.async_create_token())

    assert error.code is LastFmErrorCode.PROVIDER_UNAVAILABLE


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "GZIP", "identity, gzip"])
async def test_any_declared_encoding_is_refused_even_on_a_plain_valid_body(
    client: LastFmClient, lastfm_server: LastFmServer, encoding: str
) -> None:
    lastfm_server.queue(
        GET_TOKEN, Reply(body=TOKEN_BODY, headers={"Content-Encoding": encoding})
    )

    error = await _error(client.async_create_token())

    assert error.code is LastFmErrorCode.UNEXPECTED_RESPONSE


async def test_gzip_of_a_valid_token_is_refused(
    client: LastFmClient, lastfm_server: LastFmServer
) -> None:
    lastfm_server.queue(
        GET_TOKEN,
        Reply(body=gzip.compress(TOKEN_BODY), headers={"Content-Encoding": "gzip"}),
    )

    error = await _error(client.async_create_token())

    assert error.code is LastFmErrorCode.UNEXPECTED_RESPONSE


async def test_a_second_encoding_header_is_not_ignored(
    client: LastFmClient, raw_server: RawServer
) -> None:
    async def reply(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_request(reader)
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Encoding: identity\r\n"
            b"Content-Encoding: gzip\r\nContent-Length: %d\r\n"
            b"Connection: close\r\n\r\n%s" % (len(TOKEN_BODY), TOKEN_BODY)
        )
        await writer.drain()
        writer.close()

    await raw_server(reply)

    error = await _error(client.async_create_token())

    assert error.code is LastFmErrorCode.UNEXPECTED_RESPONSE


async def test_request_disables_redirects_and_decompression(
    hass: HomeAssistant, lastfm_server: LastFmServer
) -> None:
    session = async_get_clientsession(hass)
    received: dict[str, object] = {}

    def recording_post(url: str, **kwargs: Any) -> Any:
        received.update(kwargs)
        return session.post(url, **kwargs)

    spy = SimpleNamespace(post=recording_post)
    client = LastFmClient(
        cast(ClientSession, spy),
        LastFmCredentials(api_key=API_KEY, shared_secret=SHARED_SECRET),
    )

    assert await client.async_create_token() == TOKEN
    assert received["allow_redirects"] is False
    assert received["auto_decompress"] is False


async def test_identity_encoding_header_is_accepted(
    client: LastFmClient, lastfm_server: LastFmServer
) -> None:
    lastfm_server.queue(
        GET_TOKEN,
        Reply(body=TOKEN_BODY, headers={"Content-Encoding": "identity"}),
    )

    assert await client.async_create_token() == TOKEN


async def test_connection_failure_is_provider_unavailable(
    client: LastFmClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = await asyncio.start_server(lambda *_: None, "127.0.0.1", 0)
    port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()
    monkeypatch.setattr(lastfm, "API_ROOT", f"http://127.0.0.1:{port}/")

    error = await _error(client.async_create_token())

    assert error.code is LastFmErrorCode.PROVIDER_UNAVAILABLE
    assert error.__context__ is None


async def test_stalled_headers_hit_the_deadline(
    client: LastFmClient, raw_server: RawServer
) -> None:
    async def stall(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_request(reader)
        await reader.read()
        writer.close()

    await raw_server(stall)

    error = await _error(client.async_create_token())

    assert error.code is LastFmErrorCode.PROVIDER_UNAVAILABLE


async def test_dripping_body_hits_the_total_deadline(
    client: LastFmClient, raw_server: RawServer
) -> None:
    finished = asyncio.Event()

    async def drip(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_request(reader)
        writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
        try:
            while not reader.at_eof():
                writer.write(b"1\r\n \r\n")
                await writer.drain()
                await asyncio.sleep(0.02)
        except ConnectionError:
            pass
        finally:
            writer.close()
            finished.set()

    await raw_server(drip)
    started = time.monotonic()

    error = await _error(client.async_create_token())

    assert error.code is LastFmErrorCode.PROVIDER_UNAVAILABLE
    assert time.monotonic() - started < 2
    # Server.close() stops accepting connections, not this sleeping handler.
    async with asyncio.timeout(1):
        await finished.wait()


async def test_cancellation_propagates(
    client: LastFmClient, raw_server: RawServer
) -> None:
    connected = asyncio.Event()

    async def stall(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_request(reader)
        connected.set()
        await reader.read()
        writer.close()

    await raw_server(stall)
    task = asyncio.create_task(client.async_create_token())
    await connected.wait()

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (Reply(status=302, headers={"Location": CANARY}), "redirect"),
        (Reply(headers={"Content-Encoding": CANARY}), "content_encoding"),
        (Reply(body=CANARY.encode() * MAX_BYTES), "response_size_limit"),
        (Reply(body=CANARY.encode()), "json_object"),
        (json_reply({"error": 6, "message": CANARY}), "api_error"),
        (json_reply({"error": CANARY}), "api_error_shape"),
        (json_reply({"token": CANARY + "\n"}), "token_shape"),
        (json_reply({CANARY: SHARED_SECRET}), "token_shape"),
        (Reply(status=404, body=CANARY.encode()), "http_status"),
    ],
)
async def test_rejection_logs_explain_shape_without_exposing_values(
    client: LastFmClient,
    lastfm_server: LastFmServer,
    caplog: pytest.LogCaptureFixture,
    reply: Reply,
    reason: str,
) -> None:
    lastfm_server.queue(GET_TOKEN, reply)

    await _error(client.async_create_token())

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == lastfm.__name__
    ]
    assert len(messages) == 1
    assert f"response rejected: {reason}" in messages[0]
    for secret in (CANARY, API_KEY, SHARED_SECRET, TOKEN, SESSION_KEY, ACCOUNT_NAME):
        assert secret not in messages[0]


async def test_unknown_api_error_cannot_smuggle_unbounded_numbers_into_logs(
    client: LastFmClient,
    lastfm_server: LastFmServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    lastfm_server.queue(GET_TOKEN, json_reply({"error": 10**100, "message": CANARY}))

    await _error(client.async_create_token())

    assert "api_code=outside_range" in caplog.text
    assert str(10**100) not in caplog.text
    assert CANARY not in caplog.text


@pytest.mark.parametrize(
    "token",
    [
        "ghijklmnopqrstuvwxyz0123456789AB",
        "a",
        "x" * 128,
        "opaque+/=&?#% token",
        "opaque-ünicode",
    ],
)
async def test_request_token_is_opaque_and_round_trips_unchanged(
    client: LastFmClient,
    lastfm_server: LastFmServer,
    caplog: pytest.LogCaptureFixture,
    token: str,
) -> None:
    lastfm_server.queue(GET_TOKEN, token_reply(token))

    returned = await client.async_create_token()
    authorization = urlsplit(client.authorization_url(returned))
    session = await client.async_create_session(returned)

    assert returned == token
    assert authorization.netloc == "www.last.fm"
    assert parse_qs(authorization.query) == {"api_key": [API_KEY], "token": [token]}
    request = lastfm_server.calls(GET_SESSION)[0]
    assert request.form["token"] == token
    assert request.form["api_sig"] == sign_request(
        {"method": GET_SESSION, "api_key": API_KEY, "token": token}, SHARED_SECRET
    )
    assert session.session_key == SESSION_KEY
    assert not [record for record in caplog.records if record.name == lastfm.__name__]
