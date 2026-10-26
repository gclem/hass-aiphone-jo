"""Camera of the entrance stations (cached images and MJPEG live view)."""

from __future__ import annotations

from aiohttp import web
from homeassistant.components.camera import Camera, async_get_still_stream
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AiphoneJoConfigEntry
from .coordinator import AiphoneJoRuntime
from .entity import AiphoneJoEntity
from .media import STREAM_FPS


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AiphoneJoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the camera."""
    runtime = entry.runtime_data
    if not runtime.media.available and runtime.media.unavailable_reason:
        runtime.media.report_unavailable(runtime.media.unavailable_reason)
    async_add_entities([AiphoneJoCamera(runtime)])


class AiphoneJoCamera(AiphoneJoEntity, Camera):
    """Image and live video of the entrance station chosen in the options."""

    _attr_frame_interval = 1 / STREAM_FPS

    def __init__(self, runtime: AiphoneJoRuntime) -> None:
        """Initialize."""
        AiphoneJoEntity.__init__(self, runtime, "camera")
        Camera.__init__(self)
        self.content_type = "image/jpeg"

    @property
    def available(self) -> bool:
        """Available while connected and video is supported."""
        return super().available and self.runtime.media.available

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Cached image, refreshed at most once per refresh interval."""
        return await self.runtime.media.async_image()

    async def handle_async_mjpeg_stream(
        self, request: web.Request
    ) -> web.StreamResponse | None:
        """Live view, shared with other viewers, stopped when they leave."""
        media = self.runtime.media
        if not media.available:
            return None
        async with media.async_subscribe() as next_frame:
            return await async_get_still_stream(
                request, next_frame, self.content_type, self.frame_interval
            )
