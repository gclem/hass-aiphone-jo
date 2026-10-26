"""Control envelopes: unlock, lock, out_option, get_lock_status.

The envelopes carry the client id, ``recv_topic`` and ``send_topic``, and are
published on ``send_topic``. unlock and out_option are only honoured during an
active monitoring session. lock is untested.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from .._util import seq_id
from ..const import CLOUD_VERSION, Mid
from ..credentials import CloudCredentials
from ..exceptions import InvalidInputError
from .base import Identity, OutboundMessage, identity


def _common_header(mid: str, ident: Identity, now: datetime | None) -> dict[str, Any]:
    # Shared HEADER: MID, CLIID, RSLT, RCVTPC (recv_topic as-is), VER "1.2",
    # SYS as a string.
    return {
        "MID": mid,
        "CLIID": ident.client_id,
        "RSLT": 0,
        "RCVTPC": ident.recv_topic,
        "VER": CLOUD_VERSION,
        "SYS": ident.sys,
        "SID": seq_id(now),
    }


def _main_unit_ctrl(
    mid: str, reply_mid: str, label: str, credentials: CloudCredentials, now: datetime | None
) -> OutboundMessage:
    # Main unit control messages add AVID (0) after SID and an empty BODY for
    # MIDs without a specific body (26021, 26032, 26121).
    ident = identity(credentials)
    header = _common_header(mid, ident, now)
    header["AVID"] = 0
    return OutboundMessage(ident.send_topic, {"HEADER": header, "BODY": {}}, reply_mid, label)


def build_unlock_request(
    credentials: CloudCredentials, now: datetime | None = None
) -> OutboundMessage:
    """UNLOCK_REQ (26021 -> 27021): pulse on lock output 1 (main/large opening)."""
    return _main_unit_ctrl(Mid.UNLOCK_REQ, Mid.UNLOCK_REP, "UNLOCK_REQ", credentials, now)


def build_lock_request(
    credentials: CloudCredentials, now: datetime | None = None
) -> OutboundMessage:
    """LOCK_REQ (26032 -> 27032). Untested."""
    return _main_unit_ctrl(Mid.LOCK_REQ, Mid.LOCK_REP, "LOCK_REQ", credentials, now)


def build_out_option_request(
    credentials: CloudCredentials, now: datetime | None = None
) -> OutboundMessage:
    """OUT_OPTION_REQ (26121 -> 27121): "Option Output", lock output 2 (small opening).

    Same envelope as 26021, with an empty BODY and no entrance selector.
    """
    return _main_unit_ctrl(
        Mid.OUT_OPTION_REQ, Mid.OUT_OPTION_REP, "OUT_OPTION_REQ", credentials, now
    )


def build_get_lock_status_request(
    credentials: CloudCredentials, now: datetime | None = None
) -> OutboundMessage:
    """GET_LOCK_STATUS_REQ (26031 -> 27031 ``{ENTNR, KEYINF[{KEYTP, KEYSTS}]}``).

    The request has no BODY at all and no AVID.
    """
    ident = identity(credentials)
    header = _common_header(Mid.LOCK_STATUS_REQ, ident, now)
    return OutboundMessage(
        ident.send_topic, {"HEADER": header}, Mid.LOCK_STATUS_REP, "GET_LOCK_STATUS_REQ"
    )


class ControlCommand(StrEnum):
    """Control commands supported by :func:`build_control_request`."""

    GET_LOCK_STATUS = "get_lock_status"
    UNLOCK = "unlock"
    LOCK = "lock"
    OUT_OPTION = "out_option"


@dataclass(frozen=True, slots=True)
class _ControlEntry:
    build: Any
    dangerous: bool


CONTROL_COMMANDS: dict[ControlCommand, _ControlEntry] = {
    ControlCommand.GET_LOCK_STATUS: _ControlEntry(build_get_lock_status_request, False),
    ControlCommand.UNLOCK: _ControlEntry(build_unlock_request, True),
    ControlCommand.LOCK: _ControlEntry(build_lock_request, True),
    ControlCommand.OUT_OPTION: _ControlEntry(build_out_option_request, True),
}


def build_control_request(
    command: str, credentials: CloudCredentials, now: datetime | None = None
) -> tuple[OutboundMessage, bool]:
    """Build a control command; returns the message and whether it is dangerous."""
    try:
        entry = CONTROL_COMMANDS[ControlCommand(command)]
    except ValueError as err:
        raise InvalidInputError(f"Unknown control command: {command!r}.") from err
    message: OutboundMessage = entry.build(credentials, now)
    return message, entry.dangerous
