"""Thin adapter around the bundled aiphone-jo library."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import aiohttp
from homeassistant.components import network
from homeassistant.core import HomeAssistant

from .vendor.aiphone_jo import (
    AiphoneJoError,
    CloudCredentials,
    DiscoveredMonitor,
    NoMonitorFoundError,
    RoomInfo,
    Terminal,
    discover,
    unregister,
)

try:
    from .vendor.aiphone_jo import MediaTimeoutError, MediaUnavailableError
except ImportError:  # Bundled library without video support.

    class MediaUnavailableError(AiphoneJoError):  # type: ignore[no-redef]
        """Video is not available."""

    class MediaTimeoutError(AiphoneJoError):  # type: ignore[no-redef]
        """No image was received in time."""


_LOGGER = logging.getLogger(__name__)

LIMITED_BROADCAST = "255.255.255.255"
DISCOVERY_TIMEOUT = 3.5


@dataclass(frozen=True, slots=True)
class Station:
    """Entrance station of the installation (from /getRoomInfo)."""

    id: int
    kind: int
    name: str
    camera: bool

    @classmethod
    def from_terminal(cls, terminal: Terminal) -> Station | None:
        """Build from a library terminal (None when it has no id/kind)."""
        if terminal.id is None or terminal.term_kind is None:
            return None
        return cls(
            id=terminal.id,
            kind=terminal.term_kind,
            name=terminal.name or f"Station {terminal.id}",
            camera=terminal.is_camera_station,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Station:
        """Build from the stored snapshot."""
        return cls(
            id=int(data["id"]),
            kind=int(data["kind"]),
            name=str(data["name"]),
            camera=bool(data["camera"]),
        )

    def as_dict(self) -> dict[str, Any]:
        """Stored snapshot."""
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "camera": self.camera,
        }


def stations_from_room_info(room_info: RoomInfo) -> list[Station]:
    """Entrance stations with a camera, in /getRoomInfo order."""
    return [
        station
        for terminal in room_info.camera_terminals
        if (station := Station.from_terminal(terminal)) is not None
    ]


def is_monitor_id(value: str | None) -> bool:
    """True for a library monitor id (as opposed to a certificate fingerprint)."""
    return bool(value) and str(value).startswith("jo-")


def normalize_fingerprint(value: str) -> str:
    """SHA-256 fingerprint as lowercase hex without separators."""
    return value.replace(":", "").strip().lower()


async def async_discover(
    hass: HomeAssistant, host: str | None = None
) -> list[DiscoveredMonitor]:
    """Find monitors (broadcast, or unicast to ``host``), including refusals."""
    if host:
        targets: list[str] = [host]
    else:
        addresses = await network.async_get_ipv4_broadcast_addresses(hass)
        targets = list(
            dict.fromkeys([*(str(address) for address in addresses), LIMITED_BROADCAST])
        )
    try:
        return await discover(targets, timeout=DISCOVERY_TIMEOUT, include_refused=True)
    except NoMonitorFoundError:
        return []


def credentials_to_storage(credentials: CloudCredentials) -> dict[str, Any]:
    """Serialize credentials for the config entry."""
    return credentials.to_storage()


def credentials_from_storage(
    data: dict[str, Any], *, check_validity: bool = True
) -> CloudCredentials:
    """Load stored credentials.

    Raises CredentialsExpiredError or InvalidCredentialsError.
    """
    return CloudCredentials.from_storage(data, check_validity=check_validity)


async def async_unregister(
    session: aiohttp.ClientSession, credentials: CloudCredentials
) -> bool:
    """Delete the terminal from the Aiphone cloud; False when it failed."""
    try:
        await unregister(session, credentials)
    except AiphoneJoError as err:
        _LOGGER.debug("Terminal not unregistered: %s", type(err).__name__)
        return False
    except Exception:
        # Removal must always complete: the user is told what to do instead.
        _LOGGER.exception("Unexpected error while unregistering the terminal")
        return False
    return True
