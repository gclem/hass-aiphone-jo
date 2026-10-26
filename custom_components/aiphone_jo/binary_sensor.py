"""Ringing binary sensor."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AiphoneJoConfigEntry
from .entity import AiphoneJoEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AiphoneJoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the ringing sensor."""
    async_add_entities([AiphoneJoRingingSensor(entry.runtime_data, "ringing")])


class AiphoneJoRingingSensor(AiphoneJoEntity, BinarySensorEntity):
    """On while an entrance station is calling."""

    @property
    def is_on(self) -> bool:
        """True while ringing."""
        return self.runtime.ringing
