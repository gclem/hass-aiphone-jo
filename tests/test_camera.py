"""Tests for the camera: still images and WebRTC live view through go2rtc."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.components.camera import async_get_image
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aiphone_jo.const import CONF_IMAGE_REFRESH, CONF_STATION, DOMAIN
from custom_components.aiphone_jo.vendor.aiphone_jo import Output, StationBusyError

from .conftest import ClientHolder, FakeClient, until
from .fake_go2rtc import JPEG, VIEWER_ANSWER, VIEWER_CANDIDATE, FakeGo2Rtc

CAMERA = "camera.aiphone_jo_1mdw_camera"
ACTION_1 = "button.aiphone_jo_1mdw_action_1"


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Detect the end of sessions quickly."""
    monkeypatch.setattr("custom_components.aiphone_jo.media.SESSION_POLL", 0.02)
    monkeypatch.setattr("custom_components.aiphone_jo.media.VIDEO_TIMEOUT", 0.5)


@pytest.fixture
async def camera_entry(
    hass: HomeAssistant,
    go2rtc: FakeGo2Rtc,
    config_entry: MockConfigEntry,
    client_holder: ClientHolder,
) -> MockConfigEntry:
    """Loaded entry with go2rtc available."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry


def _client(holder: ClientHolder) -> FakeClient:
    assert holder.client is not None
    return holder.client


def _age_cache(entry: MockConfigEntry, seconds: float) -> None:
    media = entry.runtime_data.media
    assert media._image_at is not None
    media._image_at -= seconds


async def _image(hass: HomeAssistant) -> bytes:
    return (await async_get_image(hass, CAMERA)).content


async def test_capabilities(
    hass: HomeAssistant, camera_entry: MockConfigEntry, hass_ws_client: Any
) -> None:
    """The camera offers a native WebRTC live view."""
    assert hass.states.get(CAMERA).state != STATE_UNAVAILABLE
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "camera/capabilities", "entity_id": CAMERA})
    response = await client.receive_json()
    assert response["result"] == {"frontend_stream_types": ["web_rtc"]}


async def test_image_opens_short_session(
    hass: HomeAssistant,
    camera_entry: MockConfigEntry,
    client_holder: ClientHolder,
    go2rtc: FakeGo2Rtc,
) -> None:
    """An image opens one session, reads a frame from go2rtc, then closes it."""
    client = _client(client_holder)
    assert await _image(hass) == JPEG
    live = client.lives[0]
    assert live.terminal == 101
    assert live.renew is False
    assert live.exited.is_set()
    assert live.answer is not None
    assert "a=mid:audio" in live.answer
    stream = f"aiphone_jo_{camera_entry.entry_id}"
    assert go2rtc.frames == [stream]
    assert [name for name, _ in go2rtc.ingests] == [stream]


async def test_image_cached_for_refresh_interval(
    hass: HomeAssistant, camera_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """At most one session per refresh interval (30 s by default)."""
    client = _client(client_holder)
    await _image(hass)
    _age_cache(camera_entry, 29)
    await _image(hass)
    assert len(client.lives) == 1
    _age_cache(camera_entry, 2)
    await _image(hass)
    assert len(client.lives) == 2


async def test_refresh_interval_and_station_options(
    hass: HomeAssistant,
    go2rtc: FakeGo2Rtc,
    config_entry: MockConfigEntry,
    client_holder: ClientHolder,
) -> None:
    """The interval and the station come from the options."""
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_STATION: 102, CONF_IMAGE_REFRESH: 120}
    )
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    client = _client(client_holder)
    await _image(hass)
    assert client.lives[0].terminal == 102
    _age_cache(config_entry, 100)
    await _image(hass)
    assert len(client.lives) == 1
    _age_cache(config_entry, 21)
    await _image(hass)
    assert len(client.lives) == 2


async def test_errors_keep_last_image(
    hass: HomeAssistant,
    camera_entry: MockConfigEntry,
    go2rtc: FakeGo2Rtc,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Failures return the last image and are logged once."""
    await _image(hass)
    go2rtc.frame = b""
    caplog.set_level(logging.WARNING)
    for _ in range(3):
        _age_cache(camera_entry, 31)
        assert await _image(hass) == JPEG
    warnings = [r for r in caplog.records if "failed" in r.getMessage()]
    assert len(warnings) == 1
    go2rtc.frame = b"\xff\xd8new\xff\xd9"
    _age_cache(camera_entry, 31)
    assert await _image(hass) == b"\xff\xd8new\xff\xd9"


async def test_no_video(
    hass: HomeAssistant,
    camera_entry: MockConfigEntry,
    client_holder: ClientHolder,
    go2rtc: FakeGo2Rtc,
) -> None:
    """Without video from the station, the session is closed and no image shown."""
    go2rtc.video = False
    with pytest.raises(HomeAssistantError):
        await _image(hass)
    assert _client(client_holder).lives[0].exited.is_set()


async def test_busy_station_fallback(
    hass: HomeAssistant, camera_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """A busy station (486) falls back to the next camera station."""
    client = _client(client_holder)
    client.live_errors[101] = StationBusyError("busy", mid="24021", rslt=486)
    assert await _image(hass) == JPEG
    assert [live.terminal for live in client.lives] == [101, 102]


async def test_session_lock_shared_with_buttons(
    hass: HomeAssistant, camera_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """An output command waits while a session is being opened."""
    client = _client(client_holder)
    client.gate = asyncio.Event()
    image = hass.async_create_task(_image(hass))
    await until(lambda: bool(client.lives))
    press = hass.async_create_task(
        hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: ACTION_1}, blocking=True
        )
    )
    await asyncio.sleep(0.05)
    client.open_door.assert_not_awaited()
    client.gate.set()
    await image
    await press
    client.open_door.assert_awaited_once_with(Output.UNLOCK, 101, 5)


async def test_go2rtc_unavailable(
    hass: HomeAssistant, config_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """Without go2rtc, the camera is unavailable with an explanation."""
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(CAMERA).state == STATE_UNAVAILABLE
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, "go2rtc_unavailable")
    assert not issues.async_get_issue(DOMAIN, "video_unsupported")
    assert hass.states.get(ACTION_1) is not None


async def test_video_unsupported(
    hass: HomeAssistant,
    go2rtc: FakeGo2Rtc,
    config_entry: MockConfigEntry,
    client_holder: ClientHolder,
) -> None:
    """A library without live sessions keeps the camera unavailable."""

    def without_live(client: FakeClient) -> None:
        del client.live_monitor

    client_holder.setup = without_live
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(CAMERA).state == STATE_UNAVAILABLE
    assert ir.async_get(hass).async_get_issue(DOMAIN, "video_unsupported")


async def _offer(ws: Any) -> int:
    await ws.send_json_auto_id(
        {"type": "camera/webrtc/offer", "entity_id": CAMERA, "offer": "v=0 offer"}
    )
    result = await ws.receive_json()
    assert result["success"], result
    return result["id"]


async def _events(ws: Any, count: int) -> list[dict[str, Any]]:
    return [(await ws.receive_json())["event"] for _ in range(count)]


async def test_webrtc_live_view(
    hass: HomeAssistant,
    camera_entry: MockConfigEntry,
    client_holder: ClientHolder,
    go2rtc: FakeGo2Rtc,
    hass_ws_client: Any,
) -> None:
    """Viewers share one live session, stopped when the last one leaves."""
    client = _client(client_holder)
    first_ws = await hass_ws_client(hass)
    first = await _offer(first_ws)
    session, answer, candidate = await _events(first_ws, 3)
    assert session["type"] == "session"
    assert answer == {"type": "answer", "answer": VIEWER_ANSWER}
    assert candidate["type"] == "candidate"
    assert candidate["candidate"]["candidate"] == VIEWER_CANDIDATE
    assert go2rtc.viewer_messages[0] == {"type": "webrtc/offer", "value": "v=0 offer"}
    assert client.lives[0].renew is False
    assert camera_entry.runtime_data.media.live

    await first_ws.send_json_auto_id(
        {
            "type": "camera/webrtc/candidate",
            "entity_id": CAMERA,
            "session_id": session["session_id"],
            "candidate": {"candidate": "candidate:browser", "sdpMLineIndex": 0},
        }
    )
    assert (await first_ws.receive_json())["success"]
    await until(
        lambda: (
            {"type": "webrtc/candidate", "value": "candidate:browser"}
            in go2rtc.viewer_messages
        )
    )

    # Images come from the running live view, without a new session.
    assert await _image(hass) == JPEG

    second_ws = await hass_ws_client(hass)
    second = await _offer(second_ws)
    await _events(second_ws, 3)
    assert len(client.lives) == 1
    assert go2rtc.viewers_total == 2

    await first_ws.send_json_auto_id(
        {"type": "unsubscribe_events", "subscription": first}
    )
    await first_ws.receive_json()
    await until(lambda: go2rtc.viewers_open == 1)
    assert not client.lives[0].exited.is_set()

    await second_ws.send_json_auto_id(
        {"type": "unsubscribe_events", "subscription": second}
    )
    await second_ws.receive_json()
    await until(lambda: client.lives[0].exited.is_set())
    await until(lambda: go2rtc.viewers_open == 0)
    assert not camera_entry.runtime_data.media.live


async def test_webrtc_station_ends_session(
    hass: HomeAssistant,
    camera_entry: MockConfigEntry,
    client_holder: ClientHolder,
    go2rtc: FakeGo2Rtc,
    hass_ws_client: Any,
) -> None:
    """When the station ends the session, viewers are told and disconnected."""
    client = _client(client_holder)
    ws = await hass_ws_client(hass)
    await _offer(ws)
    await _events(ws, 3)
    client.lives[0].end()
    (error,) = await _events(ws, 1)
    assert error["type"] == "error"
    assert error["code"] == "live_ended"
    await until(lambda: client.lives[0].exited.is_set())
    await until(lambda: go2rtc.viewers_open == 0)

    ws2 = await hass_ws_client(hass)
    await _offer(ws2)
    await _events(ws2, 3)
    assert len(client.lives) == 2


async def test_webrtc_start_failure(
    hass: HomeAssistant,
    camera_entry: MockConfigEntry,
    client_holder: ClientHolder,
    hass_ws_client: Any,
) -> None:
    """If no station can be monitored, the viewer gets an error."""
    client = _client(client_holder)
    for terminal in (101, 102):
        client.live_errors[terminal] = StationBusyError("busy", mid="24021", rslt=486)
    ws = await hass_ws_client(hass)
    await _offer(ws)
    session, error = await _events(ws, 2)
    assert session["type"] == "session"
    assert error["type"] == "error"
    assert error["code"] == "live_failed"
    assert not camera_entry.runtime_data.media.live


async def test_unload_stops_live_view(
    hass: HomeAssistant,
    camera_entry: MockConfigEntry,
    client_holder: ClientHolder,
    go2rtc: FakeGo2Rtc,
    hass_ws_client: Any,
) -> None:
    """Unloading the entry ends the live session."""
    client = _client(client_holder)
    ws = await hass_ws_client(hass)
    await _offer(ws)
    await _events(ws, 3)
    assert await hass.config_entries.async_unload(camera_entry.entry_id)
    assert client.lives[0].exited.is_set()
    await until(lambda: go2rtc.viewers_open == 0)


async def test_remove_entry_clears_notices(
    hass: HomeAssistant, config_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """Repair notices disappear with the integration."""
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, "go2rtc_unavailable")
    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert not ir.async_get(hass).async_get_issue(DOMAIN, "go2rtc_unavailable")
