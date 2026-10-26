"""Live view and still images of the entrance stations through go2rtc.

Each live view or image opens a monitoring session on an entrance station and
pushes it into a go2rtc stream; browsers and still images read that stream.
The station ends each session after about a minute.
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
from .const import CONF_IMAGE_REFRESH, DEFAULT_IMAGE_REFRESH, DOMAIN
from .go2rtc import Go2RtcBridge, Go2RtcError, Go2RtcViewer, async_get_server
from .vendor.aiphone_jo import AiphoneJoError, StationBusyError

if TYPE_CHECKING:
    from .coordinator import AiphoneJoRuntime

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")

VIDEO_TIMEOUT = 20.0
SESSION_POLL = 1.0
ISSUE_GO2RTC_UNAVAILABLE = "go2rtc_unavailable"
ISSUE_VIDEO_UNSUPPORTED = "video_unsupported"
ISSUES = (ISSUE_GO2RTC_UNAVAILABLE, ISSUE_VIDEO_UNSUPPORTED)
LIVE_ENDED = (
    "The live view ended: the entrance station closes each session after "
    "about one minute. Open the camera again to continue."
)


class NoStationError(AiphoneJoError):
    """No entrance station with a camera is known."""


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
        self._watch: asyncio.Task[None] | None = None
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
        """True while a live session feeds the stream."""
        return self._live is not None

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
            if self._live is not None:
                return
            client: Any = self._runtime.client

            async def enter(station: Station) -> Any:
                live = client.live_monitor(bridge.async_ingest, station.id, renew=False)
                await live.__aenter__()
                return live

            async with self._runtime.session_lock:
                await bridge.async_ensure_stream()
                live = await self._with_station(enter)
            self._live = live
            self._watch = self._runtime.entry.async_create_background_task(
                self._hass, self._async_watch(live), f"{DOMAIN} live session"
            )
            try:
                await bridge.async_wait_video(VIDEO_TIMEOUT)
            except Go2RtcError:
                await self._async_stop()
                raise

    async def _async_watch(self, live: Any) -> None:
        """Stop when the station ends the session."""
        while True:
            if live.session is None:
                break
            await asyncio.sleep(SESSION_POLL)
        if self._live is live:
            await self._async_stop(ended=True)

    async def _async_stop(self, *, ended: bool = False) -> None:
        live, self._live = self._live, None
        watch, self._watch = self._watch, None
        if watch is not None and watch is not asyncio.current_task():
            watch.cancel()
        viewers, self._viewers = self._viewers, {}
        self._users = max(0, self._users - len(viewers))
        for viewer, send_message in viewers.values():
            if ended:
                send_message(WebRTCError("live_ended", LIVE_ENDED))
            await viewer.async_close()
        if live is not None:
            with contextlib.suppress(Exception):
                await live.__aexit__(None, None, None)

    async def async_acquire(self) -> None:
        """Use the live session, starting it if needed."""
        self._users += 1
        try:
            await self._async_start()
        except BaseException:
            await self.async_release()
            raise

    async def async_release(self) -> None:
        """Stop using the live session; it stops with its last user."""
        self._users = max(0, self._users - 1)
        if self._users == 0:
            await self._async_stop()

    async def async_shutdown(self) -> None:
        """Stop everything (unload)."""
        self._users = 0
        await self._async_stop()

    # ------------------------------------------------------------ images

    async def async_image(self) -> bytes | None:
        """Cached image, refreshed when older than the refresh interval."""
        bridge = self._bridge
        if not self.available or bridge is None:
            return self._image
        if self._live is None and self._fresh():
            return self._image
        async with self._image_lock:
            if self._live is None and self._fresh():
                return self._image
            try:
                if self._live is not None:
                    image = await bridge.async_frame()
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
            viewer = await bridge.async_open_viewer(on_message)
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
            await self.async_release()

    def async_notify(self) -> None:
        """Refresh entity states."""
        entry_id = self._runtime.entry.entry_id
        async_dispatcher_send(self._hass, f"{DOMAIN}_{entry_id}_update")
