"""Aiphone cloud HTTPS API: ``/registClient`` and ``/getRoomInfo``.

The :class:`aiohttp.ClientSession` is supplied by the caller (Home Assistant
requires sharing its own).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import aiohttp

from ._util import require_text, seq_id
from .const import DEFAULT_SYS_TYPE, TERM_KIND_ENTRANCE_STATION
from .credentials import CloudCredentials
from .exceptions import CloudApiError, CloudAuthError, InvalidInputError

_LOGGER = logging.getLogger(__name__)
_API_HOST = re.compile(r"^[a-z0-9.-]+\.aiphone-app\.net$", re.IGNORECASE)
REGIST_TIMEOUT = 44.0
ROOM_INFO_TIMEOUT = 20.0


def _api_url(origin: str, path: str) -> str:
    host = require_text(origin, "API origin", 253)
    if not _API_HOST.match(host):
        raise InvalidInputError(
            "API origin: an aiphone-app.net name is required, without scheme or path."
        )
    return f"https://{host}{path}"


def regist_url(origin: str) -> str:
    """``/registClient`` URL (host restricted to ``*.aiphone-app.net``)."""
    return _api_url(origin, "/registClient")


def room_info_url(origin: str) -> str:
    """``/getRoomInfo`` URL (host restricted to ``*.aiphone-app.net``)."""
    return _api_url(origin, "/getRoomInfo")


def describe_shape(value: Any, depth: int = 0) -> Any:
    """Shape of a response without its long values (certificates, topics...).

    Helps diagnosing an unexpected reply without leaking secrets.
    """
    if isinstance(value, str):
        return value if len(value) <= 12 else f"string({len(value)})"
    if not isinstance(value, (dict, list)):
        return value
    if depth > 4:
        return "..."
    if isinstance(value, list):
        return [describe_shape(item, depth + 1) for item in value[:5]]
    return {key: describe_shape(item, depth + 1) for key, item in value.items()}


def build_regist_body(
    *,
    otp: str,
    system_version: str | None = None,
    sys_type: int = DEFAULT_SYS_TYPE,
    os_kind: int = 1,
    now: datetime | None = None,
) -> dict[str, Any]:
    """``/registClient`` body.

    No ``CLIID`` is sent; ``SYSVER`` is the ``SYSVER`` of the 11001 discovery
    reply and ``OTPASS`` the code received from the monitor in 11002. Numbers
    are serialized as strings.
    """
    body: dict[str, Any] = {
        "OTPASS": require_text(otp, "OTPASS", 128),
        "OSKIND": str(os_kind),
        "SID": seq_id(now),
        "SYS": str(sys_type),
    }
    if system_version:
        body["SYSVER"] = require_text(system_version, "SYSVER", 128)
    return {"BODY": body}


def parse_regist_response(payload: Any) -> dict[str, Any]:
    """Convert the ``/registClient`` reply to the :meth:`CloudCredentials.parse` format.

    ``URL``/``CERT``/``SECKEY`` are read flat from ``BODY``, or from an ``IOT``
    object when present.
    """
    body = payload.get("BODY") if isinstance(payload, dict) else None
    if not isinstance(body, dict):
        raise CloudApiError("/registClient reply without BODY.", kind="invalid_response")
    iot = body.get("IOT") or body.get("IoT") or body.get("iot") or body
    result = {
        "server_url": iot.get("URL"),
        "client_certificate": iot.get("CERT"),
        "secret_key": iot.get("SECKEY"),
        "client_id": body.get("CLIID"),
        "recv_topic": body.get("RCVTPC"),
        "send_topic": body.get("SNDTPC"),
        "root_ca": body.get("SRVROOTCA"),
        "api_origin": body.get("SRVURL"),
        "system_version": body.get("SYSVER"),
    }
    required = ("server_url", "client_certificate", "secret_key", "recv_topic")
    if not all(result[key] for key in required):
        shape = json.dumps(describe_shape(payload), ensure_ascii=False)
        raise CloudApiError(
            f"Incomplete /registClient reply (URL/CERT/SECKEY/RCVTPC). Received shape: {shape}",
            kind="invalid_response",
        )
    return result


_AUTH_STATUSES = frozenset({401, 403})


async def _post(
    session: aiohttp.ClientSession,
    url: str,
    payload: dict[str, Any],
    timeout: float,
    name: str,
    *,
    accept: frozenset[int] = frozenset({200}),
    identity_required: bool = False,
) -> tuple[int, Any]:
    """POST a JSON body (no custom header); return (status, body).

    Only HTTP 200 carries a usable reply. With ``identity_required``, 401/403
    mean the cloud refused this terminal.
    """
    try:
        async with session.post(
            url,
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
            headers={"Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            status = response.status
            if identity_required and status in _AUTH_STATUSES:
                raise CloudAuthError(
                    f"{name} refused this terminal (HTTP {status}).", status=status
                )
            if status not in accept:
                raise CloudApiError(f"{name} answered HTTP {status}.", status=status)
            if status != 200:
                return status, None
            try:
                return status, await response.json(content_type=None)
            except ValueError as err:
                raise CloudApiError(
                    f"{name}: invalid JSON reply.", status=status, kind="invalid_response"
                ) from err
    except (CloudApiError, CloudAuthError):
        raise
    except (aiohttp.ClientError, TimeoutError, OSError) as err:
        raise CloudApiError(f"{name}: {type(err).__name__} {err}".strip(), kind="network") from err


async def regist_client(
    session: aiohttp.ClientSession,
    *,
    otp: str,
    origin: str,
    system_version: str | None = None,
    sys_type: int = DEFAULT_SYS_TYPE,
    term_name: str | None = None,
    timeout: float = REGIST_TIMEOUT,
) -> CloudCredentials:
    """Exchange the monitor OTP for an MQTT identity (``/registClient``)."""
    body = build_regist_body(otp=otp, system_version=system_version, sys_type=sys_type)
    _, payload = await _post(session, regist_url(origin), body, timeout, "/registClient")
    params = parse_regist_response(payload)
    params["api_origin"] = params["api_origin"] or origin
    params["sys_type"] = sys_type
    params["system_version"] = params["system_version"] or system_version
    if term_name:
        params["term_name"] = term_name
    return CloudCredentials.parse(params)


@dataclass(frozen=True, slots=True)
class Terminal:
    """Terminal known to the installation (entrance station, monitor...), from ``/getRoomInfo``."""

    id: int | None
    term_kind: int | None
    name: str | None
    camera: bool
    ent_key_type: int | None
    term_prop: int | None = None
    proc_flg: int | None = None

    @property
    def is_camera_station(self) -> bool:
        """Entrance station with a camera (``CMR_STS == 1`` and ``TERM_KIND == 5``)."""
        return self.camera and self.term_kind == TERM_KIND_ENTRANCE_STATION

    @property
    def ent_key_type_label(self) -> str:
        """Reading of ``ENT_KY_TP`` (key type of the entrance)."""
        return describe_ent_key_type(self.ent_key_type)


@dataclass(frozen=True, slots=True)
class RoomInfo:
    """``/getRoomInfo`` result."""

    terminals: tuple[Terminal, ...]
    term_inf_num: int
    alm_sts: Any = None
    sec_alm_sp: Any = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def camera_terminals(self) -> tuple[Terminal, ...]:
        """Every entrance station with a camera (a site may have several)."""
        return camera_terminals(self.terminals)


_ENT_KEY_LABELS = {1: "lockable", 2: "unlockable"}


def describe_ent_key_type(value: int | None) -> str:
    """Label of ``ENT_KY_TP``."""
    if value is None:
        return "unknown"
    return _ENT_KEY_LABELS.get(value, f"undocumented value ({value})")


def _num(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_room_info_body(
    credentials: CloudCredentials, now: datetime | None = None
) -> dict[str, Any]:
    """Body ``{BODY:{CLIID, SYS, SID}}``, without OTP."""
    return {
        "BODY": {
            "CLIID": require_text(credentials.client_id, "client_id", 128),
            "SYS": str(credentials.sys_type),
            "SID": seq_id(now),
        }
    }


def parse_room_info_response(payload: Any) -> RoomInfo:
    """Parse ``/getRoomInfo``.

    ``BODY.MGMT`` holds parallel arrays (``ID``, ``KIND``, ``NAME`` =
    [[name1, name2], ...], ``CAMERA``, ``KEY``, ``PROP``, ``PROCFLG``). A
    ``TERM_INF`` table keyed by terminal id is also accepted.
    """
    body = payload.get("BODY") if isinstance(payload, dict) else None
    if not isinstance(body, dict):
        raise CloudApiError("/getRoomInfo reply without BODY.", kind="invalid_response")
    mgmt = body.get("MGMT") if isinstance(body.get("MGMT"), dict) else None
    terminals: list[Terminal] = []
    if mgmt is not None and isinstance(mgmt.get("ID"), list):

        def column(name: str, index: int) -> Any:
            values = mgmt.get(name)
            return values[index] if isinstance(values, list) and index < len(values) else None

        for index, terminal_id in enumerate(mgmt["ID"]):
            name = column("NAME", index)
            terminals.append(
                Terminal(
                    id=_num(terminal_id),
                    term_kind=_num(column("KIND", index)),
                    name=(name[0] if name else None) if isinstance(name, list) else name,
                    camera=_num(column("CAMERA", index)) == 1,
                    ent_key_type=_num(column("KEY", index)),
                    term_prop=_num(column("PROP", index)),
                    proc_flg=_num(column("PROCFLG", index)),
                )
            )
    else:
        raw_term_inf = body.get("TERM_INF")
        term_inf: dict[str, Any] = raw_term_inf if isinstance(raw_term_inf, dict) else {}
        for terminal_id, entry in term_inf.items():
            item = entry if isinstance(entry, dict) else {}
            terminals.append(
                Terminal(
                    id=_num(terminal_id),
                    term_kind=_num(item.get("TERM_KIND")),
                    name=item.get("TERM_NAME"),
                    camera=item.get("CMR_STS") == 1,
                    ent_key_type=_num(item.get("ENT_KY_TP")),
                    term_prop=_num(item.get("TERM_PROP")),
                    proc_flg=_num(item.get("PROC_FLG")),
                )
            )
    term_inf_num = body.get("TERM_INF_NUM")
    return RoomInfo(
        terminals=tuple(terminals),
        term_inf_num=term_inf_num if isinstance(term_inf_num, int) else len(terminals),
        alm_sts=body.get("ALM_STS", body.get("ALMSTS")),
        sec_alm_sp=body.get("SEC_ALM_SP", body.get("SECSET")),
        raw=payload,
    )


def camera_terminals(terminals: tuple[Terminal, ...] | list[Terminal]) -> tuple[Terminal, ...]:
    """Entrance stations with a camera, in ``/getRoomInfo`` order.

    A site may have several; one of them can be busy (``RSLT 486``) while
    another accepts monitoring, so the full list is exposed.
    """
    return tuple(terminal for terminal in terminals if terminal.is_camera_station)


async def get_room_info(
    session: aiohttp.ClientSession,
    credentials: CloudCredentials,
    *,
    origin: str | None = None,
    timeout: float = ROOM_INFO_TIMEOUT,
) -> RoomInfo:
    """List the installation terminals (read-only, no hardware effect)."""
    url = room_info_url(origin or credentials.api_origin)
    _, payload = await _post(
        session,
        url,
        build_room_info_body(credentials),
        timeout,
        "/getRoomInfo",
        identity_required=True,
    )
    return parse_room_info_response(payload)


def unregister_url(origin: str) -> str:
    """``/deleteClientInfo`` URL (host restricted to ``*.aiphone-app.net``)."""
    return _api_url(origin, "/deleteClientInfo")


def build_unregister_body(
    client_id: str, sys_type: int, now: datetime | None = None
) -> dict[str, Any]:
    """Body of ``/deleteClientInfo``: ``{"BODY":{"CLIID", "SID", "SYS"}}``.

    ``CLIID`` is the client to delete, then ``SID`` (timestamp) and ``SYS`` (as
    a string). No ``HEADER``, no OTP: like ``/getRoomInfo``, the request is
    identified by the client id only.
    """
    return {
        "BODY": {
            "CLIID": require_text(client_id, "client_id", 128),
            "SID": seq_id(now),
            "SYS": str(sys_type),
        }
    }


_ALREADY_DELETED_STATUSES = frozenset({404, 410})


async def unregister(
    session: aiohttp.ClientSession,
    credentials: CloudCredentials,
    *,
    origin: str | None = None,
    timeout: float = REGIST_TIMEOUT,
) -> bool:
    """Delete this terminal from the Aiphone cloud (``/deleteClientInfo``).

    **Experimental.** Sends ``POST https://<api origin>/deleteClientInfo`` with
    this terminal's client id. The expected reply is HTTP 200 with
    ``BODY.CLIID`` (string). Returns ``True`` when the server accepted the
    deletion (HTTP 200) and ``False`` when the terminal is already absent
    (HTTP 404 or 410), which makes the call idempotent.
    Raises :class:`CloudAuthError` on HTTP 401/403 and :class:`CloudApiError`
    otherwise. The local monitor may still list the terminal: removing it there
    is done from the monitor's menu.
    """
    url = unregister_url(origin or credentials.api_origin)
    status, payload = await _post(
        session,
        url,
        build_unregister_body(credentials.client_id, credentials.sys_type),
        timeout,
        "/deleteClientInfo",
        accept=frozenset({200}) | _ALREADY_DELETED_STATUSES,
        identity_required=True,
    )
    if status in _ALREADY_DELETED_STATUSES:
        return False
    reply = payload.get("BODY") if isinstance(payload, dict) else None
    if not isinstance(reply, dict) or not isinstance(reply.get("CLIID"), str):
        _LOGGER.warning("/deleteClientInfo: HTTP 200 without BODY.CLIID")
    return True
