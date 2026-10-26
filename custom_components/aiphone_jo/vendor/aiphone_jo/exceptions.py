"""Library-specific exceptions and stable pairing failure reasons.

All exceptions derive from :class:`AiphoneJoError` so that a caller (for
example the Home Assistant integration) can catch them as a group.

Pairing failures carry machine-readable fields meant for a user interface:
:attr:`PairingError.stage`, :attr:`PairingError.reason` (a stable
:class:`PairingFailureReason` string), :attr:`PairingError.mid` (reply MID or
application error code), :attr:`PairingError.rslt` (raw result code),
:attr:`PairingError.hint` and :attr:`PairingError.retry_same_connection`.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final


class PairingStage(StrEnum):
    """Pairing step during which a failure happened."""

    DISCOVERY = "discovery"
    COMPATIBILITY = "compatibility"
    TLS = "tls"
    TRUST = "trust"
    CODE_REQUEST = "code_request"
    AUTHENTICATION = "authentication"
    CLOUD_REGISTRATION = "cloud_registration"
    TERMINAL_REGISTRATION = "terminal_registration"


class PairingFailureReason(StrEnum):
    """Stable, typed reason of a pairing failure (values never change)."""

    MONITOR_NOT_FOUND = "monitor_not_found"
    RETRY = "retry"
    REGISTRATION_IN_PROGRESS = "registration_in_progress"
    TERMINAL_LIMIT_REACHED = "terminal_limit_reached"
    CHECK_NETWORK = "check_network"
    WRONG_CODE = "wrong_code"
    CODE_EXPIRED = "code_expired"
    NAME_TOO_LONG = "name_too_long"
    NAME_ALREADY_USED = "name_already_used"
    EULA_NOT_ACCEPTED = "eula_not_accepted"
    INCOMPATIBLE_VERSION = "incompatible_version"
    TLS_FAILED = "tls_failed"
    LINK_CLOSED = "link_closed"
    FINGERPRINT_MISMATCH = "fingerprint_mismatch"
    TIMEOUT = "timeout"
    CLOUD_HTTP_ERROR = "cloud_http_error"
    CLOUD_NETWORK_ERROR = "cloud_network_error"
    CLOUD_INVALID_RESPONSE = "cloud_invalid_response"
    UNKNOWN_RESULT = "unknown_result"
    INVALID_STATE = "invalid_state"


# Application-level error codes that are not protocol MIDs:
# 70002 = the monitor's SYS type is not supported;
# 71001 = /registClient result code, i.e. the HTTP status.
APP_CODE_INCOMPATIBLE: Final = "70002"
APP_CODE_REGIST_CLIENT: Final = "71001"

_R = PairingFailureReason

# (reply MID or application code, RSLT) -> reason.
PAIRING_RSLT_REASONS: Final[Mapping[tuple[str, int], PairingFailureReason]] = MappingProxyType(
    {
        ("11001", 1): _R.RETRY,
        ("11001", 2): _R.REGISTRATION_IN_PROGRESS,
        ("11001", 3): _R.TERMINAL_LIMIT_REACHED,
        ("11002", 1): _R.RETRY,
        ("11002", 2): _R.RETRY,
        ("11002", 3): _R.CHECK_NETWORK,
        ("11002", 4): _R.EULA_NOT_ACCEPTED,
        ("11003", 1): _R.WRONG_CODE,
        ("11003", 2): _R.WRONG_CODE,
        ("11003", 3): _R.CODE_EXPIRED,
        ("11005", 1): _R.RETRY,
        ("11005", 2): _R.NAME_TOO_LONG,
        ("11005", 3): _R.NAME_ALREADY_USED,
        ("11005", 4): _R.EULA_NOT_ACCEPTED,
        (APP_CODE_INCOMPATIBLE, 1): _R.INCOMPATIBLE_VERSION,
        (APP_CODE_REGIST_CLIENT, 451): _R.EULA_NOT_ACCEPTED,
    }
)

# Errors after which the same TLS connection can be kept to retry the step:
# 11003 RSLT 2 (enter the code again) and 11005 RSLT 2 or 3 (choose another
# name). Every other error requires a new pairing connection.
RETRY_ON_SAME_CONNECTION: Final[frozenset[tuple[str, int]]] = frozenset(
    {("11003", 2), ("11005", 2), ("11005", 3)}
)

PAIRING_HINTS: Final[Mapping[PairingFailureReason, str]] = MappingProxyType(
    {
        _R.MONITOR_NOT_FOUND: (
            "no monitor answered: same LAN, 'add smartphone' mode, UDP 51711/51712"
        ),
        _R.RETRY: "retry the registration",
        _R.REGISTRATION_IN_PROGRESS: "a registration is already in progress on the monitor",
        _R.TERMINAL_LIMIT_REACHED: "the monitor cannot register more terminals",
        _R.CHECK_NETWORK: "check the network connection",
        _R.WRONG_CODE: "authentication failed: wrong code",
        _R.CODE_EXPIRED: "one-time code expired",
        _R.NAME_TOO_LONG: "terminal name too long (12 characters max)",
        _R.NAME_ALREADY_USED: "terminal name already in use",
        _R.EULA_NOT_ACCEPTED: "license agreement (EULA) not accepted on the monitor",
        _R.INCOMPATIBLE_VERSION: "unsupported monitor system type",
        _R.TLS_FAILED: "TLS connection to the monitor failed",
        _R.LINK_CLOSED: "the monitor closed the pairing connection (pairing window closed)",
        _R.FINGERPRINT_MISMATCH: "the monitor certificate differs from the approved one",
        _R.TIMEOUT: "the monitor did not answer in time",
        _R.CLOUD_HTTP_ERROR: "the Aiphone cloud rejected the registration",
        _R.CLOUD_NETWORK_ERROR: "the Aiphone cloud could not be reached",
        _R.CLOUD_INVALID_RESPONSE: "unexpected reply from the Aiphone cloud",
        _R.UNKNOWN_RESULT: "unknown result code",
        _R.INVALID_STATE: "pairing step called out of order",
    }
)


def pairing_reason(mid: str | None, rslt: Any) -> PairingFailureReason:
    """Typed reason of a ``(mid, rslt)`` pair (``UNKNOWN_RESULT`` when unknown)."""
    if mid is None or not isinstance(rslt, int) or isinstance(rslt, bool):
        return PairingFailureReason.UNKNOWN_RESULT
    return PAIRING_RSLT_REASONS.get((mid, rslt), PairingFailureReason.UNKNOWN_RESULT)


class AiphoneJoError(Exception):
    """Base error of the library."""


class InvalidInputError(AiphoneJoError, ValueError):
    """Invalid caller-supplied parameter (address, name, SDP...)."""


class ProtocolError(AiphoneJoError):
    """Unreadable or non-conforming frame received from a peer."""


class ResponseTimeoutError(AiphoneJoError, TimeoutError):
    """The expected reply was not received in time."""

    def __init__(self, message: str, *, mid: str | None = None) -> None:
        Exception.__init__(self, message)
        self.mid = mid


class PairingError(AiphoneJoError):
    """Failure during discovery or local pairing.

    ``mid`` is the reply MID (``11001``, ``11003``...) or the application error code
    (``70002``, ``71001``); ``rslt`` the raw result code; ``hint`` a short
    English explanation; ``retry_same_connection`` tells whether the same step
    can be retried without reconnecting.
    """

    default_stage: PairingStage | None = None
    default_reason: PairingFailureReason = PairingFailureReason.UNKNOWN_RESULT

    def __init__(
        self,
        message: str,
        *,
        stage: PairingStage | None = None,
        reason: PairingFailureReason | None = None,
        mid: str | None = None,
        rslt: Any = None,
        hint: str | None = None,
        retry_same_connection: bool = False,
    ) -> None:
        Exception.__init__(self, message)
        self.stage = stage or self.default_stage
        self.reason = reason or self.default_reason
        self.mid = mid
        self.rslt = rslt
        self.hint = hint if hint is not None else PAIRING_HINTS.get(self.reason)
        self.retry_same_connection = retry_same_connection


class DiscoveryError(PairingError):
    """UDP discovery failure (socket, interface...)."""

    default_stage = PairingStage.DISCOVERY
    default_reason = PairingFailureReason.MONITOR_NOT_FOUND


class NoMonitorFoundError(DiscoveryError):
    """No monitor answered the discovery request."""


class UntrustedPeerError(PairingError):
    """The monitor's TLS certificate is not the approved one.

    No application message has been sent.
    """

    default_stage = PairingStage.TRUST
    default_reason = PairingFailureReason.FINGERPRINT_MISMATCH

    def __init__(self, message: str, *, peer: Any = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.peer = peer


class PairingRejectedError(PairingError):
    """The monitor answered with a non-zero ``RSLT`` (11001, 11002, 11003, 11005)."""


class IncompatibleVersionError(PairingError):
    """The monitor's system type is not supported (error code 70002, RSLT 1)."""

    default_stage = PairingStage.COMPATIBILITY
    default_reason = PairingFailureReason.INCOMPATIBLE_VERSION


class PairingLinkClosedError(PairingError):
    """The monitor closed or refused the pairing connection (window closed or expired).

    Distinct from a TLS handshake failure (``reason == TLS_FAILED``) and from a
    timeout (:class:`PairingTimeoutError`).
    """

    default_stage = PairingStage.TLS
    default_reason = PairingFailureReason.LINK_CLOSED


class PairingTimeoutError(PairingError, ResponseTimeoutError):
    """The monitor did not answer a pairing request in time (the link is closed)."""

    default_reason = PairingFailureReason.TIMEOUT


class InvalidCredentialsError(AiphoneJoError, ValueError):
    """Invalid cloud identity (certificate, key, topics, host)."""


class CredentialsExpiredError(InvalidCredentialsError):
    """The client certificate is expired: pair again."""


class CloudApiError(AiphoneJoError):
    """Error from the Aiphone HTTPS API (``/registClient``, ``/getRoomInfo``...).

    ``kind`` is ``"http"`` (non-200 status in ``status``), ``"network"`` or
    ``"invalid_response"``.
    """

    def __init__(self, message: str, *, status: int | None = None, kind: str = "http") -> None:
        Exception.__init__(self, message)
        self.status = status
        self.kind = kind


class PairingCloudError(PairingError, CloudApiError):
    """``/registClient`` failed during pairing (error code 71001 when an HTTP status exists)."""

    default_stage = PairingStage.CLOUD_REGISTRATION

    def __init__(
        self, message: str, *, status: int | None = None, kind: str = "http", **kwargs: Any
    ) -> None:
        PairingError.__init__(self, message, **kwargs)
        self.status = status
        self.kind = kind


class CloudConnectionError(AiphoneJoError):
    """MQTT connection or subscription impossible."""


class CloudAuthError(CloudConnectionError):
    """The cloud refused this identity (revoked, deleted or unauthorized terminal).

    Raised for an MQTT CONNACK "not authorized"/"bad credentials", a TLS alert
    rejecting the client certificate, a refused subscription to the client's
    own topic, or an HTTP 401/403 from the API. Retrying will not help: pair
    again (Home Assistant: start a reauthentication). ``status`` is set for
    HTTP refusals.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        Exception.__init__(self, message)
        self.status = status


class NotConnectedError(AiphoneJoError):
    """Operation requires an active connection (local TLS or MQTT)."""


class CommandRejectedError(AiphoneJoError):
    """The entrance station or the relay rejected a command (unexpected ``RSLT``)."""

    def __init__(self, message: str, *, mid: str, rslt: Any) -> None:
        Exception.__init__(self, message)
        self.mid = mid
        self.rslt = rslt


class StationBusyError(CommandRejectedError):
    """``monitor_req`` rejected with ``RSLT 486`` (REP_BUSY).

    Typical cause: a monitoring session that was not closed, or a busy
    entrance station.
    """


class SessionError(AiphoneJoError):
    """Monitoring or call session missing, closed or already open."""
