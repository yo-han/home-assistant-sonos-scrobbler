"""Select the Sonos media_players to observe; the choice may stay empty."""

from collections.abc import Mapping
from typing import Any, override

import probatio
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ENTITY_ID
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .const import (
    CONF_DELIVERY_EPOCH,
    CONF_ENTITY_REGISTRY_IDS,
    CONF_OUTBOX_CREATED,
    CONFIG_MINOR_VERSION,
    CONFIG_VERSION,
    DOMAIN,
    MAX_SELECTED_PLAYERS,
    TITLE,
)
from .controls import FEATURES_OFF
from .delivery import delivery_service
from .lastfm_flow import LastFmOptionsFlow
from .speaker_controls import (
    MEDIA_PLAYER_DOMAIN,
    SONOS_PLATFORM,
    enabled_sonos_players,
    is_sonos_media_player,
)


@callback
def eligible_sonos_entity_ids(hass: HomeAssistant) -> list[str]:
    """Return every enabled media_player registered by the sonos integration."""
    return sorted(
        entry.entity_id for entry in enabled_sonos_players(er.async_get(hass))
    )


@callback
def validate_sonos_entity(hass: HomeAssistant, entity_id: str) -> str | None:
    """Return an error key unless the registry proves the entity is usable."""
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None or not is_sonos_media_player(entry):
        return "not_sonos_media_player"
    if entry.disabled_by is not None:
        return "entity_disabled"
    return None


@callback
def validate_selection(hass: HomeAssistant, entity_ids: list[str]) -> str | None:
    """Return an error key unless every selected player is usable and listed once."""
    if len(entity_ids) > MAX_SELECTED_PLAYERS:
        return "too_many_players"
    if len(set(entity_ids)) != len(entity_ids):
        return "selection_duplicate"
    for entity_id in entity_ids:
        if error := validate_sonos_entity(hass, entity_id):
            return error
    return None


def _selection_schema(current: list[str] | None) -> probatio.Schema:
    """Show the selection as a suggestion: a default would undo clearing it."""
    return probatio.Schema(
        {
            probatio.Optional(
                CONF_ENTITY_ID, description={"suggested_value": current or []}
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=MEDIA_PLAYER_DOMAIN,
                    integration=SONOS_PLATFORM,
                    multiple=True,
                )
            )
        }
    )


class PilotConfigFlow(ConfigFlow, domain=DOMAIN):
    """Store registry ids so a rename keeps pointing at the same players."""

    VERSION = CONFIG_VERSION
    MINOR_VERSION = CONFIG_MINOR_VERSION

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> LastFmOptionsFlow:
        """Offer the Last.fm account link and the queue."""
        return LastFmOptionsFlow()

    async def async_step_user(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the single entry."""
        return await self._async_step_select("user", user_input, None)

    async def async_step_reconfigure(
        self, user_input: Mapping[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the selected players at once; a gone player leaves the form."""
        registry = er.async_get(self.hass)
        current = [
            entity_id
            for registry_id in self._get_reconfigure_entry().data[
                CONF_ENTITY_REGISTRY_IDS
            ]
            if (entity_id := er.async_resolve_entity_id(registry, registry_id))
        ]
        return await self._async_step_select("reconfigure", user_input, current)

    async def _async_step_select(
        self,
        step_id: str,
        user_input: Mapping[str, Any] | None,
        current_entity_ids: list[str] | None,
    ) -> ConfigFlowResult:
        if not eligible_sonos_entity_ids(self.hass):
            return self.async_abort(reason="no_sonos_media_players")

        errors: dict[str, str] = {}
        if user_input is not None:
            entity_ids: list[str] = user_input.get(CONF_ENTITY_ID, [])
            if error := validate_selection(self.hass, entity_ids):
                errors[CONF_ENTITY_ID] = error
            else:
                return await self._async_finish(step_id, entity_ids)

        return self.async_show_form(
            step_id=step_id,
            data_schema=_selection_schema(current_entity_ids),
            errors=errors,
        )

    async def _async_finish(
        self, step_id: str, entity_ids: list[str]
    ) -> ConfigFlowResult:
        registry = er.async_get(self.hass)
        registry_ids = []
        for entity_id in entity_ids:
            registry_entry = registry.async_get(entity_id)
            assert registry_entry is not None
            registry_ids.append(registry_entry.id)
        if step_id == "reconfigure":
            return await self._async_change_selection(registry_ids)
        # Both features start off; only their switches turn them on.
        return self.async_create_entry(
            title=TITLE,
            data={
                CONF_ENTITY_REGISTRY_IDS: registry_ids,
                CONF_DELIVERY_EPOCH: 0,
                CONF_OUTBOX_CREATED: False,
                **FEATURES_OFF,
            },
        )

    async def _async_change_selection(
        self, registry_ids: list[str]
    ) -> ConfigFlowResult:
        """Change the selection the way a switch does; only a running entry can."""
        entry = self._get_reconfigure_entry()
        service = delivery_service(self.hass, entry.entry_id)
        if service is None:
            return self.async_abort(reason="not_running")
        try:
            await service.speakers.async_set_selection(registry_ids)
        except HomeAssistantError as error:
            reason = error.translation_key
            return self.async_abort(
                reason="not_running" if reason == "speaker_unavailable" else str(reason)
            )
        return self.async_abort(reason="reconfigure_successful")
