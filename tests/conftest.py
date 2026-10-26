"""Shared test fixtures (fictitious data only)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from homeassistant.const import CONF_ADDRESS, CONF_PORT
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aiphone_jo.api import credentials_to_storage
from custom_components.aiphone_jo.const import (
    CONF_CREDENTIALS,
    CONF_FINGERPRINT,
    CONF_MONITOR,
    CONF_STATIONS,
    CONF_SYSTEM_VERSION,
    DEFAULT_TITLE,
    DOMAIN,
)
from custom_components.aiphone_jo.vendor.aiphone_jo import (
    AiphoneEvent,
    CloudCredentials,
    DiscoveredMonitor,
    PairingRejectedError,
    PairingStep,
    PeerCertificate,
    RoomInfo,
    Terminal,
    UntrustedPeerError,
    pairing_reason,
)

from .fake_go2rtc import JANUS_OFFER, FakeGo2Rtc

FINGERPRINT = ":".join(["AB"] * 32)
FINGERPRINT_ID = "ab" * 32
OTHER_FINGERPRINT = ":".join(["CD"] * 32)
MONITOR = DiscoveredMonitor(address="192.0.2.10", port=50000, system=4, version="1.2")
OTHER_MONITOR = DiscoveredMonitor(
    address="192.0.2.11", port=50000, system=4, version="1.2"
)
VALID_CODE = "1234"
FRONT = Terminal(id=101, term_kind=5, name="Front", camera=True, ent_key_type=None)
BACK = Terminal(id=102, term_kind=5, name="Back", camera=True, ent_key_type=None)
ROOM_INFO = RoomInfo(terminals=(FRONT, BACK), term_inf_num=2)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow custom integrations to be loaded."""
    return


def make_credentials(
    term_name: str | None = "HomeAssist",
    *,
    monitor: str = "example-monitor",
    expired: bool = False,
) -> CloudCredentials:
    """Credentials with a runtime-generated self-signed certificate."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "example.invalid")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=2))
        .not_valid_after(now + timedelta(days=-1 if expired else 365))
        .sign(key, hashes.SHA256())
    )
    return CloudCredentials.parse(
        {
            "server_url": "example.iot.example-1.amazonaws.com",
            "client_id": "example-client",
            "recv_topic": f"example/{monitor}/example-client",
            "send_topic": "example/send",
            "client_certificate": certificate.public_bytes(
                serialization.Encoding.PEM
            ).decode(),
            "secret_key": key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode(),
            "term_name": term_name,
            "system_version": "1.2",
        },
        check_validity=not expired,
    )


@pytest.fixture(scope="session")
def credentials() -> CloudCredentials:
    """Fictitious credentials."""
    return make_credentials()


def entry_data(credentials: CloudCredentials) -> dict[str, Any]:
    """Config entry data as stored by the config flow."""
    return {
        CONF_CREDENTIALS: credentials_to_storage(credentials),
        CONF_MONITOR: {
            CONF_FINGERPRINT: FINGERPRINT_ID,
            CONF_ADDRESS: MONITOR.address,
            CONF_PORT: MONITOR.port,
            CONF_SYSTEM_VERSION: "1.2",
        },
        CONF_STATIONS: [
            {"id": 101, "kind": 5, "name": "Front", "camera": True},
            {"id": 102, "kind": 5, "name": "Back", "camera": True},
        ],
    }


@pytest.fixture
def config_entry(hass: HomeAssistant, credentials: CloudCredentials) -> MockConfigEntry:
    """A paired monitor."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=DEFAULT_TITLE,
        unique_id=credentials.monitor_id,
        data=entry_data(credentials),
        version=1,
        minor_version=2,
    )
    entry.add_to_hass(hass)
    return entry


def peer(fingerprint: str = FINGERPRINT) -> PeerCertificate:
    """Monitor certificate."""
    return PeerCertificate(
        fingerprint=fingerprint, not_after=datetime(2036, 1, 1, tzinfo=UTC)
    )


def rejected(mid: str, rslt: int, *, retry: bool = False) -> PairingRejectedError:
    """Monitor refusal as raised by the library."""
    return PairingRejectedError(
        "rejected",
        reason=pairing_reason(mid, rslt),
        mid=mid,
        rslt=rslt,
        retry_same_connection=retry,
    )


@dataclass
class FakePairing:
    """Stand-in for PairingFlow (0.2.0 semantics) with scriptable failures."""

    credentials: CloudCredentials
    peer: PeerCertificate = field(default_factory=peer)
    step: PairingStep = PairingStep.IDLE
    connected: bool = False
    approved_fingerprint: str | None = None
    monitor_id: str | None = None
    errors: dict[str, list[BaseException]] = field(default_factory=dict)
    taken_names: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)
    auto_rename: list[bool] = field(default_factory=list)
    closed: bool = False

    def fail(self, method: str, *errors: BaseException) -> None:
        """Make the next calls of ``method`` raise ``errors`` in order."""
        self.errors.setdefault(method, []).extend(errors)

    def _raise(self, error: BaseException) -> None:
        if not getattr(error, "retry_same_connection", False):
            self.step = PairingStep.IDLE
            self.connected = False
        raise error

    def _maybe_fail(self, method: str) -> None:
        self.calls.append(method)
        if self.errors.get(method):
            self._raise(self.errors[method].pop(0))

    async def connect(
        self, monitor: DiscoveredMonitor, *, expected_fingerprint: str | None = None
    ) -> PeerCertificate:
        self._maybe_fail("connect")
        self.closed = False
        if expected_fingerprint is not None:
            if not self.peer.matches(expected_fingerprint):
                raise UntrustedPeerError("untrusted", peer=self.peer)
            self.approved_fingerprint = self.peer.fingerprint
            self.step = PairingStep.CONNECTED
        else:
            self.step = PairingStep.AWAITING_TRUST
        self.connected = True
        return self.peer

    async def reconnect(self) -> PeerCertificate:
        self._maybe_fail("reconnect")
        assert self.approved_fingerprint is not None
        if not self.peer.matches(self.approved_fingerprint):
            raise UntrustedPeerError("untrusted", peer=self.peer)
        self.step = PairingStep.CONNECTED
        self.connected = True
        return self.peer

    def approve(self, fingerprint: str) -> None:
        self._maybe_fail("approve")
        if not self.connected:
            # As the library: the monitor closed the link before the approval.
            raise link_closed()
        self.approved_fingerprint = fingerprint
        self.step = PairingStep.CONNECTED

    async def request_code(self) -> None:
        self._maybe_fail("request_code")
        self.step = PairingStep.CODE_REQUESTED

    async def authenticate(self, code: str) -> None:
        self._maybe_fail("authenticate")
        if code != VALID_CODE:
            self._raise(rejected("11003", 2, retry=True))
        self.step = PairingStep.AUTHENTICATED

    async def register_cloud(self) -> CloudCredentials:
        self._maybe_fail("register_cloud")
        self.step = PairingStep.CLOUD_REGISTERED
        self.monitor_id = self.credentials.monitor_id
        return self.credentials

    async def register_terminal(
        self, name: str, *, auto_rename: bool = True
    ) -> CloudCredentials:
        self.auto_rename.append(auto_rename)
        self._maybe_fail("register_terminal")
        if name in self.taken_names:
            self._raise(rejected("11005", 3, retry=True))
        self.step = PairingStep.COMPLETED
        return self.credentials.with_terminal_name(name)

    async def cancel(self) -> bool:
        self.calls.append("cancel")
        self.closed = True
        self.connected = False
        return self.step is not PairingStep.COMPLETED

    async def close(self) -> None:
        self.calls.append("close")
        self.closed = True
        self.connected = False


@pytest.fixture
def pairing(credentials: CloudCredentials) -> Generator[FakePairing]:
    """Patched pairing flow, discovery and room info."""
    fake = FakePairing(credentials=credentials)
    with (
        patch(
            "custom_components.aiphone_jo.config_flow.PairingFlow",
            side_effect=lambda session: fake,
        ),
        patch(
            "custom_components.aiphone_jo.config_flow.async_discover",
            AsyncMock(return_value=[MONITOR]),
        ) as discover,
        patch(
            "custom_components.aiphone_jo.config_flow.get_room_info",
            AsyncMock(return_value=ROOM_INFO),
        ) as room_info,
        patch(
            "custom_components.aiphone_jo.config_flow.async_unregister",
            AsyncMock(return_value=True),
        ),
    ):
        fake.discover = discover  # type: ignore[attr-defined]
        fake.room_info = room_info  # type: ignore[attr-defined]
        yield fake


class FakeLive:
    """Stand-in for LiveMonitor: negotiates once, then runs until closed."""

    def __init__(
        self,
        client: FakeClient,
        negotiate: Callable[[str], Awaitable[str]],
        terminal: int | None,
        renew: bool,
    ) -> None:
        self.client = client
        self.negotiate = negotiate
        self.terminal = terminal
        self.renew = renew
        self.session: object | None = None
        self.answer: str | None = None
        self.exited = asyncio.Event()

    async def __aenter__(self) -> FakeLive:
        self.client.lives.append(self)
        if self.client.gate is not None:
            await self.client.gate.wait()
        if error := self.client.live_errors.get(self.terminal):
            raise error
        self.answer = await self.negotiate(JANUS_OFFER)
        self.session = object()
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.session = None
        self.exited.set()

    def end(self) -> None:
        """The entrance station ends the session."""
        self.session = None


class FakeClient:
    """Stand-in for AiphoneJoClient."""

    def __init__(self, credentials: CloudCredentials, **kwargs: Any) -> None:
        self.credentials = credentials
        self.kwargs = kwargs
        self.connected = False
        self.auth_failed = False
        self.broadcast_subscribed = True
        self.listeners: list[Callable[[AiphoneEvent], None]] = []
        self.connect = AsyncMock(side_effect=self._connect)
        self.disconnect = AsyncMock()
        self.open_door = AsyncMock()
        self.get_room_info = AsyncMock(return_value=ROOM_INFO)
        self.lives: list[FakeLive] = []
        self.live_errors: dict[int | None, BaseException] = {}
        self.gate: asyncio.Event | None = None
        self.live_monitor = self._live_monitor

    def _live_monitor(
        self,
        negotiate: Callable[[str], Awaitable[str]],
        terminal: int | None = None,
        *,
        on_session_end: Callable[[], Awaitable[None]] | None = None,
        renew: bool = True,
    ) -> FakeLive:
        return FakeLive(self, negotiate, terminal, renew)

    async def _connect(self) -> None:
        self.connected = True

    def add_listener(
        self, listener: Callable[[AiphoneEvent], None]
    ) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def emit(self, event: AiphoneEvent) -> None:
        for listener in list(self.listeners):
            listener(event)


@dataclass
class ClientHolder:
    """Access to the client created by the integration."""

    client: FakeClient | None = None
    setup: Callable[[FakeClient], None] | None = None


@pytest.fixture
def client_holder() -> Generator[ClientHolder]:
    """Patch the library client with a fake."""
    holder = ClientHolder()

    def factory(credentials: CloudCredentials, **kwargs: Any) -> FakeClient:
        client = FakeClient(credentials, **kwargs)
        if holder.setup is not None:
            holder.setup(client)
        holder.client = client
        return client

    with patch("custom_components.aiphone_jo.AiphoneJoClient", side_effect=factory):
        yield holder


@pytest.fixture
async def loaded_entry(
    hass: HomeAssistant, config_entry: MockConfigEntry, client_holder: ClientHolder
) -> MockConfigEntry:
    """A loaded config entry."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry


def link_closed() -> Exception:
    """Error raised when the monitor closes the pairing link."""
    from custom_components.aiphone_jo.vendor.aiphone_jo import PairingLinkClosedError

    return PairingLinkClosedError("The monitor closed the pairing connection.")


@pytest.fixture
async def go2rtc(
    hass: HomeAssistant, socket_enabled: None
) -> AsyncIterator[FakeGo2Rtc]:
    """Fake go2rtc server registered as Home Assistant's go2rtc."""
    from types import SimpleNamespace

    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    server = FakeGo2Rtc()
    url = await server.start()
    hass.data["go2rtc"] = SimpleNamespace(
        url=url, session=async_get_clientsession(hass)
    )
    yield server
    await server.stop()


async def until(predicate: Callable[[], bool], limit: float = 5) -> None:
    """Wait until ``predicate()`` is true."""
    async with asyncio.timeout(limit):
        while True:
            if predicate():
                return
            await asyncio.sleep(0.01)
