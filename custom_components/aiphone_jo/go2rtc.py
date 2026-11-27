"""Bridge between the monitor's live sessions and a go2rtc server.

Each monitoring session is pushed into one go2rtc stream. go2rtc does not
move the consumers of a stream to a newly pushed session, so viewers read a
second stream (the relay) that re-reads the first one through ffmpeg, without
transcoding. When a session ends, ffmpeg times out, go2rtc restarts it and
moves the relay's viewers to the next session.

An answered call is pushed into its own stream, which can also carry the talk
path (see :mod:`.talk`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)
from homeassistant.util.hass_dict import HassKey

from .const import CONF_GO2RTC_PASSWORD, CONF_GO2RTC_URL, CONF_GO2RTC_USERNAME

try:
    from .vendor.aiphone_jo.sdp import normalize_mids
except ImportError:  # Bundled library without SDP helpers.
    from .sdp import normalize_mids

_LOGGER = logging.getLogger(__name__)

# Configuration stored by Home Assistant's go2rtc integration (url, session).
GO2RTC_DATA: HassKey[Any] = HassKey("go2rtc")

# go2rtc only accepts pushed media into an existing stream, and its API cannot
# create a stream without a source: this one is refused immediately, so the
# stream is only fed by the pushed sessions.
PLACEHOLDER_SOURCE = "rtsp://127.0.0.1:1/aiphone_jo"

# Seconds without media before ffmpeg gives up on an ended session: short, so
# the relay quickly moves to the next session (go2rtc's default is 5).
RELAY_TIMEOUT = 2

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)
FRAME_TIMEOUT = aiohttp.ClientTimeout(total=20)
# go2rtc cannot make an image until a key frame arrives, which may come a
# little after the first video packets: a failed image is requested again.
FRAME_ATTEMPTS = 3
FRAME_RETRY_DELAY = 0.5
# Seconds to read the stream state that explains missing video.
REPORT_TIMEOUT = 5
# Characters of go2rtc's error response kept in the error message.
ERROR_DETAIL = 120
# Transports and candidate types go2rtc reports for the path of a session.
PATH_TRANSPORTS = frozenset({"udp", "tcp"})
PATH_KINDS = frozenset({"host", "srflx", "prflx", "relay"})


class Go2RtcError(Exception):
    """go2rtc request failed."""


def _sdp_summary(sdp: str) -> str:
    """Media sections and candidate count of an SDP, without any address."""
    kinds: list[str] = []
    candidates = 0
    for line in sdp.splitlines():
        if line.startswith("m="):
            kind, _, rest = line[2:].partition(" ")
            port = rest.partition(" ")[0]
            kinds.append(f"{kind} (rejected)" if port == "0" else kind)
        elif line.startswith("a=candidate:"):
            candidates += 1
    return f"{'+'.join(kinds) or 'no media'}, {candidates} candidates"


def _pushed_sessions(info: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Sessions pushed into a stream (not its configured sources)."""
    return [
        producer
        for producer in (info or {}).get("producers") or []
        if isinstance(producer, dict)
        and (producer.get("format_name") or "receivers" in producer)
    ]


def _sessions_state(pushed: list[dict[str, Any]]) -> str:
    """Tracks and bytes received by the pushed sessions."""
    parts: list[str] = []
    for producer in pushed:
        tracks = [
            f"{(receiver.get('codec') or {}).get('codec_type') or 'unknown'} "
            f"{receiver.get('packets') or 0} packets"
            for receiver in producer.get("receivers") or []
            if isinstance(receiver, dict)
        ]
        received = producer.get("bytes_recv")
        state = ", ".join(tracks) or "no track"
        if isinstance(received, int):
            state += f", {received} bytes received"
        parts.append(state)
    return "; ".join(parts)


def _pushed_summary(info: dict[str, Any] | None) -> str:
    """State of the sessions pushed into a stream, for a failed video wait.

    go2rtc lists a pushed session as a producer once its offer is answered,
    and a receiver for each track once its first packets arrive: a session
    without receivers never got media through (connectivity, encryption).
    """
    if info is None:
        return "stream missing"
    pushed = _pushed_sessions(info)
    if not pushed:
        return "no pushed session"
    return "pushed session: " + _sessions_state(pushed)


def _session_path(producer: dict[str, Any]) -> str | None:
    """Network path a pushed session selected, without any address.

    Once a session found a path to its peer, go2rtc appends the transport to
    its protocol ("http+udp") and reports the peer's address followed by the
    type of its candidate: only the transport and that type are kept.
    """
    protocol = producer.get("protocol")
    if not isinstance(protocol, str) or "+" not in protocol:
        return None
    transport = protocol.rsplit("+", 1)[1]
    if transport not in PATH_TRANSPORTS:
        transport = "unknown"
    remote = producer.get("remote_addr")
    words = remote.split() if isinstance(remote, str) else []
    kind = words[1] if len(words) > 1 and words[1] in PATH_KINDS else None
    return f"{transport} {kind}" if kind else transport


@dataclass
class _SessionWatch:
    """What the pushed sessions of a stream did while their video was awaited.

    go2rtc drops a pushed session as soon as its connection ends, with all
    it knew about it: what was seen meanwhile is kept here. Times are seconds
    after ``origin`` (when the offer was answered).
    """

    origin: float = field(default_factory=time.monotonic)
    first: float | None = None
    last: float | None = None
    present: bool = False
    state: str = ""
    path: str | None = None
    path_at: float | None = None

    def note(self, info: dict[str, Any] | None) -> None:
        """Record the state of the stream at one poll."""
        pushed = _pushed_sessions(info)
        self.present = bool(pushed)
        if not pushed:
            return
        now = time.monotonic() - self.origin
        if self.first is None:
            self.first = now
        self.last = now
        self.state = _sessions_state(pushed)
        if self.path is None:
            for producer in pushed:
                if path := _session_path(producer):
                    self.path, self.path_at = path, now
                    break

    def summary(self) -> str:
        """Timeline of the sessions, without any address."""
        if self.first is None or self.last is None:
            return "never seen"
        seen = f"seen from {self.first:.1f} s"
        if not self.present:
            seen += f" to {self.last:.1f} s with {self.state}"
        if self.path is None:
            return f"{seen}, no path found"
        return f"{seen}, path {self.path} by {self.path_at:.1f} s"


def _frame_error(status: int, body: bytes) -> str:
    if status == 200:
        return "No image: empty response"
    detail = " ".join(body.decode(errors="replace").split())[:ERROR_DETAIL]
    return f"No image: HTTP {status} {detail}".rstrip()


@dataclass(frozen=True, slots=True)
class Go2RtcServer:
    """go2rtc API endpoint."""

    url: str
    session: aiohttp.ClientSession

    def api(self, path: str) -> str:
        """Absolute URL of an API path."""
        return f"{self.url.rstrip('/')}/{path.lstrip('/')}"


def async_get_server(
    hass: HomeAssistant, options: dict[str, Any]
) -> Go2RtcServer | None:
    """go2rtc configured in the options, or the one run by Home Assistant."""
    if url := options.get(CONF_GO2RTC_URL):
        username = options.get(CONF_GO2RTC_USERNAME)
        password = options.get(CONF_GO2RTC_PASSWORD)
        if username and password:
            session = async_create_clientsession(
                hass, auth=aiohttp.BasicAuth(username, password)
            )
        else:
            session = async_get_clientsession(hass)
        return Go2RtcServer(str(url), session)
    config = hass.data.get(GO2RTC_DATA)
    url = getattr(config, "url", None)
    session = getattr(config, "session", None)
    if isinstance(url, str) and isinstance(session, aiohttp.ClientSession):
        return Go2RtcServer(url, session)
    return None


async def async_check_server(server: Go2RtcServer) -> None:
    """Raise Go2RtcError when the server does not answer."""
    try:
        async with server.session.get(
            server.api("api"), timeout=REQUEST_TIMEOUT
        ) as response:
            if response.status != 200:
                raise Go2RtcError(f"HTTP {response.status}")
    except (aiohttp.ClientError, TimeoutError) as err:
        raise Go2RtcError(type(err).__name__) from err


class Go2RtcBridge:
    """go2rtc stream fed by the monitor's live sessions, and its relay."""

    def __init__(self, server: Go2RtcServer, stream: str) -> None:
        """Initialize."""
        self.server = server
        self.stream = stream
        self.relay = f"{stream}_live"
        self.call_stream = f"{stream}_call"
        self.ring_stream = f"{stream}_ring"
        # A stream name as ffmpeg input makes go2rtc read it from its own RTSP
        # listener on the loopback address, where no RTSP credentials apply.
        self.relay_source = (
            f"ffmpeg:{stream}#video=copy#audio=copy#timeout={RELAY_TIMEOUT}"
        )
        # Last offer pushed into each stream: what it held, when it was
        # received and answered. Reported when no video follows.
        self._ingests: dict[str, tuple[str, float, float]] = {}

    def _url(self, path: str, **query: str | list[str]) -> str:
        params = "&".join(
            f"{key}={quote(value, safe='')}"
            for key, values in query.items()
            for value in ([values] if isinstance(values, str) else values)
        )
        return self.server.api(f"{path}?{params}")

    async def _request(
        self,
        method: str,
        url: str,
        *,
        data: str | None = None,
        limit: aiohttp.ClientTimeout = REQUEST_TIMEOUT,
    ) -> tuple[int, bytes]:
        headers = {"Content-Type": "application/sdp"} if data is not None else None
        try:
            async with self.server.session.request(
                method, url, data=data, headers=headers, timeout=limit
            ) as response:
                return response.status, await response.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise Go2RtcError(type(err).__name__) from err

    async def _json(self, url: str) -> dict[str, Any] | None:
        status, body = await self._request("GET", url)
        if status != 200:
            return None
        try:
            data = json.loads(body)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    async def _streams(self, name: str | None = None) -> dict[str, Any] | None:
        return await self._json(self._url("api/streams", src=name or self.stream))

    async def _create(self, name: str, source: str) -> None:
        if await self._streams(name) is not None:
            return
        status, _ = await self._request(
            "PUT", self._url("api/streams", name=name, src=source)
        )
        if status != 200:
            raise Go2RtcError(f"Cannot create the stream: HTTP {status}")

    async def async_create_stream(self, name: str, *sources: str) -> None:
        """Create the stream, or replace its sources."""
        status, _ = await self._request(
            "PUT", self._url("api/streams", name=name, src=list(sources))
        )
        # go2rtc reports an error when it cannot save its configuration file,
        # after creating the stream.
        if status != 200 and await self._streams(name) is None:
            raise Go2RtcError(f"Cannot create the stream: HTTP {status}")

    async def async_delete_stream(self, name: str) -> None:
        """Remove a stream."""
        self._ingests.pop(name, None)
        await self._request("DELETE", self._url("api/streams", src=name))

    async def async_preload(self, name: str, *, microphone: str) -> None:
        """Keep a consumer sending microphone audio on the stream.

        go2rtc answers once the stream's audio destination is connected.
        """
        status, body = await self._request(
            "PUT", self._url("api/preload", src=name, microphone=microphone)
        )
        if status != 200:
            raise Go2RtcError(f"Preload refused: HTTP {status} {body[:200]!r}")

    async def async_delete_preload(self, name: str) -> None:
        """Remove the stream's preload consumer."""
        await self._request("DELETE", self._url("api/preload", src=name))

    async def async_ensure_stream(self) -> None:
        """Create the target stream if it does not exist yet."""
        await self._create(self.stream, PLACEHOLDER_SOURCE)

    async def async_reset_stream(self) -> None:
        """Empty the target stream before a new session.

        The video of an ended session stays in the stream for a few seconds
        (about 4 on a JO-1MDW), longer than the next session takes to start:
        an image or a live view of another station could show it.
        """
        with contextlib.suppress(Go2RtcError):
            await self.async_delete_stream(self.stream)
        await self.async_create_stream(self.stream, PLACEHOLDER_SOURCE)

    async def async_relay_supported(self) -> bool:
        """True when go2rtc can relay the stream (its RTSP server is enabled)."""
        info = await self._json(self.server.api("api"))
        if info is None:
            raise Go2RtcError("No server information")
        return isinstance(info.get("rtsp"), dict)

    async def async_ensure_relay(self) -> None:
        """Create the relay stream read by the viewers if it does not exist yet.

        An existing stream is never replaced: its viewers would be orphaned.
        """
        await self._create(self.relay, self.relay_source)

    async def async_ingest(self, offer: str, stream: str | None = None) -> str:
        """Push the relay's offer into the stream; return the answer for the relay."""
        target = stream or self.stream
        offered = time.monotonic()
        normalized, restore = normalize_mids(offer)
        self._ingests.pop(target, None)
        status, body = await self._request(
            "POST", self._url("api/webrtc", dst=target), data=normalized
        )
        if status not in (200, 201):
            raise Go2RtcError(f"Ingest refused: HTTP {status}")
        answer = body.decode()
        summary = f"offer {_sdp_summary(offer)}, answer {_sdp_summary(answer)}"
        self._ingests[target] = (summary, offered, time.monotonic())
        _LOGGER.debug("Session pushed into %s: %s", target, summary)
        return restore(answer)

    def ingest_times(self, stream: str | None = None) -> tuple[float, float] | None:
        """When the stream's last offer was received and answered (monotonic)."""
        ingest = self._ingests.get(stream or self.stream)
        return (ingest[1], ingest[2]) if ingest is not None else None

    async def async_video_report(
        self, stream: str | None = None, watch: _SessionWatch | None = None
    ) -> str:
        """Where a pushed session stands, to explain missing video.

        With ``watch``, also what the session did while its video was awaited.
        """
        target = stream or self.stream
        ingest = self._ingests.get(target)
        if ingest is None:
            pushed = "no offer pushed"
        else:
            summary, _, answered = ingest
            pushed = f"{summary}, answered {time.monotonic() - answered:.1f} s ago"
        try:
            async with asyncio.timeout(REPORT_TIMEOUT):
                state = _pushed_summary(await self._streams(target))
        except (Go2RtcError, TimeoutError) as err:
            state = f"go2rtc unreachable ({type(err).__name__})"
        if watch is not None:
            state += f"; {watch.summary()}"
        return f"{state}; {pushed}"

    @staticmethod
    def _video_flowing(info: dict[str, Any] | None) -> bool:
        for producer in (info or {}).get("producers") or []:
            for receiver in producer.get("receivers") or []:
                codec = receiver.get("codec") or {}
                if codec.get("codec_type") == "video" and receiver.get("packets"):
                    return True
        return False

    async def async_video_flowing(self, stream: str | None = None) -> bool:
        """True once go2rtc receives video from a pushed session."""
        return self._video_flowing(await self._streams(stream))

    async def async_wait_video(
        self, limit: float, interval: float = 0.25, stream: str | None = None
    ) -> None:
        """Wait until video flows; raise Go2RtcError after ``limit`` seconds.

        The error tells what the pushed session did meanwhile: go2rtc forgets
        a session whose connection ended, so it is followed at each poll.
        """
        times = self.ingest_times(stream)
        watch = _SessionWatch(times[1]) if times else _SessionWatch()
        try:
            async with asyncio.timeout(limit):
                while True:
                    info = await self._streams(stream)
                    watch.note(info)
                    if self._video_flowing(info):
                        return
                    await asyncio.sleep(interval)
        except TimeoutError as err:
            report = await self.async_video_report(stream, watch)
            raise Go2RtcError(f"No video received ({report})") from err

    async def async_frame(self, stream: str | None = None) -> bytes:
        """Current JPEG image of the stream (the target stream by default)."""
        url = self._url("api/frame.jpeg", src=stream or self.stream)
        error = ""
        for attempt in range(FRAME_ATTEMPTS):
            if attempt:
                await asyncio.sleep(FRAME_RETRY_DELAY)
            status, body = await self._request("GET", url, limit=FRAME_TIMEOUT)
            if status == 200 and body:
                return body
            error = _frame_error(status, body)
            _LOGGER.debug("Image attempt %s failed: %s", attempt + 1, error)
        raise Go2RtcError(f"{error} (after {FRAME_ATTEMPTS} attempts)")

    async def async_open_viewer(
        self,
        on_message: Callable[[dict[str, Any]], None],
        stream: str | None = None,
    ) -> Go2RtcViewer:
        """WebSocket WebRTC session of a viewer (on the target stream by default)."""
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT.total):
                socket = await self.server.session.ws_connect(
                    self._url("api/ws", src=stream or self.stream)
                )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise Go2RtcError(type(err).__name__) from err
        return Go2RtcViewer(socket, on_message)


class Go2RtcViewer:
    """Browser peer connected to the stream through go2rtc (trickle ICE)."""

    def __init__(
        self,
        socket: aiohttp.ClientWebSocketResponse,
        on_message: Callable[[dict[str, Any]], None],
    ) -> None:
        """Start reading messages."""
        self._socket = socket
        self._on_message = on_message
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        async for message in self._socket:
            if message.type is not aiohttp.WSMsgType.TEXT:
                continue
            with contextlib.suppress(ValueError):
                data = json.loads(message.data)
                if isinstance(data, dict):
                    self._on_message(data)

    async def async_send(self, kind: str, value: str) -> None:
        """Send a go2rtc message (webrtc/offer, webrtc/candidate)."""
        with contextlib.suppress(aiohttp.ClientError, ConnectionResetError):
            await self._socket.send_json({"type": kind, "value": value})

    async def async_close(self) -> None:
        """Close the session."""
        self._reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._reader
        await self._socket.close()
