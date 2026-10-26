"""Runtime state of a paired monitor."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .api import Station
from .const import CONF_STATION, DOMAIN
from .media import CameraMedia
from .vendor.aiphone_jo import (
    AiphoneEvent,
    AiphoneJoClient,
    AiphoneJoError,
    CallUpdatedEvent,
    CommandRejectedError,
    ConnectionStateEvent,
    IncomingCall,
    NotConnectedError,
    Output,
    ResponseTimeoutError,
    RingEvent,
    StationBusyError,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Ring:
    """A ring from an entrance station."""

    station: str
    at: datetime


def signal_update(entry_id: str) -> str:
    """Dispatcher signal for state changes."""
    return f"{DOMAIN}_{entry_id}_update"


def signal_ring(entry_id: str) -> str:
    """Dispatcher signal for new rings."""
    return f"{DOMAIN}_{entry_id}_ring"


class AiphoneJoRuntime:
    """Bridge between the library client and Home Assistant entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: AiphoneJoClient,
        stations: list[Station],
    ) -> None:
        """Initialize."""
        self.hass = hass
        self.entry = entry
        self.client = client
        self.stations = stations
        self.options = dict(entry.options)
        self.ringing = False
        self.last_ring: Ring | None = None
        self._call: IncomingCall | None = None
        # One monitoring session at a time: outputs, images and live video.
        self.session_lock = asyncio.Lock()
        self.media = CameraMedia(hass, self)
        self._remove_listener = client.add_listener(self._handle_event)

    @property
    def connected(self) -> bool:
        """True while the cloud (MQTT) connection is up."""
        return self.client.connected

    async def async_stop(self) -> None:
        """Stop listening and disconnect."""
        self._remove_listener()
        await self.media.async_shutdown()
        await self.client.disconnect()

    def station_name(self, call: IncomingCall) -> str:
        """Readable name of the station that rang."""
        for station in self.stations:
            if station.id == call.term_id:
                return station.name
        return call.display_name or call.display_name_2 or f"Station {call.term_id}"

    @callback
    def _handle_event(self, event: AiphoneEvent) -> None:
        entry_id = self.entry.entry_id
        if isinstance(event, ConnectionStateEvent):
            if event.auth_failed:
                _LOGGER.warning(
                    "The cloud refused this terminal: pair the monitor again"
                )
                self.entry.async_start_reauth(self.hass)
            if event.connected and not event.broadcast_subscribed:
                _LOGGER.warning(
                    "The ring topic was refused by the cloud: rings may be missed"
                )
            async_dispatcher_send(self.hass, signal_update(entry_id))
        elif isinstance(event, RingEvent):
            self._call = event.call
            self.ringing = True
            self.last_ring = Ring(self.station_name(event.call), event.call.ringing_at)
            async_dispatcher_send(self.hass, signal_ring(entry_id), self.last_ring)
            async_dispatcher_send(self.hass, signal_update(entry_id))
        elif isinstance(event, CallUpdatedEvent):
            if event.call is self._call and not event.call.active and self.ringing:
                self.ringing = False
                async_dispatcher_send(self.hass, signal_update(entry_id))

    def session_stations(self) -> list[Station]:
        """Camera stations, the one chosen in the options first."""
        preferred = self.entry.options.get(CONF_STATION)
        return sorted(self.stations, key=lambda station: station.id != preferred)

    async def async_pulse(self, output: Output) -> None:
        """Pulse a monitor output; one monitoring session at a time."""
        stations = self.session_stations()
        if not stations:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="no_station"
            )
        async with self.session_lock:
            for station in stations:
                try:
                    await self.client.open_door(output, station.id, station.kind)
                except StationBusyError:
                    _LOGGER.debug("Station %s busy, trying the next one", station.id)
                    continue
                except NotConnectedError as err:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN, translation_key="not_connected"
                    ) from err
                except ResponseTimeoutError as err:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN, translation_key="command_timeout"
                    ) from err
                except CommandRejectedError as err:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="command_rejected",
                        translation_placeholders={
                            "code": f"MID {err.mid}, RSLT {err.rslt}"
                        },
                    ) from err
                except AiphoneJoError as err:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="command_failed",
                        translation_placeholders={"code": type(err).__name__},
                    ) from err
                return
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="station_busy"
        )
