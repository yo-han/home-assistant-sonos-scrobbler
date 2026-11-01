"""The only module that talks to Last.fm: signed desktop authorization, nothing else.

Errors carry a fixed code and never provider text, request parameters or URLs. They
are raised outside except blocks so that no chained exception can hold those either.
"""

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, NoReturn, TypeIs
from urllib.parse import urlencode

import aiohttp

API_ROOT = "https://ws.audioscrobbler.com/2.0/"
AUTH_ROOT = "https://www.last.fm/api/auth/"
REQUEST_DEADLINE_SECONDS = 10.0
MAX_RESPONSE_BYTES = 64 * 1024
MAX_TEXT_LENGTH = 128

_LOGGER = logging.getLogger(__name__)

_READ_CHUNK_BYTES = 4096
_UNSIGNED_PARAMETERS = frozenset({"format", "callback", "api_sig"})
_HEX_32 = re.compile(r"[0-9a-fA-F]{32}")

type _Method = Literal["auth.getToken", "auth.getSession"]


class LastFmErrorCode(StrEnum):
    """Stable failures; the values double as flow error keys."""

    TOKEN_NOT_AUTHORIZED = "token_not_authorized"
    TOKEN_INVALID = "token_invalid"
    INVALID_CREDENTIALS = "invalid_credentials"
    API_KEY_SUSPENDED = "api_key_suspended"
    RATE_LIMITED = "rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    UNEXPECTED_RESPONSE = "unexpected_response"


_COMMON_API_ERRORS = {
    8: LastFmErrorCode.PROVIDER_UNAVAILABLE,
    10: LastFmErrorCode.INVALID_CREDENTIALS,
    11: LastFmErrorCode.PROVIDER_UNAVAILABLE,
    13: LastFmErrorCode.INVALID_CREDENTIALS,
    16: LastFmErrorCode.PROVIDER_UNAVAILABLE,
    26: LastFmErrorCode.API_KEY_SUSPENDED,
    29: LastFmErrorCode.RATE_LIMITED,
}
# Code 4 is documented per method: failed authentication versus an invalid token.
_API_ERRORS: Mapping[_Method, Mapping[int, LastFmErrorCode]] = {
    "auth.getToken": {**_COMMON_API_ERRORS, 4: LastFmErrorCode.INVALID_CREDENTIALS},
    "auth.getSession": {
        **_COMMON_API_ERRORS,
        4: LastFmErrorCode.TOKEN_INVALID,
        14: LastFmErrorCode.TOKEN_NOT_AUTHORIZED,
        15: LastFmErrorCode.TOKEN_INVALID,
    },
}


class LastFmError(Exception):
    """A failed exchange; its text is the fixed code and nothing else."""

    def __init__(self, code: LastFmErrorCode) -> None:
        """Keep the code for the flow."""
        super().__init__(code.value)
        self.code = code


@dataclass(frozen=True, slots=True)
class LastFmCredentials:
    """The user's own API application."""

    api_key: str = field(repr=False)
    shared_secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class LastFmSession:
    """The result of a completed authorization."""

    account_name: str = field(repr=False)
    session_key: str = field(repr=False)


def sign_request(params: Mapping[str, str], shared_secret: str) -> str:
    """Return the MD5 signature over the sorted name/value pairs and the secret."""
    pairs = "".join(
        f"{name}{params[name]}"
        for name in sorted(params)
        if name not in _UNSIGNED_PARAMETERS
    )
    digest = hashlib.md5(f"{pairs}{shared_secret}".encode(), usedforsecurity=False)
    return digest.hexdigest()


def is_valid_api_key(value: str) -> bool:
    """Return whether the value has the documented 32-hex form."""
    return _HEX_32.fullmatch(value) is not None


def is_valid_shared_secret(value: str) -> bool:
    """Accept any bounded printable text: the format is not documented."""
    return _is_bounded_text(value)


class LastFmClient:
    """Create tokens and exchange them for sessions; no other call exists."""

    def __init__(
        self, session: aiohttp.ClientSession, credentials: LastFmCredentials
    ) -> None:
        """Use the given session; the endpoints are fixed in this module."""
        self._session = session
        self._credentials = credentials

    async def async_create_token(self) -> str:
        """Request an unauthorized token."""
        payload = await self._async_request("auth.getToken", {})
        token = payload.get("token")
        # Tokens are provider-owned opaque values, not locally verified hashes.
        # The live API returns non-hex tokens despite its older documentation.
        if _is_bounded_text(token):
            return token
        # Only shape metadata: never include the token or provider text.
        _LOGGER.warning(
            "Last.fm auth response rejected: token_shape "
            "(present=%s, string=%s, length=%d)",
            "token" in payload,
            isinstance(token, str),
            len(token) if isinstance(token, str) else 0,
        )
        _raise_unexpected()

    async def async_create_session(self, token: str) -> LastFmSession:
        """Exchange an authorized token for a session."""
        payload = await self._async_request("auth.getSession", {"token": token})
        session = payload.get("session")
        if not isinstance(session, dict):
            _LOGGER.warning("Last.fm auth response rejected: session_object")
            _raise_unexpected()
        name = session.get("name")
        key = session.get("key")
        if not _is_account_name(name) or not _is_bounded_text(key):
            _LOGGER.warning("Last.fm auth response rejected: session_fields")
            _raise_unexpected()
        return LastFmSession(account_name=name, session_key=key)

    def authorization_url(self, token: str) -> str:
        """Return the page where the user approves the token."""
        query = urlencode({"api_key": self._credentials.api_key, "token": token})
        return f"{AUTH_ROOT}?{query}"

    async def _async_request(
        self, method: _Method, params: Mapping[str, str]
    ) -> dict[str, Any]:
        signed = {"method": method, "api_key": self._credentials.api_key, **params}
        form = {
            **signed,
            "api_sig": sign_request(signed, self._credentials.shared_secret),
            "format": "json",
        }
        exchange = await self._async_post(form)
        if exchange is None:
            raise LastFmError(LastFmErrorCode.PROVIDER_UNAVAILABLE)
        status, body = exchange
        payload = _parse_object(body)
        # An API error wins: its HTTP status is undocumented, and a retryable 14
        # must not depend on it. Any other error shape falls back to the status.
        if payload is not None and _is_api_code(code := payload.get("error")):
            error = _API_ERRORS[method].get(code, LastFmErrorCode.UNEXPECTED_RESPONSE)
            if error is LastFmErrorCode.UNEXPECTED_RESPONSE:
                _LOGGER.warning(
                    "Last.fm auth response rejected: api_error "
                    "(http_status=%d, api_code=%s)",
                    status,
                    code if 0 <= code <= 999 else "outside_range",
                )
            raise LastFmError(error)
        if status != 200 or payload is None:
            if body is not None:
                reason = "http_status" if status != 200 else "json_object"
                _LOGGER.warning(
                    "Last.fm auth response rejected: %s (http_status=%d)",
                    reason,
                    status,
                )
            raise LastFmError(_status_error(status))
        if "error" in payload:
            _LOGGER.warning("Last.fm auth response rejected: api_error_shape")
            _raise_unexpected()
        return payload

    async def _async_post(
        self, form: Mapping[str, str]
    ) -> tuple[int, bytes | None] | None:
        """Return the status and bounded body, or None when the transport failed."""
        try:
            async with (
                asyncio.timeout(REQUEST_DEADLINE_SECONDS),
                self._session.post(
                    API_ROOT,
                    data=form,
                    headers={aiohttp.hdrs.ACCEPT_ENCODING: "identity"},
                    allow_redirects=False,
                    auto_decompress=False,
                ) as response,
            ):
                return response.status, await read_bounded(
                    response, MAX_RESPONSE_BYTES, "auth"
                )
        except aiohttp.ClientError, TimeoutError:
            return None


async def read_bounded(
    response: aiohttp.ClientResponse, limit: int, context: str
) -> bytes | None:
    """Return the body, or None when it is a redirect, encoded or too large.

    The context names the caller in the fixed log line and nothing else.
    """
    encodings = response.headers.getall(aiohttp.hdrs.CONTENT_ENCODING, [])
    if response.status in range(300, 400):
        _LOGGER.warning(
            "Last.fm %s response rejected: redirect (http_status=%d)",
            context,
            response.status,
        )
        return None
    if any(encoding.casefold() != "identity" for encoding in encodings):
        _LOGGER.warning("Last.fm %s response rejected: content_encoding", context)
        return None
    body = bytearray()
    async for chunk in response.content.iter_chunked(_READ_CHUNK_BYTES):
        body += chunk
        if len(body) > limit:
            _LOGGER.warning(
                "Last.fm %s response rejected: response_size_limit", context
            )
            return None
    return bytes(body)


def _parse_object(body: bytes | None) -> dict[str, Any] | None:
    if body is None:
        return None
    try:
        decoded = json.loads(body.decode(), parse_constant=_reject_constant)
    except ValueError, RecursionError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _reject_constant(constant: str) -> NoReturn:
    raise ValueError


def _is_api_code(value: object) -> TypeIs[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _status_error(status: int) -> LastFmErrorCode:
    if status == 429:
        return LastFmErrorCode.RATE_LIMITED
    if status >= 500:
        return LastFmErrorCode.PROVIDER_UNAVAILABLE
    return LastFmErrorCode.UNEXPECTED_RESPONSE


def _is_bounded_text(value: object) -> TypeIs[str]:
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_TEXT_LENGTH
        and value.isprintable()
    )


def _is_account_name(value: object) -> TypeIs[str]:
    """Refuse backticks: the confirmation step shows the name in a code span."""
    return _is_bounded_text(value) and "`" not in value


def _raise_unexpected() -> NoReturn:
    raise LastFmError(LastFmErrorCode.UNEXPECTED_RESPONSE)
