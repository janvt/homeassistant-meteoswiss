"""The config flow, with the OGD client faked."""

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.meteoswiss.const import DOMAIN
from custom_components.meteoswiss.ogd import OGDError

pytestmark = pytest.mark.usefixtures("fake_ogd")

ZURICH = {"latitude": 47.3769, "longitude": 8.5417}


def defaults(result):
    return {str(key): key.default() for key in result["data_schema"].schema}


async def start(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ZURICH)
    assert result["step_id"] == "user_two"
    return result


async def test_the_location_defaults_to_the_home_location(hass):
    hass.config.latitude = 46.2044
    hass.config.longitude = 6.1432

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )

    assert defaults(result) == {"latitude": 46.2044, "longitude": 6.1432}


@pytest.mark.parametrize(
    ("user_input", "error"),
    [
        ({"latitude": 91.0, "longitude": 8.5}, {"lat": "latitude_error"}),
        ({"latitude": 47.0, "longitude": 181.0}, {"lon": "longitude_error"}),
    ],
)
async def test_invalid_location(hass, user_input, error):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )

    assert result["step_id"] == "user"
    assert result["errors"] == error


async def test_postcode_and_name_are_guessed_from_the_location(hass):
    result = await start(hass)

    assert not result["errors"]
    assert defaults(result) == {
        "postcode": "8001",
        "forecast_name": "Zürich",
        "update_interval": 5,
    }


async def test_postcode_cannot_be_guessed_when_metadata_is_unavailable(hass, fake_ogd):
    async def fail(self, lat, lon):
        raise OGDError("down")

    fake_ogd.get_nearest_postcode_point = fail

    result = await start(hass)

    assert result["errors"] == {
        "postcode": "cannot_query_postcode",
        "forecast_name": "cannot_query_address",
    }


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"postcode": "80"}, {"postcode": "invalid_postcode"}),
        ({"postcode": "9999"}, {"postcode": "invalid_postcode"}),
        ({"forecast_name": "  "}, {"forecast_name": "real_time_name_empty"}),
        ({"update_interval": 0}, {"update_interval": "update_interval_too_low"}),
    ],
)
async def test_invalid_forecast_settings(hass, changes, error):
    result = await start(hass)
    user_input = {"postcode": "8005", "forecast_name": "Zurich", "update_interval": 5}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**user_input, **changes}
    )

    assert result["step_id"] == "user_two"
    assert result["errors"] == error


async def test_full_flow_with_stations(hass):
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"postcode": "8005", "forecast_name": "Zurich", "update_interval": 10},
    )
    assert result["step_id"] == "user_three"
    # The closest stations are suggested.
    assert defaults(result) == {
        "station": "Zürich / Fluntern (SMA)",
        "real_time_name": "Zürich / Fluntern",
        "precipitation_station": "Aarberg (ABE)",
        "real_time_precipitation_name": "Aarberg",
    }

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "station": "Zürich / Fluntern (SMA)",
            "real_time_name": "Fluntern",
            "precipitation_station": "Aarberg (ABE)",
            "real_time_precipitation_name": "Aarberg",
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Zurich / Fluntern / Aarberg"
    assert result["data"] == {
        "postcode": 8005,
        "point_id": 800500,
        "forecast_name": "Zurich",
        "update_interval": 10,
        "station": "SMA",
        "real_time_name": "Fluntern",
        "precipitation_station": "ABE",
        "real_time_precipitation_name": "Aarberg",
    }


async def test_flow_without_stations(hass):
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"postcode": "8005", "forecast_name": "Zurich", "update_interval": 5},
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "station": "No real-time weather station",
            "real_time_name": "",
            "precipitation_station": "No real-time precipitation station",
            "real_time_precipitation_name": "",
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Zurich"
    assert result["data"] == {
        "postcode": 8005,
        "point_id": 800500,
        "forecast_name": "Zurich",
        "update_interval": 5,
    }


async def test_a_station_needs_a_name(hass):
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"postcode": "8005", "forecast_name": "Zurich", "update_interval": 5},
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "station": "Zürich / Fluntern (SMA)",
            "real_time_name": " ",
            "precipitation_station": "No real-time precipitation station",
            "real_time_precipitation_name": "",
        },
    )

    assert result["step_id"] == "user_three"
    assert result["errors"] == {"real_time_name": "empty_name"}
