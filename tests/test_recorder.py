"""What the recorder keeps of the entities: nothing that only grows the database."""

import datetime

import pytest
from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.meteoswiss.const import DOMAIN

from .fakes import make_warning

pytestmark = pytest.mark.usefixtures("fake_ogd")


@pytest.fixture(autouse=True)
def _enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """The recorder has to be set up before Home Assistant (conftest's order)."""


HOUR = datetime.timedelta(hours=1)


def entity(hass, entry, key):
    return er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"sensor.{entry.entry_id}-{key}"
    )


async def test_warning_texts_are_not_recorded(
    recorder_mock, hass, entry_data, fake_alerts
):
    fake_alerts.warnings = [
        make_warning("wind", "yellow", dt_util.utcnow() - HOUR, dt_util.utcnow() + HOUR)
    ]
    start = dt_util.utcnow() - HOUR
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    sensor = entity(hass, entry, "weather_warning")
    # The state itself has them...
    assert hass.states.get(sensor).attributes["warnings"][0]["type"] == "wind"
    # ...but the recorded one does not.
    recorded = await recorder_mock.async_add_executor_job(
        state_changes_during_period, hass, start, None, sensor
    )
    attributes = recorded[sensor][-1].attributes
    assert recorded[sensor][-1].state == "yellow"
    assert "warnings" not in attributes
    assert attributes["types"] == ["wind"]


async def test_forecasts_have_no_long_term_statistics(hass, entry_data, frozen_now):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**entry_data, "pollen_station": "PZH"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    def state_class(key):
        return hass.states.get(entity(hass, entry, key)).attributes.get("state_class")

    for key in ("snow_tonight", "sunshine_tomorrow", "precipitation_next_hour"):
        assert state_class(key) is None, key
    # Measurements keep theirs.
    assert state_class("temperature") == "measurement"
    assert state_class("pollen_birch") == "measurement"


async def test_freezing_level_has_no_long_term_statistics(hass, frozen_now):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "point_type": 3,
            "point_id": 864,
            "forecast_name": "Bristen",
            "update_interval": 5,
            "hourly_temperature": True,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(entity(hass, entry, "freezing_level"))
    assert state.state == "2900.0"
    assert "state_class" not in state.attributes
