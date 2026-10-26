"""Optional video support based on aiortc (``pip install 'aiphone-jo[media]'``).

The core never imports this module: :meth:`AiphoneJoClient.snapshot` and
:meth:`AiphoneJoClient.video_stream` load it on demand and raise
:class:`~aiphone_jo.exceptions.MediaUnavailableError` when the extra is missing.

Negotiation: the relay (a Janus gateway, ICE-lite) sends the SDP offer (H264
video, PCMU audio); an aiortc peer connection without STUN/TURN servers
answers ``recvonly`` with a complete SDP (ICE gathering finished, no trickle).
Session renewal is handled by :class:`~aiphone_jo.live.LiveMonitor`.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
from collections.abc import AsyncGenerator, Callable
from types import TracebackType
from typing import TYPE_CHECKING, Self

from .exceptions import MediaTimeoutError, MediaUnavailableError

try:
    import av
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
    from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
    from PIL import Image  # noqa: F401 - required by VideoFrame.to_image()
except ImportError as err:  # pragma: no cover - exercised through the client
    raise MediaUnavailableError(
        "Video support requires the media extra: pip install 'aiphone-jo[media]'."
    ) from err

if TYPE_CHECKING:
    from .client import AiphoneJoClient
    from .cloud_api import Terminal
    from .live import LiveMonitor

_LOGGER = logging.getLogger(__name__)

DEFAULT_JPEG_QUALITY = 80
# Renew the session when no video frame arrived for this long.
FRAME_TIMEOUT = 10.0
_WATCHDOG_PERIOD = 1.0

FrameCallback = Callable[[av.VideoFrame], None]


def encode_jpeg(frame: av.VideoFrame, quality: int = DEFAULT_JPEG_QUALITY) -> bytes:
    """Encode a decoded video frame as JPEG."""
    if not 1 <= quality <= 100:
        raise ValueError("JPEG quality must be between 1 and 100.")
    buffer = io.BytesIO()
    frame.to_image().save(buffer, format="JPEG", quality=quality)  # type: ignore[no-untyped-call]
    return buffer.getvalue()


class Receiver:
    """One receive-only aiortc peer connection answering a relay offer.

    :meth:`negotiate` has the ``async (offer_sdp) -> answer_sdp`` signature
    expected by :meth:`AiphoneJoClient.live_monitor`. Every decoded video frame
    is passed to ``on_frame``; audio is received and discarded.
    """

    def __init__(self, on_frame: FrameCallback) -> None:
        self._on_frame = on_frame
        self._pc: RTCPeerConnection | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def negotiate(self, offer_sdp: str) -> str:
        """Apply the relay offer and return the complete local answer SDP."""
        await self.close()
        # No STUN/TURN: aiortc would otherwise contact a public STUN server.
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self._pc = pc

        @pc.on("track")
        def on_track(track: MediaStreamTrack) -> None:
            task = asyncio.create_task(self._pump(track), name=f"aiphone-jo-{track.kind}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
        for transceiver in pc.getTransceivers():
            transceiver.direction = "recvonly"
        await pc.setLocalDescription(await pc.createAnswer())
        description = pc.localDescription
        return description.sdp

    async def _pump(self, track: MediaStreamTrack) -> None:
        video = track.kind == "video"
        while True:
            try:
                frame = await track.recv()
            except MediaStreamError:
                return
            if video and isinstance(frame, av.VideoFrame):
                self._on_frame(frame)

    async def close(self) -> None:
        """Close the peer connection (idempotent)."""
        pc, self._pc = self._pc, None
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if pc is not None:
            await pc.close()


async def snapshot(
    client: AiphoneJoClient,
    terminal: Terminal | int | None = None,
    *,
    timeout: float = 15.0,
    quality: int = DEFAULT_JPEG_QUALITY,
) -> bytes:
    """JPEG of the first complete video frame (see :meth:`AiphoneJoClient.snapshot`)."""
    for stream in list(client._video_streams):
        if isinstance(stream, VideoStream) and stream.running:
            return await stream.wait_jpeg(timeout=timeout, quality=quality)
    first: asyncio.Future[av.VideoFrame] = asyncio.get_running_loop().create_future()

    def on_frame(frame: av.VideoFrame) -> None:
        if not first.done():
            first.set_result(frame)

    receiver = Receiver(on_frame)
    try:
        async with asyncio.timeout(timeout):
            async with client.live_monitor(
                receiver.negotiate, terminal, on_session_end=receiver.close, renew=False
            ):
                frame = await first
    except TimeoutError as err:
        raise MediaTimeoutError(f"No video frame within {timeout} s.") from err
    finally:
        await receiver.close()
    return encode_jpeg(frame, quality)


class VideoStream:
    """Live video of the entrance station, renewed every ~60 s until the context exits.

    ``frames()`` yields decoded :class:`av.VideoFrame` objects (slow consumers
    skip frames); ``latest_jpeg()`` encodes the most recent one. While the
    stream is open, :meth:`AiphoneJoClient.open_door` reuses its session and
    :meth:`AiphoneJoClient.snapshot` returns its latest frame.
    """

    def __init__(
        self,
        client: AiphoneJoClient,
        terminal: Terminal | int | None = None,
        *,
        frame_timeout: float = FRAME_TIMEOUT,
    ) -> None:
        self._client = client
        self._terminal = terminal
        self._frame_timeout = frame_timeout
        self._live: LiveMonitor | None = None
        self._receiver: Receiver | None = None
        self._latest: av.VideoFrame | None = None
        self._frame_event = asyncio.Event()
        self._subscribers: set[asyncio.Queue[av.VideoFrame | None]] = set()
        self._watchdog: asyncio.Task[None] | None = None
        self._negotiated_at = 0.0
        self._last_frame_at = 0.0
        self._closed = False

    @property
    def running(self) -> bool:
        """``True`` while the stream is open and renewing."""
        return not self._closed and self._live is not None and self._live.running

    @property
    def sessions(self) -> int:
        """Number of monitoring sessions negotiated so far."""
        return self._live.sessions if self._live is not None else 0

    async def __aenter__(self) -> Self:
        self._live = self._client.live_monitor(
            self._negotiate, self._terminal, on_session_end=self._session_ended
        )
        self._client._video_streams.append(self)
        try:
            await self._live.__aenter__()
        except BaseException:
            self._client._video_streams.remove(self)
            await self._session_ended()
            raise
        self._watchdog = asyncio.create_task(self._watch(), name="aiphone-jo-video-watchdog")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._closed = True
        watchdog, self._watchdog = self._watchdog, None
        if watchdog is not None:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog
        try:
            if self._live is not None:
                await self._live.__aexit__(exc_type, exc, traceback)
        finally:
            await self._session_ended()
            with contextlib.suppress(ValueError):
                self._client._video_streams.remove(self)
            self._broadcast(None)

    async def _negotiate(self, offer_sdp: str) -> str:
        receiver = Receiver(self._on_frame)
        self._receiver = receiver
        answer = await receiver.negotiate(offer_sdp)
        self._negotiated_at = asyncio.get_running_loop().time()
        return answer

    async def _session_ended(self) -> None:
        receiver, self._receiver = self._receiver, None
        if receiver is not None:
            await receiver.close()

    def _on_frame(self, frame: av.VideoFrame) -> None:
        self._latest = frame
        self._last_frame_at = asyncio.get_running_loop().time()
        self._frame_event.set()
        self._broadcast(frame)

    def _broadcast(self, frame: av.VideoFrame | None) -> None:
        for queue in self._subscribers:
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(frame)

    async def _watch(self) -> None:
        live = self._live
        if live is None:
            return
        loop = asyncio.get_running_loop()
        while live.running:
            await asyncio.sleep(_WATCHDOG_PERIOD)
            if live.session is None:
                continue
            since = max(self._negotiated_at, self._last_frame_at)
            if loop.time() - since > self._frame_timeout:
                _LOGGER.warning("No video for %.0f s, renewing the session", self._frame_timeout)
                self._negotiated_at = loop.time()
                await live.renew()
        # Renewal gave up: wake up the consumers.
        self._broadcast(None)

    async def frames(self) -> AsyncGenerator[av.VideoFrame]:
        """Decoded video frames until the context exits.

        Raises the renewal error (for example :class:`StationBusyError`) if the
        stream could not be renewed.
        """
        queue: asyncio.Queue[av.VideoFrame | None] = asyncio.Queue(maxsize=2)
        self._subscribers.add(queue)
        try:
            while not self._closed:
                frame = await queue.get()
                if frame is None:
                    break
                yield frame
        finally:
            self._subscribers.discard(queue)
        if self._live is not None and self._live.error is not None and not self._closed:
            raise self._live.error

    def latest_jpeg(self, quality: int = DEFAULT_JPEG_QUALITY) -> bytes | None:
        """JPEG of the most recent frame, or ``None`` before the first frame."""
        return encode_jpeg(self._latest, quality) if self._latest is not None else None

    async def wait_jpeg(
        self, *, timeout: float = 15.0, quality: int = DEFAULT_JPEG_QUALITY
    ) -> bytes:
        """Latest frame as JPEG, waiting up to ``timeout`` for the first one."""
        try:
            async with asyncio.timeout(timeout):
                await self._frame_event.wait()
        except TimeoutError as err:
            raise MediaTimeoutError(f"No video frame within {timeout} s.") from err
        image = self.latest_jpeg(quality)
        if image is None:  # pragma: no cover - the event is only set with a frame
            raise MediaTimeoutError("No video frame available.")
        return image
