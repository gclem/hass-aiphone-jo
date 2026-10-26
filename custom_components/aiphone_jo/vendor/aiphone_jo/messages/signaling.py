"""Signaling envelopes (23xxx/24xxx): monitor_req, disconnect, call signals.

The incoming call signals (24001/24002/23008/24004) are experimental.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from .._util import require_text, seq_id
from ..const import CLOUD_VERSION, DEFAULT_TERMINAL_NAME, DISCONNECT_REASON_NORMAL, Mid
from ..credentials import CloudCredentials
from ..exceptions import InvalidInputError
from .base import OutboundMessage, identity, require_cid


def _display_name(credentials: CloudCredentials) -> str:
    return require_text(credentials.term_name or DEFAULT_TERMINAL_NAME, "DSP1", 12)


def build_monitor_request(
    credentials: CloudCredentials,
    term_id: int,
    term_kind: int,
    cid: str,
    now: datetime | None = None,
) -> OutboundMessage:
    """MNT_REQ (23021 -> 24021): start monitoring an entrance station.

    CKIND=1 (monitor), DSP1 = local terminal name, TMID/TMKIND = target station
    from ``/getRoomInfo``. ``cid`` is the CID created for STA_RELAY_REQ (30001)
    and reused for the whole session; replies with another CID belong to
    another session.
    **Triggers a real monitoring call on the entrance station.**
    """
    ident = identity(credentials)
    if isinstance(term_id, bool) or not isinstance(term_id, int) or term_id <= 0:
        raise InvalidInputError("Invalid nTermId: fetch room info to find the camera station.")
    if isinstance(term_kind, bool) or not isinstance(term_kind, int) or term_kind <= 0:
        raise InvalidInputError("Invalid nTermKind: fetch room info to find the camera station.")
    header: dict[str, Any] = {
        "MID": Mid.MONITOR_REQ,
        "CLIID": ident.client_id,
        "CID": require_cid(cid, "monitor_req"),
        "PCLIID": "",
        "NCLIID": "",
        "DSP1": _display_name(credentials),
        "DSP2": "",
        "CKIND": 1,
        "TMKIND": term_kind,
        "TMID": term_id,
        "RECKIND": 0,
        "AVID": 0,
        "RSLT": 0,
        "SNDTPC": ident.send_topic,
        "RCVTPC": ident.recv_topic,
        "VER": CLOUD_VERSION,
        "SYS": ident.sys,
        "SID": seq_id(now),
    }
    return OutboundMessage(
        ident.send_topic, {"HEADER": header, "BODY": {}}, Mid.MONITOR_REP, "MNT_REQ"
    )


def build_disconnect_request(
    credentials: CloudCredentials, cid: str, now: datetime | None = None
) -> OutboundMessage:
    """DISCNCT_REQ (23003 -> 24003, REASON 16): close the session on the station side.

    Sent after LEAVE_USER_REQ (30031); without it the station keeps the session
    open and answers 486 to the next monitor_req. REASON is always present;
    TMKIND/TMID are omitted when zero.
    """
    ident = identity(credentials)
    header: dict[str, Any] = {
        "MID": Mid.DISCONNECT_REQ,
        "CLIID": ident.client_id,
        "CID": require_cid(cid, "DISCNCT_REQ"),
        "PCLIID": "",
        "NCLIID": "",
        "DSP1": _display_name(credentials),
        "DSP2": "",
        "CKIND": 1,
        "RECKIND": 0,
        "AVID": 0,
        "RSLT": 0,
        "REASON": DISCONNECT_REASON_NORMAL,
        "SNDTPC": ident.send_topic,
        "RCVTPC": ident.recv_topic,
        "VER": CLOUD_VERSION,
        "SYS": ident.sys,
        "SID": seq_id(now),
    }
    return OutboundMessage(
        ident.send_topic, {"HEADER": header, "BODY": {}}, Mid.DISCONNECT_REP, "DISCNCT_REQ"
    )


def build_disconnect_reply(
    credentials: CloudCredentials, cid: str, now: datetime | None = None
) -> OutboundMessage:
    """DISCNCT_REP (24003, RSLT 200, REASON 0): answer a 23003 sent by the station.

    The station closes monitoring by itself ~60 s after monitor_req.
    """
    request = build_disconnect_request(credentials, cid, now)
    header = dict(request.header)
    header.update({"MID": Mid.DISCONNECT_REP, "RSLT": 200, "REASON": 0})
    return OutboundMessage(request.topic, {"HEADER": header, "BODY": {}}, None, "DISCNCT_REP")


class CallSignal(StrEnum):
    """Incoming call signals (experimental)."""

    READY = "ready"
    REPLY = "reply"
    START = "start"
    REJECT = "reject"
    CANCEL_REPLY = "cancel_reply"


@dataclass(frozen=True, slots=True)
class _CallSignalEntry:
    mid: str
    rslt: int
    reply_mid: str | None
    label: str
    call_kind: int | None = None
    reason: int = 0


# Answer sequence: 30011/30012 (video) -> 24001 READY_NFY; 30021 (microphone)
# -> 24002 REPLY RSLT 200 -> 25001 REP_RCV_NFY; pick up -> 23008 CALL_STA_REQ
# RSLT 200 -> 24008. Decline: 23008 RSLT 603 then LEAVE_USER. Station cancels
# (23004) while engaged: 24004 RSLT 200 REASON 16 then LEAVE_USER.
CALL_SIGNALS: dict[CallSignal, _CallSignalEntry] = {
    CallSignal.READY: _CallSignalEntry(Mid.READY_NFY, 0, None, "READY_NFY", call_kind=0),
    CallSignal.REPLY: _CallSignalEntry(Mid.REPLY, 200, Mid.REP_RCV_NFY, "REPLY"),
    CallSignal.START: _CallSignalEntry(Mid.CALL_STA_REQ, 200, Mid.CALL_STA_REP, "CALL_STA_REQ"),
    CallSignal.REJECT: _CallSignalEntry(Mid.CALL_STA_REQ, 603, None, "CALL_STA_REQ (decline)"),
    CallSignal.CANCEL_REPLY: _CallSignalEntry(
        Mid.CALL_CANCEL_REP, 200, None, "CALL_CANCEL_REP", reason=DISCONNECT_REASON_NORMAL
    ),
}


def build_call_signal(
    credentials: CloudCredentials,
    signal: str,
    cid: str,
    call_kind: int | None = 0,
    now: datetime | None = None,
) -> OutboundMessage:
    """Incoming call signal, same envelope as 23003.

    PCLIID/NCLIID/DSP1/DSP2 are empty; CKIND is the CKIND of the 23001 ring,
    except for READY_NFY (CKIND 0).
    """
    try:
        entry = CALL_SIGNALS[CallSignal(signal)]
    except ValueError as err:
        raise InvalidInputError(f"Unknown call signal: {signal!r}.") from err
    request = build_disconnect_request(credentials, cid, now)
    header = dict(request.header)
    kind = entry.call_kind if entry.call_kind is not None else call_kind
    header.update(
        {
            "MID": entry.mid,
            "DSP1": "",
            "CKIND": kind if isinstance(kind, int) and not isinstance(kind, bool) else 0,
            "RSLT": entry.rslt,
            "REASON": entry.reason,
        }
    )
    return OutboundMessage(
        request.topic, {"HEADER": header, "BODY": {}}, entry.reply_mid, entry.label
    )
