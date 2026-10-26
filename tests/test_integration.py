"""Setup, entities and forecasts, with the OGD client faked."""

import asyncio
import datetime

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.meteoswiss import DATA_ALERTS, DATA_CLIENT, DATA_DOWNLOADS
from custom_components.meteoswiss.alerts import AlertError
from custom_components.meteoswiss.const import DOMAIN
from custom_components.meteoswiss.ogd import OGDError

from .fakes import (
    FOEHN_PRESSURES,
    NOW,
    STATION_MEASUREMENTS,
    FakeRadar,
    make_warning,
)

pytestmark = pytest.mark.usefixtures("fake_ogd", "frozen_now")

# The entity IDs of the summaries and warnings, after the device's name.
SUMMARY_NAMES = (
    "snow_tonight",
    "snow_next_24_hours",
    "snow_next_48_hours",
    "sunshine_tomorrow_9_15",
    "sunshine_tomorrow",
    "strongest_gust_tomorrow_9_15",
    "precipitation_next_hour",
    "chance_of_rain_next_hour",
    "lowest_temperature_tonight",
    "weather_warning",
    "weather_warning_next_24_hours",
    "fohn_pressure_difference",
    "fohn_outlook",
)


async def setup(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    # Including what is fetched in the background, like the climate normals.
    await hass.async_block_till_done(wait_background_tasks=True)


def entity_id(hass, platform, unique_id):
    found = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert found, f"no {platform} entity with unique id {unique_id}"
    return found


def weather_id(hass, entry):
    return entity_id(hass, "weather", f"weather.{entry.entry_id}")


async def refresh_after(hass, freezer, minutes):
    freezer.tick(datetime.timedelta(minutes=minutes))
    async_fire_time_changed(hass)
    # Including the forecast, which is refreshed in the background.
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_weather_entity(hass, entry):
    await setup(hass, entry)

    state = hass.states.get(weather_id(hass, entry))

    # 12:30 falls in the hour starting 12:00, which has symbol 1.
    assert state.state == "sunny"
    assert state.attributes["temperature"] == 13.4
    assert state.attributes["humidity"] == 80  # rounded by Home Assistant
    # At sea level, not the station's 940.5 hPa.
    assert state.attributes["pressure"] == 1012.3
    assert state.attributes["wind_speed"] == 8.6
    assert state.attributes["wind_bearing"] == 31.0
    assert state.attributes["wind_gust_speed"] == 25.2
    assert state.attributes["dew_point"] == 9.9
    assert "Forecasts from postal code 8005" in state.attributes["attribution"]


async def test_sensors(hass, entry):
    await setup(hass, entry)

    def value(suffix):
        return hass.states.get(
            entity_id(hass, "sensor", f"sensor.{entry.entry_id}-{suffix}")
        ).state

    assert value("temperature") == "13.4"
    assert value("humidity") == "79.9"
    assert value("pressure") == "940.5"
    assert value("wind_speed") == "8.6"
    assert value("10minrain") == "0.0"
    # The precipitation station only measures rain.
    assert value("10minrain-precipitation") == "0.2"
    precipitation_sensors = [
        e
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.unique_id.endswith("-precipitation")
    ]
    assert len(precipitation_sensors) == 1
    assert value("dew_point") == "9.9"
    # Missing measurements are unavailable rather than wrong.
    assert value("pressure_qnh") == "unavailable"


async def test_daily_forecast(hass, entry):
    await setup(hass, entry)

    response = await hass.services.async_call(
        "weather",
        "get_forecasts",
        {"entity_id": weather_id(hass, entry), "type": "daily"},
        blocking=True,
        return_response=True,
    )

    days = response[weather_id(hass, entry)]["forecast"]
    assert len(days) == 3
    assert days[0] == {
        "datetime": "2026-10-20",
        "temperature": 15.0,
        "templow": 8.0,
        "condition": "partlycloudy",  # symbol 3
        "precipitation": 0.0,
        "precipitation_probability": 0.0,
        "wind_gust_speed": 40.0,
    }
    assert days[1]["condition"] == "pouring"  # symbol 20
    assert days[1]["precipitation_probability"] == 30.0


async def test_hourly_forecast_starts_with_the_current_hour(hass, entry):
    await setup(hass, entry)

    response = await hass.services.async_call(
        "weather",
        "get_forecasts",
        {"entity_id": weather_id(hass, entry), "type": "hourly"},
        blocking=True,
        return_response=True,
    )

    hours = response[weather_id(hass, entry)]["forecast"]
    # The 09:00-11:00 hours are over; 12:00 is first and 20:00 last.
    assert hours[0]["datetime"].startswith("2026-10-20T12:00")
    assert hours[-1]["datetime"].startswith("2026-10-20T20:00")
    assert len(hours) == 9
    assert hours[0]["condition"] == "sunny"
    assert hours[0]["temperature"] == 13.0
    assert hours[0]["precipitation_probability"] == 15.0
    assert hours[0]["wind_speed"] == 8.0
    assert hours[0]["wind_bearing"] == 180.0
    assert hours[0]["wind_gust_speed"] == 23.0
    assert hours[0]["cloud_coverage"] == 30.0
    assert hours[5]["datetime"].startswith("2026-10-20T17:00")
    assert hours[5]["condition"] == "clear-night"  # symbol 101
    assert hours[7]["condition"] is None  # symbol 999 is unknown


async def test_unrecognised_symbol_is_unavailable(hass, entry, frozen_now):
    await setup(hass, entry)
    frozen_now.move_to("2026-10-20T19:30:00+00:00")

    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(weather_id(hass, entry)).state == "unavailable"


async def test_forecast_is_fetched_once_per_run(hass, entry, fake_ogd, frozen_now):
    await setup(hass, entry)
    assert (fake_ogd.run_checks, fake_ogd.forecast_calls) == (1, 1)

    # Station updates every five minutes do not even look for a new run...
    await refresh_after(hass, frozen_now, 5)
    await refresh_after(hass, frozen_now, 5)
    assert (fake_ogd.run_checks, fake_ogd.forecast_calls) == (1, 1)

    # ...later it looks, but finding the same run does not refetch the forecast...
    await refresh_after(hass, frozen_now, 15)
    assert (fake_ogd.run_checks, fake_ogd.forecast_calls) == (2, 1)

    # ...but a new run is picked up.
    fake_ogd.run = "202610201300"
    await refresh_after(hass, frozen_now, 15)
    assert fake_ogd.forecast_calls == 2
    assert entry.runtime_data.data["forecast"]["run"] == "202610201300"


async def test_previous_forecast_is_kept_when_a_check_fails(
    hass, entry, fake_ogd, frozen_now
):
    await setup(hass, entry)
    fake_ogd.forecast_error = OGDError("down")

    await refresh_after(hass, frozen_now, 16)

    coordinator = entry.runtime_data
    assert coordinator.last_update_success
    assert coordinator.data["forecast"]["run"] == "202610201200"
    assert hass.states.get(weather_id(hass, entry)).state == "sunny"


async def test_setup_is_retried_without_any_forecast(hass, entry, fake_ogd):
    fake_ogd.forecast_error = OGDError("down")

    assert not await hass.config_entries.async_setup(entry.entry_id)

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_station_failure_only_affects_the_sensors(hass, entry, fake_ogd):
    fake_ogd.station_error = OGDError("station down")

    await setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    temperature = entity_id(hass, "sensor", f"sensor.{entry.entry_id}-temperature")
    assert hass.states.get(temperature).state == "unavailable"
    assert hass.states.get(weather_id(hass, entry)).state == "sunny"


async def test_entry_without_stations_only_has_the_forecast(hass, entry_data):
    for key in (
        "station",
        "real_time_name",
        "precipitation_station",
        "real_time_precipitation_name",
    ):
        del entry_data[key]
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data, title="Zurich")
    entry.add_to_hass(hass)

    await setup(hass, entry)

    # Only the forecast summaries, no station sensors.
    assert {s.entity_id for s in hass.states.async_all("sensor")} == {
        f"sensor.zurich_{name}" for name in SUMMARY_NAMES
    }
    state = hass.states.get(weather_id(hass, entry))
    assert state.state == "sunny"
    assert "temperature" not in state.attributes
    assert "No real-time stations" in state.attributes["attribution"]


async def test_the_coordinator_belongs_to_the_entry(hass, entry):
    await setup(hass, entry)
    assert entry.runtime_data.config_entry is entry


async def test_unload(hass, entry):
    await setup(hass, entry)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get(weather_id(hass, entry)).state == "unavailable"


async def test_hourly_temperatures_are_refreshed_every_three_hours(
    hass, entry, fake_ogd, frozen_now
):
    await setup(hass, entry)
    coordinator = entry.runtime_data
    assert fake_ogd.temperature_calls == 1

    # Every new run refreshes the rest of the forecast but reuses temperatures...
    for hour in (13, 14):
        fake_ogd.run = f"20261020{hour}00"
        await refresh_after(hass, frozen_now, 60)
        assert coordinator.data["forecast"]["run"] == fake_ogd.run
        assert coordinator.data["forecast"]["temperature_run"] == "202610201200"
    assert fake_ogd.temperature_calls == 1

    # ...until they are three hours old.
    fake_ogd.run = "202610201500"
    await refresh_after(hass, frozen_now, 60)
    assert fake_ogd.temperature_calls == 2
    assert coordinator.data["forecast"]["temperature_run"] == "202610201500"


async def test_forecast_works_without_hourly_temperatures(hass, entry, fake_ogd):
    fake_ogd.temperature_error = OGDError("too slow")

    await setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    response = await hass.services.async_call(
        "weather",
        "get_forecasts",
        {"entity_id": weather_id(hass, entry), "type": "hourly"},
        blocking=True,
        return_response=True,
    )
    hours = response[weather_id(hass, entry)]["forecast"]
    assert hours[0]["temperature"] is None
    assert hours[0]["condition"] == "sunny"


async def test_failed_temperature_refresh_keeps_the_previous_ones(
    hass, entry, fake_ogd, frozen_now
):
    await setup(hass, entry)
    fake_ogd.temperature_error = OGDError("too slow")

    fake_ogd.run = "202610201600"
    await refresh_after(hass, frozen_now, 4 * 60)

    forecast = entry.runtime_data.data["forecast"]
    assert forecast["run"] == "202610201600"
    assert forecast["temperature_run"] == "202610201200"


async def test_hourly_temperatures_can_be_turned_off(
    hass, entry_data, fake_ogd, frozen_now
):
    entry = MockConfigEntry(
        domain=DOMAIN, data={**entry_data, "hourly_temperature": False}
    )
    entry.add_to_hass(hass)

    await setup(hass, entry)
    fake_ogd.run = "202610201600"
    await refresh_after(hass, frozen_now, 4 * 60)

    assert fake_ogd.temperature_calls == 0
    response = await hass.services.async_call(
        "weather",
        "get_forecasts",
        {"entity_id": weather_id(hass, entry), "type": "hourly"},
        blocking=True,
        return_response=True,
    )
    hours = response[weather_id(hass, entry)]["forecast"]
    assert hours[0]["temperature"] is None
    assert hours[0]["precipitation_probability"] is not None


def issues(hass):
    return {
        issue_id for (domain, issue_id) in ir.async_get(hass).issues if domain == DOMAIN
    }


async def test_a_station_without_data_is_reported_after_an_hour(
    hass, entry, fake_ogd, frozen_now
):
    await setup(hass, entry)
    fake_ogd.station_error = OGDError("gone")

    await refresh_after(hass, frozen_now, 5)
    await refresh_after(hass, frozen_now, 50)
    assert not issues(hass)

    await refresh_after(hass, frozen_now, 15)
    assert issues(hass) == {
        "SMA_real_time_name_provides_no_data_meteoswiss",
        "ABE_real_time_precipitation_name_provides_no_data_meteoswiss",
    }

    # The issues go away when the stations are back.
    fake_ogd.station_error = None
    await refresh_after(hass, frozen_now, 5)
    assert not issues(hass)


async def test_an_unknown_postcode_is_reported_straight_away(hass, entry_data):
    entry = MockConfigEntry(domain=DOMAIN, data={**entry_data, "postcode": 9999})
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert issues(hass) == {"9999_provides_no_forecast_meteoswiss"}


async def test_attribution_links_to_meteoswiss(hass, entry):
    await setup(hass, entry)
    attribution = hass.states.get(weather_id(hass, entry)).attributes["attribution"]
    assert "https://www.meteoswiss.admin.ch/" in attribution
    assert "rudd-o.com" not in attribution


MOUNTAIN = {
    "point_type": 3,
    "point_id": 864,
    "forecast_name": "Bristen",
    "update_interval": 5,
    "hourly_temperature": True,
}


async def add_mountain(hass, **changes):
    entry = MockConfigEntry(domain=DOMAIN, data={**MOUNTAIN, **changes})
    entry.add_to_hass(hass)
    await setup(hass, entry)
    return entry


async def test_mountain_shows_the_forecast_for_the_current_hour(hass):
    entry = await add_mountain(hass)

    state = hass.states.get(weather_id(hass, entry))

    # The 12:00 hour of the fake forecast.
    assert state.state == "sunny"
    assert state.attributes["temperature"] == 13.0
    assert state.attributes["wind_speed"] == 8.0
    assert state.attributes["wind_gust_speed"] == 23.0
    assert state.attributes["wind_bearing"] == 180.0
    assert state.attributes["cloud_coverage"] == 30.0
    assert state.attributes["elevation"] == 3073.0
    assert "humidity" not in state.attributes
    assert "Forecasts for Bristen (3073 m)" in state.attributes["attribution"]
    assert "forecast, not measured" in state.attributes["attribution"]


async def test_mountain_with_a_station_shows_measurements(hass):
    entry = await add_mountain(hass, station="SMA", real_time_name="Fluntern")

    state = hass.states.get(weather_id(hass, entry))

    assert state.attributes["temperature"] == 13.4
    assert "cloud_coverage" not in state.attributes
    assert "forecast, not measured" not in state.attributes["attribution"]


async def test_freezing_level_sensor(hass, fake_ogd):
    entry = await add_mountain(hass)

    sensor = entity_id(hass, "sensor", f"sensor.{entry.entry_id}-freezing_level")
    state = hass.states.get(sensor)
    assert state.state == "2900.0"
    assert state.attributes["unit_of_measurement"] == "m"
    assert state.attributes["friendly_name"] == "Bristen Freezing level"
    assert [len(d) for d in fake_ogd.large_downloads["zprfr0hs"]] == [1]


async def test_mountain_without_large_downloads(hass, fake_ogd):
    entry = await add_mountain(hass, hourly_temperature=False)

    assert fake_ogd.large_downloads == {}
    # No freezing level, only the forecast summaries.
    assert {s.entity_id for s in hass.states.async_all("sensor")} == {
        f"sensor.bristen_{name}" for name in SUMMARY_NAMES
    }
    state = hass.states.get(weather_id(hass, entry))
    assert "temperature" not in state.attributes
    assert state.attributes["wind_speed"] == 8.0


async def test_freezing_level_is_only_for_mountains(hass, entry, fake_ogd):
    await setup(hass, entry)

    assert set(fake_ogd.large_downloads) == {"tre200h0"}


async def test_an_unknown_mountain_is_reported(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={**MOUNTAIN, "point_id": 999})
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert issues(hass) == {"point_999_provides_no_forecast_meteoswiss"}


async def test_entries_share_the_large_downloads(hass, entry, fake_ogd, frozen_now):
    await setup(hass, entry)
    await add_mountain(hass)
    zurich, bristen = (800500, 2), (864, 3)
    # Each entry fetched its point when it was set up.
    assert fake_ogd.large_downloads["tre200h0"] == [{zurich}, {bristen}]

    # After that, one download serves both.
    fake_ogd.run = "202610201600"
    await refresh_after(hass, frozen_now, 4 * 60)
    assert fake_ogd.large_downloads["tre200h0"][2:] == [{zurich, bristen}]
    assert fake_ogd.large_downloads["zprfr0hs"][-1] == {bristen}

    # An entry that is removed is left out.
    assert await hass.config_entries.async_unload(entry.entry_id)
    fake_ogd.run = "202610202000"
    await refresh_after(hass, frozen_now, 4 * 60)
    assert fake_ogd.large_downloads["tre200h0"][-1] == {bristen}


async def test_entities_are_grouped_by_forecast_and_station(hass, entry):
    await setup(hass, entry)

    devices = {
        d.model: d
        for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    }
    assert {model: d.name for model, d in devices.items()} == {
        "Postal code forecast": "Zurich",
        "Weather station": "Fluntern",
        "Precipitation station": "Aarberg",
    }
    assert {d.manufacturer for d in devices.values()} == {"MeteoSwiss"}
    assert {d.entry_type for d in devices.values()} == {dr.DeviceEntryType.SERVICE}

    def device_of(entity):
        return er.async_get(hass).async_get(entity).device_id

    temperature = entity_id(hass, "sensor", f"sensor.{entry.entry_id}-temperature")
    rain = entity_id(hass, "sensor", f"sensor.{entry.entry_id}-10minrain-precipitation")
    assert device_of(weather_id(hass, entry)) == devices["Postal code forecast"].id
    assert device_of(temperature) == devices["Weather station"].id
    assert device_of(rain) == devices["Precipitation station"].id
    # The names stay, apart from Home Assistant capitalising after the device.
    assert hass.states.get(weather_id(hass, entry)).name == "Zurich"
    assert hass.states.get(temperature).name == "Fluntern Temperature"
    assert hass.states.get(rain).name == "Aarberg 10 minute rain"


async def test_each_entry_has_its_own_device(hass, entry):
    await setup(hass, entry)
    mountain = await add_mountain(hass)

    device = dr.async_entries_for_config_entry(dr.async_get(hass), mountain.entry_id)
    assert [(d.name, d.model) for d in device] == [("Bristen", "Mountain forecast")]
    # Zurich has two stations too.
    assert len(dr.async_get(hass).devices) == 4


async def test_entries_share_one_client(hass, entry, fake_ogd):
    await setup(hass, entry)
    await add_mountain(hass)

    assert len(fake_ogd.instances) == 1


def summary(hass, entry, key):
    return hass.states.get(entity_id(hass, "sensor", f"sensor.{entry.entry_id}-{key}"))


async def test_forecast_summaries(hass, entry):
    await setup(hass, entry)

    # At 12:30 UTC the next hour is the one starting 13:00 (i = 4 in the fake).
    rain = summary(hass, entry, "precipitation_next_hour")
    assert rain.state == "0.4"
    assert rain.attributes["unit_of_measurement"] == "mm"
    assert rain.attributes["friendly_name"] == "Zurich Precipitation next hour"
    chance = summary(hass, entry, "precipitation_probability_next_hour")
    assert chance.state == "20.0"
    assert chance.attributes["unit_of_measurement"] == "%"
    # The fake forecast ends at 21:00 UTC, so these windows are not covered.
    for key in (
        "snow_tonight",
        "snow_next_24h",
        "snow_next_48h",
        "sunshine_tomorrow_midday",
        "sunshine_tomorrow",
        "max_gust_tomorrow_midday",
        "min_temperature_tonight",
    ):
        assert summary(hass, entry, key).state == "unavailable", key
    sunshine = summary(hass, entry, "sunshine_tomorrow")
    assert sunshine.attributes["unit_of_measurement"] == "h"


async def test_forecast_summaries_over_whole_windows(hass, entry, fake_ogd):
    # The fake forecast then runs from 09:00 UTC on the 20th for 80 hours, with
    # temperatures of 10 + i °C, gusts of 20 + i km/h and 30 minutes of sun.
    fake_ogd.hours = 80
    await setup(hass, entry)

    def value(key):
        return summary(hass, entry, key).state

    assert value("snow_tonight") == "0.0"  # too warm
    assert value("snow_next_24h") == "0.0"
    assert value("snow_next_48h") == "0.0"
    assert value("sunshine_tomorrow") == "12.0"
    assert value("sunshine_tomorrow_midday") == "3.0"
    # Tomorrow 09-15 local is 07-13 UTC on the 21st: i = 22 to 27.
    assert value("max_gust_tomorrow_midday") == "47.0"
    # Tonight from 18:00 local (16:00 UTC, i = 7) on.
    assert value("min_temperature_tonight") == "17.0"


async def test_summaries_needing_hourly_temperatures(hass, entry_data, fake_ogd):
    """Unavailable without hourly temperatures, rather than a guess."""
    fake_ogd.hours = 80
    entry = MockConfigEntry(
        domain=DOMAIN, data={**entry_data, "hourly_temperature": False}
    )
    entry.add_to_hass(hass)

    await setup(hass, entry)

    for key in (
        "snow_tonight",
        "snow_next_24h",
        "snow_next_48h",
        "min_temperature_tonight",
    ):
        assert summary(hass, entry, key).state == "unavailable", key
    assert summary(hass, entry, "sunshine_tomorrow").state == "12.0"
    assert summary(hass, entry, "precipitation_next_hour").state == "0.4"


async def test_pollen_sensors(hass, entry_data):
    entry = MockConfigEntry(domain=DOMAIN, data={**entry_data, "pollen_station": "PZH"})
    entry.add_to_hass(hass)
    await setup(hass, entry)

    def pollen(kind):
        return hass.states.get(
            entity_id(hass, "sensor", f"sensor.{entry.entry_id}-pollen_{kind}")
        )

    birch = pollen("birch")
    assert birch.state == "42.0"
    assert birch.attributes["unit_of_measurement"] == "pollen/m³"
    assert birch.attributes["friendly_name"] == "Zürich pollen Birch"
    assert birch.attributes["measured"] == "2026-10-20T12:00:00+00:00"
    assert pollen("grasses").state == "7.0"
    assert pollen("beech").state == "unavailable"  # no value for it
    device = dr.async_get(hass).async_get(
        er.async_get(hass).async_get(birch.entity_id).device_id
    )
    assert (device.name, device.model) == ("Zürich pollen", "Pollen station")


async def test_pollen_station_without_recent_data(hass, entry_data):
    entry = MockConfigEntry(domain=DOMAIN, data={**entry_data, "pollen_station": "PLZ"})
    entry.add_to_hass(hass)
    await setup(hass, entry)

    birch = entity_id(hass, "sensor", f"sensor.{entry.entry_id}-pollen_birch")
    assert hass.states.get(birch).state == "unavailable"


def warning_state(hass, entry, key):
    return hass.states.get(entity_id(hass, "sensor", f"sensor.{entry.entry_id}-{key}"))


async def test_weather_warnings(hass, entry, fake_alerts):
    hour = datetime.timedelta(hours=1)
    fake_alerts.warnings = [
        make_warning("wind", "yellow", NOW - 5 * hour, NOW + 3 * hour, "Föhn"),
        make_warning("rain", "orange", NOW + 16 * hour, NOW + 30 * hour),
        make_warning("thunderstorm", "red", NOW + 30 * hour, NOW + 32 * hour),
    ]

    await setup(hass, entry)

    now = warning_state(hass, entry, "weather_warning")
    assert now.state == "yellow"
    assert now.attributes["types"] == ["wind"]
    assert now.attributes["warnings"] == [
        {
            "type": "wind",
            "level": "yellow",
            "event": "Föhn",
            "onset": "2026-10-20T07:30:00+00:00",
            "expires": "2026-10-20T15:30:00+00:00",
            "region": "Flüelen-Unterschächen",
            "headline": "Yellow wind warning",
            "description": "Possible impacts.",
            "url": "https://meteoalarm.org/en/live/region/CH?sa=Fl%C3%BCelen",
        }
    ]
    assert now.attributes["friendly_name"] == "Zurich Weather warning"
    later = warning_state(hass, entry, "weather_warning_next_24h")
    assert later.state == "orange"  # not the red one, in 30 hours
    assert later.attributes["types"] == ["rain", "wind"]
    # Looked up at the forecast point, in Home Assistant's language.
    assert fake_alerts.calls[0] == (47.3875, 8.521, None, "en")


async def test_no_weather_warnings(hass, entry):
    await setup(hass, entry)

    state = warning_state(hass, entry, "weather_warning")
    assert state.state == "none"
    assert state.attributes["warnings"] == []


async def test_weather_warnings_that_cannot_be_obtained(hass, entry, fake_alerts):
    fake_alerts.error = AlertError("down")

    await setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert warning_state(hass, entry, "weather_warning").state == "unavailable"
    assert hass.states.get(weather_id(hass, entry)).state == "sunny"


async def test_a_mountain_is_warned_at_its_height(hass, fake_alerts):
    await add_mountain(hass)

    lat, lon, height, _ = fake_alerts.calls[0]
    assert (lat, lon, height) == (46.7526, 8.7062, 3073.0)


async def test_stations_update_during_a_slow_download(
    hass, entry, fake_ogd, frozen_now, monkeypatch
):
    await setup(hass, entry)
    temperature = entity_id(hass, "sensor", f"sensor.{entry.entry_id}-temperature")
    coordinator = entry.runtime_data

    # Four hours later there is a new run, and its temperatures download slowly.
    fake_ogd.download_gate = asyncio.Event()
    fake_ogd.run = "202610201600"
    monkeypatch.setitem(STATION_MEASUREMENTS, "tre200s0", 15.5)
    frozen_now.tick(datetime.timedelta(hours=4))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    # The station is up to date while the download goes on...
    assert hass.states.get(temperature).state == "15.5"
    assert coordinator.data["forecast"]["run"] == "202610201200"

    # ...and the forecast follows when it is done.
    fake_ogd.download_gate.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator.data["forecast"]["run"] == "202610201600"
    assert coordinator.data["forecast"]["temperature_run"] == "202610201600"
    assert hass.states.get(temperature).state == "15.5"


async def test_a_failed_background_refresh_keeps_the_forecast(
    hass, entry, fake_ogd, frozen_now, monkeypatch
):
    await setup(hass, entry)

    async def broken(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(fake_ogd, "latest_run", broken)
    fake_ogd.run = "202610201600"
    await refresh_after(hass, frozen_now, 20)

    coordinator = entry.runtime_data
    assert coordinator.last_update_success
    assert coordinator.data["forecast"]["run"] == "202610201200"
    # The next due refresh tries again.
    monkeypatch.undo()
    fake_ogd.run = "202610201600"
    await refresh_after(hass, frozen_now, 20)
    assert coordinator.data["forecast"]["run"] == "202610201600"


def foehn_state(hass, entry, key):
    return hass.states.get(entity_id(hass, "sensor", f"sensor.{entry.entry_id}-{key}"))


async def test_foehn_sensors(hass, entry):
    await setup(hass, entry)

    difference = foehn_state(hass, entry, "foehn_pressure_difference")
    assert difference.state == "6.0"
    assert difference.attributes["unit_of_measurement"] == "hPa"
    assert difference.attributes["friendly_name"] == "Zurich Föhn pressure difference"
    assert difference.attributes["south_station"] == "LUG"
    assert difference.attributes["south_pressure"] == 1016.0
    assert difference.attributes["north_station"] == "KLO"
    assert difference.attributes["threshold"] == 4.0
    outlook = foehn_state(hass, entry, "foehn_outlook")
    assert outlook.state == "south"
    # MeteoSwiss's own index, under the weather station.
    measured = foehn_state(hass, entry, "foehn")
    assert measured.state == "foehn"
    assert measured.attributes["friendly_name"] == "Fluntern Föhn"


async def test_foehn_with_other_settings(hass, entry_data):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=entry_data,
        options={
            "foehn": True,
            "foehn_south_station": "LUG",
            "foehn_north_station": "KLO",
            "foehn_threshold": 6.5,
            "foehn_strong_threshold": 10.0,
        },
    )
    entry.add_to_hass(hass)
    await setup(hass, entry)

    assert foehn_state(hass, entry, "foehn_outlook").state == "none"


async def test_foehn_turned_off(hass, entry_data):
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data, options={"foehn": False})
    entry.add_to_hass(hass)
    await setup(hass, entry)

    keys = {
        e.unique_id.rsplit("-", 1)[-1]
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }
    assert not keys & {"foehn", "foehn_outlook", "foehn_pressure_difference"}


async def test_foehn_without_recent_pressure(hass, entry_data, monkeypatch):
    monkeypatch.setitem(FOEHN_PRESSURES, "LUG", None)
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data)
    entry.add_to_hass(hass)
    await setup(hass, entry)

    assert foehn_state(hass, entry, "foehn_pressure_difference").state == "unavailable"
    assert foehn_state(hass, entry, "foehn_outlook").state == "unavailable"


async def test_no_freezing_level_for_a_postal_code_by_default(hass, entry, fake_ogd):
    await setup(hass, entry)

    assert "zprfr0hs" not in fake_ogd.large_downloads
    assert not er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"sensor.{entry.entry_id}-freezing_level"
    )


async def test_freezing_level_for_a_postal_code(hass, entry_data, fake_ogd):
    entry = MockConfigEntry(
        domain=DOMAIN, data=entry_data, options={"freezing_level": True}
    )
    entry.add_to_hass(hass)
    await setup(hass, entry)

    state = hass.states.get(
        entity_id(hass, "sensor", f"sensor.{entry.entry_id}-freezing_level")
    )
    assert state.state == "2900.0"
    assert state.attributes["friendly_name"] == "Zurich Freezing level"
    assert fake_ogd.large_downloads["zprfr0hs"] == [{(800500, 2)}]


async def test_a_postal_code_shares_the_mountains_freezing_level(
    hass, entry_data, fake_ogd, frozen_now
):
    entry = MockConfigEntry(
        domain=DOMAIN, data=entry_data, options={"freezing_level": True}
    )
    entry.add_to_hass(hass)
    await setup(hass, entry)
    await add_mountain(hass)

    fake_ogd.run = "202610201600"
    await refresh_after(hass, frozen_now, 4 * 60)

    # One read for both, after each read its point when set up.
    assert fake_ogd.large_downloads["zprfr0hs"][:2] == [{(800500, 2)}, {(864, 3)}]
    assert fake_ogd.large_downloads["zprfr0hs"][2:] == [{(800500, 2), (864, 3)}]


def climate_state(hass, entry, key):
    return hass.states.get(entity_id(hass, "sensor", f"sensor.{entry.entry_id}-{key}"))


async def test_climate_normals(hass, entry):
    await setup(hass, entry)

    high = climate_state(hass, entry, "high_vs_normal")
    assert high.state == "8.0"  # 22.3 against 14.3
    assert high.attributes["high"] == 22.3
    assert high.attributes["normal_high"] == 14.3
    assert high.attributes["friendly_name"] == "Fluntern High vs normal"
    assert climate_state(hass, entry, "low_vs_normal").state == "0.6"
    month = climate_state(hass, entry, "month_vs_normal")
    assert month.state == "3.2"
    assert month.attributes["days"] == 19
    rain = climate_state(hass, entry, "month_rain_vs_normal")
    # 13.3 mm in 19 days, against 84.2 mm x 19 / 31.
    assert rain.state == "26"
    assert rain.attributes["normal_rain"] == 51.6
    assert climate_state(hass, entry, "month_sunshine_vs_normal").state == "113"
    record = climate_state(hass, entry, "record_high")
    assert record.state == "24.8"
    assert record.attributes["year"] == 2001
    assert record.attributes["record_low"] == -1.4
    assert record.attributes["since"] == 1864


async def test_climate_normals_turned_off(hass, entry_data):
    entry = MockConfigEntry(
        domain=DOMAIN, data=entry_data, options={"climate_normals": False}
    )
    entry.add_to_hass(hass)
    await setup(hass, entry)

    assert not er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"sensor.{entry.entry_id}-record_high"
    )


async def test_climate_normals_that_cannot_be_obtained(hass, entry, fake_ogd):
    fake_ogd.climate_error = OGDError("down")

    await setup(hass, entry)

    assert climate_state(hass, entry, "record_high").state == "unavailable"
    temperature = entity_id(hass, "sensor", f"sensor.{entry.entry_id}-temperature")
    assert hass.states.get(temperature).state == "13.4"


async def test_climate_normals_are_checked_hourly(hass, entry, fake_ogd, frozen_now):
    await setup(hass, entry)
    for _ in range(6):
        await refresh_after(hass, frozen_now, 5)
    assert fake_ogd.climate_calls == 1

    await refresh_after(hass, frozen_now, 35)
    assert fake_ogd.climate_calls == 2


async def test_a_notice_to_update_when_far_more_is_downloaded_than_usual(
    hass, entry, fake_ogd, frozen_now
):
    await setup(hass, entry)
    # A busy day is fine.
    fake_ogd.downloaded = 30 * 1024 * 1024
    fake_ogd.run = "202610201300"
    await refresh_after(hass, frozen_now, 60)
    assert issues(hass) == set()

    fake_ogd.downloaded = 300 * 1024 * 1024
    fake_ogd.run = "202610201400"
    await refresh_after(hass, frozen_now, 60)
    assert issues(hass) == {"high_usage_meteoswiss"}
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "high_usage_meteoswiss")
    assert issue.translation_placeholders == {"usage": "300"}

    fake_ogd.downloaded = 0
    fake_ogd.run = "202610201500"
    await refresh_after(hass, frozen_now, 60)
    assert issues(hass) == set()


async def test_what_entries_share_lasts_until_the_last_is_unloaded(hass, entry):
    await setup(hass, entry)
    mountain = await add_mountain(hass)
    client = hass.data[DATA_CLIENT]
    assert entry.runtime_data.client is mountain.runtime_data.client is client

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert hass.data[DATA_CLIENT] is client

    assert await hass.config_entries.async_unload(mountain.entry_id)
    for key in (DATA_CLIENT, DATA_DOWNLOADS, DATA_ALERTS):
        assert key not in hass.data


RADAR_KEYS = ("rain_rate", "hail_probability", "hail_size")


async def test_no_radar_unless_turned_on(hass, entry):
    await setup(hass, entry)

    for key in RADAR_KEYS:
        assert not er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, f"sensor.{entry.entry_id}-{key}"
        )
    assert FakeRadar.calls == []


async def test_radar_sensors(hass, entry, frozen_now):
    hass.config_entries.async_update_entry(entry, options={"radar": True})
    FakeRadar.readings = {
        "rain_rate": (2.5, NOW),
        "hail_probability": (0.0, NOW),
        "hail_size": None,  # no recent image
    }
    await setup(hass, entry)

    states = {
        key: hass.states.get(
            entity_id(hass, "sensor", f"sensor.{entry.entry_id}-{key}")
        )
        for key in RADAR_KEYS
    }
    assert states["rain_rate"].state == "2.5"
    assert states["rain_rate"].attributes["unit_of_measurement"] == "mm/h"
    assert states["hail_probability"].state == "0.0"
    assert states["hail_size"].state == "unavailable"
    # At the forecast's place, every minute.
    assert FakeRadar.calls == [(47.3875, 8.521)]
    FakeRadar.readings = {**FakeRadar.readings, "rain_rate": (0.0, NOW)}
    await refresh_after(hass, frozen_now, 1)
    assert hass.states.get(states["rain_rate"].entity_id).state == "0.0"


async def test_the_radar_is_read_at_home_near_it(hass, entry):
    # Home in Zürich, 2 km from the postal code's centre.
    await hass.config.async_update(latitude=47.37, longitude=8.54)
    hass.config_entries.async_update_entry(entry, options={"radar": True})

    await setup(hass, entry)

    assert FakeRadar.calls == [(47.37, 8.54)]


async def test_a_mountain_s_radar_is_read_at_its_summit(hass):
    await hass.config.async_update(latitude=46.76, longitude=8.72)
    entry = MockConfigEntry(domain=DOMAIN, data=MOUNTAIN, options={"radar": True})
    entry.add_to_hass(hass)
    await setup(hass, entry)

    assert entry.runtime_data.radar.location == (46.7526, 8.7062)
