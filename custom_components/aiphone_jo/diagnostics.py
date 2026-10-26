"""Diagnostics support for the Aiphone JO integration."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import async_redact_data

from . import AiphoneJoConfigEntry
from .const import TO_REDACT


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AiphoneJoConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry, with sensitive data redacted."""
    diagnostics: dict[str, Any] = {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT)
    }
    runtime = getattr(entry, "runtime_data", None)
    if runtime is not None:
        diagnostics["runtime"] = {
            "connected": runtime.connected,
            "ring_topic_subscribed": runtime.client.broadcast_subscribed,
            "station_count": len(runtime.stations),
            "ringing": runtime.ringing,
            "camera_available": runtime.media.available,
            "call_answering": runtime.calls.enabled,
            "call_talk": runtime.media.talk_enabled,
            "call_state": runtime.calls.state.value,
        }
    return diagnostics
