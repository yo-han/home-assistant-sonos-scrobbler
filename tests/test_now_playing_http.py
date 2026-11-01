"""One Now Playing request through the real client against a real socket server."""

import asyncio
import hashlib
import logging
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.personal_sonos_scrobbler import lastfm
from custom_components.personal_sonos_scrobbler.lastfm import LastFmCredentials
from custom_components.personal_sonos_scrobbler.lastfm_delivery import (
    LastFmDeliveryClient,
    NowPlayingOutcome,
    NowPlayingPayload,
    NowPlayingResult,
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

METHOD = "track.updateNowPlaying"
ARTIST = "Synthetic Årtist & Co"
TITLE = "Synthetic Tïtle"
ALBUM = "Synthetic Älbum"


def notice_body(code: object = "0") -> dict[str, Any]:
    """Return the documented reply, as JSON derived from its XML shape."""
    return {
        "nowplaying": {
            "artist": {"corrected": "0", "#text": "Provider Artist"},
            "track": {"corrected": "1", "#text": "Provider Title"},
            "album": {"corrected": "0", "#text": ""},
            "albumArtist": {"corrected": "0", "#text": ""},
            "ignoredMessage": {"code": code, "#text": "Provider text"},
        }
    }


@pytest.fixture
async def client(
    hass: HomeAssistant, lastfm_server: LastFmServer
) -> LastFmDeliveryClient:
    return LastFmDeliveryClient(
        async_get_clientsession(hass),
        LastFmCredentials(API_KEY, SHARED_SECRET),
        SESSION_KEY,
        ACCOUNT_NAME,
    )


def signature_of(form: dict[str, str]) -> str:
    """Recompute the documented signature without the production helper."""
    pairs = "".join(
        f"{name}{form[name]}"
        for name in sorted(form)
        if name not in {"api_sig", "format"}
    )
    return hashlib.md5(
        f"{pairs}{SHARED_SECRET}".encode(), usedforsecurity=False
    ).hexdigest()


async def test_signed_form_carries_the_track_and_no_timestamp(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer
) -> None:
    lastfm_server.queue(METHOD, json_reply(notice_body()))

    result = await client.async_now_playing(
        NowPlayingPayload(ARTIST, TITLE, ALBUM, 215)
    )

    call = lastfm_server.calls(METHOD)[0]
    assert result == NowPlayingResult(NowPlayingOutcome.ACCEPTED, "accepted")
    assert call.http_method == "POST"
    assert call.query == ""
    assert dict(call.form) == {
        "method": METHOD,
        "artist": ARTIST,
        "track": TITLE,
        "album": ALBUM,
        "duration": "215",
        "api_key": API_KEY,
        "sk": SESSION_KEY,
        "api_sig": signature_of(dict(call.form)),
        "format": "json",
    }
    assert len(lastfm_server.requests) == 1


async def test_optional_fields_are_left_out_of_the_form_and_signature(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer
) -> None:
    lastfm_server.queue(METHOD, json_reply(notice_body()))

    await client.async_now_playing(NowPlayingPayload(ARTIST, TITLE))

    form = dict(lastfm_server.calls(METHOD)[0].form)
    assert "album" not in form
    assert "duration" not in form
    assert form["api_sig"] == signature_of(form)


@pytest.mark.parametrize(
    ("reply", "outcome", "reason", "rate_limited"),
    [
        (
            json_reply(notice_body(0)),
            NowPlayingOutcome.ACCEPTED,
            "accepted",
            False,
        ),
        *(
            (
                json_reply(notice_body(code)),
                NowPlayingOutcome.IGNORED,
                "ignored",
                False,
            )
            for code in ("1", "2", "3", "4", 5)
        ),
        *(
            (
                json_reply(notice_body(code)),
                NowPlayingOutcome.FAILED,
                "ignored_message_shape",
                False,
            )
            for code in ("6", "-1", "x", None, True)
        ),
        (
            json_reply({"nowplaying": {}}),
            NowPlayingOutcome.FAILED,
            "ignored_message_shape",
            False,
        ),
        (json_reply({}), NowPlayingOutcome.FAILED, "nowplaying_shape", False),
        (
            json_reply({"nowplaying": []}),
            NowPlayingOutcome.FAILED,
            "nowplaying_shape",
            False,
        ),
        *(
            (
                json_reply({"error": code, "message": "Provider text"}),
                NowPlayingOutcome.AUTH_REQUIRED,
                "auth_error",
                False,
            )
            for code in (4, 9, 10, 13, 26)
        ),
        (
            json_reply({"error": 29, "message": "Provider text"}, status=429),
            NowPlayingOutcome.FAILED,
            "rate_limited",
            True,
        ),
        (Reply(status=429), NowPlayingOutcome.FAILED, "http_429", True),
        (
            json_reply({"error": 11, "message": "Provider text"}),
            NowPlayingOutcome.FAILED,
            "api_error",
            False,
        ),
        (
            json_reply({"error": "x", "message": "Provider text"}),
            NowPlayingOutcome.FAILED,
            "api_error_shape",
            False,
        ),
        (Reply(status=500), NowPlayingOutcome.FAILED, "http_status", False),
        (Reply(body=b"{ not json"), NowPlayingOutcome.FAILED, "body_not_object", False),
        (
            Reply(chunks=(b" " * 70_000,)),
            NowPlayingOutcome.FAILED,
            "body_not_object",
            False,
        ),
        (
            Reply(status=302, headers={"Location": "https://example.invalid/"}),
            NowPlayingOutcome.FAILED,
            "http_status",
            False,
        ),
        (Reply(drop=True), NowPlayingOutcome.FAILED, "transport_failed", False),
    ],
)
async def test_every_reply_has_one_fixed_result(
    client: LastFmDeliveryClient,
    lastfm_server: LastFmServer,
    reply: Reply,
    outcome: NowPlayingOutcome,
    reason: str,
    rate_limited: bool,
) -> None:
    lastfm_server.queue(METHOD, reply)

    result = await client.async_now_playing(NowPlayingPayload(ARTIST, TITLE))

    assert result == NowPlayingResult(outcome, reason, rate_limited)
    assert len(lastfm_server.calls(METHOD)) == 1


async def test_timeout_is_one_failed_attempt(
    client: LastFmDeliveryClient,
    lastfm_server: LastFmServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lastfm, "REQUEST_DEADLINE_SECONDS", 0.05)
    lastfm_server.queue(METHOD, Reply(delay=1.0))

    result = await client.async_now_playing(NowPlayingPayload(ARTIST, TITLE))

    assert result == NowPlayingResult(NowPlayingOutcome.FAILED, "transport_failed")
    assert len(lastfm_server.calls(METHOD)) == 1


async def test_cancellation_propagates(
    client: LastFmDeliveryClient, lastfm_server: LastFmServer
) -> None:
    lastfm_server.hold(METHOD)
    request = asyncio.ensure_future(
        client.async_now_playing(NowPlayingPayload(ARTIST, TITLE))
    )
    await lastfm_server.held.wait()

    request.cancel()

    with pytest.raises(asyncio.CancelledError):
        await request


async def test_results_logs_and_reprs_carry_no_listening_data(
    client: LastFmDeliveryClient,
    lastfm_server: LastFmServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    lastfm_server.queue(METHOD, json_reply(notice_body("1")))
    payload = NowPlayingPayload(ARTIST, TITLE, ALBUM, 215)

    result = await client.async_now_playing(payload)

    text = f"{caplog.text}{payload!r}{result!r}"
    for secret in (ARTIST, TITLE, ALBUM, "Provider", SESSION_KEY, SHARED_SECRET):
        assert secret not in text


@pytest.mark.parametrize(
    "overrides",
    [
        {"artist": " "},
        {"track": ""},
        {"track": "a\x00b"},
        {"album": "\n"},
        {"artist": "x" * 513},
        {"duration": 0},
        {"duration": 86_401},
        {"duration": True},
    ],
)
def test_payload_refuses_what_the_form_cannot_carry(overrides: dict[str, Any]) -> None:
    values: dict[str, Any] = {"artist": ARTIST, "track": TITLE, **overrides}

    with pytest.raises(ValueError, match="invalid now playing payload"):
        NowPlayingPayload(**values)
