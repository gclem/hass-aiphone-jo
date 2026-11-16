"""Diagnostic service: an output command without a monitoring session."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aiphone_jo.const import DOMAIN
from custom_components.aiphone_jo.services import SERVICE_TEST_OUTPUT
from custom_components.aiphone_jo.vendor.aiphone_jo import ResponseTimeoutError

from .conftest import ClientHolder


async def _call(hass: HomeAssistant, output: str) -> dict:
    return await hass.services.async_call(
        DOMAIN,
        SERVICE_TEST_OUTPUT,
        {"output": output},
        blocking=True,
        return_response=True,
    )


async def test_reply_without_identifiers(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """The monitor's reply is returned, without the terminal's identifiers."""
    client = client_holder.client
    client.active_session = None
    client.request = AsyncMock(
        return_value={
            "HEADER": {"MID": "27021", "RSLT": 0, "CLIID": "example-client"},
            "BODY": {"REASON": 0},
        }
    )
    response = await _call(hass, "1")
    assert response["answered"] is True
    assert response["reply_mid"] == "27021"
    assert response["rslt"] == 0
    assert response["body"] == {"REASON": 0}
    assert "example-client" not in str(response)
    sent = client.request.await_args.args[0]
    assert sent.payload["HEADER"]["MID"] == "26021"
    assert "CID" not in sent.payload["HEADER"]
    await _call(hass, "2")
    assert client.request.await_args.args[0].payload["HEADER"]["MID"] == "26121"


async def test_no_reply(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """A command the monitor ignores is reported as unanswered."""
    client = client_holder.client
    client.active_session = None
    client.request = AsyncMock(side_effect=ResponseTimeoutError("no reply"))
    response = await _call(hass, "1")
    assert response["answered"] is False


async def test_refused_while_a_session_is_open(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """With a session open, the test would prove nothing: it is refused."""
    client = client_holder.client
    client.active_session = object()
    client.request = AsyncMock()
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "1")
    assert err.value.translation_key == "session_open"
    client.request.assert_not_awaited()
