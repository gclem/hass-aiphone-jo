"""Translate library errors into actionable config flow errors."""

from __future__ import annotations

from dataclasses import dataclass, field

from .vendor.aiphone_jo import (
    DiscoveryError,
    NoMonitorFoundError,
    PairingError,
    PairingFailureReason,
)

_R = PairingFailureReason

# Library failure reason -> form error key (strings.json "config.error").
REASON_KEYS: dict[PairingFailureReason, str] = {
    _R.MONITOR_NOT_FOUND: "no_monitor_found",
    _R.RETRY: "retry",
    _R.REGISTRATION_IN_PROGRESS: "registration_in_progress",
    _R.TERMINAL_LIMIT_REACHED: "smartphone_limit",
    _R.CHECK_NETWORK: "check_network",
    _R.WRONG_CODE: "invalid_code",
    _R.CODE_EXPIRED: "code_expired",
    _R.NAME_TOO_LONG: "name_too_long",
    _R.NAME_ALREADY_USED: "name_taken",
    _R.EULA_NOT_ACCEPTED: "eula_not_accepted",
    _R.INCOMPATIBLE_VERSION: "incompatible_version",
    _R.TLS_FAILED: "cannot_connect",
    _R.LINK_CLOSED: "link_closed",
    _R.FINGERPRINT_MISMATCH: "fingerprint_mismatch",
    _R.TIMEOUT: "timeout",
    _R.CLOUD_HTTP_ERROR: "cloud_error",
    _R.CLOUD_NETWORK_ERROR: "cloud_unreachable",
    _R.CLOUD_INVALID_RESPONSE: "cloud_invalid_response",
    _R.UNKNOWN_RESULT: "monitor_rejected",
    _R.INVALID_STATE: "unknown",
}


@dataclass(frozen=True, slots=True)
class FlowError:
    """Error key shown in a form, with its placeholders."""

    key: str
    placeholders: dict[str, str] = field(default_factory=dict)


def raw_code(err: BaseException) -> str:
    """Raw, secret-free description of an error (type, MID, RSLT, HTTP status)."""
    parts = [type(err).__name__]
    if (mid := getattr(err, "mid", None)) is not None:
        parts.append(f"MID {mid}")
    if (rslt := getattr(err, "rslt", None)) is not None:
        parts.append(f"RSLT {rslt}")
    if (status := getattr(err, "status", None)) is not None and status != rslt:
        parts.append(f"HTTP {status}")
    return ", ".join(parts)


def _caused_by_timeout(err: BaseException) -> bool:
    cause = err.__cause__
    while cause is not None:
        if isinstance(cause, TimeoutError):
            return True
        cause = cause.__cause__
    return False


def flow_error(err: BaseException) -> FlowError:
    """Map a library exception to a form error."""
    placeholders = {"code": raw_code(err)}
    if isinstance(err, DiscoveryError) and not isinstance(err, NoMonitorFoundError):
        return FlowError("discovery_failed", {"code": str(err) or raw_code(err)})
    if isinstance(err, PairingError):
        key = REASON_KEYS.get(err.reason, "unknown")
        if key == "cloud_unreachable" and _caused_by_timeout(err):
            key = "cloud_timeout"
        return FlowError(key, placeholders)
    return FlowError("unknown", placeholders)
