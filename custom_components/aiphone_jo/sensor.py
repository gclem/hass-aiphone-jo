"""Last ringing station sensor."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import RestoreSensor
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AiphoneJoConfigEntry
from .const import ATTR_RAW_NAME
from .entity import AiphoneJoEntity

ATTR_RANG_AT = "rang_at"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AiphoneJoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the last station sensor."""
    async_add_entities([AiphoneJoLastStationSensor(entry.runtime_data, "last_station")])


class AiphoneJoLastStationSensor(AiphoneJoEntity, RestoreSensor):
    """Name of the entrance station that rang last (kept across restarts)."""

    _restored_value: str | None = None
    _restored_at: str | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the previous value."""
        await super().async_added_to_hass()
        if (data := await self.async_get_last_sensor_data()) is not None:
            value = data.native_value
            self._restored_value = str(value) if value is not None else None
        if (state := await self.async_get_last_state()) is not None:
            self._restored_at = state.attributes.get(ATTR_RANG_AT)
            if raw := state.attributes.get(ATTR_RAW_NAME):
                self._restored_value = str(raw)

    @property
    def _raw_name(self) -> str | None:
        ring = self.runtime.last_ring
        return ring.station if ring else self._restored_value

    @property
    def native_value(self) -> str | None:
        """Station name, translated when it is a factory default name."""
        raw = self._raw_name
        return self.runtime.station_label(raw) if raw is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Time of the last ring and station name from the Aiphone service."""
        ring = self.runtime.last_ring
        return {
            ATTR_RANG_AT: ring.at.isoformat() if ring else self._restored_at,
            ATTR_RAW_NAME: self._raw_name,
        }
