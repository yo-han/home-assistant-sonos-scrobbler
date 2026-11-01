"""Track metadata must not leave the Sonos entity through any output of the pilot."""

import json
import logging

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.personal_sonos_scrobbler.const import CONF_LASTFM, DOMAIN
from custom_components.personal_sonos_scrobbler.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.personal_sonos_scrobbler.lastfm import LastFmCredentials
from custom_components.personal_sonos_scrobbler.lastfm_flow import _LinkAttempt
from tests.helpers import Advance, choose, playing, start_options
from tests.lastfm_server import LastFmServer, json_reply, session_reply, token_reply

LINK_API_KEY = "c0dec0dec0dec0dec0dec0dec0dec0de"
LINK_TOKEN = "fadefadefadefadefadefadefadefade"
LINK_SECRET = "canary shared value"
LINK_SESSION = "canary-session-value"
LINK_ACCOUNT = "canary_listener"
LINK_CANARIES = (LINK_API_KEY, LINK_TOKEN, LINK_SECRET, LINK_SESSION, LINK_ACCOUNT)
CANARIES = {
    "media_artist": "canary-artist",
    "media_title": "canary-title",
    "media_album_name": "canary-album",
    "media_content_id": "x-sonos-spotify:spotify%3atrack%3acanaryuri?sid=12",
    "media_image_url": "http://192.0.2.1/canary-image.jpg",
    "entity_picture": "/api/media_player_proxy/canary-token-abc123",
}
FRAGMENTS = (
    "canary-artist",
    "canary-title",
    "canary-album",
    "canaryuri",
    "canary-image",
    "canary-token",
)


async def test_canaries_absent_from_sensor_logs_and_diagnostics(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    loaded_pilot: MockConfigEntry,
    status_entity_id: str,
    sonos_player: er.RegistryEntry,
    advance: Advance,
) -> None:
    caplog.set_level(logging.DEBUG, logger=f"custom_components.{DOMAIN}")
    hass.states.async_set(
        sonos_player.entity_id,
        "playing",
        playing(sonos_player.entity_id, **CANARIES, media_duration=40),
    )
    await hass.async_block_till_done()
    for _ in range(2):
        await advance(15)

    source = hass.states.get(sonos_player.entity_id)
    sensor = hass.states.get(status_entity_id)
    runtime = loaded_pilot.runtime_data
    assert source is not None
    assert sensor is not None
    assert all(fragment in str(source.attributes) for fragment in FRAGMENTS)
    assert sensor.attributes["has_title"] is True
    assert sensor.attributes["has_content_id"] is True
    assert runtime.snapshot.candidate_count == 1
    assert "Observation is qualified" in caplog.text

    diagnostics = await async_get_config_entry_diagnostics(hass, loaded_pilot)
    streams = runtime._streams._streams
    engine = next(iter(streams.values())).engine
    outputs = {
        "logs": caplog.text,
        "sensor_attributes": json.dumps(sensor.attributes, default=str),
        "sensor_state": sensor.state,
        "diagnostics": json.dumps(diagnostics),
        "snapshot": repr(runtime.snapshot),
        "streams": repr(streams),
        "engine_play": repr(engine._play),
        "engine_sample": repr(engine._sample),
        "active_occurrence": repr(engine.active_occurrence),
    }
    assert engine._play is not None
    assert engine._play.metadata is not None
    for name, output in outputs.items():
        for fragment in FRAGMENTS:
            assert fragment not in output, f"{fragment} leaked through {name}"


async def test_diagnostics_hold_no_entity_id_or_entry_data(
    hass: HomeAssistant,
    loaded_pilot: MockConfigEntry,
    sonos_player: er.RegistryEntry,
) -> None:
    diagnostics = await async_get_config_entry_diagnostics(hass, loaded_pilot)

    serialized = json.dumps(diagnostics)
    assert sonos_player.entity_id not in serialized
    assert sonos_player.id not in serialized
    assert loaded_pilot.entry_id not in serialized
    assert diagnostics["entity_present"] is True
    assert diagnostics["version"] == "0.8.1"
    assert diagnostics["mode"] == "observation_only"
    assert diagnostics["delivery_status"] == "disabled"
    assert diagnostics["pending_count"] == 0


async def test_diagnostics_expose_only_a_link_boolean(
    hass: HomeAssistant, loaded_pilot: MockConfigEntry
) -> None:
    link = {
        "api_key": "canary-api-key",
        "shared_secret": "canary-shared-secret",
        "session_key": "canary-session-key",
        "account_name": "canary-account",
    }
    unlinked = await async_get_config_entry_diagnostics(hass, loaded_pilot)
    hass.config_entries.async_update_entry(
        loaded_pilot, data={**loaded_pilot.data, CONF_LASTFM: link}
    )

    linked = await async_get_config_entry_diagnostics(hass, loaded_pilot)

    assert unlinked["lastfm_linked"] is False
    assert linked == {**unlinked, "lastfm_linked": True}
    assert "canary" not in json.dumps(linked)


async def _link_outputs(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    status_entity_id: str,
    server: LastFmServer,
) -> dict[str, str]:
    """Run a failed and a successful link and collect what they leave behind."""
    server.queue("auth.getToken", token_reply(LINK_TOKEN), token_reply(LINK_TOKEN))
    server.queue(
        "auth.getSession",
        json_reply({"error": 4, "message": f"rejected {LINK_SESSION}"}),
        session_reply(name=LINK_ACCOUNT, key=LINK_SESSION),
    )
    outputs: dict[str, str] = {}
    credentials = {"api_key": LINK_API_KEY, "shared_secret": LINK_SECRET}

    for name in ("failed", "linked"):
        form = await choose(hass, await start_options(hass, entry), "connect")
        flow = hass.config_entries.options._progress[form["flow_id"]]
        result = await hass.config_entries.options.async_configure(
            form["flow_id"], credentials
        )
        outputs[f"{name}_flow"] = repr(flow) + repr(vars(flow).get("_attempt"))
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {}
        )
        if name == "linked":
            result = await choose(hass, result, "confirm_link")
        outputs[f"{name}_result"] = str(result)

    sensor = hass.states.get(status_entity_id)
    assert sensor is not None
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    return {
        **outputs,
        "sensor_state": sensor.state,
        "sensor_attributes": json.dumps(sensor.attributes, default=str),
        "diagnostics": json.dumps(diagnostics),
    }


async def test_link_canaries_stay_out_of_every_output(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    loaded_pilot: MockConfigEntry,
    status_entity_id: str,
    lastfm_server: LastFmServer,
) -> None:
    caplog.set_level(logging.DEBUG)

    outputs = await _link_outputs(hass, loaded_pilot, status_entity_id, lastfm_server)

    assert loaded_pilot.data[CONF_LASTFM]["session_key"] == LINK_SESSION
    assert len(lastfm_server.requests) == 4
    outputs["logs"] = caplog.text
    for name, output in outputs.items():
        for canary in LINK_CANARIES:
            assert canary not in output, f"{canary} leaked through {name}"


def test_masked_objects_hide_their_values() -> None:
    attempt = _LinkAttempt(
        credentials=LastFmCredentials(LINK_API_KEY, LINK_SECRET),
        token=LINK_TOKEN,
        authorization_url=f"https://example.invalid/?{LINK_TOKEN}",
        expires_at=0.0,
    )

    assert not [canary for canary in LINK_CANARIES if canary in repr(attempt)]
