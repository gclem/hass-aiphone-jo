"""Camera of the entrance stations: still images and WebRTC live view."""

from __future__ import annotations

from homeassistant.components.camera import (
    Camera,
    CameraEntityFeature,
    WebRTCSendMessage,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from webrtc_models import RTCIceCandidateInit

from . import AiphoneJoConfigEntry
from .coordinator import AiphoneJoRuntime
from .entity import AiphoneJoEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AiphoneJoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the camera."""
    runtime = entry.runtime_data
    runtime.media.async_update_issues()
    async_add_entities([AiphoneJoCamera(runtime)])


class AiphoneJoCamera(AiphoneJoEntity, Camera):
    """Entrance station chosen in the options."""

    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, runtime: AiphoneJoRuntime) -> None:
        """Initialize."""
        AiphoneJoEntity.__init__(self, runtime, "camera")
        Camera.__init__(self)
        self.content_type = "image/jpeg"

    @property
    def available(self) -> bool:
        """Available while connected and video can work."""
        return super().available and self.runtime.media.available

    @property
    def is_streaming(self) -> bool:
        """True while a live session runs."""
        return self.runtime.media.live

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Cached image, refreshed at most once per refresh interval."""
        return await self.runtime.media.async_image()

    async def async_handle_async_webrtc_offer(
        self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
    ) -> None:
        """Start (or join) the live session and connect the browser."""
        await self.runtime.media.async_open_viewer(session_id, offer_sdp, send_message)

    async def async_on_webrtc_candidate(
        self, session_id: str, candidate: RTCIceCandidateInit
    ) -> None:
        """Forward a browser ICE candidate."""
        await self.runtime.media.async_viewer_candidate(session_id, candidate.candidate)

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        """The browser left: stop the session with the last viewer."""
        self.hass.async_create_task(self.runtime.media.async_close_viewer(session_id))
