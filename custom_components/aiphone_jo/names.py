"""Readable labels for the factory default names of the entrance stations.

The Aiphone service returns the names set on the monitor. Unless renamed, the
entrance stations keep their factory default Japanese names, such as "玄関1"
("entrance 1"). These are shown translated; any other name is kept as is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations

from .const import DOMAIN

STATION_ENTRANCE_KEY = "station_entrance"
DEFAULT_ENTRANCE_TEMPLATE = "Entrance {number}"

_DEFAULT_ENTRANCE = re.compile(r"\s*玄関\s*(\d*)\s*")


@dataclass(frozen=True, slots=True)
class StationNamer:
    """Translate factory default station names, keep custom names."""

    entrance: str = DEFAULT_ENTRANCE_TEMPLATE

    def __call__(self, name: str) -> str:
        """Label shown for the station named ``name`` by the Aiphone service."""
        match = _DEFAULT_ENTRANCE.fullmatch(name)
        if match is None:
            return name
        number = str(int(match.group(1))) if match.group(1) else ""
        return self.entrance.replace("{number}", number).strip()


async def async_get_station_namer(hass: HomeAssistant) -> StationNamer:
    """Station namer in the language of the Home Assistant installation."""
    translations = await async_get_translations(
        hass, hass.config.language, "exceptions", {DOMAIN}
    )
    key = f"component.{DOMAIN}.exceptions.{STATION_ENTRANCE_KEY}.message"
    return StationNamer(translations.get(key, DEFAULT_ENTRANCE_TEMPLATE))
