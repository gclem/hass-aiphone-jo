"""Buttons triggering the monitor's two outputs (pulses, not locks)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AiphoneJoConfigEntry
from .call import ACTIVE_STATES, CallState
from .coordinator import AiphoneJoRuntime
from .entity import AiphoneJoEntity, async_remove_disabled
from .vendor.aiphone_jo import Output

CALL_BUTTONS = ("call_answer", "call_hang_up", "call_reject")

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
    """Set up the output buttons and, when enabled, the call buttons."""
    runtime = entry.runtime_data
    entities: list[ButtonEntity] = [
        AiphoneJoOutputButton(runtime, key, unique_key, output)
        for key, unique_key, output in BUTTONS
    ]
    if runtime.calls.enabled:
        entities.extend(AiphoneJoCallButton(runtime, action) for action in CALL_BUTTONS)
    else:
        async_remove_disabled(hass, runtime, "button", CALL_BUTTONS)
    async_add_entities(entities)


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


class AiphoneJoCallButton(AiphoneJoEntity, ButtonEntity):
    """Answer, hang up or reject a call (experimental)."""

    def __init__(self, runtime: AiphoneJoRuntime, key: str) -> None:
        """Initialize."""
        super().__init__(runtime, key)
        self._key = key

    @property
    def available(self) -> bool:
        """Available only when the action makes sense."""
        if not super().available:
            return False
        state = self.runtime.calls.state
        if self._key == "call_hang_up":
            return state in ACTIVE_STATES
        return state is CallState.RINGING

    async def async_press(self) -> None:
        """Run the action."""
        calls = self.runtime.calls
        if self._key == "call_answer":
            await calls.async_answer()
        elif self._key == "call_hang_up":
            await calls.async_hang_up()
        else:
            await calls.async_reject()
