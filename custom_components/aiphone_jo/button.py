"""Buttons triggering the monitor's two outputs (pulses, not locks)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AiphoneJoConfigEntry
from .coordinator import AiphoneJoRuntime
from .entity import AiphoneJoEntity
from .vendor.aiphone_jo import Output

# (translation key, unique id suffix, monitor output). The unique id suffixes
# predate the current names and must not change: entities would be recreated.
BUTTONS: tuple[tuple[str, str, Output], ...] = (
    ("action_1", "large_opening", Output.UNLOCK),
    ("action_2", "small_opening", Output.OPTION),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AiphoneJoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the output buttons."""
    runtime = entry.runtime_data
    async_add_entities(
        AiphoneJoOutputButton(runtime, key, unique_key, output)
        for key, unique_key, output in BUTTONS
    )


class AiphoneJoOutputButton(AiphoneJoEntity, ButtonEntity):
    """Pulse one of the monitor's outputs (1: unlock, 2: option)."""

    def __init__(
        self, runtime: AiphoneJoRuntime, key: str, unique_key: str, output: Output
    ) -> None:
        """Initialize."""
        super().__init__(runtime, key, unique_key=unique_key)
        self._output = output

    async def async_press(self) -> None:
        """Trigger the output."""
        await self.runtime.async_pulse(self._output)
