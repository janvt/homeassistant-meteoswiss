"""Lifecycle of MeteoSwiss."""

import asyncio
import datetime
import logging
import time
from typing import Any, Literal, TypedDict, cast

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import CONF_NAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.issue_registry import IssueSeverity
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from custom_components.meteoswiss.alerts import AlertError, MeteoAlarm, WeatherWarning
from custom_components.meteoswiss.const import (
    CONF_FORECAST_NAME,
    CONF_HOURLY_TEMPERATURE,
    CONF_POINT_ID,
    CONF_POINT_TYPE,
    CONF_POLLEN_STATION,
    CONF_POSTCODE,
    CONF_PRECIPITATION_STATION,
    CONF_REAL_TIME_NAME,
    CONF_REAL_TIME_PRECIPITATION_NAME,
    CONF_STATION,
    CONF_UPDATE_INTERVAL,
    DEFAULT_HOURLY_TEMPERATURE,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    STATION_URL,
    USER_AGENT,
)
from custom_components.meteoswiss.downloads import SharedDownloads
from custom_components.meteoswiss.issues import (
    async_delete_issues,
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
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.WEATHER]
MAX_CONTINUOUS_ERROR_TIME = 60 * 60
# MeteoSwiss publishes a new forecast run every hour.  Look for one this often
# rather than on every (much more frequent) station update.
FORECAST_CHECK_INTERVAL = datetime.timedelta(minutes=15)
# Leaves time for the large downloads (see downloads.py) on a slow connection.
UPDATE_TIMEOUT = 300
# Shared by all entries.
DATA_CLIENT = f"{DOMAIN}_client"
DATA_ALERTS = f"{DOMAIN}_alerts"
DATA_DOWNLOADS = f"{DOMAIN}_downloads"


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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})

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
        # One client for all entries, so that they share what they download.
        hass.data[DATA_CLIENT] = MeteoSwissOGD(
            async_get_clientsession(hass), USER_AGENT
        )
        hass.data[DATA_DOWNLOADS] = SharedDownloads(hass.data[DATA_CLIENT])
        hass.data[DATA_ALERTS] = MeteoAlarm(async_get_clientsession(hass), USER_AGENT)
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
        forecast_name=entry.data.get(CONF_FORECAST_NAME, name),
        weather_station=entry.data.get(CONF_STATION, None),
        real_time_weather_station_name=entry.data.get(CONF_REAL_TIME_NAME, None),
        precipitation_station=entry.data.get(CONF_PRECIPITATION_STATION, None),
        pollen_station=entry.data.get(CONF_POLLEN_STATION),
        real_time_precipitation_station_name=entry.data.get(
            CONF_REAL_TIME_PRECIPITATION_NAME, None
        ),
    )
    await coordinator.async_config_entry_first_refresh()

    entry.async_on_unload(entry.add_update_listener(update_listener))

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(
        entry,
        PLATFORMS,
    )

    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the notices of a removed entry, which would stay until a restart."""
    async_delete_issues(hass, entry.data)


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update listener."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry,
        PLATFORMS,
    )

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        downloads: SharedDownloads = hass.data[DATA_DOWNLOADS]
        downloads.unregister(entry.entry_id)
        if not hass.data[DOMAIN]:
            hass.data.pop(DATA_DOWNLOADS)
            hass.data.pop(DATA_CLIENT)
            hass.data.pop(DATA_ALERTS)

    return unload_ok


class MeteoSwissClientResult(TypedDict):
    forecast: LocalForecast | None
    # Latest measurements of each configured station, by station.
    condition_by_station: dict[str, dict[str, float | None]]
    # The same, in order of preference.
    condition: list[dict[str, float | None]]
    # The latest pollen measurements by pollen type, and "measured".
    pollen: dict[str, Any]
    # The weather warnings for the place that have not expired; None if unknown.
    warnings: list[WeatherWarning] | None
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
    ) -> None:
        """Initialize."""
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
        self._point: Point | None = None
        self._forecast: LocalForecast | None = None
        self._forecast_checked: datetime.datetime | None = None
        _LOGGER.debug("Client obtained")

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=update_interval,
        )

    async def _async_get_forecast(self) -> LocalForecast | None:
        """The forecast of the latest model run, kept if none can be obtained."""
        now = datetime.datetime.now(datetime.timezone.utc)
        if (
            self._forecast is not None
            and self._forecast_checked is not None
            and now - self._forecast_checked < FORECAST_CHECK_INTERVAL
        ):
            return self._forecast
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
        if not self.hourly_temperature:
            return set()
        if self.is_mountain:
            return {HOURLY_TEMPERATURE, FREEZING_LEVEL}
        return {HOURLY_TEMPERATURE}

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

    async def _async_update_data(self) -> MeteoSwissClientResult:
        """Update data from MeteoSwiss open data."""
        condition_by_station: dict[str, dict[str, float | None]] = {}
        try:
            async with asyncio.timeout(UPDATE_TIMEOUT):
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
                forecast = await self._async_get_forecast()
                warnings = await self._async_get_warnings()
        except UnknownPointError as exc:
            # This will not fix itself, so tell the user straight away.
            self._async_create_no_forecast_issue()
            raise UpdateFailed(exc) from exc
        except Exception as exc:
            _LOGGER.exception("Failed getting data")
            raise UpdateFailed(exc) from exc

        data = {
            "forecast": forecast,
            "condition_by_station": condition_by_station,
            "condition": [c for c in condition_by_station.values() if c],
            "pollen": pollen,
            "warnings": warnings,
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
