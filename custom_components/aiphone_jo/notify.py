"""User notifications."""

from __future__ import annotations

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations

from .const import CONF_CREDENTIALS, DEFAULT_TERMINAL_NAME, DOMAIN

_FALLBACK_TITLE = "Aiphone JO: remove the terminal from the monitor"
_FALLBACK_MESSAGE = (
    "Home Assistant could not unregister the terminal **{name}** from the monitor. "
    "It still uses a smartphone slot: on the monitor, open the smartphone "
    "settings menu and delete **{name}**."
)


async def async_notify_manual_removal(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Tell the user how to remove the terminal from the monitor by hand."""
    name = (
        entry.data.get(CONF_CREDENTIALS, {}).get("term_name") or DEFAULT_TERMINAL_NAME
    )
    prefix = f"component.{DOMAIN}.exceptions.manual_removal"
    translations = await async_get_translations(
        hass, hass.config.language, "exceptions", {DOMAIN}
    )
    title = translations.get(f"{prefix}_title.message", _FALLBACK_TITLE)
    message = translations.get(f"{prefix}.message", _FALLBACK_MESSAGE)
    persistent_notification.async_create(
        hass,
        message.replace("{name}", name),
        title=title,
        notification_id=f"{DOMAIN}_manual_removal_{entry.entry_id}",
    )
