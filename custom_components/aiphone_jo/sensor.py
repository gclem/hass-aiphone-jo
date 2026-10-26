"""Last ringing station sensor."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AiphoneJoConfigEntry
from .call import CallState
from .const import ATTR_RAW_NAME
from .entity import AiphoneJoEntity, async_remove_disabled

ATTR_RANG_AT = "rang_at"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AiphoneJoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the last station sensor and, when enabled, the call state."""
    runtime = entry.runtime_data
    entities: list[SensorEntity] = [AiphoneJoLastStationSensor(runtime, "last_station")]
    if runtime.calls.enabled:
        entities.append(AiphoneJoCallStateSensor(runtime, "call_state"))
    else:
        async_remove_disabled(hass, runtime, "sensor", ["call_state"])
    async_add_entities(entities)


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


class AiphoneJoCallStateSensor(AiphoneJoEntity, SensorEntity):
    """State of the current or last call (experimental call answering)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [state.value for state in CallState]  # noqa: RUF012

    @property
    def native_value(self) -> str:
        """Call state."""
        return self.runtime.calls.state.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Station and start time of the call."""
        calls = self.runtime.calls
        return {
            "station": calls.station,
            "started_at": calls.started_at.isoformat() if calls.started_at else None,
            "end_reason": calls.end_reason,
        }
