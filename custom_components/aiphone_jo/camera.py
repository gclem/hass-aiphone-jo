"""Cameras of the entrance stations: still images and WebRTC live view.

Each entrance station with a camera has its own camera entity. The one chosen
in the options is the **Camera** entity, whose unique id predates the other
cameras. The intercom runs one monitoring session at a time, which the
cameras share (see :mod:`.media`).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.camera import DOMAIN as CAMERA_DOMAIN
from homeassistant.components.camera import (
    Camera,
    CameraEntityFeature,
    WebRTCSendMessage,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from webrtc_models import RTCIceCandidateInit

from . import AiphoneJoConfigEntry
from .api import Station
from .const import CONF_CALL_ANSWERING, CONF_CALL_TALK
from .coordinator import AiphoneJoRuntime
from .entity import AiphoneJoEntity

try:
    from homeassistant.components.camera import CameraEntityCapabilities
except ImportError:  # Home Assistant without two-way audio.
    CameraEntityCapabilities = None

MAIN_KEY = "camera"
STATION_KEY = "station_camera"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AiphoneJoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one camera per entrance station with a camera."""
    runtime = entry.runtime_data
    runtime.media.async_update_issues()
    stations = runtime.camera_stations
    cameras = [AiphoneJoCamera(runtime, stations[0] if stations else None)]
    cameras += [
        AiphoneJoCamera(runtime, station, main=False) for station in stations[1:]
    ]
    _async_remove_stale(hass, entry, {str(camera.unique_id) for camera in cameras})
    async_add_entities(cameras)


@callback
def _async_remove_stale(
    hass: HomeAssistant, entry: AiphoneJoConfigEntry, unique_ids: set[str]
) -> None:
    """Remove the cameras of stations gone or now shown by the Camera entity."""
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.domain == CAMERA_DOMAIN and entity.unique_id not in unique_ids:
            registry.async_remove(entity.entity_id)


class AiphoneJoCamera(AiphoneJoEntity, Camera):
    """Camera of one entrance station."""

    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self, runtime: AiphoneJoRuntime, station: Station | None, *, main: bool = True
    ) -> None:
        """Initialize; ``main`` is the Camera entity (station chosen in the options)."""
        if main or station is None:
            AiphoneJoEntity.__init__(self, runtime, MAIN_KEY)
        else:
            AiphoneJoEntity.__init__(
                self, runtime, STATION_KEY, unique_key=f"{MAIN_KEY}_{station.id}"
            )
            self._attr_translation_placeholders = {
                "station": runtime.station_label(station.name)
            }
        Camera.__init__(self)
        self.station = station
        self.content_type = "image/jpeg"
        options = runtime.entry.options
        if (
            CameraEntityCapabilities is not None
            and options.get(CONF_CALL_ANSWERING)
            and options.get(CONF_CALL_TALK)
        ):
            # The frontend then offers the microphone in the live view.
            self._attr_camera_entity_capabilities = CameraEntityCapabilities(
                two_way_audio=True
            )

    @property
    def available(self) -> bool:
        """Available while connected and video can work."""
        return (
            super().available
            and self.station is not None
            and self.runtime.media.available
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Entrance station shown."""
        station = self.station
        label = self.runtime.station_label(station.name) if station else None
        return {"station": label}

    @property
    def is_streaming(self) -> bool:
        """True while a live session of the station runs."""
        station = self.station
        return station is not None and self.runtime.media.live_on(station.id)

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Cached image, refreshed at most once per refresh interval."""
        if self.station is None:
            return None
        return await self.runtime.media.async_image(self.station)

    async def async_handle_async_webrtc_offer(
        self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
    ) -> None:
        """Start (or join) the live session and connect the browser."""
        await self.runtime.media.async_open_viewer(
            self.station, session_id, offer_sdp, send_message
        )

    async def async_on_webrtc_candidate(
        self, session_id: str, candidate: RTCIceCandidateInit
    ) -> None:
        """Forward a browser ICE candidate."""
        await self.runtime.media.async_viewer_candidate(session_id, candidate.candidate)

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        """The browser left: stop the session with the last viewer."""
        self.hass.async_create_task(self.runtime.media.async_close_viewer(session_id))
