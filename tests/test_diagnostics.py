"""Tests for Aiphone JO diagnostics."""

import json

from homeassistant.const import CONF_HOST, CONF_MAC, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import REDACTED
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aiphone_jo.const import DOMAIN, MODEL
from custom_components.aiphone_jo.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.aiphone_jo.vendor.aiphone_jo import CloudCredentials

from .conftest import FINGERPRINT_ID, MONITOR

# Fictitious values only (RFC 5737 / RFC 2606).
SENSITIVE_DATA = {
    CONF_HOST: "192.0.2.10",
    CONF_MAC: "00:00:5e:00:53:01",
    CONF_USERNAME: "user@example.invalid",
    "endpoint": "iot.example.invalid",
    "topic": "example/topic",
}


async def test_diagnostics_redacts_sensitive_data(hass: HomeAssistant) -> None:
    """Sensitive config entry fields are redacted from diagnostics."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="example-unique-id",
        data={**SENSITIVE_DATA, "model": MODEL},
    )
    entry.add_to_hass(hass)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["entry"]["unique_id"] == REDACTED
    assert diagnostics["entry"]["data"]["model"] == MODEL
    for key in SENSITIVE_DATA:
        assert diagnostics["entry"]["data"][key] == REDACTED


async def test_diagnostics_of_paired_monitor(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, credentials: CloudCredentials
) -> None:
    """No credential, identifier or address of a real entry leaks."""
    diagnostics = await async_get_config_entry_diagnostics(hass, loaded_entry)
    dumped = json.dumps(diagnostics)

    for secret in (
        credentials.key_pem.splitlines()[1],
        credentials.cert_pem.splitlines()[1],
        credentials.host,
        credentials.client_id,
        credentials.recv_topic,
        "example/send",
        FINGERPRINT_ID,
        MONITOR.address,
        "HomeAssist",
        "Front",
    ):
        assert secret not in dumped, secret
    assert diagnostics["runtime"] == {
        "connected": True,
        "ring_topic_subscribed": True,
        "station_count": 2,
        "ringing": False,
        "camera_available": False,
        "call_answering": False,
        "call_talk": False,
        "ring_snapshot": False,
        "call_state": "idle",
    }
