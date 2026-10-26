"""The switch that pauses an entry's downloads."""

import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meteoswiss.const import DOMAIN

from .fakes import FakeMeteoAlarm, FakeOGD, FakeRadar
from .test_integration import entity_id, refresh_after, setup, weather_id

pytestmark = pytest.mark.usefixtures("fake_ogd", "frozen_now")


def fetches():
    return (
        FakeOGD.station_calls,
        FakeOGD.run_checks,
        len(FakeMeteoAlarm.calls),
        len(FakeRadar.calls),
    )


@pytest.fixture
def radar_entry(hass, entry_data):
    entry = MockConfigEntry(
        domain=DOMAIN, data=entry_data, options={"radar": True}, title="Zurich"
    )
    entry.add_to_hass(hass)
    return entry


def switch_id(hass, entry):
    return entity_id(hass, "switch", f"switch.{entry.entry_id}-downloads")


async def turn(hass, entry, service):
    await hass.services.async_call(
        "switch", service, {"entity_id": switch_id(hass, entry)}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_paused_fetches_nothing_and_keeps_the_values(
    hass, radar_entry, frozen_now, hass_storage
):
    await setup(hass, radar_entry)
    switch = switch_id(hass, radar_entry)
    assert hass.states.get(switch).state == "on"
    registry_entry = er.async_get(hass).async_get(switch)
    assert registry_entry.entity_category == "config"

    await turn(hass, radar_entry, "turn_off")
    before = fetches()
    await refresh_after(hass, frozen_now, 120)

    assert fetches() == before
    assert hass.states.get(switch).state == "off"
    assert hass_storage["meteoswiss.paused"]["data"] == {
        "paused": [radar_entry.entry_id]
    }
    state = hass.states.get(weather_id(hass, radar_entry))
    assert state.attributes["temperature"] == 13.4

    await turn(hass, radar_entry, "turn_on")

    station_calls, run_checks, warning_calls, radar_calls = fetches()
    assert station_calls > before[0]
    assert warning_calls > before[2]
    assert radar_calls > before[3]
    assert hass.states.get(switch).state == "on"
    assert hass_storage["meteoswiss.paused"]["data"] == {"paused": []}


async def test_paused_across_a_restart(hass, radar_entry, hass_storage):
    hass_storage["meteoswiss.paused"] = {
        "version": 1,
        "key": "meteoswiss.paused",
        "data": {"paused": [radar_entry.entry_id]},
    }
    await setup(hass, radar_entry)

    # Nothing fetched, so there is nothing to show yet.
    assert fetches() == (0, 0, 0, 0)
    assert hass.states.get(switch_id(hass, radar_entry)).state == "off"
    assert hass.states.get(weather_id(hass, radar_entry)).state == "unavailable"

    # Turned on, it is set up again, with the radar.
    await turn(hass, radar_entry, "turn_on")

    state = hass.states.get(weather_id(hass, radar_entry))
    assert state.attributes["temperature"] == 13.4
    assert FakeRadar.calls
    assert hass.states.get(switch_id(hass, radar_entry)).state == "on"


async def test_removing_an_entry_forgets_its_pause(hass, radar_entry, hass_storage):
    await setup(hass, radar_entry)
    await turn(hass, radar_entry, "turn_off")

    assert await hass.config_entries.async_remove(radar_entry.entry_id)
    await hass.async_block_till_done()

    assert hass_storage["meteoswiss.paused"]["data"] == {"paused": []}
