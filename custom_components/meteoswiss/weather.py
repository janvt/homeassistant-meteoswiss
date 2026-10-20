"""Support for the MeteoSwiss service."""

from __future__ import annotations

import datetime
import logging

from homeassistant.components.weather import (
    ATTR_FORECAST_CLOUD_COVERAGE,
    ATTR_FORECAST_CONDITION,
    ATTR_FORECAST_NATIVE_PRECIPITATION,
    ATTR_FORECAST_NATIVE_TEMP,
    ATTR_FORECAST_NATIVE_TEMP_LOW,
    ATTR_FORECAST_NATIVE_WIND_GUST_SPEED,
    ATTR_FORECAST_NATIVE_WIND_SPEED,
    ATTR_FORECAST_PRECIPITATION_PROBABILITY,
    ATTR_FORECAST_TIME,
    ATTR_FORECAST_WIND_BEARING,
    Forecast,
    WeatherEntity,
    WeatherEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    STATE_UNAVAILABLE,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from custom_components.meteoswiss import (
    MeteoSwissClientResult,
    MeteoSwissDataUpdateCoordinator,
)
from custom_components.meteoswiss.const import (
    CODE_TO_CONDITION_MAP,
    CONF_POSTCODE,
    CONF_PRECIPITATION_STATION,
    CONF_REAL_TIME_NAME,
    CONF_REAL_TIME_PRECIPITATION_NAME,
    CONF_STATION,
    DOMAIN,
    STATION_URL,
)
from custom_components.meteoswiss.ogd import HourlyForecast, current_hour

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up weather entity."""
    c: MeteoSwissDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MeteoSwissWeather(entry.entry_id, c)], True)


def condition_name_to_first_value(
    condition: None | list[dict[str, float | None]], name: str
) -> float | None:
    if not condition:
        # Real-time weather station provides no data.
        _LOGGER.debug("Current condition is empty for all stations: %s", condition)
        return None
    for n, row in enumerate(condition):
        try:
            value = row[name]  # type:ignore[literal-required]
        except Exception:
            _LOGGER.exception(
                "Current condition %s (%s) has no value for %s", n, row, name
            )
            continue
        if value is None or value == "-":
            _LOGGER.debug(
                "Value %s of current condition %s (%s) is %s, so not available",
                name,
                n,
                row,
                value,
            )
            continue
        try:
            return float(value)
        except Exception:
            _LOGGER.exception(
                "Error converting %s to float for condition in row %s (%s)",
                value,
                n,
                row,
            )
            continue
    return None


class MeteoSwissWeather(
    CoordinatorEntity[MeteoSwissDataUpdateCoordinator],
    WeatherEntity,
):
    _attr_has_entity_name = True
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_native_pressure_unit = UnitOfPressure.HPA
    _attr_native_wind_speed_unit = UnitOfSpeed.KILOMETERS_PER_HOUR
    _attr_supported_features = (
        WeatherEntityFeature.FORECAST_DAILY | WeatherEntityFeature.FORECAST_HOURLY
    )

    def __init__(
        self,
        integration_id: str,
        coordinator: MeteoSwissDataUpdateCoordinator,
    ):
        super().__init__(coordinator)
        self._invalid_legacy_icon_logged = False
        self._attr_unique_id = "weather.%s" % integration_id
        self._attr_device_info = coordinator.device_info
        self._attr_postcode = coordinator.data[CONF_POSTCODE]
        self._attr_station = coordinator.data[CONF_STATION]
        self._attr_weather_station = self._attr_station
        self._attr_weather_station_name = coordinator.data[CONF_REAL_TIME_NAME]
        self._attr_precipitation_station = coordinator.data[CONF_PRECIPITATION_STATION]
        self._attr_precipitation_station_name = coordinator.data[
            CONF_REAL_TIME_PRECIPITATION_NAME
        ]

        point = coordinator.data["point"]
        # A summit has no station, so show the forecast for the current hour.
        self._from_forecast = coordinator.is_mountain and not self._attr_station
        a = "Data provided by MeteoSwiss."
        if coordinator.is_mountain:
            a += "  Forecasts for %s" % (point.name if point else "a mountain",)
            if point and point.height is not None:
                a += " (%.0f m)" % (point.height,)
            a += "."
            self._attr_extra_state_attributes = {
                "elevation": point.height if point else None
            }
        else:
            a += "  Forecasts from postal code %s." % (self._attr_postcode,)
        if self._from_forecast:
            a += "  Current conditions are forecast, not measured."
        if self._attr_weather_station:
            a += "  Real-time weather data from weather station %s (%s)." % (
                self._attr_weather_station,
                self._attr_weather_station_name,
            )
        if self._attr_precipitation_station:
            a += "  Real-time precipitation data from weather station %s (%s)." % (
                self._attr_precipitation_station,
                self._attr_precipitation_station_name,
            )
        if self._attr_weather_station or self._attr_precipitation_station:
            url = STATION_URL
            a += "  Stations available at %s ." % (url,)
        else:
            a += "  No real-time stations used by this weather entry."
        self._attr_attribution = a

        # Named after the device, which has the name of the forecast.
        self._attr_name = None
        self.__set_data(coordinator.data)

    def __set_data(self, data: MeteoSwissClientResult) -> None:
        self._forecastData = data["forecast"]
        self._condition_for_all_stations = data["condition"]

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle data update."""
        data = self.coordinator.data
        self.__set_data(data)
        self.async_write_ha_state()

    def _measured_or_forecast(
        self, station_code: str, forecast_key: str
    ) -> float | None:
        """A station measurement, or the current hour's forecast for summits."""
        if self._from_forecast:
            current = self._current_hour()
            return current[forecast_key] if current else None  # type: ignore[literal-required]
        return condition_name_to_first_value(
            self._condition_for_all_stations, station_code
        )

    @property
    def native_temperature(self) -> float | None:
        return self._measured_or_forecast("tre200s0", "temperature")

    @property
    def native_pressure(self) -> float | None:
        return condition_name_to_first_value(
            self._condition_for_all_stations, "prestas0"
        )

    @property
    def humidity(self) -> float | None:
        return condition_name_to_first_value(
            self._condition_for_all_stations, "ure200s0"
        )

    @property
    def native_wind_speed(self) -> float | None:
        return self._measured_or_forecast("fu3010z0", "wind_speed")

    @property
    def native_wind_gust_speed(self) -> float | None:
        return self._measured_or_forecast("fu3010z1", "wind_gust")

    @property
    def native_dew_point(self) -> float | None:
        return condition_name_to_first_value(
            self._condition_for_all_stations, "tde200s0"
        )

    @property
    def wind_bearing(self) -> float | None:
        return self._measured_or_forecast("dkl010z0", "wind_bearing")

    @property
    def cloud_coverage(self) -> float | None:
        # Stations do not measure clouds.
        if not self._from_forecast:
            return None
        current = self._current_hour()
        return current["cloud_coverage"] if current else None

    def _current_hour(self) -> HourlyForecast | None:
        """The forecast of the hour we are in."""
        if not self._forecastData:
            return None
        now = datetime.datetime.now(datetime.timezone.utc)
        return current_hour(self._forecastData["hourly"], now)

    @property
    def condition(self) -> str | None:
        current = self._current_hour()
        symbolId = current["symbol"] if current else None
        cond = CODE_TO_CONDITION_MAP.get(symbolId, ("", None))[0] if symbolId else ""
        if not cond:
            _LOGGER.error(
                "MeteoSwiss returned no recognized current-weather symbol: %r",
                symbolId,
            )
            return STATE_UNAVAILABLE
        _LOGGER.debug("Current symbol is %s, condition is: %s", symbolId, cond)
        return str(cond)

    def _daily_forecast(self) -> list[Forecast] | None:
        if not self._forecastData:
            return None
        fcdata_out: list[Forecast] = []
        for forecast in self._forecastData["daily"]:
            symbol = forecast["symbol"]
            data_out: Forecast = {
                ATTR_FORECAST_TIME: forecast["date"],
                ATTR_FORECAST_NATIVE_TEMP_LOW: forecast["temperature_min"],
                ATTR_FORECAST_NATIVE_TEMP: forecast["temperature_max"],
                ATTR_FORECAST_CONDITION: (
                    str(CODE_TO_CONDITION_MAP.get(symbol, ("", None))[0]) or None
                    if symbol
                    else None
                ),
                ATTR_FORECAST_NATIVE_PRECIPITATION: forecast["precipitation"],
                ATTR_FORECAST_PRECIPITATION_PROBABILITY: forecast[
                    "precipitation_probability"
                ],
                ATTR_FORECAST_NATIVE_WIND_GUST_SPEED: forecast["wind_gust"],
            }
            fcdata_out.append(data_out)
        _LOGGER.debug("Daily forecast has %d items", len(fcdata_out))
        return fcdata_out

    def _hourly_forecast(self) -> list[Forecast] | None:
        if not self._forecastData:
            return None
        fcdata_out: list[Forecast] = []
        now = datetime.datetime.now(datetime.timezone.utc)
        one_hour = datetime.timedelta(hours=1)
        for forecast in self._forecastData["hourly"]:
            if forecast["time"] + one_hour <= now:
                continue
            symbol = forecast["symbol"]
            data_out: Forecast = {
                ATTR_FORECAST_TIME: forecast["time"].isoformat(),
                ATTR_FORECAST_CONDITION: (
                    str(CODE_TO_CONDITION_MAP.get(symbol, ("", None))[0]) or None
                    if symbol
                    else None
                ),
                ATTR_FORECAST_NATIVE_TEMP: forecast["temperature"],
                ATTR_FORECAST_NATIVE_PRECIPITATION: forecast["precipitation"],
                ATTR_FORECAST_PRECIPITATION_PROBABILITY: forecast[
                    "precipitation_probability"
                ],
                ATTR_FORECAST_NATIVE_WIND_SPEED: forecast["wind_speed"],
                ATTR_FORECAST_WIND_BEARING: forecast["wind_bearing"],
                ATTR_FORECAST_NATIVE_WIND_GUST_SPEED: forecast["wind_gust"],
                ATTR_FORECAST_CLOUD_COVERAGE: forecast["cloud_coverage"],
            }
            fcdata_out.append(data_out)
        _LOGGER.debug("Hourly forecast has %d items", len(fcdata_out))
        return fcdata_out

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return bool(self._daily_forecast())

    async def async_forecast_daily(self) -> list[Forecast]:
        """Return the daily forecast in native units."""
        return self._daily_forecast() or []

    async def async_forecast_hourly(self) -> list[Forecast]:
        """Return the hourly forecast in native units."""
        return self._hourly_forecast() or []
