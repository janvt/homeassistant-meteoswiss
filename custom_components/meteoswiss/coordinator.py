"""Fetching the data of a MeteoSwiss entry, and of its radar."""

import asyncio
import datetime
import logging
import time
from typing import Any, TypedDict, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.issue_registry import IssueSeverity
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util.hass_dict import HassKey

from custom_components.meteoswiss.alerts import AlertError, MeteoAlarm, WeatherWarning
from custom_components.meteoswiss.const import (
    CONF_FOEHN,
    CONF_FOEHN_NORTH,
    CONF_FOEHN_SOUTH,
    CONF_FORECAST_NAME,
    CONF_POSTCODE,
    CONF_PRECIPITATION_STATION,
    CONF_REAL_TIME_NAME,
    CONF_REAL_TIME_PRECIPITATION_NAME,
    CONF_STATION,
    DOMAIN,
    STATION_URL,
)
from custom_components.meteoswiss.downloads import SharedDownloads
from custom_components.meteoswiss.issues import (
    HIGH_USAGE_ISSUE,
    USAGE_NOTICE_PER_ENTRY,
    no_forecast_issue_id,
    station_issue_id,
)
from custom_components.meteoswiss.ogd import (
    FREEZING_LEVEL,
    HOURLY_TEMPERATURE,
    POINT_TYPE_POI,
    LocalForecast,
    MeteoSwissOGD,
    OGDError,
    Point,
    StationType,
    UnknownPointError,
    distance_km,
)
from custom_components.meteoswiss.outages import Outages
from custom_components.meteoswiss.pause import Pauses
from custom_components.meteoswiss.radar import RAIN_RATE, Radar
from custom_components.meteoswiss.radar_loop import RadarLoop
from custom_components.meteoswiss.usage import MonthToDate

_LOGGER = logging.getLogger(__name__)
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


def _update_failed(error: str) -> UpdateFailed:
    """An update that failed, with a message the user's language can show."""
    return UpdateFailed(
        translation_domain=DOMAIN,
        translation_key="update_failed",
        translation_placeholders={"error": error},
    )


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
        self._outages = Outages(_LOGGER)
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
        except TimeoutError:
            self._outages.failed("Forecast", "timed out; keeping the previous one")
            return
        except Exception as exc:
            self._outages.failed(
                "Forecast", f"{exc!r}; keeping the previous one", trace=True
            )
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
            self._outages.failed("Forecast", f"{exc}; keeping the previous one")
            return self._forecast
        self._outages.worked("Forecast")
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
    def device_identifiers(self) -> set[tuple[str, str]]:
        """The identifiers of the entry's devices: its forecast and stations."""
        devices = [self.device_info] + [
            self.station_device_info(station_type)
            for station_type, station in (
                (StationType.WEATHER, self.weather_station),
                (StationType.PRECIPITATION, self.precipitation_station),
                (StationType.POLLEN, self.pollen_station),
            )
            if station
        ]
        return {i for device in devices for i in device["identifiers"]}

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
            warnings = await self.alerts.get_warnings(
                self._point.lat,
                self._point.lon,
                self._point.height,
                self.hass.config.language,
                datetime.datetime.now(datetime.timezone.utc),
            )
        except AlertError as exc:
            self._outages.failed("Weather warnings", exc)
            return None
        self._outages.worked("Weather warnings")
        return warnings

    async def _async_get_today(self) -> dict[str, float | None]:
        """The weather station's highest and lowest temperature today."""
        if not self.climate_normals:
            return {}
        source = f"Today's temperatures at station {self.weather_station}"
        try:
            today = await self.client.get_station_today(self.weather_station)
        except OGDError as exc:
            self._outages.failed(source, exc)
            return {}
        self._outages.worked(source)
        return today

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
        source = f"Climate history of station {self.weather_station}"
        try:
            async with asyncio.timeout(FORECAST_TIMEOUT):
                self._climate = await self.client.get_climate(self.weather_station)
            self._climate_checked = now
        except (OGDError, TimeoutError) as exc:
            self._outages.failed(source, str(exc) or "timed out")
            return
        self._outages.worked(source)
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
            source = f"Föhn station {abbr}"
            try:
                now = await self.client.get_station_now(abbr, StationType.WEATHER)
            except OGDError as exc:
                self._outages.failed(source, exc)
                now = {}
            else:
                self._outages.worked(source)
            pressures[side] = now.get("pp0qffs0")
            pressures[f"{side}_station"] = abbr
        return pressures

    async def _async_get_pollen(self) -> dict[str, Any]:
        """The latest pollen measurements, empty without a station or data."""
        if not self.pollen_station:
            return {}
        source = f"Pollen station {self.pollen_station}"
        try:
            if self.pollen_station_name == self.pollen_station:
                stations = await self.client.get_stations(StationType.POLLEN)
                self.pollen_station_name = next(
                    (s.name for s in stations if s.abbr == self.pollen_station),
                    self.pollen_station,
                )
            pollen = await self.client.get_pollen_now(self.pollen_station)
        except OGDError as exc:
            self._outages.failed(source, exc)
            return {}
        self._outages.worked(source)
        return pollen

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
                    source = f"Station {station}"
                    try:
                        measured = await self.client.get_station_now(
                            station, station_type
                        )
                    except OGDError as exc:
                        self._outages.failed(source, exc)
                        measured = {}
                    else:
                        if measured:
                            self._outages.worked(source)
                        else:
                            self._outages.failed(source, "no real-time data")
                    condition_by_station[station] = measured
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
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="unknown_place",
                translation_placeholders={"name": self.forecast_name},
            ) from exc
        except (OGDError, AlertError, TimeoutError) as exc:
            # Logged by the coordinator, once until an update works again.
            raise _update_failed(str(exc) or "timed out") from exc
        except Exception as exc:
            # Unexpected, so with its traceback, but also only once.
            if self.last_update_success:
                _LOGGER.exception("Unexpected error updating %s", self.forecast_name)
            raise _update_failed(repr(exc)) from exc

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
                    # Logged above.  After an hour without data, the user is
                    # told to choose another station.
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

        no_forecast = f"Forecast for {self.forecast_name}"
        if not data["forecast"]:
            self._outages.failed(no_forecast, "MeteoSwiss provided none")
            if self.first_error[CONF_POSTCODE] is None:
                self.first_error[CONF_POSTCODE] = time.time()

            m = MAX_CONTINUOUS_ERROR_TIME
            last_error: float = time.time() - cast(
                float, self.first_error[CONF_POSTCODE]
            )
            if not self.error_raised[CONF_POSTCODE] and last_error > m:
                self._async_create_no_forecast_issue()
        else:
            self._outages.worked(no_forecast)
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
        self.loop = RadarLoop(hass, location, radar.usage)
        # Set by the entry's coordinator; see its async_set_paused.
        self.paused = False

    async def _async_update_data(self) -> RadarReadings:
        if self.paused:
            return self.data or {}
        now = datetime.datetime.now(datetime.timezone.utc)
        readings = await self._radar.get(*self.location, now)
        if (rain := self._radar.image(RAIN_RATE)) is not None:
            self.loop.add(rain)
        return readings


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
