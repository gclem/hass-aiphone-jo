"""Camera images and live video from the entrance stations.

Every image or stream opens a monitoring session on an entrance station, so
images are cached and a single live stream is shared between viewers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeVar

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .api import MediaUnavailableError, Station
from .const import CONF_IMAGE_REFRESH, DEFAULT_IMAGE_REFRESH, DOMAIN
from .vendor.aiphone_jo import AiphoneJoError, StationBusyError

if TYPE_CHECKING:
    from .coordinator import AiphoneJoRuntime

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")
FrameSource = Callable[[], Awaitable[bytes | None]]

STREAM_FPS = 8
JPEG_QUALITY = 80
SNAPSHOT_TIMEOUT = 15.0
FRAME_WAIT = 10.0
ISSUE_VIDEO_UNAVAILABLE = "video_unavailable"


class NoStationError(AiphoneJoError):
    """No entrance station with a camera is known."""


class CameraMedia:
    """Cached snapshots and a shared live stream for the monitor camera."""

    def __init__(self, hass: HomeAssistant, runtime: AiphoneJoRuntime) -> None:
        """Initialize."""
        self._hass = hass
        self._runtime = runtime
        self._image: bytes | None = None
        self._image_at: float | None = None
        self._logged_error: str | None = None
        self.unavailable_reason: str | None = None
        if not self.supported:
            self.unavailable_reason = "this version does not support video"
        self._viewers = 0
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._frame: bytes | None = None
        self._version = 0
        self._changed = asyncio.Condition()

    @property
    def supported(self) -> bool:
        """True when the library client provides video."""
        client = self._runtime.client
        return hasattr(client, "snapshot") and hasattr(client, "video_stream")

    @property
    def available(self) -> bool:
        """True when images can be requested."""
        return self.unavailable_reason is None

    @property
    def refresh_interval(self) -> float:
        """Maximum age of the cached image, in seconds."""
        options = self._runtime.entry.options
        return float(options.get(CONF_IMAGE_REFRESH, DEFAULT_IMAGE_REFRESH))

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

    # ----------------------------------------------------------- errors

    def report_unavailable(self, reason: str) -> None:
        """Mark video as unavailable and tell the user why."""
        if self.unavailable_reason is None:
            _LOGGER.warning("Camera unavailable: %s", reason)
        self.unavailable_reason = reason
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            ISSUE_VIDEO_UNAVAILABLE,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_VIDEO_UNAVAILABLE,
            translation_placeholders={"reason": reason},
        )
        entry_id = self._runtime.entry.entry_id
        async_dispatcher_send(self._hass, f"{DOMAIN}_{entry_id}_update")

    def _log_error(self, err: AiphoneJoError) -> None:
        key = type(err).__name__
        if key != self._logged_error:
            _LOGGER.warning("Camera image not refreshed: %s", key)
            self._logged_error = key
        else:
            _LOGGER.debug("Camera image not refreshed: %s", key)

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

    # ---------------------------------------------------------- snapshot

    async def async_image(self) -> bytes | None:
        """Cached image, refreshed when older than the refresh interval."""
        if self._running and self._frame is not None:
            return self._frame
        if not self.available or self._fresh():
            return self._image
        client: Any = self._runtime.client
        async with self._runtime.session_lock:
            if self._fresh():
                return self._image
            try:
                image = await self._with_station(
                    lambda station: client.snapshot(
                        station.id, timeout=SNAPSHOT_TIMEOUT
                    )
                )
            except MediaUnavailableError as err:
                self.report_unavailable(str(err) or type(err).__name__)
                return self._image
            except AiphoneJoError as err:
                self._log_error(err)
                return self._image
            self._store(image)
            return image

    # ------------------------------------------------------------- live

    @contextlib.asynccontextmanager
    async def async_subscribe(self) -> AsyncIterator[FrameSource]:
        """Join the shared live stream; yields a "next frame" callback."""
        self._viewers += 1
        if self._task is None or self._task.done():
            self._running = True
            self._task = self._runtime.entry.async_create_background_task(
                self._hass, self._async_run(), f"{DOMAIN} live video"
            )
        last = 0

        async def next_frame() -> bytes | None:
            nonlocal last
            async with self._changed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._changed.wait_for(
                            lambda: self._version != last or not self._running
                        ),
                        FRAME_WAIT,
                    )
            if not self._running or self._version == last:
                return None
            last = self._version
            return self._frame

        try:
            yield next_frame
        finally:
            self._viewers -= 1
            if self._viewers == 0:
                await self.async_stop()

    async def async_stop(self) -> None:
        """Stop the live stream."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _async_run(self) -> None:
        client: Any = self._runtime.client
        stream_context: Any = None
        try:

            async def open_stream(station: Station) -> Any:
                nonlocal stream_context
                stream_context = client.video_stream(station.id)
                try:
                    return await stream_context.__aenter__()
                except BaseException:
                    stream_context = None
                    raise

            async with self._runtime.session_lock:
                stream = await self._with_station(open_stream)
            while True:
                jpeg = await self._hass.async_add_executor_job(
                    stream.latest_jpeg, JPEG_QUALITY
                )
                if jpeg:
                    self._frame = jpeg
                    self._store(jpeg)
                    async with self._changed:
                        self._version += 1
                        self._changed.notify_all()
                await asyncio.sleep(1 / STREAM_FPS)
        except MediaUnavailableError as err:
            self.report_unavailable(str(err) or type(err).__name__)
        except AiphoneJoError as err:
            self._log_error(err)
        finally:
            self._running = False
            self._frame = None
            if stream_context is not None:
                with contextlib.suppress(Exception):
                    await stream_context.__aexit__(None, None, None)
            async with self._changed:
                self._changed.notify_all()
