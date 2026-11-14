"""Tests for the problems reported to the user (connection, repairs, events)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.components.camera import async_get_image
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.aiphone_jo.const import (
    CONF_CALL_ANSWERING,
    CONF_RING_SNAPSHOT,
    DOMAIN,
    EVENT_ERROR_BUS,
)
from custom_components.aiphone_jo.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.aiphone_jo.health import UNREACHABLE_DELAY
from custom_components.aiphone_jo.vendor.aiphone_jo import (
    CallStatus,
    CallUpdatedEvent,
    CloudCredentials,
    ConnectionStateEvent,
    Output,
    ResponseTimeoutError,
    RoomInfo,
)
from custom_components.aiphone_jo.vendor.aiphone_jo.models import CallSignalRecord

from .conftest import FRONT, ClientHolder, FakeClient, until
from .fake_go2rtc import JPEG, FakeGo2Rtc
from .test_call import ANSWER, HANG_UP, REJECT, _ring

CLOUD = "binary_sensor.aiphone_jo_1mdw_cloud_connection"
PROBLEM = "event.aiphone_jo_1mdw_problem"
ACTION_1 = "button.aiphone_jo_1mdw_action_1"
CAMERA = "camera.aiphone_jo_1mdw_camera"
BACK_CAMERA = "camera.aiphone_jo_1mdw_back_camera"
IMAGE = "image.aiphone_jo_1mdw_last_visitor"


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Short waits."""
    monkeypatch.setattr("custom_components.aiphone_jo.call.SESSION_POLL", 0.02)
    monkeypatch.setattr("custom_components.aiphone_jo.media.SESSION_POLL", 0.02)
    monkeypatch.setattr("custom_components.aiphone_jo.media.VIDEO_TIMEOUT", 0.5)
    monkeypatch.setattr("custom_components.aiphone_jo.snapshot.SNAPSHOT_TIMEOUT", 2.0)


@pytest.fixture
def errors(hass: HomeAssistant) -> list[Event]:
    """Collected aiphone_jo_error events."""
    events: list[Event] = []
    hass.bus.async_listen(EVENT_ERROR_BUS, events.append)
    return events


def _client(holder: ClientHolder) -> FakeClient:
    assert holder.client is not None
    return holder.client


def _issue(hass: HomeAssistant, issue: str, entry: MockConfigEntry) -> Any:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{issue}_{entry.entry_id}")


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, **options: Any) -> None:
    hass.config_entries.async_update_entry(entry, options=options)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _lose(hass: HomeAssistant, client: FakeClient, error: str) -> None:
    client.connected = False
    client.emit(ConnectionStateEvent(False, False, error))
    await hass.async_block_till_done()


async def _restore(hass: HomeAssistant, client: FakeClient, ring: bool = True) -> None:
    client.connected = True
    client.broadcast_subscribed = ring
    client.emit(ConnectionStateEvent(True, ring))
    await hass.async_block_till_done()


# ------------------------------------------------------------- connection


async def test_connection_lost_and_restored(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    client_holder: ClientHolder,
    errors: list[Event],
) -> None:
    """The sensor, the Problem entity and the bus event tell what happened."""
    client = _client(client_holder)
    state = hass.states.get(CLOUD)
    assert state.state == STATE_ON
    assert state.attributes["connected_since"]
    assert state.attributes["reconnections"] == 0
    assert state.attributes["ring_topic_subscribed"] is True

    await _lose(hass, client, "Disconnected during message iteration")
    state = hass.states.get(CLOUD)
    # Still available: it explains why the other entities are unavailable.
    assert state.state == STATE_OFF
    assert state.attributes["last_error"] == "Disconnected during message iteration"
    assert state.attributes["disconnected_since"]
    assert state.attributes["connected_since"] is None
    problem = hass.states.get(PROBLEM)
    assert problem.attributes["event_type"] == "connection_lost"
    assert problem.attributes["message"] == "Disconnected during message iteration"
    [event] = errors
    assert event.data["type"] == "connection_lost"
    assert event.data["entry_id"] == loaded_entry.entry_id
    assert event.data["device_id"]

    await _restore(hass, client)
    state = hass.states.get(CLOUD)
    assert state.state == STATE_ON
    assert state.attributes["reconnections"] == 1
    assert state.attributes["disconnected_since"] is None
    # The last error stays visible after the reconnection.
    assert state.attributes["last_error"] == "Disconnected during message iteration"
    assert len(errors) == 1


async def test_cloud_unreachable_repair(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    client_holder: ClientHolder,
    errors: list[Event],
) -> None:
    """A lasting outage opens a repair, removed once connected again."""
    client = _client(client_holder)
    await _lose(hass, client, "timeout")
    assert _issue(hass, "cloud_unreachable", loaded_entry) is None

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=UNREACHABLE_DELAY + 1)
    )
    await hass.async_block_till_done()
    issue = _issue(hass, "cloud_unreachable", loaded_entry)
    assert issue is not None
    assert issue.translation_placeholders["error"] == "timeout"
    assert [event.data["type"] for event in errors] == [
        "connection_lost",
        "cloud_unreachable",
    ]

    await _restore(hass, client)
    assert _issue(hass, "cloud_unreachable", loaded_entry) is None


async def test_short_outage_has_no_repair(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """A reconnection before the delay cancels the repair."""
    client = _client(client_holder)
    await _lose(hass, client, "timeout")
    await _restore(hass, client)
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=UNREACHABLE_DELAY + 1)
    )
    await hass.async_block_till_done()
    assert _issue(hass, "cloud_unreachable", loaded_entry) is None


async def test_auth_refused_is_reported(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    client_holder: ClientHolder,
    errors: list[Event],
) -> None:
    """A refused identity is reported once, without the unreachable repair."""
    client = _client(client_holder)
    client.connected = False
    client.emit(ConnectionStateEvent(False, False, "refused", auth_failed=True))
    await hass.async_block_till_done()
    assert [event.data["type"] for event in errors] == ["auth_refused"]
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=UNREACHABLE_DELAY + 1)
    )
    await hass.async_block_till_done()
    assert _issue(hass, "cloud_unreachable", loaded_entry) is None


async def test_errors_are_redacted(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    client_holder: ClientHolder,
    credentials: CloudCredentials,
    errors: list[Event],
) -> None:
    """Identifiers of the terminal never reach attributes or events."""
    client = _client(client_holder)
    await _lose(
        hass,
        client,
        f"cannot reach {credentials.host} as {credentials.client_id} "
        f"on {credentials.recv_topic}",
    )
    message = errors[0].data["message"]
    for secret in (credentials.host, credentials.client_id, credentials.recv_topic):
        assert secret not in message
        assert secret not in str(hass.states.get(CLOUD).attributes)
    assert "**REDACTED**" in message


# -------------------------------------------------------------- ring topic


async def test_ring_topic_refused_at_setup(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_holder: ClientHolder,
    errors: list[Event],
) -> None:
    """A refused ring topic is shown at once, then cleared when accepted."""

    def refuse(client: FakeClient) -> None:
        client.broadcast_subscribed = False

    client_holder.setup = refuse
    await _setup(hass, config_entry)
    assert _issue(hass, "ring_topic_refused", config_entry) is not None
    assert [event.data["type"] for event in errors] == ["ring_topic_refused"]
    assert hass.states.get(PROBLEM).attributes["event_type"] == "ring_topic_refused"
    assert hass.states.get(CLOUD).attributes["ring_topic_subscribed"] is False

    client = _client(client_holder)
    await _lose(hass, client, "lost")
    await _restore(hass, client, ring=True)
    assert _issue(hass, "ring_topic_refused", config_entry) is None


async def test_remove_entry_clears_repairs(
    hass: HomeAssistant, config_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """Removing the integration removes its repairs."""

    def refuse(client: FakeClient) -> None:
        client.broadcast_subscribed = False

    client_holder.setup = refuse
    await _setup(hass, config_entry)
    assert _issue(hass, "ring_topic_refused", config_entry) is not None
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("custom_components.aiphone_jo.async_unregister", _unregistered)
        await hass.config_entries.async_remove(config_entry.entry_id)
        await hass.async_block_till_done()
    assert _issue(hass, "ring_topic_refused", config_entry) is None


async def _unregistered(*_: Any) -> bool:
    return True


# --------------------------------------------------------- other problems


async def test_command_failure_is_reported(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    client_holder: ClientHolder,
    errors: list[Event],
) -> None:
    """A failed output pulse raises and is reported."""
    client = _client(client_holder)
    client.open_door.side_effect = ResponseTimeoutError("no reply")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: ACTION_1}, blocking=True
        )
    await hass.async_block_till_done()
    [event] = errors
    assert event.data["type"] == "command_failed"
    assert event.data["message"] == "Action 1: ResponseTimeoutError: no reply"


async def test_ring_snapshot_failure_is_reported(
    hass: HomeAssistant,
    go2rtc: FakeGo2Rtc,
    config_entry: MockConfigEntry,
    client_holder: ClientHolder,
    errors: list[Event],
) -> None:
    """A ring without image is reported."""
    from custom_components.aiphone_jo.vendor.aiphone_jo import SessionError

    await _setup(hass, config_entry, **{CONF_RING_SNAPSHOT: True})
    client = _client(client_holder)
    client.call_errors["answer_call"] = SessionError("busy")
    await _ring(hass, client)
    await until(lambda: errors)
    [event] = errors
    assert event.data["type"] == "ring_snapshot_failed"
    message = "No image of the visitor at Front: SessionError: busy"
    assert event.data["message"] == message


# ------------------------------------------------- no monitoring while ringing


async def test_image_avoids_the_ringing_station(
    hass: HomeAssistant,
    go2rtc: FakeGo2Rtc,
    config_entry: MockConfigEntry,
    client_holder: ClientHolder,
) -> None:
    """While a station rings, its camera keeps the last image; the others work."""
    await _setup(hass, config_entry)
    client = _client(client_holder)
    media = config_entry.runtime_data.media
    assert (await async_get_image(hass, CAMERA)).content == JPEG
    image, at = media._images[101]
    media._images[101] = (image, at - 3600)  # Stale: refreshed without the ring.
    await _ring(hass, client)  # Front (101) rings.
    assert (await async_get_image(hass, CAMERA)).content == JPEG
    assert (await async_get_image(hass, BACK_CAMERA)).content == JPEG
    assert [live.terminal for live in client.lives] == [101, 102]


async def test_single_station_ringing_keeps_last_image(
    hass: HomeAssistant,
    go2rtc: FakeGo2Rtc,
    config_entry: MockConfigEntry,
    client_holder: ClientHolder,
    hass_ws_client: Any,
) -> None:
    """With the ringing station only, no session is opened during the ring."""

    def single(client: FakeClient) -> None:
        client.get_room_info.return_value = RoomInfo(terminals=(FRONT,), term_inf_num=1)

    client_holder.setup = single
    await _setup(hass, config_entry)
    client = _client(client_holder)
    runtime = config_entry.runtime_data
    media = runtime.media
    [front] = runtime.camera_stations
    assert hass.states.get(BACK_CAMERA) is None
    assert await media.async_image(front) == JPEG
    assert len(client.lives) == 1
    media._images[101] = (JPEG, 0.0)  # Stale: would be refreshed without the ring.

    call = await _ring(hass, client)
    assert await media.async_image(front) == JPEG
    assert len(client.lives) == 1

    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id(
        {"type": "camera/webrtc/offer", "entity_id": CAMERA, "offer": "v=0 offer"}
    )
    assert (await ws.receive_json())["success"]
    session = (await ws.receive_json())["event"]
    error = (await ws.receive_json())["event"]
    assert session["type"] == "session"
    assert error["code"] == "station_ringing"
    assert len(client.lives) == 1

    call.status = CallStatus.CANCELLED
    client.emit(CallUpdatedEvent(call))
    await hass.async_block_till_done()
    assert await media.async_image(front) == JPEG
    assert len(client.lives) == 2


async def test_buttons_use_the_ringing_station_last(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """The output buttons prefer a station that is not calling."""
    client = _client(client_holder)
    await _ring(hass, client)
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: ACTION_1}, blocking=True
    )
    client.open_door.assert_awaited_once_with(Output.UNLOCK, 102, 5)
    runtime = loaded_entry.runtime_data
    assert [station.id for station in runtime.session_stations()] == [102, 101]


# ------------------------------------------------------------- call buttons


async def test_call_buttons_stay_available(
    hass: HomeAssistant,
    go2rtc: FakeGo2Rtc,
    config_entry: MockConfigEntry,
    client_holder: ClientHolder,
) -> None:
    """Call buttons are available without a call, and say why they do nothing."""
    await _setup(hass, config_entry, **{CONF_CALL_ANSWERING: True})
    for entity_id in (ANSWER, REJECT, HANG_UP):
        assert hass.states.get(entity_id).state == "unknown"
    for entity_id, key in (
        (ANSWER, "call_not_ringing"),
        (REJECT, "call_not_ringing"),
        (HANG_UP, "call_not_active"),
    ):
        with pytest.raises(HomeAssistantError) as err:
            await hass.services.async_call(
                BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
            )
        assert err.value.translation_key == key
    await _lose(hass, _client(client_holder), "lost")
    assert hass.states.get(ANSWER).state == "unavailable"


# ------------------------------------------------------ last visitor image


async def test_last_visitor_image_survives_restart(
    hass: HomeAssistant,
    go2rtc: FakeGo2Rtc,
    config_entry: MockConfigEntry,
    client_holder: ClientHolder,
    hass_storage: dict[str, Any],
) -> None:
    """The last image, its station and time are restored after a restart."""
    await _setup(hass, config_entry, **{CONF_RING_SNAPSHOT: True})
    client = _client(client_holder)
    await _ring(hass, client)
    snapshots = config_entry.runtime_data.snapshots
    await until(lambda: snapshots.image == JPEG)
    captured_at = hass.states.get(IMAGE).state

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    key = f"{DOMAIN}.{config_entry.entry_id}.last_visitor"
    assert hass_storage[key]["data"]["station"] == "Front"

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(IMAGE)
    assert state.state == captured_at
    assert state.attributes["station"] == "Front"
    assert state.attributes["ringing_at"] == "2026-01-01T12:00:00+00:00"
    entity = hass.data["image"].get_entity(IMAGE)
    assert await entity.async_image() == JPEG

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("custom_components.aiphone_jo.async_unregister", _unregistered)
        await hass.config_entries.async_remove(config_entry.entry_id)
        await hass.async_block_till_done()
    assert key not in hass_storage


async def test_unreadable_stored_image_is_ignored(
    hass: HomeAssistant,
    go2rtc: FakeGo2Rtc,
    config_entry: MockConfigEntry,
    client_holder: ClientHolder,
    hass_storage: dict[str, Any],
) -> None:
    """A damaged stored image does not prevent the setup."""
    hass_storage[f"{DOMAIN}.{config_entry.entry_id}.last_visitor"] = {
        "version": 1,
        "key": f"{DOMAIN}.{config_entry.entry_id}.last_visitor",
        "data": {"image": "not base64 !", "station": "Front"},
    }
    await _setup(hass, config_entry, **{CONF_RING_SNAPSHOT: True})
    assert config_entry.runtime_data.snapshots.image is None


# ------------------------------------------------------------- diagnostics


async def test_diagnostics_history(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    client_holder: ClientHolder,
) -> None:
    """Diagnostics keep the recent connections, problems and last call."""
    client = _client(client_holder)
    await _lose(hass, client, "lost")
    await _restore(hass, client)
    call = await _ring(hass, client)
    call.signals.append(
        CallSignalRecord("23004", None, "example-station", dt_util.now())
    )
    call.status = CallStatus.CANCELLED
    client.current_call = call
    client.emit(CallUpdatedEvent(call))
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, loaded_entry)
    health = diagnostics["health"]
    assert [item["connected"] for item in health["connections"]] == [False, True]
    assert [problem["type"] for problem in health["problems"]] == ["connection_lost"]
    assert health["reconnections"] == 1
    last_call = diagnostics["last_call"]
    assert last_call["station_id"] == 101
    assert last_call["status"] == "cancelled"
    assert [signal["mid"] for signal in last_call["signals"]] == ["23004"]
    dumped = str(diagnostics)
    assert "example-station" not in dumped
    assert call.cid not in dumped
