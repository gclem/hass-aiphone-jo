"""Bridge between the monitor's live sessions and a go2rtc server."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
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

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)
FRAME_TIMEOUT = aiohttp.ClientTimeout(total=20)


class Go2RtcError(Exception):
    """go2rtc request failed."""


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
    """One go2rtc stream fed by the monitor's live sessions."""

    def __init__(self, server: Go2RtcServer, stream: str) -> None:
        """Initialize."""
        self.server = server
        self.stream = stream

    def _url(self, path: str, **query: str) -> str:
        params = "&".join(f"{k}={quote(v, safe='')}" for k, v in query.items())
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

    async def _streams(self) -> dict[str, Any] | None:
        url = self._url("api/streams", src=self.stream)
        status, body = await self._request("GET", url)
        if status != 200:
            return None
        try:
            data = json.loads(body)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    async def async_ensure_stream(self) -> None:
        """Create the target stream if it does not exist yet."""
        if await self._streams() is not None:
            return
        status, _ = await self._request(
            "PUT",
            self._url("api/streams", name=self.stream, src=PLACEHOLDER_SOURCE),
        )
        if status != 200:
            raise Go2RtcError(f"Cannot create the stream: HTTP {status}")

    async def async_ingest(self, offer: str) -> str:
        """Push the relay's offer into the stream; return the answer for the relay."""
        normalized, restore = normalize_mids(offer)
        status, body = await self._request(
            "POST", self._url("api/webrtc", dst=self.stream), data=normalized
        )
        if status not in (200, 201):
            raise Go2RtcError(f"Ingest refused: HTTP {status}")
        return restore(body.decode())

    async def async_video_flowing(self) -> bool:
        """True once go2rtc receives video from a pushed session."""
        info = await self._streams()
        for producer in (info or {}).get("producers") or []:
            for receiver in producer.get("receivers") or []:
                codec = receiver.get("codec") or {}
                if codec.get("codec_type") == "video" and receiver.get("packets"):
                    return True
        return False

    async def async_wait_video(self, limit: float, interval: float = 0.25) -> None:
        """Wait until video flows; raise Go2RtcError after ``limit`` seconds."""
        try:
            async with asyncio.timeout(limit):
                while True:
                    if await self.async_video_flowing():
                        return
                    await asyncio.sleep(interval)
        except TimeoutError as err:
            raise Go2RtcError("No video received") from err

    async def async_frame(self) -> bytes:
        """Current JPEG image of the stream."""
        status, body = await self._request(
            "GET", self._url("api/frame.jpeg", src=self.stream), limit=FRAME_TIMEOUT
        )
        if status != 200 or not body:
            raise Go2RtcError(f"No image: HTTP {status}")
        return body

    async def async_open_viewer(
        self, on_message: Callable[[dict[str, Any]], None]
    ) -> Go2RtcViewer:
        """WebSocket WebRTC session of a viewer on the stream."""
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT.total):
                socket = await self.server.session.ws_connect(
                    self._url("api/ws", src=self.stream)
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
