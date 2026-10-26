"""Constants for the Aiphone JO integration."""

from __future__ import annotations

from typing import Final

from homeassistant.const import (
    CONF_ACCESS_TOKEN,
    CONF_ADDRESS,
    CONF_API_KEY,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_EMAIL,
    CONF_HOST,
    CONF_IP_ADDRESS,
    CONF_MAC,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_TOKEN,
    CONF_UNIQUE_ID,
    CONF_USERNAME,
)

DOMAIN: Final = "aiphone_jo"

MANUFACTURER: Final = "Aiphone"
MODEL: Final = "JO-1MDW"
DEFAULT_TITLE: Final = f"{MANUFACTURER} {MODEL}"

# Config entry layout (entry.data).
CONF_CREDENTIALS: Final = "credentials"
CONF_MONITOR: Final = "monitor"
CONF_STATIONS: Final = "stations"
CONF_FINGERPRINT: Final = "fingerprint"
CONF_SYSTEM_VERSION: Final = "system_version"
CONF_TERMINAL_NAME: Final = "terminal_name"
# Options (entry.options).
CONF_STATION: Final = "station"
CONF_IMAGE_REFRESH: Final = "image_refresh_interval"

DEFAULT_IMAGE_REFRESH: Final = 30
MIN_IMAGE_REFRESH: Final = 10
MAX_IMAGE_REFRESH: Final = 3600

DEFAULT_TERMINAL_NAME: Final = "HomeAssist"
MAX_TERMINAL_NAME: Final = 12
RING_TIMEOUT: Final = 60.0

EVENT_RING: Final = "ring"

# Keys that must never leave the config entry: always redacted from diagnostics
# and never logged. Extend this set whenever a new sensitive field is stored.
TO_REDACT: Final = frozenset(
    {
        CONF_ACCESS_TOKEN,
        CONF_ADDRESS,
        CONF_API_KEY,
        CONF_CLIENT_ID,
        CONF_CLIENT_SECRET,
        CONF_EMAIL,
        CONF_FINGERPRINT,
        CONF_HOST,
        CONF_IP_ADDRESS,
        CONF_MAC,
        CONF_NAME,
        CONF_PASSWORD,
        CONF_TERMINAL_NAME,
        CONF_TOKEN,
        CONF_UNIQUE_ID,
        CONF_USERNAME,
        "api_origin",
        "cert_pem",
        "certificate",
        "client_certificate",
        "endpoint",
        "otp",
        "key_pem",
        "private_key",
        "recv_topic",
        "root_ca",
        "root_ca_pem",
        "secret_key",
        "send_topic",
        "serial_number",
        "server_url",
        "term_name",
        "topic",
        "topics",
    }
)
