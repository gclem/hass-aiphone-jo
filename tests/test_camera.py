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
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aiphone_jo.const import CONF_IMAGE_REFRESH, CONF_STATION, DOMAIN
from custom_components.aiphone_jo.media import CAMERA_BUSY, LIVE_ENDED, LIVE_STOPPED
from custom_components.aiphone_jo.vendor.aiphone_jo import (
    AiphoneJoError,
    Output,
    StationBusyError,
)

from .conftest import ClientHolder, FakeClient, until
from .fake_go2rtc import JPEG, VIEWER_ANSWER, VIEWER_CANDIDATE, FakeGo2Rtc

CAMERA = "camera.aiphone_jo_1mdw_camera"
BACK_CAMERA = "camera.aiphone_jo_1mdw_back_camera"
FRONT_CAMERA = "camera.aiphone_jo_1mdw_front_camera"
ACTION_1 = "button.aiphone_jo_1mdw_action_1"


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Detect the end of sessions quickly."""
    monkeypatch.setattr("custom_components.aiphone_jo.media.SESSION_POLL", 0.02)
    monkeypatch.setattr("custom_components.aiphone_jo.media.VIDEO_TIMEOUT", 0.5)
    monkeypatch.setattr("custom_components.aiphone_jo.media.VIEWER_GRACE", 0.05)


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


def _age_cache(entry: MockConfigEntry, seconds: float, station: int = 101) -> None:
    images = entry.runtime_data.media._images
    image, at = images[station]
    images[station] = (image, at - seconds)


async def _image(hass: HomeAssistant, entity_id: str = CAMERA) -> bytes:
    return (await async_get_image(hass, entity_id)).content


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
    # Renewable, so that a viewer arriving meanwhile can keep it.
    assert live.renew is True
    assert live.exited.is_set()
    assert live.answer is not None
    assert "a=mid:audio" in live.answer
    stream = f"aiphone_jo_{camera_entry.entry_id}"
    assert go2rtc.frames == [stream]
    assert [name for name, _ in go2rtc.ingests] == [stream]
    assert [name for name, _ in go2rtc.puts] == [stream, f"{stream}_live"]
    assert go2rtc.streams[f"{stream}_live"][0].startswith(f"ffmpeg:{stream}#")


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
    """The interval and the station of the Camera entity come from the options."""
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_STATION: 102, CONF_IMAGE_REFRESH: 120}
    )
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    client = _client(client_holder)
    await _image(hass)
    assert client.lives[0].terminal == 102
    _age_cache(config_entry, 100, 102)
    await _image(hass)
    assert len(client.lives) == 1
    _age_cache(config_entry, 21, 102)
    await _image(hass)
    assert len(client.lives) == 2
    await _image(hass, FRONT_CAMERA)
    assert client.lives[2].terminal == 101


async def test_one_camera_per_station(
    hass: HomeAssistant, camera_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """Each entrance station has its own camera; Camera shows the preferred one."""
    registry = er.async_get(hass)
    unique_id = camera_entry.unique_id
    assert registry.async_get(CAMERA).unique_id == f"{unique_id}_camera"
    assert registry.async_get(BACK_CAMERA).unique_id == f"{unique_id}_camera_102"
    assert hass.states.get(CAMERA).attributes["station"] == "Front"
    back = hass.states.get(BACK_CAMERA)
    assert back.attributes["station"] == "Back"
    assert back.attributes["friendly_name"] == "Aiphone JO-1MDW Back camera"
    client = _client(client_holder)
    assert await _image(hass, BACK_CAMERA) == JPEG
    assert await _image(hass) == JPEG
    assert [live.terminal for live in client.lives] == [102, 101]
    # Each station has its own cached image.
    assert await _image(hass, BACK_CAMERA) == JPEG
    assert len(client.lives) == 2


async def test_station_option_moves_the_camera_entity(
    hass: HomeAssistant, camera_entry: MockConfigEntry
) -> None:
    """Choosing another station keeps the Camera entity, which follows it."""
    hass.config_entries.async_update_entry(camera_entry, options={CONF_STATION: 102})
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    unique_id = camera_entry.unique_id
    cameras = {
        entity.unique_id: entity.entity_id
        for entity in er.async_entries_for_config_entry(registry, camera_entry.entry_id)
        if entity.domain == "camera"
    }
    assert cameras == {
        f"{unique_id}_camera": CAMERA,
        f"{unique_id}_camera_101": FRONT_CAMERA,
    }
    assert hass.states.get(CAMERA).attributes["station"] == "Back"


async def test_images_one_session_at_a_time(
    hass: HomeAssistant,
    go2rtc: FakeGo2Rtc,
    config_entry: MockConfigEntry,
    client_holder: ClientHolder,
) -> None:
    """Images of both cameras requested together open one session after the other."""
    overlaps: list[int | None] = []

    def watch(client: FakeClient) -> None:
        live_monitor = client.live_monitor

        def checked(*args: Any, **kwargs: Any) -> Any:
            if not all(live.exited.is_set() for live in client.lives):
                overlaps.append(args[1])
            return live_monitor(*args, **kwargs)

        client.live_monitor = checked

    client_holder.setup = watch
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    images = await asyncio.gather(_image(hass), _image(hass, BACK_CAMERA))
    assert images == [JPEG, JPEG]
    client = _client(client_holder)
    assert sorted(live.terminal for live in client.lives) == [101, 102]
    assert all(live.exited.is_set() for live in client.lives)
    assert overlaps == []


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


async def test_busy_station_keeps_its_camera(
    hass: HomeAssistant, camera_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """A busy station (486) never shows another station's image."""
    client = _client(client_holder)
    await _image(hass)
    _age_cache(camera_entry, 31)
    client.live_errors[101] = StationBusyError("busy", mid="24021", rslt=486)
    assert await _image(hass) == JPEG
    assert [live.terminal for live in client.lives] == [101, 101]


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


async def _offer(ws: Any, entity_id: str = CAMERA) -> int:
    await ws.send_json_auto_id(
        {"type": "camera/webrtc/offer", "entity_id": entity_id, "offer": "v=0 offer"}
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
    assert client.lives[0].renew is True
    relay = f"aiphone_jo_{camera_entry.entry_id}_live"
    assert go2rtc.viewer_streams == [relay]
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
    assert go2rtc.frames == [relay]

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


async def test_live_image_waits_for_video(
    hass: HomeAssistant,
    camera_entry: MockConfigEntry,
    client_holder: ClientHolder,
    go2rtc: FakeGo2Rtc,
    hass_ws_client: Any,
) -> None:
    """While the live view restarts (session renewed), images wait for video."""
    ws = await hass_ws_client(hass)
    await _offer(ws)
    await _events(ws, 3)
    relay = f"aiphone_jo_{camera_entry.entry_id}_live"
    go2rtc.video = False
    image = hass.async_create_task(_image(hass))
    await asyncio.sleep(0.3)
    assert not image.done()
    assert go2rtc.frames == []
    go2rtc.video = True
    assert await image == JPEG
    assert go2rtc.frames == [relay]


async def test_webrtc_continuous(
    hass: HomeAssistant,
    camera_entry: MockConfigEntry,
    client_holder: ClientHolder,
    go2rtc: FakeGo2Rtc,
    hass_ws_client: Any,
) -> None:
    """Renewed sessions are pushed into the stream; viewers stay connected."""
    client = _client(client_holder)
    ws = await hass_ws_client(hass)
    await _offer(ws)
    await _events(ws, 3)
    live = client.lives[0]
    stream = f"aiphone_jo_{camera_entry.entry_id}"
    for _ in range(2):
        live.end()
        await asyncio.sleep(0.1)
        await live.renewed()
    assert [name for name, _ in go2rtc.ingests] == [stream] * 3
    assert len(live.answers) == 3
    assert len(client.lives) == 1
    assert go2rtc.viewers_open == 1
    assert not live.exited.is_set()
    assert camera_entry.runtime_data.media.live

    # Renewal gave up: viewers are told and disconnected.
    live.give_up(AiphoneJoError("unreachable"))
    (error,) = await _events(ws, 1)
    assert error == {"type": "error", "code": "live_ended", "message": LIVE_STOPPED}
    await until(lambda: live.exited.is_set())
    await until(lambda: go2rtc.viewers_open == 0)
    assert not camera_entry.runtime_data.media.live


async def test_webrtc_viewer_grace(
    hass: HomeAssistant,
    camera_entry: MockConfigEntry,
    client_holder: ClientHolder,
    go2rtc: FakeGo2Rtc,
    hass_ws_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The session outlives the last viewer briefly, for a viewer coming back."""
    monkeypatch.setattr("custom_components.aiphone_jo.media.VIEWER_GRACE", 0.5)
    client = _client(client_holder)
    ws = await hass_ws_client(hass)
    first = await _offer(ws)
    await _events(ws, 3)
    await ws.send_json_auto_id({"type": "unsubscribe_events", "subscription": first})
    await ws.receive_json()
    await until(lambda: go2rtc.viewers_open == 0)
    assert camera_entry.runtime_data.media.live

    await _offer(ws)
    await _events(ws, 3)
    assert len(client.lives) == 1
    await asyncio.sleep(0.6)
    assert not client.lives[0].exited.is_set()


async def test_webrtc_without_relay(
    hass: HomeAssistant,
    camera_entry: MockConfigEntry,
    client_holder: ClientHolder,
    go2rtc: FakeGo2Rtc,
    hass_ws_client: Any,
) -> None:
    """Without go2rtc's RTSP server, the live view stops with the session."""
    go2rtc.rtsp = False
    client = _client(client_holder)
    ws = await hass_ws_client(hass)
    await _offer(ws)
    await _events(ws, 3)
    stream = f"aiphone_jo_{camera_entry.entry_id}"
    assert client.lives[0].renew is False
    assert go2rtc.viewer_streams == [stream]
    assert [name for name, _ in go2rtc.puts] == [stream]
    client.lives[0].end()
    (error,) = await _events(ws, 1)
    assert error == {"type": "error", "code": "live_ended", "message": LIVE_ENDED}
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
    """If the station cannot be monitored, the viewer gets an error."""
    client = _client(client_holder)
    client.live_errors[101] = StationBusyError("busy", mid="24021", rslt=486)
    ws = await hass_ws_client(hass)
    await _offer(ws)
    session, error = await _events(ws, 2)
    assert session["type"] == "session"
    assert error["type"] == "error"
    assert error["code"] == "live_failed"
    assert not camera_entry.runtime_data.media.live


async def test_live_view_of_another_station(
    hass: HomeAssistant,
    camera_entry: MockConfigEntry,
    client_holder: ClientHolder,
    go2rtc: FakeGo2Rtc,
    hass_ws_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One station at a time: a live view keeps the other cameras on hold."""
    monkeypatch.setattr("custom_components.aiphone_jo.media.VIEWER_GRACE", 5)
    client = _client(client_holder)
    media = camera_entry.runtime_data.media
    assert await _image(hass, BACK_CAMERA) == JPEG
    _age_cache(camera_entry, 31, 102)

    front_ws = await hass_ws_client(hass)
    front = await _offer(front_ws)
    await _events(front_ws, 3)
    assert [live.terminal for live in client.lives] == [102, 101]
    assert hass.states.get(CAMERA).state == "streaming"
    assert hass.states.get(BACK_CAMERA).state != "streaming"

    # The other camera keeps its last image and cannot start a live view.
    assert await _image(hass, BACK_CAMERA) == JPEG
    back_ws = await hass_ws_client(hass)
    await _offer(back_ws, BACK_CAMERA)
    session, error = await _events(back_ws, 2)
    assert session["type"] == "session"
    assert error == {"type": "error", "code": "camera_busy", "message": CAMERA_BUSY}
    assert len(client.lives) == 2
    assert media.live_on(101)

    # Once the live view is closed, the other station takes the session over
    # without waiting for the grace period.
    await front_ws.send_json_auto_id(
        {"type": "unsubscribe_events", "subscription": front}
    )
    await front_ws.receive_json()
    await until(lambda: go2rtc.viewers_open == 0)
    assert media.live_on(101)
    await _offer(back_ws, BACK_CAMERA)
    await _events(back_ws, 3)
    assert client.lives[1].exited.is_set()
    assert client.lives[2].terminal == 102
    assert media.live_on(102)
    assert hass.states.get(BACK_CAMERA).state == "streaming"


async def test_first_image_waits_for_the_session(
    hass: HomeAssistant,
    camera_entry: MockConfigEntry,
    client_holder: ClientHolder,
    go2rtc: FakeGo2Rtc,
    hass_ws_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A camera without any image waits for another station's live view."""
    monkeypatch.setattr("custom_components.aiphone_jo.media.FIRST_IMAGE_POLL", 0.05)
    client = _client(client_holder)
    front_ws = await hass_ws_client(hass)
    front = await _offer(front_ws)
    await _events(front_ws, 3)
    assert [live.terminal for live in client.lives] == [101]

    image = hass.async_create_task(_image(hass, BACK_CAMERA))
    await asyncio.sleep(0.3)
    assert not image.done()
    await front_ws.send_json_auto_id(
        {"type": "unsubscribe_events", "subscription": front}
    )
    await front_ws.receive_json()
    assert await image == JPEG
    assert [live.terminal for live in client.lives] == [101, 102]


async def test_first_image_gives_up(
    hass: HomeAssistant,
    camera_entry: MockConfigEntry,
    client_holder: ClientHolder,
    hass_ws_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without any image, a camera still busy after the wait has no image."""
    monkeypatch.setattr("custom_components.aiphone_jo.media.FIRST_IMAGE_WAIT", 0.3)
    monkeypatch.setattr("custom_components.aiphone_jo.media.FIRST_IMAGE_POLL", 0.05)
    client = _client(client_holder)
    front_ws = await hass_ws_client(hass)
    await _offer(front_ws)
    await _events(front_ws, 3)
    with pytest.raises(HomeAssistantError):
        await _image(hass, BACK_CAMERA)
    assert [live.terminal for live in client.lives] == [101]


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
