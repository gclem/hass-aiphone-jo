"""Tests for the image of the visitor on each ring."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import Event, HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aiphone_jo.const import (
    CONF_CALL_ANSWERING,
    CONF_RING_SNAPSHOT,
    EVENT_RING_BUS,
)
from custom_components.aiphone_jo.vendor.aiphone_jo import (
    CallStatus,
    SessionError,
)

from .conftest import ClientHolder, until
from .fake_go2rtc import JPEG, FakeGo2Rtc
from .test_call import ANSWER, CALL, REJECT, _client, _press, _ring

IMAGE = "image.aiphone_jo_1mdw_last_visitor"
PREVIEW = ["request_offer", "send_answer", "withdraw"]
# Never sent by a capture: they would end the call for every phone.
FORBIDDEN = {"reject", "close", "ready", "reply", "start"}


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Short waits."""
    monkeypatch.setattr("custom_components.aiphone_jo.call.SESSION_POLL", 0.02)
    monkeypatch.setattr("custom_components.aiphone_jo.media.SESSION_POLL", 0.02)
    monkeypatch.setattr("custom_components.aiphone_jo.snapshot.SNAPSHOT_TIMEOUT", 2.0)


@pytest.fixture
def ring_events(hass: HomeAssistant) -> list[Event]:
    """Collected aiphone_jo_ring events."""
    events: list[Event] = []
    hass.bus.async_listen(EVENT_RING_BUS, events.append)
    return events


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, **options: Any) -> None:
    hass.config_entries.async_update_entry(entry, options=options)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_disabled(
    hass: HomeAssistant,
    go2rtc: FakeGo2Rtc,
    config_entry: MockConfigEntry,
    client_holder: ClientHolder,
    ring_events: list[Event],
) -> None:
    """Without the option, the event is fired at once and nothing is captured."""
    await _setup(hass, config_entry)
    client = _client(client_holder)
    await _ring(hass, client)
    assert hass.states.get(IMAGE) is None
    assert client.call_sessions == []
    [event] = ring_events
    assert event.data["snapshot"] is False
    assert event.data["image_url"] is None
    assert event.data["station"] == "Front"
    assert event.data["entry_id"] == config_entry.entry_id
    assert event.data["device_id"]


async def test_capture_without_answering(
    hass: HomeAssistant,
    go2rtc: FakeGo2Rtc,
    config_entry: MockConfigEntry,
    client_holder: ClientHolder,
    ring_events: list[Event],
) -> None:
    """The call's video is read, then the relay is left: the call keeps ringing."""
    await _setup(hass, config_entry, **{CONF_RING_SNAPSHOT: True})
    client = _client(client_holder)
    call = await _ring(hass, client)
    await until(lambda: ring_events)
    [session] = client.call_sessions
    assert session.steps == PREVIEW
    assert call.status is CallStatus.RINGING
    stream = f"aiphone_jo_{config_entry.entry_id}_ring"
    assert [name for name, _ in go2rtc.ingests] == [stream]
    assert stream in go2rtc.deleted
    [event] = ring_events
    assert event.data["snapshot"] is True
    assert event.data["image_entity_id"] == IMAGE
    assert event.data["image_url"] == f"/api/image_proxy/{IMAGE}"
    state = hass.states.get(IMAGE)
    assert state.attributes["station"] == "Front"
    assert state.attributes["ringing_at"] == "2026-01-01T12:00:00+00:00"
    runtime = config_entry.runtime_data
    assert runtime.snapshots.image == JPEG
    entity = hass.data["image"].get_entity(IMAGE)
    assert await entity.async_image() == JPEG
    # The camera shows the visitor too, without a new session.
    assert await runtime.media.async_image() == JPEG
    assert len(client.call_sessions) == 1
    assert hass.states.get("binary_sensor.aiphone_jo_1mdw_ringing").state == "on"


async def test_no_video(
    hass: HomeAssistant,
    go2rtc: FakeGo2Rtc,
    config_entry: MockConfigEntry,
    client_holder: ClientHolder,
    ring_events: list[Event],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Without video before the timeout, the capture fails cleanly."""
    monkeypatch.setattr("custom_components.aiphone_jo.snapshot.SNAPSHOT_TIMEOUT", 0.3)
    go2rtc.video = False
    await _setup(hass, config_entry, **{CONF_RING_SNAPSHOT: True})
    client = _client(client_holder)
    await _ring(hass, client)
    await until(lambda: ring_events)
    assert client.call_sessions[0].steps == PREVIEW
    assert ring_events[0].data["snapshot"] is False
    assert ring_events[0].data["image_url"] is None
    assert "No image of the visitor" in caplog.text
    assert config_entry.runtime_data.session_lock.locked() is False


async def test_library_error(
    hass: HomeAssistant,
    go2rtc: FakeGo2Rtc,
    config_entry: MockConfigEntry,
    client_holder: ClientHolder,
    ring_events: list[Event],
) -> None:
    """A refused relay offer leaves the relay and fires the event without image."""
    await _setup(hass, config_entry, **{CONF_RING_SNAPSHOT: True})
    client = _client(client_holder)
    client.call_errors["request_offer"] = SessionError("refused")
    await _ring(hass, client)
    await until(lambda: ring_events)
    steps = client.call_sessions[0].steps
    assert steps[-1] == "withdraw"
    assert not FORBIDDEN & set(steps)
    assert ring_events[0].data["snapshot"] is False


async def test_answer_during_capture(
    hass: HomeAssistant,
    go2rtc: FakeGo2Rtc,
    config_entry: MockConfigEntry,
    client_holder: ClientHolder,
    ring_events: list[Event],
) -> None:
    """Answering stops the capture, which then reads the answered call."""
    go2rtc.video = False
    await _setup(
        hass, config_entry, **{CONF_RING_SNAPSHOT: True, CONF_CALL_ANSWERING: True}
    )
    client = _client(client_holder)
    await _ring(hass, client)
    await until(lambda: "send_answer" in client.call_sessions[0].steps)
    await _press(hass, ANSWER)
    preview, answered = client.call_sessions
    assert preview.steps == PREVIEW
    assert answered.steps[-1] == "start"
    assert hass.states.get(CALL).state == "talking"
    go2rtc.video = True
    await until(lambda: ring_events)
    assert ring_events[0].data["snapshot"] is True
    assert len(client.call_sessions) == 2
    call_stream = f"aiphone_jo_{config_entry.entry_id}_call"
    assert go2rtc.frames[-1] == call_stream


async def test_reject_during_capture(
    hass: HomeAssistant,
    go2rtc: FakeGo2Rtc,
    config_entry: MockConfigEntry,
    client_holder: ClientHolder,
    ring_events: list[Event],
) -> None:
    """Rejecting stops the capture first; the event is fired without image."""
    go2rtc.video = False
    await _setup(
        hass, config_entry, **{CONF_RING_SNAPSHOT: True, CONF_CALL_ANSWERING: True}
    )
    client = _client(client_holder)
    await _ring(hass, client)
    await until(lambda: "send_answer" in client.call_sessions[0].steps)
    await _press(hass, REJECT)
    await hass.async_block_till_done()
    preview, rejecting = client.call_sessions
    assert preview.steps == PREVIEW
    assert rejecting.steps == ["reject"]
    await until(lambda: ring_events)
    assert ring_events[0].data["snapshot"] is False


async def test_unload_during_capture(
    hass: HomeAssistant,
    go2rtc: FakeGo2Rtc,
    config_entry: MockConfigEntry,
    client_holder: ClientHolder,
    ring_events: list[Event],
) -> None:
    """Unloading leaves the relay without ending the call."""
    go2rtc.video = False
    await _setup(hass, config_entry, **{CONF_RING_SNAPSHOT: True})
    client = _client(client_holder)
    await _ring(hass, client)
    await until(lambda: "send_answer" in client.call_sessions[0].steps)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert client.call_sessions[0].steps == PREVIEW
    assert ring_events == []


async def test_live_view_busy(
    hass: HomeAssistant,
    go2rtc: FakeGo2Rtc,
    config_entry: MockConfigEntry,
    client_holder: ClientHolder,
    ring_events: list[Event],
) -> None:
    """An open live view keeps its session; the ring has no image."""
    await _setup(hass, config_entry, **{CONF_RING_SNAPSHOT: True})
    client = _client(client_holder)
    media = config_entry.runtime_data.media
    await media.async_acquire()
    try:
        await _ring(hass, client)
        await until(lambda: ring_events)
        assert client.call_sessions == []
        assert ring_events[0].data["snapshot"] is False
    finally:
        await media.async_release()


async def test_answer_right_after_failed_capture(
    hass: HomeAssistant,
    go2rtc: FakeGo2Rtc,
    config_entry: MockConfigEntry,
    client_holder: ClientHolder,
    ring_events: list[Event],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out capture has left the call before it reports, even slowly."""
    monkeypatch.setattr("custom_components.aiphone_jo.snapshot.SNAPSHOT_TIMEOUT", 0.2)
    go2rtc.video = False
    await _setup(
        hass, config_entry, **{CONF_RING_SNAPSHOT: True, CONF_CALL_ANSWERING: True}
    )
    client = _client(client_holder)
    client.withdraw_delay = 0.3
    await _ring(hass, client)
    await until(lambda: ring_events)
    assert not client.call_session_open
    await _press(hass, ANSWER)
    assert hass.states.get(CALL).state == "talking"


async def test_answer_during_slow_withdraw(
    hass: HomeAssistant,
    go2rtc: FakeGo2Rtc,
    config_entry: MockConfigEntry,
    client_holder: ClientHolder,
    ring_events: list[Event],
) -> None:
    """Answering while the capture leaves the call waits for it."""
    await _setup(
        hass, config_entry, **{CONF_RING_SNAPSHOT: True, CONF_CALL_ANSWERING: True}
    )
    client = _client(client_holder)
    client.withdraw_delay = 0.3
    await _ring(hass, client)
    await until(lambda: "withdraw" in client.call_sessions[0].steps)
    await _press(hass, ANSWER)
    assert hass.states.get(CALL).state == "talking"
    await until(lambda: ring_events)
    assert len(ring_events) == 1


async def test_new_ring_replaces_capture(
    hass: HomeAssistant,
    go2rtc: FakeGo2Rtc,
    config_entry: MockConfigEntry,
    client_holder: ClientHolder,
    ring_events: list[Event],
) -> None:
    """A new ring reports the previous one without image, then captures."""
    go2rtc.video = False
    await _setup(hass, config_entry, **{CONF_RING_SNAPSHOT: True})
    client = _client(client_holder)
    await _ring(hass, client)
    await until(lambda: "send_answer" in client.call_sessions[0].steps)
    await _ring(hass, client)
    assert [e.data["snapshot"] for e in ring_events] == [False]
    go2rtc.video = True
    await until(lambda: len(ring_events) == 2)
    assert ring_events[1].data["snapshot"] is True
    assert client.call_sessions[0].steps == PREVIEW
    await hass.async_block_till_done()
    assert len(ring_events) == 2


async def test_leave_with_older_library(
    hass: HomeAssistant,
    go2rtc: FakeGo2Rtc,
    config_entry: MockConfigEntry,
    client_holder: ClientHolder,
    ring_events: list[Event],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sessions without the public withdraw() are left the same way."""
    from .conftest import FakeCallSession

    monkeypatch.setattr(
        FakeCallSession, "_withdraw", FakeCallSession.withdraw, raising=False
    )
    monkeypatch.delattr(FakeCallSession, "withdraw")
    await _setup(hass, config_entry, **{CONF_RING_SNAPSHOT: True})
    client = _client(client_holder)
    await _ring(hass, client)
    await until(lambda: ring_events)
    assert client.call_sessions[0].steps == PREVIEW
    assert not client.call_session_open
