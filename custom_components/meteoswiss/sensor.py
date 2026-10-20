import logging
import pprint
import typing
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfLength,
    UnitOfPrecipitationDepth,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from custom_components.meteoswiss import MeteoSwissDataUpdateCoordinator, summaries
from custom_components.meteoswiss.alerts import (
    LEVELS,
    NO_WARNING,
    in_force,
    level_between,
)
from custom_components.meteoswiss.const import (
    CONF_POSTCODE,
    CONF_PRECIPITATION_STATION,
    CONF_REAL_TIME_NAME,
    CONF_REAL_TIME_PRECIPITATION_NAME,
    CONF_STATION,
    DOMAIN,
    SENSOR_DATA_ID,
    SENSOR_TYPE_CLASS,
    SENSOR_TYPE_ICON,
    SENSOR_TYPE_NAME,
    SENSOR_TYPE_UNIT,
    SENSOR_TYPES,
)
from custom_components.meteoswiss.ogd import (
    POLLEN_PARAMETERS,
    HourlyForecast,
    StationType,
    current_hour,
)

_LOGGER = logging.getLogger(__name__)

# Precipitation stations only measure rain.
PRECIPITATION_SENSOR_TYPES = ("10minrain",)


@dataclass(frozen=True, kw_only=True)
class SummaryDescription(SensorEntityDescription):
    """A summary of the hourly forecast over a local time window."""

    # None when its data is missing, e.g. temperatures with hourly
    # temperatures turned off.
    value: Callable[[list[HourlyForecast], datetime], float | None]


def _snow(description: str, window) -> SummaryDescription:
    return SummaryDescription(
        key=description,
        translation_key=description,
        icon="mdi:snowflake",
        device_class=SensorDeviceClass.PRECIPITATION,
        native_unit_of_measurement=UnitOfPrecipitationDepth.MILLIMETERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value=lambda hourly, now: summaries.snow(hourly, window(now)),
    )


def _sunshine(description: str, window) -> SummaryDescription:
    return SummaryDescription(
        key=description,
        translation_key=description,
        icon="mdi:weather-sunny",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value=lambda hourly, now: summaries.sunshine_hours(hourly, window(now)),
    )


SUMMARIES: tuple[SummaryDescription, ...] = (
    # In mm of water: the precipitation of the hours at or below 0.5 °C.
    _snow("snow_tonight", summaries.tonight),
    _snow("snow_next_24h", lambda now: summaries.coming_hours(now, 24)),
    _snow("snow_next_48h", lambda now: summaries.coming_hours(now, 48)),
    _sunshine(
        "sunshine_tomorrow_midday",
        lambda now: summaries.tomorrow(now, *summaries.MIDDAY),
    ),
    _sunshine("sunshine_tomorrow", summaries.tomorrow),
    SummaryDescription(
        key="max_gust_tomorrow_midday",
        translation_key="max_gust_tomorrow_midday",
        icon="mdi:weather-windy",
        device_class=SensorDeviceClass.WIND_SPEED,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value=lambda hourly, now: summaries.max_gust(
            hourly, summaries.tomorrow(now, *summaries.MIDDAY)
        ),
    ),
    SummaryDescription(
        key="precipitation_next_hour",
        translation_key="precipitation_next_hour",
        icon="mdi:weather-rainy",
        device_class=SensorDeviceClass.PRECIPITATION,
        native_unit_of_measurement=UnitOfPrecipitationDepth.MILLIMETERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value=lambda hourly, now: summaries.precipitation(
            hourly, summaries.next_hour(now)
        ),
    ),
    SummaryDescription(
        # MeteoSwiss's chance of rain in the three hours up to its end.
        key="precipitation_probability_next_hour",
        translation_key="precipitation_probability_next_hour",
        icon="mdi:umbrella-outline",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value=lambda hourly, now: summaries.precipitation_probability(
            hourly, summaries.next_hour(now)
        ),
    ),
    SummaryDescription(
        key="min_temperature_tonight",
        translation_key="min_temperature_tonight",
        icon="mdi:thermometer-low",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value=lambda hourly, now: summaries.min_temperature(
            hourly, summaries.tonight(now)
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up all sensors."""
    _LOGGER.debug("Starting async setup platform for sensor")
    c: MeteoSwissDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    if c.weather_station:
        async_add_entities(
            [
                MeteoSwissSensor(entry.entry_id, typ, c, StationType.WEATHER)
                for typ in SENSOR_TYPES
            ],
            True,
        )
    else:
        _LOGGER.debug(
            "The data update coordinator has no real-time weather station configured"
            + " — not providing weather sensor data."
        )
    if c.precipitation_station:
        async_add_entities(
            [
                MeteoSwissSensor(entry.entry_id, typ, c, StationType.PRECIPITATION)
                for typ in PRECIPITATION_SENSOR_TYPES
            ],
            True,
        )
    else:
        _LOGGER.debug(
            "The data update coordinator has no real-time precipitation station"
            + " configured — not providing precipitation sensor data."
        )
    # The freezing level comes with the hourly temperatures.
    if c.is_mountain and c.hourly_temperature:
        async_add_entities([MeteoSwissFreezingLevelSensor(entry.entry_id, c)], True)
    async_add_entities(
        [MeteoSwissSummarySensor(entry.entry_id, c, d) for d in SUMMARIES], True
    )
    async_add_entities(
        [
            MeteoSwissWarningSensor(entry.entry_id, c, key, hours)
            for key, hours in (("weather_warning", 0), ("weather_warning_next_24h", 24))
        ],
        True,
    )
    if c.pollen_station:
        async_add_entities(
            [
                MeteoSwissPollenSensor(entry.entry_id, c, kind)
                for kind in POLLEN_PARAMETERS
            ],
            True,
        )


class MeteoSwissWarningSensor(
    CoordinatorEntity[MeteoSwissDataUpdateCoordinator],
    SensorEntity,
):
    """The most severe weather warning for the place, now or in coming hours.

    The warnings themselves are attributes, with a type (e.g. "wind") to tell
    them apart in templates.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [NO_WARNING, *LEVELS]
    _attr_icon = "mdi:alert-outline"
    _attr_attribution = "MeteoSwiss, via MeteoAlarm"

    def __init__(
        self,
        integration_id: str,
        coordinator: MeteoSwissDataUpdateCoordinator,
        key: str,
        hours: int,
    ) -> None:
        super().__init__(coordinator)
        self._hours = hours
        self._attr_translation_key = key
        self._attr_unique_id = f"sensor.{integration_id}-{key}"
        self._attr_device_info = coordinator.device_info

    def _window(self) -> tuple[datetime, datetime]:
        now = datetime.now(timezone.utc)
        # "Now" is the coming second, so that a warning starting now counts.
        return now, now + (timedelta(hours=self._hours) or timedelta(seconds=1))

    @property
    def native_value(self) -> str | None:
        warnings = self.coordinator.data["warnings"]
        if warnings is None:
            return None
        return level_between(warnings, *self._window())

    @property
    def extra_state_attributes(self) -> dict[str, typing.Any] | None:
        warnings = self.coordinator.data["warnings"]
        if warnings is None:
            return None
        current = in_force(warnings, *self._window())
        return {
            "types": sorted({w["type"] for w in current if w["type"]}),
            "warnings": [
                {
                    "type": w["type"],
                    "level": w["level"],
                    "event": w["event"],
                    "onset": w["onset"].isoformat(),
                    "expires": w["expires"].isoformat(),
                    "region": w["region"],
                    "headline": w["headline"],
                    "description": w["description"],
                    "url": w["url"],
                }
                for w in current
            ],
        }

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class MeteoSwissPollenSensor(
    CoordinatorEntity[MeteoSwissDataUpdateCoordinator],
    SensorEntity,
):
    """The latest hourly concentration of a type of pollen at a pollen station."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:flower-pollen"
    _attr_native_unit_of_measurement = "pollen/m³"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(
        self,
        integration_id: str,
        coordinator: MeteoSwissDataUpdateCoordinator,
        kind: str,
    ) -> None:
        super().__init__(coordinator)
        self._kind = kind
        self._attr_translation_key = f"pollen_{kind}"
        self._attr_unique_id = f"sensor.{integration_id}-pollen_{kind}"
        self._attr_device_info = coordinator.station_device_info(StationType.POLLEN)

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data["pollen"].get(self._kind)

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        measured = self.coordinator.data["pollen"].get("measured")
        return {"measured": measured.isoformat()} if measured else None

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class MeteoSwissSummarySensor(
    CoordinatorEntity[MeteoSwissDataUpdateCoordinator],
    SensorEntity,
):
    """A summary of the forecast, such as the snow expected tonight.

    Without the data it needs, it is unavailable rather than a guess.
    """

    entity_description: SummaryDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        integration_id: str,
        coordinator: MeteoSwissDataUpdateCoordinator,
        description: SummaryDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"sensor.{integration_id}-{description.key}"
        self._attr_device_info = coordinator.device_info

    @property
    def native_value(self) -> float | None:
        forecast = self.coordinator.data["forecast"]
        if not forecast:
            return None
        return self.entity_description.value(
            forecast["hourly"], datetime.now(timezone.utc)
        )

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class MeteoSwissFreezingLevelSensor(
    CoordinatorEntity[MeteoSwissDataUpdateCoordinator],
    SensorEntity,
):
    """The forecast height of the 0 °C line for the current hour."""

    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_native_unit_of_measurement = UnitOfLength.METERS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:snowflake-thermometer"
    # Shown after the name of the device, e.g. "Bristen Freezing level".
    _attr_has_entity_name = True
    _attr_name = "Freezing level"
    _attr_suggested_display_precision = 0

    def __init__(
        self, integration_id: str, coordinator: MeteoSwissDataUpdateCoordinator
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"sensor.{integration_id}-freezing_level"
        self._attr_device_info = coordinator.device_info

    @property
    def native_value(self) -> float | None:
        forecast = self.coordinator.data["forecast"]
        if not forecast:
            return None
        current = current_hour(forecast["hourly"], datetime.now(timezone.utc))
        return current["freezing_level"] if current else None

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class MeteoSwissSensor(
    CoordinatorEntity[MeteoSwissDataUpdateCoordinator],
    SensorEntity,
):
    """Represents a sensor from MeteoSwiss."""

    def __init__(
        self,
        integration_id: str,
        sensor_type: typing.Any,
        coordinator: MeteoSwissDataUpdateCoordinator,
        station_type: StationType,
    ):
        super().__init__(coordinator)
        self._station_type = station_type
        self._attr_unique_id = "sensor.%s-%s%s" % (
            integration_id,
            sensor_type,
            "-precipitation" if station_type == StationType.PRECIPITATION else "",
        )
        self._attr_device_info = coordinator.station_device_info(station_type)
        self._state = None
        self._type = sensor_type
        self._attr_native_unit_of_measurement = SENSOR_TYPES[self._type][
            SENSOR_TYPE_UNIT
        ]
        self._attr_icon = SENSOR_TYPES[self._type][SENSOR_TYPE_ICON]
        self._attr_device_class = SENSOR_TYPES[self._type][SENSOR_TYPE_CLASS]
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._data = coordinator.data
        self._attr_station = coordinator.data[
            CONF_STATION
            if station_type == StationType.WEATHER
            else CONF_PRECIPITATION_STATION
        ]
        self._attr_postcode = coordinator.data[CONF_POSTCODE]

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        x = SENSOR_TYPES[self._type][SENSOR_TYPE_NAME]
        name_key = (
            CONF_REAL_TIME_NAME
            if self._station_type == StationType.WEATHER
            else CONF_REAL_TIME_PRECIPITATION_NAME
        )
        return f"{self._data[name_key]} {x}"

    @property
    def native_value(self) -> StateType | date | datetime | Decimal:
        dataId = SENSOR_TYPES[self._type][SENSOR_DATA_ID]
        data: StateType | date | datetime | Decimal = None
        if (
            self._attr_station not in self._data["condition_by_station"]
            or not self._data["condition_by_station"][self._attr_station]
        ):
            pass
        else:
            try:
                data = self._data["condition_by_station"][self._attr_station][dataId]
            except Exception:
                _LOGGER.warning(
                    "Real-time weather station returned bad data:\n%s",
                    pprint.pformat(self._data),
                )
                data = None
        return data

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        available = False
        dataId = SENSOR_TYPES[self._type][SENSOR_DATA_ID]
        if (
            self._attr_station not in self._data["condition_by_station"]
            or not self._data["condition_by_station"][self._attr_station]
        ):
            pass
        else:
            try:
                available = (
                    self._data["condition_by_station"][self._attr_station][dataId]
                    is not None
                )
            except Exception:
                available = False
        return available

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle data update."""
        self._data = self.coordinator.data
        self.async_write_ha_state()
