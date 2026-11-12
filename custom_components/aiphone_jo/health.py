"""Problems reported to the user: cloud connection, repairs and error events.

Logs are not enough: a lost connection or a refused ring topic silently breaks
rings. Every problem is therefore also exposed through Home Assistant:

* the Cloud connection binary sensor and its attributes;
* repairs for lasting problems (cloud unreachable, ring topic refused),
  removed as soon as the problem is gone;
* the Problem event entity and the ``aiphone_jo_error`` bus event, for
  notifications and automations;
* the diagnostics, with the recent history.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import DOMAIN, EVENT_ERROR_BUS
from .vendor.aiphone_jo import ConnectionStateEvent

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .vendor.aiphone_jo import CloudCredentials

_LOGGER = logging.getLogger(__name__)

# Problem types (event types of the Problem entity and of the bus event).
PROBLEM_CONNECTION_LOST = "connection_lost"
PROBLEM_CLOUD_UNREACHABLE = "cloud_unreachable"
PROBLEM_AUTH_REFUSED = "auth_refused"
PROBLEM_RING_TOPIC_REFUSED = "ring_topic_refused"
PROBLEM_RING_SNAPSHOT_FAILED = "ring_snapshot_failed"
PROBLEM_CAMERA_FAILED = "camera_failed"
PROBLEM_CALL_FAILED = "call_failed"
PROBLEM_COMMAND_FAILED = "command_failed"
PROBLEM_TYPES = (
    PROBLEM_CONNECTION_LOST,
    PROBLEM_CLOUD_UNREACHABLE,
    PROBLEM_AUTH_REFUSED,
    PROBLEM_RING_TOPIC_REFUSED,
    PROBLEM_RING_SNAPSHOT_FAILED,
    PROBLEM_CAMERA_FAILED,
    PROBLEM_CALL_FAILED,
    PROBLEM_COMMAND_FAILED,
)

# Repairs, one per config entry.
ISSUE_CLOUD_UNREACHABLE = "cloud_unreachable"
ISSUE_RING_TOPIC_REFUSED = "ring_topic_refused"
HEALTH_ISSUES = (ISSUE_CLOUD_UNREACHABLE, ISSUE_RING_TOPIC_REFUSED)

# A short disconnection is reported by the sensor and the event only; the
# repair appears when the cloud stays unreachable this long (seconds).
UNREACHABLE_DELAY = 300.0
MAX_HISTORY = 20
MAX_MESSAGE = 300
REDACTED = "**REDACTED**"


def signal_problem(entry_id: str) -> str:
    """Dispatcher signal for new problems."""
    return f"{DOMAIN}_{entry_id}_problem"


def issue_id(issue: str, entry_id: str) -> str:
    """Repair id of ``issue`` for one config entry."""
    return f"{issue}_{entry_id}"


def async_delete_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Remove the repairs of a config entry."""
    for issue in HEALTH_ISSUES:
        ir.async_delete_issue(hass, DOMAIN, issue_id(issue, entry_id))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@callback
def async_entry_device(
    hass: HomeAssistant, entry_id: str, identifier: str
) -> dr.DeviceEntry | None:
    """Device of the config entry with this identifier.

    Identifiers are no longer unique across config entries, so the device is
    looked up among the entry's own devices.
    """
    wanted = (DOMAIN, identifier)
    for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry_id):
        if wanted in device.identifiers:
            return device
    return None


def describe(err: BaseException) -> str:
    """Short description of an error: its type and message."""
    text = str(err)
    name = type(err).__name__
    return f"{name}: {text}" if text and text != name else name


@dataclass(frozen=True, slots=True)
class Problem:
    """A problem reported to the user."""

    type: str
    message: str
    at: datetime

    def as_dict(self) -> dict[str, Any]:
        """Serializable form (diagnostics)."""
        return {"type": self.type, "message": self.message, "at": self.at.isoformat()}


class Health:
    """Cloud connection state and problems of one monitor."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        credentials: CloudCredentials,
        notify: Callable[[], None],
        *,
        connected: bool,
        ring_topic_subscribed: bool,
    ) -> None:
        """Initialize from the state of the connection opened by the setup."""
        self._hass = hass
        self._entry = entry
        self._notify = notify
        self._secrets = [
            value
            for value in (
                credentials.host,
                credentials.client_id,
                credentials.recv_topic,
                credentials.send_topic,
                credentials.broadcast_topic,
                credentials.api_origin,
            )
            if isinstance(value, str) and len(value) >= 4
        ]
        # Longest first, so that a topic is redacted before its client id.
        self._secrets.sort(key=len, reverse=True)
        now = dt_util.now()
        self.connected = connected
        self.connected_since: datetime | None = now if connected else None
        self.disconnected_since: datetime | None = None if connected else now
        self.last_error: str | None = None
        self.reconnections = 0
        self.ring_topic_subscribed: bool | None = (
            ring_topic_subscribed if connected else None
        )
        self.connections: deque[dict[str, Any]] = deque(maxlen=MAX_HISTORY)
        self.problems: deque[Problem] = deque(maxlen=MAX_HISTORY)
        self._cancel_unreachable: Callable[[], None] | None = None

    # ------------------------------------------------------------ setup

    @callback
    def async_start(self) -> None:
        """Report the problems already present when the entry is set up."""
        if self.connected and self.ring_topic_subscribed is False:
            self._ring_topic_refused()
        else:
            self._delete_issue(ISSUE_RING_TOPIC_REFUSED)
        self._delete_issue(ISSUE_CLOUD_UNREACHABLE)

    @callback
    def async_shutdown(self) -> None:
        """Stop the timers (unload); repairs are kept until the next setup."""
        if self._cancel_unreachable is not None:
            self._cancel_unreachable()
            self._cancel_unreachable = None

    # ------------------------------------------------------- connection

    @callback
    def async_connection_changed(self, event: ConnectionStateEvent) -> None:
        """Follow the cloud connection state reported by the library."""
        now = dt_util.now()
        error = self.redact(event.error) if event.error else None
        self.connections.append(
            {
                "at": now.isoformat(),
                "connected": event.connected,
                "ring_topic_subscribed": event.broadcast_subscribed,
                "error": error,
                "auth_failed": event.auth_failed,
            }
        )
        if event.connected:
            self._on_connected(event, now)
        else:
            self._on_disconnected(event, error, now)
        self._notify()

    def _on_connected(self, event: ConnectionStateEvent, now: datetime) -> None:
        was_down = not self.connected
        self.connected = True
        self.connected_since = now
        if was_down:
            self.reconnections += 1
            if self.disconnected_since is not None:
                down = (now - self.disconnected_since).total_seconds()
                _LOGGER.info("Cloud connection restored after %.0f s", down)
        self.disconnected_since = None
        self._cancel_unreachable_timer()
        self._delete_issue(ISSUE_CLOUD_UNREACHABLE)
        self.ring_topic_subscribed = event.broadcast_subscribed
        if event.broadcast_subscribed:
            self._delete_issue(ISSUE_RING_TOPIC_REFUSED)
        else:
            self._ring_topic_refused()

    def _on_disconnected(
        self, event: ConnectionStateEvent, error: str | None, now: datetime
    ) -> None:
        was_up = self.connected
        self.connected = False
        self.connected_since = None
        if error:
            self.last_error = error
        if was_up or self.disconnected_since is None:
            self.disconnected_since = now
        if event.auth_failed:
            self._cancel_unreachable_timer()
            self.report(
                PROBLEM_AUTH_REFUSED,
                "The Aiphone cloud refused this terminal: pair the monitor again.",
            )
            return
        if was_up:
            self.report(PROBLEM_CONNECTION_LOST, error or "Connection lost")
        if self._cancel_unreachable is None:
            self._cancel_unreachable = async_call_later(
                self._hass, UNREACHABLE_DELAY, self._async_unreachable
            )

    @callback
    def _async_unreachable(self, _now: Any) -> None:
        self._cancel_unreachable = None
        if self.connected:
            return
        minutes = str(round(UNREACHABLE_DELAY / 60))
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            issue_id(ISSUE_CLOUD_UNREACHABLE, self._entry.entry_id),
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_CLOUD_UNREACHABLE,
            translation_placeholders={
                "name": self._entry.title,
                "minutes": minutes,
                "error": self.last_error or "-",
            },
        )
        self.report(
            PROBLEM_CLOUD_UNREACHABLE,
            f"The Aiphone cloud has been unreachable for {minutes} minutes"
            + (f" ({self.last_error})" if self.last_error else ""),
        )

    def _cancel_unreachable_timer(self) -> None:
        if self._cancel_unreachable is not None:
            self._cancel_unreachable()
            self._cancel_unreachable = None

    def _ring_topic_refused(self) -> None:
        issue = issue_id(ISSUE_RING_TOPIC_REFUSED, self._entry.entry_id)
        if ir.async_get(self._hass).async_get_issue(DOMAIN, issue) is not None:
            return
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            issue,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_RING_TOPIC_REFUSED,
            translation_placeholders={"name": self._entry.title},
        )
        self.report(
            PROBLEM_RING_TOPIC_REFUSED,
            "The Aiphone cloud refused the ring topic: rings may be missed.",
        )

    def _delete_issue(self, issue: str) -> None:
        ir.async_delete_issue(self._hass, DOMAIN, issue_id(issue, self._entry.entry_id))

    # ---------------------------------------------------------- problems

    def redact(self, text: str) -> str:
        """``text`` without identifiers of the terminal, shortened."""
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        if len(text) > MAX_MESSAGE:
            text = text[: MAX_MESSAGE - 1] + "…"
        return text

    @callback
    def report(self, problem_type: str, message: str | BaseException) -> None:
        """Report a problem: Problem entity, ``aiphone_jo_error`` event, history."""
        text = describe(message) if isinstance(message, BaseException) else message
        problem = Problem(problem_type, self.redact(text), dt_util.now())
        self.problems.append(problem)
        entry = self._entry
        device = async_entry_device(self._hass, entry.entry_id, str(entry.unique_id))
        self._hass.bus.async_fire(
            EVENT_ERROR_BUS,
            {
                "entry_id": entry.entry_id,
                "device_id": device.id if device else None,
                "type": problem.type,
                "message": problem.message,
                "at": problem.at.isoformat(),
            },
        )
        async_dispatcher_send(self._hass, signal_problem(entry.entry_id), problem)

    # ------------------------------------------------------------ views

    @property
    def attributes(self) -> dict[str, Any]:
        """State attributes of the Cloud connection sensor."""
        return {
            "connected_since": _iso(self.connected_since),
            "disconnected_since": _iso(self.disconnected_since),
            "last_error": self.last_error,
            "reconnections": self.reconnections,
            "ring_topic_subscribed": self.ring_topic_subscribed,
        }

    def as_diagnostics(self) -> dict[str, Any]:
        """Recent history, for the diagnostics."""
        return {
            **self.attributes,
            "connected": self.connected,
            "connections": list(self.connections),
            "problems": [problem.as_dict() for problem in self.problems],
        }
