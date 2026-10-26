"""Door opening buttons (pulses, not locks)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AiphoneJoConfigEntry
from .coordinator import AiphoneJoRuntime
from .entity import AiphoneJoEntity
from .vendor.aiphone_jo import Gate


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AiphoneJoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the opening buttons."""
    runtime = entry.runtime_data
    async_add_entities(
        [
            AiphoneJoOpenButton(runtime, "large_opening", Gate.MAIN),
            AiphoneJoOpenButton(runtime, "small_opening", Gate.OPTION),
        ]
    )


class AiphoneJoOpenButton(AiphoneJoEntity, ButtonEntity):
    """Pulse one of the monitor's lock outputs."""

    def __init__(self, runtime: AiphoneJoRuntime, key: str, gate: Gate) -> None:
        """Initialize."""
        super().__init__(runtime, key)
        self._gate = gate

    async def async_press(self) -> None:
        """Open."""
        await self.runtime.async_open(self._gate)
