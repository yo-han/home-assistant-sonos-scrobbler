"""The status sensor exposes the snapshot and nothing else."""

from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.personal_sonos_scrobbler.const import (
    CONF_LASTFM,
    DOMAIN,
    SIGNAL_LASTFM_LINK_CHANGED,
)
from custom_components.personal_sonos_scrobbler.delivery import (
    DeliveryStatus,
    account_hash,
    resolve_status,
)
from custom_components.personal_sonos_scrobbler.outbox import MAX_ACTIVE_PLAYS
from custom_components.personal_sonos_scrobbler.playback import MarkerDelta
from tests.delivery_harness import RECENT, SCROBBLE, Pilot, accepted, history
from tests.helpers import Advance, link_account, playing
from tests.lastfm_server import ACCOUNT_NAME, Reply, json_reply
from tests.outbox_support import KEY_B, T0, fill_plays, write

PLAYING = {
    "source": "Spotify Connect",
    "media_artist": "Artist",
    "media_title": "Title",
    "media_duration": 200,
}
ATTRIBUTES = {
    "mode",
    "estimate_basis",
    "reason",
    "source_category",
    "has_artist",
    "has_title",
    "has_album",
    "has_duration",
    "has_position",
    "has_content_id",
    "has_queue_position",
    "has_group_members",
    "observed_seconds",
    "duration_seconds",
    "threshold_seconds",
    "candidate_count",
    "unknown_source_threshold_count",
    "ambiguous_restart_count",
    "seek_count",
    "clock_gap_count",
    "lastfm_linked",
    "speakers_left_out",
    "selected_speakers",
    "active_groups",
    "aggregated_across_groups",
}
DELIVERY_ATTRIBUTES = {
    "pending_count",
    "held_count",
    "uncertain_count",
    "sent_total",
    "skipped_total",
    "ignored_total",
    "oldest_queue_age_minutes",
    "error_code",
}
HOME_ASSISTANT_ATTRIBUTES = {"device_class", "friendly_name", "options"}


async def test_sensor_reports_observation_only_mode_and_stage(
    hass: HomeAssistant, status_entity_id: str
) -> None:
    state = hass.states.get(status_entity_id)

    assert state is not None
    assert state.state == "waiting"
    assert state.attributes["mode"] == "observation_only"
    assert state.attributes["estimate_basis"] == "ha_reported_playback"
    assert state.attributes["reason"] == "entity_missing"


async def test_sensor_updates_on_state_and_tick(
    hass: HomeAssistant,
    status_entity_id: str,
    sonos_player: er.RegistryEntry,
    advance: Advance,
) -> None:
    hass.states.async_set(
        sonos_player.entity_id, "playing", playing(sonos_player.entity_id, **PLAYING)
    )
    await hass.async_block_till_done()

    state = hass.states.get(status_entity_id)
    assert state is not None
    assert state.state == "observing"
    assert state.attributes["has_title"] is True
    assert state.attributes["source_category"] == "spotify_confirmed"
    assert state.attributes["duration_seconds"] == 200
    assert state.attributes["threshold_seconds"] == 100

    await advance(15)

    state = hass.states.get(status_entity_id)
    assert state is not None
    assert state.attributes["observed_seconds"] == 15


async def test_unique_id_is_stable_across_reload(
    hass: HomeAssistant,
    loaded_pilot: MockConfigEntry,
    status_entity_id: str,
    entity_registry: er.EntityRegistry,
) -> None:
    assert await hass.config_entries.async_reload(loaded_pilot.entry_id)
    await hass.async_block_till_done()

    assert (
        entity_registry.async_get_entity_id(
            "sensor", "personal_sonos_scrobbler", f"{loaded_pilot.entry_id}_status"
        )
        == status_entity_id
    )
    assert hass.states.get(status_entity_id) is not None


async def test_attributes_are_exact_allowlist(
    hass: HomeAssistant,
    status_entity_id: str,
    sonos_player: er.RegistryEntry,
) -> None:
    hass.states.async_set(
        sonos_player.entity_id, "playing", playing(sonos_player.entity_id, **PLAYING)
    )
    await hass.async_block_till_done()

    state = hass.states.get(status_entity_id)

    assert state is not None
    assert set(state.attributes) - HOME_ASSISTANT_ATTRIBUTES == ATTRIBUTES


async def test_entry_from_0_1_0_reads_as_unlinked(
    hass: HomeAssistant, status_entity_id: str
) -> None:
    state = hass.states.get(status_entity_id)

    assert state is not None
    assert state.attributes["lastfm_linked"] is False


async def test_link_signal_updates_the_idle_sensor_without_a_heartbeat(
    hass: HomeAssistant, loaded_pilot: MockConfigEntry, status_entity_id: str
) -> None:
    hass.config_entries.async_update_entry(
        loaded_pilot, data={**loaded_pilot.data, CONF_LASTFM: {"session_key": "x"}}
    )

    before = hass.states.get(status_entity_id)
    async_dispatcher_send(
        hass, SIGNAL_LASTFM_LINK_CHANGED.format(loaded_pilot.entry_id)
    )
    await hass.async_block_till_done()

    after = hass.states.get(status_entity_id)
    assert before is not None
    assert after is not None
    assert before.attributes["lastfm_linked"] is False
    assert after.attributes["lastfm_linked"] is True
    assert "x" not in {str(value) for value in after.attributes.values()}


def delivery_state(pilot: Pilot) -> State:
    registry = er.async_get(pilot.hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{pilot.entry.entry_id}_delivery"
    )
    assert entity_id is not None
    state = pilot.hass.states.get(entity_id)
    assert state is not None
    return state


async def test_existing_unique_id_unchanged(
    hass: HomeAssistant,
    loaded_pilot: MockConfigEntry,
    status_entity_id: str,
    entity_registry: er.EntityRegistry,
    sonos_player: er.RegistryEntry,
) -> None:
    ids = {
        entry.unique_id
        for entry in er.async_entries_for_config_entry(
            entity_registry, loaded_pilot.entry_id
        )
    }

    assert ids == {
        f"{loaded_pilot.entry_id}_speaker_{sonos_player.id}",
        f"{loaded_pilot.entry_id}_status",
        f"{loaded_pilot.entry_id}_delivery",
        f"{loaded_pilot.entry_id}_current_track",
        f"{loaded_pilot.entry_id}_current_speaker",
        f"{loaded_pilot.entry_id}_now_playing",
        f"{loaded_pilot.entry_id}_scrobbling",
    }


async def test_exact_attribute_allowlist_for_both_sensors(
    hass: HomeAssistant,
    pilot: Pilot,
    entity_registry: er.EntityRegistry,
) -> None:
    await pilot.setup()
    status_id = entity_registry.async_get_entity_id(
        "sensor", DOMAIN, f"{pilot.entry.entry_id}_status"
    )
    assert status_id is not None
    observation = hass.states.get(status_id)
    assert observation is not None

    delivery = delivery_state(pilot)

    assert set(observation.attributes) - HOME_ASSISTANT_ATTRIBUTES == ATTRIBUTES
    assert set(delivery.attributes) - HOME_ASSISTANT_ATTRIBUTES == DELIVERY_ATTRIBUTES
    assert observation.attributes["mode"] == "observation_only"
    assert observation.attributes["selected_speakers"] == 1
    assert observation.attributes["active_groups"] == 1
    assert observation.attributes["aggregated_across_groups"] is False


async def test_mode_follows_the_delivery_switch(pilot: Pilot) -> None:
    await pilot.linked()
    registry = er.async_get(pilot.hass)
    status_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{pilot.entry.entry_id}_status"
    )
    assert status_id is not None

    state = pilot.hass.states.get(status_id)
    assert state is not None
    assert state.attributes["mode"] == "delivery_enabled"

    await pilot.pause()
    state = pilot.hass.states.get(status_id)
    assert state is not None
    assert state.attributes["mode"] == "observation_only"


@pytest.mark.parametrize(
    ("failed", "enabled", "activity", "uncertain", "expected"),
    [
        (True, False, DeliveryStatus.READY, False, DeliveryStatus.STORAGE_ERROR),
        (True, True, DeliveryStatus.AUTH_REQUIRED, True, DeliveryStatus.STORAGE_ERROR),
        (False, False, DeliveryStatus.AUTH_REQUIRED, True, DeliveryStatus.DISABLED),
        (False, True, DeliveryStatus.AUTH_REQUIRED, True, DeliveryStatus.AUTH_REQUIRED),
        (False, True, DeliveryStatus.RETRYING, True, DeliveryStatus.UNCERTAIN),
        (False, True, DeliveryStatus.WAITING_GRACE, True, DeliveryStatus.UNCERTAIN),
        (
            False,
            True,
            DeliveryStatus.WAITING_GRACE,
            False,
            DeliveryStatus.WAITING_GRACE,
        ),
        (False, True, DeliveryStatus.RETRYING, False, DeliveryStatus.RETRYING),
        (False, True, DeliveryStatus.READY, False, DeliveryStatus.READY),
    ],
)
def test_status_priority(
    failed: bool,
    enabled: bool,
    activity: DeliveryStatus,
    uncertain: bool,
    expected: DeliveryStatus,
) -> None:
    assert (
        resolve_status(
            failed=failed, enabled=enabled, activity=activity, uncertain=uncertain
        )
        is expected
    )


async def test_status_states_are_distinct(pilot: Pilot) -> None:
    seen: dict[str, str] = {}

    await pilot.setup()
    await link_account(pilot.hass, pilot.entry)
    seen["disabled"] = delivery_state(pilot).state

    await pilot.enable()
    await pilot.quiet()
    seen["ready"] = delivery_state(pilot).state

    await pilot.accept_now()
    await pilot.reach(lambda: delivery_state(pilot).state == "waiting_grace")
    seen["waiting_grace"] = delivery_state(pilot).state

    pilot.server.queue(RECENT, json_reply({"error": 29}))
    await pilot.past_grace()
    await pilot.reach(lambda: delivery_state(pilot).state == "retrying", tick=1)
    seen["retrying"] = delivery_state(pilot).state

    assert seen == {name: name for name in seen}
    assert len(seen) == 4


async def test_uncertain_is_visible_with_its_count(pilot: Pilot) -> None:
    await pilot.linked()
    await pilot.accept_now()
    pilot.server.queue(RECENT, history())
    pilot.server.queue(SCROBBLE, Reply(status=200, body=b"{}"))

    await pilot.past_grace()
    await pilot.reach(lambda: pilot.state() == "uncertain", tick=1)
    await pilot.quiet()

    state = delivery_state(pilot)
    assert state.state == "uncertain"
    assert state.attributes["uncertain_count"] == 1
    assert state.attributes["pending_count"] == 0
    assert state.attributes["oldest_queue_age_minutes"] is not None


async def test_auth_required_is_visible(pilot: Pilot) -> None:
    await pilot.linked()
    await pilot.accept_now()
    pilot.server.queue(RECENT, json_reply({"error": 10}))

    await pilot.past_grace()
    await pilot.reach(lambda: delivery_state(pilot).state == "auth_required", tick=1)

    state = delivery_state(pilot)
    assert state.state == "auth_required"
    assert state.attributes["error_code"] == "auth_required"


async def test_counts_follow_a_delivery(pilot: Pilot) -> None:
    await pilot.linked()
    await pilot.accept_now()
    state = delivery_state(pilot)
    assert state.attributes["pending_count"] == 1
    pilot.server.queue(RECENT, history())
    pilot.server.queue(SCROBBLE, accepted())

    await pilot.past_grace()
    await pilot.delivered()
    await pilot.quiet()

    state = delivery_state(pilot)
    assert state.attributes["sent_total"] == 1
    assert state.attributes["pending_count"] == 0
    assert state.attributes["oldest_queue_age_minutes"] is None
    assert state.attributes["error_code"] is None


async def test_storage_error_is_visible_on_a_paused_entry(pilot: Pilot) -> None:
    await pilot.linked()
    await pilot.pause()
    await pilot.unload()
    pilot.path.unlink()

    await pilot.setup()

    state = delivery_state(pilot)
    assert state.state == "storage_error"
    assert state.attributes["error_code"] == "storage_missing"
    registry = er.async_get(pilot.hass)
    status_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{pilot.entry.entry_id}_status"
    )
    assert status_id is not None
    observation = pilot.hass.states.get(status_id)
    assert observation is not None
    assert observation.attributes["mode"] == "observation_only"


async def test_queue_full_is_an_error_code_and_the_worker_keeps_draining(
    pilot: Pilot,
) -> None:
    await pilot.linked()
    await pilot.pause()
    fill_plays(pilot.path, MAX_ACTIVE_PLAYS)
    # The generic fixture belongs to another account. A live worker must discard
    # those rows, so they cannot establish that this account's queue stays full.
    write(
        pilot.path,
        "UPDATE plays SET account_hash = ?, not_before = ?",
        account_hash(ACCOUNT_NAME),
        int((T0 + timedelta(hours=1)).timestamp()),
    )
    await pilot.enable()

    await pilot.accept_now("over", start=2000, key=KEY_B)
    pilot.service.async_on_occurrence_change(MarkerDelta())
    await pilot.reach(
        lambda: delivery_state(pilot).attributes["error_code"] == "queue_full"
    )

    state = delivery_state(pilot)
    assert state.attributes["pending_count"] == MAX_ACTIVE_PLAYS
    assert state.attributes["error_code"] == "queue_full"
    assert state.state != "storage_error"
    worker = pilot.service._worker
    assert worker is not None
    assert not worker.done()


async def test_clock_regression_is_an_error_code(
    pilot: Pilot, freezer: FrozenDateTimeFactory
) -> None:
    await pilot.linked()
    await pilot.accept_now()
    pilot.server.queue(RECENT, history())
    pilot.server.queue(SCROBBLE, accepted())
    await pilot.past_grace()
    await pilot.delivered()

    freezer.move_to(T0 + timedelta(seconds=60))
    await pilot.accept_now("second", start=1, key=KEY_B)
    await pilot.quiet()

    state = delivery_state(pilot)
    assert state.state == "retrying"
    assert state.attributes["error_code"] == "clock_regressed"
