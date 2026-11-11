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
from .call import CallController
from .const import CONF_STATION, DOMAIN
from .health import PROBLEM_COMMAND_FAILED, Health, describe
from .media import CameraMedia
from .names import StationNamer
from .snapshot import RingSnapshots
from .vendor.aiphone_jo import (
    AiphoneEvent,
    AiphoneJoClient,
    AiphoneJoError,
    CallStatus,
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

OUTPUT_NAMES = {Output.UNLOCK: "Action 1", Output.OPTION: "Action 2"}


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
        namer: StationNamer | None = None,
    ) -> None:
        """Initialize."""
        self.hass = hass
        self.entry = entry
        self.client = client
        self.stations = stations
        self.station_label = namer or StationNamer()
        self.options = dict(entry.options)
        self.ringing = False
        self.last_ring: Ring | None = None
        self._call: IncomingCall | None = None
        # One monitoring session at a time: outputs, images and live video.
        self.session_lock = asyncio.Lock()
        update = signal_update(entry.entry_id)
        self.health = Health(
            hass,
            entry,
            client.credentials,
            lambda: async_dispatcher_send(hass, update),
            connected=client.connected,
            ring_topic_subscribed=client.broadcast_subscribed,
        )
        self.media = CameraMedia(hass, self)
        self.calls = CallController(
            hass, self, lambda: async_dispatcher_send(hass, update)
        )
        self.snapshots = RingSnapshots(
            hass, self, lambda: async_dispatcher_send(hass, update)
        )
        self._remove_listener = client.add_listener(self._handle_event)

    @property
    def connected(self) -> bool:
        """True while the cloud (MQTT) connection is up."""
        return self.client.connected

    async def async_stop(self) -> None:
        """Stop listening and disconnect."""
        self._remove_listener()
        self.health.async_shutdown()
        await self.snapshots.async_shutdown()
        await self.calls.async_shutdown()
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
            self.health.async_connection_changed(event)
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
            self.snapshots.async_handle_ring(event.call, self.last_ring.station)
        elif isinstance(event, CallUpdatedEvent):
            answered_or_over = event.call.status is not CallStatus.RINGING
            if event.call is self._call and answered_or_over and self.ringing:
                self.ringing = False
                async_dispatcher_send(self.hass, signal_update(entry_id))
        self.calls.async_handle_event(event)

    @property
    def ringing_station_id(self) -> int | None:
        """Id of the entrance station calling right now, if any."""
        call = self._call
        if not self.ringing or call is None or call.status is not CallStatus.RINGING:
            return None
        return call.term_id

    def session_stations(self, *, media: bool = False) -> list[Station]:
        """Camera stations for a monitoring session, the one chosen first.

        Monitoring a station while it calls may end the call on every phone:
        images and live views never monitor it (``media``), and the output
        buttons only use it as a last resort.
        """
        preferred = self.entry.options.get(CONF_STATION)
        ringing = self.ringing_station_id
        stations = sorted(
            self.stations,
            key=lambda station: (station.id == ringing, station.id != preferred),
        )
        if media and ringing is not None:
            return [station for station in stations if station.id != ringing]
        return stations

    async def async_pulse(self, output: Output) -> None:
        """Pulse a monitor output; one monitoring session at a time."""
        try:
            await self._async_pulse(output)
        except HomeAssistantError as err:
            cause = err.__cause__
            detail = describe(cause) if cause is not None else err.translation_key
            self.health.report(
                PROBLEM_COMMAND_FAILED, f"{OUTPUT_NAMES[output]}: {detail}"
            )
            raise

    async def _async_pulse(self, output: Output) -> None:
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
