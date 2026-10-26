"""Internal helpers: ``SID``/``CID`` timestamps and input validation."""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime

from .exceptions import InvalidInputError

_CONTROL_CHARS = re.compile(r"[\x00-\x1f]")
_HEX64 = re.compile(r"^[a-f0-9]{64}$")
_PRIVATE_NETWORKS = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
)


def seq_id(now: datetime | None = None) -> str:
    """Timestamp ``%04d%02d%02d%02d%02d%02d%06ld`` (local time, microseconds).

    Format of ``SID`` and of the ``CID`` suffix.
    """
    moment = now or datetime.now()
    return f"{moment:%Y%m%d%H%M%S}{moment.microsecond:06d}"


def create_call_id(client_id: str, now: datetime | None = None) -> str:
    """Session ``CID``: the client id concatenated with :func:`seq_id`, no separator.

    An empty CID is rejected by the server (RSLT 400), so it is never empty.
    """
    return f"{require_text(client_id, 'client_id', 128)}{seq_id(now)}"


def require_text(value: object, name: str, max_length: int = 256) -> str:
    """Non-empty, bounded string without control characters."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > max_length
        or _CONTROL_CHARS.search(value)
    ):
        raise InvalidInputError(f"Invalid {name} field.")
    return value.strip()


def private_address(value: object) -> str:
    """IPv4 address of a private network (10/8, 172.16/12, 192.168/16) only."""
    if not isinstance(value, str):
        raise InvalidInputError("A private IPv4 address is required.")
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError as err:
        raise InvalidInputError("A private IPv4 address is required.") from err
    if not any(address in network for network in _PRIVATE_NETWORKS):
        raise InvalidInputError("Only private network addresses are accepted.")
    return value


def valid_port(value: object) -> int:
    """Valid TCP/UDP port (1-65535)."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise InvalidInputError("Invalid port.")
    return value


def normalize_fingerprint(value: object) -> str:
    """SHA-256 fingerprint as lowercase hex without separators."""
    result = require_text(value, "SHA-256 fingerprint", 95).replace(":", "").lower()
    if not _HEX64.match(result):
        raise InvalidInputError("Invalid SHA-256 fingerprint.")
    return result


def format_fingerprint(digest: bytes) -> str:
    """Fingerprint formatted as ``AB:CD:...`` (uppercase hex, colon separated)."""
    return ":".join(f"{byte:02X}" for byte in digest)
