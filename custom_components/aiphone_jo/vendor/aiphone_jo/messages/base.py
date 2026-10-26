"""Shared envelope helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .._util import require_text
from ..credentials import CloudCredentials
from ..exceptions import InvalidCredentialsError, InvalidInputError


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """Envelope ready to be published on ``topic``.

    ``reply_mid`` is the MID of the expected answer (``None`` when no answer is
    expected). ``label`` is the message name used in logs.
    """

    topic: str = field(repr=False)
    payload: dict[str, Any] = field(repr=False)
    reply_mid: str | None
    label: str

    @property
    def mid(self) -> str:
        """MID of the envelope."""
        return str(self.payload["HEADER"]["MID"])

    @property
    def header(self) -> dict[str, Any]:
        """``HEADER`` of the envelope."""
        header: dict[str, Any] = self.payload["HEADER"]
        return header

    def encode(self) -> bytes:
        """Compact JSON (no whitespace, UTF-8, insertion order kept)."""
        return json.dumps(self.payload, ensure_ascii=False, separators=(",", ":")).encode()


@dataclass(frozen=True, slots=True)
class Identity:
    """Validated identifiers required by every cloud envelope."""

    client_id: str = field(repr=False)
    recv_topic: str = field(repr=False)
    send_topic: str = field(repr=False)
    sys: str
    term_name: str | None


def identity(credentials: CloudCredentials) -> Identity:
    """Check ``client_id``/``recv_topic``/``send_topic``."""
    try:
        return Identity(
            client_id=require_text(credentials.client_id, "client_id", 128),
            recv_topic=require_text(credentials.recv_topic, "recv_topic", 512),
            send_topic=require_text(credentials.send_topic, "send_topic", 512),
            sys=str(credentials.sys_type),
            term_name=credentials.term_name,
        )
    except InvalidInputError as err:
        raise InvalidCredentialsError(
            f"Incomplete cloud credentials (client_id/recv_topic/send_topic): {err}"
        ) from err


def require_cid(cid: str | None, what: str) -> str:
    """A session ``CID`` is mandatory (an empty CID is rejected with RSLT 400)."""
    if not cid:
        raise InvalidInputError(f"{what} requires the session CID.")
    return cid
