"""Stale flows, lifecycle changes and in-flight requests cannot corrupt the link."""

import asyncio
import json
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.const import CONF_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    flush_store,
)

from custom_components.personal_sonos_scrobbler.const import (
    CONF_ENTITY_REGISTRY_IDS,
    CONF_LASTFM,
    CONF_LASTFM_LINK_REVISION,
    CONFIG_MINOR_VERSION,
    CONFIG_VERSION,
    DOMAIN,
)
from custom_components.personal_sonos_scrobbler.lastfm_flow import (
    LINK_TTL_SECONDS,
    LinkAbort,
)
from tests.helpers import (
    CREDENTIALS,
    Advance,
    choose,
    entry_data,
    link_account,
    reach_authorize,
    reach_confirm,
    start_options,
)
from tests.lastfm_server import (
    SESSION_KEY,
    SHARED_SECRET,
    TOKEN,
    LastFmServer,
    json_reply,
    session_reply,
)

GET_TOKEN = "auth.getToken"
GET_SESSION = "auth.getSession"

pytestmark = pytest.mark.usefixtures("lastfm_server")


@pytest.fixture
async def running_pilot(
    hass: HomeAssistant, pilot_entry: MockConfigEntry
) -> MockConfigEntry:
    """Set up a pilot that a test may remove; teardown does not unload it."""
    assert await hass.config_entries.async_setup(pilot_entry.entry_id)
    await hass.async_block_till_done()
    return pilot_entry


async def submit_held(
    hass: HomeAssistant,
    server: LastFmServer,
    result: ConfigFlowResult,
    method: str,
    user_input: dict[str, Any],
) -> asyncio.Task[ConfigFlowResult]:
    """Submit a step and return once its request waits on the server."""
    server.hold(method)
    task = asyncio.create_task(
        hass.config_entries.options.async_configure(result["flow_id"], user_input)
    )
    await server.held.wait()
    return task


def account(entry: ConfigEntry) -> str:
    return entry.data[CONF_LASTFM]["account_name"]


async def test_first_of_two_overlapping_flows_wins(
    hass: HomeAssistant, running_pilot: MockConfigEntry, lastfm_server: LastFmServer
) -> None:
    lastfm_server.queue(
        GET_SESSION, session_reply(name="account_a"), session_reply(name="account_b")
    )
    first = await reach_confirm(hass, running_pilot)
    second = await reach_confirm(hass, running_pilot)

    assert (await choose(hass, first, "confirm_link"))["reason"] == LinkAbort.LINKED
    result = await choose(hass, second, "confirm_link")

    assert result["reason"] == LinkAbort.STALE_LINK_FLOW
    assert account(running_pilot) == "account_a"
    assert running_pilot.data[CONF_LASTFM_LINK_REVISION] == 1


async def test_disconnect_during_exchange_cannot_recreate_credentials(
    hass: HomeAssistant, running_pilot: MockConfigEntry, lastfm_server: LastFmServer
) -> None:
    await link_account(hass, running_pilot)
    authorize = await reach_authorize(hass, running_pilot, "replace")
    task = await submit_held(hass, lastfm_server, authorize, GET_SESSION, {})

    disconnect = await choose(
        hass, await start_options(hass, running_pilot), "disconnect"
    )
    await hass.config_entries.options.async_configure(disconnect["flow_id"], {})
    lastfm_server.release(GET_SESSION)
    result = await task

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == LinkAbort.STALE_LINK_FLOW
    assert CONF_LASTFM not in running_pilot.data


async def test_old_flow_cannot_replace_a_newer_account(
    hass: HomeAssistant, running_pilot: MockConfigEntry, lastfm_server: LastFmServer
) -> None:
    await link_account(hass, running_pilot)
    lastfm_server.queue(
        GET_SESSION, session_reply(name="older"), session_reply(name="newer")
    )
    old = await reach_confirm(hass, running_pilot, "replace")

    await link_account(hass, running_pilot, "replace")
    result = await choose(hass, old, "confirm_link")

    assert result["reason"] == LinkAbort.STALE_LINK_FLOW
    assert account(running_pilot) == "newer"


async def test_old_disconnect_screen_cannot_remove_a_newer_account(
    hass: HomeAssistant, running_pilot: MockConfigEntry, lastfm_server: LastFmServer
) -> None:
    await link_account(hass, running_pilot)
    old = await choose(hass, await start_options(hass, running_pilot), "disconnect")
    lastfm_server.queue(GET_SESSION, session_reply(name="newer"))
    await link_account(hass, running_pilot, "replace")

    result = await hass.config_entries.options.async_configure(old["flow_id"], {})

    assert result["reason"] == LinkAbort.STALE_LINK_FLOW
    assert account(running_pilot) == "newer"


async def test_stale_flow_spends_no_token_on_submit(
    hass: HomeAssistant, running_pilot: MockConfigEntry, lastfm_server: LastFmServer
) -> None:
    await link_account(hass, running_pilot)
    authorize = await reach_authorize(hass, running_pilot, "replace")
    sessions = len(lastfm_server.calls(GET_SESSION))
    await link_account(hass, running_pilot, "replace")

    result = await hass.config_entries.options.async_configure(authorize["flow_id"], {})

    assert result["reason"] == LinkAbort.STALE_LINK_FLOW
    assert len(lastfm_server.calls(GET_SESSION)) == sessions + 1


async def test_stale_credentials_form_requests_no_token(
    hass: HomeAssistant, running_pilot: MockConfigEntry, lastfm_server: LastFmServer
) -> None:
    form = await choose(hass, await start_options(hass, running_pilot), "connect")
    await link_account(hass, running_pilot)
    tokens = len(lastfm_server.calls(GET_TOKEN))

    result = await hass.config_entries.options.async_configure(
        form["flow_id"], dict(CREDENTIALS)
    )

    assert result["reason"] == LinkAbort.STALE_LINK_FLOW
    assert len(lastfm_server.calls(GET_TOKEN)) == tokens


async def test_entry_removed_during_exchange_aborts_cleanly(
    hass: HomeAssistant, running_pilot: MockConfigEntry, lastfm_server: LastFmServer
) -> None:
    authorize = await reach_authorize(hass, running_pilot)
    task = await submit_held(hass, lastfm_server, authorize, GET_SESSION, {})

    await hass.config_entries.async_remove(running_pilot.entry_id)
    lastfm_server.release(GET_SESSION)
    result = await task

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == LinkAbort.ENTRY_REMOVED


async def test_entry_removed_during_token_request_aborts_cleanly(
    hass: HomeAssistant, running_pilot: MockConfigEntry, lastfm_server: LastFmServer
) -> None:
    form = await choose(hass, await start_options(hass, running_pilot), "connect")
    task = await submit_held(hass, lastfm_server, form, GET_TOKEN, dict(CREDENTIALS))

    await hass.config_entries.async_remove(running_pilot.entry_id)
    lastfm_server.release(GET_TOKEN)
    result = await task

    assert result["reason"] == LinkAbort.ENTRY_REMOVED


async def test_recreated_entry_is_out_of_reach_of_the_old_flow(
    hass: HomeAssistant,
    running_pilot: MockConfigEntry,
    sonos_player: er.RegistryEntry,
    lastfm_server: LastFmServer,
) -> None:
    confirm = await reach_confirm(hass, running_pilot)
    await hass.config_entries.async_remove(running_pilot.entry_id)
    replacement = MockConfigEntry(
        domain=DOMAIN,
        version=CONFIG_VERSION,
        minor_version=CONFIG_MINOR_VERSION,
        data=entry_data(sonos_player.id),
    )
    replacement.add_to_hass(hass)

    result = await choose(hass, confirm, "confirm_link")

    assert result["reason"] == LinkAbort.ENTRY_REMOVED
    assert replacement.data == entry_data(sonos_player.id)


async def test_flow_aborted_during_exchange_keeps_nothing(
    hass: HomeAssistant, running_pilot: MockConfigEntry, lastfm_server: LastFmServer
) -> None:
    authorize = await reach_authorize(hass, running_pilot)
    flow = hass.config_entries.options.async_get(authorize["flow_id"])
    assert flow is not None
    handler = hass.config_entries.options._progress[authorize["flow_id"]]
    task = await submit_held(hass, lastfm_server, authorize, GET_SESSION, {})

    hass.config_entries.options.async_abort(authorize["flow_id"])
    lastfm_server.release(GET_SESSION)
    result = await task

    assert result["reason"] == LinkAbort.LINK_CANCELLED
    assert handler._attempt is None  # type: ignore[attr-defined]
    assert CONF_LASTFM not in running_pilot.data


async def test_deadline_during_exchange_discards_the_late_session(
    hass: HomeAssistant,
    running_pilot: MockConfigEntry,
    lastfm_server: LastFmServer,
    advance: Advance,
) -> None:
    authorize = await reach_authorize(hass, running_pilot)
    handler = hass.config_entries.options._progress[authorize["flow_id"]]
    task = await submit_held(hass, lastfm_server, authorize, GET_SESSION, {})

    await advance(LINK_TTL_SECONDS)
    assert handler._attempt is None  # type: ignore[attr-defined]
    lastfm_server.release(GET_SESSION)
    result = await task

    assert result["reason"] == LinkAbort.LINK_EXPIRED
    assert handler._attempt is None  # type: ignore[attr-defined]
    assert CONF_LASTFM not in running_pilot.data


async def test_reconfigure_during_a_link_keeps_both_changes(
    hass: HomeAssistant,
    running_pilot: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    other = entity_registry.async_get_or_create("media_player", "sonos", "RINCON_2")
    confirm = await reach_confirm(hass, running_pilot)

    reconfigure = await running_pilot.start_reconfigure_flow(hass)
    await hass.config_entries.flow.async_configure(
        reconfigure["flow_id"], {CONF_ENTITY_ID: [other.entity_id]}
    )
    await hass.async_block_till_done()
    result = await choose(hass, confirm, "confirm_link")

    assert result["reason"] == LinkAbort.LINKED
    assert running_pilot.data[CONF_ENTITY_REGISTRY_IDS] == [other.id]
    assert running_pilot.data[CONF_LASTFM]["session_key"] == SESSION_KEY


async def test_double_submit_never_half_persists(
    hass: HomeAssistant, running_pilot: MockConfigEntry, lastfm_server: LastFmServer
) -> None:
    authorize = await reach_authorize(hass, running_pilot)
    lastfm_server.queue(GET_SESSION, session_reply(), json_reply({"error": 15}))
    first = await submit_held(hass, lastfm_server, authorize, GET_SESSION, {})
    second = asyncio.create_task(
        hass.config_entries.options.async_configure(authorize["flow_id"], {})
    )
    await asyncio.sleep(0)

    lastfm_server.release(GET_SESSION)
    results = await asyncio.gather(first, second)

    assert LinkAbort.TOKEN_INVALID in {result.get("reason") for result in results}
    assert CONF_LASTFM not in running_pilot.data
    assert hass.config_entries.options.async_progress() == []


async def test_pending_values_never_reach_storage(
    hass: HomeAssistant,
    running_pilot: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    await reach_confirm(hass, running_pilot)

    hass.config_entries.async_update_entry(running_pilot, title="touched")
    await flush_store(hass.config_entries._store)

    stored = json.dumps(hass_storage)
    assert "touched" in stored
    for pending in (TOKEN, SHARED_SECRET, SESSION_KEY):
        assert pending not in stored
