"""Setup, entities and forecasts, with the OGD client faked."""

import datetime

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.meteoswiss.const import DOMAIN
from custom_components.meteoswiss.ogd import OGDError

pytestmark = pytest.mark.usefixtures("fake_ogd", "frozen_now")


async def setup(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def entity_id(hass, platform, unique_id):
    found = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert found, f"no {platform} entity with unique id {unique_id}"
    return found


def weather_id(hass, entry):
    return entity_id(hass, "weather", f"weather.{entry.entry_id}")


async def refresh_after(hass, freezer, minutes):
    freezer.tick(datetime.timedelta(minutes=minutes))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_weather_entity(hass, entry):
    await setup(hass, entry)

    state = hass.states.get(weather_id(hass, entry))

    # 12:30 falls in the hour starting 12:00, which has symbol 1.
    assert state.state == "sunny"
    assert state.attributes["temperature"] == 13.4
    assert state.attributes["humidity"] == 80  # rounded by Home Assistant
    assert state.attributes["pressure"] == 940.5
    assert state.attributes["wind_speed"] == 8.6
    assert state.attributes["wind_bearing"] == 31.0
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
    # Missing measurements are unavailable rather than wrong.
    assert value("dew_point") == "unavailable"


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
    assert hours[5]["datetime"].startswith("2026-10-20T17:00")
    assert hours[5]["condition"] == "clear-night"  # symbol 101
    assert hours[7]["condition"] is None  # symbol 999 is unknown


async def test_unrecognised_symbol_is_unavailable(hass, entry, frozen_now):
    await setup(hass, entry)
    frozen_now.move_to("2026-10-20T19:30:00+00:00")

    await hass.data[DOMAIN][entry.entry_id].async_refresh()
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
    assert hass.data[DOMAIN][entry.entry_id].data["forecast"]["run"] == "202610201300"


async def test_previous_forecast_is_kept_when_a_check_fails(
    hass, entry, fake_ogd, frozen_now
):
    await setup(hass, entry)
    fake_ogd.forecast_error = OGDError("down")

    await refresh_after(hass, frozen_now, 16)

    coordinator = hass.data[DOMAIN][entry.entry_id]
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

    assert not hass.states.async_all("sensor")
    state = hass.states.get(weather_id(hass, entry))
    assert state.state == "sunny"
    assert "temperature" not in state.attributes
    assert "No real-time stations" in state.attributes["attribution"]


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
    coordinator = hass.data[DOMAIN][entry.entry_id]
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

    forecast = hass.data[DOMAIN][entry.entry_id].data["forecast"]
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
