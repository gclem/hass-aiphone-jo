"""JO-1MDW protocol constants."""

from __future__ import annotations

from typing import Final

# LAN discovery.
DISCOVERY_SEND_PORT: Final = 51711
DISCOVERY_RECEIVE_PORT: Final = 51712
DISCOVERY_TIMEOUT: Final = 3.5
LIMITED_BROADCAST: Final = "255.255.255.255"

# Local frames: HEADER.VER of TLS/UDP frames.
LOCAL_FRAME_VERSION: Final = "1.0"
MAX_FRAME_BYTES: Final = 16384

# Cloud envelopes.
CLOUD_VERSION: Final = "1.2"
DEFAULT_SYS_TYPE: Final = 4
DEFAULT_TERMINAL_NAME: Final = "aiphone-jo"
MAX_TERMINAL_NAME: Final = 12

# HTTPS API and MQTT.
DEFAULT_API_ORIGIN: Final = "api-r1.jo.aiphone-app.net"
DEFAULT_MQTT_PORT: Final = 8883
ALLOWED_MQTT_PORTS: Final = (443, 8883)
AWS_IOT_ALPN: Final = "x-amzn-mqtt-ca"
MQTT_KEEPALIVE: Final = 30
MQTT_CONNECT_TIMEOUT: Final = 10.0

# Timeouts: MQTT replies, session teardown steps, local TLS exchanges, 01005.
LOCAL_REQUEST_TIMEOUT: Final = 6.0
TERMINAL_REGISTER_TIMEOUT: Final = 60.0
MQTT_REPLY_TIMEOUT: Final = 13.0
END_SESSION_STEP_TIMEOUT: Final = 5.0
CALL_RING_TIMEOUT: Final = 60.0


class Mid:
    """MIDs used by the library (5-digit strings)."""

    # Local (UDP / TLS)
    DISCOVERY_REQ: Final = "01001"
    DISCOVERY_REP: Final = "11001"
    OTP_REQ: Final = "01002"
    OTP_REP: Final = "11002"
    AUTH_REQ: Final = "01003"
    AUTH_REP: Final = "11003"
    TERMINAL_REQ: Final = "01005"
    TERMINAL_REP: Final = "11005"

    # Signaling (23xxx / 24xxx / 25001)
    CALL_REQ: Final = "23001"
    DISCONNECT_REQ: Final = "23003"
    CALL_CANCEL_REQ: Final = "23004"
    CALL_STA_REQ: Final = "23008"
    MONITOR_REQ: Final = "23021"
    READY_NFY: Final = "24001"
    REPLY: Final = "24002"
    DISCONNECT_REP: Final = "24003"
    CALL_CANCEL_REP: Final = "24004"
    CALL_STA_REP: Final = "24008"
    MONITOR_REP: Final = "24021"
    REP_RCV_NFY: Final = "25001"

    # Control (26xxx / 27xxx)
    UNLOCK_REQ: Final = "26021"
    UNLOCK_REP: Final = "27021"
    LOCK_STATUS_REQ: Final = "26031"
    LOCK_STATUS_REP: Final = "27031"
    LOCK_REQ: Final = "26032"
    LOCK_REP: Final = "27032"
    OUT_OPTION_REQ: Final = "26121"
    OUT_OPTION_REP: Final = "27121"

    # Relay (30xxx / 31xxx)
    STA_RELAY_REQ: Final = "30001"
    STA_RELAY_REP: Final = "31001"
    ADD_RCV_USER_REQ: Final = "30011"
    ADD_RCV_USER_REP: Final = "31011"
    UPDATE_RCV_USER_REQ: Final = "30012"
    UPDATE_RCV_USER_REP: Final = "31012"
    ADD_SND_USER_REQ: Final = "30021"
    ADD_SND_USER_REP: Final = "31021"
    LEAVE_USER_REQ: Final = "30031"
    LEAVE_USER_REP: Final = "31031"


# Result codes of the signaling family reuse the SIP table (PROTOCOL.md §12ter).
RSLT_OK: Final = 200
RSLT_BUSY: Final = 486
RSLT_DECLINE: Final = 603
# Disconnect reason 16: normal termination.
DISCONNECT_REASON_NORMAL: Final = 16

# TERM_KIND of an entrance station (a camera station also has CMR_STS == 1).
TERM_KIND_ENTRANCE_STATION: Final = 5

# Monitor system types supported by this library (``SYS`` of 11001). Other
# values are rejected with error code 70002.
SUPPORTED_SYS_TYPES: Final = frozenset({DEFAULT_SYS_TYPE})
