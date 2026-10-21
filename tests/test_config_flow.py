"""The config flow, with the OGD client faked."""

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meteoswiss.const import DOMAIN
from custom_components.meteoswiss.ogd import OGDError

pytestmark = pytest.mark.usefixtures("fake_ogd")

ZURICH = {"latitude": 47.3769, "longitude": 8.5417}
NO_WEATHER = "No real-time weather station"
NO_PRECIPITATION = "No real-time precipitation station"


def defaults(result):
    return {str(key): key.default() for key in result["data_schema"].schema}


async def start(hass, step="user_two"):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ZURICH)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["user_two", "mountain"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": step}
    )
    assert result["step_id"] == step
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
        ({"latitude": 91.0, "longitude": 8.5}, {"latitude": "latitude_error"}),
        ({"latitude": 47.0, "longitude": 181.0}, {"longitude": "longitude_error"}),
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
        "hourly_temperature": True,
    }


async def test_postcode_cannot_be_guessed_when_metadata_is_unavailable(
    hass, fake_ogd, monkeypatch
):
    async def fail(self, lat, lon):
        raise OGDError("down")

    monkeypatch.setattr(fake_ogd, "get_nearest_postcode_point", fail)

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
        ({"forecast_name": "  "}, {"forecast_name": "forecast_name_empty"}),
        ({"update_interval": 0}, {"update_interval": "update_interval_too_low"}),
    ],
)
async def test_invalid_forecast_settings(hass, changes, error):
    result = await start(hass)
    user_input = {
        "postcode": "8005",
        "forecast_name": "Zurich",
        "update_interval": 5,
        "hourly_temperature": True,
    }

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
        "pollen_station": "Zürich (PZH)",
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
        "hourly_temperature": True,
        "station": "SMA",
        "real_time_name": "Fluntern",
        "precipitation_station": "ABE",
        "real_time_precipitation_name": "Aarberg",
        "pollen_station": "PZH",
    }


async def test_flow_without_stations(hass):
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "postcode": "8005",
            "forecast_name": "Zurich",
            "update_interval": 5,
            "hourly_temperature": False,
        },
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "station": "No real-time weather station",
            "real_time_name": "",
            "precipitation_station": "No real-time precipitation station",
            "real_time_precipitation_name": "",
            "pollen_station": "No pollen station",
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Zurich"
    assert result["data"] == {
        "postcode": 8005,
        "point_id": 800500,
        "forecast_name": "Zurich",
        "update_interval": 5,
        "hourly_temperature": False,
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


@pytest.mark.parametrize(
    ("data", "options", "shown"),
    [
        ({}, {}, True),  # entries from before the option existed
        ({"hourly_temperature": False}, {}, False),
        ({"hourly_temperature": False}, {"hourly_temperature": True}, True),
    ],
)
async def test_options_show_the_current_setting(hass, entry_data, data, options, shown):
    entry = MockConfigEntry(domain=DOMAIN, data={**entry_data, **data}, options=options)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["step_id"] == "init"
    assert defaults(result)["hourly_temperature"] is shown


async def test_options_turn_hourly_temperatures_off(hass, entry, fake_ogd):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert fake_ogd.temperature_calls == 1

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"hourly_temperature": False}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["hourly_temperature"] is False
    # The entry was reloaded with the new setting and did not download again.
    coordinator = entry.runtime_data
    assert coordinator.hourly_temperature is False
    assert coordinator.data["forecast"]["temperature_run"] is None
    assert fake_ogd.temperature_calls == 1


async def test_yaml_import(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "import"},
        data={
            "name": "Zurich",
            "postcode": 8005,
            "station": "SMA",
            "precipitation_station": "ABE",
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Zurich"
    assert result["data"] == {
        "postcode": 8005,
        "update_interval": 5,
        "forecast_name": "Zurich",
        "station": "SMA",
        "real_time_name": "Zurich",
        "precipitation_station": "ABE",
        "real_time_precipitation_name": "Zurich",
    }


async def test_yaml_is_imported_only_once(hass):
    config = {"name": "Zurich", "postcode": 8005}
    for _ in range(2):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "import"}, data=config
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


def options(result, field):
    """The (value, label) choices of a select field."""
    for key, validator in result["data_schema"].schema.items():
        if str(key) == field:
            return [(o["value"], o["label"]) for o in validator.config["options"]]
    raise KeyError(field)


async def test_mountains_are_listed_closest_first(hass):
    result = await start(hass, "mountain")

    assert not result["errors"]
    assert options(result, "point_id") == [
        ("864", "Bristen, 3073 m, 71 km"),
        ("442", "Treschhütte SAC, 1475 m, 71 km"),
        ("213", "Ouest romand, 490 m, 136 km"),
    ]
    assert defaults(result) == {
        "point_id": "864",
        "forecast_name": "",
        "update_interval": 5,
        "hourly_temperature": True,
    }


async def test_mountain_flow(hass):
    result = await start(hass, "mountain")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"point_id": "864", "update_interval": 10, "hourly_temperature": True},
    )
    assert result["step_id"] == "user_three"
    # Summits rarely have a station nearby, so none is suggested.
    assert defaults(result)["station"] == "No real-time weather station"
    assert defaults(result)["precipitation_station"] == (
        "No real-time precipitation station"
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
    # Named after the mountain, as no name was given.
    assert result["title"] == "Bristen"
    assert result["data"] == {
        "point_type": 3,
        "point_id": 864,
        "forecast_name": "Bristen",
        "update_interval": 10,
        "hourly_temperature": True,
    }


async def test_a_mountain_can_be_named(hass):
    result = await start(hass, "mountain")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "point_id": "442",
            "forecast_name": " Hut ",
            "update_interval": 5,
            "hourly_temperature": False,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "station": "Zürich / Fluntern (SMA)",
            "real_time_name": "Fluntern",
            "precipitation_station": "No real-time precipitation station",
            "real_time_precipitation_name": "",
        },
    )

    assert result["title"] == "Hut / Fluntern"
    assert result["data"]["forecast_name"] == "Hut"
    assert result["data"]["point_id"] == 442
    assert result["data"]["station"] == "SMA"


async def test_mountain_list_unavailable(hass, fake_ogd, monkeypatch):
    async def fail(self, lat, lon):
        raise OGDError("down")

    monkeypatch.setattr(fake_ogd, "get_poi_points", fail)

    result = await start(hass, "mountain")

    assert result["errors"] == {"base": "cannot_query_points"}


async def test_only_the_closest_mountains_are_listed(hass, monkeypatch):
    monkeypatch.setattr("custom_components.meteoswiss.config_flow.MAX_MOUNTAINS", 2)

    result = await start(hass, "mountain")

    assert [value for value, _ in options(result, "point_id")] == ["864", "442"]


async def settle(hass, entry):
    """Let the reload after reconfiguring finish, then stop the entry."""
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def reconfigure(hass, entry, step="user_two"):
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ZURICH)
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": step}
    )


STATIONS = {
    "station": "Zürich / Fluntern (SMA)",
    "real_time_name": "Fluntern",
    "precipitation_station": "Aarberg (ABE)",
    "real_time_precipitation_name": "Aarberg",
}


async def test_reconfigure_starts_from_the_current_settings(hass, entry):
    result = await reconfigure(hass, entry)

    assert defaults(result) == {
        "postcode": "8005",
        "forecast_name": "Zurich",
        "update_interval": 5,
        "hourly_temperature": True,
    }
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**defaults(result), "update_interval": 10}
    )
    # The entry has no pollen station, so none is suggested.
    assert defaults(result) == {**STATIONS, "pollen_station": "No pollen station"}


async def test_reconfigure_a_postcode(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await reconfigure(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "postcode": "1000",
            "forecast_name": "Lausanne",
            "update_interval": 5,
            "hourly_temperature": True,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**STATIONS, "precipitation_station": NO_PRECIPITATION}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert entry.title == "Lausanne / Fluntern"
    assert entry.data == {
        "postcode": 1000,
        "point_id": 100026,  # the point of 1000 closest to the location
        "forecast_name": "Lausanne",
        "update_interval": 5,
        "hourly_temperature": True,
        "station": "SMA",
        "real_time_name": "Fluntern",
    }
    # Reloaded with the new settings.
    assert entry.runtime_data.postcode == 1000


async def test_reconfigure_a_postcode_into_a_mountain(hass, entry):
    result = await reconfigure(hass, entry, "mountain")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"point_id": "864", "update_interval": 5, "hourly_temperature": True},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "station": NO_WEATHER,
            "real_time_name": "",
            "precipitation_station": NO_PRECIPITATION,
            "real_time_precipitation_name": "",
        },
    )

    await settle(hass, entry)
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {
        "point_type": 3,
        "point_id": 864,
        "forecast_name": "Bristen",
        "update_interval": 5,
        "hourly_temperature": True,
    }


async def test_reconfigure_a_mountain_starts_from_it(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "point_type": 3,
            "point_id": 442,
            "forecast_name": "Hut",
            "update_interval": 7,
            "hourly_temperature": False,
        },
    )
    entry.add_to_hass(hass)

    result = await reconfigure(hass, entry, "mountain")

    assert defaults(result) == {
        "point_id": "442",
        "forecast_name": "Hut",
        "update_interval": 7,
        "hourly_temperature": False,
    }


async def test_reconfigure_drops_a_station_that_is_gone(hass, entry_data):
    entry = MockConfigEntry(
        domain=DOMAIN, data={**entry_data, "station": "MRP", "real_time_name": "Old"}
    )
    entry.add_to_hass(hass)

    result = await reconfigure(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], defaults(result)
    )

    assert defaults(result)["station"] == NO_WEATHER
    assert defaults(result)["precipitation_station"] == "Aarberg (ABE)"


async def test_reconfigure_moves_the_option_into_the_settings(hass, entry_data):
    entry = MockConfigEntry(
        domain=DOMAIN, data=entry_data, options={"hourly_temperature": False}
    )
    entry.add_to_hass(hass)

    result = await reconfigure(hass, entry)
    assert defaults(result)["hourly_temperature"] is False
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**defaults(result), "hourly_temperature": True}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], STATIONS)

    await settle(hass, entry)
    assert entry.data["hourly_temperature"] is True
    assert entry.options == {}


async def test_reconfigure_clears_the_notices_of_the_old_settings(hass, entry):
    for issue_id in (
        "SMA_real_time_name_provides_no_data_meteoswiss",
        "8005_provides_no_forecast_meteoswiss",
    ):
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="station_no_data",
        )

    result = await reconfigure(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], defaults(result)
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**STATIONS, "station": NO_WEATHER, "real_time_name": ""}
    )

    await settle(hass, entry)
    assert result["reason"] == "reconfigure_successful"
    assert not [i for (d, i) in ir.async_get(hass).issues if d == DOMAIN]


async def test_removing_an_entry_clears_its_notices(hass, entry):
    ir.async_create_issue(
        hass,
        DOMAIN,
        "SMA_real_time_name_provides_no_data_meteoswiss",
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="station_no_data",
    )

    await hass.config_entries.async_remove(entry.entry_id)

    assert not [i for (d, i) in ir.async_get(hass).issues if d == DOMAIN]


async def test_the_closest_pollen_station_is_suggested(hass):
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"postcode": "8005", "forecast_name": "Zurich", "update_interval": 5},
    )

    assert defaults(result)["pollen_station"] == "Zürich (PZH)"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**STATIONS, "pollen_station": "Luzern (PLZ)"}
    )
    assert result["data"]["pollen_station"] == "PLZ"


async def test_no_pollen_station_is_suggested_for_a_mountain(hass):
    result = await start(hass, "mountain")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"point_id": "864", "update_interval": 5, "hourly_temperature": True},
    )
    assert defaults(result)["pollen_station"] == "No pollen station"


async def test_reconfigure_keeps_the_pollen_station(hass, entry_data):
    entry = MockConfigEntry(domain=DOMAIN, data={**entry_data, "pollen_station": "PLZ"})
    entry.add_to_hass(hass)

    result = await reconfigure(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], defaults(result)
    )

    assert defaults(result)["pollen_station"] == "Luzern (PLZ)"


async def test_foehn_options_have_sane_defaults(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert defaults(result) == {
        "hourly_temperature": True,
        "freezing_level": False,  # a postal code
        "climate_normals": True,
        "foehn": True,
        "foehn_south_station": "LUG",
        "foehn_north_station": "KLO",
        "foehn_threshold": 4.0,
        "foehn_strong_threshold": 8.0,
    }


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"foehn_north_station": "LUG"}, {"foehn_north_station": "same_foehn_station"}),
        (
            {"foehn_strong_threshold": 3.0},
            {"foehn_strong_threshold": "foehn_thresholds"},
        ),
    ],
)
async def test_foehn_options_are_checked(hass, entry, changes, error):
    result = await hass.config_entries.options.async_init(entry.entry_id)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**defaults(result), **changes}
    )

    assert result["step_id"] == "init"
    assert result["errors"] == error


async def test_foehn_options_are_saved(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**defaults(result), "foehn_north_station": "SMA", "foehn_threshold": 5.0},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["foehn_north_station"] == "SMA"
    assert entry.options["foehn_threshold"] == 5.0


@pytest.mark.parametrize(
    ("data", "options", "shown"),
    [
        ({"point_type": 3, "hourly_temperature": True}, {}, True),
        ({"point_type": 3, "hourly_temperature": False}, {}, False),
        (
            {"point_type": 3, "hourly_temperature": True},
            {"freezing_level": False},
            False,
        ),
        ({}, {"freezing_level": True}, True),
    ],
)
async def test_the_freezing_level_setting(hass, entry_data, data, options, shown):
    entry = MockConfigEntry(domain=DOMAIN, data={**entry_data, **data}, options=options)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert defaults(result)["freezing_level"] is shown
