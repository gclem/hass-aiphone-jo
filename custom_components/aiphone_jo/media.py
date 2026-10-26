"""Live view and still images of the entrance stations through go2rtc.

Each live view or image opens a monitoring session on an entrance station and
pushes it into a go2rtc stream. The station ends each session after about a
minute; while viewers are connected, a new session is opened each time and
viewers keep watching through the relay stream (see :mod:`.go2rtc`), with a
gap of a few seconds. Without the relay (go2rtc's RTSP server disabled),
viewers read the session stream and the live view stops with the session.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeVar

from homeassistant.components.camera import (
    WebRTCAnswer,
    WebRTCCandidate,
    WebRTCError,
    WebRTCSendMessage,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from webrtc_models import RTCIceCandidateInit

from .api import Station
from .const import CONF_CALL_TALK, CONF_IMAGE_REFRESH, DEFAULT_IMAGE_REFRESH, DOMAIN
from .go2rtc import (
    PLACEHOLDER_SOURCE,
    Go2RtcBridge,
    Go2RtcError,
    Go2RtcViewer,
    async_get_server,
)
from .talk import SendOffer, TalkPath, TalkServer
from .vendor.aiphone_jo import AiphoneJoError, StationBusyError

if TYPE_CHECKING:
    from .coordinator import AiphoneJoRuntime

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")

VIDEO_TIMEOUT = 20.0
SESSION_POLL = 1.0
# Seconds the live session is kept after the last viewer left, so a viewer that
# reconnects (dashboard reload, dialog reopened) does not open a new session.
VIEWER_GRACE = 5.0
ISSUE_GO2RTC_UNAVAILABLE = "go2rtc_unavailable"
ISSUE_VIDEO_UNSUPPORTED = "video_unsupported"
ISSUES = (ISSUE_GO2RTC_UNAVAILABLE, ISSUE_VIDEO_UNSUPPORTED)
LIVE_ENDED = (
    "The live view ended: the entrance station closes each session after "
    "about one minute. Open the camera again to continue."
)
LIVE_STOPPED = (
    "The live view stopped: no new session could be opened on the entrance "
    "station. Open the camera again to retry."
)
CALL_STARTED = "A call was answered: open the camera again to see the visitor."
CALL_ENDED = "The call ended."
STOP_MESSAGES = {
    "live_ended": LIVE_ENDED,
    "call_started": CALL_STARTED,
    "call_ended": CALL_ENDED,
}


class NoStationError(AiphoneJoError):
    """No entrance station with a camera is known."""


class MediaBusyError(AiphoneJoError):
    """A live view uses the monitoring session."""


class CameraMedia:
    """Shared live session, viewers and cached still images."""

    def __init__(self, hass: HomeAssistant, runtime: AiphoneJoRuntime) -> None:
        """Initialize."""
        self._hass = hass
        self._runtime = runtime
        entry = runtime.entry
        self._image: bytes | None = None
        self._image_at: float | None = None
        self._logged_error: str | None = None
        server = async_get_server(hass, dict(entry.options))
        self._bridge = (
            Go2RtcBridge(server, f"aiphone_jo_{entry.entry_id}") if server else None
        )
        self.unavailable_reason: str | None = None
        if not hasattr(runtime.client, "live_monitor"):
            self.unavailable_reason = ISSUE_VIDEO_UNSUPPORTED
        elif self._bridge is None:
            self.unavailable_reason = ISSUE_GO2RTC_UNAVAILABLE
        self._live: Any = None
        self._call_session: Any = None
        self._call_stream: str | None = None
        self._talk_server: TalkServer | None = None
        self._talk: TalkPath | None = None
        self._watch: asyncio.Task[None] | None = None
        self._stopper: asyncio.Task[None] | None = None
        self._continuous: bool | None = None
        self._users = 0
        self._start_lock = asyncio.Lock()
        self._image_lock = asyncio.Lock()
        self._viewers: dict[str, tuple[Go2RtcViewer, WebRTCSendMessage]] = {}

    # ------------------------------------------------------------ status

    @property
    def available(self) -> bool:
        """True when the live view and images can work."""
        return self.unavailable_reason is None

    @property
    def live(self) -> bool:
        """True while a live or call session feeds the stream."""
        return self._live is not None or self._call_session is not None

    @property
    def _viewer_stream(self) -> str | None:
        """Stream read by viewers and live images (None: the session stream)."""
        if self._call_stream is not None:
            return self._call_stream
        bridge = self._bridge
        return bridge.relay if bridge is not None and self._continuous else None

    @property
    def refresh_interval(self) -> float:
        """Maximum age of the cached image, in seconds."""
        options = self._runtime.entry.options
        return float(options.get(CONF_IMAGE_REFRESH, DEFAULT_IMAGE_REFRESH))

    def async_update_issues(self) -> None:
        """Show why the camera is unavailable, or clear the notice."""
        for issue in ISSUES:
            if issue == self.unavailable_reason:
                ir.async_create_issue(
                    self._hass,
                    DOMAIN,
                    issue,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=issue,
                )
            else:
                ir.async_delete_issue(self._hass, DOMAIN, issue)

    def _log_error(self, err: Exception) -> None:
        key = type(err).__name__
        if key != self._logged_error:
            _LOGGER.warning("Camera image or live view failed: %s (%s)", key, err)
            self._logged_error = key
        else:
            _LOGGER.debug("Camera image or live view failed: %s", key)

    def _fresh(self) -> bool:
        return (
            self._image is not None
            and self._image_at is not None
            and time.monotonic() - self._image_at < self.refresh_interval
        )

    def _store(self, image: bytes) -> None:
        self._image = image
        self._image_at = time.monotonic()
        if self._logged_error is not None:
            _LOGGER.info("Camera images are available again")
            self._logged_error = None

    # -------------------------------------------------------- live session

    async def _with_station(self, action: Callable[[Station], Awaitable[_T]]) -> _T:
        stations = self._runtime.session_stations()
        if not stations:
            raise NoStationError("No entrance station with a camera")
        busy: StationBusyError | None = None
        for station in stations:
            try:
                return await action(station)
            except StationBusyError as err:
                busy = err
        assert busy is not None
        raise busy

    async def _async_start(self) -> None:
        bridge = self._bridge
        assert bridge is not None
        async with self._start_lock:
            if self.live:
                return
            client: Any = self._runtime.client
            if self._continuous is None:
                self._continuous = await bridge.async_relay_supported()
            # Renewed sessions only reach viewers through the relay. Image-only
            # sessions are renewed too, so that a viewer can join them.
            renew = self._continuous

            async def enter(station: Station) -> Any:
                live = client.live_monitor(bridge.async_ingest, station.id, renew=renew)
                await live.__aenter__()
                return live

            async with self._runtime.session_lock:
                await bridge.async_ensure_stream()
                if renew:
                    await bridge.async_ensure_relay()
                live = await self._with_station(enter)
            self._live = live
            self._watch = self._runtime.entry.async_create_background_task(
                self._hass, self._async_watch(live, renew), f"{DOMAIN} live session"
            )
            try:
                await bridge.async_wait_video(VIDEO_TIMEOUT)
            except Go2RtcError:
                await self._async_stop()
                raise

    async def _async_watch(self, live: Any, renew: bool) -> None:
        """Stop when the session ends, or when renewing it failed."""
        while True:
            if not (live.running if renew else live.session is not None):
                break
            await asyncio.sleep(SESSION_POLL)
        if self._live is live:
            if renew:
                _LOGGER.warning("The live view stopped: %s", live.error)
            await self._async_stop(notice="live_ended")

    async def _async_stop(self, *, notice: str | None = None) -> None:
        self._cancel_stopper()
        live, self._live = self._live, None
        watch, self._watch = self._watch, None
        if watch is not None and watch is not asyncio.current_task():
            watch.cancel()
        viewers, self._viewers = self._viewers, {}
        self._users = max(0, self._users - len(viewers))
        message = STOP_MESSAGES.get(notice or "", "")
        if notice == "live_ended" and self._continuous:
            message = LIVE_STOPPED
        for viewer, send_message in viewers.values():
            if notice is not None:
                send_message(WebRTCError(notice, message))
            await viewer.async_close()
        if live is not None:
            with contextlib.suppress(Exception):
                await live.__aexit__(None, None, None)

    def _cancel_stopper(self) -> None:
        stopper, self._stopper = self._stopper, None
        if stopper is not None and stopper is not asyncio.current_task():
            stopper.cancel()

    async def _async_stop_later(self, delay: float) -> None:
        await asyncio.sleep(delay)
        # From here on, a new user must not cancel the session being closed.
        self._stopper = None
        if self._users == 0:
            await self._async_stop()

    async def async_acquire(self) -> None:
        """Use the live session, starting it if needed."""
        self._users += 1
        self._cancel_stopper()
        try:
            await self._async_start()
        except BaseException:
            await self.async_release()
            raise

    async def async_release(self, grace: float = 0) -> None:
        """Stop using the live session; it stops with its last user.

        With ``grace``, the session is kept that many seconds for a new user.
        """
        self._users = max(0, self._users - 1)
        if self._users or self._live is None:
            return
        if grace <= 0:
            await self._async_stop()
        elif self._stopper is None:
            self._stopper = self._runtime.entry.async_create_background_task(
                self._hass, self._async_stop_later(grace), f"{DOMAIN} live grace"
            )

    async def async_shutdown(self) -> None:
        """Stop everything (unload)."""
        self._users = 0
        self._call_session = None
        await self._async_stop()
        await self._async_close_call_stream()
        server, self._talk_server = self._talk_server, None
        if server is not None:
            await server.async_stop()

    # ------------------------------------------------------------- calls

    async def async_prepare_call(self, notice: str = "call_started") -> None:
        """Free the monitoring session for a call; live viewers are told why."""
        await self._async_stop(notice=notice)

    @property
    def talk_enabled(self) -> bool:
        """True when talking to the visitor is enabled in the options."""
        return bool(self._runtime.entry.options.get(CONF_CALL_TALK, False))

    @property
    def talk(self) -> Callable[[SendOffer], Awaitable[None]] | None:
        """Talk-path callback for the call being answered, if any."""
        return self._talk.async_talk if self._talk is not None else None

    async def _async_open_talk(self, bridge: Go2RtcBridge) -> TalkPath | None:
        try:
            if self._talk_server is None:
                server = TalkServer()
                await server.async_start()
                self._talk_server = server
        except OSError as err:
            _LOGGER.warning("Talking is unavailable: %s", err)
            return None
        return TalkPath(self._talk_server, bridge, bridge.call_stream)

    async def async_call_negotiator(self) -> Callable[[str], Awaitable[str]]:
        """Create the call stream for an answered call; returns its SDP negotiator."""
        bridge = self._bridge
        if not self.available or bridge is None:
            raise Go2RtcError("Video is unavailable")
        await self._async_close_call_stream()
        sources = [PLACEHOLDER_SOURCE]
        if self.talk_enabled and (talk := await self._async_open_talk(bridge)):
            self._talk = talk
            sources.append(talk.source)
        stream = bridge.call_stream
        self._call_stream = stream
        await bridge.async_create_stream(stream, *sources)

        async def negotiate(offer: str) -> str:
            return await bridge.async_ingest(offer, stream)

        return negotiate

    def async_attach_call(self, session: Any) -> None:
        """The call stream is fed by an answered call."""
        self._call_session = session

    async def _async_close_call_stream(self) -> None:
        talk, self._talk = self._talk, None
        if talk is not None:
            await talk.async_close()
        stream, self._call_stream = self._call_stream, None
        bridge = self._bridge
        if stream is not None and bridge is not None:
            with contextlib.suppress(Go2RtcError):
                await bridge.async_delete_stream(stream)

    async def async_end_call(self) -> None:
        """The call ended (or could not be answered): close its stream."""
        if self._call_session is not None:
            self._call_session = None
            await self._async_stop(notice="call_ended")
        await self._async_close_call_stream()

    # ------------------------------------------------------- ring images

    async def async_ring_image(self, call: Any, limit: float) -> bytes:
        """Image of the visitor while a call rings, without answering it.

        An answered call is read from its stream. Otherwise the ringing call's
        session is taken over just long enough to receive its video (no
        READY, REPLY, reject nor hang-up), then left with LEAVE_USER only, so
        the call keeps ringing everywhere and can still be answered.
        """
        bridge = self._bridge
        if not self.available or bridge is None:
            raise Go2RtcError("Video is unavailable")
        lock = self._runtime.session_lock
        async with asyncio.timeout(limit):
            # Waits for an answer in progress, whose stream is then read.
            await lock.acquire()
        session: Any = None
        try:
            if self._call_session is not None and self._call_stream is not None:
                stream = self._call_stream
                lock.release()
                lock = None
                async with asyncio.timeout(limit):
                    await bridge.async_wait_video(limit, stream=stream)
                    image = await bridge.async_frame(stream)
            else:
                if self._live is not None:
                    raise MediaBusyError("A live view is open")
                client: Any = self._runtime.client
                stream = bridge.ring_stream
                async with asyncio.timeout(limit):
                    session = await client.answer_call(call)
                    await bridge.async_create_stream(stream, PLACEHOLDER_SOURCE)
                    offer = await session.request_offer()
                    await session.send_answer(await bridge.async_ingest(offer, stream))
                    await bridge.async_wait_video(limit, stream=stream)
                    image = await bridge.async_frame(stream)
        finally:
            try:
                if session is not None:
                    await self._async_leave_ring_fully(session, bridge)
            finally:
                if lock is not None:
                    lock.release()
        self._store(image)
        return image

    async def _async_leave_ring_fully(self, session: Any, bridge: Go2RtcBridge) -> None:
        """Leave the call before the locks are released, even if cancelled again."""
        leave = asyncio.ensure_future(self._async_leave_ring(session, bridge))
        cancelled = False
        while not leave.done():
            try:
                await asyncio.shield(leave)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    @staticmethod
    async def _async_leave_ring(session: Any, bridge: Go2RtcBridge) -> None:
        # Never reject nor hang up: that would end the call for every phone.
        leave = getattr(session, "withdraw", None) or session._withdraw
        try:
            await leave()
        except AiphoneJoError as err:
            _LOGGER.debug("Leaving the ringing call failed: %s", err)
        with contextlib.suppress(Go2RtcError):
            await bridge.async_delete_stream(bridge.ring_stream)

    # ------------------------------------------------------------ images

    async def async_image(self) -> bytes | None:
        """Cached image, refreshed when older than the refresh interval."""
        bridge = self._bridge
        if not self.available or bridge is None:
            return self._image
        if not self.live and self._fresh():
            return self._image
        async with self._image_lock:
            if not self.live and self._fresh():
                return self._image
            try:
                if self.live:
                    image = await bridge.async_frame(self._viewer_stream)
                else:
                    await self.async_acquire()
                    try:
                        image = await bridge.async_frame()
                    finally:
                        await self.async_release()
            except (AiphoneJoError, Go2RtcError) as err:
                self._log_error(err)
                return self._image
            self._store(image)
            return image

    # ----------------------------------------------------------- viewers

    async def async_open_viewer(
        self, session_id: str, offer_sdp: str, send_message: WebRTCSendMessage
    ) -> None:
        """Connect a browser to the live view."""
        bridge = self._bridge
        if not self.available or bridge is None:
            send_message(WebRTCError("unavailable", "The camera is unavailable."))
            return
        try:
            await self.async_acquire()
        except (AiphoneJoError, Go2RtcError) as err:
            self._log_error(err)
            reason = type(err).__name__
            send_message(
                WebRTCError("live_failed", f"The live view could not start ({reason}).")
            )
            return

        def on_message(message: dict[str, Any]) -> None:
            kind, value = message.get("type"), message.get("value")
            if kind == "webrtc/answer" and isinstance(value, str):
                send_message(WebRTCAnswer(value))
            elif kind == "webrtc/candidate" and isinstance(value, str):
                send_message(WebRTCCandidate(RTCIceCandidateInit(value)))
            elif kind == "error":
                send_message(WebRTCError("go2rtc_error", str(value)))

        try:
            viewer = await bridge.async_open_viewer(on_message, self._viewer_stream)
        except Go2RtcError as err:
            self._log_error(err)
            await self.async_release()
            send_message(WebRTCError("live_failed", "The live view could not start."))
            return
        self._viewers[session_id] = (viewer, send_message)
        await viewer.async_send("webrtc/offer", offer_sdp)

    async def async_viewer_candidate(self, session_id: str, candidate: str) -> None:
        """Forward a browser ICE candidate."""
        if entry := self._viewers.get(session_id):
            await entry[0].async_send("webrtc/candidate", candidate)

    async def async_close_viewer(self, session_id: str) -> None:
        """A browser left the live view."""
        if entry := self._viewers.pop(session_id, None):
            await entry[0].async_close()
            await self.async_release(VIEWER_GRACE)

    def async_notify(self) -> None:
        """Refresh entity states."""
        entry_id = self._runtime.entry.entry_id
        async_dispatcher_send(self._hass, f"{DOMAIN}_{entry_id}_update")
