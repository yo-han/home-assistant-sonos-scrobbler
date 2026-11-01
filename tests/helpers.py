"""Helpers shared by tests that run on a real Home Assistant event loop."""

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.personal_sonos_scrobbler.const import (
    CONF_DELIVERY_EPOCH,
    CONF_ENTITY_REGISTRY_IDS,
    CONF_OUTBOX_CREATED,
)
from custom_components.personal_sonos_scrobbler.controls import FEATURES_OFF
from custom_components.personal_sonos_scrobbler.streams import TOPOLOGY_SETTLE_SECONDS
from tests.lastfm_server import API_KEY, SHARED_SECRET

type Advance = Callable[[float], Awaitable[None]]

CREDENTIALS = {"api_key": API_KEY, "shared_secret": SHARED_SECRET}
HOME_ASSISTANT_TIMERS = ("_TrackTimeInterval", "Store._async")
SETTLE = TOPOLOGY_SETTLE_SECONDS + 0.1
HEARTBEAT = 15


class Room:
    """A Sonos entity that publishes what a real speaker writes."""

    def __init__(self, hass: HomeAssistant, player: er.RegistryEntry) -> None:
        self._hass = hass
        self.entity_id = player.entity_id

    def play(
        self,
        title: str = "Title",
        state: str = "playing",
        *,
        members: Iterable[Room] | None = None,
        position: float = 0.0,
        **attributes: Any,
    ) -> None:
        self._hass.states.async_set(
            self.entity_id,
            state,
            {
                "source": "Spotify Connect",
                "media_artist": "Synthetic Artist",
                "media_album_name": "Synthetic Album",
                "media_title": title,
                "media_duration": 200,
                "media_position": position,
                "media_position_updated_at": dt_util.utcnow(),
                "group_members": [room.entity_id for room in (members or (self,))],
                **attributes,
            },
        )


def entry_data(*registry_ids: str) -> dict[str, object]:
    """Return what a new entry holds: both features off, every epoch 0."""
    return {
        CONF_ENTITY_REGISTRY_IDS: list(registry_ids),
        CONF_DELIVERY_EPOCH: 0,
        CONF_OUTBOX_CREATED: False,
        **FEATURES_OFF,
    }


def playing(entity_id: str, **attributes: object) -> dict[str, object]:
    """Return the state attributes of a standalone Sonos speaker."""
    return {"group_members": [entity_id], **attributes}


def shown(hass: HomeAssistant, entity_id: str) -> tuple[str, dict[str, Any]]:
    state = hass.states.get(entity_id)
    assert state is not None
    attributes = dict(state.attributes)
    attributes.pop("friendly_name", None)
    attributes.pop("icon", None)
    return state.state, attributes


def is_clear(hass: HomeAssistant, entity_id: str) -> bool:
    return shown(hass, entity_id) == (STATE_UNKNOWN, {})


def active_timers(hass: HomeAssistant) -> int:
    """Count pending timers, without Home Assistant's own housekeeping."""
    timers = hass.loop._scheduled  # type: ignore[attr-defined]
    return sum(
        not timer.cancelled()
        and not any(name in repr(timer) for name in HOME_ASSISTANT_TIMERS)
        for timer in timers
    )


async def start_options(hass: HomeAssistant, entry: ConfigEntry) -> ConfigFlowResult:
    """Open the options flow on the menu."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    return result


async def choose(
    hass: HomeAssistant, result: ConfigFlowResult, option: str
) -> ConfigFlowResult:
    """Pick a menu option."""
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": option}
    )


async def reach_authorize(
    hass: HomeAssistant, entry: ConfigEntry, option: str = "connect"
) -> ConfigFlowResult:
    """Walk a flow to the step that shows the authorization link."""
    result = await choose(hass, await start_options(hass, entry), option)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], dict(CREDENTIALS)
    )
    assert result.get("step_id") == "authorize"
    return result


async def reach_confirm(
    hass: HomeAssistant, entry: ConfigEntry, option: str = "connect"
) -> ConfigFlowResult:
    """Walk a flow to the account confirmation."""
    result = await reach_authorize(hass, entry, option)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result.get("step_id") == "confirm"
    return result


async def link_account(
    hass: HomeAssistant, entry: ConfigEntry, option: str = "connect"
) -> ConfigFlowResult:
    """Complete a link through the real flow."""
    return await choose(hass, await reach_confirm(hass, entry, option), "confirm_link")
