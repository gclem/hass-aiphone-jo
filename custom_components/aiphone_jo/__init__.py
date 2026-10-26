"""Home Assistant integration for the Aiphone JO-1MDW intercom."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    Station,
    async_unregister,
    credentials_from_storage,
    credentials_to_storage,
    stations_from_room_info,
)
from .const import CONF_CREDENTIALS, CONF_STATIONS, DOMAIN, RING_TIMEOUT
from .coordinator import AiphoneJoRuntime
from .media import ISSUES
from .names import async_get_station_namer
from .notify import async_notify_manual_removal
from .vendor.aiphone_jo import (
    AiphoneJoClient,
    AiphoneJoError,
    CloudAuthError,
    CredentialsExpiredError,
    InvalidCredentialsError,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.IMAGE,
    Platform.SENSOR,
]

type AiphoneJoConfigEntry = ConfigEntry[AiphoneJoRuntime]


def _auth_failed(key: str) -> ConfigEntryAuthFailed:
    return ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key=key)


async def async_setup_entry(hass: HomeAssistant, entry: AiphoneJoConfigEntry) -> bool:
    """Connect to the monitor through the cloud and set up entities."""
    try:
        credentials = credentials_from_storage(entry.data[CONF_CREDENTIALS])
    except CredentialsExpiredError as err:
        raise _auth_failed("credentials_expired") from err
    except InvalidCredentialsError as err:
        raise _auth_failed("invalid_credentials") from err

    session = async_get_clientsession(hass)
    client = AiphoneJoClient(credentials, session=session, ring_timeout=RING_TIMEOUT)
    try:
        await client.connect()
    except CloudAuthError as err:
        await client.disconnect()
        raise _auth_failed("auth_refused") from err
    except AiphoneJoError as err:
        await client.disconnect()
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="cloud_unavailable"
        ) from err

    stations = [Station.from_dict(item) for item in entry.data.get(CONF_STATIONS, [])]
    try:
        fresh = stations_from_room_info(await client.get_room_info())
    except CloudAuthError as err:
        await client.disconnect()
        raise _auth_failed("auth_refused") from err
    except AiphoneJoError as err:
        _LOGGER.debug("Using stored entrance stations: %s", type(err).__name__)
    else:
        if fresh and fresh != stations:
            stations = fresh
            hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, CONF_STATIONS: [s.as_dict() for s in fresh]},
            )

    namer = await async_get_station_namer(hass)
    entry.runtime_data = AiphoneJoRuntime(hass, entry, client, stations, namer)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: AiphoneJoConfigEntry
) -> None:
    if dict(entry.options) != entry.runtime_data.options:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: AiphoneJoConfigEntry) -> bool:
    """Unload entities and disconnect."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Unregister the terminal when the integration is removed."""
    for issue in ISSUES:
        ir.async_delete_issue(hass, DOMAIN, issue)
    removed = False
    try:
        credentials = credentials_from_storage(
            entry.data[CONF_CREDENTIALS], check_validity=False
        )
    except (InvalidCredentialsError, KeyError) as err:
        _LOGGER.debug("Terminal not unregistered: %s", type(err).__name__)
    else:
        removed = await async_unregister(async_get_clientsession(hass), credentials)
    if not removed:
        await async_notify_manual_removal(hass, entry)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate older config entries."""
    if entry.version > 1:
        # Written by a newer version of the integration.
        return False
    if entry.minor_version < 2:
        await _async_migrate_to_monitor_id(hass, entry)
    return True


async def _async_migrate_to_monitor_id(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """1.1 -> 1.2: library storage format and monitor id as unique id."""
    # 1.1 stored the /registClient layout with a "schema" marker that the
    # library would read as its own schema 1: drop it to get schema 0.
    legacy = {k: v for k, v in entry.data[CONF_CREDENTIALS].items() if k != "schema"}
    data = {**entry.data, CONF_CREDENTIALS: legacy}
    unique_id = entry.unique_id
    try:
        credentials = credentials_from_storage(legacy, check_validity=False)
    except InvalidCredentialsError:
        _LOGGER.warning("Stored identity unreadable: pair the monitor again")
    else:
        data[CONF_CREDENTIALS] = credentials_to_storage(credentials)
        monitor_id = credentials.monitor_id
        if monitor_id and not hass.config_entries.async_entry_for_domain_unique_id(
            DOMAIN, monitor_id
        ):
            await _async_migrate_identifiers(
                hass, entry, str(entry.unique_id), monitor_id
            )
            unique_id = monitor_id
    hass.config_entries.async_update_entry(
        entry, data=data, unique_id=unique_id, minor_version=2
    )


async def _async_migrate_identifiers(
    hass: HomeAssistant, entry: ConfigEntry, old: str, new: str
) -> None:
    @callback
    def update_entity(entity: er.RegistryEntry) -> dict[str, str] | None:
        if entity.unique_id.startswith(f"{old}_"):
            return {"new_unique_id": new + entity.unique_id[len(old) :]}
        return None

    await er.async_migrate_entries(hass, entry.entry_id, update_entity)
    registry = dr.async_get(hass)
    if device := registry.async_get_device(identifiers={(DOMAIN, old)}):
        registry.async_update_device(device.id, new_identifiers={(DOMAIN, new)})
