"""Cloud identity (AWS IoT) obtained from ``/registClient`` and its validation.

The structure is serializable (:meth:`CloudCredentials.to_storage`) so it can be
stored as-is in a Home Assistant config entry.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import ssl
import tempfile
import warnings
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from ._util import require_text
from .const import (
    ALLOWED_MQTT_PORTS,
    AWS_IOT_ALPN,
    DEFAULT_API_ORIGIN,
    DEFAULT_MQTT_PORT,
    DEFAULT_SYS_TYPE,
)
from .exceptions import CredentialsExpiredError, InvalidCredentialsError, InvalidInputError

_MQTT_HOST = re.compile(
    r"^(?:[a-z0-9-]+\.iot\.[a-z0-9-]+\.amazonaws\.com|[a-z0-9.-]+\.aiphone-app\.net)$",
    re.IGNORECASE,
)
_MAX_PEM = 32768


def broadcast_topic_of(topic: str | None, client_id: str | None) -> str | None:
    """Monitor-wide topic: ``recv_topic`` without its ``/<clientId>`` suffix.

    A terminal receives messages addressed to it on ``recv_topic`` and
    messages broadcast to every terminal of the monitor on this shared topic,
    where the 23001 ring is expected.
    """
    if not topic or not client_id:
        return None
    suffix = f"/{client_id}"
    if topic.endswith(suffix) and len(topic) > len(suffix):
        return topic[: -len(suffix)]
    return None


def _pem(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) > _MAX_PEM or "-----BEGIN " not in value:
        raise InvalidCredentialsError(f"Missing or invalid {name} PEM.")
    return value


def _text(value: object, name: str, max_length: int = 256) -> str:
    try:
        return require_text(value, name, max_length)
    except InvalidInputError as err:
        raise InvalidCredentialsError(str(err)) from err


def _check_identity(
    cert_pem: str, key_pem: str, root_ca_pem: str | None, *, now: datetime
) -> datetime:
    """Check that key and certificate match and are valid; return the expiry date."""
    try:
        certificate = x509.load_pem_x509_certificate(cert_pem.encode())
        key = serialization.load_pem_private_key(key_pem.encode(), password=None)
        spki = serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        if certificate.public_key().public_bytes(*spki) != key.public_key().public_bytes(*spki):
            raise InvalidCredentialsError(
                "Certificate/key: the key does not match the certificate."
            )
        if root_ca_pem:
            x509.load_pem_x509_certificate(root_ca_pem.encode())
    except InvalidCredentialsError:
        raise
    except (ValueError, TypeError) as err:
        raise InvalidCredentialsError(f"Certificate/key: {err}") from err
    expires = certificate.not_valid_after_utc
    if expires <= now:
        raise CredentialsExpiredError("Certificate/key: the client certificate is expired.")
    if certificate.not_valid_before_utc > now:
        raise InvalidCredentialsError("Certificate/key: the client certificate is not yet valid.")
    return expires


def certificate_expiry(cert_pem: str) -> datetime:
    """``notAfter`` (UTC) of a PEM certificate."""
    try:
        return x509.load_pem_x509_certificate(cert_pem.encode()).not_valid_after_utc
    except ValueError as err:
        raise InvalidCredentialsError(f"Certificate: {err}") from err


STORAGE_SCHEMA: Final = 1

# Storage key (schema >= 1) -> /registClient-style key understood by parse().
_STORAGE_TO_REGISTRATION = {
    "host": "server_url",
    "client_id": "client_id",
    "recv_topic": "recv_topic",
    "send_topic": "send_topic",
    "cert_pem": "client_certificate",
    "key_pem": "secret_key",
    "root_ca_pem": "root_ca",
    "port": "port",
    "alpn": "alpn",
    "api_origin": "api_origin",
    "term_name": "term_name",
    "sys_type": "sys_type",
    "system_version": "system_version",
}


def _to_registration_layout(data: dict[str, Any]) -> dict[str, Any]:
    return {target: data.get(source) for source, target in _STORAGE_TO_REGISTRATION.items()}


def _migrate_storage(data: dict[str, Any], schema: int) -> dict[str, Any]:
    """Bring stored data to the layout read by :meth:`CloudCredentials.parse`."""
    if schema == 0:
        # 0.1.0 as_dict(): already the /registClient layout.
        return {key: value for key, value in data.items() if value is not None}
    layout = _to_registration_layout(data)
    return {key: value for key, value in layout.items() if value is not None}


@dataclass(frozen=True, slots=True)
class CloudCredentials:
    """MQTT identity of a paired terminal (virtual smartphone).

    Holds secrets (``key_pem``) and identifiers (endpoint, client id, topics):
    they are excluded from ``repr``. Persist it with :meth:`to_storage` and
    restore it with :meth:`from_storage`; never log the stored data.
    """

    host: str = field(repr=False)
    client_id: str = field(repr=False)
    recv_topic: str = field(repr=False)
    cert_pem: str = field(repr=False)
    key_pem: str = field(repr=False)
    send_topic: str | None = field(default=None, repr=False)
    root_ca_pem: str | None = field(default=None, repr=False)
    port: int = DEFAULT_MQTT_PORT
    alpn: str | None = None
    expires: datetime | None = None
    api_origin: str = DEFAULT_API_ORIGIN
    term_name: str | None = None
    sys_type: int = DEFAULT_SYS_TYPE
    system_version: str | None = None

    @classmethod
    def parse(
        cls,
        data: dict[str, Any],
        *,
        check_validity: bool = True,
        now: datetime | None = None,
    ) -> CloudCredentials:
        """Validate parameters in ``/registClient`` format (``server_url``, ``client_id``...).

        Rules: AWS IoT or aiphone-app.net host, topic without MQTT wildcard,
        key matching the certificate, certificate currently valid, port 443 or
        8883, AWS ALPN only. Unknown keys are ignored. An expired certificate raises
        :class:`CredentialsExpiredError` when ``check_validity`` is true.
        """
        if not isinstance(data, dict):
            raise InvalidCredentialsError("A parameter object is required.")
        host = _text(data.get("server_url"), "server_url")
        if not _MQTT_HOST.match(host):
            raise InvalidCredentialsError(
                "server_url must be an AWS IoT or aiphone-app.net DNS name, without scheme or path."
            )
        client_id = _text(data.get("client_id"), "client_id", 128)
        topic = _text(data.get("recv_topic"), "recv_topic", 512)
        if "+" in topic or "#" in topic:
            raise InvalidCredentialsError(
                "MQTT wildcard subscriptions are forbidden: "
                "use the exact topic assigned to the client."
            )
        send_topic_raw = data.get("send_topic")
        send_topic = _text(send_topic_raw, "send_topic", 512) if send_topic_raw else None
        if send_topic and ("+" in send_topic or "#" in send_topic):
            raise InvalidCredentialsError("send_topic must not contain an MQTT wildcard.")
        cert_pem = _pem(data.get("client_certificate"), "client_certificate")
        key_pem = _pem(data.get("secret_key"), "secret_key")
        root_ca = data.get("root_ca") or None
        root_ca_pem = _pem(root_ca, "root_ca") if root_ca else None
        moment = now or datetime.now(UTC)
        if check_validity:
            expires = _check_identity(cert_pem, key_pem, root_ca_pem, now=moment)
        else:
            expires = certificate_expiry(cert_pem)
        port = data.get("port", DEFAULT_MQTT_PORT)
        if port not in ALLOWED_MQTT_PORTS or isinstance(port, bool):
            raise InvalidCredentialsError("Allowed MQTT ports: 443 or 8883.")
        alpn = data.get("alpn") or None
        if alpn and alpn != AWS_IOT_ALPN:
            raise InvalidCredentialsError("Unsupported ALPN.")
        term_name = data.get("term_name") or None
        sys_type = data.get("sys_type", DEFAULT_SYS_TYPE)
        if isinstance(sys_type, str) and sys_type.isdigit():
            sys_type = int(sys_type)
        if not isinstance(sys_type, int) or isinstance(sys_type, bool):
            raise InvalidCredentialsError("Invalid sys_type.")
        return cls(
            host=host,
            client_id=client_id,
            recv_topic=topic,
            send_topic=send_topic,
            cert_pem=cert_pem,
            key_pem=key_pem,
            root_ca_pem=root_ca_pem,
            port=port,
            alpn=alpn,
            expires=expires,
            api_origin=_text(data.get("api_origin") or DEFAULT_API_ORIGIN, "api_origin", 253),
            term_name=_text(term_name, "term_name", 12) if term_name else None,
            sys_type=sys_type,
            system_version=data.get("system_version") or None,
        )

    @property
    def broadcast_topic(self) -> str | None:
        """Monitor-wide topic (ring), see :func:`broadcast_topic_of`."""
        return broadcast_topic_of(self.recv_topic, self.client_id)

    def with_terminal_name(self, name: str) -> CloudCredentials:
        """Copy with the terminal name accepted by the monitor (01005)."""
        return replace(self, term_name=_text(name, "term_name", 12))

    @property
    def monitor_id(self) -> str | None:
        """Stable, non-secret identifier of the paired monitor (for example a Home Assistant
        ``unique_id``).

        It is derived from the monitor-wide topic, which is built from the
        monitor's MAC address and identical for every terminal paired with that
        monitor, so it survives re-pairing. Only a truncated SHA-256 digest is
        exposed, never the topic or the MAC. MAC addresses have little entropy,
        so treat the value as an identifier, not as a secret. ``None`` when
        the topic does not have the expected ``.../<client id>`` form.
        """
        topic = self.broadcast_topic
        if topic is None:
            return None
        digest = hashlib.sha256(f"aiphone-jo/monitor-id/v1:{topic}".encode()).hexdigest()
        return f"jo-{digest[:24]}"

    def is_expired(self, now: datetime | None = None) -> bool:
        """``True`` when the client certificate is expired at ``now`` (default: now).

        Client certificates are typically valid for about 50 years; to renew
        one, pair again.
        """
        expires = self.expires or certificate_expiry(self.cert_pem)
        return expires <= (now or datetime.now(UTC))

    def to_storage(self) -> dict[str, Any]:
        """JSON-compatible dict for long-term storage (reversible with :meth:`from_storage`).

        Contains the private key: store it like a password.
        """
        expires = self.expires or certificate_expiry(self.cert_pem)
        return {
            "schema": STORAGE_SCHEMA,
            "host": self.host,
            "port": self.port,
            "client_id": self.client_id,
            "recv_topic": self.recv_topic,
            "send_topic": self.send_topic,
            "cert_pem": self.cert_pem,
            "key_pem": self.key_pem,
            "root_ca_pem": self.root_ca_pem,
            "alpn": self.alpn,
            "api_origin": self.api_origin,
            "term_name": self.term_name,
            "sys_type": self.sys_type,
            "system_version": self.system_version,
            "expires": expires.isoformat(),
        }

    @classmethod
    def from_storage(
        cls,
        data: dict[str, Any],
        *,
        check_validity: bool = True,
        now: datetime | None = None,
    ) -> CloudCredentials:
        """Restore credentials written by :meth:`to_storage` (any schema up to the current one).

        Data without ``schema`` (written by 0.1.0 ``as_dict()``, i.e. the
        ``/registClient`` field names) is migrated. Unknown keys are ignored.
        The expiry date is always re-read from the certificate. Raises
        :class:`CredentialsExpiredError` for an expired certificate when
        ``check_validity`` is true, and :class:`InvalidCredentialsError` for
        data written by a newer, unknown schema.
        """
        if not isinstance(data, dict):
            raise InvalidCredentialsError("A stored credentials object is required.")
        schema = data.get("schema", 0)
        if not isinstance(schema, int) or isinstance(schema, bool) or schema < 0:
            raise InvalidCredentialsError("Invalid credentials schema.")
        if schema > STORAGE_SCHEMA:
            raise InvalidCredentialsError(
                f"Credentials schema {schema} is newer than supported ({STORAGE_SCHEMA}): "
                "upgrade aiphone-jo."
            )
        legacy = _migrate_storage(data, schema)
        return cls.parse(legacy, check_validity=check_validity, now=now)

    def as_dict(self) -> dict[str, Any]:
        """Deprecated since 0.2.0: use :meth:`to_storage`. Returns the ``/registClient`` layout."""
        warnings.warn(
            "CloudCredentials.as_dict() is deprecated, use to_storage().",
            DeprecationWarning,
            stacklevel=2,
        )
        return _to_registration_layout(self.to_storage())

    def create_ssl_context(self) -> ssl.SSLContext:
        """Mutual TLS context for AWS IoT (blocking: uses temporary files).

        ``ssl`` only loads the client chain from files: they are created in a
        private directory (0700/0600) and deleted immediately. Inside an
        asyncio loop, prefer :meth:`async_create_ssl_context`.
        """
        context = ssl.create_default_context(cadata=self.root_ca_pem or None)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        with tempfile.TemporaryDirectory(prefix="aiphone-jo-") as directory:
            os.chmod(directory, 0o700)
            cert_path = Path(directory, "client.crt")
            key_path = Path(directory, "client.key")
            for path, content in ((cert_path, self.cert_pem), (key_path, self.key_pem)):
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                    handle.write(content)
            context.load_cert_chain(cert_path, key_path)
        if self.alpn:
            context.set_alpn_protocols([self.alpn])
        return context

    async def async_create_ssl_context(self) -> ssl.SSLContext:
        """Non-blocking version of :meth:`create_ssl_context` (default executor)."""
        return await asyncio.get_running_loop().run_in_executor(None, self.create_ssl_context)
