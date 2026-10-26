"""Bridge tests against a real go2rtc server (set GO2RTC_URL to run them).

A local WebRTC peer plays the relay: it sends audio (PCMU) and video (H264)
with an offer shaped like the relay's (named mids, sdes:mid extension). The
continuous live view also needs ffmpeg on the go2rtc host.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import re
import shutil
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aiohttp
import pytest

from custom_components.aiphone_jo.go2rtc import (
    PLACEHOLDER_SOURCE,
    Go2RtcBridge,
    Go2RtcServer,
)

aiortc = pytest.importorskip("aiortc")
from aiortc import RTCPeerConnection, RTCRtpSender, RTCSessionDescription  # noqa: E402
from aiortc.mediastreams import AudioStreamTrack, VideoStreamTrack  # noqa: E402

GO2RTC_URL = os.environ.get("GO2RTC_URL")
pytestmark = pytest.mark.skipif(not GO2RTC_URL, reason="GO2RTC_URL is not set")


@pytest.fixture(autouse=True, scope="session")
def mock_network() -> None:
    """Use the real network interfaces: the local peer needs ICE candidates."""


def _as_relay_offer(sdp: str) -> str:
    """Rename numeric mids to the relay's names."""
    sdp = re.sub(r"^a=mid:0\r?$", "a=mid:audio\r", sdp, flags=re.M)
    sdp = re.sub(r"^a=mid:1\r?$", "a=mid:video\r", sdp, flags=re.M)
    return sdp.replace("a=group:BUNDLE 0 1", "a=group:BUNDLE audio video")


def _as_local_answer(sdp: str) -> str:
    sdp = re.sub(r"^a=mid:audio\r?$", "a=mid:0\r", sdp, flags=re.M)
    sdp = re.sub(r"^a=mid:video\r?$", "a=mid:1\r", sdp, flags=re.M)
    return sdp.replace("a=group:BUNDLE audio video", "a=group:BUNDLE 0 1")


def _codecs(kind: str, mime: str) -> list[Any]:
    return [c for c in RTCRtpSender.getCapabilities(kind).codecs if c.mimeType == mime]


async def _gathered(pc: RTCPeerConnection) -> str:
    await pc.setLocalDescription(await pc.createOffer())
    while True:
        if pc.iceGatheringState == "complete":
            return pc.localDescription.sdp
        await asyncio.sleep(0.05)


async def _relay(bridge: Go2RtcBridge) -> RTCPeerConnection:
    """Push a session into the stream like the relay does."""
    pc = RTCPeerConnection()
    audio = pc.addTransceiver(AudioStreamTrack(), direction="sendonly")
    video = pc.addTransceiver(VideoStreamTrack(), direction="sendonly")
    audio.setCodecPreferences(_codecs("audio", "audio/PCMU"))
    video.setCodecPreferences(_codecs("video", "video/H264"))
    offer = _as_relay_offer(await _gathered(pc))
    assert "a=mid:audio" in offer
    assert "sdes:mid" in offer
    answer = await bridge.async_ingest(offer)
    assert "a=mid:audio" in answer
    assert "a=inactive" not in answer
    await pc.setRemoteDescription(
        RTCSessionDescription(_as_local_answer(answer), "answer")
    )
    return pc


@asynccontextmanager
async def _viewer(
    bridge: Go2RtcBridge, stream: str | None = None
) -> AsyncIterator[dict[str, list[float]]]:
    """Viewer connected through the WebSocket API; yields frame arrival times."""
    pc = RTCPeerConnection()
    pc.addTransceiver("video", direction="recvonly")
    pc.addTransceiver("audio", direction="recvonly")
    received: dict[str, list[float]] = {"video": [], "audio": []}
    readers: list[asyncio.Future[None]] = []
    answered: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    @pc.on("track")
    def on_track(track: Any) -> None:
        async def read() -> None:
            while True:
                try:
                    await track.recv()
                except Exception:
                    return
                received[track.kind].append(time.monotonic())

        readers.append(asyncio.ensure_future(read()))

    def on_message(message: dict[str, Any]) -> None:
        if message.get("type") == "webrtc/answer" and not answered.done():
            answered.set_result(message["value"])

    viewer = await bridge.async_open_viewer(on_message, stream)
    try:
        await viewer.async_send("webrtc/offer", await _gathered(pc))
        answer = await asyncio.wait_for(answered, 10)
        await pc.setRemoteDescription(RTCSessionDescription(answer, "answer"))
        yield received
    finally:
        await viewer.async_close()
        await pc.close()
        for reader in readers:
            reader.cancel()


async def _watch(bridge: Go2RtcBridge, seconds: float = 3) -> dict[str, int]:
    """Count the frames a viewer receives during ``seconds``."""
    async with _viewer(bridge) as received:
        await asyncio.sleep(seconds)
    return {kind: len(times) for kind, times in received.items()}


async def _frames_after(
    received: dict[str, list[float]], since: float, limit: float, count: int = 30
) -> float:
    """Wait for ``count`` video frames after ``since``; return the first one's time."""
    async with asyncio.timeout(limit):
        while True:
            after = [t for t in received["video"] if t > since]
            if len(after) >= count:
                return after[0]
            await asyncio.sleep(0.1)


@pytest.fixture
async def bridge(socket_enabled: None) -> Any:
    """Bridge on the real server, with a unique stream."""
    assert GO2RTC_URL
    session = aiohttp.ClientSession()
    server = Go2RtcServer(GO2RTC_URL, session)
    yield Go2RtcBridge(server, f"aiphone_jo_test_{os.getpid()}")
    await session.close()


async def test_relay_session_through_go2rtc(bridge: Go2RtcBridge) -> None:
    """Placeholder stream, pushed session, viewer and still image."""
    await bridge.async_ensure_stream()
    streams = await bridge._streams()
    assert streams is not None
    assert [p.get("url") for p in streams["producers"]] == [PLACEHOLDER_SOURCE]

    relay = await _relay(bridge)
    try:
        await bridge.async_wait_video(15)
        received = await _watch(bridge)
        assert received["video"] > 0
        assert received["audio"] > 0
        if shutil.which("ffmpeg"):
            image = await bridge.async_frame()
            assert image.startswith(b"\xff\xd8")
    finally:
        await relay.close()


async def test_next_session_on_same_stream(bridge: Go2RtcBridge) -> None:
    """A new session can be pushed into the same stream for new viewers."""
    await bridge.async_ensure_stream()
    for _ in range(2):
        relay = await _relay(bridge)
        try:
            await bridge.async_wait_video(15)
            assert (await _watch(bridge, 2))["video"] > 0
        finally:
            await relay.close()
        await asyncio.sleep(1)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is not installed")
async def test_relay_keeps_viewers_across_sessions(bridge: Go2RtcBridge) -> None:
    """A relay viewer keeps receiving when the next session replaces the first."""
    assert await bridge.async_relay_supported()
    await bridge.async_ensure_stream()
    await bridge.async_ensure_relay()
    relay = await _relay(bridge)
    try:
        await bridge.async_wait_video(15)
        async with _viewer(bridge, bridge.relay) as received:
            await _frames_after(received, 0, 20)
            assert received["audio"]
            image = await bridge.async_frame(bridge.relay)
            assert image.startswith(b"\xff\xd8")

            # The station ends the session; the next one is pushed shortly after.
            await relay.close()
            ended = time.monotonic()
            await asyncio.sleep(1)
            relay = await _relay(bridge)
            await _frames_after(received, ended + 0.5, 30)
            audio = len(received["audio"])
            await asyncio.sleep(1)
            assert len(received["audio"]) > audio
        video = [t for t in received["video"] if t > ended - 1]
        gap = max(b - a for a, b in itertools.pairwise(video))
        print(f"longest video gap across the renewal: {gap:.1f} s")
    finally:
        await relay.close()
