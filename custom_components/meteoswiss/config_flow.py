"""Config flow to configure the Meteo-Swiss integration."""

import logging
import re
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.issue_registry import IssueSeverity
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from custom_components.meteoswiss.const import (
    CONF_FOEHN,
    CONF_FOEHN_NORTH,
    CONF_FOEHN_SOUTH,
    CONF_FOEHN_STRONG_THRESHOLD,
    CONF_FOEHN_THRESHOLD,
    CONF_FORECAST_NAME,
    CONF_FREEZING_LEVEL,
    CONF_HOURLY_TEMPERATURE,
    CONF_LAT,
    CONF_LON,
    CONF_POINT_ID,
    CONF_POINT_TYPE,
    CONF_POLLEN_STATION,
    CONF_POSTCODE,
    CONF_PRECIPITATION_NAME,
    CONF_PRECIPITATION_STATION,
    CONF_REAL_TIME_NAME,
    CONF_REAL_TIME_PRECIPITATION_NAME,
    CONF_STATION,
    CONF_UPDATE_INTERVAL,
    DEFAULT_HOURLY_TEMPERATURE,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    FOEHN_DEFAULTS,
    STATION_URL,
    USER_AGENT,
)
from custom_components.meteoswiss.issues import (
    async_delete_issues,
    freezing_level_setting,
)
from custom_components.meteoswiss.ogd import (
    POINT_TYPE_POI,
    POINT_TYPE_POSTCODE,
    MeteoSwissOGD,
    OGDError,
    Point,
    StationType,
    distance_km,
    nearest,
)

_LOGGER = logging.getLogger(__name__)


NO_STATION = "No real-time weather station"
NO_PRECIPITATION_STATION = "No real-time precipitation station"
NO_POLLEN_STATION = "No pollen station"
# The dropdown cannot be searched, so only the closest places are offered.
MAX_MOUNTAINS = 100


def station_selection(stations: dict[str, str], abbr: str | None, none: str) -> str:
    """The choice of a station list for an abbreviation, if it is still listed."""
    return next((label for label, a in stations.items() if abbr and a == abbr), none)


def mountain_label(point: Point, lat: float, lon: float) -> str:
    """E.g. Bristen, 3073 m, 14 km: some names are used by several mountains."""
    distance = distance_km(lat, lon, point.lat, point.lon)
    height = f", {point.height:.0f} m" if point.height is not None else ""
    return f"{point.name}{height}, {distance:.0f} km"


class MeteoSwissFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):  # type:ignore
    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    def __init__(self):
        """Init FlowHandler."""
        super().__init__()
        self._lat = None
        self._lon = None
        self._postcode = None
        self._point_id = None
        self._point_type = POINT_TYPE_POSTCODE
        self._forecast_name = None
        self._update_interval = None
        self._hourly_temperature = DEFAULT_HOURLY_TEMPERATURE
        # The settings of the entry being reconfigured, if any.
        self._previous: dict[str, Any] | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Options that can be changed after setup."""
        return MeteoSwissOptionsFlow()

    async def async_step_reconfigure(self, user_input=None):
        """Change the place or stations of an entry, keeping its entities."""
        entry = self._get_reconfigure_entry()
        self._previous = {**entry.data, **entry.options}
        return await self.async_step_user()

    def _previously(self, key: str, default: Any) -> Any:
        """A setting of the entry being reconfigured, as the default."""
        return (self._previous or {}).get(key, default)

    def _previous_place(self, point_type: int) -> bool:
        """Whether the entry being reconfigured is for this kind of place."""
        return (
            self._previous is not None
            and self._previous.get(CONF_POINT_TYPE, POINT_TYPE_POSTCODE) == point_type
        )

    async def async_step_user(self, user_input=None):
        """Handle a flow initiated by the user.

        In this step, we collect the latitude and longitude.
        """

        _LOGGER.debug(
            "step user: starting with lat %s lon %s post %s",
            self._lat,
            self._lon,
            self._postcode,
        )

        def data_schema(lat, lon):
            return vol.Schema(
                {
                    vol.Required(
                        CONF_LAT,
                        default=lat,
                    ): float,
                    vol.Required(
                        CONF_LON,
                        default=lon,
                    ): float,
                }
            )

        errors = {}
        if user_input is not None:
            if user_input[CONF_LAT] > 90 or user_input[CONF_LAT] < -90:
                errors[CONF_LAT] = "latitude_error"
            if user_input[CONF_LON] > 180 or user_input[CONF_LON] < -180:
                errors[CONF_LON] = "longitude_error"
            schema = data_schema(user_input[CONF_LAT], user_input[CONF_LON])
        else:
            schema = data_schema(
                self.hass.config.latitude,
                self.hass.config.longitude,
            )

        if errors or user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=schema, errors=errors
            )

        self._lat = user_input[CONF_LAT]
        self._lon = user_input[CONF_LON]
        _LOGGER.debug(
            "step user: continuing with lat %s lon %s post %s",
            self._lat,
            self._lon,
            self._postcode,
        )
        return await self.async_step_location()

    async def async_step_location(self, user_input=None):
        """Choose between the forecast of a postal code and of a mountain."""
        return self.async_show_menu(
            step_id="location", menu_options=["user_two", "mountain"]
        )

    async def async_step_mountain(self, user_input=None):
        """Choose a mountain, hut or pass, closest first."""
        ogd = MeteoSwissOGD(async_get_clientsession(self.hass), USER_AGENT)
        errors = {}
        try:
            points = (await ogd.get_poi_points(self._lat, self._lon))[:MAX_MOUNTAINS]
        except OGDError:
            points = []
            errors["base"] = "cannot_query_points"

        if user_input is not None and points:
            point = next(
                (p for p in points if str(p.point_id) == user_input[CONF_POINT_ID]),
                None,
            )
            if point is None:
                errors[CONF_POINT_ID] = "invalid_point"
            if user_input[CONF_UPDATE_INTERVAL] < 1:
                errors[CONF_UPDATE_INTERVAL] = "update_interval_too_low"
            if not errors:
                self._point_type = POINT_TYPE_POI
                self._point_id = point.point_id
                self._postcode = None
                self._forecast_name = (
                    user_input.get(CONF_FORECAST_NAME, "").strip() or point.name
                )
                self._update_interval = int(user_input[CONF_UPDATE_INTERVAL])
                self._hourly_temperature = bool(user_input[CONF_HOURLY_TEMPERATURE])
                return await self.async_step_user_three()

        user_input = user_input or {}
        if self._previous_place(POINT_TYPE_POI) and any(
            p.point_id == self._previously(CONF_POINT_ID, None) for p in points
        ):
            closest = str(self._previously(CONF_POINT_ID, None))
            name = self._previously(CONF_FORECAST_NAME, "")
        else:
            closest = str(points[0].point_id) if points else ""
            name = ""
        default_point = user_input.get(CONF_POINT_ID, closest)
        schema = vol.Schema(
            {
                vol.Required(CONF_POINT_ID, default=default_point): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(
                                value=str(p.point_id),
                                label=mountain_label(p, self._lat, self._lon),
                            )
                            for p in points
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(
                    CONF_FORECAST_NAME,
                    default=user_input.get(CONF_FORECAST_NAME, name),
                ): str,
                vol.Required(
                    CONF_UPDATE_INTERVAL,
                    default=user_input.get(
                        CONF_UPDATE_INTERVAL,
                        self._previously(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
                    ),
                ): int,
                vol.Required(
                    CONF_HOURLY_TEMPERATURE,
                    default=user_input.get(
                        CONF_HOURLY_TEMPERATURE,
                        self._previously(
                            CONF_HOURLY_TEMPERATURE, DEFAULT_HOURLY_TEMPERATURE
                        ),
                    ),
                ): bool,
            }
        )
        return self.async_show_form(
            step_id="mountain", data_schema=schema, errors=errors
        )

    async def async_step_user_two(self, user_input=None):
        """Handle the second step of setup.

        In this step we collect the postal code, the name of the forecast,
        the update interval and whether to download hourly temperatures.
        """

        _LOGGER.debug(
            "step user two: starting with lat %s lon %s post %s",
            self._lat,
            self._lon,
            self._postcode,
        )

        def data_schema(postcode, name, interval, hourly_temperature):
            return vol.Schema(
                {
                    vol.Required(
                        CONF_POSTCODE,
                        default=postcode,
                    ): str,
                    vol.Required(
                        CONF_FORECAST_NAME,
                        default=name,
                    ): str,
                    vol.Required(
                        CONF_UPDATE_INTERVAL,
                        default=interval,
                    ): int,
                    vol.Required(
                        CONF_HOURLY_TEMPERATURE,
                        default=hourly_temperature,
                    ): bool,
                }
            )

        ogd = MeteoSwissOGD(async_get_clientsession(self.hass), USER_AGENT)

        errors = {}
        point_id = None
        if user_input is not None:
            if not re.match(r"^\d{4}$", str(user_input[CONF_POSTCODE])):
                errors[CONF_POSTCODE] = "invalid_postcode"
            else:
                try:
                    points = await ogd.get_postcode_points(user_input[CONF_POSTCODE])
                except OGDError:
                    errors[CONF_POSTCODE] = "cannot_query_postcode"
                else:
                    # Some postal codes have several forecast points.
                    point = nearest(points, self._lat, self._lon)
                    if point is None:
                        errors[CONF_POSTCODE] = "invalid_postcode"
                    else:
                        point_id = point.point_id
            if not str(user_input[CONF_FORECAST_NAME]).strip():
                errors[CONF_FORECAST_NAME] = "forecast_name_empty"
            # check if the station name is 3 character
            if user_input[CONF_UPDATE_INTERVAL] < 1:
                errors[CONF_UPDATE_INTERVAL] = "update_interval_too_low"

            schema = data_schema(
                user_input[CONF_POSTCODE],
                user_input[CONF_FORECAST_NAME],
                user_input[CONF_UPDATE_INTERVAL],
                user_input[CONF_HOURLY_TEMPERATURE],
            )
        else:
            try:
                guess = await ogd.get_nearest_postcode_point(self._lat, self._lon)
            except OGDError:
                guess = None
            if guess:
                guessed_postal_code = guess.postcode
                guessed_address = guess.name
            else:
                errors[CONF_POSTCODE] = "cannot_query_postcode"
                errors[CONF_FORECAST_NAME] = "cannot_query_address"
                guessed_postal_code = ""
                guessed_address = ""
            if self._previous_place(POINT_TYPE_POSTCODE):
                errors = {}
                guessed_postal_code = str(self._previously(CONF_POSTCODE, ""))
                guessed_address = self._previously(CONF_FORECAST_NAME, "")

            schema = data_schema(
                guessed_postal_code,
                guessed_address,
                self._previously(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
                self._previously(CONF_HOURLY_TEMPERATURE, DEFAULT_HOURLY_TEMPERATURE),
            )

        if errors or user_input is None:
            return self.async_show_form(
                step_id="user_two", data_schema=schema, errors=errors
            )

        self._postcode = int(user_input[CONF_POSTCODE])
        self._point_id = point_id
        self._forecast_name = user_input[CONF_FORECAST_NAME].strip()
        self._update_interval = int(user_input[CONF_UPDATE_INTERVAL])
        self._hourly_temperature = bool(user_input[CONF_HOURLY_TEMPERATURE])
        _LOGGER.debug(
            "step user two: continuing with lat %s lon %s post %s name %s",
            self._lat,
            self._lon,
            self._postcode,
            self._forecast_name,
        )
        return await self.async_step_user_three()

    async def _get_all_weather_stations_and_closest_one(
        self, ogd, lat, lon
    ) -> tuple[str, str, str]:
        return await self._get_all_stations_and_closest_one(
            ogd, lat, lon, station_type=StationType.WEATHER
        )

    async def _get_all_precipitation_stations_and_closest_one(
        self, ogd, lat, lon
    ) -> tuple[str, str, str]:
        return await self._get_all_stations_and_closest_one(
            ogd, lat, lon, station_type=StationType.PRECIPITATION
        )

    async def _get_all_stations_and_closest_one(
        self, ogd, lat, lon, station_type
    ) -> tuple[str, str, str]:
        try:
            stations = await ogd.get_stations(station_type)
        except OGDError:
            stations = []
        closest = nearest(stations, lat, lon)
        default_station = closest.abbr if closest else ""
        default_station_name = closest.name if closest else ""
        none = {
            StationType.WEATHER: NO_STATION,
            StationType.PRECIPITATION: NO_PRECIPITATION_STATION,
            StationType.POLLEN: NO_POLLEN_STATION,
        }[station_type]
        all_stations = {none: ""}
        all_stations.update({"%s (%s)" % (s.name, s.abbr): s.abbr for s in stations})
        return default_station, default_station_name, all_stations

    async def async_step_user_three(self, user_input=None):
        """Handle the step of setup."""
        _LOGGER.debug(
            "step user three: continuing with lat %s lon %s post %s",
            self._lat,
            self._lon,
            self._postcode,
        )

        def data_schema(
            name,
            weather_station,
            weather_stations,
            precipitation_name,
            precipitation_station,
            precipitation_stations,
            pollen_station,
            pollen_stations,
        ):
            return vol.Schema(
                {
                    vol.Required(
                        CONF_STATION,
                        default=weather_station,
                    ): vol.In(list(weather_stations)),
                    vol.Optional(
                        CONF_REAL_TIME_NAME,
                        default=name,
                    ): str,
                    vol.Required(
                        CONF_PRECIPITATION_STATION,
                        default=precipitation_station,
                    ): vol.In(list(precipitation_stations)),
                    vol.Optional(
                        CONF_REAL_TIME_PRECIPITATION_NAME,
                        default=precipitation_name,
                    ): str,
                    vol.Required(
                        CONF_POLLEN_STATION,
                        default=pollen_station,
                    ): vol.In(list(pollen_stations)),
                }
            )

        ogd = MeteoSwissOGD(async_get_clientsession(self.hass), USER_AGENT)

        (
            default_weather_station,
            default_weather_station_name,
            weather_stations,
        ) = await self._get_all_weather_stations_and_closest_one(
            ogd,
            self._lat,
            self._lon,
        )

        (
            default_precipitation_station,
            default_precipitation_station_name,
            precipitation_stations,
        ) = await self._get_all_precipitation_stations_and_closest_one(
            ogd,
            self._lat,
            self._lon,
        )
        (
            default_pollen_station,
            default_pollen_station_name,
            pollen_stations,
        ) = await self._get_all_stations_and_closest_one(
            ogd, self._lat, self._lon, StationType.POLLEN
        )

        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_STATION] not in weather_stations:
                errors[CONF_STATION] = "invalid_station_name"
            weather_station = weather_stations[user_input[CONF_STATION]]
            if weather_station:
                real_time_weather_name = user_input[CONF_REAL_TIME_NAME].strip()
                if len(real_time_weather_name) < 1:
                    errors[CONF_REAL_TIME_NAME] = "empty_name"
                # check if the station name is 3 character
                if not re.match(r"^\w{3}$", weather_station):
                    errors[CONF_STATION] = "invalid_station_name"
            else:
                weather_station = None
                real_time_weather_name = None

            if user_input[CONF_PRECIPITATION_STATION] not in precipitation_stations:
                errors[CONF_PRECIPITATION_STATION] = "invalid_station_name"
            precipitation_station = precipitation_stations[
                user_input[CONF_PRECIPITATION_STATION]
            ]
            if precipitation_station:
                real_time_precipitation_name = user_input[
                    CONF_REAL_TIME_PRECIPITATION_NAME
                ].strip()
                if len(real_time_precipitation_name) < 1:
                    errors[CONF_REAL_TIME_PRECIPITATION_NAME] = "empty_name"
                # check if the station name is 3 character
                if not re.match(r"^\w{3}$", precipitation_station):
                    errors[CONF_PRECIPITATION_STATION] = "invalid_station_name"
            else:
                precipitation_station = None
                real_time_precipitation_name = None
            # The form fills in the default (the closest station) if left out.
            pollen_selection = user_input.get(CONF_POLLEN_STATION, NO_POLLEN_STATION)
            if pollen_selection not in pollen_stations:
                errors[CONF_POLLEN_STATION] = "invalid_station_name"
            pollen_station = pollen_stations.get(pollen_selection) or None

            schema = data_schema(
                user_input[CONF_REAL_TIME_NAME],
                user_input[CONF_STATION],
                weather_stations,
                user_input[CONF_REAL_TIME_PRECIPITATION_NAME],
                user_input[CONF_PRECIPITATION_STATION],
                precipitation_stations,
                pollen_selection,
                pollen_stations,
            )
        else:
            if default_weather_station:
                default_station_selection = "%s (%s)" % (
                    default_weather_station_name,
                    default_weather_station,
                )
            else:
                default_station_selection = NO_STATION
            if self._point_type == POINT_TYPE_POI:
                # Stations are rarely near a summit.
                default_weather_station = default_precipitation_station = None
                default_weather_station_name = None
                default_precipitation_station_name = None
                default_station_selection = NO_STATION
            if default_precipitation_station:
                default_precipitation_station_selection = "%s (%s)" % (
                    default_precipitation_station_name,
                    default_precipitation_station,
                )
            else:
                default_precipitation_station_selection = NO_PRECIPITATION_STATION
            if default_pollen_station and self._point_type != POINT_TYPE_POI:
                default_pollen_selection = "%s (%s)" % (
                    default_pollen_station_name,
                    default_pollen_station,
                )
            else:
                default_pollen_selection = NO_POLLEN_STATION
            if self._previous is not None:
                # Keep the stations, as long as they are still listed.
                default_station_selection = station_selection(
                    weather_stations, self._previously(CONF_STATION, None), NO_STATION
                )
                default_weather_station_name = self._previously(CONF_REAL_TIME_NAME, "")
                default_precipitation_station_selection = station_selection(
                    precipitation_stations,
                    self._previously(CONF_PRECIPITATION_STATION, None),
                    NO_PRECIPITATION_STATION,
                )
                default_precipitation_station_name = self._previously(
                    CONF_REAL_TIME_PRECIPITATION_NAME, ""
                )
                default_pollen_selection = station_selection(
                    pollen_stations,
                    self._previously(CONF_POLLEN_STATION, None),
                    NO_POLLEN_STATION,
                )
            default_weather_station_name = default_weather_station_name or ""
            default_precipitation_station_name = (
                default_precipitation_station_name or ""
            )

            schema = data_schema(
                default_weather_station_name,
                default_station_selection,
                weather_stations,
                default_precipitation_station_name,
                default_precipitation_station_selection,
                precipitation_stations,
                default_pollen_selection,
                pollen_stations,
            )

        if errors or user_input is None:
            return self.async_show_form(
                step_id="user_three",
                data_schema=schema,
                errors=errors,
                description_placeholders={
                    "station_url": STATION_URL,
                },
            )

        data = {
            CONF_FORECAST_NAME: self._forecast_name,
            CONF_UPDATE_INTERVAL: self._update_interval,
            CONF_HOURLY_TEMPERATURE: self._hourly_temperature,
        }
        if self._point_type == POINT_TYPE_POI:
            data[CONF_POINT_TYPE] = POINT_TYPE_POI
        else:
            data[CONF_POSTCODE] = self._postcode
        if self._point_id is not None:
            data[CONF_POINT_ID] = self._point_id
        if weather_station and real_time_weather_name:
            data.update(
                {
                    CONF_STATION: weather_station,
                    CONF_REAL_TIME_NAME: real_time_weather_name,
                }
            )
        if precipitation_station and real_time_precipitation_name:
            data.update(
                {
                    CONF_PRECIPITATION_STATION: precipitation_station,
                    CONF_REAL_TIME_PRECIPITATION_NAME: real_time_precipitation_name,
                }
            )
        if pollen_station:
            data[CONF_POLLEN_STATION] = pollen_station

        title = self._forecast_name
        if real_time_weather_name:
            title += f" / {real_time_weather_name}"
        if real_time_precipitation_name:
            title += f" / {real_time_precipitation_name}"

        _LOGGER.debug(
            "step user three: finishing with %s",
            data,
        )
        if self._previous is not None:
            entry = self._get_reconfigure_entry()
            # The notices about the previous place or stations no longer apply.
            async_delete_issues(self.hass, entry.data)
            return self.async_update_reload_and_abort(
                entry,
                title=title,
                data=data,
                # The setting is now in the data.
                options={
                    k: v
                    for k, v in entry.options.items()
                    if k != CONF_HOURLY_TEMPERATURE
                },
            )
        return self.async_create_entry(
            title=title,
            data=data,
        )

    async def async_step_import(self, import_config: dict[str, Any]):
        """Import a config entry."""
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            "deprecated_yaml",
            is_fixable=False,
            severity=IssueSeverity.WARNING,
            translation_key="deprecated_yaml",
        )

        data = {
            CONF_POSTCODE: import_config[CONF_POSTCODE],
            CONF_UPDATE_INTERVAL: import_config.get(
                CONF_UPDATE_INTERVAL,
                DEFAULT_UPDATE_INTERVAL,
            ),
            CONF_FORECAST_NAME: import_config[CONF_NAME],
        }
        if import_config.get(CONF_STATION):
            data[CONF_STATION] = import_config[CONF_STATION]
            data[CONF_REAL_TIME_NAME] = import_config[CONF_NAME]
        if import_config.get(CONF_PRECIPITATION_STATION):
            data[CONF_PRECIPITATION_STATION] = import_config[CONF_PRECIPITATION_STATION]
            data[CONF_REAL_TIME_PRECIPITATION_NAME] = import_config.get(
                CONF_PRECIPITATION_NAME, import_config[CONF_NAME]
            )

        # The YAML stays until removed, so it is imported on every start.
        self._async_abort_entries_match(
            {
                CONF_POSTCODE: data[CONF_POSTCODE],
                CONF_FORECAST_NAME: data[CONF_FORECAST_NAME],
            }
        )
        return self.async_create_entry(
            title=data[CONF_FORECAST_NAME],
            data=data,
        )


class MeteoSwissOptionsFlow(config_entries.OptionsFlow):
    """Change the options of a MeteoSwiss entry."""

    async def async_step_init(self, user_input=None):
        entry = self.config_entry
        current = {
            CONF_HOURLY_TEMPERATURE: entry.data.get(
                CONF_HOURLY_TEMPERATURE, DEFAULT_HOURLY_TEMPERATURE
            ),
            CONF_FREEZING_LEVEL: freezing_level_setting(entry.data, entry.options),
            **FOEHN_DEFAULTS,
            **entry.options,
        }
        ogd = MeteoSwissOGD(async_get_clientsession(self.hass), USER_AGENT)
        try:
            stations = await ogd.get_stations(StationType.WEATHER)
        except OGDError:
            stations = []
        # The configured stations stay choosable, even if not listed now.
        choices = {s.abbr: f"{s.name} ({s.abbr})" for s in stations}
        for key in (CONF_FOEHN_SOUTH, CONF_FOEHN_NORTH):
            choices.setdefault(current[key], current[key])

        errors = {}
        if user_input is not None:
            if user_input[CONF_FOEHN_SOUTH] == user_input[CONF_FOEHN_NORTH]:
                errors[CONF_FOEHN_NORTH] = "same_foehn_station"
            if not (
                0
                < user_input[CONF_FOEHN_THRESHOLD]
                < user_input[CONF_FOEHN_STRONG_THRESHOLD]
            ):
                errors[CONF_FOEHN_STRONG_THRESHOLD] = "foehn_thresholds"
            if not errors:
                return self.async_create_entry(data=user_input)
            current.update(user_input)

        def required(key):
            return vol.Required(key, default=current[key])

        options = [
            SelectOptionDict(value=abbr, label=label)
            for abbr, label in sorted(choices.items(), key=lambda kv: kv[1])
        ]
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    required(CONF_HOURLY_TEMPERATURE): bool,
                    required(CONF_FREEZING_LEVEL): bool,
                    required(CONF_FOEHN): bool,
                    required(CONF_FOEHN_SOUTH): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.DROPDOWN
                        )
                    ),
                    required(CONF_FOEHN_NORTH): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.DROPDOWN
                        )
                    ),
                    required(CONF_FOEHN_THRESHOLD): NumberSelector(
                        NumberSelectorConfig(
                            min=0.5,
                            max=20,
                            step=0.5,
                            unit_of_measurement="hPa",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    required(CONF_FOEHN_STRONG_THRESHOLD): NumberSelector(
                        NumberSelectorConfig(
                            min=0.5,
                            max=30,
                            step=0.5,
                            unit_of_measurement="hPa",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
            errors=errors,
        )
