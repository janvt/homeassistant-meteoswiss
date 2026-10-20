"""The repair notices of an entry."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from custom_components.meteoswiss.const import (
    CONF_POINT_ID,
    CONF_POINT_TYPE,
    CONF_POSTCODE,
    CONF_PRECIPITATION_STATION,
    CONF_REAL_TIME_NAME,
    CONF_REAL_TIME_PRECIPITATION_NAME,
    CONF_STATION,
    DOMAIN,
)
from custom_components.meteoswiss.ogd import POINT_TYPE_POI, POINT_TYPE_POSTCODE


def station_issue_id(station: str, name_key: str) -> str:
    """The notice that a station gives no data (name_key tells its role)."""
    return f"{station}_{name_key}_provides_no_data_{DOMAIN}"


def no_forecast_issue_id(
    postcode: int | None, point_type: int, point_id: int | None
) -> str:
    """The notice that there is no forecast for the configured place."""
    if point_type == POINT_TYPE_POI:
        return f"point_{point_id}_provides_no_forecast_{DOMAIN}"
    return f"{postcode}_provides_no_forecast_{DOMAIN}"


@callback
def async_delete_issues(hass: HomeAssistant, data: Mapping[str, Any]) -> None:
    """Remove the notices about the place and stations of an entry's settings."""
    ids = [
        no_forecast_issue_id(
            data.get(CONF_POSTCODE),
            data.get(CONF_POINT_TYPE, POINT_TYPE_POSTCODE),
            data.get(CONF_POINT_ID),
        )
    ]
    if data.get(CONF_STATION):
        ids.append(station_issue_id(data[CONF_STATION], CONF_REAL_TIME_NAME))
    if data.get(CONF_PRECIPITATION_STATION):
        ids.append(
            station_issue_id(
                data[CONF_PRECIPITATION_STATION], CONF_REAL_TIME_PRECIPITATION_NAME
            )
        )
    for issue_id in ids:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
