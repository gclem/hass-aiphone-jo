"""Tests for the talk path (microphone to the entrance station)."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aiphone_jo import camera as camera_module
from custom_components.aiphone_jo.const import CONF_CALL_ANSWERING, CONF_CALL_TALK
from custom_components.aiphone_jo.go2rtc import PLACEHOLDER_SOURCE, Go2RtcBridge
from custom_components.aiphone_jo.talk import (
    TalkServer,
    add_candidates,
    fill_rejected,
    keep_codec,
)
from custom_components.aiphone_jo.vendor.aiphone_jo import SessionError
from custom_components.aiphone_jo.vendor.aiphone_jo.sdp import (
    extract_section,
    media_sections,
)

from .conftest import ClientHolder, FakeClient, until
from .fake_go2rtc import TALK_CANDIDATE, TALK_OFFER, FakeGo2Rtc
from .test_call import ANSWER, CALL, HANG_UP, _client, _press, _ring, _session


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Short waits."""
    monkeypatch.setattr("custom_components.aiphone_jo.call.SESSION_POLL", 0.02)
    monkeypatch.setattr("custom_components.aiphone_jo.talk.CANDIDATE_WAIT", 0.3)
    monkeypatch.setattr("custom_components.aiphone_jo.talk.CANDIDATE_QUIET", 0.05)
    monkeypatch.setattr("custom_components.aiphone_jo.talk.TALK_TIMEOUT", 2.0)


@pytest.fixture
async def talk_entry(
    hass: HomeAssistant,
    go2rtc: FakeGo2Rtc,
    config_entry: MockConfigEntry,
    client_holder: ClientHolder,
) -> MockConfigEntry:
    """Loaded entry with call answering and talking enabled."""
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_CALL_ANSWERING: True, CONF_CALL_TALK: True}
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry


# ------------------------------------------------------------------- SDP


def test_keep_codec() -> None:
    """Only PCMU is offered to the station, with its own attributes."""
    section, _ = extract_section(TALK_OFFER, "audio", direction="sendonly")
    kept = keep_codec(section, "PCMU")
    assert "m=audio 9 UDP/TLS/RTP/SAVPF 0\r\n" in kept
    assert "a=rtpmap:0 PCMU/8000" in kept
    assert "a=rtcp-fb:0 transport-cc" in kept
    for removed in ("opus", "PCMA", "a=fmtp:101", "a=rtcp-fb:101"):
        assert removed not in kept
    assert "a=sendonly" in kept
    assert kept.endswith("\r\n")
    assert "\n" not in kept.replace("\r\n", "")
    with pytest.raises(ValueError):
        keep_codec(section, "G722")


def test_add_candidates() -> None:
    """Trickled candidates are added once to each section."""
    section, _ = extract_section(TALK_OFFER, "audio", direction="sendonly")
    once = add_candidates(section, [TALK_CANDIDATE])
    assert once.count(f"a={TALK_CANDIDATE}\r\n") == 1
    assert add_candidates(once, [TALK_CANDIDATE]) == once
    assert add_candidates(section, []) == section


def test_fill_rejected() -> None:
    """Rejected sections get the rtpmap of their format, once."""
    _, restore = extract_section(TALK_OFFER, "audio", direction="sendonly")
    answer = "\r\n".join(
        [
            "v=0",
            "o=- 4 4 IN IP4 198.51.100.1",
            "s=-",
            "t=0 0",
            "m=audio 20004 UDP/TLS/RTP/SAVPF 0",
            "a=mid:2",
            "a=rtpmap:0 PCMU/8000",
            "a=recvonly",
            "",
        ]
    )
    full = restore(answer)
    filled = fill_rejected(TALK_OFFER, full)
    sections = media_sections(filled)
    assert [(s.mid, s.port) for s in sections] == [("0", 0), ("1", 0), ("2", 20004)]
    video = filled.split("m=video 0 ")[1].split("m=audio")[0]
    assert "a=rtpmap:96 H264/90000" in video
    assert "a=rtpmap:97" not in video
    audio = filled.split("m=audio 0 ")[1].split("m=audio")[0]
    assert "a=rtpmap:101 opus/48000/2" in audio
    assert fill_rejected(TALK_OFFER, filled) == filled


async def test_unknown_token_refused(socket_enabled: None) -> None:
    """go2rtc cannot dial a talk path that does not exist."""
    server = TalkServer()
    await server.async_start()
    try:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.WSServerHandshakeError):
                await session.ws_connect(server.url("unknown"))
    finally:
        await server.async_stop()


# ------------------------------------------------------------ with calls


async def test_answer_opens_talk_path(
    hass: HomeAssistant,
    talk_entry: MockConfigEntry,
    client_holder: ClientHolder,
    go2rtc: FakeGo2Rtc,
) -> None:
    """Answering sends the microphone section to the station before the pick-up."""
    client = _client(client_holder)
    await _ring(hass, client)
    await _press(hass, ANSWER)
    session = _session(client)
    assert session.steps == [
        "request_offer",
        "send_answer",
        "ready",
        "add_send_audio",
        "reply",
        "start",
    ]
    stream = f"aiphone_jo_{talk_entry.entry_id}_call"
    sources = go2rtc.streams[stream]
    assert sources[0] == PLACEHOLDER_SOURCE
    assert sources[1].startswith("webrtc:ws://127.0.0.1:")
    assert go2rtc.preloads == [(stream, "PCMU")]
    offer = session.talk_offer
    assert offer is not None
    [section] = media_sections(offer)
    assert (section.kind, section.direction, section.formats) == (
        "audio",
        "sendonly",
        ("0",),
    )
    assert f"a={TALK_CANDIDATE}" in offer
    await until(lambda: go2rtc.talk_answers)
    [answer] = go2rtc.talk_answers
    assert [(s.mid, s.port) for s in media_sections(answer)] == [
        ("0", 0),
        ("1", 0),
        ("2", 20004),
    ]
    assert "a=rtpmap:96 H264/90000" in answer
    assert hass.states.get(CALL).state == "talking"

    await _press(hass, HANG_UP)
    assert go2rtc.preloads_deleted == [stream]
    assert stream in go2rtc.deleted
    assert stream not in go2rtc.streams
    # The talk source is closed with the call.
    async with aiohttp.ClientSession() as http:
        with pytest.raises(aiohttp.WSServerHandshakeError):
            await http.ws_connect(sources[1].removeprefix("webrtc:"))


async def test_talk_failure_keeps_call(
    hass: HomeAssistant,
    talk_entry: MockConfigEntry,
    client_holder: ClientHolder,
    go2rtc: FakeGo2Rtc,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the station refuses the microphone, the call goes on without it."""
    client: FakeClient = _client(client_holder)
    client.call_errors["add_send_audio"] = SessionError("refused")
    await _ring(hass, client)
    await _press(hass, ANSWER)
    assert _session(client).steps[-2:] == ["reply", "start"]
    assert hass.states.get(CALL).state == "talking"
    assert go2rtc.talk_answers == []
    assert "Talk path negotiation failed: SessionError" in caplog.text


async def test_preload_refused_keeps_call(
    hass: HomeAssistant,
    talk_entry: MockConfigEntry,
    client_holder: ClientHolder,
    go2rtc: FakeGo2Rtc,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If go2rtc refuses the preload, answering goes on without waiting."""
    go2rtc.talk_dial = False
    client = _client(client_holder)
    await _ring(hass, client)
    await _press(hass, ANSWER)
    assert "add_send_audio" not in _session(client).steps
    assert hass.states.get(CALL).state == "talking"
    assert "The talk path did not connect" in caplog.text
    assert "could not be opened in time" not in caplog.text


async def test_talk_timeout_keeps_call(
    hass: HomeAssistant,
    talk_entry: MockConfigEntry,
    client_holder: ClientHolder,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If go2rtc never dials the talk source, answering does not wait forever."""
    monkeypatch.setattr("custom_components.aiphone_jo.talk.TALK_TIMEOUT", 0.2)
    hang = asyncio.Event()

    async def preload(*args: object, **kwargs: object) -> None:
        await hang.wait()

    monkeypatch.setattr(Go2RtcBridge, "async_preload", preload)
    client = _client(client_holder)
    await _ring(hass, client)
    await _press(hass, ANSWER)
    assert "add_send_audio" not in _session(client).steps
    assert hass.states.get(CALL).state == "talking"
    assert "The talk path could not be opened in time" in caplog.text
    await _press(hass, HANG_UP)


async def test_talk_disabled(
    hass: HomeAssistant,
    go2rtc: FakeGo2Rtc,
    config_entry: MockConfigEntry,
    client_holder: ClientHolder,
) -> None:
    """Without the option, the call stream has no talk source."""
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_CALL_ANSWERING: True}
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    client = _client(client_holder)
    await _ring(hass, client)
    await _press(hass, ANSWER)
    stream = f"aiphone_jo_{config_entry.entry_id}_call"
    assert go2rtc.streams[stream] == [PLACEHOLDER_SOURCE]
    assert go2rtc.preloads == []
    assert "add_send_audio" not in _session(client).steps


async def test_two_way_audio_capability(
    hass: HomeAssistant, talk_entry: MockConfigEntry
) -> None:
    """The camera declares two-way audio when Home Assistant supports it."""
    if camera_module.CameraEntityCapabilities is None:
        pytest.skip("This Home Assistant version has no two-way audio")
    from homeassistant.helpers import entity_registry as er

    entity_id = er.async_get(hass).async_get_entity_id(
        "camera", "aiphone_jo", f"{talk_entry.unique_id}_camera"
    )
    assert entity_id is not None
    component = hass.data["camera"]
    camera = component.get_entity(entity_id)
    assert camera.camera_entity_capabilities.two_way_audio


async def test_hang_up_while_connecting(
    hass: HomeAssistant,
    talk_entry: MockConfigEntry,
    client_holder: ClientHolder,
    go2rtc: FakeGo2Rtc,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hanging up while the talk path opens withdraws without picking up."""
    monkeypatch.setattr("custom_components.aiphone_jo.talk.TALK_TIMEOUT", 30.0)
    started = asyncio.Event()
    hang = asyncio.Event()

    async def preload(*args: object, **kwargs: object) -> None:
        started.set()
        await hang.wait()

    monkeypatch.setattr(Go2RtcBridge, "async_preload", preload)
    client = _client(client_holder)
    await _ring(hass, client)
    answering = hass.async_create_task(_press(hass, ANSWER))
    await started.wait()
    assert hass.states.get(CALL).state == "connecting"
    await _press(hass, HANG_UP)
    await answering
    await hass.async_block_till_done()
    session = _session(client)
    assert "reply" not in session.steps
    assert "start" not in session.steps
    assert session.steps[-1] == "withdraw"
    state = hass.states.get(CALL)
    assert state.state == "ended"
    assert state.attributes["end_reason"] == "ended_by_client"
    stream = f"aiphone_jo_{talk_entry.entry_id}_call"
    assert stream not in go2rtc.streams
    assert not talk_entry.runtime_data.media.live
