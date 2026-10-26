"""Talk path of an answered call: from the browser's microphone to the station.

The call stream in go2rtc has a WebRTC source,
``webrtc:ws://127.0.0.1:<port>/talk/<token>``, served by this module. When a
consumer of the stream sends audio (a browser microphone, or the preload
started when the call is answered), go2rtc dials it with an offer whose
``sendonly`` audio section carries the microphone. That section is sent to the
station (``ADD_SND_USER``) and the station's answer is merged back into a
complete answer for go2rtc. Later microphones reuse the same connection.

go2rtc must run on the same host as Home Assistant to reach this endpoint,
which is the case for the go2rtc server run by Home Assistant.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import WSMsgType, web

from .go2rtc import Go2RtcBridge, Go2RtcError
from .vendor.aiphone_jo import AiphoneJoError
from .vendor.aiphone_jo.sdp import extract_section

_LOGGER = logging.getLogger(__name__)

SendOffer = Callable[[str], Awaitable[str]]

# Codec of the official application's talk path.
SEND_CODEC = "PCMU"
# go2rtc trickles its ICE candidates after the offer; the station needs them
# in the offer.
CANDIDATE_WAIT = 1.0
CANDIDATE_QUIET = 0.2
OFFER_TIMEOUT = 10.0
TALK_TIMEOUT = 10.0

_RTPMAP = re.compile(r"^a=rtpmap:(?P<pt>\d+) (?P<name>[^/\s]+)")
_FORMAT_LINE = re.compile(r"^a=(?:rtpmap|fmtp|rtcp-fb):(?P<pt>\d+)[ \t]")


# ------------------------------------------------------------------- SDP


def _split(sdp: str) -> tuple[list[str], str]:
    newline = "\r\n" if "\r\n" in sdp else "\n"
    return [line for line in sdp.split(newline) if line], newline


def _join(lines: list[str], newline: str) -> str:
    return newline.join(lines) + newline


def _sections(lines: list[str]) -> tuple[list[str], list[list[str]]]:
    session: list[str] = []
    sections: list[list[str]] = []
    for line in lines:
        if line.startswith("m="):
            sections.append([line])
        elif sections:
            sections[-1].append(line)
        else:
            session.append(line)
    return session, sections


def keep_codec(sdp: str, name: str) -> str:
    """Keep only the formats of codec ``name`` in every media section."""
    lines, newline = _split(sdp)
    session, sections = _sections(lines)
    result = list(session)
    for section in sections:
        keep = {
            match["pt"]
            for line in section
            if (match := _RTPMAP.match(line)) and match["name"].upper() == name.upper()
        }
        if not keep:
            raise ValueError(f"No {name} format in the offer.")
        fields = section[0].split()
        result.append(" ".join([*fields[:3], *(f for f in fields[3:] if f in keep)]))
        result.extend(
            line
            for line in section[1:]
            if (match := _FORMAT_LINE.match(line)) is None or match["pt"] in keep
        )
    return _join(result, newline)


def add_candidates(sdp: str, candidates: list[str]) -> str:
    """Add trickled ICE candidates to every media section."""
    lines, newline = _split(sdp)
    session, sections = _sections(lines)
    result = list(session)
    for section in sections:
        result.extend(section)
        result.extend(
            f"a={candidate}"
            for candidate in candidates
            if f"a={candidate}" not in section
        )
    return _join(result, newline)


def fill_rejected(offer: str, answer: str) -> str:
    """Give rejected answer sections the rtpmap of their format.

    go2rtc refuses an answer section whose dynamic payload type has no
    rtpmap, even when the section is rejected (port 0).
    """
    _, offered = _sections(_split(offer)[0])
    lines, newline = _split(answer)
    session, sections = _sections(lines)
    result = list(session)
    for index, section in enumerate(sections):
        fields = section[0].split()
        result.append(section[0])
        if (
            len(fields) > 3
            and fields[1] == "0"
            and index < len(offered)
            and not any(line.startswith(f"a=rtpmap:{fields[3]} ") for line in section)
        ):
            result.extend(
                line
                for line in offered[index]
                if line.startswith(f"a=rtpmap:{fields[3]} ")
            )
        result.extend(section[1:])
    return _join(result, newline)


# --------------------------------------------------------------- server


class TalkServer:
    """Local signaling endpoint dialled by go2rtc."""

    def __init__(self) -> None:
        """Initialize."""
        self._paths: dict[str, TalkPath] = {}
        self._runner: web.AppRunner | None = None
        self._port: int | None = None

    async def async_start(self) -> None:
        """Listen on the loopback address (once)."""
        if self._runner is not None:
            return
        app = web.Application()
        app.router.add_get("/talk/{token}", self._handle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
        except OSError:
            await runner.cleanup()
            raise
        self._runner = runner
        self._port = runner.addresses[0][1]

    async def async_stop(self) -> None:
        """Stop listening."""
        runner, self._runner = self._runner, None
        self._paths.clear()
        if runner is not None:
            await runner.cleanup()

    def url(self, token: str) -> str:
        """WebSocket URL of a talk path."""
        return f"ws://127.0.0.1:{self._port}/talk/{token}"

    def register(self, path: TalkPath) -> None:
        """Accept dials for ``path``."""
        self._paths[path.token] = path

    def unregister(self, path: TalkPath) -> None:
        """Refuse further dials for ``path``."""
        self._paths.pop(path.token, None)

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        path = self._paths.get(request.match_info["token"])
        if path is None:
            return web.Response(status=404)
        return await path.async_handle(request)


class TalkPath:
    """Talk path of one call, through its go2rtc call stream."""

    def __init__(self, server: TalkServer, bridge: Go2RtcBridge, stream: str) -> None:
        """Initialize and accept dials from go2rtc."""
        self._server = server
        self._bridge = bridge
        self._stream = stream
        self.token = secrets.token_urlsafe(16)
        self.source = f"webrtc:{server.url(self.token)}"
        self._send_offer: SendOffer | None = None
        self._lock = asyncio.Lock()
        # Set once the station answered, or when the preload failed: a
        # microphone joining later can still open the talk path.
        self._settled = asyncio.Event()
        self._preload: asyncio.Task[None] | None = None
        self.connected = False
        server.register(self)

    async def async_talk(self, send_offer: SendOffer) -> None:
        """Library callback while answering: open the talk path now.

        The preload makes go2rtc dial the talk source, as a microphone would.
        Failures are logged: the call then goes on without talking.
        """
        self._send_offer = send_offer
        self._preload = asyncio.create_task(self._async_preload())
        try:
            async with asyncio.timeout(TALK_TIMEOUT):
                await self._settled.wait()
        except TimeoutError:
            _LOGGER.warning("The talk path could not be opened in time")

    async def _async_preload(self) -> None:
        try:
            await self._bridge.async_preload(self._stream, microphone=SEND_CODEC)
        except Go2RtcError as err:
            _LOGGER.warning("The talk path did not connect: %s", err)
            self._settled.set()
        else:
            self.connected = True

    async def async_handle(self, request: web.Request) -> web.WebSocketResponse:
        """go2rtc dialled the talk source: negotiate with the station."""
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        reader = asyncio.create_task(self._read(socket, messages))
        try:
            async with self._lock:
                answer = await self._negotiate(messages)
            await socket.send_json({"type": "webrtc/answer", "value": answer})
            self._settled.set()
            await reader
        except (TimeoutError, ValueError, AiphoneJoError) as err:
            _LOGGER.warning("Talk path negotiation failed: %s", type(err).__name__)
            await socket.close()
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        return socket

    @staticmethod
    async def _read(
        socket: web.WebSocketResponse, messages: asyncio.Queue[dict[str, Any]]
    ) -> None:
        async for message in socket:
            if message.type is not WSMsgType.TEXT:
                continue
            with contextlib.suppress(ValueError):
                data = json.loads(message.data)
                if isinstance(data, dict):
                    messages.put_nowait(data)

    async def _negotiate(self, messages: asyncio.Queue[dict[str, Any]]) -> str:
        send_offer = self._send_offer
        if send_offer is None:
            raise ValueError("No call to talk to")
        offer: str | None = None
        candidates: list[str] = []
        async with asyncio.timeout(OFFER_TIMEOUT):
            while offer is None:
                data = await messages.get()
                if data.get("type") == "webrtc/offer":
                    offer = str(data.get("value") or "")
                elif data.get("type") == "webrtc/candidate" and data.get("value"):
                    candidates.append(str(data["value"]))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CANDIDATE_WAIT
        while (remaining := deadline - loop.time()) > 0:
            wait = remaining if not candidates else min(remaining, CANDIDATE_QUIET)
            try:
                async with asyncio.timeout(wait):
                    data = await messages.get()
            except TimeoutError:
                if candidates:
                    break
                continue
            if data.get("type") == "webrtc/candidate" and data.get("value"):
                candidates.append(str(data["value"]))
        section, restore = extract_section(offer, "audio", direction="sendonly")
        section = add_candidates(keep_codec(section, SEND_CODEC), candidates)
        answer = await send_offer(section)
        return fill_rejected(offer, restore(answer))

    async def async_close(self) -> None:
        """Close the talk path (end of the call)."""
        self._server.unregister(self)
        self._send_offer = None
        self._settled.set()
        preload, self._preload = self._preload, None
        if preload is not None:
            preload.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await preload
        with contextlib.suppress(Go2RtcError):
            await self._bridge.async_delete_preload(self._stream)
