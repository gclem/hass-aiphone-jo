"""Local pairing over TLS (01002 OTP, 01003 authentication, 01005 terminal).

Every failure is a :class:`~aiphone_jo.exceptions.PairingError` carrying the
stage, the reply MID, the raw ``RSLT``, a stable typed reason and whether the
same step can be retried on the same connection (see
:mod:`aiphone_jo.exceptions`).

The monitor presents a self-signed certificate: trust is decided by pinning
its SHA-256 fingerprint (trust on first use, like SSH), on the same TLS
connection and before any application data is sent, because the monitor's
pairing window is short.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import hashlib
import logging
import secrets
import ssl
import string
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import aiohttp
from cryptography import x509

from ._util import format_fingerprint, normalize_fingerprint, require_text
from .cloud_api import regist_client, unregister
from .const import (
    DEFAULT_API_ORIGIN,
    DEFAULT_SYS_TYPE,
    LOCAL_REQUEST_TIMEOUT,
    MAX_TERMINAL_NAME,
    SUPPORTED_SYS_TYPES,
    TERMINAL_REGISTER_TIMEOUT,
    Mid,
)
from .credentials import CloudCredentials
from .discovery import DiscoveredMonitor
from .exceptions import (
    APP_CODE_INCOMPATIBLE,
    APP_CODE_REGIST_CLIENT,
    RETRY_ON_SAME_CONNECTION,
    AiphoneJoError,
    CloudApiError,
    IncompatibleVersionError,
    InvalidCredentialsError,
    PairingCloudError,
    PairingError,
    PairingFailureReason,
    PairingLinkClosedError,
    PairingRejectedError,
    PairingStage,
    PairingTimeoutError,
    UntrustedPeerError,
    pairing_reason,
)
from .frames import JsonLinesDecoder, Message, body, frame, header
from .redact import redact

_LOGGER = logging.getLogger(__name__)
_READ_CHUNK = 4096
_NAME_TAKEN = 3
_MAX_RENAME_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class PeerCertificate:
    """Certificate presented by the monitor's local TLS server."""

    fingerprint: str
    subject: dict[str, str] = field(default_factory=dict)
    issuer: dict[str, str] = field(default_factory=dict)
    not_before: datetime | None = None
    not_after: datetime | None = None
    protocol: str | None = None
    cipher: str | None = None

    def matches(self, fingerprint: str) -> bool:
        """``True`` when ``fingerprint`` (any ``:``/case format) is this certificate's."""
        return normalize_fingerprint(fingerprint) == normalize_fingerprint(self.fingerprint)


def _names(name: x509.Name) -> dict[str, str]:
    return {attribute.rfc4514_attribute_name: str(attribute.value) for attribute in name}


def _tls_context() -> ssl.SSLContext:
    # TLS 1.2 only; the certificate is self-signed so verification is replaced
    # by pinning.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    return context


def _peer_certificate(writer: asyncio.StreamWriter) -> PeerCertificate:
    ssl_object: ssl.SSLObject | None = writer.get_extra_info("ssl_object")
    der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
    if not der or ssl_object is None:
        raise PairingError(
            "The peer did not present a certificate.",
            stage=PairingStage.TLS,
            reason=PairingFailureReason.TLS_FAILED,
        )
    certificate = x509.load_der_x509_certificate(der)
    cipher = ssl_object.cipher()
    return PeerCertificate(
        fingerprint=format_fingerprint(hashlib.sha256(der).digest()),
        subject=_names(certificate.subject),
        issuer=_names(certificate.issuer),
        not_before=certificate.not_valid_before_utc,
        not_after=certificate.not_valid_after_utc,
        protocol=ssl_object.version(),
        cipher=cipher[0] if cipher else None,
    )


_CLOSED_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionRefusedError,
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
    asyncio.IncompleteReadError,
    ssl.SSLEOFError,
    ssl.SSLZeroReturnError,
)


def _connection_error(err: BaseException, stage: PairingStage = PairingStage.TLS) -> PairingError:
    """Classify a socket/TLS failure: closed window, TLS failure or timeout."""
    if isinstance(err, TimeoutError):
        return PairingTimeoutError("TLS: connection to the monitor timed out.", stage=stage)
    if isinstance(err, _CLOSED_ERRORS):
        return PairingLinkClosedError(
            f"The monitor closed or refused the pairing connection ({type(err).__name__}).",
            stage=stage,
        )
    return PairingError(
        f"TLS: {err or type(err).__name__}", stage=stage, reason=PairingFailureReason.TLS_FAILED
    )


async def inspect_peer(
    host: str, port: int, *, timeout: float = LOCAL_REQUEST_TIMEOUT
) -> PeerCertificate:
    """TLS handshake only: return the monitor certificate, send no application data."""
    try:
        async with asyncio.timeout(timeout):
            _, writer = await asyncio.open_connection(host, port, ssl=_tls_context())
    except (OSError, TimeoutError) as err:
        raise _connection_error(err) from err
    try:
        return _peer_certificate(writer)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


class LocalLink:
    """Pinned TLS "JSON lines" connection to the monitor (one request at a time)."""

    def __init__(self) -> None:
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._pending: tuple[str, PairingStage | None, asyncio.Future[Message]] | None = None
        self._ready = False
        self.peer: PeerCertificate | None = None
        self.on_disconnect: Callable[[], None] | None = None

    @property
    def connected(self) -> bool:
        """``True`` once the TLS session is open and the peer is trusted."""
        return self._ready and self._writer is not None and not self._writer.is_closing()

    async def connect(
        self,
        host: str,
        port: int,
        *,
        fingerprint: str | None = None,
        decide: Callable[[PeerCertificate], bool] | None = None,
        timeout: float = LOCAL_REQUEST_TIMEOUT,
    ) -> PeerCertificate:
        """Open TLS and approve the peer with ``fingerprint`` or ``decide(peer)``.

        Nothing is sent before approval; an untrusted peer raises
        :class:`UntrustedPeerError` (with ``peer`` set) and the socket is closed.
        A refused or closed connection raises :class:`PairingLinkClosedError`, a
        handshake failure a :class:`PairingError` with ``reason == TLS_FAILED``
        and a timeout :class:`PairingTimeoutError`.
        """
        if fingerprint is None and decide is None:
            raise PairingError(
                "A pinned fingerprint or a trust decision is required.",
                stage=PairingStage.TRUST,
                reason=PairingFailureReason.INVALID_STATE,
            )
        expected = normalize_fingerprint(fingerprint) if fingerprint is not None else None
        await self.close()
        try:
            async with asyncio.timeout(timeout):
                reader, writer = await asyncio.open_connection(host, port, ssl=_tls_context())
        except (OSError, TimeoutError) as err:
            raise _connection_error(err) from err
        try:
            peer = _peer_certificate(writer)
            trusted = decide(peer) is True if decide else peer.matches(expected or "")
        except BaseException:
            writer.close()
            raise
        if not trusted:
            writer.close()
            raise UntrustedPeerError(
                "TLS fingerprint not approved or different from the approved one. "
                "No message was sent.",
                peer=peer,
            )
        self._reader, self._writer, self.peer, self._ready = reader, writer, peer, True
        self._read_task = asyncio.create_task(self._read_loop(reader), name="aiphone-jo-local")
        _LOGGER.debug("TLS connection open, peer certificate approved")
        return peer

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        decoder = JsonLinesDecoder()
        error: AiphoneJoError | None = None
        try:
            while chunk := await reader.read(_READ_CHUNK):
                for message in decoder.push(chunk):
                    _LOGGER.debug("rx pairing %s", redact(message, identifiers=True))
                    self._resolve(message)
        except AiphoneJoError as err:
            error = err
        except OSError as err:
            error = _connection_error(err)
        finally:
            if self._reader is reader:
                self._ready = False
                self._fail_pending(error)
                if self._writer is not None:
                    self._writer.close()
                if self.on_disconnect:
                    self.on_disconnect()

    def _resolve(self, message: Message) -> None:
        if self._pending is None:
            return
        expected_mid, _, future = self._pending
        if header(message).get("MID") != expected_mid or future.done():
            return
        self._pending = None
        future.set_result(message)

    def _fail_pending(self, error: AiphoneJoError | None) -> None:
        if self._pending is None:
            return
        _, stage, future = self._pending
        self._pending = None
        if future.done():
            return
        if error is None:
            error = PairingLinkClosedError(
                "The monitor closed the pairing connection.", stage=stage
            )
        elif isinstance(error, PairingError) and error.stage in (None, PairingStage.TLS):
            error.stage = stage or error.stage
        future.set_exception(error)

    async def request(
        self,
        mid: str,
        payload: dict[str, Any] | None,
        response_mid: str,
        *,
        timeout: float = LOCAL_REQUEST_TIMEOUT,
        stage: PairingStage | None = None,
    ) -> Message:
        """Send a frame and wait for ``response_mid`` with ``RSLT == 0``.

        A timeout closes the link: a late reply must never satisfy a later
        request with the same MID. A non-zero ``RSLT`` raises
        :class:`PairingRejectedError` with the typed reason.
        """
        if not self.connected or self._writer is None:
            raise PairingLinkClosedError(
                "Connect to and approve the monitor first: the pairing connection is closed.",
                stage=stage,
            )
        if self._pending is not None:
            raise PairingError(
                "A TLS exchange is already in progress.",
                stage=stage,
                reason=PairingFailureReason.INVALID_STATE,
            )
        future: asyncio.Future[Message] = asyncio.get_running_loop().create_future()
        self._pending = (response_mid, stage, future)
        _LOGGER.debug(
            "tx pairing %s", redact({"HEADER": {"MID": mid}, "BODY": payload}, identifiers=True)
        )
        try:
            self._writer.write(frame(mid, payload))
            await self._writer.drain()
            async with asyncio.timeout(timeout):
                reply = await future
        except TimeoutError as err:
            self._pending = None
            await self.close()
            raise PairingTimeoutError(
                f"No {response_mid} reply. Reconnection required.", stage=stage, mid=response_mid
            ) from err
        except OSError as err:
            self._pending = None
            await self.close()
            raise _connection_error(err, stage or PairingStage.TLS) from err
        rslt = body(reply).get("RSLT")
        if rslt != 0:
            reason = pairing_reason(response_mid, rslt)
            error = PairingRejectedError(
                f"The monitor rejected {mid} (RSLT={rslt if rslt is not None else 'missing'}).",
                stage=stage,
                reason=reason,
                mid=response_mid,
                rslt=rslt,
                retry_same_connection=(response_mid, rslt) in RETRY_ON_SAME_CONNECTION,
            )
            if error.hint:
                error.args = (f"{error.args[0][:-1]}: {error.hint}.",)
            raise error
        return reply

    async def close(self) -> None:
        """Close the TLS session (idempotent)."""
        self._ready = False
        self._fail_pending(
            PairingLinkClosedError("TLS session interrupted.", stage=PairingStage.TLS)
        )
        writer, task = self._writer, self._read_task
        self._reader = self._writer = self._read_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


class PairingStep(enum.StrEnum):
    """Progress of a :class:`PairingFlow`."""

    IDLE = "idle"
    AWAITING_TRUST = "awaiting_trust"
    CONNECTED = "connected"
    CODE_REQUESTED = "code_requested"
    AUTHENTICATED = "authenticated"
    CLOUD_REGISTERED = "cloud_registered"
    COMPLETED = "completed"


def _alternative_name(base: str, attempt: int) -> str:
    alphabet = string.digits + string.ascii_lowercase
    suffix = "".join(secrets.choice(alphabet) for _ in range(2))
    return f"{base[:9]}-{suffix}{attempt}"[:MAX_TERMINAL_NAME]


def _cloud_error(err: AiphoneJoError) -> PairingCloudError:
    """Map a /registClient failure to a typed pairing error (error code 71001)."""
    if isinstance(err, CloudApiError) and err.kind == "http" and err.status is not None:
        reason = pairing_reason(APP_CODE_REGIST_CLIENT, err.status)
        if reason is PairingFailureReason.UNKNOWN_RESULT:
            reason = PairingFailureReason.CLOUD_HTTP_ERROR
        return PairingCloudError(
            str(err),
            status=err.status,
            reason=reason,
            mid=APP_CODE_REGIST_CLIENT,
            rslt=err.status,
            retry_same_connection=err.status >= 500,
        )
    if isinstance(err, CloudApiError) and err.kind == "network":
        return PairingCloudError(
            str(err),
            kind="network",
            reason=PairingFailureReason.CLOUD_NETWORK_ERROR,
            retry_same_connection=True,
        )
    return PairingCloudError(
        str(err), kind="invalid_response", reason=PairingFailureReason.CLOUD_INVALID_RESPONSE
    )


class PairingFlow:
    """Step-by-step pairing, designed for a Home Assistant config flow.

    The TLS connection stays open between steps::

        flow = PairingFlow(session)
        peer = await flow.connect(monitor)            # nothing sent yet
        flow.approve(peer.fingerprint)                # user confirmed the fingerprint
        await flow.request_code()                     # 01002: the monitor shows a code
        await flow.authenticate(code)                 # 01003 with the displayed code
        await flow.register_cloud()                   # /registClient with the 11002 OTP
        credentials = await flow.register_terminal("Home")  # 01005
        await flow.close()

    Failures raise :class:`PairingError` subclasses. When
    ``err.retry_same_connection`` is true (wrong code 11003/2, name too long or
    already used 11005/2-3, cloud network errors), the flow stays on the same
    step and the call can be repeated. Otherwise the connection is closed;
    :meth:`reconnect` opens a new one with the already approved
    fingerprint (the monitor must still be in "add smartphone" mode).
    :meth:`cancel` also deletes a cloud registration that has no terminal yet.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        api_origin: str = DEFAULT_API_ORIGIN,
        link: LocalLink | None = None,
    ) -> None:
        self._session = session
        self._api_origin = api_origin
        self._link = link or LocalLink()
        self._link.on_disconnect = self._on_disconnect
        self._monitor: DiscoveredMonitor | None = None
        self._approved: str | None = None
        self._otp: str | None = None
        self._credentials: CloudCredentials | None = None
        self.step = PairingStep.IDLE
        self.peer: PeerCertificate | None = None

    async def __aenter__(self) -> PairingFlow:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def connected(self) -> bool:
        """``True`` while the pairing connection is open."""
        return self._link.connected

    @property
    def approved_fingerprint(self) -> str | None:
        """Fingerprint approved for this monitor (store it to re-pair without asking)."""
        return self._approved

    @property
    def monitor_id(self) -> str | None:
        """Stable, non-secret monitor identifier, known after :meth:`register_cloud`.

        See :attr:`CloudCredentials.monitor_id`.
        """
        return self._credentials.monitor_id if self._credentials else None

    def _on_disconnect(self) -> None:
        self._otp = None

    def _require(self, stage: PairingStage, *steps: PairingStep) -> None:
        if self.step not in steps and self._monitor is not None and not self._link.connected:
            raise PairingLinkClosedError(
                "The pairing connection is closed: call reconnect().", stage=stage
            )
        if self.step not in steps:
            raise PairingError(
                f"Invalid pairing step: {self.step} (expected {', '.join(steps)}).",
                stage=stage,
                reason=PairingFailureReason.INVALID_STATE,
            )
        if self.step is not PairingStep.AUTHENTICATED and not self._link.connected:
            raise PairingLinkClosedError(
                "The pairing connection is closed: call reconnect().", stage=stage
            )

    async def _fail(self) -> None:
        self._otp = None
        self.step = PairingStep.IDLE
        await self._link.close()

    async def _exchange(
        self,
        stage: PairingStage,
        mid: str,
        payload: dict[str, Any] | None,
        reply_mid: str,
        *,
        timeout: float = LOCAL_REQUEST_TIMEOUT,
    ) -> Message:
        try:
            return await self._link.request(mid, payload, reply_mid, timeout=timeout, stage=stage)
        except PairingError as err:
            if not err.retry_same_connection:
                await self._fail()
            raise

    async def connect(
        self, monitor: DiscoveredMonitor, *, expected_fingerprint: str | None = None
    ) -> PeerCertificate:
        """Open the TLS connection; approve now if ``expected_fingerprint`` matches.

        Raises :class:`IncompatibleVersionError` (error code 70002, RSLT 1) for an
        unsupported monitor system type and :class:`PairingRejectedError` for a
        monitor that refused the discovery (11001).
        """
        if monitor.sys_type is not None and monitor.sys_type not in SUPPORTED_SYS_TYPES:
            raise IncompatibleVersionError(
                f"Unsupported monitor system type {monitor.sys_type}: only the JO-1MDW "
                "(SYS 4) is supported.",
                mid=APP_CODE_INCOMPATIBLE,
                rslt=1,
            )
        if not monitor.accepted:
            raise PairingRejectedError(
                f"The monitor refused the discovery (RSLT={monitor.rslt}).",
                stage=PairingStage.DISCOVERY,
                reason=pairing_reason(Mid.DISCOVERY_REP, monitor.rslt),
                mid=Mid.DISCOVERY_REP,
                rslt=monitor.rslt,
            )
        await self._abandon_registration()
        self._monitor = monitor
        self._otp = None
        self.step = PairingStep.IDLE
        approved = False

        def decide(peer: PeerCertificate) -> bool:
            nonlocal approved
            self.peer = peer
            approved = expected_fingerprint is not None and peer.matches(expected_fingerprint)
            # Without a pinned fingerprint, keep the connection and wait for approve().
            return approved or expected_fingerprint is None

        self.peer = await self._link.connect(monitor.address, monitor.port, decide=decide)
        if approved:
            self._approved = self.peer.fingerprint
            self.step = PairingStep.CONNECTED
        else:
            self.step = PairingStep.AWAITING_TRUST
        return self.peer

    async def reconnect(self) -> PeerCertificate:
        """Open a new connection to the same monitor with the approved fingerprint.

        Use it after the monitor closed the link or after a non-retryable
        error; the flow restarts at :meth:`request_code`. A cloud registration
        without terminal is deleted first (best effort).
        """
        if self._monitor is None or self._approved is None:
            raise PairingError(
                "Nothing to reconnect to: call connect() and approve() first.",
                stage=PairingStage.TLS,
                reason=PairingFailureReason.INVALID_STATE,
            )
        return await self.connect(self._monitor, expected_fingerprint=self._approved)

    def approve(self, fingerprint: str) -> None:
        """Explicitly trust the presented certificate (must match its fingerprint)."""
        self._require(PairingStage.TRUST, PairingStep.AWAITING_TRUST)
        if self.peer is None or not self.peer.matches(fingerprint):
            raise UntrustedPeerError(
                "The observed certificate differs from the approved fingerprint.", peer=self.peer
            )
        self._approved = self.peer.fingerprint
        self.step = PairingStep.CONNECTED

    async def request_code(self) -> None:
        """01002 -> 11002: the monitor displays a code and returns an OTP (kept private)."""
        self._require(PairingStage.CODE_REQUEST, PairingStep.CONNECTED, PairingStep.CODE_REQUESTED)
        reply = await self._exchange(PairingStage.CODE_REQUEST, Mid.OTP_REQ, None, Mid.OTP_REP)
        try:
            self._otp = require_text(body(reply).get("OTPASS"), "received OTPASS", 128)
        except AiphoneJoError as err:
            await self._fail()
            raise PairingError(
                "11002 without a usable OTPASS.",
                stage=PairingStage.CODE_REQUEST,
                reason=PairingFailureReason.UNKNOWN_RESULT,
                mid=Mid.OTP_REP,
            ) from err
        self.step = PairingStep.CODE_REQUESTED

    async def authenticate(self, code: str) -> None:
        """01003 ``{OTPASS: code}`` -> 11003, with the code shown on the monitor.

        On a wrong code (11003 RSLT 2) the step can be retried with another code.
        """
        self._require(PairingStage.AUTHENTICATION, PairingStep.CODE_REQUESTED)
        value = require_text(code, "code displayed by the monitor", 128)
        await self._exchange(
            PairingStage.AUTHENTICATION, Mid.AUTH_REQ, {"OTPASS": value}, Mid.AUTH_REP
        )
        self.step = PairingStep.AUTHENTICATED

    async def register_cloud(self) -> CloudCredentials:
        """``/registClient`` with the 11002 OTP and the 11001 ``SYSVER``.

        Failures raise :class:`PairingCloudError` (``mid="71001"`` and
        ``rslt`` = HTTP status when the cloud answered).
        """
        self._require(PairingStage.CLOUD_REGISTRATION, PairingStep.AUTHENTICATED)
        if self._otp is None or self._monitor is None:
            raise PairingError(
                "Local authentication is required before /registClient.",
                stage=PairingStage.CLOUD_REGISTRATION,
                reason=PairingFailureReason.INVALID_STATE,
            )
        try:
            self._credentials = await regist_client(
                self._session,
                otp=self._otp,
                origin=self._api_origin,
                system_version=self._monitor.version,
                sys_type=self._monitor.sys_type or DEFAULT_SYS_TYPE,
            )
        except (CloudApiError, InvalidCredentialsError) as err:
            error = _cloud_error(err)
            if not error.retry_same_connection:
                await self._fail()
            raise error from err
        self.step = PairingStep.CLOUD_REGISTERED
        return self._credentials

    async def register_terminal(self, name: str, *, auto_rename: bool = True) -> CloudCredentials:
        """01005 ``{CLIID, TERMNM, TERMTPC}`` -> 11005.

        With ``auto_rename`` (default), a name already in use (RSLT 3) is
        retried up to three times with a random suffix; otherwise the error is
        raised and :meth:`register_terminal` can be called again with another
        name on the same connection. Returns the final credentials (with the
        accepted terminal name), to be stored by the caller.
        """
        self._require(PairingStage.TERMINAL_REGISTRATION, PairingStep.CLOUD_REGISTERED)
        if self._credentials is None:
            raise PairingError(
                "Cloud registration is required before the terminal.",
                stage=PairingStage.TERMINAL_REGISTRATION,
                reason=PairingFailureReason.INVALID_STATE,
            )
        base = require_text(name, "terminal name", MAX_TERMINAL_NAME)
        candidate = base
        attempts = _MAX_RENAME_ATTEMPTS if auto_rename else 0
        for attempt in range(attempts + 1):
            try:
                await self._exchange(
                    PairingStage.TERMINAL_REGISTRATION,
                    Mid.TERMINAL_REQ,
                    {
                        "CLIID": self._credentials.client_id,
                        "TERMNM": candidate,
                        "TERMTPC": self._credentials.recv_topic,
                    },
                    Mid.TERMINAL_REP,
                    timeout=TERMINAL_REGISTER_TIMEOUT,
                )
                break
            except PairingRejectedError as err:
                if err.rslt != _NAME_TAKEN or attempt >= attempts:
                    raise
                candidate = _alternative_name(base, attempt)
                _LOGGER.warning("Terminal name already used, retrying with %s", candidate)
        self._credentials = self._credentials.with_terminal_name(candidate)
        self.step = PairingStep.COMPLETED
        return self._credentials

    async def _abandon_registration(self) -> bool:
        credentials = self._credentials
        self._credentials = None
        if credentials is None or self.step is PairingStep.COMPLETED:
            return False
        try:
            return await unregister(self._session, credentials)
        except AiphoneJoError as err:
            _LOGGER.warning("Could not delete the unfinished cloud registration: %s", err)
            return False

    async def cancel(self) -> bool:
        """Abort the pairing: delete an unfinished cloud registration, then close.

        Returns ``True`` when a registration was deleted (see :func:`unregister`).
        """
        removed = await self._abandon_registration()
        await self.close()
        return removed

    async def close(self) -> None:
        """Close the local TLS connection and forget the OTP (keeps the result)."""
        self._otp = None
        await self._link.close()
