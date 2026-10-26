"""Experimental call answering: answer, hang up and reject incoming calls."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later

from .const import CONF_CALL_ANSWERING, DOMAIN
from .go2rtc import Go2RtcError
from .vendor.aiphone_jo import (
    AiphoneEvent,
    AiphoneJoError,
    CallAnsweredElsewhereError,
    CallEndedError,
    CallEndReason,
    CallStatus,
    CallUpdatedEvent,
    IncomingCall,
    RingEvent,
)

if TYPE_CHECKING:
    from .coordinator import AiphoneJoRuntime

_LOGGER = logging.getLogger(__name__)

# Same limit as the official application; the station may end calls earlier.
MAX_TALK_TIME = 200.0
SESSION_POLL = 1.0


class CallState(StrEnum):
    """State of the current or last call, as seen by Home Assistant."""

    IDLE = "idle"
    RINGING = "ringing"
    CONNECTING = "connecting"
    TALKING = "talking"
    ENDED = "ended"
    REJECTED = "rejected"
    MISSED = "missed"
    ANSWERED_ELSEWHERE = "answered_elsewhere"
    FAILED = "failed"


ACTIVE_STATES = frozenset({CallState.CONNECTING, CallState.TALKING})


def end_state(
    reason: CallEndReason | None, status: CallStatus | None, *, answered: bool
) -> CallState:
    """State shown when a call ends, from the library's end reason."""
    if (
        reason is CallEndReason.ANSWERED_ELSEWHERE
        or status is CallStatus.ANSWERED_ELSEWHERE
    ):
        return CallState.ANSWERED_ELSEWHERE
    if reason is CallEndReason.REJECTED or status is CallStatus.REJECTED:
        return CallState.REJECTED
    return CallState.ENDED if answered else CallState.MISSED


def _error(key: str, **placeholders: str) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders or None,
    )


class CallController:
    """Tracks incoming calls and drives the experimental answering sequence."""

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
        self.state = CallState.IDLE
        self.station: str | None = None
        self.started_at: datetime | None = None
        self.end_reason: str | None = None
        self._call: IncomingCall | None = None
        self._session: Any = None
        self._cancel_timeout: Callable[[], None] | None = None
        self._watch: asyncio.Task[None] | None = None
        self._answering: asyncio.Task[Any] | None = None

    @property
    def enabled(self) -> bool:
        """True when call answering is enabled in the options."""
        return bool(self._runtime.entry.options.get(CONF_CALL_ANSWERING, False))

    @property
    def active(self) -> bool:
        """True while a call answered from Home Assistant is in progress."""
        return self.state in ACTIVE_STATES

    def _set(self, state: CallState) -> None:
        if state != self.state:
            self.state = state
            self._notify()

    # ------------------------------------------------------------ events

    @callback
    def async_handle_event(self, event: AiphoneEvent) -> None:
        """Follow rings and call updates from the library."""
        if isinstance(event, RingEvent):
            if self.active:
                return
            self._call = event.call
            self.station = self._runtime.station_name(event.call)
            self.started_at = event.call.ringing_at
            self.end_reason = None
            self._set(CallState.RINGING)
        elif isinstance(event, CallUpdatedEvent) and event.call is self._call:
            self._on_call_update(event)

    def _on_call_update(self, event: CallUpdatedEvent) -> None:
        status = event.call.status
        if status in (CallStatus.RINGING, CallStatus.TALKING):
            return
        reason = event.reason or getattr(event.call, "end_reason", None)
        if reason is not None:
            self.end_reason = str(reason)
        if self.active:
            # Without a session, the answer in progress or the hang-up handles it.
            if self._session is not None:
                self._hass.async_create_task(
                    self._async_finish(end_state(reason, status, answered=True))
                )
        elif self.state is CallState.RINGING:
            self._set(end_state(reason, status, answered=False))

    # ----------------------------------------------------------- actions

    async def async_answer(self) -> None:
        """Answer the ringing call: the camera shows the visitor."""
        self._check_ringing()
        snapshots = self._runtime.snapshots
        # The ring image is then taken from the answered call.
        resume = await snapshots.async_interrupt()
        try:
            await self._async_answer()
        finally:
            snapshots.async_resume(resume)

    def _check_ringing(self) -> None:
        if not self.enabled:
            raise _error("call_answering_disabled")
        if self.state is not CallState.RINGING or self._call is None:
            raise _error("call_not_ringing")

    async def _async_answer(self) -> None:
        call = self._call
        if not self.enabled:
            raise _error("call_answering_disabled")
        if self.state is not CallState.RINGING or call is None:
            raise _error("call_not_ringing")
        media = self._runtime.media
        if not media.available:
            raise _error("call_video_unavailable")
        async with self._runtime.session_lock:
            if self.state is not CallState.RINGING:
                raise _error("call_not_ringing")
            await media.async_prepare_call()
            client: Any = self._runtime.client
            self._set(CallState.CONNECTING)
            try:
                negotiate = await media.async_call_negotiator()
                if self.state is not CallState.CONNECTING:
                    # Hung up while the call stream was being prepared.
                    await media.async_end_call()
                    return

                async def answer() -> Any:
                    # The library runs the whole sequence and tears it down on
                    # failure or cancellation (hang-up while connecting).
                    return await client.answer_call(call, negotiate, talk=media.talk)

                self._answering = asyncio.create_task(answer())
                try:
                    session = await self._answering
                finally:
                    self._answering = None
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
                # Hung up while connecting: _async_finish already cleaned up.
                return
            except CallAnsweredElsewhereError as err:
                self.end_reason = str(CallEndReason.ANSWERED_ELSEWHERE)
                await self._async_finish(CallState.ANSWERED_ELSEWHERE)
                raise _error("call_answered_elsewhere") from err
            except CallEndedError as err:
                if err.reason is not None:
                    self.end_reason = str(err.reason)
                await self._async_finish(end_state(err.reason, None, answered=False))
                raise _error("call_ended") from err
            except (AiphoneJoError, Go2RtcError) as err:
                _LOGGER.warning("Answering failed: %s", type(err).__name__)
                # The library withdrew without ending the call: it can be retried.
                ringing = call.status is CallStatus.RINGING
                await self._async_finish(
                    CallState.RINGING if ringing else CallState.FAILED
                )
                raise _error("call_failed", code=type(err).__name__) from err
            self._session = session
            media.async_attach_call(session)
        self.started_at = datetime.now().astimezone()
        self._set(CallState.TALKING)
        self._cancel_timeout = async_call_later(
            self._hass, MAX_TALK_TIME, self._async_timeout
        )
        self._watch = self._runtime.entry.async_create_background_task(
            self._hass, self._async_watch(session), f"{DOMAIN} call"
        )

    async def async_hang_up(self) -> None:
        """Hang up the call answered from Home Assistant."""
        if not self.active:
            raise _error("call_not_active")
        self.end_reason = str(CallEndReason.ENDED_BY_CLIENT)
        await self._async_finish(CallState.ENDED)

    async def async_reject(self) -> None:
        """Decline the ringing call."""
        self._check_ringing()
        snapshots = self._runtime.snapshots
        resume = await snapshots.async_interrupt()
        try:
            await self._async_reject()
        finally:
            snapshots.async_resume(resume)

    async def _async_reject(self) -> None:
        call = self._call
        if not self.enabled:
            raise _error("call_answering_disabled")
        if self.state is not CallState.RINGING or call is None:
            raise _error("call_not_ringing")
        async with self._runtime.session_lock:
            await self._runtime.media.async_prepare_call("live_ended")
            client: Any = self._runtime.client
            try:
                session = await client.answer_call(call)
                await session.reject()
            except CallEndedError as err:
                if err.reason is not None:
                    self.end_reason = str(err.reason)
                self._set(end_state(err.reason, None, answered=False))
                raise _error(
                    "call_answered_elsewhere"
                    if isinstance(err, CallAnsweredElsewhereError)
                    else "call_ended"
                ) from err
            except AiphoneJoError as err:
                raise _error("call_failed", code=type(err).__name__) from err
        self.end_reason = str(CallEndReason.REJECTED)
        self._set(CallState.REJECTED)

    async def async_shutdown(self) -> None:
        """Hang up on unload."""
        if self.active or self._session is not None:
            await self._async_finish(CallState.ENDED)

    # ---------------------------------------------------------- internals

    async def _async_timeout(self, _now: Any) -> None:
        self._cancel_timeout = None
        if self.active:
            _LOGGER.info("Call hung up after the maximum talk time")
            self.end_reason = str(CallEndReason.ENDED_BY_CLIENT)
            await self._async_finish(CallState.ENDED)

    async def _async_watch(self, session: Any) -> None:
        while True:
            if not getattr(session, "active", False):
                break
            await asyncio.sleep(SESSION_POLL)
        if self._session is session:
            call = getattr(session, "call", None)
            reason = getattr(call, "end_reason", None)
            if reason is not None:
                self.end_reason = str(reason)
            await self._async_finish(
                end_state(reason, getattr(call, "status", None), answered=True)
            )

    async def _async_finish(self, state: CallState) -> None:
        answering = self._answering
        if answering is not None and not answering.done():
            answering.cancel()
            with contextlib.suppress(asyncio.CancelledError, AiphoneJoError):
                await asyncio.wait({answering})
        session, self._session = self._session, None
        if self._cancel_timeout is not None:
            self._cancel_timeout()
            self._cancel_timeout = None
        watch, self._watch = self._watch, None
        if watch is not None and watch is not asyncio.current_task():
            watch.cancel()
        if session is not None and getattr(session, "active", False):
            with contextlib.suppress(AiphoneJoError):
                await session.close()
        await self._runtime.media.async_end_call()
        self._set(state)
