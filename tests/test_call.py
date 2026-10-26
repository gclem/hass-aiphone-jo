"""Tests for the experimental call answering."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.aiphone_jo.const import CONF_CALL_ANSWERING, DOMAIN
from custom_components.aiphone_jo.vendor.aiphone_jo import (
    CallAnsweredElsewhereError,
    CallEndedError,
    CallEndReason,
    CallStatus,
    CallUpdatedEvent,
    IncomingCall,
    Output,
    RingEvent,
    SessionError,
)

from .conftest import ClientHolder, FakeCallSession, FakeClient, until
from .fake_go2rtc import FakeGo2Rtc

ANSWER = "button.aiphone_jo_1mdw_answer"
HANG_UP = "button.aiphone_jo_1mdw_hang_up"
REJECT = "button.aiphone_jo_1mdw_reject"
CALL = "sensor.aiphone_jo_1mdw_call"
RINGING = "binary_sensor.aiphone_jo_1mdw_ringing"
ACTION_1 = "button.aiphone_jo_1mdw_action_1"
SEQUENCE = ["request_offer", "send_answer", "ready", "reply", "start"]


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Detect the end of sessions quickly."""
    monkeypatch.setattr("custom_components.aiphone_jo.call.SESSION_POLL", 0.02)
    monkeypatch.setattr("custom_components.aiphone_jo.media.SESSION_POLL", 0.02)
    monkeypatch.setattr("custom_components.aiphone_jo.media.VIDEO_TIMEOUT", 0.5)


@pytest.fixture
async def call_entry(
    hass: HomeAssistant,
    go2rtc: FakeGo2Rtc,
    config_entry: MockConfigEntry,
    client_holder: ClientHolder,
) -> MockConfigEntry:
    """Loaded entry with call answering enabled."""
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_CALL_ANSWERING: True}
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry


def _client(holder: ClientHolder) -> FakeClient:
    assert holder.client is not None
    return holder.client


def _incoming_call() -> IncomingCall:
    return IncomingCall(
        cid="example-call",
        station_client_id="example-station",
        display_name="",
        display_name_2="",
        call_kind=1,
        term_kind=5,
        term_id=101,
        topic="example/topic",
        ringing_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
    )


async def _ring(hass: HomeAssistant, client: FakeClient) -> IncomingCall:
    call = _incoming_call()
    client.emit(RingEvent(call))
    await hass.async_block_till_done()
    return call


async def _press(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )


def _session(client: FakeClient) -> FakeCallSession:
    assert client.call_sessions
    return client.call_sessions[-1]


async def test_disabled_by_default(
    hass: HomeAssistant, config_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """Without the option, no call entity exists (stale ones are removed)."""
    registry = er.async_get(hass)
    stale = registry.async_get_or_create(
        "button",
        DOMAIN,
        f"{config_entry.unique_id}_call_answer",
        config_entry=config_entry,
    )
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(stale.entity_id) is None
    for entity_id in (ANSWER, HANG_UP, REJECT, CALL):
        assert hass.states.get(entity_id) is None
    await _ring(hass, _client(client_holder))
    with pytest.raises(HomeAssistantError) as err:
        await config_entry.runtime_data.calls.async_answer()
    assert err.value.translation_key == "call_answering_disabled"


async def test_ring_then_answer_and_hang_up(
    hass: HomeAssistant,
    call_entry: MockConfigEntry,
    client_holder: ClientHolder,
    go2rtc: FakeGo2Rtc,
) -> None:
    """Answering runs the sequence and shows the visitor in the camera."""
    client = _client(client_holder)
    assert hass.states.get(CALL).state == "idle"
    assert hass.states.get(ANSWER).state == STATE_UNAVAILABLE

    await _ring(hass, client)
    assert hass.states.get(CALL).state == "ringing"
    assert hass.states.get(ANSWER).state != STATE_UNAVAILABLE
    assert hass.states.get(HANG_UP).state == STATE_UNAVAILABLE

    await _press(hass, ANSWER)
    session = _session(client)
    assert session.steps == SEQUENCE
    assert session.answer is not None
    assert "a=mid:audio" in session.answer
    assert [name for name, _ in go2rtc.ingests] == [f"aiphone_jo_{call_entry.entry_id}"]
    state = hass.states.get(CALL)
    assert state.state == "talking"
    assert state.attributes["station"] == "Front"
    assert hass.states.get(RINGING).state == "off"
    assert hass.states.get(HANG_UP).state != STATE_UNAVAILABLE
    media = call_entry.runtime_data.media
    assert media.live
    assert client.lives == []

    await _press(hass, HANG_UP)
    assert session.steps[-1] == "close"
    assert hass.states.get(CALL).state == "ended"
    assert not media.live


async def test_station_ends_call(
    hass: HomeAssistant, call_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """The call ends when the station closes the session."""
    client = _client(client_holder)
    await _ring(hass, client)
    await _press(hass, ANSWER)
    _session(client).active = False
    await until(lambda: hass.states.get(CALL).state == "ended")
    assert not call_entry.runtime_data.media.live


@pytest.mark.parametrize("status", [CallStatus.ENDED, CallStatus.CANCELLED])
async def test_call_updated_ends_call(
    hass: HomeAssistant,
    call_entry: MockConfigEntry,
    client_holder: ClientHolder,
    status: CallStatus,
) -> None:
    """Library updates end an answered call."""
    client = _client(client_holder)
    call = await _ring(hass, client)
    await _press(hass, ANSWER)
    call.status = status
    client.emit(CallUpdatedEvent(call))
    await until(lambda: hass.states.get(CALL).state == "ended")
    assert _session(client).steps[-1] == "close"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (CallStatus.CANCELLED, "missed"),
        (CallStatus.EXPIRED, "missed"),
        (CallStatus.ENDED, "missed"),
        (CallStatus.REJECTED, "rejected"),
    ],
)
async def test_unanswered_call(
    hass: HomeAssistant,
    call_entry: MockConfigEntry,
    client_holder: ClientHolder,
    status: CallStatus,
    expected: str,
) -> None:
    """A call that ends while ringing is missed (or rejected elsewhere)."""
    client = _client(client_holder)
    call = await _ring(hass, client)
    call.status = status
    client.emit(CallUpdatedEvent(call))
    await hass.async_block_till_done()
    assert hass.states.get(CALL).state == expected
    assert hass.states.get(ANSWER).state == STATE_UNAVAILABLE


async def test_reject(
    hass: HomeAssistant, call_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """Rejecting declines the ringing call."""
    client = _client(client_holder)
    await _ring(hass, client)
    await _press(hass, REJECT)
    assert _session(client).steps == ["reject"]
    assert hass.states.get(CALL).state == "rejected"


@pytest.mark.parametrize("step", ["request_offer", "ready", "reply"])
async def test_failure_before_pick_up(
    hass: HomeAssistant,
    call_entry: MockConfigEntry,
    client_holder: ClientHolder,
    step: str,
) -> None:
    """Before the pick-up, a failure withdraws and the call can be retried."""
    client = _client(client_holder)
    client.call_errors[step] = SessionError("boom")
    await _ring(hass, client)
    with pytest.raises(HomeAssistantError) as err:
        await _press(hass, ANSWER)
    assert err.value.translation_key == "call_failed"
    assert err.value.translation_placeholders == {"code": "SessionError"}
    assert _session(client).steps[-1] == "withdraw"
    assert hass.states.get(CALL).state == "ringing"
    assert not call_entry.runtime_data.media.live
    client.call_errors.clear()
    await _press(hass, ANSWER)
    assert hass.states.get(CALL).state == "talking"


async def test_start_refused(
    hass: HomeAssistant, call_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """A refused pick-up hangs up."""
    client = _client(client_holder)
    client.call_start_result = False
    await _ring(hass, client)
    with pytest.raises(HomeAssistantError) as err:
        await _press(hass, ANSWER)
    assert err.value.translation_key == "call_failed"
    assert _session(client).steps == [*SEQUENCE, "close"]
    assert hass.states.get(CALL).state == "failed"


async def test_answer_call_refused(
    hass: HomeAssistant, call_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """If the library refuses to take the call, it can be retried."""
    client = _client(client_holder)
    client.call_errors["answer_call"] = SessionError("busy")
    await _ring(hass, client)
    with pytest.raises(HomeAssistantError) as err:
        await _press(hass, ANSWER)
    assert err.value.translation_key == "call_failed"
    assert hass.states.get(CALL).state == "ringing"


async def test_not_ringing(hass: HomeAssistant, call_entry: MockConfigEntry) -> None:
    """Actions are refused when they make no sense."""
    calls = call_entry.runtime_data.calls
    for action, key in (
        (calls.async_answer, "call_not_ringing"),
        (calls.async_reject, "call_not_ringing"),
        (calls.async_hang_up, "call_not_active"),
    ):
        with pytest.raises(HomeAssistantError) as err:
            await action()
        assert err.value.translation_key == key


async def test_automatic_hang_up(
    hass: HomeAssistant, call_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """A forgotten call is hung up after the maximum talk time."""
    client = _client(client_holder)
    await _ring(hass, client)
    await _press(hass, ANSWER)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=91))
    await hass.async_block_till_done()
    assert hass.states.get(CALL).state == "talking"
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=201))
    await until(lambda: hass.states.get(CALL).state == "ended")
    assert _session(client).steps[-1] == "close"


async def test_answer_replaces_live_view(
    hass: HomeAssistant,
    call_entry: MockConfigEntry,
    client_holder: ClientHolder,
    go2rtc: FakeGo2Rtc,
    hass_ws_client: Any,
) -> None:
    """Live viewers are told to reconnect when a call is answered."""
    client = _client(client_holder)
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id(
        {
            "type": "camera/webrtc/offer",
            "entity_id": "camera.aiphone_jo_1mdw_camera",
            "offer": "v=0 offer",
        }
    )
    assert (await ws.receive_json())["success"]
    for _ in range(3):
        await ws.receive_json()
    assert client.lives

    await _ring(hass, client)
    await _press(hass, ANSWER)
    event = (await ws.receive_json())["event"]
    assert event["type"] == "error"
    assert event["code"] == "call_started"
    assert client.lives[0].exited.is_set()
    assert hass.states.get(CALL).state == "talking"

    # New viewers join the call stream without a new monitoring session.
    ws2 = await hass_ws_client(hass)
    await ws2.send_json_auto_id(
        {
            "type": "camera/webrtc/offer",
            "entity_id": "camera.aiphone_jo_1mdw_camera",
            "offer": "v=0 offer",
        }
    )
    assert (await ws2.receive_json())["success"]
    for _ in range(3):
        await ws2.receive_json()
    assert len(client.lives) == 1

    await _press(hass, HANG_UP)
    event = (await ws2.receive_json())["event"]
    assert event["code"] == "call_ended"


async def test_outputs_during_call(
    hass: HomeAssistant, call_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """Output buttons keep working during a call."""
    client = _client(client_holder)
    await _ring(hass, client)
    await _press(hass, ANSWER)
    await _press(hass, ACTION_1)
    client.open_door.assert_awaited_once_with(Output.UNLOCK, 101, 5)


async def test_unload_hangs_up(
    hass: HomeAssistant, call_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """Unloading the integration hangs up."""
    client = _client(client_holder)
    await _ring(hass, client)
    await _press(hass, ANSWER)
    assert await hass.config_entries.async_unload(call_entry.entry_id)
    assert _session(client).steps[-1] == "close"


async def test_video_unavailable(
    hass: HomeAssistant, config_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """Without go2rtc, answering is refused with an explanation."""
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_CALL_ANSWERING: True}
    )
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    client = _client(client_holder)
    await _ring(hass, client)
    with pytest.raises(HomeAssistantError) as err:
        await _press(hass, ANSWER)
    assert err.value.translation_key == "call_video_unavailable"
    assert client.call_sessions == []


async def test_answered_elsewhere_while_answering(
    hass: HomeAssistant, call_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """Another phone answered first: the relay is left and the user is told."""
    client = _client(client_holder)
    client.call_errors["reply"] = CallAnsweredElsewhereError(
        "Another phone answered the call.", CallEndReason.ANSWERED_ELSEWHERE
    )
    await _ring(hass, client)
    with pytest.raises(HomeAssistantError) as err:
        await _press(hass, ANSWER)
    assert err.value.translation_key == "call_answered_elsewhere"
    assert _session(client).steps[-1] == "withdraw"
    state = hass.states.get(CALL)
    assert state.state == "answered_elsewhere"
    assert state.attributes["end_reason"] == "answered_elsewhere"
    assert not call_entry.runtime_data.media.live


async def test_call_ended_while_answering(
    hass: HomeAssistant, call_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """The visitor gave up while the call was being answered."""
    client = _client(client_holder)
    client.call_errors["request_offer"] = CallEndedError(
        "The call ended.", CallEndReason.CANCELLED_BY_STATION
    )
    await _ring(hass, client)
    with pytest.raises(HomeAssistantError) as err:
        await _press(hass, ANSWER)
    assert err.value.translation_key == "call_ended"
    state = hass.states.get(CALL)
    assert state.state == "missed"
    assert state.attributes["end_reason"] == "cancelled_by_station"


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (CallEndReason.ANSWERED_ELSEWHERE, "answered_elsewhere"),
        (CallEndReason.EXPIRED, "missed"),
        (CallEndReason.CANCELLED_BY_STATION, "missed"),
        (CallEndReason.REJECTED, "rejected"),
    ],
)
async def test_end_reason_while_ringing(
    hass: HomeAssistant,
    call_entry: MockConfigEntry,
    client_holder: ClientHolder,
    reason: CallEndReason,
    expected: str,
) -> None:
    """The library's end reason decides the state of an unanswered call."""
    client = _client(client_holder)
    call = await _ring(hass, client)
    call.status = CallStatus.ANSWERED_ELSEWHERE
    if reason is not CallEndReason.ANSWERED_ELSEWHERE:
        call.status = CallStatus.ENDED
    client.emit(CallUpdatedEvent(call, reason))
    await hass.async_block_till_done()
    state = hass.states.get(CALL)
    assert state.state == expected
    assert state.attributes["end_reason"] == reason.value


async def test_station_hangs_up_with_reason(
    hass: HomeAssistant, call_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """A call ended by the station keeps the reason."""
    client = _client(client_holder)
    call = await _ring(hass, client)
    await _press(hass, ANSWER)
    call.status = CallStatus.ENDED
    client.emit(CallUpdatedEvent(call, CallEndReason.ENDED_BY_STATION))
    await until(lambda: hass.states.get(CALL).state == "ended")
    assert hass.states.get(CALL).attributes["end_reason"] == "ended_by_station"


async def test_hang_up_reason(
    hass: HomeAssistant, call_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """Hanging up from Home Assistant is recorded as such."""
    client = _client(client_holder)
    await _ring(hass, client)
    await _press(hass, ANSWER)
    await _press(hass, HANG_UP)
    state = hass.states.get(CALL)
    assert state.state == "ended"
    assert state.attributes["end_reason"] == "ended_by_client"
