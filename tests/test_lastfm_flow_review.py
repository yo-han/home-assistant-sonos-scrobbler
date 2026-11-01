"""A stale linking screen must stop before making a provider request."""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests.helpers import (
    CREDENTIALS,
    choose,
    link_account,
    reach_authorize,
    start_options,
)
from tests.lastfm_server import LastFmServer


@pytest.mark.parametrize("old_step", ["credentials", "authorize"])
async def test_disconnect_blocks_new_requests_from_a_stale_screen(
    hass: HomeAssistant,
    loaded_pilot: MockConfigEntry,
    lastfm_server: LastFmServer,
    old_step: str,
) -> None:
    await link_account(hass, loaded_pilot)
    if old_step == "credentials":
        old = await choose(hass, await start_options(hass, loaded_pilot), "replace")
        submission = dict(CREDENTIALS)
    else:
        old = await reach_authorize(hass, loaded_pilot, "replace")
        submission = {}
    disconnect = await choose(
        hass, await start_options(hass, loaded_pilot), "disconnect"
    )
    result = await hass.config_entries.options.async_configure(
        disconnect["flow_id"], {}
    )
    assert result["reason"] == "disconnected"
    request_count = len(lastfm_server.requests)

    result = await hass.config_entries.options.async_configure(
        old["flow_id"], submission
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "stale_link_flow"
    assert len(lastfm_server.requests) == request_count
