"""The manifest must load through Home Assistant's own integration loader."""

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from custom_components.personal_sonos_scrobbler.const import DOMAIN


async def test_manifest_loads_as_custom_integration(hass: HomeAssistant) -> None:
    integration = await async_get_integration(hass, DOMAIN)

    assert integration.single_config_entry is True
    assert integration.requirements == []
    assert integration.after_dependencies == ["sonos"]
    assert integration.iot_class == "calculated"
    assert integration.integration_type == "service"
    assert str(integration.version) == "0.8.0"
    assert integration.config_flow is True
    assert integration.documentation == (
        "https://github.com/yo-han/home-assistant-sonos-scrobbler"
    )
    assert integration.issue_tracker == (
        "https://github.com/yo-han/home-assistant-sonos-scrobbler/issues"
    )
