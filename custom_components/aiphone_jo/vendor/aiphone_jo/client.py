"""High-level asyncio client for a paired JO-1MDW.

Behaviour:

* door release: ``STA_RELAY_REQ`` (30001, synthetic SDP) -> ``MNT_REQ`` (23021,
  RSLT 200) -> ``UNLOCK_REQ`` 26021 / ``OUT_OPTION_REQ`` 26121 -> teardown
  ``LEAVE_USER_REQ`` (30031) then ``DISCNCT_REQ`` (23003). Outside a
  monitoring session the station ignores the door commands;
* the station closes monitoring by itself ~60 s after monitor_req (23003): the
  client answers ``LEAVE_USER_REQ`` + ``DISCNCT_REP`` (24003, RSLT 200),
  otherwise the next monitor_req gets 486;
* ring tracking (23001 on the monitor-wide topic, 23004 cancel, 23003 end) and
  call answering (24001/24002/23008) are **experimental**.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import warnings
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

import aiohttp

from .cloud_api import RoomInfo, Terminal, get_room_info
from .const import (
    CALL_RING_TIMEOUT,
    END_SESSION_STEP_TIMEOUT,
    MAX_FRAME_BYTES,
    MQTT_REPLY_TIMEOUT,
    RSLT_BUSY,
    TERM_KIND_ENTRANCE_STATION,
    Mid,
)
from .credentials import CloudCredentials
from .exceptions import (
    AiphoneJoError,
    CloudAuthError,
    CloudConnectionError,
    CommandRejectedError,
    InvalidCredentialsError,
    InvalidInputError,
    MediaUnavailableError,
    NotConnectedError,
    ResponseTimeoutError,
    SessionError,
    StationBusyError,
)
from .frames import Message, body, header, parse_frame
from .live import LiveMonitor, Negotiator, SessionEndCallback
from .messages import (
    CallSignal,
    OutboundMessage,
    build_call_signal,
    build_disconnect_reply,
    build_disconnect_request,
    build_get_lock_status_request,
    build_monitor_request,
    build_out_option_request,
    build_relay_request,
    build_synthetic_relay_sdp,
    build_unlock_request,
)
from .messages.relay import MAX_SDP_LENGTH, RelayCommand
from .models import (
    ACTIVE_CALL_STATUSES,
    AiphoneEvent,
    CallSignalRecord,
    CallStatus,
    CallUpdatedEvent,
    ConnectionStateEvent,
    IncomingCall,
    LockStatus,
    MessageEvent,
    Output,
    RingEvent,
    SessionEndedBy,
    SessionEndedEvent,
)
from .redact import redact
from .transport import MqttConnection, MqttConnectOptions, TransportFactory, aiomqtt_connect

if TYPE_CHECKING:
    from .media import VideoStream

_LOGGER = logging.getLogger(__name__)
_RELAY_OK = frozenset({0, 200})
_SIGNAL_OK = frozenset({0, 200})
_MAX_CALL_SIGNALS = 20
RECONNECT_DELAYS = (5.0, 10.0, 30.0, 60.0, 120.0, 300.0)

Listener = Callable[[AiphoneEvent], None]


@dataclass(slots=True)
class _Waiter:
    mid: str
    cid: str | None
    future: asyncio.Future[Message]


def _rslt(message: Message) -> Any:
    return header(message).get("RSLT")


def _load_media() -> Any:
    try:
        return importlib.import_module("aiphone_jo.media")
    except ImportError as err:
        raise MediaUnavailableError(
            "Video support requires the media extra: pip install 'aiphone-jo[media]'."
        ) from err


def _resolve_output(output: Output | str | int, gate: Output | None) -> Output:
    if gate is not None:
        warnings.warn(
            "The 'gate' keyword is deprecated, use 'output'.", DeprecationWarning, stacklevel=3
        )
        output = gate
    try:
        return Output.coerce(output)
    except ValueError as err:
        raise InvalidInputError(str(err)) from err


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text_or_empty(value: Any, max_length: int = 128) -> str:
    return value[:max_length] if isinstance(value, str) else ""


class RelaySession:
    """Monitoring or call session bound to one relay ``CID``.

    Use :meth:`AiphoneJoClient.monitor` to obtain one. Closing it sends
    ``LEAVE_USER_REQ`` (30031) then ``DISCNCT_REQ`` (23003) unless the station
    already ended it.
    """

    def __init__(self, client: AiphoneJoClient, cid: str, cor_cli_id: str = "") -> None:
        self._client = client
        self.cid = cid
        self.cor_cli_id = cor_cli_id
        self.active = True
        self.ended_by: SessionEndedBy | None = None
        self._released = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    def _require_active(self) -> None:
        if not self.active:
            raise SessionError("The session is no longer active (closed or ended by the station).")

    def _update_correlation(self, reply: Message) -> None:
        # The CLIID of each successful reply is used as PCLIID by the following
        # relay requests.
        cli_id = header(reply).get("CLIID")
        if isinstance(cli_id, str) and cli_id:
            self.cor_cli_id = cli_id

    async def _relay(self, command: RelayCommand, sdp: str | None = None) -> Message:
        self._require_active()
        request = build_relay_request(
            command, self._client.credentials, cid=self.cid, cor_cli_id=self.cor_cli_id, sdp=sdp
        )
        reply = await self._client.request(request, cid=self.cid)
        if _rslt(reply) not in _RELAY_OK:
            raise CommandRejectedError(
                f"{request.label} rejected (RSLT={_rslt(reply)}).",
                mid=request.reply_mid or "",
                rslt=_rslt(reply),
            )
        self._update_correlation(reply)
        return reply

    async def request_offer(self) -> str:
        """``ADD_RCV_USER_REQ`` (30011 -> 31011): return the Janus SDP offer.

        The offer comes from a Janus VideoRoom (ICE-lite, PCMU audio, H264
        video). Answer it with a WebRTC stack, then call :meth:`send_answer`.
        """
        reply = await self._relay(RelayCommand.ADD_RCV_USER)
        sdp = body(reply).get("SDP")
        if not isinstance(sdp, str) or not sdp or len(sdp) > MAX_SDP_LENGTH:
            raise CommandRejectedError(
                "ADD_RCV_USER reply without SDP.", mid=Mid.ADD_RCV_USER_REP, rslt=_rslt(reply)
            )
        return sdp

    async def send_answer(self, sdp: str) -> None:
        """``UPDATE_RCV_USER_REQ`` (30012 -> 31012) with our complete SDP answer."""
        await self._relay(RelayCommand.UPDATE_RCV_USER, sdp)

    async def _output(self, message: OutboundMessage) -> None:
        self._require_active()
        reply = await self._client.request(message)
        if _rslt(reply) != 0:
            raise CommandRejectedError(
                f"{message.label} rejected (RSLT={_rslt(reply)}).",
                mid=message.reply_mid or "",
                rslt=_rslt(reply),
            )

    async def unlock(self) -> None:
        """``UNLOCK_REQ`` 26021 -> 27021: pulse on lock output 1."""
        await self._output(build_unlock_request(self._client.credentials))

    async def out_option(self) -> None:
        """``OUT_OPTION_REQ`` 26121 -> 27121: pulse on lock output 2 (option output)."""
        await self._output(build_out_option_request(self._client.credentials))

    async def open(
        self, output: Output | str | int = Output.UNLOCK, *, gate: Output | None = None
    ) -> None:
        """Pulse a lock output: :attr:`Output.UNLOCK` (1) or :attr:`Output.OPTION` (2).

        ``gate`` is a deprecated alias of ``output``.
        """
        if _resolve_output(output, gate) is Output.UNLOCK:
            await self.unlock()
        else:
            await self.out_option()

    async def close(self) -> dict[str, Any]:
        """End the session: ``LEAVE_USER_REQ`` then ``DISCNCT_REQ`` (REASON 16).

        Each step is attempted even if the previous one failed; the local
        state is always reset. Returns the
        ``RSLT`` (or error text) of each step.
        """
        steps: dict[str, Any] = {}
        if self.active:
            self._client._detach_session(self, SessionEndedBy.CLIENT)
            credentials = self._client.credentials
            try:
                leave = build_relay_request(
                    RelayCommand.LEAVE_USER, credentials, cid=self.cid, cor_cli_id=self.cor_cli_id
                )
                reply = await self._client.request(
                    leave, cid=self.cid, timeout=END_SESSION_STEP_TIMEOUT
                )
                steps["leave"] = _rslt(reply)
            except AiphoneJoError as err:
                steps["leave"] = f"error: {err}"
            try:
                disconnect = build_disconnect_request(credentials, self.cid)
                reply = await self._client.request(
                    disconnect, cid=self.cid, timeout=END_SESSION_STEP_TIMEOUT
                )
                steps["disconnect"] = _rslt(reply)
            except AiphoneJoError as err:
                steps["disconnect"] = f"error: {err}"
            _LOGGER.debug("Session ended by the client: %s", steps)
        self._release()
        return steps

    def _mark_ended(self, by: SessionEndedBy) -> None:
        self.active = False
        self.ended_by = by

    def _release(self) -> None:
        if not self._released:
            self._released = True
            self._client._release_session(self)


class MonitorSession(RelaySession):
    """Monitoring session opened by :meth:`AiphoneJoClient.monitor`."""

    def __init__(self, client: AiphoneJoClient, cid: str, term_id: int, term_kind: int) -> None:
        super().__init__(client, cid)
        self.term_id = term_id
        self.term_kind = term_kind


class CallSession(RelaySession):
    """**Experimental**: handling of an incoming call.

    Sequence: :meth:`request_offer` /
    :meth:`send_answer` (station video), :meth:`ready` (24001),
    :meth:`add_send_audio` (30021, our microphone), :meth:`reply` (24002),
    then :meth:`start` (23008 RSLT 200) to pick up, or :meth:`reject`.
    """

    def __init__(self, client: AiphoneJoClient, call: IncomingCall) -> None:
        super().__init__(client, call.cid, call.station_client_id)
        self.call = call

    async def _signal(self, signal: CallSignal) -> Message | None:
        self._require_active()
        message = build_call_signal(
            self._client.credentials, signal, self.cid, call_kind=self.call.call_kind
        )
        if message.reply_mid is None:
            await self._client.publish(message)
            return None
        try:
            return await self._client.request(message, cid=self.cid)
        except ResponseTimeoutError:
            _LOGGER.warning("No %s after %s", message.reply_mid, message.label)
            return None

    async def ready(self) -> None:
        """``READY_NFY`` (24001, CKIND 0)."""
        await self._signal(CallSignal.READY)

    async def add_send_audio(self, sdp: str) -> str:
        """``ADD_SND_USER_REQ`` (30021 -> 31021): send our audio offer, return the answer."""
        reply = await self._relay(RelayCommand.ADD_SND_USER, sdp)
        answer = body(reply).get("SDP")
        return answer if isinstance(answer, str) and len(answer) <= MAX_SDP_LENGTH else ""

    async def reply(self) -> Message | None:
        """``REPLY`` (24002, RSLT 200) -> ``REP_RCV_NFY`` (25001)."""
        return await self._signal(CallSignal.REPLY)

    async def start(self) -> bool:
        """``CALL_STA_REQ`` (23008, RSLT 200) -> 24008: pick up. Returns ``True`` if accepted."""
        reply = await self._signal(CallSignal.START)
        if reply is not None and _rslt(reply) in _SIGNAL_OK:
            self._client._set_call_status(self.call, CallStatus.TALKING)
            return True
        return False

    async def close(self) -> dict[str, Any]:
        """Hang up: ``LEAVE_USER_REQ`` then ``DISCNCT_REQ`` (REASON 16)."""
        steps = await super().close()
        self._client._end_call(self.call, CallStatus.ENDED)
        return steps

    async def reject(self) -> None:
        """Decline: ``CALL_STA_REQ`` (23008, RSLT 603) then ``LEAVE_USER_REQ``."""
        self._require_active()
        credentials = self._client.credentials
        try:
            await self._client.publish(
                build_call_signal(credentials, CallSignal.REJECT, self.cid, self.call.call_kind)
            )
            await self._client.publish(
                build_relay_request(
                    RelayCommand.LEAVE_USER, credentials, cid=self.cid, cor_cli_id=self.cor_cli_id
                )
            )
        finally:
            self._client._detach_session(self, SessionEndedBy.CLIENT)
            self._client._end_call(self.call, CallStatus.REJECTED)
            self._release()


class AiphoneJoClient:
    """Cloud (MQTT) client of one paired JO-1MDW terminal.

    A client certificate can only hold **one** MQTT connection at a time: do
    not run two clients with the same credentials.
    """

    def __init__(
        self,
        credentials: CloudCredentials,
        *,
        session: aiohttp.ClientSession | None = None,
        transport_factory: TransportFactory = aiomqtt_connect,
        auto_reconnect: bool = True,
        reply_timeout: float = MQTT_REPLY_TIMEOUT,
        ring_timeout: float = CALL_RING_TIMEOUT,
        reconnect_delays: tuple[float, ...] = RECONNECT_DELAYS,
        camera_terminal: Terminal | int | None = None,
    ) -> None:
        if not credentials.send_topic:
            raise InvalidCredentialsError("send_topic is required to send commands.")
        self._credentials = credentials
        self._http = session
        self._transport_factory = transport_factory
        self._auto_reconnect = auto_reconnect
        self._reply_timeout = reply_timeout
        self._ring_timeout = ring_timeout
        self._reconnect_delays = reconnect_delays or RECONNECT_DELAYS
        self._connection: MqttConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._closing = False
        self._listeners: list[Listener] = []
        self._waiters: list[_Waiter] = []
        self._mid_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._session_lock = asyncio.Lock()
        self._session: RelaySession | None = None
        self._call: IncomingCall | None = None
        self._call_timer: asyncio.TimerHandle | None = None
        self._background: set[asyncio.Task[Any]] = set()
        self._broadcast_subscribed = False
        self._auth_failed = False
        self.camera_terminal = camera_terminal
        self._camera_candidates: tuple[Terminal, ...] | None = None
        self._video_streams: list[Any] = []

    # ------------------------------------------------------------------ state

    @property
    def credentials(self) -> CloudCredentials:
        """Credentials in use."""
        return self._credentials

    @property
    def connected(self) -> bool:
        """``True`` while the MQTT connection is up."""
        return self._connection is not None

    @property
    def auth_failed(self) -> bool:
        """``True`` after the cloud refused this identity (reconnection stopped)."""
        return self._auth_failed

    @property
    def broadcast_subscribed(self) -> bool:
        """``True`` if the broker accepted the monitor-wide (ring) topic."""
        return self._broadcast_subscribed

    @property
    def current_call(self) -> IncomingCall | None:
        """Current (or last seen) incoming call."""
        return self._call

    @property
    def active_session(self) -> RelaySession | None:
        """Open monitoring/call session, if any."""
        return self._session if self._session is not None and self._session.active else None

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        """Register an event callback (called in the event loop); returns an unsubscriber."""
        self._listeners.append(listener)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return remove

    def _emit(self, event: AiphoneEvent) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                _LOGGER.exception("Error in an aiphone_jo listener")

    # ------------------------------------------------------------- connection

    async def connect(self) -> None:
        """Connect to AWS IoT and subscribe.

        Raises :class:`CloudAuthError` when the identity is refused (pair
        again) and :class:`CloudConnectionError` for other failures.
        """
        self._closing = False
        try:
            await self._open_connection()
        except CloudAuthError:
            self._auth_failed = True
            raise
        self._auth_failed = False

    async def _open_connection(self) -> None:
        credentials = self._credentials
        context = await credentials.async_create_ssl_context()
        topics = [credentials.recv_topic]
        if credentials.broadcast_topic:
            topics.append(credentials.broadcast_topic)
        connection = await self._transport_factory(
            MqttConnectOptions(
                host=credentials.host,
                port=credentials.port,
                client_id=credentials.client_id,
                topics=tuple(topics),
                ssl_context=context,
            )
        )
        if self._closing:
            await connection.close()
            raise CloudConnectionError("Client closed while connecting.")
        self._connection = connection
        broadcast = credentials.broadcast_topic
        self._broadcast_subscribed = bool(broadcast and connection.granted.get(broadcast))
        if broadcast and not self._broadcast_subscribed:
            _LOGGER.warning("Monitor-wide topic refused by the broker: rings may be missed")
        self._reader = asyncio.create_task(self._read_loop(connection), name="aiphone-jo-mqtt")
        _LOGGER.debug("MQTT connected (monitor-wide topic: %s)", self._broadcast_subscribed)
        self._emit(ConnectionStateEvent(True, self._broadcast_subscribed))

    async def _read_loop(self, connection: MqttConnection) -> None:
        error: str | None = None
        try:
            async for topic, payload in connection.messages():
                self._handle_payload(topic, payload)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            error = str(err) or type(err).__name__
        if self._connection is connection:
            self._on_connection_lost(error or "connection closed")

    def _on_connection_lost(self, error: str) -> None:
        connection, self._connection = self._connection, None
        self._broadcast_subscribed = False
        if connection is not None:
            self._spawn(connection.close())
        self._fail_waiters(NotConnectedError(f"MQTT connection lost: {error}"))
        if self._session is not None and self._session.active:
            self._detach_session(self._session, SessionEndedBy.CONNECTION_LOST)
        _LOGGER.warning("MQTT connection lost: %s", error)
        self._emit(ConnectionStateEvent(False, False, error))
        if self._auto_reconnect and not self._closing and self._reconnect_task is None:
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        attempt = 0
        try:
            while not self._closing and self._connection is None:
                delay = self._reconnect_delays[min(attempt, len(self._reconnect_delays) - 1)]
                await asyncio.sleep(delay)
                attempt += 1
                try:
                    await self._open_connection()
                except CloudAuthError as err:
                    self._auth_failed = True
                    _LOGGER.warning("MQTT identity refused, reconnection stopped: %s", err)
                    self._emit(ConnectionStateEvent(False, False, str(err), auth_failed=True))
                    return
                except AiphoneJoError as err:
                    _LOGGER.debug("MQTT reconnection attempt %d failed: %s", attempt, err)
        finally:
            self._reconnect_task = None

    async def disconnect(self) -> None:
        """Close the session (if any) and the MQTT connection; stop reconnecting."""
        self._closing = True
        if self._session is not None and self._session.active and self._connection is not None:
            with contextlib.suppress(AiphoneJoError):
                await self._session.close()
        for task in (self._reconnect_task, self._reader):
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._reconnect_task = self._reader = None
        if self._call_timer is not None:
            self._call_timer.cancel()
        connection, self._connection = self._connection, None
        self._fail_waiters(NotConnectedError("Client disconnected."))
        if connection is not None:
            await connection.close()
            self._emit(ConnectionStateEvent(False, False, None))
        for task in list(self._background):
            task.cancel()

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ---------------------------------------------------------- send / reply

    async def publish(self, message: OutboundMessage) -> None:
        """Publish an envelope without waiting for a reply."""
        connection = self._connection
        if connection is None:
            raise NotConnectedError("An active MQTT connection is required.")
        _LOGGER.debug("tx %s %s", message.label, redact(message.payload, identifiers=True))
        await connection.publish(message.topic, message.encode())

    async def request(
        self, message: OutboundMessage, *, cid: str | None = None, timeout: float | None = None
    ) -> Message:
        """Publish and wait for ``message.reply_mid`` (and ``CID`` when given).

        Requests expecting the same reply MID are serialized.
        """
        if message.reply_mid is None:
            raise InvalidInputError(f"{message.label} expects no reply.")
        async with self._mid_locks[message.reply_mid]:
            future: asyncio.Future[Message] = asyncio.get_running_loop().create_future()
            waiter = _Waiter(message.reply_mid, cid, future)
            self._waiters.append(waiter)
            try:
                await self.publish(message)
                async with asyncio.timeout(timeout or self._reply_timeout):
                    return await future
            except TimeoutError as err:
                raise ResponseTimeoutError(
                    f"No {message.reply_mid} reply to {message.label} in time.",
                    mid=message.reply_mid,
                ) from err
            finally:
                with contextlib.suppress(ValueError):
                    self._waiters.remove(waiter)

    def _fail_waiters(self, error: BaseException) -> None:
        for waiter in self._waiters:
            if not waiter.future.done():
                waiter.future.set_exception(error)

    def _handle_payload(self, topic: str, payload: bytes) -> None:
        if len(payload) > MAX_FRAME_BYTES:
            _LOGGER.warning("MQTT message too large (%d bytes), ignored", len(payload))
            return
        try:
            message = parse_frame(payload)
        except AiphoneJoError as err:
            # Never dump opaque payloads: they may contain unknown secrets.
            _LOGGER.warning("Invalid MQTT message (%d bytes): %s", len(payload), err)
            return
        _LOGGER.debug("rx %s", redact(message, identifiers=True))
        head = header(message)
        mid, cid = str(head.get("MID")), head.get("CID")
        for waiter in self._waiters:
            if waiter.future.done() or waiter.mid != mid:
                continue
            if waiter.cid and isinstance(cid, str) and cid and cid != waiter.cid:
                continue
            waiter.future.set_result(message)
            break
        self._emit(MessageEvent(topic, message))
        self._track_call(message, topic)
        if mid == Mid.DISCONNECT_REQ:
            self._on_station_disconnect(message)

    # ---------------------------------------------------------- read commands

    async def get_lock_status(self) -> LockStatus:
        """``GET_LOCK_STATUS_REQ`` (26031 -> 27031); works outside a monitoring session."""
        reply = await self.request(build_get_lock_status_request(self._credentials))
        if _rslt(reply) != 0:
            raise CommandRejectedError(
                f"GET_LOCK_STATUS rejected (RSLT={_rslt(reply)}).",
                mid=Mid.LOCK_STATUS_REP,
                rslt=_rslt(reply),
            )
        return LockStatus.from_body(body(reply))

    async def get_room_info(self) -> RoomInfo:
        """``/getRoomInfo`` over HTTPS (requires the ``session`` given to the constructor)."""
        if self._http is None:
            raise InvalidInputError("An aiohttp ClientSession is required for /getRoomInfo.")
        return await get_room_info(self._http, self._credentials)

    # --------------------------------------------------------------- sessions

    def monitor(
        self, terminal: Terminal | int, term_kind: int = TERM_KIND_ENTRANCE_STATION
    ) -> _MonitorContext:
        """Open a monitoring session on a camera entrance station.

        Use as ``async with client.monitor(terminal) as session:``. Only one
        session exists at a time (waits for the current one). **Triggers a
        real monitoring call on the station** (screen, possibly a tone).
        Raises :class:`StationBusyError` on ``RSLT 486``.
        """
        if isinstance(terminal, Terminal):
            if terminal.id is None or terminal.term_kind is None:
                raise InvalidInputError("The terminal has no id/kind.")
            return _MonitorContext(self, terminal.id, terminal.term_kind)
        return _MonitorContext(self, terminal, term_kind)

    async def start_monitor(
        self, terminal: Terminal | int, term_kind: int = TERM_KIND_ENTRANCE_STATION
    ) -> MonitorSession:
        """Same as :meth:`monitor` without a context manager; call ``close()`` afterwards."""
        return await self.monitor(terminal, term_kind).__aenter__()

    async def start_camera_session(
        self, terminal: Terminal | int | None = None, term_kind: int = TERM_KIND_ENTRANCE_STATION
    ) -> MonitorSession:
        """Open a monitoring session on ``terminal``, :attr:`camera_terminal`, or the first
        camera station of ``/getRoomInfo`` that is not busy.

        Raises :class:`StationBusyError` when every candidate answered 486 and
        :class:`InvalidInputError` when no station is known.
        """
        if terminal is not None:
            return await self.start_monitor(terminal, term_kind)
        if self.camera_terminal is not None:
            return await self.start_monitor(self.camera_terminal, term_kind)
        if self._camera_candidates is None:
            if self._http is None:
                raise InvalidInputError(
                    "No camera station: pass a terminal, set camera_terminal, "
                    "or give the client an aiohttp session for /getRoomInfo."
                )
            self._camera_candidates = (await self.get_room_info()).camera_terminals
        if not self._camera_candidates:
            raise InvalidInputError("/getRoomInfo lists no camera entrance station.")
        busy: StationBusyError | None = None
        for candidate in self._camera_candidates:
            try:
                return await self.start_monitor(candidate)
            except StationBusyError as err:
                busy = err
        if busy is None:
            raise InvalidInputError("No camera entrance station could be monitored.")
        raise busy

    def live_monitor(
        self,
        negotiate: Negotiator,
        terminal: Terminal | int | None = None,
        *,
        on_session_end: SessionEndCallback | None = None,
        renew: bool = True,
    ) -> LiveMonitor:
        """Monitoring with media negotiation, renewed automatically (see :class:`LiveMonitor`).

        ``negotiate(offer_sdp) -> answer_sdp`` is awaited for every new
        session; no media library is required by the core.
        """
        return LiveMonitor(self, negotiate, terminal, on_session_end=on_session_end, renew=renew)

    async def snapshot(
        self, terminal: Terminal | int | None = None, *, timeout: float = 15.0
    ) -> bytes:
        """JPEG image of the entrance station camera (requires the ``media`` extra).

        Reuses the latest frame of an active :meth:`video_stream`; otherwise
        opens a monitoring session, decodes the first complete video frame and
        always closes the session (30031 then 23003). Raises
        :class:`MediaUnavailableError`, :class:`MediaTimeoutError` or
        :class:`StationBusyError`.
        """
        media = _load_media()
        image: bytes = await media.snapshot(self, terminal, timeout=timeout)
        return image

    def video_stream(self, terminal: Terminal | int | None = None) -> VideoStream:
        """Live video of the entrance station (requires the ``media`` extra).

        Use as ``async with client.video_stream() as stream:``; see
        :class:`aiphone_jo.media.VideoStream`.
        """
        media = _load_media()
        stream: VideoStream = media.VideoStream(self, terminal)
        return stream

    async def _open_monitor(self, term_id: int, term_kind: int) -> MonitorSession:
        await self._session_lock.acquire()
        try:
            return await self._start_monitor(term_id, term_kind)
        except BaseException:
            session = self._session
            if session is not None:
                session._mark_ended(SessionEndedBy.CLIENT)
                session._release()
            elif self._session_lock.locked():
                self._session_lock.release()
            raise

    async def _start_monitor(self, term_id: int, term_kind: int) -> MonitorSession:
        credentials = self._credentials
        # A new session always gets a fresh CID, never an old one.
        start = build_relay_request(
            RelayCommand.STA_RELAY, credentials, sdp=build_synthetic_relay_sdp()
        )
        cid = str(start.header["CID"])
        monitor = build_monitor_request(credentials, term_id, term_kind, cid)
        session = MonitorSession(self, cid, term_id, term_kind)
        self._session = session
        reply = await self.request(start, cid=cid)
        if _rslt(reply) not in _RELAY_OK:
            session._mark_ended(SessionEndedBy.CLIENT)
            raise CommandRejectedError(
                f"STA_RELAY_REQ rejected (RSLT={_rslt(reply)}).",
                mid=Mid.STA_RELAY_REP,
                rslt=_rslt(reply),
            )
        session._update_correlation(reply)
        try:
            reply = await self.request(monitor, cid=cid)
        except ResponseTimeoutError:
            # The station may have started monitoring anyway: full teardown.
            await session.close()
            raise
        result = _rslt(reply)
        if result not in _SIGNAL_OK:
            # Release the Janus room so that it does not look busy later.
            session._mark_ended(SessionEndedBy.CLIENT)
            with contextlib.suppress(AiphoneJoError):
                await self.publish(
                    build_relay_request(
                        RelayCommand.LEAVE_USER, credentials, cid=cid, cor_cli_id=session.cor_cli_id
                    )
                )
            error = StationBusyError if result == RSLT_BUSY else CommandRejectedError
            raise error(f"MNT_REQ rejected (RSLT={result}).", mid=Mid.MONITOR_REP, rslt=result)
        session._update_correlation(reply)
        return session

    async def open_door(
        self,
        output: Output | str | int = Output.UNLOCK,
        terminal: Terminal | int | None = None,
        term_kind: int = TERM_KIND_ENTRANCE_STATION,
        *,
        gate: Output | None = None,
    ) -> None:
        """Pulse a lock output: reuse the open session, or open a short monitoring one.

        ``output`` is :attr:`Output.UNLOCK` (or ``1``: UNLOCK_REQ 26021, output
        1) or :attr:`Output.OPTION` (or ``2``: OUT_OPTION_REQ 26121, option
        output 2); what each output opens depends on the installation. A
        temporary session is always closed (30031 then 23003), even on error.
        ``gate`` is a deprecated alias of ``output``.
        """
        selected = _resolve_output(output, gate)
        current = self.active_session
        if current is not None:
            await current.open(selected)
            return
        if terminal is None:
            raise InvalidInputError("A camera entrance station is required to open a session.")
        async with self.monitor(terminal, term_kind) as session:
            await session.open(selected)

    async def answer_call(self, call: IncomingCall | None = None) -> CallSession:
        """**Experimental**: take over the ringing call (reuses the ring ``CID``).

        No STA_RELAY_REQ nor monitor_req: the station already opened the
        session.
        """
        target = call or self._call
        if target is None or target.status is not CallStatus.RINGING:
            raise SessionError("No ringing call.")
        if self._session_lock.locked():
            raise SessionError("A session is already open: close it first.")
        if self._connection is None:
            raise NotConnectedError("An active MQTT connection is required.")
        # The lock is free: acquire() returns without suspending.
        await self._session_lock.acquire()
        target.engaged = True
        session = CallSession(self, target)
        self._session = session
        return session

    def _detach_session(self, session: RelaySession, by: SessionEndedBy) -> None:
        if not session.active:
            return
        session._mark_ended(by)
        if isinstance(session, CallSession):
            session.call.engaged = False
        self._emit(SessionEndedEvent(session.cid, by))

    def _release_session(self, session: RelaySession) -> None:
        if self._session is session:
            self._session = None
        if self._session_lock.locked():
            self._session_lock.release()

    def _on_station_disconnect(self, message: Message) -> None:
        # The station closes monitoring ~60 s after monitor_req with 23003 on the
        # session CID; the expected answer is LEAVE_USER_REQ then DISCNCT_REP
        # RSLT 200.
        session = self._session
        cid = header(message).get("CID")
        if session is None or not session.active or not cid or cid != session.cid:
            return
        cor_cli_id = session.cor_cli_id
        self._detach_session(session, SessionEndedBy.STATION)
        if isinstance(session, CallSession):
            self._end_call(session.call, CallStatus.ENDED)
        self._spawn(self._answer_station_disconnect(session.cid, cor_cli_id))

    async def _answer_station_disconnect(self, cid: str, cor_cli_id: str) -> None:
        try:
            await self.publish(
                build_relay_request(
                    RelayCommand.LEAVE_USER, self._credentials, cid=cid, cor_cli_id=cor_cli_id
                )
            )
            await self.publish(build_disconnect_reply(self._credentials, cid))
        except AiphoneJoError as err:
            _LOGGER.warning("Incomplete cleanup after 23003: %s", err)

    # ------------------------------------------------------------ ring / call

    def _track_call(self, message: Message, topic: str) -> None:
        head = header(message)
        mid, cid = str(head.get("MID")), head.get("CID")
        if mid == Mid.CALL_REQ:
            if not isinstance(cid, str) or not 0 < len(cid) <= 256:
                return
            if self._call and self._call.cid == cid:
                return
            new_call = IncomingCall(
                cid=cid,
                station_client_id=_text_or_empty(head.get("CLIID")),
                display_name=_text_or_empty(head.get("DSP1")),
                display_name_2=_text_or_empty(head.get("DSP2")),
                call_kind=_int_or_none(head.get("CKIND")),
                term_kind=_int_or_none(head.get("TMKIND")),
                term_id=_int_or_none(head.get("TMID")),
                topic=topic,
                ringing_at=datetime.now().astimezone(),
            )
            self._call = new_call
            if self._call_timer is not None:
                self._call_timer.cancel()
            self._call_timer = asyncio.get_running_loop().call_later(
                self._ring_timeout, self._expire_call, new_call
            )
            _LOGGER.info("Ring from entrance station %s", new_call.term_id)
            self._emit(RingEvent(new_call))
            return
        call = self._call
        if call is None or cid != call.cid:
            return
        call.signals.append(
            CallSignalRecord(
                mid,
                _int_or_none(head.get("RSLT")),
                _text_or_empty(head.get("CLIID")) or None,
                datetime.now().astimezone(),
            )
        )
        del call.signals[:-_MAX_CALL_SIGNALS]
        if mid in (Mid.CALL_CANCEL_REQ, Mid.DISCONNECT_REQ) and call.status in ACTIVE_CALL_STATUSES:
            session = self._session
            engaged = isinstance(session, CallSession) and session.active and session.cid == cid
            if mid == Mid.CALL_CANCEL_REQ and engaged and session is not None:
                cor_cli_id = session.cor_cli_id
                self._detach_session(session, SessionEndedBy.STATION)
                self._spawn(self._answer_engaged_cancel(call, cor_cli_id))
            self._end_call(
                call, CallStatus.CANCELLED if mid == Mid.CALL_CANCEL_REQ else CallStatus.ENDED
            )

    async def _answer_engaged_cancel(self, call: IncomingCall, cor_cli_id: str) -> None:
        # 23004 while engaged: CALL_CANCEL_REP (24004 RSLT 200 REASON 16) then LEAVE_USER.
        try:
            await self.publish(
                build_call_signal(
                    self._credentials, CallSignal.CANCEL_REPLY, call.cid, call.call_kind
                )
            )
            await self.publish(
                build_relay_request(
                    RelayCommand.LEAVE_USER, self._credentials, cid=call.cid, cor_cli_id=cor_cli_id
                )
            )
        except AiphoneJoError as err:
            _LOGGER.warning("Incomplete cleanup after 23004: %s", err)

    def _expire_call(self, call: IncomingCall) -> None:
        if call.status is CallStatus.RINGING and not call.engaged:
            self._end_call(call, CallStatus.EXPIRED)

    def _set_call_status(self, call: IncomingCall, status: CallStatus) -> None:
        if call.status is not status:
            call.status = status
            self._emit(CallUpdatedEvent(call))

    def _end_call(self, call: IncomingCall, status: CallStatus) -> None:
        if call.status not in ACTIVE_CALL_STATUSES:
            return
        call.ended_at = datetime.now().astimezone()
        if self._call_timer is not None and self._call is call:
            self._call_timer.cancel()
            self._call_timer = None
        self._set_call_status(call, status)


class _MonitorContext:
    """Async context manager returned by :meth:`AiphoneJoClient.monitor`."""

    def __init__(self, client: AiphoneJoClient, term_id: int, term_kind: int) -> None:
        self._client = client
        self._term_id = term_id
        self._term_kind = term_kind
        self._session: MonitorSession | None = None

    async def __aenter__(self) -> MonitorSession:
        self._session = await self._client._open_monitor(self._term_id, self._term_kind)
        return self._session

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is not None:
            await self._session.close()
