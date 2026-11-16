"""Diagnostic service: an output command without a monitoring session.

The protocol notes say that the monitor only honours output commands during
a monitoring session; another integration for a similar Aiphone model sends
them without one. This service sends one command while no session is open
and returns the monitor's reply, to check it on real hardware. It may operate
the equipment wired to the output.
"""

from __future__ import annotations

import time
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError

from .const import DOMAIN
from .health import describe
from .vendor.aiphone_jo import AiphoneJoError, ResponseTimeoutError
from .vendor.aiphone_jo.messages import (
    build_out_option_request,
    build_unlock_request,
)

SERVICE_TEST_OUTPUT = "test_output_without_session"
ATTR_OUTPUT = "output"
REPLY_TIMEOUT = 8.0

SCHEMA = vol.Schema(
    {vol.Required(ATTR_OUTPUT): vol.All(vol.Coerce(int), vol.In([1, 2]))}
)


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the diagnostic service."""

    async def test_output(call: ServiceCall) -> ServiceResponse:
        entry = next(
            (
                entry
                for entry in hass.config_entries.async_entries(DOMAIN)
                if entry.state is ConfigEntryState.LOADED
            ),
            None,
        )
        if entry is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="not_loaded"
            )
        runtime = entry.runtime_data
        client: Any = runtime.client
        async with runtime.session_lock:
            if client.active_session is not None or runtime.media.live:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="session_open"
                )
            build = (
                build_unlock_request
                if call.data[ATTR_OUTPUT] == 1
                else build_out_option_request
            )
            started = time.monotonic()
            try:
                reply = await client.request(
                    build(client.credentials), timeout=REPLY_TIMEOUT
                )
            except ResponseTimeoutError:
                return {"answered": False, "waited_s": REPLY_TIMEOUT}
            except AiphoneJoError as err:
                return {"answered": False, "error": describe(err)}
        head = reply.get("HEADER") or {}
        # Only the result: the header also holds the terminal's identifiers.
        return {
            "answered": True,
            "reply_mid": head.get("MID"),
            "rslt": head.get("RSLT"),
            "body": reply.get("BODY") or {},
            "elapsed_s": round(time.monotonic() - started, 2),
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_TEST_OUTPUT,
        test_output,
        schema=SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
