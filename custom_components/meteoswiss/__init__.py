"""Lifecycle of MeteoSwiss."""

import asyncio
import datetime
import logging
import time
from typing import Any, Literal, TypedDict, cast

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import CONF_NAME, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.issue_registry import IssueSeverity
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from custom_components.meteoswiss.alerts import AlertError, MeteoAlarm, WeatherWarning
from custom_components.meteoswiss.const import (
    CONF_CLIMATE_NORMALS,
    CONF_FOEHN,
    CONF_FOEHN_NORTH,
    CONF_FOEHN_SOUTH,
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
    STATION_URL,
    USER_AGENT,
)
from custom_components.meteoswiss.downloads import SharedDownloads
from custom_components.meteoswiss.issues import (
    HIGH_USAGE_ISSUE,
    USAGE_NOTICE_PER_ENTRY,
    async_delete_issues,
    freezing_level_setting,
    no_forecast_issue_id,
    station_issue_id,
)
from custom_components.meteoswiss.ogd import (
    FREEZING_LEVEL,
    HOURLY_TEMPERATURE,
    POINT_TYPE_POI,
    POINT_TYPE_POSTCODE,
    LocalForecast,
    MeteoSwissOGD,
    OGDError,
    Point,
    StationType,
    UnknownPointError,
    distance_km,
)
from custom_components.meteoswiss.pause import Pauses
from custom_components.meteoswiss.radar import Radar
from custom_components.meteoswiss.usage import MonthToDate

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.SWITCH, Platform.WEATHER]
MAX_CONTINUOUS_ERROR_TIME = 60 * 60
# MeteoSwiss publishes a new forecast run every hour.  Look for one this often
# rather than on every (much more frequent) station update.
FORECAST_CHECK_INTERVAL = datetime.timedelta(minutes=15)
# Leaves time for the large downloads (see downloads.py) on a slow connection.
FORECAST_TIMEOUT = 300
# Stations, pollen and warnings, which are updated without waiting for those.
UPDATE_TIMEOUT = 60
# The client keeps a station's history for longer; this is how often to ask.
CLIMATE_CHECK_INTERVAL = datetime.timedelta(hours=1)
# Shared by all entries.
DATA_CLIENT: HassKey[MeteoSwissOGD] = HassKey(f"{DOMAIN}_client")
DATA_ALERTS: HassKey[MeteoAlarm] = HassKey(f"{DOMAIN}_alerts")
DATA_DOWNLOADS: HassKey[SharedDownloads] = HassKey(f"{DOMAIN}_downloads")
DATA_RADAR: HassKey[Radar] = HassKey(f"{DOMAIN}_radar")
DATA_MONTH: HassKey[MonthToDate] = HassKey(f"{DOMAIN}_month")
DATA_PAUSES: HassKey[Pauses] = HassKey(f"{DOMAIN}_pauses")
# Radar images come every 5 minutes, a minute or so after their interval.
RADAR_UPDATE_INTERVAL = datetime.timedelta(minutes=1)
# A place this close to the home is taken to be the home's: the radar is then
# read at the home, rather than at the postal code's centre a few km away.
RADAR_HOME_DISTANCE_KM = 10

type MeteoSwissConfigEntry = ConfigEntry[MeteoSwissDataUpdateCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> Literal[True]:
    """Setup via old entry in configuration.yaml."""
    _LOGGER.debug("Async setup: meteoswiss")

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
    """Update listener."""
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


class MeteoSwissClientResult(TypedDict):
    forecast: LocalForecast | None
    # Latest measurements of each configured station, by station: by
    # parameter, and "measured", the time of the latest.
    condition_by_station: dict[str, dict[str, Any]]
    # The same, in order of preference.
    condition: list[dict[str, Any]]
    # The latest pollen measurements by pollen type, and "measured".
    pollen: dict[str, Any]
    # The weather warnings for the place that have not expired; None if unknown.
    warnings: list[WeatherWarning] | None
    # Sea-level pressures for the Föhn outlook; None when it is turned off.
    foehn: dict[str, Any] | None
    # The weather station's highest and lowest temperature today.
    today: dict[str, float | None]
    # Its normals and records (climate.Climate) and this year's days, if known.
    climate: tuple[Any, list] | None
    # Forecast attributes.
    postcode: int | None  # e.g. 8005, None for mountains
    point: Point | None  # where the forecast is for
    forecast_name: str
    # Real-time weather station attributes.
    station: str | None  # e.g. BLA
    real_time_name: str | None
    # Real-time precipitation station attributes.
    precipitation_station: str | None  # e.g. BEL
    real_time_precipitation_name: str | None


class MeteoSwissDataUpdateCoordinator(DataUpdateCoordinator[MeteoSwissClientResult]):
    """Class to manage fetching MeteoSwiss data API."""

    data: MeteoSwissClientResult

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        update_interval: datetime.timedelta,
        postcode: int | None,
        point_id: int | None,
        point_type: int,
        client: MeteoSwissOGD,
        downloads: SharedDownloads,
        alerts: MeteoAlarm,
        hourly_temperature: bool,
        forecast_name: str,
        weather_station: str | None,
        real_time_weather_station_name: str | None,
        precipitation_station: str | None,
        real_time_precipitation_station_name: str | None,
        pollen_station: str | None = None,
        freezing_level: bool = False,
        foehn: dict[str, Any] | None = None,
        climate_normals: bool = False,
        paused: bool = False,
    ) -> None:
        """Initialize."""
        # Downloads paused by the user: the entities keep their last values.
        self.paused = paused
        self.first_error: dict[str, float | None] = {
            CONF_REAL_TIME_NAME: None,
            CONF_REAL_TIME_PRECIPITATION_NAME: None,
            CONF_POSTCODE: None,
        }
        self.error_raised = {
            CONF_REAL_TIME_NAME: False,
            CONF_REAL_TIME_PRECIPITATION_NAME: False,
            CONF_POSTCODE: False,
        }
        self.hass = hass
        self.postcode = postcode
        self.point_id = point_id
        self.point_type = point_type
        self.downloads = downloads
        self.hourly_temperature = hourly_temperature
        self.forecast_name = forecast_name
        _LOGGER.debug(
            "Forecast %s will be provided for post code %s / point %s every %s",
            forecast_name,
            postcode,
            point_id,
            update_interval,
        )

        self.weather_station = weather_station
        self.real_time_weather_station_name = real_time_weather_station_name
        if weather_station:
            _LOGGER.debug(
                "Real-time weather %s will be updated from %s every %s",
                real_time_weather_station_name,
                weather_station,
                update_interval,
            )

        self.precipitation_station = precipitation_station
        self.pollen_station = pollen_station
        self.freezing_level = freezing_level
        # Normals need the weather station's history.
        self.climate_normals = bool(climate_normals and weather_station)
        self._climate: tuple[Any, list] | None = None
        self._climate_checked: datetime.datetime | None = None
        self._climate_task: asyncio.Task | None = None
        # The Föhn settings (see const.FOEHN_DEFAULTS), or None.
        self.foehn = foehn if foehn and foehn[CONF_FOEHN] else None
        # Looked up with the first update, for the name of its device.
        self.pollen_station_name = pollen_station
        self.real_time_precipitation_station_name = real_time_precipitation_station_name
        if precipitation_station:
            _LOGGER.debug(
                "Real-time precipitation %s will be updated from %s every %s",
                real_time_precipitation_station_name,
                precipitation_station,
                update_interval,
            )

        self.client = client
        self.alerts = alerts
        # The radar's readings at the place, if turned on.
        self.radar: MeteoSwissRadarCoordinator | None = None
        self._point: Point | None = None
        self._forecast: LocalForecast | None = None
        self._forecast_checked: datetime.datetime | None = None
        # Refreshes the forecast while the stations go on updating.
        self._forecast_task: asyncio.Task | None = None
        _LOGGER.debug("Client obtained")

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=update_interval,
        )

    def _forecast_due(self, now: datetime.datetime) -> bool:
        return (
            self._forecast is None
            or self._forecast_checked is None
            or now - self._forecast_checked >= FORECAST_CHECK_INTERVAL
        )

    @callback
    def _async_refresh_forecast_soon(self) -> None:
        """Refresh the forecast in the background, if it is due.

        A refresh can take minutes on a slow connection (the large downloads),
        and the stations should not wait for it.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        # done(): a refresh can finish before it is even stored here.
        running = self._forecast_task is not None and not self._forecast_task.done()
        if not running and self._forecast_due(now):
            self._forecast_task = self.config_entry.async_create_background_task(
                self.hass, self._async_refresh_forecast(), "meteoswiss forecast"
            )

    async def _async_refresh_forecast(self) -> None:
        try:
            async with asyncio.timeout(FORECAST_TIMEOUT):
                forecast = await self._async_get_forecast()
        except Exception:
            _LOGGER.exception("Keeping the previous forecast")
            return
        if self.data is not None and forecast is not self.data["forecast"]:
            self.async_set_updated_data({**self.data, "forecast": forecast})

    async def _async_get_forecast(self) -> LocalForecast | None:
        """The forecast of the latest model run, kept if none can be obtained."""
        now = datetime.datetime.now(datetime.timezone.utc)
        if not self._forecast_due(now):
            return self._forecast
        try:
            return await self._async_fetch_forecast(now)
        finally:
            self._async_update_download_issues()

    async def _async_fetch_forecast(
        self, now: datetime.datetime
    ) -> LocalForecast | None:
        try:
            if self._point is None:
                self._point = await self.client.resolve_point(
                    self.postcode, self.point_id, self.point_type
                )
                self.downloads.register(
                    self.config_entry.entry_id, self._point, self._large_parameters
                )
            run = await self.client.latest_run(now)
            if self._forecast is None or self._forecast["run"] != run:
                large = {
                    parameter: await self.downloads.get(
                        self._point, parameter, run, now
                    )
                    for parameter in self._large_parameters
                }
                self._forecast = await self.client.get_forecast(
                    self._point,
                    run,
                    hourly_temperature=large.get(HOURLY_TEMPERATURE),
                    freezing_level=large.get(FREEZING_LEVEL),
                )
                _LOGGER.debug("Forecast for run %s obtained", run)
            self._forecast_checked = now
        except OGDError as exc:
            if self._forecast is None:
                raise
            _LOGGER.warning("Keeping the previous forecast: %s", exc)
        return self._forecast

    @callback
    def _async_update_download_issues(self) -> None:
        """Tell the user to update while MeteoSwiss's files cost far more than
        they should, which means MeteoSwiss changed something."""
        entries = len(self.hass.config_entries.async_loaded_entries(DOMAIN))
        downloaded = self.client.downloaded
        if downloaded > USAGE_NOTICE_PER_ENTRY * max(1, entries):
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                HIGH_USAGE_ISSUE,
                is_fixable=False,
                is_persistent=False,
                severity=IssueSeverity.WARNING,
                translation_key="high_usage",
                translation_placeholders={
                    "usage": str(round(downloaded / 1024 / 1024))
                },
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, HIGH_USAGE_ISSUE)

    @property
    def downloaded(self) -> int:
        """Bytes downloaded for all entries in the last day: forecasts and
        measurements, warnings and the radar."""
        radar = self.hass.data.get(DATA_RADAR)
        return (
            self.client.downloaded
            + self.alerts.downloaded
            + (radar.downloaded if radar is not None else 0)
        )

    @property
    def downloaded_this_month(self) -> int:
        """Bytes downloaded for all entries this calendar month, local time."""
        return self.hass.data[DATA_MONTH].downloaded

    @property
    def device_info(self) -> DeviceInfo:
        """The device that groups the entities of the entry."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.config_entry.entry_id)},
            name=self.forecast_name,
            manufacturer="MeteoSwiss",
            model="Mountain forecast" if self.is_mountain else "Postal code forecast",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://www.meteoswiss.admin.ch/",
        )

    def station_device_info(self, station_type: StationType) -> DeviceInfo:
        """The device of a station, named like its sensors.

        Home Assistant puts the device name before the names of its entities,
        so the sensors keep their names ("Fluntern temperature") under their
        station rather than under the forecast.  It is identified by its role,
        so choosing another station renames it.
        """
        if station_type == StationType.WEATHER:
            name = self.real_time_weather_station_name
            model = "Weather station"
        elif station_type == StationType.POLLEN:
            # The sensors are named after the pollen, e.g. "Zürich pollen Birch".
            name = f"{self.pollen_station_name} pollen"
            model = "Pollen station"
        else:
            name = self.real_time_precipitation_station_name
            model = "Precipitation station"
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.config_entry.entry_id}-{station_type}")},
            name=name,
            manufacturer="MeteoSwiss",
            model=model,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=STATION_URL,
        )

    @property
    def is_mountain(self) -> bool:
        return self.point_type == POINT_TYPE_POI

    @property
    def _large_parameters(self) -> set[str]:
        """The large files this entry needs, which are only fetched on request."""
        parameters = set()
        if self.hourly_temperature:
            parameters.add(HOURLY_TEMPERATURE)
        if self.freezing_level:
            parameters.add(FREEZING_LEVEL)
        return parameters

    @property
    def _no_forecast_issue_id(self) -> str:
        return no_forecast_issue_id(self.postcode, self.point_type, self.point_id)

    def _async_create_no_forecast_issue(self) -> None:
        if self.is_mountain:
            translation_key = "point_no_data"
            placeholders = {"name": self.forecast_name}
        else:
            translation_key = "postcode_no_data"
            placeholders = {"postcode": str(self.postcode)}
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._no_forecast_issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=IssueSeverity.ERROR,
            translation_key=translation_key,
            translation_placeholders=placeholders,
        )
        self.error_raised[CONF_POSTCODE] = True

    async def _async_get_warnings(self) -> list[WeatherWarning] | None:
        """The warnings for the forecast's place, None if they are unknown."""
        if self._point is None:
            return None
        try:
            return await self.alerts.get_warnings(
                self._point.lat,
                self._point.lon,
                self._point.height,
                self.hass.config.language,
                datetime.datetime.now(datetime.timezone.utc),
            )
        except AlertError as exc:
            _LOGGER.warning("Weather warnings: %s", exc)
            return None

    async def _async_get_today(self) -> dict[str, float | None]:
        """The weather station's highest and lowest temperature today."""
        if not self.climate_normals:
            return {}
        try:
            return await self.client.get_station_today(self.weather_station)
        except OGDError as exc:
            _LOGGER.warning("Station %s: %s", self.weather_station, exc)
            return {}

    @callback
    def _async_refresh_climate_soon(self) -> None:
        """Fetch the station's history in the background (6 MB at first)."""
        if not self.climate_normals:
            return
        now = datetime.datetime.now(datetime.timezone.utc)
        running = self._climate_task is not None and not self._climate_task.done()
        due = self._climate_checked is None or (
            now - self._climate_checked >= CLIMATE_CHECK_INTERVAL
        )
        if not running and due:
            self._climate_task = self.config_entry.async_create_background_task(
                self.hass, self._async_refresh_climate(now), "meteoswiss climate"
            )

    async def _async_refresh_climate(self, now: datetime.datetime) -> None:
        try:
            async with asyncio.timeout(FORECAST_TIMEOUT):
                self._climate = await self.client.get_climate(self.weather_station)
            self._climate_checked = now
        except (OGDError, TimeoutError) as exc:
            _LOGGER.warning("Climate of %s: %s", self.weather_station, exc)
            return
        if self.data is not None:
            self.async_set_updated_data({**self.data, "climate": self._climate})

    async def _async_get_foehn(self) -> dict[str, Any] | None:
        """The sea-level pressures south and north of the Alps, if wanted.

        Missing values (a station without recent data) are None.
        """
        if not self.foehn:
            return None
        pressures: dict[str, Any] = {}
        for side, key in (("south", CONF_FOEHN_SOUTH), ("north", CONF_FOEHN_NORTH)):
            abbr = self.foehn[key]
            try:
                now = await self.client.get_station_now(abbr, StationType.WEATHER)
            except OGDError as exc:
                _LOGGER.warning("Föhn station %s: %s", abbr, exc)
                now = {}
            pressures[side] = now.get("pp0qffs0")
            pressures[f"{side}_station"] = abbr
        return pressures

    async def _async_get_pollen(self) -> dict[str, Any]:
        """The latest pollen measurements, empty without a station or data."""
        if not self.pollen_station:
            return {}
        try:
            if self.pollen_station_name == self.pollen_station:
                stations = await self.client.get_stations(StationType.POLLEN)
                self.pollen_station_name = next(
                    (s.name for s in stations if s.abbr == self.pollen_station),
                    self.pollen_station,
                )
            return await self.client.get_pollen_now(self.pollen_station)
        except OGDError as exc:
            _LOGGER.warning("Pollen station %s: %s", self.pollen_station, exc)
            return {}

    async def async_set_paused(self, paused: bool) -> None:
        """Pause or resume this entry's downloads, until changed again."""
        entry_id = self.config_entry.entry_id
        await self.hass.data[DATA_PAUSES].async_set(entry_id, paused)
        self.paused = paused
        if self.radar is not None:
            self.radar.paused = paused
        if paused:
            # The shared files are no longer read for its place.
            self.downloads.unregister(entry_id)
            self.async_update_listeners()
        elif self._point is None:
            # Paused since it was set up, so it has nothing yet: set it up
            # again, with the radar if it is turned on.
            self.hass.config_entries.async_schedule_reload(entry_id)
        else:
            self.downloads.register(entry_id, self._point, self._large_parameters)
            # Now rather than debounced: the values may be days old.
            await self.async_refresh()
            if self.radar is not None:
                await self.radar.async_refresh()

    def _paused_data(self) -> MeteoSwissClientResult:
        """The last data, or none if paused since it was set up."""
        if self.data is not None:
            return self.data
        return self._with_entry_attributes(
            {
                "forecast": None,
                "condition_by_station": {},
                "condition": [],
                "pollen": {},
                "warnings": None,
                "foehn": None,
                "today": {},
                "climate": None,
            }
        )

    async def _async_update_data(self) -> MeteoSwissClientResult:
        """Update data from MeteoSwiss open data."""
        if self.paused:
            return self._paused_data()
        condition_by_station: dict[str, dict[str, Any]] = {}
        # Only the first forecast is waited for: there is none to show before.
        first = self._forecast is None
        try:
            async with asyncio.timeout(FORECAST_TIMEOUT if first else UPDATE_TIMEOUT):
                for station, station_type in (
                    (self.weather_station, StationType.WEATHER),
                    (self.precipitation_station, StationType.PRECIPITATION),
                ):
                    if not station:
                        continue
                    try:
                        condition_by_station[
                            station
                        ] = await self.client.get_station_now(station, station_type)
                    except OGDError as exc:
                        _LOGGER.warning("Station %s: %s", station, exc)
                        condition_by_station[station] = {}
                pollen = await self._async_get_pollen()
                foehn = await self._async_get_foehn()
                today = await self._async_get_today()
                self._async_refresh_climate_soon()
                if first:
                    await self._async_get_forecast()
                else:
                    self._async_refresh_forecast_soon()
                warnings = await self._async_get_warnings()
        except UnknownPointError as exc:
            # This will not fix itself, so tell the user straight away.
            self._async_create_no_forecast_issue()
            raise UpdateFailed(exc) from exc
        except Exception as exc:
            _LOGGER.exception("Failed getting data")
            raise UpdateFailed(exc) from exc

        data = {
            # Read only now: a refresh in the background may have finished.
            "forecast": self._forecast,
            "condition_by_station": condition_by_station,
            "condition": [c for c in condition_by_station.values() if c],
            "pollen": pollen,
            "warnings": warnings,
            "foehn": foehn,
            "today": today,
            "climate": self._climate,
        }

        # _LOGGER.debug("Data obtained (%s):\n%s", type(data), pprint.pformat(data))
        for station, name in (
            (self.weather_station, CONF_REAL_TIME_NAME),
            (
                self.precipitation_station,
                CONF_REAL_TIME_PRECIPITATION_NAME,
            ),
        ):
            if station:
                if not data["condition_by_station"].get(station):
                    # Oh no.  We could not retrieve the URL.
                    # We try 20 times.  If it does not succeed,
                    # we will induce a config error.
                    _LOGGER.warning(
                        "Station %s (%s) provided us with no real-time data",
                        station,
                        name,
                    )
                    if self.first_error[name] is None:
                        self.first_error[name] = time.time()

                    m = MAX_CONTINUOUS_ERROR_TIME
                    last_error: float = time.time() - cast(
                        float, self.first_error[name]
                    )
                    if not self.error_raised[name] and last_error > m:
                        ir.async_create_issue(
                            self.hass,
                            DOMAIN,
                            station_issue_id(station, name),
                            is_fixable=False,
                            is_persistent=False,
                            severity=IssueSeverity.ERROR,
                            translation_key="station_no_data",
                            translation_placeholders={
                                "station": station,
                            },
                        )
                        self.error_raised[name] = True
                else:
                    if self.error_raised[name]:
                        ir.async_delete_issue(
                            self.hass,
                            DOMAIN,
                            station_issue_id(station, name),
                        )
                    self.first_error[name] = None
                    self.error_raised[name] = False

        if not data["forecast"]:
            # Oh no.  The forecast is empty.
            _LOGGER.warning(
                "%s (postal code %s) provided us with no forecast",
                self.forecast_name,
                self.postcode,
            )
            if self.first_error[CONF_POSTCODE] is None:
                self.first_error[CONF_POSTCODE] = time.time()

            m = MAX_CONTINUOUS_ERROR_TIME
            last_error: float = time.time() - cast(
                float, self.first_error[CONF_POSTCODE]
            )
            if not self.error_raised[CONF_POSTCODE] and last_error > m:
                self._async_create_no_forecast_issue()
        else:
            if self.error_raised[CONF_POSTCODE]:
                ir.async_delete_issue(self.hass, DOMAIN, self._no_forecast_issue_id)
            self.first_error[CONF_POSTCODE] = None
            self.error_raised[CONF_POSTCODE] = False

        return self._with_entry_attributes(data)

    def _with_entry_attributes(self, data: dict[str, Any]) -> MeteoSwissClientResult:
        newdata = cast(MeteoSwissClientResult, data)
        newdata[CONF_POSTCODE] = self.postcode
        newdata["point"] = self._point
        newdata[CONF_FORECAST_NAME] = self.forecast_name
        newdata[CONF_STATION] = self.weather_station
        newdata[CONF_REAL_TIME_NAME] = self.real_time_weather_station_name
        newdata[CONF_PRECIPITATION_STATION] = self.precipitation_station
        newdata[CONF_REAL_TIME_PRECIPITATION_NAME] = (
            self.real_time_precipitation_station_name
        )
        return newdata


type RadarReadings = dict[str, tuple[float | None, datetime.datetime] | None]


class MeteoSwissRadarCoordinator(DataUpdateCoordinator[RadarReadings]):
    """The radar's readings at an entry's place, apart from its other data."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        radar: Radar,
        name: str,
        location: tuple[float, float],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"MeteoSwiss radar {name}",
            update_interval=RADAR_UPDATE_INTERVAL,
        )
        self._radar = radar
        self.location = location
        # Set by the entry's coordinator; see its async_set_paused.
        self.paused = False

    async def _async_update_data(self) -> RadarReadings:
        if self.paused:
            return self.data or {}
        now = datetime.datetime.now(datetime.timezone.utc)
        return await self._radar.get(*self.location, now)


def radar_location(hass: HomeAssistant, point: Point) -> tuple[float, float]:
    """Where the radar is read for a place: at the home if it is near.

    Not for mountains, whose place is their summit.
    """
    home = (hass.config.latitude, hass.config.longitude)
    if (
        point.point_type != POINT_TYPE_POI
        and distance_km(point.lat, point.lon, *home) <= RADAR_HOME_DISTANCE_KM
    ):
        return home
    return point.lat, point.lon
