"""Relay family envelopes (MID 30001-30031, replies 31xxx).

The relay is a standard Janus gateway (WebRTC).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from .._util import create_call_id, seq_id
from ..const import CLOUD_VERSION, Mid
from ..credentials import CloudCredentials
from ..exceptions import InvalidInputError
from .base import OutboundMessage, identity

MAX_SDP_LENGTH = 16000


class RelayCommand(StrEnum):
    """Relay commands."""

    STA_RELAY = "sta_relay"
    ADD_RCV_USER = "add_rcv_user"
    UPDATE_RCV_USER = "update_rcv_user"
    ADD_SND_USER = "add_snd_user"
    LEAVE_USER = "leave_user"


@dataclass(frozen=True, slots=True)
class _RelayEntry:
    mid: str
    reply_mid: str
    sdp: bool
    label: str


RELAY_COMMANDS: dict[RelayCommand, _RelayEntry] = {
    RelayCommand.STA_RELAY: _RelayEntry(
        Mid.STA_RELAY_REQ, Mid.STA_RELAY_REP, True, "STA_RELAY_REQ"
    ),
    RelayCommand.ADD_RCV_USER: _RelayEntry(
        Mid.ADD_RCV_USER_REQ, Mid.ADD_RCV_USER_REP, False, "ADD_RCV_USER_REQ"
    ),
    RelayCommand.UPDATE_RCV_USER: _RelayEntry(
        Mid.UPDATE_RCV_USER_REQ, Mid.UPDATE_RCV_USER_REP, True, "UPDATE_RCV_USER_REQ"
    ),
    RelayCommand.ADD_SND_USER: _RelayEntry(
        Mid.ADD_SND_USER_REQ, Mid.ADD_SND_USER_REP, True, "ADD_SND_USER_REQ"
    ),
    RelayCommand.LEAVE_USER: _RelayEntry(
        Mid.LEAVE_USER_REQ, Mid.LEAVE_USER_REP, False, "LEAVE_USER_REQ"
    ),
}


def build_relay_request(
    command: str,
    credentials: CloudCredentials,
    *,
    cid: str | None = None,
    cor_cli_id: str = "",
    sdp: str | None = None,
    rls: int = 0,
    now: datetime | None = None,
) -> OutboundMessage:
    """Relay request. HEADER order: MID, CID, CLIID, PCLIID,
    RSLT, RCVTPC, RLS, VER, SYS, SID; ``BODY.SDP`` only for 30001/30012/30021.

    A missing ``cid`` is only allowed for ``sta_relay``: a fresh one is then
    created (a new session must never reuse an old CID).
    """
    ident = identity(credentials)
    try:
        entry = RELAY_COMMANDS[RelayCommand(command)]
    except ValueError as err:
        raise InvalidInputError(f"Unknown relay command: {command!r}.") from err
    if not cid:
        if entry.mid != Mid.STA_RELAY_REQ:
            raise InvalidInputError(f"{command} requires the relay session CID.")
        cid = create_call_id(ident.client_id, now)
    header: dict[str, Any] = {
        "MID": entry.mid,
        "CID": cid,
        "CLIID": ident.client_id,
        "PCLIID": cor_cli_id,
        "RSLT": 0,
        "RCVTPC": ident.recv_topic,
        "RLS": str(rls),
        "VER": CLOUD_VERSION,
        "SYS": ident.sys,
        "SID": seq_id(now),
    }
    body: dict[str, Any] = {}
    if entry.sdp:
        if not isinstance(sdp, str) or not sdp.strip() or len(sdp) > MAX_SDP_LENGTH:
            raise InvalidInputError(f"{command} requires a valid SDP.")
        body["SDP"] = sdp
    return OutboundMessage(
        ident.send_topic, {"HEADER": header, "BODY": body}, entry.reply_mid, entry.label
    )
