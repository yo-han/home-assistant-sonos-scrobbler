"""Stored credentials never make observation talk to the provider."""

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.personal_sonos_scrobbler.const import CONF_LASTFM
from tests.helpers import Advance, choose, link_account, playing, start_options
from tests.lastfm_server import ACCOUNT_NAME, API_KEY, LastFmServer

PLAYING = {
    "source": "Spotify Connect",
    "media_artist": "Artist",
    "media_title": "Title",
    "media_duration": 200,
}
FULL_LINK = {
    "api_key": API_KEY,
    "shared_secret": "synthetic",
    "session_key": "synthetic-session",
    "account_name": ACCOUNT_NAME,
}


async def test_counter_sees_a_real_link(
    hass: HomeAssistant, loaded_pilot: MockConfigEntry, lastfm_server: LastFmServer
) -> None:
    await link_account(hass, loaded_pilot)

    assert [call.form["method"] for call in lastfm_server.requests] == [
        "auth.getToken",
        "auth.getSession",
    ]


async def test_setup_playback_reload_unload_and_stop_make_no_requests(
    hass: HomeAssistant,
    pilot_entry: MockConfigEntry,
    sonos_player: er.RegistryEntry,
    lastfm_server: LastFmServer,
    advance: Advance,
) -> None:
    hass.config_entries.async_update_entry(
        pilot_entry, data={**pilot_entry.data, CONF_LASTFM: FULL_LINK}
    )

    assert await hass.config_entries.async_setup(pilot_entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set(
        sonos_player.entity_id, "playing", playing(sonos_player.entity_id, **PLAYING)
    )
    await hass.async_block_till_done()
    for _ in range(8):
        await advance(15)
    assert pilot_entry.runtime_data.snapshot.observed_seconds > 100
    assert await hass.config_entries.async_reload(pilot_entry.entry_id)
    await hass.async_block_till_done()
    await advance(15)
    assert await hass.config_entries.async_unload(pilot_entry.entry_id)
    await hass.async_block_till_done()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    assert lastfm_server.requests == []


async def test_linking_during_playback_keeps_the_running_observation(
    hass: HomeAssistant,
    loaded_pilot: MockConfigEntry,
    status_entity_id: str,
    sonos_player: er.RegistryEntry,
    lastfm_server: LastFmServer,
    advance: Advance,
) -> None:
    runtime = loaded_pilot.runtime_data
    hass.states.async_set(
        sonos_player.entity_id, "playing", playing(sonos_player.entity_id, **PLAYING)
    )
    await hass.async_block_till_done()
    await advance(15)
    await advance(15)
    observed = runtime.snapshot.observed_seconds
    assert observed > 0

    await link_account(hass, loaded_pilot)

    state = hass.states.get(status_entity_id)
    assert state is not None
    assert state.attributes["lastfm_linked"] is True
    assert loaded_pilot.runtime_data is runtime
    await advance(15)
    assert runtime.snapshot.observed_seconds == observed + 15
    requests = len(lastfm_server.requests)
    await advance(15)
    assert len(lastfm_server.requests) == requests


async def test_idle_sensor_follows_link_and_disconnect_at_once(
    hass: HomeAssistant,
    loaded_pilot: MockConfigEntry,
    status_entity_id: str,
    lastfm_server: LastFmServer,
) -> None:
    await link_account(hass, loaded_pilot)
    linked = hass.states.get(status_entity_id)

    disconnect = await choose(
        hass, await start_options(hass, loaded_pilot), "disconnect"
    )
    await hass.config_entries.options.async_configure(disconnect["flow_id"], {})
    unlinked = hass.states.get(status_entity_id)

    assert linked is not None
    assert unlinked is not None
    assert linked.attributes["lastfm_linked"] is True
    assert unlinked.attributes["lastfm_linked"] is False
