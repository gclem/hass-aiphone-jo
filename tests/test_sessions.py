"""History of the monitoring sessions, kept across restarts."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aiphone_jo.const import DOMAIN
from custom_components.aiphone_jo.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.aiphone_jo.sessions import MAX_STORED
from custom_components.aiphone_jo.vendor.aiphone_jo import (
    SessionRecord,
    SessionRecordEvent,
    SessionStatus,
)

from .conftest import ClientHolder

CLIENT_ID = "example-client-id"


def _record(
    suffix: str, status: SessionStatus, age: timedelta = timedelta()
) -> SessionRecord:
    moment = dt_util.now() - age
    return SessionRecord(
        cid=f"{CLIENT_ID}2026111613{suffix:0>16}",
        term_id=101,
        term_kind=5,
        status=status,
        started_at=moment,
        updated_at=moment,
        cor_cli_id="relay-1",
    )


def _key(entry: MockConfigEntry) -> str:
    return f"{DOMAIN}.{entry.entry_id}.sessions"


async def test_stored_sessions_restored_before_connecting(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_holder: ClientHolder,
    hass_storage: dict[str, Any],
) -> None:
    """Sessions stored before a restart go back to the client, old ones dropped."""
    kept = _record("1", SessionStatus.ABANDONED)
    old = _record("2", SessionStatus.ABANDONED, timedelta(days=16))
    hass_storage[_key(config_entry)] = {
        "version": 1,
        "key": _key(config_entry),
        "data": {"sessions": [kept.to_storage(), old.to_storage(), {"cid": "?"}]},
    }
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert client_holder.client.restored == [kept]


async def test_only_problematic_sessions_are_stored(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    client_holder: ClientHolder,
    hass_storage: dict[str, Any],
) -> None:
    """Ordinary sessions are not written; problems, and how they ended, are."""
    client = client_holder.client
    client.emit(SessionRecordEvent(_record("1", SessionStatus.CLOSED)))
    client.emit(SessionRecordEvent(_record("2", SessionStatus.ABANDONED)))
    client.emit(SessionRecordEvent(_record("2", SessionStatus.CLEANED)))
    client.emit(SessionRecordEvent(_record("3", SessionStatus.ABANDONED)))
    client.emit(SessionRecordEvent(_record("3", SessionStatus.ENDED)))
    assert await hass.config_entries.async_unload(loaded_entry.entry_id)
    await hass.async_block_till_done()
    stored = hass_storage[_key(loaded_entry)]["data"]["sessions"]
    statuses = {item["cid"][-1]: item["status"] for item in stored}
    assert statuses == {"2": "cleaned", "3": "ended"}


async def test_stored_history_stays_bounded(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    client_holder: ClientHolder,
    hass_storage: dict[str, Any],
) -> None:
    """At most MAX_STORED sessions; finished ones go first."""
    client = client_holder.client
    for index in range(MAX_STORED + 10):
        status = SessionStatus.CLEANED if index < 40 else SessionStatus.ABANDONED
        client.emit(SessionRecordEvent(_record(str(index), status)))
    assert await hass.config_entries.async_unload(loaded_entry.entry_id)
    await hass.async_block_till_done()
    stored = hass_storage[_key(loaded_entry)]["data"]["sessions"]
    assert len(stored) == MAX_STORED
    assert sum(item["status"] == "abandoned" for item in stored) == 20


async def test_diagnostics_without_session_ids(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    client_holder: ClientHolder,
) -> None:
    """The diagnostics show the history with the time part of the CID only."""
    record = _record("7", SessionStatus.ABANDONED)
    record.detail = "MNT_REQ no answer"
    client_holder.client.session_history = [record]
    diagnostics = await async_get_config_entry_diagnostics(hass, loaded_entry)
    [session] = diagnostics["sessions"]
    assert session["tag"] == record.cid[-20:]
    assert session["status"] == "abandoned"
    assert session["detail"] == "MNT_REQ no answer"
    assert CLIENT_ID not in str(diagnostics)


async def test_removal_deletes_the_history(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    client_holder: ClientHolder,
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing the integration deletes the stored sessions."""

    async def unregistered(*_: Any) -> bool:
        return True

    client_holder.client.emit(SessionRecordEvent(_record("1", SessionStatus.ABANDONED)))
    assert await hass.config_entries.async_unload(loaded_entry.entry_id)
    await hass.async_block_till_done()
    assert _key(loaded_entry) in hass_storage
    monkeypatch.setattr("custom_components.aiphone_jo.async_unregister", unregistered)
    await hass.config_entries.async_remove(loaded_entry.entry_id)
    await hass.async_block_till_done()
    assert _key(loaded_entry) not in hass_storage
