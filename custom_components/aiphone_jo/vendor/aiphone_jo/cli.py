"""``aiphone-jo`` command line tool.

Every command that acts on the hardware asks for an explicit confirmation
(``--yes`` skips it). The paired identity is stored in a private JSON file
(mode 0600). A client certificate can only hold one MQTT connection at a time:
do not run this tool while another client uses the same identity.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import aiohttp

from . import __version__
from .client import AiphoneJoClient
from .cloud_api import get_room_info, unregister
from .const import DEFAULT_API_ORIGIN, DISCOVERY_TIMEOUT
from .credentials import CloudCredentials
from .discovery import DiscoveredMonitor, broadcast_targets, discover
from .exceptions import AiphoneJoError, InvalidInputError, NoMonitorFoundError, PairingError
from .models import (
    AiphoneEvent,
    CallUpdatedEvent,
    ConnectionStateEvent,
    Gate,
    MessageEvent,
    RingEvent,
    SessionEndedEvent,
)
from .pairing import PairingFlow

DEFAULT_STATE = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "aiphone-jo"
    / "credentials.json"
)
OPEN_CONFIRMATION = "OPEN"
UNREGISTER_CONFIRMATION = "UNREGISTER"
_CODE_ATTEMPTS = 3


def _print(text: str = "") -> None:
    sys.stdout.write(f"{text}\n")
    sys.stdout.flush()


async def _ask(prompt: str) -> str:
    return (await asyncio.to_thread(input, prompt)).strip()


async def _confirm(prompt: str, args: argparse.Namespace, expected: str = "yes") -> None:
    if args.yes:
        return
    answer = await _ask(f"{prompt} Type '{expected}' to continue: ")
    if answer != expected:
        raise InvalidInputError("Cancelled by the user.")


def save_state(path: Path, credentials: CloudCredentials, monitor: dict[str, Any]) -> None:
    """Write the paired identity to ``path`` with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = {"credentials": credentials.to_storage(), "monitor": monitor}
    tmp = path.with_suffix(".tmp")
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    tmp.replace(path)


def load_state(path: Path) -> tuple[CloudCredentials, dict[str, Any]]:
    """Read and validate a file written by :func:`save_state`."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise InvalidInputError(f"No paired identity at {path}: run 'aiphone-jo pair'.") from err
    except ValueError as err:
        raise InvalidInputError(f"Unreadable identity file {path}.") from err
    if not isinstance(data, dict) or not isinstance(data.get("credentials"), dict):
        raise InvalidInputError(f"Unreadable identity file {path}.")
    return CloudCredentials.from_storage(data["credentials"]), data.get("monitor") or {}


def _targets(args: argparse.Namespace) -> list[str]:
    return list(args.target) if args.target else broadcast_targets(args.bind)


def _describe_monitor(monitor: DiscoveredMonitor) -> str:
    return f"{monitor.address}:{monitor.port} (SYS {monitor.system}, SYSVER {monitor.version})"


async def cmd_discover(args: argparse.Namespace) -> int:
    for monitor in await discover(_targets(args), bind_address=args.bind, timeout=args.timeout):
        _print(_describe_monitor(monitor))
    return 0


async def _discover_until(args: argparse.Namespace) -> DiscoveredMonitor:
    deadline = time.monotonic() + max(args.wait, 0)
    while True:
        try:
            monitors = await discover(_targets(args), bind_address=args.bind, timeout=args.timeout)
            break
        except NoMonitorFoundError:
            if time.monotonic() >= deadline:
                raise
    if len(monitors) > 1:
        raise InvalidInputError("Several monitors found: pass --target with its address.")
    return monitors[0]


async def cmd_pair(args: argparse.Namespace) -> int:
    path: Path = args.credentials
    if path.exists() and not args.force:
        raise InvalidInputError(f"{path} already exists (use --force to replace it).")
    _print("Put the monitor in 'add smartphone' mode...")
    monitor = await _discover_until(args)
    _print(f"Monitor found: {_describe_monitor(monitor)}")
    async with (
        aiohttp.ClientSession() as session,
        PairingFlow(session, api_origin=args.api_origin) as flow,
    ):
        peer = await flow.connect(monitor, expected_fingerprint=args.fingerprint)
        _print(f"TLS certificate: {peer.fingerprint} ({peer.subject.get('CN', '?')})")
        if args.fingerprint is None:
            await _confirm("Trust this monitor certificate?", args)
            flow.approve(peer.fingerprint)
        try:
            await flow.request_code()
            for attempt in range(1, _CODE_ATTEMPTS + 1):
                code = await _ask("Code displayed on the monitor: ")
                try:
                    await flow.authenticate(code)
                    break
                except PairingError as err:
                    if not err.retry_same_connection or attempt == _CODE_ATTEMPTS:
                        raise
                    _print(f"{err.hint or err}. Try again.")
            await _confirm(
                f"Register this terminal with the Aiphone cloud ({args.api_origin})?", args
            )
            await flow.register_cloud()
            credentials = await flow.register_terminal(args.name)
        except BaseException:
            # Delete an unfinished cloud registration.
            if await flow.cancel():
                _print("The unfinished cloud registration was deleted.")
            raise
    save_state(
        path,
        credentials,
        {"address": monitor.address, "port": monitor.port, "fingerprint": peer.fingerprint},
    )
    _print(f"Paired as '{credentials.term_name}'. Identity saved to {path} (mode 0600).")
    return 0


async def cmd_unregister(args: argparse.Namespace) -> int:
    path: Path = args.credentials
    credentials, _ = load_state(path)
    _print("This deletes the terminal from the Aiphone cloud (experimental).")
    await _confirm("Really unregister this terminal?", args, UNREGISTER_CONFIRMATION)
    async with aiohttp.ClientSession() as session:
        deleted = await unregister(session, credentials)
    _print("Terminal deleted from the cloud." if deleted else "Terminal was already deleted.")
    if not args.keep_file:
        path.unlink(missing_ok=True)
        _print(f"Removed {path}.")
    _print("Remove the terminal from the monitor's smartphone list as well.")
    return 0


async def cmd_rooms(args: argparse.Namespace) -> int:
    credentials, _ = load_state(args.credentials)
    async with aiohttp.ClientSession() as session:
        info = await get_room_info(session, credentials)
    for terminal in info.terminals:
        flag = " [camera station]" if terminal.is_camera_station else ""
        _print(
            f"{terminal.id}\tkind={terminal.term_kind}\tname={terminal.name!r}"
            f"\tkey={terminal.ent_key_type_label}{flag}"
        )
    return 0


@contextlib.asynccontextmanager
async def _client(args: argparse.Namespace) -> Any:
    credentials, _ = load_state(args.credentials)
    client = AiphoneJoClient(credentials, auto_reconnect=False)
    await client.connect()
    try:
        yield client
    finally:
        await client.disconnect()


async def cmd_status(args: argparse.Namespace) -> int:
    async with _client(args) as client:
        status = await client.get_lock_status()
    _print(f"Entrances: {status.entrance_count}")
    for index, key in enumerate(status.keys, start=1):
        _print(f"  key {index}: KEYTP={key.key_type} KEYSTS={key.key_status}")
    return 0


async def cmd_open(args: argparse.Namespace) -> int:
    gate = Gate.OPTION if args.small else Gate.MAIN
    label = "small opening (output 2)" if args.small else "large opening (output 1)"
    _print(f"This starts a monitoring session on station {args.terminal} and opens the {label}.")
    await _confirm("Really open the door?", args, OPEN_CONFIRMATION)
    async with _client(args) as client:
        await client.open_door(gate, args.terminal)
    _print("Command accepted by the entrance station.")
    return 0


def _describe_event(event: AiphoneEvent) -> str | None:
    if isinstance(event, RingEvent):
        call = event.call
        return f"RING from {call.display_name or 'station'} (terminal {call.term_id})"
    if isinstance(event, CallUpdatedEvent):
        return f"call {event.call.status.value}"
    if isinstance(event, SessionEndedEvent):
        return f"session ended by {event.by.value}"
    if isinstance(event, ConnectionStateEvent):
        state = "connected" if event.connected else f"disconnected ({event.error})"
        return f"MQTT {state}, monitor-wide topic: {event.broadcast_subscribed}"
    if isinstance(event, MessageEvent):
        return f"MID {event.message.get('HEADER', {}).get('MID')}"
    return None


async def cmd_ring_watch(args: argparse.Namespace) -> int:
    async with _client(args) as client:
        if not client.broadcast_subscribed:
            _print("Warning: monitor-wide topic refused, rings may be missed.")

        def show(event: AiphoneEvent) -> None:
            text = _describe_event(event)
            if text:
                _print(f"{time.strftime('%H:%M:%S')} {text}")

        client.add_listener(show)
        _print("Waiting for rings (Ctrl+C to stop)...")
        if args.duration:
            await asyncio.sleep(args.duration)
        else:
            await asyncio.Event().wait()
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Argument parser of the ``aiphone-jo`` command."""
    parser = argparse.ArgumentParser(
        prog="aiphone-jo",
        description="Unofficial command line tool for the Aiphone JO-1MDW intercom.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logs (redacted)")
    parser.add_argument(
        "--credentials", type=Path, default=DEFAULT_STATE, help=f"identity file ({DEFAULT_STATE})"
    )
    parser.add_argument("-y", "--yes", action="store_true", help="skip confirmations")
    sub = parser.add_subparsers(dest="command", required=True)

    def lan(command: argparse.ArgumentParser) -> None:
        command.add_argument("--target", action="append", help="monitor or broadcast address")
        command.add_argument("--bind", default="0.0.0.0", help="local IPv4 address")  # noqa: S104
        command.add_argument("--timeout", type=float, default=DISCOVERY_TIMEOUT)

    lan(sub.add_parser("discover", help="find monitors in 'add smartphone' mode"))
    pair = sub.add_parser("pair", help="pair a new terminal (TLS, code, cloud, 01005)")
    lan(pair)
    pair.add_argument("--name", default="HomeAsst", help="terminal name (12 characters max)")
    pair.add_argument("--wait", type=float, default=60.0, help="seconds to keep searching")
    pair.add_argument("--fingerprint", help="already approved SHA-256 fingerprint")
    pair.add_argument("--api-origin", default=DEFAULT_API_ORIGIN)
    pair.add_argument("--force", action="store_true", help="replace an existing identity file")
    unregister_parser = sub.add_parser(
        "unregister", help="delete this terminal from the cloud (experimental)"
    )
    unregister_parser.add_argument(
        "--keep-file", action="store_true", help="keep the local identity file"
    )
    sub.add_parser("rooms", help="list terminals (/getRoomInfo)")
    sub.add_parser("status", help="lock status (26031)")
    open_parser = sub.add_parser("open", help="open the door (monitoring session + 26021/26121)")
    which = open_parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--big", action="store_true", help="output 1, large opening (26021)")
    which.add_argument("--small", action="store_true", help="output 2, small opening (26121)")
    open_parser.add_argument("--terminal", type=int, required=True, help="camera station id")
    watch = sub.add_parser("ring-watch", help="print rings and events")
    watch.add_argument("--duration", type=float, default=0, help="seconds (0 = until Ctrl+C)")
    return parser


_COMMANDS = {
    "discover": cmd_discover,
    "pair": cmd_pair,
    "rooms": cmd_rooms,
    "unregister": cmd_unregister,
    "status": cmd_status,
    "open": cmd_open,
    "ring-watch": cmd_ring_watch,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of the ``aiphone-jo`` command."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_COMMANDS[args.command](args))
    except KeyboardInterrupt:
        return 130
    except PairingError as err:
        details = ", ".join(
            f"{name}={value}"
            for name, value in (
                ("stage", err.stage),
                ("reason", err.reason),
                ("mid", err.mid),
                ("rslt", err.rslt),
            )
            if value is not None
        )
        sys.stderr.write(f"Error: {err} [{details}]\n")
        return 1
    except AiphoneJoError as err:
        sys.stderr.write(f"Error: {err}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
