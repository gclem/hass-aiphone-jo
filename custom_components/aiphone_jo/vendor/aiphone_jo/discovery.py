"""LAN discovery of JO-1MDW monitors (UDP 01001 -> 11001).

The 01001 frame is sent to UDP 51711 (three times: 0, 0.5 and 1 s) and
replies are received on UDP 51712. macOS refuses 255.255.255.255
(EHOSTUNREACH) while the subnet-directed broadcast works, so several
targets can be given (see :func:`broadcast_targets`).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ._util import valid_port
from .const import (
    DISCOVERY_RECEIVE_PORT,
    DISCOVERY_SEND_PORT,
    DISCOVERY_TIMEOUT,
    LIMITED_BROADCAST,
    MAX_FRAME_BYTES,
    Mid,
)
from .exceptions import (
    AiphoneJoError,
    DiscoveryError,
    NoMonitorFoundError,
    PairingRejectedError,
    PairingStage,
    ProtocolError,
    pairing_reason,
)
from .frames import body, frame, header, parse_frame
from .redact import redact

_LOGGER = logging.getLogger(__name__)
_RESEND_DELAYS = (0.5, 1.0)


@dataclass(frozen=True, slots=True)
class DiscoveredMonitor:
    """Monitor that answered the discovery (TLS pairing address and port).

    ``rslt`` is the 11001 result: ``0`` = accepted; ``1`` retry, ``2``
    registration already in progress, ``3`` terminal limit reached (``port`` is
    then ``0`` when the monitor did not send one).
    """

    address: str
    port: int
    system: Any = None
    version: str | None = None
    rslt: int = 0

    @property
    def accepted(self) -> bool:
        """``True`` when the monitor accepted the discovery (``RSLT 0``)."""
        return self.rslt == 0

    @property
    def sys_type(self) -> int | None:
        """``SYS`` as an integer when available (``4`` for the JO-1MDW)."""
        try:
            return int(self.system)
        except (TypeError, ValueError):
            return None


def broadcast_targets(bind_address: str | None = None) -> list[str]:
    """Directed broadcast of every local IPv4 interface, plus 255.255.255.255."""
    import ifaddr  # noqa: PLC0415 - optional at import time, only needed here

    targets: list[str] = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if not ip.is_IPv4 or not isinstance(ip.ip, str):
                continue
            address = ipaddress.IPv4Address(ip.ip)
            if address.is_loopback or address.is_link_local:
                continue
            if bind_address and bind_address not in ("0.0.0.0", ip.ip):  # noqa: S104
                continue
            network = ipaddress.IPv4Network(f"{ip.ip}/{ip.network_prefix}", strict=False)
            targets.append(str(network.broadcast_address))
    return list(dict.fromkeys([*targets, LIMITED_BROADCAST]))


class _Receiver(asyncio.DatagramProtocol):
    def __init__(self, strict_peer: str | None, done: asyncio.Event) -> None:
        self.devices: dict[str, DiscoveredMonitor] = {}
        self.refused: dict[str, DiscoveredMonitor] = {}
        self._strict_peer = strict_peer
        self._done = done

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        peer = str(addr[0])
        if self._strict_peer and peer != self._strict_peer:
            return
        try:
            if len(data) > MAX_FRAME_BYTES:
                _LOGGER.warning("Discovery: datagram too large, ignored")
                return
            message = parse_frame(data)
            if header(message).get("MID") != Mid.DISCOVERY_REP:
                return
            _LOGGER.debug("rx discovery %s", redact(message, identifiers=True))
            reply = body(message)
            rslt = reply.get("RSLT")
            if not isinstance(rslt, int) or isinstance(rslt, bool):
                raise ProtocolError("11001 without an integer RSLT.")
            if rslt != 0:
                # 11001 RSLT 1 retry, 2 registration in progress (also answered to
                # our own repeated 01001), 3 limit reached.
                _LOGGER.info("Discovery: monitor refused (RSLT=%s)", rslt)
                raw_port = reply.get("PORT")
                port = raw_port if isinstance(raw_port, int) and 0 < raw_port < 65536 else 0
            else:
                port = valid_port(reply.get("PORT"))
        except AiphoneJoError as err:
            _LOGGER.warning("Discovery: ignored datagram: %s", err)
            return
        version = reply.get("SYSVER")
        monitor = DiscoveredMonitor(
            address=peer,
            port=port,
            system=reply.get("SYS"),
            version=str(version) if version is not None else None,
            rslt=rslt,
        )
        if rslt != 0:
            self.refused.setdefault(peer, monitor)
        else:
            self.devices[f"{peer}:{port}"] = monitor
        # Single target: the JO only opens its pairing window once, do not wait.
        if self._strict_peer:
            self._done.set()


async def discover(
    targets: str | Sequence[str] = LIMITED_BROADCAST,
    *,
    bind_address: str = "0.0.0.0",  # noqa: S104 - listen on every interface by default
    timeout: float = DISCOVERY_TIMEOUT,
    receive_port: int = DISCOVERY_RECEIVE_PORT,
    send_port: int = DISCOVERY_SEND_PORT,
    include_refused: bool = False,
) -> list[DiscoveredMonitor]:
    """Broadcast 01001 and collect 11001 replies (deduplicated by address:port).

    The monitor must be in "add smartphone" mode. With a single unicast
    target, the first valid reply ends the discovery early.

    Refusals (11001 with ``RSLT != 0``) are returned only with
    ``include_refused=True``, and only for addresses that did not also accept.
    Raises :class:`PairingRejectedError` (``mid="11001"``, ``rslt``, ``reason``)
    when monitors answered but none accepted, :class:`NoMonitorFoundError` when
    nobody answered and :class:`DiscoveryError` on socket errors.
    """
    target_list = [targets] if isinstance(targets, str) else list(targets)
    if not target_list:
        raise DiscoveryError("At least one discovery target is required.")
    strict_peer = (
        target_list[0] if len(target_list) == 1 and target_list[0] != LIMITED_BROADCAST else None
    )
    loop = asyncio.get_running_loop()
    done = asyncio.Event()
    try:
        transport, receiver = await loop.create_datagram_endpoint(
            lambda: _Receiver(strict_peer, done), local_addr=(bind_address, receive_port)
        )
    except OSError as err:
        raise DiscoveryError(f"UDP discovery: cannot listen on {receive_port}: {err}") from err
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sender.setblocking(False)
        sender.bind((bind_address, 0))
        payload = frame(Mid.DISCOVERY_REQ)

        def send_round() -> None:
            failures: list[OSError] = []
            for target in target_list:
                try:
                    sender.sendto(payload, (target, send_port))
                except OSError as err:
                    failures.append(err)
            _LOGGER.debug("tx discovery 01001 to %s", target_list)
            if len(failures) == len(target_list):
                raise DiscoveryError(f"UDP discovery: {failures[0]}")

        send_round()
        handles = [loop.call_later(delay, _safe_round, send_round) for delay in _RESEND_DELAYS]
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(done.wait(), timeout)
        for handle in handles:
            handle.cancel()
    except OSError as err:
        raise DiscoveryError(f"UDP discovery: {err}") from err
    finally:
        sender.close()
        transport.close()
    accepted = list(receiver.devices.values())
    accepted_addresses = {monitor.address for monitor in accepted}
    refused = [m for a, m in receiver.refused.items() if a not in accepted_addresses]
    if include_refused:
        return accepted + refused
    if accepted:
        return accepted
    if refused:
        first = refused[0]
        raise PairingRejectedError(
            f"The monitor refused the discovery (RSLT={first.rslt}).",
            stage=PairingStage.DISCOVERY,
            reason=pairing_reason(Mid.DISCOVERY_REP, first.rslt),
            mid=Mid.DISCOVERY_REP,
            rslt=first.rslt,
        )
    raise NoMonitorFoundError(
        "No monitor found. Check that it is on the same LAN, in 'add smartphone' "
        "mode, and that UDP 51711/51712 is not blocked by a firewall."
    )


def _safe_round(send_round: Any) -> None:
    try:
        send_round()
    except DiscoveryError as err:
        _LOGGER.warning("%s", err)
