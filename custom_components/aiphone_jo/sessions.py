"""History of the monitoring sessions, kept across restarts.

The intercom refuses every new session (486) while one is left open on the
station: a session this integration gave up (no answer, cancelled) or lost
with the connection. The library records each session with its status and
closes the unfinished ones; this module stores the problematic ones and how
they ended, privately, so that they can still be closed after a restart.

Ordinary sessions (closed, ended by the station) are frequent and not
stored: nothing is written to disk unless a session goes wrong.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .vendor.aiphone_jo import (
    AiphoneEvent,
    SessionRecord,
    SessionRecordEvent,
    SessionStatus,
)

STORAGE_VERSION = 1
SAVE_DELAY = 10.0
# Stored: sessions that went wrong, and how they ended.
STORED_STATUSES = frozenset({SessionStatus.ABANDONED, SessionStatus.CLEANED})
MAX_STORED = 50
RETENTION = timedelta(days=15)


def _store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.sessions", private=True)


async def async_remove_sessions_store(hass: HomeAssistant, entry_id: str) -> None:
    """Delete the stored sessions (integration removed)."""
    await _store(hass, entry_id).async_remove()


class SessionHistory:
    """Stores the problematic monitoring sessions of one config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize."""
        self._store = _store(hass, entry_id)
        self._records: dict[str, SessionRecord] = {}

    async def async_load(self) -> list[SessionRecord]:
        """Stored sessions, for ``AiphoneJoClient.restore_sessions``."""
        data = await self._store.async_load() or {}
        for item in data.get("sessions") or []:
            if isinstance(item, dict) and (record := SessionRecord.from_storage(item)):
                self._records[record.cid] = record
        self._trim()
        return list(self._records.values())

    @callback
    def async_handle_event(self, event: AiphoneEvent) -> None:
        """Keep a session that went wrong, then how it ended."""
        if not isinstance(event, SessionRecordEvent):
            return
        record = event.record
        if record.status not in STORED_STATUSES and record.cid not in self._records:
            return
        self._records[record.cid] = record
        self._trim()
        self._store.async_delay_save(self._data, SAVE_DELAY)

    def _trim(self) -> None:
        oldest = dt_util.now() - RETENTION
        for cid in [c for c, r in self._records.items() if r.updated_at < oldest]:
            del self._records[cid]
        ordered = sorted(self._records.values(), key=lambda r: r.updated_at)
        excess = len(ordered) - MAX_STORED
        if excess > 0:
            # Finished ones go first, then the oldest unfinished.
            ordered.sort(key=lambda r: (r.unfinished, r.updated_at))
            for record in ordered[:excess]:
                del self._records[record.cid]

    def _data(self) -> dict[str, Any]:
        return {"sessions": [r.to_storage() for r in self._records.values()]}

    async def async_flush(self) -> None:
        """Write now (unload)."""
        await self._store.async_save(self._data())


def session_diagnostics(records: list[SessionRecord]) -> list[dict[str, Any]]:
    """History for the diagnostics: the time part of the CID only."""
    return [
        {
            "tag": record.tag,
            "station_id": record.term_id,
            "status": record.status.value,
            "started_at": record.started_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
            "detail": record.detail,
        }
        for record in records
    ]
