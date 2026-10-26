"""Tests for the camera (cached images and MJPEG live view)."""

from __future__ import annotations

import asyncio
import logging

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

from custom_components.aiphone_jo.api import MediaTimeoutError, MediaUnavailableError
from custom_components.aiphone_jo.const import CONF_IMAGE_REFRESH, CONF_STATION, DOMAIN
from custom_components.aiphone_jo.vendor.aiphone_jo import Output, StationBusyError

from .conftest import ClientHolder, FakeClient

CAMERA = "camera.aiphone_jo_1mdw_camera"
STREAM_URL = f"/api/camera_proxy_stream/{CAMERA}"


def _client(holder: ClientHolder) -> FakeClient:
    assert holder.client is not None
    return holder.client


def _age_cache(entry: MockConfigEntry, seconds: float) -> None:
    media = entry.runtime_data.media
    assert media._image_at is not None
    media._image_at -= seconds


async def _image(hass: HomeAssistant) -> bytes:
    return (await async_get_image(hass, CAMERA)).content


async def test_image_cached_for_refresh_interval(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """A snapshot is taken at most once per refresh interval (30 s by default)."""
    client = _client(client_holder)
    assert hass.states.get(CAMERA).state != STATE_UNAVAILABLE

    assert await _image(hass) == b"jpeg-snapshot"
    client.snapshot.assert_awaited_once_with(101, timeout=15.0)

    _age_cache(loaded_entry, 29)
    await _image(hass)
    assert client.snapshot.await_count == 1

    _age_cache(loaded_entry, 2)
    client.snapshot.return_value = b"jpeg-new"
    assert await _image(hass) == b"jpeg-new"
    assert client.snapshot.await_count == 2


async def test_refresh_interval_option(
    hass: HomeAssistant, config_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """The refresh interval comes from the options and the preferred station."""
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_STATION: 102, CONF_IMAGE_REFRESH: 120}
    )
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    client = _client(client_holder)

    await _image(hass)
    client.snapshot.assert_awaited_once_with(102, timeout=15.0)
    _age_cache(config_entry, 100)
    await _image(hass)
    assert client.snapshot.await_count == 1
    _age_cache(config_entry, 21)
    await _image(hass)
    assert client.snapshot.await_count == 2


async def test_options_change_applies_without_restart(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """Changing the interval reloads the entry with the new value."""
    hass.config_entries.async_update_entry(
        loaded_entry, options={CONF_IMAGE_REFRESH: 600}
    )
    await hass.async_block_till_done()
    assert loaded_entry.state is ConfigEntryState.LOADED
    assert loaded_entry.runtime_data.media.refresh_interval == 600


async def test_errors_keep_last_image(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    client_holder: ClientHolder,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Failures return the last image and are logged once."""
    client = _client(client_holder)
    await _image(hass)
    client.snapshot.side_effect = MediaTimeoutError("no image")
    caplog.set_level(logging.WARNING)

    for _ in range(3):
        _age_cache(loaded_entry, 31)
        assert await _image(hass) == b"jpeg-snapshot"
    warnings = [r for r in caplog.records if "not refreshed" in r.getMessage()]
    assert len(warnings) == 1

    client.snapshot.side_effect = None
    client.snapshot.return_value = b"jpeg-back"
    _age_cache(loaded_entry, 31)
    assert await _image(hass) == b"jpeg-back"


async def test_error_without_image(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """Without any image yet, the camera reports that none is available."""
    _client(client_holder).snapshot.side_effect = MediaTimeoutError("no image")
    with pytest.raises(HomeAssistantError):
        await _image(hass)


async def test_busy_station_fallback(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """A busy station (486) falls back to the next camera station."""
    client = _client(client_holder)
    client.snapshot.side_effect = [
        StationBusyError("busy", mid="24021", rslt=486),
        b"jpeg-back-station",
    ]
    assert await _image(hass) == b"jpeg-back-station"
    assert [c.args[0] for c in client.snapshot.await_args_list] == [101, 102]


async def test_session_lock_shared_with_buttons(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """An output command waits for the running snapshot."""
    client = _client(client_holder)
    release = asyncio.Event()
    events: list[str] = []

    started = asyncio.Event()

    async def snapshot(*args: object, **kwargs: object) -> bytes:
        events.append("snapshot start")
        started.set()
        await release.wait()
        events.append("snapshot end")
        return b"jpeg"

    async def open_door(*args: object) -> None:
        events.append("output")

    client.snapshot.side_effect = snapshot
    client.open_door.side_effect = open_door
    image = hass.async_create_task(_image(hass))
    await started.wait()
    press = hass.async_create_task(
        hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: "button.aiphone_jo_1mdw_action_1"},
            blocking=True,
        )
    )
    await asyncio.sleep(0.05)
    assert events == ["snapshot start"]
    release.set()
    await image
    await press
    assert events == ["snapshot start", "snapshot end", "output"]
    client.open_door.assert_awaited_once_with(Output.UNLOCK, 101, 5)


async def test_video_unavailable(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """When video cannot work, the camera is unavailable with an explanation."""
    _client(client_holder).snapshot.side_effect = MediaUnavailableError(
        "video support is not installed"
    )
    with pytest.raises(HomeAssistantError):
        await _image(hass)
    await hass.async_block_till_done()
    assert hass.states.get(CAMERA).state == STATE_UNAVAILABLE
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "video_unavailable")
    assert issue is not None
    assert issue.translation_placeholders == {
        "reason": "video support is not installed"
    }
    assert loaded_entry.state is ConfigEntryState.LOADED


async def test_video_not_supported(
    hass: HomeAssistant, config_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """The integration still loads when the library has no video support."""

    def without_video(client: FakeClient) -> None:
        del client.snapshot

    client_holder.setup = without_video
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(CAMERA).state == STATE_UNAVAILABLE
    assert ir.async_get(hass).async_get_issue(DOMAIN, "video_unavailable")
    assert hass.states.get("button.aiphone_jo_1mdw_action_1") is not None


async def _read_until(response, marker: bytes) -> bytes:  # type: ignore[no-untyped-def]
    data = b""
    async with asyncio.timeout(5):
        while marker not in data:
            chunk = await response.content.read(1024)
            assert chunk, data
            data += chunk
    return data


async def _wait_closed(client: FakeClient) -> None:
    await asyncio.wait_for(client.stream.closed.wait(), 5)


async def test_mjpeg_stream_shared_and_stopped(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    client_holder: ClientHolder,
    hass_client,  # type: ignore[no-untyped-def]
) -> None:
    """Viewers share one live stream, stopped when the last one leaves."""
    client = _client(client_holder)
    http = await hass_client()

    first = await http.get(STREAM_URL)
    assert first.status == 200
    assert first.headers["Content-Type"].startswith("multipart/x-mixed-replace")
    await _read_until(first, b"frame-1")
    second = await http.get(STREAM_URL)
    await _read_until(second, b"frame-")
    assert client.stream_terminals == [101]

    # While streaming, still images come from the live view.
    assert (await _image(hass)).startswith(b"frame-")
    client.snapshot.assert_not_awaited()

    first.close()
    await asyncio.sleep(0.3)
    assert client.stream.open
    second.close()
    await _wait_closed(client)
    assert client.stream_terminals == [101]


async def test_mjpeg_stream_busy_fallback(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    client_holder: ClientHolder,
    hass_client,  # type: ignore[no-untyped-def]
) -> None:
    """The live view uses the next station when the first is busy."""
    client = _client(client_holder)
    calls = 0
    original = client.video_stream

    def video_stream(terminal: int | None = None):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        client.stream_error = (
            StationBusyError("busy", mid="24021", rslt=486) if calls == 1 else None
        )
        return original(terminal)

    client.video_stream = video_stream  # type: ignore[method-assign]
    http = await hass_client()
    response = await http.get(STREAM_URL)
    await _read_until(response, b"frame-1")
    assert client.stream_terminals == [101, 102]
    response.close()
    await _wait_closed(client)


async def test_mjpeg_stream_failure_ends_response(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    client_holder: ClientHolder,
    hass_client,  # type: ignore[no-untyped-def]
) -> None:
    """If no station can stream, the response ends without frames."""
    client = _client(client_holder)
    client.stream_error = MediaTimeoutError("no video")
    http = await hass_client()
    response = await http.get(STREAM_URL)
    body = await asyncio.wait_for(response.read(), 15)
    assert b"frame-" not in body
    assert client.stream_terminals == [101]
