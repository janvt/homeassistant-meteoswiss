"""Lifecycle of MeteoSwiss."""

import datetime
import logging
from typing import Literal

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import CONF_NAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.issue_registry import IssueSeverity
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from custom_components.meteoswiss.alerts import MeteoAlarm
from custom_components.meteoswiss.const import (
    CONF_CLIMATE_NORMALS,
    CONF_FORECAST_NAME,
    CONF_HOURLY_TEMPERATURE,
    CONF_POINT_ID,
    CONF_POINT_TYPE,
    CONF_POLLEN_STATION,
    CONF_POSTCODE,
    CONF_PRECIPITATION_STATION,
    CONF_RADAR,
    CONF_REAL_TIME_NAME,
    CONF_REAL_TIME_PRECIPITATION_NAME,
    CONF_STATION,
    CONF_UPDATE_INTERVAL,
    DEFAULT_CLIMATE_NORMALS,
    DEFAULT_HOURLY_TEMPERATURE,
    DEFAULT_RADAR,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    FOEHN_DEFAULTS,
    RELOADS_ON_OPTIONS_CHANGE,
    USER_AGENT,
)
from custom_components.meteoswiss.coordinator import (
    DATA_ALERTS,
    DATA_CLIENT,
    DATA_DOWNLOADS,
    DATA_MONTH,
    DATA_PAUSES,
    DATA_RADAR,
    MeteoSwissConfigEntry,
    MeteoSwissDataUpdateCoordinator,
    MeteoSwissRadarCoordinator,
    radar_location,
)
from custom_components.meteoswiss.downloads import SharedDownloads
from custom_components.meteoswiss.history import async_setup_services
from custom_components.meteoswiss.issues import (
    async_delete_issues,
    freezing_level_setting,
)
from custom_components.meteoswiss.ogd import POINT_TYPE_POSTCODE, MeteoSwissOGD
from custom_components.meteoswiss.pause import Pauses
from custom_components.meteoswiss.radar import Radar
from custom_components.meteoswiss.usage import MonthToDate

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.IMAGE, Platform.SENSOR, Platform.SWITCH, Platform.WEATHER]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> Literal[True]:
    """Setup via old entry in configuration.yaml."""
    _LOGGER.debug("Async setup: meteoswiss")
    async_setup_services(hass)

    conf = config.get(DOMAIN)
    if conf is None:
        return True

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_IMPORT}, data=conf
        )
    )
    _LOGGER.debug("END Async setup: meteoswiss")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: MeteoSwissConfigEntry) -> bool:
    _LOGGER.debug("Current configuration: %s", entry.data)
    name = entry.data.get(
        CONF_FORECAST_NAME,
        entry.data.get(
            CONF_REAL_TIME_NAME,
            entry.data.get(CONF_NAME, ""),
        ),
    )
    if not name:
        entry_id = entry.entry_id
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"{entry_id}_improperly_configured_{DOMAIN}",
            is_fixable=False,
            is_persistent=False,
            severity=IssueSeverity.ERROR,
            translation_key="improperly_configured",
            translation_placeholders={
                "entry_id": entry_id,
            },
        )
        return False

    interval = datetime.timedelta(
        seconds=entry.data.get(
            CONF_UPDATE_INTERVAL,
            DEFAULT_UPDATE_INTERVAL,
        )
        * 60
    )
    if DATA_CLIENT not in hass.data:
        # What all entries download this month, counted from here on.
        month = MonthToDate(Store(hass, 1, f"{DOMAIN}.usage"), dt_util.now)
        await month.async_load()
        hass.data[DATA_MONTH] = month
        pauses = Pauses(Store(hass, 1, f"{DOMAIN}.paused"))
        await pauses.async_load()
        hass.data[DATA_PAUSES] = pauses
        # One client for all entries, so that they share what they download.
        hass.data[DATA_CLIENT] = MeteoSwissOGD(
            async_get_clientsession(hass), USER_AGENT
        )
        hass.data[DATA_DOWNLOADS] = SharedDownloads(hass.data[DATA_CLIENT])
        hass.data[DATA_ALERTS] = MeteoAlarm(async_get_clientsession(hass), USER_AGENT)
        hass.data[DATA_CLIENT].usage.on_count = month.add
        hass.data[DATA_ALERTS].usage.on_count = month.add
    coordinator = MeteoSwissDataUpdateCoordinator(
        hass,
        entry,
        interval,
        entry.data.get(CONF_POSTCODE),
        point_id=entry.data.get(CONF_POINT_ID),
        point_type=entry.data.get(CONF_POINT_TYPE, POINT_TYPE_POSTCODE),
        client=hass.data[DATA_CLIENT],
        downloads=hass.data[DATA_DOWNLOADS],
        alerts=hass.data[DATA_ALERTS],
        # Set up with the entry, then changeable in the options.
        hourly_temperature=entry.options.get(
            CONF_HOURLY_TEMPERATURE,
            entry.data.get(CONF_HOURLY_TEMPERATURE, DEFAULT_HOURLY_TEMPERATURE),
        ),
        freezing_level=freezing_level_setting(entry.data, entry.options),
        forecast_name=entry.data.get(CONF_FORECAST_NAME, name),
        weather_station=entry.data.get(CONF_STATION, None),
        real_time_weather_station_name=entry.data.get(CONF_REAL_TIME_NAME, None),
        precipitation_station=entry.data.get(CONF_PRECIPITATION_STATION, None),
        pollen_station=entry.data.get(CONF_POLLEN_STATION),
        foehn={**FOEHN_DEFAULTS, **entry.options},
        climate_normals=entry.options.get(
            CONF_CLIMATE_NORMALS, DEFAULT_CLIMATE_NORMALS
        ),
        real_time_precipitation_station_name=entry.data.get(
            CONF_REAL_TIME_PRECIPITATION_NAME, None
        ),
        paused=entry.entry_id in hass.data[DATA_PAUSES],
    )
    await coordinator.async_config_entry_first_refresh()
    point = coordinator.data["point"]
    if entry.options.get(CONF_RADAR, DEFAULT_RADAR) and point is not None:
        if DATA_RADAR not in hass.data:
            hass.data[DATA_RADAR] = Radar(async_get_clientsession(hass), USER_AGENT)
            hass.data[DATA_RADAR].usage.on_count = hass.data[DATA_MONTH].add
        coordinator.radar = MeteoSwissRadarCoordinator(
            hass, entry, hass.data[DATA_RADAR], point.name, radar_location(hass, point)
        )
        # Not first_refresh: the rest of the entry works without the radar.
        await coordinator.radar.async_refresh()

    if not RELOADS_ON_OPTIONS_CHANGE:
        entry.async_on_unload(entry.add_update_listener(update_listener))

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(
        entry,
        PLATFORMS,
    )

    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the notices of a removed entry, which would stay until a restart,
    and its pause."""
    async_delete_issues(hass, entry.data)
    pauses = hass.data.get(DATA_PAUSES)
    if pauses is None:
        # It was the last entry.
        pauses = Pauses(Store(hass, 1, f"{DOMAIN}.paused"))
        await pauses.async_load()
    if entry.entry_id in pauses:
        await pauses.async_set(entry.entry_id, False)


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload on an options change, for Home Assistant before 2025.8."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: MeteoSwissConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry,
        PLATFORMS,
    )

    if unload_ok:
        hass.data[DATA_DOWNLOADS].unregister(entry.entry_id)
        others = [
            e
            for e in hass.config_entries.async_loaded_entries(DOMAIN)
            if e.entry_id != entry.entry_id
        ]
        if not others:
            # The last entry: what was downloaded for all of them can go.
            hass.data.pop(DATA_DOWNLOADS)
            hass.data.pop(DATA_CLIENT)
            hass.data.pop(DATA_ALERTS)
            hass.data.pop(DATA_RADAR, None)
            await hass.data.pop(DATA_MONTH).async_save()
            hass.data.pop(DATA_PAUSES)

    return unload_ok
