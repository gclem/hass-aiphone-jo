"""Ring event."""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AiphoneJoConfigEntry
from .const import ATTR_RAW_NAME, EVENT_RING
from .coordinator import Ring, signal_ring
from .entity import AiphoneJoEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AiphoneJoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the ring event."""
    async_add_entities([AiphoneJoRingEvent(entry.runtime_data, "ring")])


class AiphoneJoRingEvent(AiphoneJoEntity, EventEntity):
    """Fired on every call from an entrance station."""

    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = [EVENT_RING]  # noqa: RUF012

    async def async_added_to_hass(self) -> None:
        """Listen for rings."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_ring(self.runtime.entry.entry_id), self._handle_ring
            )
        )

    @callback
    def _handle_ring(self, ring: Ring) -> None:
        label = self.runtime.station_label(ring.station)
        self._trigger_event(EVENT_RING, {"station": label, ATTR_RAW_NAME: ring.station})
        self.async_write_ha_state()
