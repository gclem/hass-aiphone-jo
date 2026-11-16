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
    ANSWERED_ELSEWHERE = "answered_elsewhere"


class CallEndReason(StrEnum):
    """Why an incoming call ended (:attr:`IncomingCall.end_reason`)."""

    ANSWERED_ELSEWHERE = "answered_elsewhere"
    """Another phone answered first (25001 naming another client, or its 24002)."""
    ENDED_BY_STATION = "ended_by_station"
    """The station hung up (DISCNCT_REQ 23003)."""
    CANCELLED_BY_STATION = "cancelled_by_station"
    """The visitor or the station abandoned the call (CALL_CANCEL_REQ 23004)."""
    EXPIRED = "expired"
    """Nobody answered before the ring timeout."""
    REJECTED = "rejected"
    """This client declined the call (23008 RSLT 603)."""
    ENDED_BY_CLIENT = "ended_by_client"
    """This client hung up (30031 then 23003)."""


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
    end_reason: CallEndReason | None = None

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
    """The status of the current call changed.

    ``reason`` is set when the call ends (same value as
    :attr:`IncomingCall.end_reason`).
    """

    call: IncomingCall
    reason: CallEndReason | None = None


@dataclass(frozen=True, slots=True)
class SessionEndedEvent:
    """A monitoring or call session ended."""

    cid: str = field(repr=False)
    by: SessionEndedBy


class SessionStatus(StrEnum):
    """Status of a monitoring session opened by this client."""

    STARTING = "starting"
    """``STA_RELAY_REQ`` / ``MNT_REQ`` sent, no answer yet."""
    ACTIVE = "active"
    """Accepted by the station (``24021`` RSLT 200)."""
    REFUSED = "refused"
    """Refused (486, 480...): never started on the station."""
    ABANDONED = "abandoned"
    """Given up before the station answered (timeout, cancellation), or lost
    with the connection: the station may still hold it."""
    CLOSED = "closed"
    """Closed by this client (``LEAVE_USER_REQ``, ``DISCNCT_REQ``)."""
    ENDED = "ended"
    """Ended by the station (``23003``), and answered."""
    CLEANED = "cleaned"
    """An unfinished session closed again later (busy station, restart)."""


UNFINISHED_SESSION_STATUSES = frozenset(
    {SessionStatus.STARTING, SessionStatus.ACTIVE, SessionStatus.ABANDONED}
)


@dataclass(slots=True)
class SessionRecord:
    """A monitoring session opened by this client, kept to finish it later.

    Store :meth:`to_storage` (it holds the session ``CID``, which contains the
    client id: keep it private) and give the records back with
    :meth:`AiphoneJoClient.restore_sessions` after a restart, so that a
    session left open on the station can still be closed.
    """

    cid: str = field(repr=False)
    term_id: int
    term_kind: int
    status: SessionStatus
    started_at: datetime
    updated_at: datetime
    cor_cli_id: str = field(default="", repr=False)
    detail: str = ""

    @property
    def tag(self) -> str:
        """Time part of the ``CID``, without the client id: safe to show."""
        return self.cid[-20:]

    @property
    def unfinished(self) -> bool:
        """True when the station may still hold the session."""
        return self.status in UNFINISHED_SESSION_STATUSES

    def to_storage(self) -> dict[str, Any]:
        """JSON-serializable form (private: contains the ``CID``)."""
        return {
            "cid": self.cid,
            "term_id": self.term_id,
            "term_kind": self.term_kind,
            "status": self.status.value,
            "started_at": self.started_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "cor_cli_id": self.cor_cli_id,
            "detail": self.detail,
        }

    @classmethod
    def from_storage(cls, data: dict[str, Any]) -> SessionRecord | None:
        """Record from :meth:`to_storage`, or ``None`` when unreadable."""
        try:
            return cls(
                cid=str(data["cid"]),
                term_id=int(data["term_id"]),
                term_kind=int(data["term_kind"]),
                status=SessionStatus(data["status"]),
                started_at=datetime.fromisoformat(data["started_at"]),
                updated_at=datetime.fromisoformat(data["updated_at"]),
                cor_cli_id=str(data.get("cor_cli_id") or ""),
                detail=str(data.get("detail") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True, slots=True)
class SessionRecordEvent:
    """A monitoring session of this client changed status (a copy, to store)."""

    record: SessionRecord


@dataclass(frozen=True, slots=True)
class MessageEvent:
    """Any valid frame received over MQTT (raw, unredacted: handle with care)."""

    topic: str = field(repr=False)
    message: dict[str, Any] = field(repr=False)


AiphoneEvent = (
    ConnectionStateEvent
    | RingEvent
    | CallUpdatedEvent
    | SessionEndedEvent
    | SessionRecordEvent
    | MessageEvent
)
