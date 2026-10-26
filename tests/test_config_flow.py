"""Tests for the pairing config flow."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aiphone_jo.const import (
    CONF_CREDENTIALS,
    CONF_FINGERPRINT,
    CONF_MONITOR,
    CONF_STATION,
    CONF_STATIONS,
    DOMAIN,
)
from custom_components.aiphone_jo.vendor.aiphone_jo import (
    CloudApiError,
    CloudCredentials,
    DiscoveredMonitor,
    DiscoveryError,
    IncompatibleVersionError,
    PairingCloudError,
    PairingError,
    PairingFailureReason,
    PairingTimeoutError,
    ProtocolError,
)

from .conftest import (
    FINGERPRINT_ID,
    MONITOR,
    OTHER_FINGERPRINT,
    OTHER_MONITOR,
    VALID_CODE,
    FakePairing,
    entry_data,
    link_closed,
    make_credentials,
    peer,
    rejected,
)


async def _start(hass: HomeAssistant) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def _configure(
    hass: HomeAssistant,
    result: dict[str, Any],
    user_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input or {}
    )


async def _to_code(hass: HomeAssistant) -> dict[str, Any]:
    result = await _configure(hass, await _start(hass), {"method": "discover"})
    assert result["step_id"] == "confirm_fingerprint"
    result = await _configure(hass, result, {"trust": True})
    assert result["step_id"] == "code"
    return result


async def _to_name(hass: HomeAssistant) -> dict[str, Any]:
    result = await _configure(hass, await _to_code(hass), {"code": VALID_CODE})
    assert result["step_id"] == "terminal_name"
    return result


async def _finish(hass: HomeAssistant, result: dict[str, Any]) -> dict[str, Any]:
    """From the resume step: reconnect, enter the code and the name."""
    result = await _configure(hass, result)
    assert result["step_id"] == "code"
    result = await _configure(hass, result, {"code": VALID_CODE})
    return await _configure(hass, result, {"terminal_name": "Hall"})


def _error(result: dict[str, Any]) -> str:
    assert result["type"] is FlowResultType.FORM
    return result["errors"]["base"]


async def test_full_pairing(
    hass: HomeAssistant, pairing: FakePairing, credentials: CloudCredentials
) -> None:
    """Discovery, trust, code and name create the entry."""
    result = await _configure(hass, await _start(hass), {"method": "discover"})
    assert result["step_id"] == "confirm_fingerprint"
    assert result["description_placeholders"]["fingerprint"] == ":".join(["AB"] * 32)
    assert "request_code" not in pairing.calls

    result = await _configure(hass, result, {"trust": True})
    assert result["step_id"] == "code"
    assert pairing.calls.count("request_code") == 1

    result = await _configure(hass, result, {"code": f" {VALID_CODE} "})
    result = await _configure(hass, result, {"terminal_name": "Hall"})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.unique_id == credentials.monitor_id
    assert entry.unique_id.startswith("jo-")
    assert entry.minor_version == 2
    assert entry.data[CONF_MONITOR][CONF_FINGERPRINT] == FINGERPRINT_ID
    stored = entry.data[CONF_CREDENTIALS]
    assert stored["term_name"] == "Hall"
    assert stored["schema"] == 1
    assert "cert_pem" in stored
    assert [s["id"] for s in entry.data[CONF_STATIONS]] == [101, 102]
    assert pairing.auto_rename == [False]
    assert pairing.closed
    assert "cancel" not in pairing.calls


async def test_room_info_failure_is_not_blocking(
    hass: HomeAssistant, pairing: FakePairing
) -> None:
    """The entry is created even if the stations cannot be listed yet."""
    pairing.room_info.side_effect = CloudApiError("down")  # type: ignore[attr-defined]
    result = await _configure(hass, await _to_name(hass), {"terminal_name": "Hall"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].data[CONF_STATIONS] == []


async def test_several_monitors(hass: HomeAssistant, pairing: FakePairing) -> None:
    """The user picks a monitor when several answer."""
    pairing.discover.return_value = [MONITOR, OTHER_MONITOR]  # type: ignore[attr-defined]
    result = await _configure(hass, await _start(hass), {"method": "discover"})
    assert result["step_id"] == "pick_monitor"
    result = await _configure(hass, result, {"monitor": "192.0.2.11:50000"})
    assert result["step_id"] == "confirm_fingerprint"
    assert result["description_placeholders"]["address"] == "192.0.2.11"


async def test_no_monitor_found(hass: HomeAssistant, pairing: FakePairing) -> None:
    """An actionable error is shown when nobody answers."""
    pairing.discover.return_value = []  # type: ignore[attr-defined]
    result = await _configure(hass, await _start(hass), {"method": "discover"})
    assert _error(result) == "no_monitor_found"
    assert result["step_id"] == "user"


@pytest.mark.parametrize(
    ("rslt", "key"),
    [(1, "retry"), (2, "registration_in_progress"), (3, "smartphone_limit")],
)
async def test_discovery_refused(
    hass: HomeAssistant, pairing: FakePairing, rslt: int, key: str
) -> None:
    """Refusals reported by the monitor during discovery are explained."""
    refused = DiscoveredMonitor(address="192.0.2.10", port=0, system=4, rslt=rslt)
    pairing.discover.return_value = [refused]  # type: ignore[attr-defined]
    result = await _configure(hass, await _start(hass), {"method": "discover"})
    assert _error(result) == key
    assert "connect" not in pairing.calls


async def test_discovery_failure(hass: HomeAssistant, pairing: FakePairing) -> None:
    """Socket errors show the raw reason."""
    pairing.discover.side_effect = DiscoveryError("port in use")  # type: ignore[attr-defined]
    result = await _configure(hass, await _start(hass), {"method": "discover"})
    assert _error(result) == "discovery_failed"
    assert result["description_placeholders"]["code"] == "port in use"


async def test_manual_address(hass: HomeAssistant, pairing: FakePairing) -> None:
    """Manual address: unicast discovery, then fallback on the given port."""
    discover: AsyncMock = pairing.discover  # type: ignore[attr-defined]
    discover.return_value = []
    result = await _configure(hass, await _start(hass), {"method": "manual"})
    assert result["step_id"] == "manual"

    result = await _configure(hass, result, {"host": "192.0.2.10"})
    assert _error(result) == "no_monitor_at_address"
    assert discover.call_args.args[1] == "192.0.2.10"

    result = await _configure(hass, result, {"host": "192.0.2.10", "port": 50000})
    assert result["step_id"] == "confirm_fingerprint"


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (
            PairingError("TLS", reason=PairingFailureReason.TLS_FAILED),
            "cannot_connect",
        ),
        (link_closed(), "cannot_connect"),
        (
            IncompatibleVersionError("SYS 3", mid="70002", rslt=1),
            "incompatible_version",
        ),
    ],
)
async def test_connection_failure(
    hass: HomeAssistant, pairing: FakePairing, error: Exception, key: str
) -> None:
    """Connection failures send the user back with an explanation."""
    pairing.fail("connect", error)
    result = await _configure(hass, await _start(hass), {"method": "discover"})
    assert result["step_id"] == "user"
    assert _error(result) == key


async def test_fingerprint_refused(hass: HomeAssistant, pairing: FakePairing) -> None:
    """Refusing the certificate aborts without sending anything."""
    result = await _configure(hass, await _start(hass), {"method": "discover"})
    result = await _configure(hass, result, {"trust": False})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "fingerprint_rejected"
    assert "request_code" not in pairing.calls
    assert pairing.closed


async def test_already_configured_by_fingerprint(
    hass: HomeAssistant, pairing: FakePairing, config_entry: MockConfigEntry
) -> None:
    """A known certificate aborts before anything is sent."""
    result = await _configure(hass, await _start(hass), {"method": "discover"})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert "approve" not in pairing.calls
    assert pairing.closed


async def test_already_configured_by_monitor_id(
    hass: HomeAssistant,
    pairing: FakePairing,
    config_entry: MockConfigEntry,
) -> None:
    """A known monitor with a new certificate aborts and drops the registration."""
    pairing.peer = peer(OTHER_FINGERPRINT)
    result = await _to_name(hass)
    result = await _configure(hass, result, {"terminal_name": "Hall"})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert "cancel" in pairing.calls
    assert "register_terminal" not in pairing.calls


async def test_wrong_code_same_connection(
    hass: HomeAssistant, pairing: FakePairing
) -> None:
    """A wrong code (RSLT 2) is corrected on the same connection."""
    result = await _configure(hass, await _to_code(hass), {"code": "0000"})
    assert _error(result) == "invalid_code"
    result = await _configure(hass, result, {"code": VALID_CODE})
    assert result["step_id"] == "terminal_name"
    assert "reconnect" not in pairing.calls


@pytest.mark.parametrize(
    ("rslt", "key"), [(1, "invalid_code_new_code"), (3, "code_expired")]
)
async def test_code_error_requests_new_code(
    hass: HomeAssistant, pairing: FakePairing, rslt: int, key: str
) -> None:
    """When the monitor closes the link, a new code is requested automatically."""
    pairing.fail("authenticate", rejected("11003", rslt))
    result = await _configure(hass, await _to_code(hass), {"code": VALID_CODE})
    assert _error(result) == key
    assert pairing.calls.count("reconnect") == 1
    assert pairing.calls.count("request_code") == 2
    result = await _configure(hass, result, {"code": VALID_CODE})
    assert result["step_id"] == "terminal_name"


async def test_code_error_reconnect_fails(
    hass: HomeAssistant, pairing: FakePairing
) -> None:
    """If the automatic new code fails, the user can resume later."""
    pairing.fail("authenticate", rejected("11003", 3))
    pairing.fail("reconnect", link_closed())
    result = await _configure(hass, await _to_code(hass), {"code": VALID_CODE})
    assert result["step_id"] == "pairing_window_closed"
    assert _error(result) == "code_expired"
    result = await _finish(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_code_request_refused(hass: HomeAssistant, pairing: FakePairing) -> None:
    """11002 refusals are explained on the resume step."""
    pairing.fail("request_code", rejected("11002", 4))
    result = await _configure(hass, await _start(hass), {"method": "discover"})
    result = await _configure(hass, result, {"trust": True})
    assert result["step_id"] == "pairing_window_closed"
    assert _error(result) == "eula_not_accepted"


async def test_name_validation_and_taken(
    hass: HomeAssistant, pairing: FakePairing
) -> None:
    """Names are checked locally, and a taken name can be changed in place."""
    pairing.taken_names.add("HomeAssist")
    result = await _to_name(hass)
    result = await _configure(hass, result, {"terminal_name": "A" * 13})
    assert _error(result) == "name_too_long"
    result = await _configure(hass, result, {"terminal_name": "HomeAssist"})
    assert _error(result) == "name_taken"
    result = await _configure(hass, result, {"terminal_name": "Hall"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert pairing.calls.count("register_cloud") == 1
    assert "reconnect" not in pairing.calls


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (rejected("11005", 1), "retry"),
        (rejected("11005", 4), "eula_not_accepted"),
        (rejected("11005", 9), "monitor_rejected"),
        (ProtocolError("bad frame"), "unknown"),
    ],
)
async def test_registration_errors_resume(
    hass: HomeAssistant, pairing: FakePairing, error: Exception, key: str
) -> None:
    """Errors that close the link are explained, then pairing resumes."""
    pairing.fail("register_terminal", error)
    result = await _configure(hass, await _to_name(hass), {"terminal_name": "Hall"})
    assert result["step_id"] == "pairing_window_closed"
    assert _error(result) == key
    assert result["description_placeholders"]["code"]
    result = await _finish(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert pairing.calls.count("reconnect") == 1


def _cloud_error(
    status: int | None, kind: str, reason: PairingFailureReason, retry: bool
) -> PairingCloudError:
    return PairingCloudError(
        "cloud",
        status=status,
        kind=kind,
        reason=reason,
        mid="71001" if status else None,
        rslt=status,
        retry_same_connection=retry,
    )


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (
            _cloud_error(
                None, "network", PairingFailureReason.CLOUD_NETWORK_ERROR, True
            ),
            "cloud_unreachable",
        ),
        (
            _cloud_error(503, "http", PairingFailureReason.CLOUD_HTTP_ERROR, True),
            "cloud_error",
        ),
    ],
)
async def test_cloud_errors_same_connection(
    hass: HomeAssistant, pairing: FakePairing, error: Exception, key: str
) -> None:
    """Transient /registClient failures are retried without pairing again."""
    pairing.fail("register_cloud", error)
    result = await _configure(hass, await _to_name(hass), {"terminal_name": "Hall"})
    assert result["step_id"] == "terminal_name"
    assert _error(result) == key
    result = await _configure(hass, result, {"terminal_name": "Hall"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "reconnect" not in pairing.calls


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (
            _cloud_error(451, "http", PairingFailureReason.EULA_NOT_ACCEPTED, False),
            "eula_not_accepted",
        ),
        (
            _cloud_error(400, "http", PairingFailureReason.CLOUD_HTTP_ERROR, False),
            "cloud_error",
        ),
        (
            _cloud_error(
                None,
                "invalid_response",
                PairingFailureReason.CLOUD_INVALID_RESPONSE,
                False,
            ),
            "cloud_invalid_response",
        ),
    ],
)
async def test_cloud_errors_resume(
    hass: HomeAssistant, pairing: FakePairing, error: Exception, key: str
) -> None:
    """Definitive /registClient failures resume from a new connection."""
    pairing.fail("register_cloud", error)
    result = await _configure(hass, await _to_name(hass), {"terminal_name": "Hall"})
    assert result["step_id"] == "pairing_window_closed"
    assert _error(result) == key
    result = await _finish(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_cloud_timeout(hass: HomeAssistant, pairing: FakePairing) -> None:
    """A cloud timeout has its own message."""
    error = _cloud_error(
        None, "network", PairingFailureReason.CLOUD_NETWORK_ERROR, True
    )
    cause = CloudApiError("timeout", kind="network")
    cause.__cause__ = TimeoutError()
    error.__cause__ = cause
    pairing.fail("register_cloud", error)
    result = await _configure(hass, await _to_name(hass), {"terminal_name": "Hall"})
    assert _error(result) == "cloud_timeout"


async def test_window_closed_then_resume(
    hass: HomeAssistant, pairing: FakePairing
) -> None:
    """A closed pairing window can be resumed without starting over."""
    pairing.fail("authenticate", link_closed())
    result = await _configure(hass, await _to_code(hass), {"code": VALID_CODE})
    assert result["step_id"] == "pairing_window_closed"
    assert _error(result) == "link_closed"

    pairing.fail(
        "reconnect", PairingError("TLS", reason=PairingFailureReason.TLS_FAILED)
    )
    result = await _configure(hass, result)
    assert _error(result) == "cannot_connect"

    result = await _finish(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert pairing.calls.count("approve") == 1


async def test_timeout(hass: HomeAssistant, pairing: FakePairing) -> None:
    """A monitor timeout leads to the resume step with an explanation."""
    pairing.fail("request_code", PairingTimeoutError("late", mid="11002"))
    result = await _configure(hass, await _start(hass), {"method": "discover"})
    result = await _configure(hass, result, {"trust": True})
    assert result["step_id"] == "pairing_window_closed"
    assert _error(result) == "timeout"


async def test_dismissed_flow_cancels(
    hass: HomeAssistant, pairing: FakePairing
) -> None:
    """Closing the dialog drops the link and any unfinished registration."""
    result = await _to_name(hass)
    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()
    assert "cancel" in pairing.calls
    assert pairing.closed


async def test_reauth(
    hass: HomeAssistant, pairing: FakePairing, config_entry: MockConfigEntry
) -> None:
    """Re-pairing the same monitor replaces the credentials silently."""
    unique_id = config_entry.unique_id
    with (
        patch("custom_components.aiphone_jo.async_setup_entry", return_value=True),
        patch(
            "custom_components.aiphone_jo.config_flow.async_unregister",
            return_value=False,
        ) as unregister,
        patch(
            "custom_components.aiphone_jo.config_flow.async_notify_manual_removal"
        ) as notify,
    ):
        result = await config_entry.start_reauth_flow(hass)
        assert result["step_id"] == "reauth_confirm"
        result = await _configure(hass, result)
        assert result["step_id"] == "user"
        result = await _configure(hass, result, {"method": "discover"})
        assert result["step_id"] == "code"
        assert "approve" not in pairing.calls
        result = await _configure(hass, result, {"code": VALID_CODE})
        assert result["data_schema"]({})["terminal_name"] == "HomeAssist"
        result = await _configure(hass, result, {"terminal_name": "HomeAssist"})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.unique_id == unique_id
    unregister.assert_awaited_once()
    notify.assert_not_called()


async def test_reauth_fingerprint_changed(
    hass: HomeAssistant, pairing: FakePairing, config_entry: MockConfigEntry
) -> None:
    """A new certificate needs an explicit opt-in; the monitor id is kept."""
    unique_id = config_entry.unique_id
    pairing.peer = peer(OTHER_FINGERPRINT)
    with patch("custom_components.aiphone_jo.async_setup_entry", return_value=True):
        result = await config_entry.start_reauth_flow(hass)
        result = await _configure(hass, result)
        result = await _configure(hass, result, {"method": "discover"})
        assert result["step_id"] == "fingerprint_changed"
        assert result["description_placeholders"]["previous"] == ":".join(["AB"] * 32)
        result = await _configure(hass, result, {"trust": True})
        assert result["step_id"] == "code"
        result = await _configure(hass, result, {"code": VALID_CODE})
        result = await _configure(hass, result, {"terminal_name": "HomeAssist"})
        await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    assert config_entry.unique_id == unique_id
    assert config_entry.data[CONF_MONITOR][CONF_FINGERPRINT] == "cd" * 32


async def test_reauth_fingerprint_changed_refused(
    hass: HomeAssistant, pairing: FakePairing, config_entry: MockConfigEntry
) -> None:
    """Refusing the new certificate aborts."""
    pairing.peer = peer(OTHER_FINGERPRINT)
    result = await config_entry.start_reauth_flow(hass)
    result = await _configure(hass, result)
    result = await _configure(hass, result, {"method": "discover"})
    result = await _configure(hass, result, {"trust": False})
    assert result["reason"] == "fingerprint_rejected"


async def test_reauth_wrong_monitor(
    hass: HomeAssistant, pairing: FakePairing, config_entry: MockConfigEntry
) -> None:
    """Re-pairing another monitor is refused and its registration dropped."""
    pairing.credentials = make_credentials(monitor="other-monitor")
    result = await config_entry.start_reauth_flow(hass)
    result = await _configure(hass, result)
    result = await _configure(hass, result, {"method": "discover"})
    result = await _configure(hass, result, {"code": VALID_CODE})
    result = await _configure(hass, result, {"terminal_name": "HomeAssist"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_monitor"
    assert "cancel" in pairing.calls


async def test_reconfigure(
    hass: HomeAssistant, pairing: FakePairing, config_entry: MockConfigEntry
) -> None:
    """Reconfigure pairs again and unregisters the previous terminal."""
    with (
        patch("custom_components.aiphone_jo.async_setup_entry", return_value=True),
        patch(
            "custom_components.aiphone_jo.config_flow.async_unregister",
            return_value=True,
        ) as unregister,
        patch(
            "custom_components.aiphone_jo.config_flow.async_notify_manual_removal"
        ) as notify,
    ):
        result = await config_entry.start_reconfigure_flow(hass)
        assert result["step_id"] == "reconfigure"
        result = await _configure(hass, result, {"terminal_name": "A" * 13})
        assert _error(result) == "name_too_long"
        result = await _configure(hass, result, {"terminal_name": "Hall"})
        result = await _configure(hass, result, {"method": "discover"})
        result = await _configure(hass, result, {"code": VALID_CODE})
        assert result["data_schema"]({})["terminal_name"] == "Hall"
        result = await _configure(hass, result, {"terminal_name": "Hall"})
        await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_CREDENTIALS]["term_name"] == "Hall"
    previous = unregister.await_args.args[1]
    assert previous.term_name == "HomeAssist"
    notify.assert_not_called()


async def test_reconfigure_unregister_failure_notifies(
    hass: HomeAssistant, pairing: FakePairing, config_entry: MockConfigEntry
) -> None:
    """If the previous terminal cannot be unregistered, the user is told."""
    with (
        patch("custom_components.aiphone_jo.async_setup_entry", return_value=True),
        patch(
            "custom_components.aiphone_jo.config_flow.async_unregister",
            return_value=False,
        ),
        patch(
            "custom_components.aiphone_jo.config_flow.async_notify_manual_removal"
        ) as notify,
    ):
        result = await config_entry.start_reconfigure_flow(hass)
        result = await _configure(hass, result, {"terminal_name": "Hall"})
        result = await _configure(hass, result, {"method": "discover"})
        result = await _configure(hass, result, {"code": VALID_CODE})
        result = await _configure(hass, result, {"terminal_name": "Hall"})
        await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    notify.assert_awaited_once()


async def test_options(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """The station carrying the output session can be chosen."""
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_STATION: "102"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {CONF_STATION: 102}


async def test_options_without_stations(
    hass: HomeAssistant, credentials: CloudCredentials
) -> None:
    """Options explain that stations are unknown."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={**entry_data(credentials), CONF_STATIONS: []}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_stations"
