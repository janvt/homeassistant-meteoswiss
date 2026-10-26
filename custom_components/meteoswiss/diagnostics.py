"""Diagnostics download of an entry, to attach to an issue."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from custom_components.meteoswiss import MeteoSwissConfigEntry
from custom_components.meteoswiss.const import CONF_LAT, CONF_LON, CONF_POSTCODE

# Where the user lives: the coordinates of the setup and of the radar, and the
# postal code.  Station codes and the place's name stay, as they are needed to
# make sense of the rest.
TO_REDACT = {CONF_LAT, CONF_LON, CONF_POSTCODE, "location"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: MeteoSwissConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    c = entry.runtime_data
    data = c.data
    point = data["point"]
    forecast = data["forecast"]
    hourly = forecast["hourly"] if forecast else []
    radar = c.radar
    return async_redact_data(
        {
            "entry": {"data": dict(entry.data), "options": dict(entry.options)},
            "coordinator": {
                "last_update_success": c.last_update_success,
                "last_exception": repr(c.last_exception) if c.last_exception else None,
                "update_interval": str(c.update_interval),
            },
            "point": point
            and {
                "point_id": point.point_id,
                "point_type": point.point_type,
                "height": point.height,
            },
            "forecast": forecast
            and {
                "run": forecast["run"],
                "days": len(forecast["daily"]),
                "hours": len(hourly),
                "first_hour": hourly[0]["time"].isoformat() if hourly else None,
                "last_hour": hourly[-1]["time"].isoformat() if hourly else None,
            },
            "stations": data["condition_by_station"],
            "pollen": data["pollen"],
            "warnings": data["warnings"]
            and [
                {
                    "type": w["type"],
                    "level": w["level"],
                    "onset": w["onset"].isoformat(),
                    "expires": w["expires"].isoformat(),
                }
                for w in data["warnings"]
            ],
            "foehn": data["foehn"],
            "climate_normals": data["climate"] is not None,
            "radar": radar
            and {
                "last_update_success": radar.last_update_success,
                "location": radar.location,
                "readings": radar.data,
            },
            # Shared by all entries.
            "client": c.client.diagnostics(),
            "shared_downloads": c.downloads.diagnostics(),
        },
        TO_REDACT,
    )
