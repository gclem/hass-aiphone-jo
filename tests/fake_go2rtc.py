"""Fake go2rtc server for tests (HTTP API and WebSocket WebRTC signaling)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

# Offer shaped like the relay's (named mids, sdes:mid extension); fictitious values.
JANUS_OFFER = "\r\n".join(
    [
        "v=0",
        "o=- 1 1 IN IP4 198.51.100.1",
        "s=-",
        "t=0 0",
        "a=group:BUNDLE audio video",
        "a=msid-semantic: WMS janus",
        "m=audio 9 UDP/TLS/RTP/SAVPF 0",
        "c=IN IP4 198.51.100.1",
        "a=sendonly",
        "a=mid:audio",
        "a=rtcp-mux",
        "a=rtpmap:0 PCMU/8000",
        "a=extmap:1 urn:ietf:params:rtp-hdrext:sdes:mid",
        "a=candidate:1 1 udp 2015363327 198.51.100.1 20000 typ host",
        "m=video 9 UDP/TLS/RTP/SAVPF 96",
        "c=IN IP4 198.51.100.1",
        "a=sendonly",
        "a=mid:video",
        "a=rtcp-mux",
        "a=rtpmap:96 H264/90000",
        "a=extmap:1 urn:ietf:params:rtp-hdrext:sdes:mid",
        "a=candidate:1 1 udp 2015363327 198.51.100.1 20002 typ host",
        "",
    ]
)

GO2RTC_ANSWER = "\r\n".join(
    [
        "v=0",
        "o=- 2 2 IN IP4 0.0.0.0",
        "s=-",
        "t=0 0",
        "a=group:BUNDLE 0 1",
        "m=audio 9 UDP/TLS/RTP/SAVPF 0",
        "a=recvonly",
        "a=mid:0",
        "a=candidate:2 1 udp 2130706431 192.0.2.20 50000 typ host",
        "m=video 9 UDP/TLS/RTP/SAVPF 96",
        "a=recvonly",
        "a=mid:1",
        "",
    ]
)

VIEWER_ANSWER = "v=0\r\ns=viewer-answer\r\n"
VIEWER_CANDIDATE = "candidate:3 1 udp 2130706431 192.0.2.20 50002 typ host"
JPEG = b"\xff\xd8fake-jpeg\xff\xd9"


@dataclass
class FakeGo2Rtc:
    """In-memory go2rtc: streams, pushed sessions, frames and viewers."""

    streams: dict[str, list[str]] = field(default_factory=dict)
    ingests: list[tuple[str, str]] = field(default_factory=list)
    puts: list[tuple[str, str]] = field(default_factory=list)
    frames: list[str] = field(default_factory=list)
    viewer_messages: list[dict[str, Any]] = field(default_factory=list)
    viewer_streams: list[str] = field(default_factory=list)
    viewers_open: int = 0
    viewers_total: int = 0
    ingest_status: int = 201
    frame: bytes = JPEG
    video: bool = True
    rtsp: bool = True
    pushed: set[str] = field(default_factory=set)
    _server: TestServer | None = None

    async def start(self) -> str:
        """Start on localhost; return the base URL."""
        app = web.Application()
        app.router.add_get("/api", self._api)
        app.router.add_get("/api/streams", self._get_streams)
        app.router.add_put("/api/streams", self._put_streams)
        app.router.add_post("/api/webrtc", self._webrtc)
        app.router.add_get("/api/frame.jpeg", self._frame)
        app.router.add_get("/api/ws", self._ws)
        self._server = TestServer(app, host="127.0.0.1")
        await self._server.start_server()
        return str(self._server.make_url("/"))

    async def stop(self) -> None:
        """Stop the server."""
        if self._server is not None:
            await self._server.close()

    async def _api(self, request: web.Request) -> web.Response:
        info: dict[str, Any] = {"version": "1.9.15"}
        if self.rtsp:
            info["rtsp"] = {"listen": "127.0.0.1:18554"}
        return web.json_response(info)

    def _live(self, name: str) -> bool:
        """True when the stream, or the stream it relays, received a session."""
        return name in self.pushed or any(
            source.startswith(f"ffmpeg:{pushed}#")
            for pushed in self.pushed
            for source in self.streams.get(name, [])
        )

    async def _get_streams(self, request: web.Request) -> web.Response:
        name = request.query["src"]
        if name not in self.streams:
            return web.Response(status=404)
        producers: list[dict[str, Any]] = [{"url": url} for url in self.streams[name]]
        if name in self.pushed:
            receivers = (
                [{"codec": {"codec_type": "video"}, "packets": 10, "bytes": 1000}]
                if self.video
                else []
            )
            producers.append({"format_name": "webrtc", "receivers": receivers})
        return web.json_response({"producers": producers})

    async def _put_streams(self, request: web.Request) -> web.Response:
        name, src = request.query["name"], request.query.get("src", "")
        self.puts.append((name, src))
        if src:
            self.streams[name] = [src]
        return web.Response(status=200)

    async def _webrtc(self, request: web.Request) -> web.Response:
        name = request.query["dst"]
        body = await request.text()
        self.ingests.append((name, body))
        if name not in self.streams:
            return web.Response(status=404)
        if self.ingest_status != 201:
            return web.Response(status=self.ingest_status)
        self.pushed.add(name)
        return web.Response(
            status=201, text=GO2RTC_ANSWER, content_type="application/sdp"
        )

    async def _frame(self, request: web.Request) -> web.Response:
        name = request.query["src"]
        self.frames.append(name)
        body = self.frame if self._live(name) else b""
        return web.Response(status=200, body=body, content_type="image/jpeg")

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        self.viewer_streams.append(request.query["src"])
        self.viewers_open += 1
        self.viewers_total += 1
        try:
            async for message in socket:
                if message.type is not WSMsgType.TEXT:
                    continue
                data = json.loads(message.data)
                self.viewer_messages.append(data)
                if data["type"] == "webrtc/offer":
                    await socket.send_json(
                        {"type": "webrtc/answer", "value": VIEWER_ANSWER}
                    )
                    await socket.send_json(
                        {"type": "webrtc/candidate", "value": VIEWER_CANDIDATE}
                    )
        finally:
            self.viewers_open -= 1
        return socket
