"""Image of the visitor on each ring, and the ``aiphone_jo_ring`` event."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import ATTR_RAW_NAME, CONF_RING_SNAPSHOT, DOMAIN, EVENT_RING_BUS
from .go2rtc import Go2RtcError
from .vendor.aiphone_jo import AiphoneJoError, CallStatus, IncomingCall

if TYPE_CHECKING:
    from .coordinator import AiphoneJoRuntime

_LOGGER = logging.getLogger(__name__)

SNAPSHOT_TIMEOUT = 12.0
IMAGE_KEY = "last_visitor"


@dataclass(slots=True)
class _Ring:
    call: IncomingCall
    station: str
    at: datetime


class RingSnapshots:
    """Captures the visitor when a station rings, then fires the ring event."""

    def __init__(
        self,
        hass: HomeAssistant,
        runtime: AiphoneJoRuntime,
        notify: Callable[[], None],
    ) -> None:
        """Initialize."""
        self._hass = hass
        self._runtime = runtime
        self._notify = notify
        self.image: bytes | None = None
        self.image_at: datetime | None = None
        self.station: str | None = None
        self.ringing_at: datetime | None = None
        self._ring: _Ring | None = None
        self._task: asyncio.Task[None] | None = None
        self._logged_error: str | None = None

    @property
    def enabled(self) -> bool:
        """True when the snapshot on ring is enabled in the options."""
        return bool(self._runtime.entry.options.get(CONF_RING_SNAPSHOT, False))

    # ------------------------------------------------------------ rings

    @callback
    def async_handle_ring(self, call: IncomingCall, station: str) -> None:
        """A station rings: capture it, or fire the event right away."""
        ring = _Ring(call, station, call.ringing_at or dt_util.now())
        if not self.enabled:
            self._fire(ring, None)
            return
        if (previous := self._ring) is not None:
            # A new call replaces the one being captured: report it without image.
            self._cancel()
            self._finish(previous, None)
        self._ring = ring
        self._start(ring)

    def _cancel(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()

    def _start(self, ring: _Ring) -> None:
        self._cancel()
        self._task = self._runtime.entry.async_create_background_task(
            self._hass, self._async_capture(ring), f"{DOMAIN} ring snapshot"
        )

    async def _async_capture(self, ring: _Ring) -> None:
        image: bytes | None = None
        try:
            image = await self._runtime.media.async_ring_image(
                ring.call, SNAPSHOT_TIMEOUT
            )
        except asyncio.CancelledError:
            # Interrupted or replaced: whoever cancelled decides what follows.
            return
        except (AiphoneJoError, Go2RtcError, TimeoutError) as err:
            self._log_error(err)
        self._finish(ring, image)

    def _log_error(self, err: Exception) -> None:
        reason = type(err).__name__
        if reason != self._logged_error:
            _LOGGER.warning("No image of the visitor: %s (%s)", reason, err)
            self._logged_error = reason
        else:
            _LOGGER.debug("No image of the visitor: %s", reason)

    def _finish(self, ring: _Ring, image: bytes | None) -> None:
        if self._ring is not ring:
            return
        self._ring = None
        if image is not None:
            self._logged_error = None
            self.image = image
            self.image_at = dt_util.now()
            self.station = self._runtime.station_label(ring.station)
            self.ringing_at = ring.at
            self._notify()
        self._fire(ring, image)

    # -------------------------------------------------- answer and reject

    async def async_interrupt(self) -> Any:
        """Stop the capture before answering or rejecting.

        Returns the interrupted ring, to pass to :meth:`async_resume`.
        """
        task = self._task
        ring = self._ring
        if task is None or task.done() or ring is None:
            return None
        self._task = None
        task.cancel()
        # The capture leaves the call before it ends.
        await asyncio.wait({task})
        return ring

    @callback
    def async_resume(self, ring: Any) -> None:
        """Capture again after an answer (from its stream) or a failed one."""
        if ring is None or ring is not self._ring:
            return
        if self._task is not None and not self._task.done():
            return
        if ring.call.status in (CallStatus.RINGING, CallStatus.TALKING):
            self._start(ring)
        else:
            self._finish(ring, None)

    async def async_shutdown(self) -> None:
        """Stop the capture (unload)."""
        task, self._task = self._task, None
        self._ring = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait({task})

    # ------------------------------------------------------------ event

    def _fire(self, ring: _Ring, image: bytes | None) -> None:
        entry = self._runtime.entry
        image_entity = er.async_get(self._hass).async_get_entity_id(
            "image", DOMAIN, f"{entry.unique_id}_{IMAGE_KEY}"
        )
        device = dr.async_get(self._hass).async_get_device(
            identifiers={(DOMAIN, str(entry.unique_id))}
        )
        data: dict[str, Any] = {
            "entry_id": entry.entry_id,
            "device_id": device.id if device else None,
            "station": self._runtime.station_label(ring.station),
            ATTR_RAW_NAME: ring.station,
            "ringing_at": ring.at.isoformat(),
            "snapshot": image is not None,
            "image_entity_id": image_entity,
            "image_url": (
                f"/api/image_proxy/{image_entity}"
                if image is not None and image_entity
                else None
            ),
        }
        self._hass.bus.async_fire(EVENT_RING_BUS, data)
