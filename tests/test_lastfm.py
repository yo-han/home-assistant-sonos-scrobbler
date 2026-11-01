"""Signing, validation and masking of the Last.fm auth client, without a network."""

from types import MappingProxyType
from urllib.parse import parse_qs, urlsplit

import pytest

from custom_components.personal_sonos_scrobbler import lastfm
from custom_components.personal_sonos_scrobbler.lastfm import (
    AUTH_ROOT,
    LastFmClient,
    LastFmCredentials,
    LastFmError,
    LastFmErrorCode,
    LastFmSession,
    is_valid_api_key,
    is_valid_shared_secret,
    sign_request,
)
from tests.lastfm_server import API_KEY, SESSION_KEY, SHARED_SECRET, TOKEN

# Computed outside Python with `printf '<bytes>' | md5`.
UNICODE_VECTOR = "2c05cf2a7b1f4895dc353fd7a1e95201"
ASCII_VECTOR = "37a21fac22cd26cd76aef95bb1bd2f03"


def test_signature_matches_independent_unicode_vector() -> None:
    params = {"token": "tökèn", "method": "auth.getSession", "api_key": "abc"}

    assert sign_request(params, "söcret") == UNICODE_VECTOR


def test_signature_ignores_insertion_order() -> None:
    first = {"method": "auth.getToken", "api_key": "abc"}
    second = {"api_key": "abc", "method": "auth.getToken"}

    assert sign_request(first, "s3cret") == ASCII_VECTOR
    assert sign_request(second, "s3cret") == ASCII_VECTOR


@pytest.mark.parametrize("excluded", ["format", "callback", "api_sig"])
def test_signature_excludes_unsigned_parameters(excluded: str) -> None:
    params = {"method": "auth.getToken", "api_key": "abc", excluded: "ignored"}

    assert sign_request(params, "s3cret") == ASCII_VECTOR


def test_signature_does_not_mutate_input() -> None:
    params = MappingProxyType({"method": "auth.getToken", "api_key": "abc"})
    before = dict(params)

    sign_request(params, "s3cret")

    assert dict(params) == before


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (API_KEY, True),
        (API_KEY.upper(), True),
        (f"{API_KEY}\n", False),
        (API_KEY[:-1], False),
        (f"{API_KEY}0", False),
        ("g" * 32, False),
        ("", False),
    ],
)
def test_api_key_is_exactly_32_hex(value: str, expected: bool) -> None:
    assert is_valid_api_key(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (SHARED_SECRET, True),
        ("short", True),
        ("x" * 128, True),
        ("x" * 129, False),
        ("", False),
        ("line\nbreak", False),
        ("nul\x00byte", False),
    ],
)
def test_shared_secret_is_opaque_but_bounded(value: str, expected: bool) -> None:
    assert is_valid_shared_secret(value) is expected


def test_authorization_url_is_fixed_and_encoded() -> None:
    client = LastFmClient(_unused_session(), _credentials())

    url = client.authorization_url(TOKEN)

    parts = urlsplit(url)
    assert url.startswith(AUTH_ROOT)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == AUTH_ROOT
    assert parts.scheme == "https"
    assert parse_qs(parts.query) == {"api_key": [API_KEY], "token": [TOKEN]}


def test_authorization_url_escapes_separators() -> None:
    client = LastFmClient(_unused_session(), _credentials())

    query = urlsplit(client.authorization_url("a&b=c")).query

    assert parse_qs(query)["token"] == ["a&b=c"]


def test_reprs_hide_credentials_and_session() -> None:
    credentials = _credentials()
    session = LastFmSession(account_name="canary_name", session_key=SESSION_KEY)

    for text in (repr(credentials), str(credentials), repr(session), str(session)):
        for secret in (API_KEY, SHARED_SECRET, "canary_name", SESSION_KEY):
            assert secret not in text


def test_error_text_is_only_the_fixed_code() -> None:
    error = LastFmError(LastFmErrorCode.TOKEN_INVALID)

    assert error.code is LastFmErrorCode.TOKEN_INVALID
    assert str(error) == "token_invalid"
    assert error.args == ("token_invalid",)


def test_parser_survives_recursion_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    def exhausted(*args: object, **kwargs: object) -> object:
        raise RecursionError

    # 64 KiB does not reach this on every stack size, so it is forced here.
    monkeypatch.setattr(lastfm.json, "loads", exhausted)

    assert lastfm._parse_object(b"[]") is None


def _credentials() -> LastFmCredentials:
    return LastFmCredentials(api_key=API_KEY, shared_secret=SHARED_SECRET)


def _unused_session() -> object:
    return object()
