"""Options flow that links one Last.fm account; it never sends listening data.

Pending values live only on the flow instance and are dropped at a local deadline,
even if the user walks away, because Home Assistant keeps abandoned flows. Every
point after an await re-checks the entry and the link revision, because the flow, the
entry or another flow's commit can change while a request is on the wire.
"""

import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, override

import probatio
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_API_KEY
from homeassistant.core import CALLBACK_TYPE, HassJob, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later

from .const import (
    CONF_ACCOUNT_NAME,
    CONF_DELIVERY_EPOCH,
    CONF_LASTFM,
    CONF_LASTFM_LINK_REVISION,
    CONF_OUTBOX_CREATED,
    CONF_SESSION_KEY,
    CONF_SHARED_SECRET,
    SIGNAL_LASTFM_LINK_CHANGED,
)
from .controls import all_features_off
from .delivery import delivery_service
from .lastfm import (
    LastFmClient,
    LastFmCredentials,
    LastFmError,
    LastFmErrorCode,
    LastFmSession,
    is_valid_api_key,
    is_valid_shared_secret,
)
from .outbox import OutboxError

LINK_TTL_SECONDS = 600.0

_RETRYABLE_SESSION_ERRORS = frozenset(
    {
        LastFmErrorCode.TOKEN_NOT_AUTHORIZED,
        LastFmErrorCode.RATE_LIMITED,
        LastFmErrorCode.PROVIDER_UNAVAILABLE,
    }
)
_SECRET_TEXT = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)
_CREDENTIALS_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_API_KEY): _SECRET_TEXT,
        probatio.Required(CONF_SHARED_SECRET): _SECRET_TEXT,
    }
)


class LinkAbort(StrEnum):
    """Every way this flow ends."""

    LINKED = "linked"
    DISCONNECTED = "disconnected"
    LINK_CANCELLED = "link_cancelled"
    LINK_EXPIRED = "link_expired"
    TOKEN_INVALID = "token_invalid"
    STALE_LINK_FLOW = "stale_link_flow"
    ENTRY_REMOVED = "entry_removed"
    LINK_FAILED = "link_failed"
    QUEUE_CLEARED = "queue_cleared"
    STALE_DELIVERY_FLOW = "stale_delivery_flow"
    STORAGE_ERROR = "storage_error"


@dataclass(repr=False, slots=True)
class _LinkAttempt:
    """One authorization in progress; the deadline runs until confirmation."""

    credentials: LastFmCredentials
    token: str
    authorization_url: str
    expires_at: float
    session: LastFmSession | None = None

    def is_expired(self) -> bool:
        """Return whether the local deadline passed."""
        return time.monotonic() >= self.expires_at


def _link_revision(entry: ConfigEntry) -> int:
    revision: int = entry.data.get(CONF_LASTFM_LINK_REVISION, 0)
    return revision


def _delivery_epoch(entry: ConfigEntry) -> int:
    epoch: int = entry.data.get(CONF_DELIVERY_EPOCH, 0)
    return epoch


class LastFmOptionsFlow(OptionsFlow):
    """Link an account and clear the queue; only a submit reaches the network.

    The switches own what is sent. A clear is fenced by the persisted delivery epoch:
    a flow only commits if the epoch it saw when it opened is still the live one.
    """

    def __init__(self) -> None:
        """Start without pending state."""
        self._revision = 0
        self._epoch = 0
        self._attempt: _LinkAttempt | None = None
        self._cancel_expiry: CALLBACK_TYPE | None = None
        self._closed = False

    @callback
    @override
    def async_remove(self) -> None:
        """Forget pending secrets, however the flow ended."""
        self._async_forget_attempt()
        self._closed = True

    async def async_step_init(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Snapshot the link revision and offer the choices for the current state."""
        entry = self.hass.config_entries.async_get_entry(self.handler)
        if entry is None:
            return self.async_abort(reason=LinkAbort.ENTRY_REMOVED)
        self._revision = _link_revision(entry)
        self._epoch = _delivery_epoch(entry)
        options = ["connect"]
        if CONF_LASTFM in entry.data:
            options = ["replace", "disconnect"]
        if entry.data.get(CONF_OUTBOX_CREATED):
            options.append("clear_queue")
        return self.async_show_menu(step_id="init", menu_options=options)

    async def async_step_connect(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Link a first account."""
        return await self.async_step_credentials()

    async def async_step_replace(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Link another account; the current one stays until confirmation."""
        return await self.async_step_credentials()

    async def async_step_credentials(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the user's own API application and create a token."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if (stale := self._stale_abort()) is not None:
                return stale
            # The key has a documented form; the secret is opaque and kept verbatim.
            api_key = user_input[CONF_API_KEY].strip()
            shared_secret = user_input[CONF_SHARED_SECRET]
            if not is_valid_api_key(api_key):
                errors[CONF_API_KEY] = "invalid_api_key"
            elif not is_valid_shared_secret(shared_secret):
                errors[CONF_SHARED_SECRET] = "invalid_shared_secret"
            else:
                credentials = LastFmCredentials(api_key, shared_secret)
                result = await self._async_start_attempt(credentials)
                if isinstance(result, LastFmErrorCode):
                    errors["base"] = result
                else:
                    return result
        return self.async_show_form(
            step_id="credentials", data_schema=_CREDENTIALS_SCHEMA, errors=errors
        )

    async def async_step_authorize(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the link; a submit exchanges the token, and may be repeated."""
        attempt = self._attempt
        if attempt is None or attempt.is_expired():
            return self.async_abort(reason=LinkAbort.LINK_EXPIRED)

        errors: dict[str, str] = {}
        if user_input is not None:
            if (stale := self._stale_abort()) is not None:
                return stale
            client = self._client(attempt.credentials)
            error: LastFmErrorCode | None = None
            try:
                attempt.session = await client.async_create_session(attempt.token)
            except LastFmError as failure:
                error = failure.code
            if (stale := self._stale_abort()) is not None:
                return stale
            if self._attempt is not attempt:
                return self.async_abort(reason=LinkAbort.LINK_EXPIRED)
            if error is None:
                return await self.async_step_confirm()
            if error is LastFmErrorCode.TOKEN_INVALID:
                return self.async_abort(reason=LinkAbort.TOKEN_INVALID)
            if error not in _RETRYABLE_SESSION_ERRORS:
                return self.async_abort(reason=LinkAbort.LINK_FAILED)
            errors["base"] = error

        return self.async_show_form(
            step_id="authorize",
            data_schema=probatio.Schema({}),
            errors=errors,
            description_placeholders={"auth_url": attempt.authorization_url},
        )

    async def async_step_confirm(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show who authorized, so that a wrong account is not linked silently."""
        attempt = self._attempt
        if attempt is None or attempt.session is None:
            return self.async_abort(reason=LinkAbort.LINK_EXPIRED)
        return self.async_show_menu(
            step_id="confirm",
            menu_options=["confirm_link", "cancel_link"],
            description_placeholders={CONF_ACCOUNT_NAME: attempt.session.account_name},
        )

    async def async_step_confirm_link(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Persist the confirmed account."""
        attempt = self._attempt
        if attempt is None or attempt.session is None or attempt.is_expired():
            return self.async_abort(reason=LinkAbort.LINK_EXPIRED)
        result = self._async_commit(
            {
                CONF_API_KEY: attempt.credentials.api_key,
                CONF_SHARED_SECRET: attempt.credentials.shared_secret,
                CONF_SESSION_KEY: attempt.session.session_key,
                CONF_ACCOUNT_NAME: attempt.session.account_name,
            },
            LinkAbort.LINKED,
        )
        return await self._async_once_durable(result, LinkAbort.LINKED)

    async def async_step_cancel_link(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Drop the attempt; a current account stays as it is."""
        return self.async_abort(reason=LinkAbort.LINK_CANCELLED)

    async def async_step_disconnect(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Explain that this is local only; a submit removes the saved account."""
        if user_input is None:
            return self.async_show_form(
                step_id="disconnect", data_schema=probatio.Schema({})
            )
        result = self._async_commit(None, LinkAbort.DISCONNECTED)
        return await self._async_once_durable(result, LinkAbort.DISCONNECTED)

    async def async_step_clear_queue(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Explain that queued plays are discarded; a submit asks the outbox to."""
        if user_input is None:
            return self.async_show_form(
                step_id="clear_queue", data_schema=probatio.Schema({})
            )
        if (stale := self._delivery_stale_abort()) is not None:
            return stale
        service = delivery_service(self.hass, self.handler)
        if service is None:
            return self.async_abort(reason=LinkAbort.STORAGE_ERROR)
        try:
            await service.async_clear_queue()
        except OutboxError:
            return self.async_abort(reason=LinkAbort.STORAGE_ERROR)
        return self.async_abort(reason=LinkAbort.QUEUE_CLEARED)

    async def _async_start_attempt(
        self, credentials: LastFmCredentials
    ) -> ConfigFlowResult | LastFmErrorCode:
        client = self._client(credentials)
        token: str | None = None
        code: LastFmErrorCode | None = None
        try:
            token = await client.async_create_token()
        except LastFmError as failure:
            code = failure.code
        if (stale := self._stale_abort()) is not None:
            return stale
        if token is None:
            assert code is not None
            return code
        self._async_forget_attempt()
        self._attempt = _LinkAttempt(
            credentials=credentials,
            token=token,
            authorization_url=client.authorization_url(token),
            expires_at=time.monotonic() + LINK_TTL_SECONDS,
        )
        # Stopping Home Assistant does not end options flows, so it cancels this itself.
        self._cancel_expiry = async_call_later(
            self.hass,
            LINK_TTL_SECONDS,
            HassJob(self._async_expire, cancel_on_shutdown=True),
        )
        return await self.async_step_authorize()

    @callback
    def _async_forget_attempt(self) -> None:
        self._attempt = None
        if self._cancel_expiry is not None:
            self._cancel_expiry()
            self._cancel_expiry = None

    @callback
    def _async_expire(self, _now: datetime) -> None:
        """Drop pending values, including those in the step shown last."""
        self._cancel_expiry = None
        self._attempt = None
        if self.cur_step is not None:
            self.cur_step["description_placeholders"] = None

    def _client(self, credentials: LastFmCredentials) -> LastFmClient:
        return LastFmClient(async_get_clientsession(self.hass), credentials)

    @callback
    def _stale_abort(self) -> ConfigFlowResult | None:
        """Return the abort for a flow that may no longer act, if it is one."""
        entry = self.hass.config_entries.async_get_entry(self.handler)
        if entry is None:
            return self.async_abort(reason=LinkAbort.ENTRY_REMOVED)
        if self._closed:
            return self.async_abort(reason=LinkAbort.LINK_CANCELLED)
        if _link_revision(entry) != self._revision:
            return self.async_abort(reason=LinkAbort.STALE_LINK_FLOW)
        return None

    @callback
    def _async_commit(
        self, link: Mapping[str, str] | None, outcome: LinkAbort
    ) -> ConfigFlowResult:
        """Apply a link change; being synchronous, nothing interleaves the fence."""
        if (stale := self._stale_abort()) is not None:
            return stale
        entry = self.hass.config_entries.async_get_known_entry(self.handler)
        data = {key: value for key, value in entry.data.items() if key != CONF_LASTFM}
        if link is not None:
            data[CONF_LASTFM] = dict(link)
        data[CONF_LASTFM_LINK_REVISION] = _link_revision(entry) + 1
        # Any link change switches both features off in the same write: linking
        # never activates sending, and each needs its switch again.
        self.hass.config_entries.async_update_entry(entry, data=all_features_off(data))
        async_dispatcher_send(
            self.hass, SIGNAL_LASTFM_LINK_CHANGED.format(entry.entry_id)
        )
        return self.async_abort(reason=outcome)

    @callback
    def _delivery_stale_abort(self) -> ConfigFlowResult | None:
        """Return the abort unless this flow still saw the live delivery epoch."""
        if (stale := self._stale_abort()) is not None:
            return stale
        entry = self.hass.config_entries.async_get_known_entry(self.handler)
        if _delivery_epoch(entry) != self._epoch:
            return self.async_abort(reason=LinkAbort.STALE_DELIVERY_FLOW)
        return None

    async def _async_once_durable(
        self, result: ConfigFlowResult, outcome: LinkAbort
    ) -> ConfigFlowResult:
        """Hold a success back until the ledger has the new epoch.

        The commit fenced this process at once, but Home Assistant saves entries
        later: only the ledger makes the change survive a crash.
        """
        if result.get("reason") != outcome:
            return result
        entry = self.hass.config_entries.async_get_entry(self.handler)
        if entry is None or not entry.data.get(CONF_OUTBOX_CREATED):
            return result
        service = delivery_service(self.hass, self.handler)
        if service is None:
            return self.async_abort(reason=LinkAbort.STORAGE_ERROR)
        try:
            await service.async_settle()
        except OutboxError:
            return self.async_abort(reason=LinkAbort.STORAGE_ERROR)
        return result
