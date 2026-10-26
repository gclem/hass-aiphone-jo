"""Data models and events exposed by :class:`aiphone_jo.AiphoneJoClient`."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Output(StrEnum):
    """Lock outputs of the JO-1MDW.

    ``UNLOCK`` = output 1 (UNLOCK_REQ 26021) and ``OPTION`` = output 2, the
    option output (OUT_OPTION_REQ 26121). Each command sends a pulse on the
    output; what it opens depends on how the installation is wired. Both are
    only honoured during a monitoring session.

    ``MAIN`` is an alias of ``UNLOCK`` kept for compatibility (``Gate.MAIN``).
    """

    UNLOCK = "main"
    OPTION = "option"
    MAIN = "main"

    @property
    def number(self) -> int:
        """Output number: 1 for ``UNLOCK``, 2 for ``OPTION``."""
        return 1 if self is Output.UNLOCK else 2

    @classmethod
    def from_number(cls, number: int) -> Output:
        """Output 1 or 2."""
        if number == 1:
            return cls.UNLOCK
        if number == 2:
            return cls.OPTION
        raise ValueError(f"Invalid lock output {number!r}: expected 1 or 2.")

    @classmethod
    def coerce(cls, value: Output | str | int) -> Output:
        """Accept an :class:`Output`, its value (``"main"``/``"option"``) or 1/2."""
        if isinstance(value, bool):
            raise ValueError(f"Invalid lock output {value!r}.")
        if isinstance(value, int):
            return cls.from_number(value)
        return cls(value)


# Backward-compatible name: ``Gate.MAIN`` and ``Gate.OPTION`` keep working.
Gate = Output


@dataclass(frozen=True, slots=True)
class KeyInfo:
    """One ``KEYINF`` entry of 27031 (``KEYTP``/``KEYSTS`` meaning undocumented)."""

    key_type: int | None
    key_status: int | None


@dataclass(frozen=True, slots=True)
class LockStatus:
    """GET_LOCK_STATUS reply (27031 ``{ENTNR, KEYINF[{KEYTP, KEYSTS}]}``)."""

    entrance_count: int | None
    keys: tuple[KeyInfo, ...]
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> LockStatus:
        """Build from the 27031 ``BODY``."""
        keys = body.get("KEYINF")
        entries = keys if isinstance(keys, list) else []
        return cls(
            entrance_count=body.get("ENTNR") if isinstance(body.get("ENTNR"), int) else None,
            keys=tuple(
                KeyInfo(item.get("KEYTP"), item.get("KEYSTS"))
                for item in entries
                if isinstance(item, dict)
            ),
            raw=body,
        )


class CallStatus(StrEnum):
    """Status of an incoming call (ring 23001)."""

    RINGING = "ringing"
    TALKING = "talking"
    CANCELLED = "cancelled"
    ENDED = "ended"
    REJECTED = "rejected"
    EXPIRED = "expired"


ACTIVE_CALL_STATUSES = frozenset({CallStatus.RINGING, CallStatus.TALKING})


@dataclass(frozen=True, slots=True)
class CallSignalRecord:
    """Other signaling message seen on the call CID (other phones, cancel...)."""

    mid: str
    rslt: Any
    sender: str | None = field(repr=False)
    at: datetime


@dataclass(slots=True)
class IncomingCall:
    """Incoming call from an entrance station.

    **Experimental**: ring detection may not work on every installation.
    ``station_client_id`` is the station's CLIID, used later as PCLIID.
    """

    cid: str = field(repr=False)
    station_client_id: str = field(repr=False)
    display_name: str
    display_name_2: str
    call_kind: int | None
    term_kind: int | None
    term_id: int | None
    topic: str | None = field(repr=False)
    ringing_at: datetime
    status: CallStatus = CallStatus.RINGING
    ended_at: datetime | None = None
    engaged: bool = False
    signals: list[CallSignalRecord] = field(default_factory=list)

    @property
    def active(self) -> bool:
        """``True`` while ringing or talking."""
        return self.status in ACTIVE_CALL_STATUSES


class SessionEndedBy(StrEnum):
    """Who closed a monitoring/call session."""

    CLIENT = "client"
    STATION = "station"
    CONNECTION_LOST = "connection_lost"


@dataclass(frozen=True, slots=True)
class ConnectionStateEvent:
    """MQTT connection state changed.

    ``auth_failed`` is true when the cloud refused this identity
    (:class:`~aiphone_jo.exceptions.CloudAuthError`): automatic reconnection
    is stopped and the terminal must be paired again.
    """

    connected: bool
    broadcast_subscribed: bool = False
    error: str | None = None
    auth_failed: bool = False


@dataclass(frozen=True, slots=True)
class RingEvent:
    """A new incoming call (23001) was received."""

    call: IncomingCall


@dataclass(frozen=True, slots=True)
class CallUpdatedEvent:
    """The status of the current call changed."""

    call: IncomingCall


@dataclass(frozen=True, slots=True)
class SessionEndedEvent:
    """A monitoring or call session ended."""

    cid: str = field(repr=False)
    by: SessionEndedBy


@dataclass(frozen=True, slots=True)
class MessageEvent:
    """Any valid frame received over MQTT (raw, unredacted: handle with care)."""

    topic: str = field(repr=False)
    message: dict[str, Any] = field(repr=False)


AiphoneEvent = (
    ConnectionStateEvent | RingEvent | CallUpdatedEvent | SessionEndedEvent | MessageEvent
)
