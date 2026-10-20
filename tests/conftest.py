"""Shared fixtures."""

from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meteoswiss.const import DOMAIN

from .fakes import NOW, FakeMeteoAlarm, FakeOGD


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/meteoswiss."""


@pytest.fixture
def fake_ogd():
    """Replace the OGD client everywhere with one that never hits the network."""
    FakeOGD.reset()
    FakeMeteoAlarm.reset()
    with (
        patch("custom_components.meteoswiss.MeteoSwissOGD", FakeOGD),
        patch("custom_components.meteoswiss.config_flow.MeteoSwissOGD", FakeOGD),
        patch("custom_components.meteoswiss.MeteoAlarm", FakeMeteoAlarm),
    ):
        yield FakeOGD


@pytest.fixture
def fake_alerts(fake_ogd):
    return FakeMeteoAlarm


@pytest.fixture
def frozen_now(freezer):
    freezer.move_to(NOW)
    return freezer


@pytest.fixture
def entry_data():
    return {
        "postcode": 8005,
        "point_id": 800500,
        "forecast_name": "Zurich",
        "update_interval": 5,
        "station": "SMA",
        "real_time_name": "Fluntern",
        "precipitation_station": "ABE",
        "real_time_precipitation_name": "Aarberg",
    }


@pytest.fixture
def entry(hass, entry_data):
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data, title="Zurich")
    entry.add_to_hass(hass)
    return entry
