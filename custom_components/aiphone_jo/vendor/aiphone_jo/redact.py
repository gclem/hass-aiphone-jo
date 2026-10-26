"""Masking of secrets and identifiers before logging.

Masks sensitive keys and PEM blocks, bounds lengths and depth, and offers an
``identifiers`` mode that also masks the cloud identity
(``CLIID``, topics, ``CID``...): Home Assistant logs are often shared publicly.
"""

from __future__ import annotations

import re
from typing import Any

MASK = "[masked]"
PEM_MASK = "[PEM masked]"

_SECRET_FIELD = re.compile(
    r"OTPASS|password|private.?key|secret|certificate|client.?cert|root.?ca|^key$|^cert$|^ca$"
    r"|token|pwinfo|^SECKEY$|^CERT$|^SRVROOTCA$|_pem$",
    re.IGNORECASE,
)
_IDENTIFIER_FIELD = re.compile(
    r"^(CLIID|PCLIID|NCLIID|CID|RCVTPC|SNDTPC|ALLRCVTPC|TERMTPC|client_?id|recv_?topic"
    r"|send_?topic|broadcast_?topic|topic|cid|cor_?cli_?id|station_?client_?id|SDP|sdp"
    r"|URL|SRVURL|server_?url|host)$",
    re.IGNORECASE,
)
_PEM = re.compile(r"-----BEGIN [^-]*(?:PRIVATE KEY|CERTIFICATE)-----")
_MAX_DEPTH = 12
_MAX_ITEMS = 100
_MAX_STRING = 2000


def redact(value: Any, *, identifiers: bool = False, _depth: int = 0) -> Any:
    """Copy of ``value`` without secrets (and without identifiers if ``identifiers``)."""
    if _depth > _MAX_DEPTH:
        return "[depth limited]"
    if isinstance(value, (list, tuple)):
        return [
            redact(item, identifiers=identifiers, _depth=_depth + 1)
            for item in list(value)[:_MAX_ITEMS]
        ]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:_MAX_ITEMS]:
            name = str(key)
            if _SECRET_FIELD.search(name) or (identifiers and _IDENTIFIER_FIELD.search(name)):
                result[name] = MASK if item not in (None, "") else item
            else:
                result[name] = redact(item, identifiers=identifiers, _depth=_depth + 1)
        return result
    if isinstance(value, str):
        if _PEM.search(value):
            return PEM_MASK
        return f"{value[:_MAX_STRING]} [truncated]" if len(value) > _MAX_STRING else value
    return value
