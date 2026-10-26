"""Tests for setup, runtime and entities."""

from __future__ import annotations

import asyncio
import warnings
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.aiphone_jo.const import (
    CONF_CREDENTIALS,
    CONF_STATION,
    CONF_STATIONS,
    DOMAIN,
)
from custom_components.aiphone_jo.vendor.aiphone_jo import (
    CallStatus,
    CallUpdatedEvent,
    CloudApiError,
    CloudAuthError,
    CloudConnectionError,
    CloudCredentials,
    CommandRejectedError,
    ConnectionStateEvent,
    IncomingCall,
    NotConnectedError,
    Output,
    RingEvent,
    RoomInfo,
    StationBusyError,
    Terminal,
)

from .conftest import (
    FINGERPRINT_ID,
    ClientHolder,
    FakeClient,
    entry_data,
    make_credentials,
)

EVENT = "event.aiphone_jo_1mdw_ring"
RINGING = "binary_sensor.aiphone_jo_1mdw_ringing"
LAST_STATION = "sensor.aiphone_jo_1mdw_last_ringing_station"
ACTION_1 = "button.aiphone_jo_1mdw_action_1"
ACTION_2 = "button.aiphone_jo_1mdw_action_2"


def _call(term_id: int = 102) -> IncomingCall:
    return IncomingCall(
        cid="example-cid",
        station_client_id="example-station",
        display_name="",
        display_name_2="",
        call_kind=1,
        term_kind=5,
        term_id=term_id,
        topic="example/topic",
        ringing_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
    )


def _client(holder: ClientHolder) -> FakeClient:
    assert holder.client is not None
    return holder.client


async def _press(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )


async def test_setup_and_unload(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """The entry connects, creates entities and disconnects on unload."""
    client = _client(client_holder)
    assert loaded_entry.state is ConfigEntryState.LOADED
    assert client.kwargs["ring_timeout"] == 60
    for entity_id in (EVENT, RINGING, LAST_STATION, ACTION_1, ACTION_2):
        assert hass.states.get(entity_id) is not None, entity_id
    assert hass.states.get(RINGING).state == STATE_OFF

    assert await hass.config_entries.async_unload(loaded_entry.entry_id)
    assert loaded_entry.state is ConfigEntryState.NOT_LOADED
    client.disconnect.assert_awaited_once()
    assert client.listeners == []


async def test_cloud_unreachable_retries(
    hass: HomeAssistant, config_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """A temporary cloud failure makes Home Assistant retry."""
    client_holder.setup = lambda c: setattr(
        c.connect, "side_effect", CloudConnectionError("down")
    )
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    _client(client_holder).disconnect.assert_awaited()


async def test_invalid_credentials_start_reauth(
    hass: HomeAssistant, config_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """Unusable stored credentials start a re-pairing."""
    data = dict(config_entry.data)
    data[CONF_CREDENTIALS] = {**data[CONF_CREDENTIALS], "cert_pem": "invalid"}
    hass.config_entries.async_update_entry(config_entry, data=data)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]
    assert client_holder.client is None


async def test_stations_refreshed(
    hass: HomeAssistant, config_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """Stations are refreshed at startup, or kept when the cloud fails."""
    renamed = Terminal(id=101, term_kind=5, name="Side", camera=True, ent_key_type=None)
    client_holder.setup = lambda c: setattr(
        c.get_room_info, "return_value", RoomInfo(terminals=(renamed,), term_inf_num=1)
    )
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.data[CONF_STATIONS] == [
        {"id": 101, "kind": 5, "name": "Side", "camera": True}
    ]
    assert config_entry.state is ConfigEntryState.LOADED


async def test_stations_kept_when_cloud_fails(
    hass: HomeAssistant, config_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """The stored stations are used when /getRoomInfo fails."""
    client_holder.setup = lambda c: setattr(
        c.get_room_info, "side_effect", CloudApiError("down")
    )
    stored = config_entry.data[CONF_STATIONS]
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.data[CONF_STATIONS] == stored
    assert len(config_entry.runtime_data.stations) == 2


async def test_ring(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """A ring fires the event and turns the ringing sensor on until it ends."""
    client = _client(client_holder)
    call = _call(102)
    client.emit(RingEvent(call))
    await hass.async_block_till_done()

    event = hass.states.get(EVENT)
    assert event.attributes["event_type"] == "ring"
    assert event.attributes["station"] == "Back"
    assert event.attributes["device_class"] == "doorbell"
    assert hass.states.get(RINGING).state == STATE_ON
    last = hass.states.get(LAST_STATION)
    assert last.state == "Back"
    assert last.attributes["rang_at"] == "2026-01-01T12:00:00+00:00"

    call.status = CallStatus.EXPIRED
    client.emit(CallUpdatedEvent(call))
    await hass.async_block_till_done()
    assert hass.states.get(RINGING).state == STATE_OFF
    assert hass.states.get(LAST_STATION).state == "Back"


async def test_ring_unknown_station(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """A station missing from the room info still gets a readable name."""
    _client(client_holder).emit(RingEvent(_call(999)))
    await hass.async_block_till_done()
    assert hass.states.get(LAST_STATION).state == "Station 999"


async def test_last_station_restored(
    hass: HomeAssistant, config_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """The last ringing station survives a restart."""
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(LAST_STATION, "Front", {"rang_at": "2026-01-01T08:00:00+00:00"}),
                {"native_value": "Front", "native_unit_of_measurement": None},
            )
        ],
    )
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(LAST_STATION)
    assert state.state == "Front"
    assert state.attributes["rang_at"] == "2026-01-01T08:00:00+00:00"


async def test_availability(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """Entities are unavailable while the cloud connection is down."""
    client = _client(client_holder)
    client.connected = False
    client.emit(ConnectionStateEvent(False, False, "lost"))
    await hass.async_block_till_done()
    for entity_id in (RINGING, LAST_STATION, ACTION_1, ACTION_2):
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

    client.connected = True
    client.emit(ConnectionStateEvent(True, False))
    await hass.async_block_till_done()
    assert hass.states.get(RINGING).state == STATE_OFF


@pytest.mark.parametrize(
    ("entity_id", "output"), [(ACTION_1, Output.UNLOCK), (ACTION_2, Output.OPTION)]
)
async def test_buttons(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    client_holder: ClientHolder,
    entity_id: str,
    output: Output,
) -> None:
    """Each button pulses its output through the first camera station."""
    await _press(hass, entity_id)
    _client(client_holder).open_door.assert_awaited_once_with(output, 101, 5)


async def test_preferred_station(
    hass: HomeAssistant, config_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """The station chosen in the options carries the session."""
    hass.config_entries.async_update_entry(config_entry, options={CONF_STATION: 102})
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    await _press(hass, ACTION_1)
    _client(client_holder).open_door.assert_awaited_once_with(Output.UNLOCK, 102, 5)


async def test_options_change_reloads(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """Changing the options reloads the entry."""
    first = _client(client_holder)
    hass.config_entries.async_update_entry(loaded_entry, options={CONF_STATION: 102})
    await hass.async_block_till_done()
    assert client_holder.client is not first
    first.disconnect.assert_awaited_once()


async def test_busy_station_fallback(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """A busy station (486) falls back to the next one."""
    client = _client(client_holder)
    client.open_door.side_effect = [
        StationBusyError("busy", mid="24021", rslt=486),
        None,
    ]
    await _press(hass, ACTION_1)
    assert [call.args for call in client.open_door.await_args_list] == [
        (Output.UNLOCK, 101, 5),
        (Output.UNLOCK, 102, 5),
    ]


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (StationBusyError("busy", mid="24021", rslt=486), "station_busy"),
        (NotConnectedError("down"), "not_connected"),
        (CommandRejectedError("no", mid="27021", rslt=1), "command_rejected"),
    ],
)
async def test_button_errors(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    client_holder: ClientHolder,
    error: Exception,
    key: str,
) -> None:
    """Failures are reported with an actionable message."""
    _client(client_holder).open_door.side_effect = error
    with pytest.raises(HomeAssistantError) as exc_info:
        await _press(hass, ACTION_1)
    assert exc_info.value.translation_key == key


async def test_no_station(
    hass: HomeAssistant, config_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """The output buttons explain that a station is needed."""
    hass.config_entries.async_update_entry(
        config_entry, data={**config_entry.data, CONF_STATIONS: []}
    )
    client_holder.setup = lambda c: setattr(
        c.get_room_info, "return_value", RoomInfo(terminals=(), term_inf_num=0)
    )
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(HomeAssistantError) as exc_info:
        await _press(hass, ACTION_1)
    assert exc_info.value.translation_key == "no_station"


async def test_outputs_are_serialized(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """Only one monitoring session runs at a time."""
    running = 0
    peak = 0

    async def open_door(*args: object) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1

    client = _client(client_holder)
    client.open_door.side_effect = open_door
    await asyncio.gather(_press(hass, ACTION_1), _press(hass, ACTION_2))
    assert client.open_door.await_count == 2
    assert peak == 1


async def test_remove_entry_notifies(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """Removing the entry explains how to free the smartphone slot."""
    with (
        patch(
            "custom_components.aiphone_jo.api.unregister",
            side_effect=CloudApiError("unreachable", kind="network"),
        ) as unregister,
        patch(
            "custom_components.aiphone_jo.notify.persistent_notification.async_create"
        ) as create,
    ):
        assert await hass.config_entries.async_remove(loaded_entry.entry_id)
        await hass.async_block_till_done()
    unregister.assert_awaited_once()
    create.assert_called_once()
    message = create.call_args.args[1]
    assert "**HomeAssist**" in message
    assert "{name}" not in message
    assert create.call_args.kwargs["title"].startswith("Aiphone JO")


async def test_remove_entry_unregistered(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """No notification when the terminal was unregistered."""
    with (
        patch(
            "custom_components.aiphone_jo.async_unregister", return_value=True
        ) as unregister,
        patch("custom_components.aiphone_jo.async_notify_manual_removal") as notify,
    ):
        assert await hass.config_entries.async_remove(loaded_entry.entry_id)
        await hass.async_block_till_done()
    unregister.assert_awaited_once()
    notify.assert_not_called()


async def test_future_version_is_refused(
    hass: HomeAssistant, config_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """An entry from a newer version is not loaded."""
    hass.config_entries.async_update_entry(config_entry, version=2)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.MIGRATION_ERROR


def _reauth_started(hass: HomeAssistant) -> bool:
    return any(
        flow["context"]["source"] == SOURCE_REAUTH
        for flow in hass.config_entries.flow.async_progress()
    )


async def test_auth_refused_at_startup(
    hass: HomeAssistant, config_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """A refused identity starts a re-pairing instead of retrying forever."""
    client_holder.setup = lambda c: setattr(
        c.connect, "side_effect", CloudAuthError("not authorized")
    )
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert _reauth_started(hass)


async def test_auth_refused_by_room_info(
    hass: HomeAssistant, config_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """An HTTP 401/403 from the API also starts a re-pairing."""
    client_holder.setup = lambda c: setattr(
        c.get_room_info, "side_effect", CloudAuthError("forbidden", status=403)
    )
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert _reauth_started(hass)
    _client(client_holder).disconnect.assert_awaited()


async def test_auth_refused_at_runtime(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """A refusal while running (terminal deleted on the monitor) starts reauth."""
    client = _client(client_holder)
    client.connected = False
    client.emit(ConnectionStateEvent(False, False, "refused", auth_failed=True))
    await hass.async_block_till_done()
    assert _reauth_started(hass)
    assert hass.states.get(RINGING).state == STATE_UNAVAILABLE


async def test_expired_credentials(
    hass: HomeAssistant, client_holder: ClientHolder
) -> None:
    """An expired certificate starts a re-pairing."""
    expired = make_credentials(expired=True)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=expired.monitor_id,
        data=entry_data(expired),
        version=1,
        minor_version=2,
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert _reauth_started(hass)
    assert client_holder.client is None


def _legacy_entry(
    hass: HomeAssistant, credentials: CloudCredentials
) -> MockConfigEntry:
    """Entry as written by version 1.1 (registration layout, fingerprint id)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        legacy = {"schema": 1, **credentials.as_dict()}
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=FINGERPRINT_ID,
        data={**entry_data(credentials), CONF_CREDENTIALS: legacy},
        version=1,
        minor_version=1,
    )
    entry.add_to_hass(hass)
    return entry


async def test_migration_to_monitor_id(
    hass: HomeAssistant, credentials: CloudCredentials, client_holder: ClientHolder
) -> None:
    """1.1 entries get the library storage format and the monitor id, entities kept."""
    entry = _legacy_entry(hass, credentials)
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, FINGERPRINT_ID)}
    )
    ring = entities.async_get_or_create(
        "event",
        DOMAIN,
        f"{FINGERPRINT_ID}_ring",
        config_entry=entry,
        device_id=device.id,
        suggested_object_id="aiphone_jo_1mdw_ring",
    )

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    monitor_id = credentials.monitor_id
    assert entry.state is ConfigEntryState.LOADED
    assert entry.minor_version == 2
    assert entry.unique_id == monitor_id
    stored = entry.data[CONF_CREDENTIALS]
    assert stored["schema"] == 1
    assert stored["cert_pem"] == credentials.cert_pem
    assert "client_certificate" not in stored
    migrated = entities.async_get(ring.entity_id)
    assert migrated is not None
    assert migrated.unique_id == f"{monitor_id}_ring"
    assert devices.async_get(device.id).identifiers == {(DOMAIN, monitor_id)}
    assert hass.states.get(ring.entity_id) is not None
    assert len(er.async_entries_for_config_entry(entities, entry.entry_id)) == 6


async def test_migration_with_unreadable_credentials(
    hass: HomeAssistant, credentials: CloudCredentials, client_holder: ClientHolder
) -> None:
    """Unreadable legacy credentials still migrate, then ask for re-pairing."""
    entry = _legacy_entry(hass, credentials)
    data = dict(entry.data)
    data[CONF_CREDENTIALS] = {**data[CONF_CREDENTIALS], "client_certificate": "invalid"}
    hass.config_entries.async_update_entry(entry, data=data)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.minor_version == 2
    assert entry.unique_id == FINGERPRINT_ID
    assert "schema" not in entry.data[CONF_CREDENTIALS]
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert _reauth_started(hass)


async def test_remove_entry_unregisters_through_library(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """The library unregister call is used on removal."""
    with (
        patch("custom_components.aiphone_jo.api.unregister", return_value=True) as call,
        patch("custom_components.aiphone_jo.async_notify_manual_removal") as notify,
    ):
        assert await hass.config_entries.async_remove(loaded_entry.entry_id)
        await hass.async_block_till_done()
    call.assert_awaited_once()
    notify.assert_not_called()


async def test_existing_button_entities_are_kept(
    hass: HomeAssistant, config_entry: MockConfigEntry, client_holder: ClientHolder
) -> None:
    """Buttons created under their former names keep their entity ids."""
    entities = er.async_get(hass)
    old = entities.async_get_or_create(
        "button",
        DOMAIN,
        f"{config_entry.unique_id}_large_opening",
        config_entry=config_entry,
        suggested_object_id="aiphone_jo_1mdw_large_opening",
    )
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    buttons = [
        entity
        for entity in er.async_entries_for_config_entry(entities, config_entry.entry_id)
        if entity.domain == "button"
    ]
    assert len(buttons) == 2
    kept = entities.async_get(old.entity_id)
    assert kept is not None
    assert kept.translation_key == "action_1"
    state = hass.states.get(old.entity_id)
    assert state.attributes["friendly_name"].endswith("Action 1")
    await _press(hass, old.entity_id)
    _client(client_holder).open_door.assert_awaited_once_with(Output.UNLOCK, 101, 5)
