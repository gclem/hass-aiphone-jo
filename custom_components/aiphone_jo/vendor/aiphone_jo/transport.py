"""MQTT transport (AWS IoT, mutual TLS) behind a small replaceable interface.

MQTT 3.1.1, clean session,
keepalive 30 s, one SUBSCRIBE (QoS 0) to ``recv_topic`` and to the monitor-wide
topic. A denied ``recv_topic`` raises :class:`CloudAuthError` (like a CONNACK
"not authorized" or a TLS alert rejecting the client certificate); a denied
monitor-wide topic is only reported (rings may then be missed).

:class:`MqttConnection` lets tests (and alternative stacks) replace aiomqtt.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .const import MQTT_CONNECT_TIMEOUT, MQTT_KEEPALIVE
from .exceptions import CloudAuthError, CloudConnectionError

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MqttConnectOptions:
    """Parameters of an MQTT connection."""

    host: str
    port: int
    client_id: str
    topics: Sequence[str]
    ssl_context: ssl.SSLContext | None
    keepalive: int = MQTT_KEEPALIVE
    timeout: float = MQTT_CONNECT_TIMEOUT


class MqttConnection(Protocol):
    """Connected and subscribed MQTT session."""

    @property
    def granted(self) -> Mapping[str, bool]:
        """Subscription result per topic (``True`` = accepted)."""

    async def publish(self, topic: str, payload: bytes) -> None:
        """Publish with QoS 0."""

    def messages(self) -> AsyncIterator[tuple[str, bytes]]:
        """Received ``(topic, payload)``; ends or raises when the connection drops."""

    async def close(self) -> None:
        """Disconnect (idempotent)."""


TransportFactory = Callable[[MqttConnectOptions], Awaitable[MqttConnection]]


# CONNACK return codes meaning "identity refused": MQTT 3.1.1 4 (bad user name
# or password) and 5 (not authorized); MQTT 5 0x86, 0x87, 0x8A (banned), 0x8C.
_AUTH_CONNACK_CODES = frozenset({4, 5, 0x86, 0x87, 0x8A, 0x8C})
# TLS alerts sent by a server that rejects the client certificate. A failure to
# verify the *server* (CERTIFICATE_VERIFY_FAILED) is not an identity problem.
_AUTH_TLS_ALERTS = (
    "ALERT_BAD_CERTIFICATE",
    "ALERT_CERTIFICATE_REVOKED",
    "ALERT_CERTIFICATE_EXPIRED",
    "ALERT_CERTIFICATE_UNKNOWN",
    "ALERT_CERTIFICATE_REQUIRED",
    "ALERT_UNSUPPORTED_CERTIFICATE",
    "ALERT_UNKNOWN_CA",
    "ALERT_ACCESS_DENIED",
)


def is_auth_failure(err: BaseException) -> bool:
    """``True`` when a connection error means the broker refused this identity."""
    code = getattr(err, "rc", None)
    value = code if isinstance(code, int) else getattr(code, "value", None)
    if isinstance(value, int) and value in _AUTH_CONNACK_CODES:
        return True
    text = str(err).upper()
    return any(alert in text for alert in _AUTH_TLS_ALERTS)


def _accepted(code: Any) -> bool:
    if isinstance(code, int):
        return code < 0x80
    if getattr(code, "is_failure", False):
        return False
    value = getattr(code, "value", 0)
    return isinstance(value, int) and value < 0x80


class AiomqttConnection:
    """:class:`MqttConnection` implemented with ``aiomqtt``."""

    def __init__(self, client: Any, granted: dict[str, bool]) -> None:
        self._client = client
        self._granted = granted
        self._closed = False

    @property
    def granted(self) -> Mapping[str, bool]:
        return self._granted

    async def publish(self, topic: str, payload: bytes) -> None:
        import aiomqtt  # noqa: PLC0415

        try:
            await self._client.publish(topic, payload, qos=0)
        except aiomqtt.MqttError as err:
            raise CloudConnectionError(f"MQTT publish: {err}") from err

    async def messages(self) -> AsyncIterator[tuple[str, bytes]]:
        import aiomqtt  # noqa: PLC0415

        try:
            async for message in self._client.messages:
                payload = message.payload
                if isinstance(payload, str):
                    payload = payload.encode()
                elif not isinstance(payload, (bytes, bytearray)):
                    continue
                yield str(message.topic), bytes(payload)
        except aiomqtt.MqttError as err:
            if not self._closed:
                raise CloudConnectionError(f"MQTT connection lost: {err}") from err

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await self._client.__aexit__(None, None, None)


async def aiomqtt_connect(options: MqttConnectOptions) -> MqttConnection:
    """Connect with aiomqtt, subscribe, and return the live connection."""
    import aiomqtt  # noqa: PLC0415

    client = aiomqtt.Client(
        options.host,
        options.port,
        identifier=options.client_id,
        protocol=aiomqtt.ProtocolVersion.V311,
        clean_session=True,
        keepalive=options.keepalive,
        timeout=options.timeout,
        tls_context=options.ssl_context,
    )
    try:
        async with asyncio.timeout(options.timeout):
            await client.__aenter__()
    except (aiomqtt.MqttError, OSError, TimeoutError) as err:
        if is_auth_failure(err):
            raise CloudAuthError(f"MQTT: the broker refused this identity ({err}).") from err
        raise CloudConnectionError(f"MQTT: {err or type(err).__name__}") from err
    connection = AiomqttConnection(client, {})
    try:
        async with asyncio.timeout(options.timeout):
            codes = await client.subscribe([(topic, 0) for topic in options.topics])
    except (aiomqtt.MqttError, TimeoutError) as err:
        await connection.close()
        raise CloudConnectionError(f"MQTT subscription failed: {err or 'timeout'}") from err
    granted = {topic: _accepted(code) for topic, code in zip(options.topics, codes, strict=False)}
    connection._granted = granted
    if not granted.get(options.topics[0]):
        await connection.close()
        raise CloudAuthError(
            "The broker refused the subscription to this client's own topic: "
            "the terminal is no longer authorized."
        )
    return connection
