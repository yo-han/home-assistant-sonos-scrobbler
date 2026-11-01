"""The options flow links, replaces and disconnects one Last.fm account."""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.personal_sonos_scrobbler.const import (
    CONF_DELIVERY_EPOCH,
    CONF_LASTFM,
    CONF_LASTFM_LINK_REVISION,
    CONF_NOW_PLAYING_EPOCH,
    DOMAIN,
)
from custom_components.personal_sonos_scrobbler.lastfm import LastFmErrorCode
from custom_components.personal_sonos_scrobbler.lastfm_flow import (
    LINK_TTL_SECONDS,
    LinkAbort,
)
from tests.helpers import (
    CREDENTIALS,
    Advance,
    choose,
    link_account,
    reach_authorize,
    reach_confirm,
    start_options,
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
COMPONENT = Path(__file__).parents[1] / "custom_components" / DOMAIN
SOURCES = {
    "strings": COMPONENT / "strings.json",
    "en": COMPONENT / "translations/en.json",
    "nl": COMPONENT / "translations/nl.json",
}
STORED_LINK = {
    "api_key": API_KEY,
    "shared_secret": SHARED_SECRET,
    "session_key": SESSION_KEY,
    "account_name": ACCOUNT_NAME,
}

pytestmark = pytest.mark.usefixtures("lastfm_server")


async def test_menu_offers_connect_when_unlinked(
    hass: HomeAssistant, loaded_pilot: MockConfigEntry
) -> None:
    result = await start_options(hass, loaded_pilot)

    assert result["menu_options"] == ["connect"]


async def test_opening_the_flow_makes_no_request(
    hass: HomeAssistant, loaded_pilot: MockConfigEntry, lastfm_server: LastFmServer
) -> None:
    result = await choose(hass, await start_options(hass, loaded_pilot), "connect")

    assert result["step_id"] == "credentials"
    assert lastfm_server.requests == []


@pytest.mark.parametrize("request_token", [TOKEN, "ghijklmnopqrstuvwxyz0123456789AB"])
async def test_connect_confirm_persists_all_fields_in_data(
    hass: HomeAssistant,
    loaded_pilot: MockConfigEntry,
    lastfm_server: LastFmServer,
    request_token: str,
) -> None:
    lastfm_server.queue(GET_TOKEN, token_reply(request_token))
    base = dict(loaded_pilot.data)

    result = await reach_confirm(hass, loaded_pilot)
    assert result["type"] is FlowResultType.MENU
    assert result["description_placeholders"] == {"account_name": ACCOUNT_NAME}
    assert CONF_LASTFM not in loaded_pilot.data

    result = await choose(hass, result, "confirm_link")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == LinkAbort.LINKED
    assert loaded_pilot.data == {
        **base,
        CONF_DELIVERY_EPOCH: 1,
        CONF_NOW_PLAYING_EPOCH: 1,
        CONF_LASTFM: STORED_LINK,
        CONF_LASTFM_LINK_REVISION: 1,
    }
    assert loaded_pilot.options == {}
    assert lastfm_server.calls(GET_SESSION)[0].form["token"] == request_token


async def test_authorize_step_shows_the_fixed_link_without_a_request(
    hass: HomeAssistant, loaded_pilot: MockConfigEntry, lastfm_server: LastFmServer
) -> None:
    result = await reach_authorize(hass, loaded_pilot)

    assert result["description_placeholders"] == {
        "auth_url": f"https://www.last.fm/api/auth/?api_key={API_KEY}&token={TOKEN}"
    }
    assert lastfm_server.calls(GET_SESSION) == []


async def test_not_authorized_stays_retryable_with_the_same_token(
    hass: HomeAssistant, loaded_pilot: MockConfigEntry, lastfm_server: LastFmServer
) -> None:
    unapproved = json_reply({"error": 14, "message": "Unauthorized Token"})
    lastfm_server.queue(GET_SESSION, unapproved, unapproved)
    result = await reach_authorize(hass, loaded_pilot)
    flow_id = result["flow_id"]

    for _ in range(2):
        result = await hass.config_entries.options.async_configure(flow_id, {})
        assert result["step_id"] == "authorize"
        assert result["errors"] == {"base": "token_not_authorized"}

    result = await hass.config_entries.options.async_configure(flow_id, {})

    assert result["step_id"] == "confirm"
    assert len(lastfm_server.calls(GET_TOKEN)) == 1
    assert {call.form["token"] for call in lastfm_server.calls(GET_SESSION)} == {TOKEN}


@pytest.mark.parametrize("step", ["authorize", "confirm"])
async def test_local_expiry_aborts_without_a_request(
    hass: HomeAssistant,
    loaded_pilot: MockConfigEntry,
    lastfm_server: LastFmServer,
    freezer: FrozenDateTimeFactory,
    step: str,
) -> None:
    if step == "authorize":
        result = await reach_authorize(hass, loaded_pilot)
        submit = {}
    else:
        result = await reach_confirm(hass, loaded_pilot)
        submit = {"next_step_id": "confirm_link"}
    requests = len(lastfm_server.requests)
    freezer.tick(timedelta(seconds=LINK_TTL_SECONDS + 1))

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], submit
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == LinkAbort.LINK_EXPIRED
    assert len(lastfm_server.requests) == requests
    assert CONF_LASTFM not in loaded_pilot.data


@pytest.mark.parametrize("step", ["authorize", "confirm"])
async def test_abandoned_attempt_forgets_pending_values_at_the_deadline(
    hass: HomeAssistant,
    loaded_pilot: MockConfigEntry,
    lastfm_server: LastFmServer,
    advance: Advance,
    step: str,
) -> None:
    if step == "authorize":
        result = await reach_authorize(hass, loaded_pilot)
        submit = {}
    else:
        result = await reach_confirm(hass, loaded_pilot)
        submit = {"next_step_id": "confirm_link"}
    handler = hass.config_entries.options._progress[result["flow_id"]]
    requests = len(lastfm_server.requests)

    await advance(LINK_TTL_SECONDS - 1)
    assert handler._attempt is not None  # type: ignore[attr-defined]
    await advance(1)

    assert handler._attempt is None  # type: ignore[attr-defined]
    for pending in (TOKEN, API_KEY, ACCOUNT_NAME):
        assert pending not in repr(handler.cur_step)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], submit
    )
    assert result["reason"] == LinkAbort.LINK_EXPIRED
    assert len(lastfm_server.requests) == requests
    assert CONF_LASTFM not in loaded_pilot.data


async def test_ending_the_flow_cancels_the_deadline(
    hass: HomeAssistant, loaded_pilot: MockConfigEntry
) -> None:
    result = await reach_authorize(hass, loaded_pilot)
    assert _scheduled_deadlines(hass) == 1

    hass.config_entries.options.async_abort(result["flow_id"])

    assert _scheduled_deadlines(hass) == 0


def _scheduled_deadlines(hass: HomeAssistant) -> int:
    timers = hass.loop._scheduled  # type: ignore[attr-defined]
    return sum(
        not timer.cancelled() and "_async_expire" in repr(timer._args)
        for timer in timers
    )


@pytest.mark.parametrize(
    ("api_code", "error"),
    [
        (10, "invalid_credentials"),
        (26, "api_key_suspended"),
        (29, "rate_limited"),
        (11, "provider_unavailable"),
        (999, "unexpected_response"),
    ],
)
async def test_token_failure_returns_to_the_form(
    hass: HomeAssistant,
    loaded_pilot: MockConfigEntry,
    lastfm_server: LastFmServer,
    api_code: int,
    error: str,
) -> None:
    lastfm_server.queue(GET_TOKEN, json_reply({"error": api_code}))
    result = await choose(hass, await start_options(hass, loaded_pilot), "connect")

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], dict(CREDENTIALS)
    )

    assert result["step_id"] == "credentials"
    assert result["errors"] == {"base": error}


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (json_reply({"error": 29}), "rate_limited"),
        (Reply(status=429), "rate_limited"),
        (json_reply({"error": 11}), "provider_unavailable"),
        (Reply(status=503), "provider_unavailable"),
    ],
)
async def test_transient_session_failure_keeps_the_token(
    hass: HomeAssistant,
    loaded_pilot: MockConfigEntry,
    lastfm_server: LastFmServer,
    reply: Reply,
    error: str,
) -> None:
    lastfm_server.queue(GET_SESSION, reply)
    result = await reach_authorize(hass, loaded_pilot)

    result = await hass.config_entries.options.async_configure(result["flow_id"], {})

    assert result["step_id"] == "authorize"
    assert result["errors"] == {"base": error}
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == "confirm"


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (json_reply({"error": 15}), LinkAbort.TOKEN_INVALID),
        (json_reply({"error": 4}), LinkAbort.TOKEN_INVALID),
        (json_reply({"error": 13}), LinkAbort.LINK_FAILED),
        (json_reply({"error": 26}), LinkAbort.LINK_FAILED),
        (json_reply({"error": 999}), LinkAbort.LINK_FAILED),
        (Reply(body=b"garbage"), LinkAbort.LINK_FAILED),
        (session_reply(name="line\nbreak"), LinkAbort.LINK_FAILED),
    ],
)
async def test_final_session_failure_aborts_without_persisting(
    hass: HomeAssistant,
    loaded_pilot: MockConfigEntry,
    lastfm_server: LastFmServer,
    reply: Reply,
    reason: LinkAbort,
) -> None:
    lastfm_server.queue(GET_SESSION, reply)
    result = await reach_authorize(hass, loaded_pilot)

    result = await hass.config_entries.options.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason
    assert CONF_LASTFM not in loaded_pilot.data


async def test_wrong_account_cancel_keeps_the_existing_account(
    hass: HomeAssistant, loaded_pilot: MockConfigEntry, lastfm_server: LastFmServer
) -> None:
    await link_account(hass, loaded_pilot)
    before = dict(loaded_pilot.data)
    lastfm_server.queue(GET_SESSION, session_reply(name="other_account"))

    result = await reach_confirm(hass, loaded_pilot, "replace")
    assert result["description_placeholders"] == {"account_name": "other_account"}
    result = await choose(hass, result, "cancel_link")

    assert result["reason"] == LinkAbort.LINK_CANCELLED
    assert loaded_pilot.data == before


async def test_replace_swaps_the_account_and_advances_the_revision(
    hass: HomeAssistant, loaded_pilot: MockConfigEntry, lastfm_server: LastFmServer
) -> None:
    await link_account(hass, loaded_pilot)
    lastfm_server.queue(GET_SESSION, session_reply(name="next_account"))

    result = await link_account(hass, loaded_pilot, "replace")

    assert result["reason"] == LinkAbort.LINKED
    assert loaded_pilot.data[CONF_LASTFM]["account_name"] == "next_account"
    assert loaded_pilot.data[CONF_LASTFM_LINK_REVISION] == 2


async def test_replace_shows_no_stored_value(
    hass: HomeAssistant, loaded_pilot: MockConfigEntry
) -> None:
    await link_account(hass, loaded_pilot)

    menu = await start_options(hass, loaded_pilot)
    result = await choose(hass, menu, "replace")

    assert menu["menu_options"] == ["replace", "disconnect"]
    schema = result["data_schema"]
    assert schema is not None
    assert all(
        not callable(getattr(key, "default", None))
        and not getattr(key, "description", None)
        for key in schema.schema
    )
    assert API_KEY not in str(result)
    assert SHARED_SECRET not in str(result)


async def test_disconnect_removes_every_credential_and_keeps_the_revision(
    hass: HomeAssistant, loaded_pilot: MockConfigEntry
) -> None:
    base = dict(loaded_pilot.data)
    await link_account(hass, loaded_pilot)

    result = await choose(hass, await start_options(hass, loaded_pilot), "disconnect")
    assert result["step_id"] == "disconnect"
    assert CONF_LASTFM in loaded_pilot.data
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})

    assert result["reason"] == LinkAbort.DISCONNECTED
    assert loaded_pilot.data == {
        **base,
        CONF_DELIVERY_EPOCH: 2,
        CONF_NOW_PLAYING_EPOCH: 2,
        CONF_LASTFM_LINK_REVISION: 2,
    }


@pytest.mark.parametrize(
    ("submitted", "field", "error"),
    [
        ({"api_key": "short"}, "api_key", "invalid_api_key"),
        ({"api_key": "g" * 32}, "api_key", "invalid_api_key"),
        ({"shared_secret": ""}, "shared_secret", "invalid_shared_secret"),
        ({"shared_secret": "x" * 129}, "shared_secret", "invalid_shared_secret"),
        ({"shared_secret": "a\nb"}, "shared_secret", "invalid_shared_secret"),
    ],
)
async def test_malformed_credentials_send_no_request(
    hass: HomeAssistant,
    loaded_pilot: MockConfigEntry,
    lastfm_server: LastFmServer,
    submitted: dict[str, str],
    field: str,
    error: str,
) -> None:
    result = await choose(hass, await start_options(hass, loaded_pilot), "connect")

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**CREDENTIALS, **submitted}
    )

    assert result["step_id"] == "credentials"
    assert result["errors"] == {field: error}
    assert lastfm_server.requests == []


async def test_non_string_input_is_rejected_without_echoing_it(
    hass: HomeAssistant, loaded_pilot: MockConfigEntry, lastfm_server: LastFmServer
) -> None:
    result = await choose(hass, await start_options(hass, loaded_pilot), "connect")

    with pytest.raises(InvalidData) as caught:
        await hass.config_entries.options.async_configure(
            result["flow_id"], {**CREDENTIALS, "shared_secret": 73317331}
        )

    assert "73317331" not in f"{caught.value!s} {caught.value.schema_errors}"
    assert lastfm_server.requests == []


async def test_pasted_whitespace_is_stripped_from_the_api_key_only(
    hass: HomeAssistant, loaded_pilot: MockConfigEntry, lastfm_server: LastFmServer
) -> None:
    result = await choose(hass, await start_options(hass, loaded_pilot), "connect")
    secret = "  spaces are part of an opaque secret  "

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"api_key": f" {API_KEY}\n", "shared_secret": secret}
    )
    assert result["step_id"] == "authorize"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await choose(hass, result, "confirm_link")

    assert lastfm_server.requests[0].form["api_key"] == API_KEY
    assert loaded_pilot.data[CONF_LASTFM]["api_key"] == API_KEY
    assert loaded_pilot.data[CONF_LASTFM]["shared_secret"] == secret


@pytest.mark.parametrize("name", list(SOURCES))
def test_every_error_and_abort_reason_is_translated(name: str) -> None:
    options = json.loads(SOURCES[name].read_text())["options"]

    assert set(options["abort"]) == {reason.value for reason in LinkAbort}
    assert set(options["error"]) == {
        *(code.value for code in LastFmErrorCode),
        "invalid_api_key",
        "invalid_shared_secret",
    }
    assert set(options["step"]) == {
        "init",
        "credentials",
        "authorize",
        "confirm",
        "disconnect",
        "clear_queue",
    }


@pytest.mark.parametrize("name", list(SOURCES))
def test_translations_render_provider_text_inertly(name: str) -> None:
    steps = json.loads(SOURCES[name].read_text())["options"]["step"]

    assert "`{account_name}`" in steps["confirm"]["description"]
    assert "{auth_url}" in steps["authorize"]["description"]
    assert "http" not in steps["authorize"]["description"]


def test_english_strings_match_the_source_file() -> None:
    assert SOURCES["strings"].read_text() == SOURCES["en"].read_text()
