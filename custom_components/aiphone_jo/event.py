"""Ring and problem events."""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AiphoneJoConfigEntry
from .const import ATTR_RAW_NAME, EVENT_RING
from .coordinator import Ring, signal_ring
from .entity import AiphoneJoEntity
from .health import PROBLEM_TYPES, Problem, signal_problem


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AiphoneJoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the ring and problem events."""
    runtime = entry.runtime_data
    async_add_entities(
        [AiphoneJoRingEvent(runtime, "ring"), AiphoneJoProblemEvent(runtime, "problem")]
    )


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


class AiphoneJoProblemEvent(AiphoneJoEntity, EventEntity):
    """Fired on every problem: lost connection, failed command, image or call.

    Always available, so that a lost connection is shown too.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_event_types = list(PROBLEM_TYPES)  # noqa: RUF012

    @property
    def available(self) -> bool:
        """Always available."""
        return True

    async def async_added_to_hass(self) -> None:
        """Listen for problems."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_problem(self.runtime.entry.entry_id),
                self._handle_problem,
            )
        )
        # Problems found while the entry was being set up (ring topic refused).
        if problems := self.runtime.health.problems:
            self._handle_problem(problems[-1])

    @callback
    def _handle_problem(self, problem: Problem) -> None:
        self._trigger_event(problem.type, {"message": problem.message})
        self.async_write_ha_state()
