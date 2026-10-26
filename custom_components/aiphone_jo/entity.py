"""Base entity for the Aiphone JO integration."""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import (
    CONF_MONITOR,
    CONF_SYSTEM_VERSION,
    DEFAULT_TITLE,
    DOMAIN,
    MANUFACTURER,
    MODEL,
)
from .coordinator import AiphoneJoRuntime, signal_update


class AiphoneJoEntity(Entity):
    """Entity attached to the monitor device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, runtime: AiphoneJoRuntime, key: str, *, unique_key: str | None = None
    ) -> None:
        """Initialize."""
        self.runtime = runtime
        entry = runtime.entry
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.unique_id}_{unique_key or key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(entry.unique_id))},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=DEFAULT_TITLE,
            sw_version=entry.data.get(CONF_MONITOR, {}).get(CONF_SYSTEM_VERSION),
        )

    @property
    def available(self) -> bool:
        """Available while the cloud connection is up."""
        return self.runtime.connected

    async def async_added_to_hass(self) -> None:
        """Follow runtime updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_update(self.runtime.entry.entry_id),
                self.async_write_ha_state,
            )
        )


def async_remove_disabled(
    hass: HomeAssistant, runtime: AiphoneJoRuntime, domain: str, keys: Iterable[str]
) -> None:
    """Remove entities of a disabled optional feature from the registry."""
    registry = er.async_get(hass)
    for key in keys:
        unique_id = f"{runtime.entry.unique_id}_{key}"
        if entity_id := registry.async_get_entity_id(domain, DOMAIN, unique_id):
            registry.async_remove(entity_id)
