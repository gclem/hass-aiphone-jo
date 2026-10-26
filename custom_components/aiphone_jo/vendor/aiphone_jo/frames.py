"""JSON frames of the local protocol (UDP and TLS "JSON lines")."""

from __future__ import annotations

import codecs
import json
import re
from typing import Any

from .const import LOCAL_FRAME_VERSION, MAX_FRAME_BYTES
from .exceptions import ProtocolError

_MID = re.compile(r"^\d{5}$")

Message = dict[str, Any]


def frame(mid: str, body: dict[str, Any] | None = None) -> bytes:
    """Local frame ``{"HEADER":{"MID":...,"VER":"1.0"},"BODY":...}`` terminated by ``\\n``."""
    message: Message = {"HEADER": {"MID": mid, "VER": LOCAL_FRAME_VERSION}}
    if body:
        message["BODY"] = body
    return (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def parse_frame(text: str | bytes) -> Message:
    """Decode a frame and check that ``HEADER.MID`` is a 5-digit string."""
    try:
        message = json.loads(text)
    except (ValueError, UnicodeDecodeError) as err:
        raise ProtocolError("Invalid JSON message received from the peer.") from err
    header = message.get("HEADER") if isinstance(message, dict) else None
    mid = header.get("MID") if isinstance(header, dict) else None
    if not isinstance(mid, str) or not _MID.match(mid):
        raise ProtocolError("Invalid protocol header.")
    return message  # type: ignore[no-any-return]


def header(message: Message) -> dict[str, Any]:
    """``HEADER`` of a message (empty dict when missing)."""
    value = message.get("HEADER")
    return value if isinstance(value, dict) else {}


def body(message: Message) -> dict[str, Any]:
    """``BODY`` of a message (empty dict when missing)."""
    value = message.get("BODY")
    return value if isinstance(value, dict) else {}


class JsonLinesDecoder:
    """Split a byte stream into frames, handling fragmented UTF-8 and bounded size."""

    def __init__(self, max_bytes: int = MAX_FRAME_BYTES) -> None:
        self._max_bytes = max_bytes
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._pending = ""

    def push(self, chunk: bytes) -> list[Message]:
        """Feed bytes and return the complete frames."""
        self._pending += self._decoder.decode(chunk)
        messages: list[Message] = []
        while (end := self._pending.find("\n")) != -1:
            line, self._pending = self._pending[:end], self._pending[end + 1 :]
            if len(line.encode()) > self._max_bytes:
                raise ProtocolError("Frame too large.")
            if line.strip():
                messages.append(parse_frame(line))
        if len(self._pending.encode()) > self._max_bytes:
            raise ProtocolError("Unterminated frame too large.")
        return messages
