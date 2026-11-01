"""Switch Now Playing and scrobbling; the one writer of those settings.

Each feature has its own flag and epoch, so that a toggle of one leaves the other's
worker and claims alone. A link change moves both in one write. Home Assistant may save
the entry at any moment, so an enable stages a new epoch with the flag off and
publishes True only once the ledger has it: a saved True never comes before the commit.
An off takes effect at once and cannot be blocked by a fault, because a stuck ledger
is exactly when it must still work. A request counts from its arrival: an off or an
account change that comes after it ends it, however long it waited for its turn.

A new selection of speakers is written here too, because it ends the scrobbling
period: old rows have no room, so all of them park, and only the enable that follows
may start the next period. While scrobbling is off there is no period to end, and an
enable on its way reads its start after any change that reached it.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from enum import StrEnum
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DELIVERY_ENABLED,
    CONF_DELIVERY_ENABLED_AT,
    CONF_DELIVERY_EPOCH,
    CONF_ENTITY_REGISTRY_IDS,
    CONF_HISTORICAL_DELIVERY,
    CONF_LASTFM,
    CONF_LASTFM_LINK_REVISION,
    CONF_NOW_PLAYING,
    CONF_NOW_PLAYING_EPOCH,
    CONF_SCROBBLING,
    CONF_SCROBBLING_SINCE,
    DOMAIN,
    SIGNAL_LASTFM_LINK_CHANGED,
)
from .outbox import OutboxError

_LOGGER = logging.getLogger(__name__)


class Feature(StrEnum):
    """What can be sent to Last.fm, each on its own switch."""

    NOW_PLAYING = "now_playing"
    SCROBBLING = "scrobbling"


FEATURES_OFF = {
    CONF_NOW_PLAYING: False,
    CONF_NOW_PLAYING_EPOCH: 0,
    CONF_SCROBBLING: False,
    CONF_SCROBBLING_SINCE: None,
}
_FLAGS = {Feature.NOW_PLAYING: CONF_NOW_PLAYING, Feature.SCROBBLING: CONF_SCROBBLING}
_EPOCHS = {
    Feature.NOW_PLAYING: CONF_NOW_PLAYING_EPOCH,
    Feature.SCROBBLING: CONF_DELIVERY_EPOCH,
}


def is_enabled(data: Mapping[str, Any], feature: Feature) -> bool:
    """Return whether the entry holds a literal True for this feature."""
    return data.get(_FLAGS[feature]) is True


def any_enabled(data: Mapping[str, Any]) -> bool:
    """Return whether anything may be sent at all."""
    return any(is_enabled(data, feature) for feature in Feature)


def feature_epoch(data: Mapping[str, Any], feature: Feature) -> int:
    """Return the epoch of this feature."""
    epoch: int = data.get(_EPOCHS[feature], 0)
    return epoch


def feature_off(
    data: Mapping[str, Any], feature: Feature, epoch: int
) -> dict[str, Any]:
    """Return the data with this feature off at this epoch."""
    changed = {**data, _FLAGS[feature]: False, _EPOCHS[feature]: epoch}
    if feature is Feature.SCROBBLING:
        changed[CONF_SCROBBLING_SINCE] = None
    return changed


def all_features_off(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return the data a link change leaves: nothing is on, every fence moved."""
    changed = dict(data)
    for feature in Feature:
        changed = feature_off(changed, feature, feature_epoch(changed, feature) + 1)
    return changed


def migrated_features(data: Mapping[str, Any]) -> dict[str, Any]:
    """Keep a linked 0.4 Now Playing; no older value can ever switch scrobbling on."""
    legacy = (
        CONF_DELIVERY_ENABLED,
        CONF_DELIVERY_ENABLED_AT,
        CONF_HISTORICAL_DELIVERY,
    )
    return {
        **{key: value for key, value in data.items() if key not in legacy},
        **FEATURES_OFF,
        CONF_NOW_PLAYING: data.get(CONF_DELIVERY_ENABLED) is True
        and CONF_LASTFM in data,
    }


def _failure(translation_key: str) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN, translation_key=translation_key
    )


class FeatureControl:
    """Apply switch changes for one entry; what it returns has really happened."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[Any],
        prepare_ledger: Callable[[], Awaitable[None]],
        settle: Callable[[], Awaitable[None]],
        latest_queued_start: Callable[[], Awaitable[int | None]],
        is_open: Callable[[], bool],
    ) -> None:
        """Take the ledger steps of the delivery service, which owns the file."""
        self._hass = hass
        self._entry = entry
        self._prepare_ledger = prepare_ledger
        self._settle = settle
        self._latest_queued_start = latest_queued_start
        self._is_open = is_open
        self._enabling = {feature: asyncio.Lock() for feature in Feature}
        # Not the lock's own state: it reads as free before its next waiter woke.
        self._requests = dict.fromkeys(Feature, 0)

    def is_on(self, feature: Feature) -> bool:
        """Return the confirmed setting."""
        return is_enabled(self._entry.data, feature)

    async def async_turn_on(self, feature: Feature) -> None:
        """Switch on, once the ledger holds the new epoch; a click twice is one on."""
        data = self._entry.data
        epoch = feature_epoch(data, feature)
        revision = data.get(CONF_LASTFM_LINK_REVISION, 0)
        self._requests[feature] += 1
        try:
            async with self._enabling[feature]:
                await self._async_enable(feature, epoch, revision)
        finally:
            self._requests[feature] -= 1

    async def _async_enable(self, feature: Feature, epoch: int, revision: int) -> None:
        """Take this request's turn; an on before it may already have done the work."""
        if self.is_on(feature):
            return
        if CONF_LASTFM not in self._entry.data:
            raise _failure("link_required")
        self._require_unchanged(feature, epoch, revision)
        since: float | None = None
        try:
            await self._prepare_ledger()
            self._require_unchanged(feature, epoch, revision)
            epoch += 1
            self._write(feature_off(self._entry.data, feature, epoch))
            await self._settle()
            if feature is Feature.SCROBBLING:
                since = await self._async_period_start()
        except OutboxError:
            self._require_unchanged(feature, epoch, revision)
            raise _failure("storage_error") from None
        self._require_unchanged(feature, epoch, revision)
        self._publish(feature, since)

    async def _async_period_start(self) -> float:
        """Return a start no queued row reaches, also if the clock went back.

        Read after the commit: the fence is off and drained, so no row can follow.
        The clock comes last: a track that starts during the query is not new.
        """
        latest = await self._latest_queued_start()
        now = dt_util.utcnow().timestamp()
        return now if latest is None else max(now, latest + 1.0)

    async def async_turn_off(self, feature: Feature) -> None:
        """Fence at once, also an enable still on its way, then wait for the ledger."""
        if not self.is_on(feature) and not self._requests[feature]:
            return
        epoch = feature_epoch(self._entry.data, feature) + 1
        self._write(feature_off(self._entry.data, feature, epoch))
        try:
            await self._settle()
        except OutboxError as error:
            # Off stands in the entry; only surviving a crash is not proven.
            _LOGGER.warning("Switching off is not in the ledger: %s", error.code)

    async def async_change_selection(
        self, registry_ids: list[str], rebind: Callable[[list[str]], None]
    ) -> None:
        """Change the speakers and rebind at once; scrobbling resumes in a new period.

        Nothing before the rebind awaits, so nothing can leave on the old selection.
        The change stands whatever happens to the period after it: a later off, an
        account change or a stop simply decides that no new period starts.
        """
        if not self._is_open():
            raise _failure("speaker_unavailable")
        feature = Feature.SCROBBLING
        data = {**self._entry.data, CONF_ENTITY_REGISTRY_IDS: registry_ids}
        if not self.is_on(feature):
            self._write(data)
            rebind(registry_ids)
            return
        epoch = feature_epoch(data, feature) + 1
        revision = data.get(CONF_LASTFM_LINK_REVISION, 0)
        self._requests[feature] += 1
        try:
            self._write(feature_off(data, feature, epoch))
            rebind(registry_ids)
            async with self._enabling[feature]:
                await self._async_enable(feature, epoch, revision)
        except HomeAssistantError as error:
            if error.translation_key == "storage_error":
                raise _failure("selection_without_scrobbling") from None
            if not self._is_open():
                _LOGGER.warning("Scrobbling stopped with the integration")
        finally:
            self._requests[feature] -= 1

    @callback
    def _require_unchanged(self, feature: Feature, epoch: int, revision: int) -> None:
        """Refuse a late step: an off, another account or an unload came first."""
        data = self._entry.data
        if (
            not self._is_open()
            or CONF_LASTFM not in data
            or feature_epoch(data, feature) != epoch
            or data.get(CONF_LASTFM_LINK_REVISION, 0) != revision
        ):
            raise _failure("changed_meanwhile")

    @callback
    def _publish(self, feature: Feature, since: float | None) -> None:
        """Switch on; scrobbling starts a new period at since."""
        changed = {**self._entry.data, _FLAGS[feature]: True}
        if feature is Feature.SCROBBLING:
            changed[CONF_SCROBBLING_SINCE] = since
        self._write(changed)

    @callback
    def _write(self, data: Mapping[str, Any]) -> None:
        self._hass.config_entries.async_update_entry(self._entry, data=data)
        async_dispatcher_send(
            self._hass, SIGNAL_LASTFM_LINK_CHANGED.format(self._entry.entry_id)
        )
