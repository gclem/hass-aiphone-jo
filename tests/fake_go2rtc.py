"""Fake go2rtc server for tests (HTTP API and WebSocket WebRTC signaling)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import aiohttp
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

# Offer of go2rtc's WebRTC client (talk source): video and audio to receive,
# and the microphone to send; candidates are trickled. Fictitious values.
TALK_OFFER = "\r\n".join(
    [
        "v=0",
        "o=- 3 3 IN IP4 0.0.0.0",
        "s=-",
        "t=0 0",
        "a=fingerprint:sha-256 00:11:22:33",
        "a=group:BUNDLE 0 1 2",
        "m=video 9 UDP/TLS/RTP/SAVPF 96 97",
        "c=IN IP4 0.0.0.0",
        "a=setup:actpass",
        "a=mid:0",
        "a=ice-ufrag:go2rtcufrag",
        "a=ice-pwd:go2rtcpassword",
        "a=rtpmap:96 H264/90000",
        "a=rtpmap:97 VP8/90000",
        "a=recvonly",
        "m=audio 9 UDP/TLS/RTP/SAVPF 101 0 8",
        "c=IN IP4 0.0.0.0",
        "a=setup:actpass",
        "a=mid:1",
        "a=ice-ufrag:go2rtcufrag",
        "a=ice-pwd:go2rtcpassword",
        "a=rtpmap:101 opus/48000/2",
        "a=rtpmap:0 PCMU/8000",
        "a=rtpmap:8 PCMA/8000",
        "a=recvonly",
        "m=audio 9 UDP/TLS/RTP/SAVPF 101 0 8",
        "c=IN IP4 0.0.0.0",
        "a=setup:actpass",
        "a=mid:2",
        "a=ice-ufrag:go2rtcufrag",
        "a=ice-pwd:go2rtcpassword",
        "a=rtpmap:101 opus/48000/2",
        "a=fmtp:101 minptime=10;useinbandfec=1",
        "a=rtcp-fb:101 transport-cc",
        "a=rtpmap:0 PCMU/8000",
        "a=rtcp-fb:0 transport-cc",
        "a=rtpmap:8 PCMA/8000",
        "a=sendonly",
        "",
    ]
)
TALK_CANDIDATE = "candidate:4 1 udp 2130706431 192.0.2.20 50004 typ host"

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
    deleted: list[str] = field(default_factory=list)
    preloads: list[tuple[str, str]] = field(default_factory=list)
    preloads_deleted: list[str] = field(default_factory=list)
    talk_answers: list[str] = field(default_factory=list)
    talk_dial: bool = True
    _server: TestServer | None = None

    async def start(self) -> str:
        """Start on localhost; return the base URL."""
        app = web.Application()
        app.router.add_get("/api", self._api)
        app.router.add_get("/api/streams", self._get_streams)
        app.router.add_put("/api/streams", self._put_streams)
        app.router.add_delete("/api/streams", self._delete_streams)
        app.router.add_put("/api/preload", self._put_preload)
        app.router.add_delete("/api/preload", self._delete_preload)
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
        name, sources = request.query["name"], request.query.getall("src", [])
        self.puts.append((name, sources[0] if sources else ""))
        if any(sources):
            self.streams[name] = [source for source in sources if source]
        return web.Response(status=200)

    async def _delete_streams(self, request: web.Request) -> web.Response:
        name = request.query["src"]
        self.deleted.append(name)
        self.streams.pop(name, None)
        self.pushed.discard(name)
        return web.Response(status=200)

    async def _put_preload(self, request: web.Request) -> web.Response:
        """Like go2rtc: a microphone consumer makes it dial the talk source."""
        name = request.query["src"]
        self.preloads.append((name, request.query.get("microphone", "")))
        talk = next(
            (
                source.removeprefix("webrtc:")
                for source in self.streams.get(name, [])
                if source.startswith("webrtc:ws://")
            ),
            None,
        )
        if talk is None or not self.talk_dial:
            return web.Response(status=500, text="streams: no producer")
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.ws_connect(talk) as socket,
            ):
                await socket.send_json({"type": "webrtc/offer", "value": TALK_OFFER})
                await socket.send_json(
                    {"type": "webrtc/candidate", "value": TALK_CANDIDATE}
                )
                async with asyncio.timeout(5):
                    message = await socket.receive()
        except (aiohttp.ClientError, TimeoutError) as err:
            return web.Response(status=500, text=type(err).__name__)
        if message.type is not WSMsgType.TEXT:
            return web.Response(status=500, text="webrtc: no answer")
        data = json.loads(message.data)
        if data.get("type") != "webrtc/answer":
            return web.Response(status=500, text="wrong answer")
        self.talk_answers.append(data["value"])
        return web.Response(status=200)

    async def _delete_preload(self, request: web.Request) -> web.Response:
        name = request.query["src"]
        self.preloads_deleted.append(name)
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
