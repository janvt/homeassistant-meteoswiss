"""Lifecycle of MeteoSwiss."""

import asyncio
import datetime
import logging
import time
from typing import Literal, TypedDict, cast

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import CONF_NAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.issue_registry import IssueSeverity
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from custom_components.meteoswiss.const import (
    CONF_FORECAST_NAME,
    CONF_POINT_ID,
    CONF_POSTCODE,
    CONF_PRECIPITATION_STATION,
    CONF_REAL_TIME_NAME,
    CONF_REAL_TIME_PRECIPITATION_NAME,
    CONF_STATION,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    USER_AGENT,
)
from custom_components.meteoswiss.ogd import (
    LocalForecast,
    MeteoSwissOGD,
    OGDError,
    Point,
    StationType,
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.WEATHER]
MAX_CONTINUOUS_ERROR_TIME = 60 * 60
# MeteoSwiss publishes a new forecast run every hour.  Look for one this often
# rather than on every (much more frequent) station update.
FORECAST_CHECK_INTERVAL = datetime.timedelta(minutes=15)
# Hourly temperatures are only published in a 33 MB file, so they are
# downloaded at most this often rather than for every run.
TEMPERATURE_REFRESH_INTERVAL = datetime.timedelta(hours=3)
# Leaves time for that download on a slow connection.
UPDATE_TIMEOUT = 300


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
    coordinator = MeteoSwissDataUpdateCoordinator(
        hass,
        interval,
        entry.data[CONF_POSTCODE],
        point_id=entry.data.get(CONF_POINT_ID),
        forecast_name=entry.data.get(CONF_FORECAST_NAME, name),
        weather_station=entry.data.get(CONF_STATION, None),
        real_time_weather_station_name=entry.data.get(CONF_REAL_TIME_NAME, None),
        precipitation_station=entry.data.get(CONF_PRECIPITATION_STATION, None),
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

    return unload_ok


class MeteoSwissClientResult(TypedDict):
    forecast: LocalForecast | None
    # Latest measurements of each configured station, by station.
    condition_by_station: dict[str, dict[str, float | None]]
    # The same, in order of preference.
    condition: list[dict[str, float | None]]
    # Forecast attributes.
    postcode: int  # e.g. 8005
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
        update_interval: datetime.timedelta,
        postcode: int,
        point_id: int | None,
        forecast_name: str,
        weather_station: str | None,
        real_time_weather_station_name: str | None,
        precipitation_station: str | None,
        real_time_precipitation_station_name: str | None,
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
        self.forecast_name = forecast_name
        _LOGGER.debug(
            "Forecast %s will be provided for post code %s every %s",
            forecast_name,
            postcode,
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
        self.real_time_precipitation_station_name = real_time_precipitation_station_name
        if precipitation_station:
            _LOGGER.debug(
                "Real-time precipitation %s will be updated from %s every %s",
                real_time_precipitation_station_name,
                precipitation_station,
                update_interval,
            )

        self.client = MeteoSwissOGD(async_get_clientsession(hass), USER_AGENT)
        self._point: Point | None = None
        self._forecast: LocalForecast | None = None
        self._forecast_checked: datetime.datetime | None = None
        self._temperature: dict[str, str] | None = None
        self._temperature_fetched: datetime.datetime | None = None
        _LOGGER.debug("Client obtained")

        super().__init__(
            hass,
            _LOGGER,
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
                    self.postcode, self.point_id
                )
            run = await self.client.latest_run(now)
            if self._forecast is None or self._forecast["run"] != run:
                await self._async_refresh_temperature(run, now)
                self._forecast = await self.client.get_forecast(
                    self._point, run, self._temperature
                )
                _LOGGER.debug("Forecast for run %s obtained", run)
            self._forecast_checked = now
        except OGDError as exc:
            if self._forecast is None:
                raise
            _LOGGER.warning("Keeping the previous forecast: %s", exc)
        return self._forecast

    async def _async_refresh_temperature(
        self, run: str, now: datetime.datetime
    ) -> None:
        """Fetch hourly temperatures if they are missing or old.

        Without them the forecast simply has no hourly temperatures, so a
        failure only keeps the previous ones.
        """
        assert self._point is not None
        if (
            self._temperature is not None
            and self._temperature_fetched is not None
            and now - self._temperature_fetched < TEMPERATURE_REFRESH_INTERVAL
        ):
            return
        try:
            self._temperature = await self.client.get_hourly_temperature(
                self._point, run
            )
            self._temperature_fetched = now
            _LOGGER.debug("Hourly temperatures for run %s obtained", run)
        except OGDError as exc:
            _LOGGER.warning("Cannot update the hourly temperatures: %s", exc)

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
                forecast = await self._async_get_forecast()
        except Exception as exc:
            _LOGGER.exception("Failed getting data")
            raise UpdateFailed(exc) from exc

        data = {
            "forecast": forecast,
            "condition_by_station": condition_by_station,
            "condition": [c for c in condition_by_station.values() if c],
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
                    if not self.error_raised and last_error > m:
                        ir.async_create_issue(
                            self.hass,
                            DOMAIN,
                            f"{station}_{name}_provides_no_data_{DOMAIN}",
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
                            f"{station}_{name}_provides_no_data_{DOMAIN}",
                        )
                    self.first_error[name] = None
                    self.error_raised[name] = False

        if not data["forecast"]:
            # Oh no.  The forecast is empty.
            _LOGGER.warning(
                "Post code %s provided us with no forecast",
                self.postcode,
            )
            if self.first_error[CONF_POSTCODE] is None:
                self.first_error[CONF_POSTCODE] = time.time()

            m = MAX_CONTINUOUS_ERROR_TIME
            last_error: float = time.time() - cast(
                float, self.first_error[CONF_POSTCODE]
            )
            if not self.error_raised and last_error > m:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    f"{self.postcode}_provides_no_forecast_{DOMAIN}",
                    is_fixable=False,
                    is_persistent=False,
                    severity=IssueSeverity.ERROR,
                    translation_key="postcode_no_data",
                    translation_placeholders={
                        "postcode": str(self.postcode),
                    },
                )
                self.error_raised[CONF_POSTCODE] = True
        else:
            if self.error_raised[CONF_POSTCODE]:
                ir.async_delete_issue(
                    self.hass,
                    DOMAIN,
                    f"{self.postcode}_provides_no_forecast_{DOMAIN}",
                )
            self.first_error[CONF_POSTCODE] = None
            self.error_raised[CONF_POSTCODE] = False

        newdata = cast(MeteoSwissClientResult, data)
        newdata[CONF_POSTCODE] = self.postcode
        newdata[CONF_FORECAST_NAME] = self.forecast_name
        newdata[CONF_STATION] = self.weather_station
        newdata[CONF_REAL_TIME_NAME] = self.real_time_weather_station_name
        newdata[CONF_PRECIPITATION_STATION] = self.precipitation_station
        newdata[CONF_REAL_TIME_PRECIPITATION_NAME] = (
            self.real_time_precipitation_station_name
        )
        return newdata
