"""Import of a weather station's hourly history into long-term statistics.

Only on request (the import_history action): the history files are 1 MB for
this year and 7 to 13 MB for each decade before.  Each sensor gets the hours
before its first recorded statistic, so what Home Assistant recorded itself is
never replaced, and importing again adds nothing.  The statistics keep their
metadata, so the values are converted to the unit they are kept in.
"""

from __future__ import annotations

import datetime
import logging
from functools import partial
from typing import TYPE_CHECKING, NamedTuple

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from custom_components.meteoswiss.const import DOMAIN
from custom_components.meteoswiss.ogd import OGDError

if TYPE_CHECKING:
    from custom_components.meteoswiss import MeteoSwissDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

SERVICE_IMPORT_HISTORY = "import_history"
IMPORT_HISTORY_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Optional("since"): cv.date,
    }
)


class Columns(NamedTuple):
    """Where a sensor's hourly statistics come from in the history files."""

    mean: str
    min: str | None = None
    max: str | None = None
    # The 10-minute sums: the mean of an hour's six is its total over 6.
    divisor: float = 1
    # The unit of the files, which is the sensor's.
    unit: str | None = None


# By the key of the sensor.  Gusts are left out (the hour's is a maximum, not
# a mean of 10-minute peaks), and so is the wind direction (a mean of angles
# is meaningless); wind chill has no hourly values.
SENSOR_COLUMNS: dict[str, Columns] = {
    "temperature": Columns("tre200h0", "tre200hn", "tre200hx", unit="°C"),
    "temperature_5cm": Columns("tre005h0", "tre005hn", unit="°C"),
    "humidity": Columns("ure200h0", unit="%"),
    "dew_point": Columns("tde200h0", unit="°C"),
    "pressure": Columns("prestah0", unit="hPa"),
    "pressure_qff": Columns("pp0qffh0", unit="hPa"),
    "pressure_qnh": Columns("pp0qnhh0", unit="hPa"),
    "wind_speed": Columns("fu3010h0", unit="km/h"),
    "10minrain": Columns("rre150h0", divisor=6, unit="mm"),
    "10minsun": Columns("sre000h0", divisor=6, unit="min"),
    "sun_radiant": Columns("gre000h0", unit="W/m²"),
    "diffuse_radiation": Columns("ods000h0", unit="W/m²"),
    "longwave_radiation": Columns("oli000h0", unit="W/m²"),
    # Values at the end of the hour rather than means.
    "snow_depth": Columns("htoauths", unit="cm"),
    "soil_temperature_5cm": Columns("tso005hs", unit="°C"),
    "soil_temperature_10cm": Columns("tso010hs", unit="°C"),
    "soil_temperature_20cm": Columns("tso020hs", unit="°C"),
}


def _file_start(url: str, now: datetime.datetime) -> datetime.datetime:
    """When a history file starts: its decade, or this year for the recent."""
    if url.endswith("_recent.csv"):
        year = now.year
    else:
        year = int(url.rsplit("_", 1)[1].split("-")[0])
    return datetime.datetime(year, 1, 1, tzinfo=datetime.UTC)


def default_since(now: datetime.datetime) -> datetime.datetime:
    """The start of this decade: about 9 MB of statistics a year for all the
    station sensors, kept for good, so older years are only on request."""
    return datetime.datetime(now.year // 10 * 10, 1, 1, tzinfo=datetime.UTC)


async def async_import_history(
    hass: HomeAssistant,
    coordinator: MeteoSwissDataUpdateCoordinator,
    since: datetime.datetime,
) -> dict[str, int]:
    """Import the weather station's hours from `since` on; the number of
    hours imported by statistic ID."""
    # Not at the top: the recorder is only needed for this.
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import (
        STATISTIC_UNIT_TO_UNIT_CONVERTER,
        async_import_statistics,
        get_metadata,
        statistics_during_period,
    )

    entry_id = coordinator.config_entry.entry_id
    registry = er.async_get(hass)
    entity_ids = {
        key: entity_id
        for key in SENSOR_COLUMNS
        if (
            entity_id := registry.async_get_entity_id(
                "sensor", DOMAIN, f"sensor.{entry_id}-{key}"
            )
        )
    }
    recorder = get_instance(hass)
    # Only statistics the recorder already keeps: their metadata is reused.
    metadata = await recorder.async_add_executor_job(
        partial(get_metadata, hass, statistic_ids=set(entity_ids.values()))
    )
    keys = {key: e for key, e in entity_ids.items() if e in metadata}
    if not keys:
        return {}
    recorded = await recorder.async_add_executor_job(
        statistics_during_period,
        hass,
        since,
        None,
        set(keys.values()),
        "hour",
        None,
        {"mean"},
    )
    # Hours from this one on are Home Assistant's own.
    first: dict[str, datetime.datetime] = {}
    for statistic_id, rows in recorded.items():
        if rows:
            start = min(row["start"] for row in rows)
            first[statistic_id] = (
                datetime.datetime.fromtimestamp(start, datetime.UTC)
                if isinstance(start, float | int)
                else start
            )
    # Converts from the files' unit to the one a statistic is kept in.
    convert = {}
    for key, statistic_id in list(keys.items()):
        unit = SENSOR_COLUMNS[key].unit
        kept = metadata[statistic_id][1]["unit_of_measurement"]
        converter = STATISTIC_UNIT_TO_UNIT_CONVERTER.get(kept)
        if kept == unit:
            convert[key] = None
        elif converter is not None and unit in converter.VALID_UNITS:
            convert[key] = converter.converter_factory(unit, kept)
        else:
            _LOGGER.warning(
                "Not importing %s, kept in %s rather than %s", statistic_id, kept, unit
            )
            del keys[key]

    columns = {c for key in keys for c in SENSOR_COLUMNS[key][:3] if c}
    # From when every statistic is Home Assistant's own: files after it are
    # not needed.
    end = max(first.values()) if len(first) == len(keys) else None
    imported = dict.fromkeys(keys.values(), 0)
    client = coordinator.client
    now = datetime.datetime.now(datetime.UTC)
    # A file at a time, so that only one is in memory.
    for url in client.station_history_urls(coordinator.weather_station, since, now):
        if end is not None and _file_start(url, now) >= end:
            continue
        hours = await client.get_station_hours(url, columns, since)
        for key, statistic_id in keys.items():
            source = SENSOR_COLUMNS[key]
            before = first.get(statistic_id)
            to_unit = convert[key]
            statistics = []
            for start, row in hours:
                if before is not None and start >= before:
                    continue
                values = {}
                for name in ("mean", "min", "max"):
                    column = getattr(source, name)
                    value = row.get(column) if column else None
                    if value is not None:
                        value /= source.divisor
                        values[name] = to_unit(value) if to_unit else value
                if "mean" in values:
                    statistics.append({"start": start, **values})
            if statistics:
                async_import_statistics(hass, metadata[statistic_id][1], statistics)
                imported[statistic_id] += len(statistics)
        _LOGGER.debug("Imported %s", url)
    return imported


def async_setup_services(hass: HomeAssistant) -> None:
    async def import_history(call: ServiceCall) -> ServiceResponse:
        entry = hass.config_entries.async_get_entry(call.data["config_entry_id"])
        if (
            entry is None
            or entry.domain != DOMAIN
            or entry.state is not ConfigEntryState.LOADED
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="entry_not_loaded"
            )
        coordinator = entry.runtime_data
        if not coordinator.weather_station:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_weather_station"
            )
        if coordinator.paused:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="downloads_paused"
            )
        if "recorder" not in hass.config.components:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_recorder"
            )
        now = datetime.datetime.now(datetime.UTC)
        since = (
            datetime.datetime.combine(
                call.data["since"], datetime.time(0), datetime.UTC
            )
            if "since" in call.data
            else default_since(now)
        )
        try:
            imported = await async_import_history(hass, coordinator, since)
        except OGDError as exc:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="history_unavailable",
                translation_placeholders={"error": str(exc)},
            ) from exc
        return {"since": since.date().isoformat(), "imported_hours": imported}

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_HISTORY,
        import_history,
        schema=IMPORT_HISTORY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
