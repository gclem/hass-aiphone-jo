"""Ringing and cloud connection binary sensors."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AiphoneJoConfigEntry
from .entity import AiphoneJoEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AiphoneJoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the ringing and cloud connection sensors."""
    runtime = entry.runtime_data
    async_add_entities(
        [
            AiphoneJoRingingSensor(runtime, "ringing"),
            AiphoneJoCloudConnectionSensor(runtime, "cloud_connection"),
        ]
    )


class AiphoneJoRingingSensor(AiphoneJoEntity, BinarySensorEntity):
    """On while an entrance station is calling."""

    @property
    def is_on(self) -> bool:
        """True while ringing."""
        return self.runtime.ringing


class AiphoneJoCloudConnectionSensor(AiphoneJoEntity, BinarySensorEntity):
    """Connection to the Aiphone cloud, with the last error.

    Always available: it explains why the other entities are unavailable.
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        """Always available."""
        return True

    @property
    def is_on(self) -> bool:
        """True while connected."""
        return self.runtime.connected

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Since when, last error, reconnections and ring topic."""
        return self.runtime.health.attributes
