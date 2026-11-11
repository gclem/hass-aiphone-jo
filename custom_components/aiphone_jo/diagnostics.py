"""Diagnostics support for the Aiphone JO integration."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import async_redact_data

from . import AiphoneJoConfigEntry
from .const import TO_REDACT


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _last_call(runtime: Any) -> dict[str, Any] | None:
    """Last call seen by the library, without identifiers (CID, client ids)."""
    call = getattr(runtime.client, "current_call", None)
    if call is None:
        return None
    return {
        "station_id": call.term_id,
        "station_kind": call.term_kind,
        "call_kind": call.call_kind,
        "status": str(call.status),
        "end_reason": str(call.end_reason) if call.end_reason else None,
        "ringing_at": _iso(call.ringing_at),
        "ended_at": _iso(call.ended_at),
        "engaged": call.engaged,
        # Signaling seen on the call: tells who ended it, and when.
        "signals": [
            {"mid": signal.mid, "rslt": signal.rslt, "at": _iso(signal.at)}
            for signal in call.signals
        ],
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AiphoneJoConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry, with sensitive data redacted."""
    diagnostics: dict[str, Any] = {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT)
    }
    runtime = getattr(entry, "runtime_data", None)
    if runtime is not None:
        calls = runtime.calls
        diagnostics["runtime"] = {
            "connected": runtime.connected,
            "ring_topic_subscribed": runtime.client.broadcast_subscribed,
            "station_count": len(runtime.stations),
            "ringing": runtime.ringing,
            "camera_available": runtime.media.available,
            "ring_snapshot": runtime.snapshots.enabled,
            "call_answering": calls.enabled,
            "call_talk": runtime.media.talk_enabled,
            "call_state": calls.state.value,
            "call_end_reason": calls.end_reason,
        }
        diagnostics["health"] = runtime.health.as_diagnostics()
        diagnostics["last_call"] = _last_call(runtime)
    return diagnostics
