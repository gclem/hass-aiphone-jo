"""Live view and still images of the entrance stations through go2rtc.

Each live view or image opens a monitoring session on an entrance station and
pushes it into a go2rtc stream. The intercom runs one session at a time, so
the cameras of the entrance stations share it: while it serves the live view
of one station (or an answered call), the other cameras keep their last
image.

The station ends each session after about a minute; while viewers are
connected, a new session is opened each time and viewers keep watching
through the relay stream (see :mod:`.go2rtc`), with a gap of a few seconds.
Without the relay (go2rtc's RTSP server disabled), viewers read the session
stream and the live view stops with the session.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

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
from .health import PROBLEM_CAMERA_FAILED
from .talk import SendOffer, TalkPath, TalkServer
from .vendor.aiphone_jo import AiphoneJoError

if TYPE_CHECKING:
    from .coordinator import AiphoneJoRuntime

_LOGGER = logging.getLogger(__name__)

VIDEO_TIMEOUT = 20.0
# Seconds an image of the live view waits for video: when the station renews
# its session, the live stream restarts and briefly carries no video.
LIVE_IMAGE_WAIT = 8.0
# Seconds a camera without any image yet (after a restart) waits for the
# session used by another station, instead of returning no image at all.
FIRST_IMAGE_WAIT = 15.0
FIRST_IMAGE_POLL = 0.5
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
CALL_STARTED = (
    "A call was answered: open the camera of the entrance station that rang "
    "to see the visitor."
)
CALL_ENDED = "The call ended."
STOP_MESSAGES = {
    "live_ended": LIVE_ENDED,
    "call_started": CALL_STARTED,
    "call_ended": CALL_ENDED,
}


class MediaBusyError(AiphoneJoError):
    """A live view or a call uses the monitoring session."""


def _timeout(limit: float, step: str) -> TimeoutError:
    return TimeoutError(f"Timed out after {limit:g} s {step}")


class StationRingingError(AiphoneJoError):
    """The station is calling: monitoring it may end the call."""


STATION_RINGING = (
    "This entrance station is ringing: opening the live view now could end the "
    "call. Answer the call to see the visitor, or open the camera again when "
    "the ringing stops."
)
CAMERA_BUSY = (
    "The intercom shows one entrance station at a time, and another one is "
    "being viewed or is in a call. Close it, or open this camera again when "
    "it ends."
)


class CameraMedia:
    """Live session shared by the cameras, viewers and cached still images."""

    def __init__(self, hass: HomeAssistant, runtime: AiphoneJoRuntime) -> None:
        """Initialize."""
        self._hass = hass
        self._runtime = runtime
        entry = runtime.entry
        # Last image of each station, and when it was taken (monotonic).
        self._images: dict[int, tuple[bytes, float]] = {}
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
        self._live_station: int | None = None
        self._call_session: Any = None
        self._call_station: int | None = None
        self._call_stream: str | None = None
        self._talk_server: TalkServer | None = None
        self._talk: TalkPath | None = None
        self._watch: asyncio.Task[None] | None = None
        self._stopper: asyncio.Task[None] | None = None
        self._continuous: bool | None = None
        self._users = 0
        # One image or session start at a time: an image of one station never
        # makes a viewer of another station fail, it only delays it.
        self._lock = asyncio.Lock()
        self._refreshes: dict[int, asyncio.Task[bytes | None]] = {}
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
    def session_station(self) -> int | None:
        """Station whose session feeds the stream, if any."""
        if self._call_session is not None:
            return self._call_station
        if self._live is not None:
            return self._live_station
        return None

    def live_on(self, station_id: int) -> bool:
        """True while a live or call session of the station feeds the stream."""
        return self.live and self.session_station == station_id

    def cached_image(self, station_id: int) -> bytes | None:
        """Last image of the station, if any."""
        cached = self._images.get(station_id)
        return cached[0] if cached is not None else None

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
            # Reported once per kind of error, like the log, not on every image.
            self._runtime.health.report(PROBLEM_CAMERA_FAILED, err)
        else:
            _LOGGER.debug("Camera image or live view failed: %s", key)

    def _keep(self, station_id: int) -> bool:
        """True when the station's last image is returned without a session."""
        if self.live_on(station_id):
            return False
        cached = self._images.get(station_id)
        if cached is not None and time.monotonic() - cached[1] < self.refresh_interval:
            return True
        if station_id == self._runtime.ringing_station_id:
            # Monitoring the station that calls may end the call.
            return True
        # The session serves the live view of another station, or a call.
        return bool(self._viewers) or self._call_session is not None

    def _busy_elsewhere(self, station_id: int) -> bool:
        """True while the session serves another station (live view, call)."""
        if self.live_on(station_id):
            return False
        return (
            bool(self._viewers)
            or self._call_session is not None
            or (self.live and self._users > 0)
        )

    async def _async_wait_first_image(self, station_id: int) -> None:
        """Without any image of the station yet, wait for the session a while."""
        if self.cached_image(station_id) is not None:
            return
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(FIRST_IMAGE_WAIT):
                while True:
                    if not self._busy_elsewhere(station_id):
                        return
                    await asyncio.sleep(FIRST_IMAGE_POLL)

    def _store(self, station_id: int, image: bytes) -> None:
        self._images[station_id] = (image, time.monotonic())
        if self._logged_error is not None:
            _LOGGER.info("Camera images are available again")
            self._logged_error = None

    # -------------------------------------------------------- live session

    def _check_not_ringing(self, station: Station) -> None:
        if station.id == self._runtime.ringing_station_id:
            raise StationRingingError("The entrance station is ringing")

    async def _async_start(self, station: Station) -> None:
        bridge = self._bridge
        assert bridge is not None
        client: Any = self._runtime.client
        if self._continuous is None:
            self._continuous = await bridge.async_relay_supported()
        # Renewed sessions only reach viewers through the relay. Image-only
        # sessions are renewed too, so that a viewer can join them.
        renew = self._continuous
        async with self._runtime.session_lock:
            # The station may have started ringing while waiting for the lock.
            self._check_not_ringing(station)
            await bridge.async_ensure_stream()
            if renew:
                await bridge.async_ensure_relay()
            live = client.live_monitor(bridge.async_ingest, station.id, renew=renew)
            await live.__aenter__()
        self._live = live
        self._live_station = station.id
        self.async_notify()
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
        self._live_station = None
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
            self.async_notify()
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

    async def async_acquire(self, station: Station) -> None:
        """Use the live session of the station, starting it if needed.

        Called with the media lock held. A session kept for a returning viewer
        of another station is stopped first; one with viewers, or a call, is
        never taken over (``MediaBusyError``).
        """
        if self.live_on(station.id):
            # Joining a running session opens no new one, even while ringing.
            self._users += 1
            self._cancel_stopper()
            return
        self._check_not_ringing(station)
        if self.live:
            if self._users or self._call_session is not None:
                raise MediaBusyError("Another entrance station is monitored")
            await self._async_stop()
        self._users += 1
        try:
            await self._async_start(station)
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
        self._call_station = None
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

    def async_attach_call(self, session: Any, station_id: int | None = None) -> None:
        """The call stream is fed by an answered call of the station."""
        self._call_session = session
        self._call_station = station_id
        self.async_notify()

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
            self._call_station = None
            self.async_notify()
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
        # Named in the error when the time runs out.
        step = "waiting for another session to end"
        session: Any = None
        try:
            async with asyncio.timeout(limit):
                # Waits for an answer in progress, whose stream is then read.
                await lock.acquire()
        except TimeoutError as err:
            raise _timeout(limit, step) from err
        try:
            if self._call_session is not None and self._call_stream is not None:
                stream = self._call_stream
                lock.release()
                lock = None
                async with asyncio.timeout(limit):
                    step = "waiting for the call's video"
                    await bridge.async_wait_video(limit, stream=stream)
                    step = "reading the image"
                    image = await bridge.async_frame(stream)
            else:
                if self._live is not None:
                    raise MediaBusyError("A live view is open")
                client: Any = self._runtime.client
                stream = bridge.ring_stream
                async with asyncio.timeout(limit):
                    step = "joining the call"
                    session = await client.answer_call(call)
                    await bridge.async_create_stream(stream, PLACEHOLDER_SOURCE)
                    step = "negotiating the call's video"
                    offer = await session.request_offer()
                    await session.send_answer(await bridge.async_ingest(offer, stream))
                    step = "waiting for the call's video"
                    await bridge.async_wait_video(limit, stream=stream)
                    step = "reading the image"
                    image = await bridge.async_frame(stream)
        except TimeoutError as err:
            raise _timeout(limit, step) from err
        finally:
            try:
                if session is not None:
                    await self._async_leave_ring_fully(session, bridge)
            finally:
                if lock is not None:
                    lock.release()
        if call.term_id is not None:
            self._store(call.term_id, image)
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
        # withdraw() is public since library 0.4.1.
        leave = getattr(session, "withdraw", None) or session._withdraw
        try:
            await leave()
        except AiphoneJoError as err:
            _LOGGER.debug("Leaving the ringing call failed: %s", err)
        with contextlib.suppress(Go2RtcError):
            await bridge.async_delete_stream(bridge.ring_stream)

    # ------------------------------------------------------------ images

    async def async_image(self, station: Station) -> bytes | None:
        """Image of the station, refreshed when older than the refresh interval.

        Home Assistant gives up on a camera image after 10 seconds, about the
        time a monitoring session takes to start. So the refresh runs in the
        background: a camera that has an image returns it at once while the
        refresh serves the next request, and a camera without any image waits
        for the refresh, which still completes if Home Assistant gives up.
        """
        if not self.available or self._bridge is None:
            return self.cached_image(station.id)
        cached = self.cached_image(station.id)
        if cached is not None and self._keep(station.id):
            return cached
        task = self._refreshes.get(station.id)
        if task is None or task.done():
            task = self._runtime.entry.async_create_background_task(
                self._hass, self._async_refresh(station), f"{DOMAIN} camera image"
            )
            self._refreshes[station.id] = task
        if cached is not None and not self.live_on(station.id):
            return cached
        return await asyncio.shield(task)

    async def _async_refresh(self, station: Station) -> bytes | None:
        """New image of the station, or its last one when none can be taken.

        While the session serves another station (live view or call), or while
        the station rings, the last image is kept. A camera without any image
        yet first waits a little for the session to be free.
        """
        bridge = self._bridge
        assert bridge is not None
        await self._async_wait_first_image(station.id)
        if self._keep(station.id):
            return self.cached_image(station.id)
        async with self._lock:
            if self._keep(station.id):
                return self.cached_image(station.id)
            try:
                if self.live_on(station.id):
                    stream = self._viewer_stream
                    await bridge.async_wait_video(LIVE_IMAGE_WAIT, stream=stream)
                    image = await bridge.async_frame(stream)
                else:
                    await self.async_acquire(station)
                    try:
                        image = await bridge.async_frame()
                    finally:
                        await self.async_release()
            except (MediaBusyError, StationRingingError) as err:
                _LOGGER.debug("Last camera image kept: %s", err)
                return self.cached_image(station.id)
            except (AiphoneJoError, Go2RtcError) as err:
                self._log_error(err)
                return self.cached_image(station.id)
            self._store(station.id, image)
            return image

    # ----------------------------------------------------------- viewers

    async def async_open_viewer(
        self,
        station: Station | None,
        session_id: str,
        offer_sdp: str,
        send_message: WebRTCSendMessage,
    ) -> None:
        """Connect a browser to the live view of the station."""
        bridge = self._bridge
        if not self.available or bridge is None or station is None:
            send_message(WebRTCError("unavailable", "The camera is unavailable."))
            return
        try:
            async with self._lock:
                await self.async_acquire(station)
        except StationRingingError:
            send_message(WebRTCError("station_ringing", STATION_RINGING))
            return
        except MediaBusyError:
            send_message(WebRTCError("camera_busy", CAMERA_BUSY))
            return
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
        """Refresh entity states (which camera is streaming)."""
        entry_id = self._runtime.entry.entry_id
        async_dispatcher_send(self._hass, f"{DOMAIN}_{entry_id}_update")
