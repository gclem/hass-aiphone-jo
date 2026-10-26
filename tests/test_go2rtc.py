"""Tests for the go2rtc bridge against a fake go2rtc server."""

from __future__ import annotations

from types import SimpleNamespace

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.aiphone_jo.const import (
    CONF_GO2RTC_PASSWORD,
    CONF_GO2RTC_URL,
    CONF_GO2RTC_USERNAME,
)
from custom_components.aiphone_jo.go2rtc import (
    PLACEHOLDER_SOURCE,
    Go2RtcBridge,
    Go2RtcError,
    Go2RtcServer,
    async_check_server,
    async_get_server,
)

from .conftest import until
from .fake_go2rtc import JANUS_OFFER, JPEG, VIEWER_ANSWER, VIEWER_CANDIDATE, FakeGo2Rtc


@pytest.fixture
async def bridge(hass: HomeAssistant, go2rtc: FakeGo2Rtc) -> Go2RtcBridge:
    """Bridge on the fake server."""
    server = async_get_server(hass, {})
    assert server is not None
    return Go2RtcBridge(server, "aiphone_jo_test")


async def test_server_from_home_assistant(
    hass: HomeAssistant, go2rtc: FakeGo2Rtc
) -> None:
    """The go2rtc run by Home Assistant is used by default."""
    server = async_get_server(hass, {})
    assert server is not None
    assert server.url == hass.data["go2rtc"].url
    await async_check_server(server)


async def test_server_from_options(hass: HomeAssistant) -> None:
    """A URL in the options takes precedence, with optional credentials."""
    hass.data["go2rtc"] = SimpleNamespace(
        url="http://192.0.2.1:11984/", session=async_get_clientsession(hass)
    )
    server = async_get_server(
        hass,
        {
            CONF_GO2RTC_URL: "http://192.0.2.2:1984",
            CONF_GO2RTC_USERNAME: "user",
            CONF_GO2RTC_PASSWORD: "example-password",
        },
    )
    assert server is not None
    assert server.url == "http://192.0.2.2:1984"
    assert server.session is not async_get_clientsession(hass)
    assert server.api("api/streams") == "http://192.0.2.2:1984/api/streams"


@pytest.mark.parametrize(
    "data", [None, SimpleNamespace(url=None, session=None), object()]
)
async def test_no_server(hass: HomeAssistant, data: object) -> None:
    """Without go2rtc, there is no server."""
    if data is not None:
        hass.data["go2rtc"] = data
    assert async_get_server(hass, {}) is None


async def test_check_server_failure(hass: HomeAssistant, socket_enabled: None) -> None:
    """An unreachable server is reported."""
    server = Go2RtcServer("http://127.0.0.1:9/", async_get_clientsession(hass))
    with pytest.raises(Go2RtcError):
        await async_check_server(server)


async def test_ensure_stream(bridge: Go2RtcBridge, go2rtc: FakeGo2Rtc) -> None:
    """The stream is created once, with the placeholder source."""
    await bridge.async_ensure_stream()
    await bridge.async_ensure_stream()
    assert go2rtc.puts == [("aiphone_jo_test", PLACEHOLDER_SOURCE)]
    assert go2rtc.streams["aiphone_jo_test"] == [PLACEHOLDER_SOURCE]


async def test_ingest(bridge: Go2RtcBridge, go2rtc: FakeGo2Rtc) -> None:
    """The offer is normalized for go2rtc and the answer restored for the relay."""
    await bridge.async_ensure_stream()
    answer = await bridge.async_ingest(JANUS_OFFER)
    name, sent = go2rtc.ingests[0]
    assert name == "aiphone_jo_test"
    assert "a=mid:0" in sent
    assert "sdes:mid" not in sent
    assert "a=group:BUNDLE audio video" in answer
    assert "a=mid:video" in answer


async def test_ingest_refused(bridge: Go2RtcBridge, go2rtc: FakeGo2Rtc) -> None:
    """A missing stream (404) is an error."""
    with pytest.raises(Go2RtcError):
        await bridge.async_ingest(JANUS_OFFER)


async def test_wait_video(bridge: Go2RtcBridge, go2rtc: FakeGo2Rtc) -> None:
    """Video is detected from the producer receivers."""
    await bridge.async_ensure_stream()
    assert not await bridge.async_video_flowing()
    await bridge.async_ingest(JANUS_OFFER)
    await bridge.async_wait_video(1)
    go2rtc.video = False
    with pytest.raises(Go2RtcError):
        await bridge.async_wait_video(0.3, interval=0.05)


async def test_frame(bridge: Go2RtcBridge, go2rtc: FakeGo2Rtc) -> None:
    """An empty frame (no producer) is an error."""
    await bridge.async_ensure_stream()
    with pytest.raises(Go2RtcError):
        await bridge.async_frame()
    await bridge.async_ingest(JANUS_OFFER)
    assert await bridge.async_frame() == JPEG


async def test_viewer(bridge: Go2RtcBridge, go2rtc: FakeGo2Rtc) -> None:
    """Viewer signaling goes through the WebSocket API."""
    received: list[dict] = []
    viewer = await bridge.async_open_viewer(received.append)
    await viewer.async_send("webrtc/offer", "v=0 browser-offer")
    await viewer.async_send("webrtc/candidate", "candidate:browser")
    await until(lambda: len(received) == 2)
    assert received == [
        {"type": "webrtc/answer", "value": VIEWER_ANSWER},
        {"type": "webrtc/candidate", "value": VIEWER_CANDIDATE},
    ]
    assert go2rtc.viewer_messages == [
        {"type": "webrtc/offer", "value": "v=0 browser-offer"},
        {"type": "webrtc/candidate", "value": "candidate:browser"},
    ]
    await viewer.async_close()
    await until(lambda: go2rtc.viewers_open == 0)


async def test_unreachable_viewer(hass: HomeAssistant, socket_enabled: None) -> None:
    """An unreachable server raises Go2RtcError for viewers."""
    server = Go2RtcServer("http://127.0.0.1:9/", aiohttp.ClientSession())
    try:
        with pytest.raises(Go2RtcError):
            await Go2RtcBridge(server, "x").async_open_viewer(lambda _: None)
    finally:
        await server.session.close()
