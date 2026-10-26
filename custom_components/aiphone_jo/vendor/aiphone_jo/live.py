"""Long-lived monitoring with automatic renewal and pluggable media negotiation.

The entrance station ends a monitoring session about 60 s after it started.
:class:`LiveMonitor` opens a session, asks the relay for its SDP offer
(``ADD_RCV_USER_REQ`` 30011), passes it to a caller-supplied ``negotiate``
coroutine that returns a complete SDP answer (ICE gathering finished, no
trickle), sends that answer (``UPDATE_RCV_USER_REQ`` 30012), and opens a new
session whenever the current one ends, until the context exits.

It contains no media code: a WebRTC stack (for example :mod:`aiphone_jo.media`
with aiortc, or a bridge to another media server) only implements
``negotiate`` and, optionally, ``on_session_end``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import TYPE_CHECKING, Self

from .const import TERM_KIND_ENTRANCE_STATION
from .exceptions import AiphoneJoError, NotConnectedError
from .models import AiphoneEvent, SessionEndedEvent

if TYPE_CHECKING:
    from .client import AiphoneJoClient, MonitorSession
    from .cloud_api import Terminal

_LOGGER = logging.getLogger(__name__)

Negotiator = Callable[[str], Awaitable[str]]
SessionEndCallback = Callable[[], Awaitable[None]]

RETRY_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0)
MAX_RENEW_FAILURES = 5


class LiveMonitor:
    """Monitoring session renewed transparently while the context is open.

    Use :meth:`AiphoneJoClient.live_monitor`::

        async def negotiate(offer_sdp: str) -> str:
            ...  # create a WebRTC peer, apply the offer, return the answer SDP

        async with client.live_monitor(negotiate, terminal) as live:
            ...  # media flows; a new session is negotiated every ~60 s

    ``negotiate`` is called once per session with the relay's SDP offer and
    must return the complete answer. ``on_session_end`` is awaited each time a
    session ends (before a renewal and when the context exits), so the media
    side can release the previous peer connection. While a session is open,
    :meth:`AiphoneJoClient.open_door` reuses it.

    A renewal that fails is retried with a growing delay; after
    ``max_failures`` consecutive failures, renewal stops and :attr:`error`
    is set (``NotConnectedError`` failures are not counted, the MQTT client
    reconnects by itself).
    """

    def __init__(
        self,
        client: AiphoneJoClient,
        negotiate: Negotiator,
        terminal: Terminal | int | None = None,
        *,
        on_session_end: SessionEndCallback | None = None,
        renew: bool = True,
        max_failures: int = MAX_RENEW_FAILURES,
        retry_delays: tuple[float, ...] = RETRY_DELAYS,
    ) -> None:
        self._client = client
        self._negotiate = negotiate
        self._terminal = terminal
        self._term_kind = TERM_KIND_ENTRANCE_STATION
        self._on_session_end = on_session_end
        self._renew = renew
        self._max_failures = max_failures
        self._retry_delays = retry_delays or RETRY_DELAYS
        self._session: MonitorSession | None = None
        self._ended = asyncio.Event()
        self._stopped = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._closing = False
        self.sessions = 0
        self.error: AiphoneJoError | None = None

    @property
    def session(self) -> MonitorSession | None:
        """Current monitoring session (``None`` during a renewal)."""
        return self._session if self._session is not None and self._session.active else None

    @property
    def running(self) -> bool:
        """``True`` until the context exits or renewal gave up."""
        return not self._stopped.is_set()

    async def wait_stopped(self) -> None:
        """Wait until the context exits or renewal gives up (see :attr:`error`)."""
        await self._stopped.wait()

    async def __aenter__(self) -> Self:
        self._unsubscribe = self._client.add_listener(self._on_event)
        try:
            await self._open()
        except BaseException:
            self._unsubscribe()
            self._stopped.set()
            raise
        if self._renew:
            self._task = asyncio.create_task(self._run(), name="aiphone-jo-live")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._closing = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._finish_session()
        if self._unsubscribe is not None:
            self._unsubscribe()
        self._stopped.set()

    async def renew(self) -> None:
        """End the current session now; a new one is negotiated (if renewal is enabled)."""
        session = self._session
        if session is not None and session.active:
            await session.close()

    def _on_event(self, event: AiphoneEvent) -> None:
        session = self._session
        if (
            isinstance(event, SessionEndedEvent)
            and session is not None
            and event.cid == session.cid
        ):
            self._ended.set()

    async def _open(self) -> None:
        session = await self._client.start_camera_session(self._terminal, self._term_kind)
        try:
            offer = await session.request_offer()
            answer = await self._negotiate(offer)
            await session.send_answer(answer)
        except BaseException:
            await session.close()
            await self._notify_end()
            raise
        # Renewals target the station that accepted, not a busy one.
        self._terminal, self._term_kind = session.term_id, session.term_kind
        self._session = session
        self._ended.clear()
        if not session.active:
            self._ended.set()
        self.sessions += 1
        _LOGGER.debug("Live monitoring session %d negotiated", self.sessions)

    async def _notify_end(self) -> None:
        if self._on_session_end is None:
            return
        try:
            await self._on_session_end()
        except Exception:
            _LOGGER.exception("Error in the live monitoring on_session_end callback")

    async def _finish_session(self) -> None:
        session, self._session = self._session, None
        if session is None:
            return
        try:
            await session.close()
        finally:
            await self._notify_end()

    async def _run(self) -> None:
        failures = 0
        while not self._closing:
            await self._ended.wait()
            if self._closing:
                return
            await self._finish_session()
            while not self._closing:
                try:
                    await self._open()
                    failures = 0
                    break
                except AiphoneJoError as err:
                    if not isinstance(err, NotConnectedError):
                        failures += 1
                    index = min(max(failures, 1), len(self._retry_delays)) - 1
                    delay = self._retry_delays[index]
                    _LOGGER.warning("Live monitoring renewal failed (%s), retrying", err)
                    if failures >= self._max_failures:
                        self.error = err
                        self._stopped.set()
                        return
                    await asyncio.sleep(delay)
