"""Image of the last visitor, captured when a station rings."""

from __future__ import annotations

from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AiphoneJoConfigEntry
from .coordinator import AiphoneJoRuntime
from .entity import AiphoneJoEntity, async_remove_disabled
from .snapshot import IMAGE_KEY


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AiphoneJoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the last visitor image when the snapshot on ring is enabled."""
    runtime = entry.runtime_data
    if runtime.snapshots.enabled:
        async_add_entities([AiphoneJoLastVisitorImage(hass, runtime)])
    else:
        async_remove_disabled(hass, runtime, "image", [IMAGE_KEY])


class AiphoneJoLastVisitorImage(AiphoneJoEntity, ImageEntity):
    """Visitor captured at the last ring."""

    _attr_content_type = "image/jpeg"

    def __init__(self, hass: HomeAssistant, runtime: AiphoneJoRuntime) -> None:
        """Initialize."""
        AiphoneJoEntity.__init__(self, runtime, IMAGE_KEY)
        ImageEntity.__init__(self, hass)

    @property
    def available(self) -> bool:
        """The last image stays available while the cloud is down."""
        return True

    @property
    def image_last_updated(self) -> Any:
        """Time of the capture."""
        return self.runtime.snapshots.image_at

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Station and time of the ring."""
        snapshots = self.runtime.snapshots
        return {
            "station": snapshots.station,
            "ringing_at": (
                snapshots.ringing_at.isoformat() if snapshots.ringing_at else None
            ),
        }

    async def async_image(self) -> bytes | None:
        """Last captured image."""
        return self.runtime.snapshots.image
