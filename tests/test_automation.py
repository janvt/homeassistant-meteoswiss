"""The triggers and conditions of the automation editor."""

import json
from pathlib import Path

import pytest
import yaml
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.meteoswiss.condition import CONDITIONS
from custom_components.meteoswiss.const import DOMAIN
from custom_components.meteoswiss.trigger import TRIGGERS

from .harness import MockConfigEntry

# Home Assistant has the API from 2026.11.
pytestmark = [
    pytest.mark.skipif(not TRIGGERS, reason="no trigger API"),
    pytest.mark.usefixtures("fake_ogd", "frozen_now"),
]

COMPONENT = Path(__file__).parent.parent / "custom_components" / "meteoswiss"
LANGUAGES = sorted(
    p.stem for p in (COMPONENT / "translations").glob("*.json") if p.stem != "rm"
)
KEYS = (
    "weather_warning",
    "weather_warning_next_24h",
    "rain_rate",
    "hail_probability",
    "foehn_outlook",
    "pollen_birch",
    "max_temperature_today",
)


@pytest.fixture
async def sensors(hass, entry_data, entity_registry_enabled_by_default):
    """The entity IDs of an entry's sensors with radar, by their keys."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**entry_data, "pollen_station": "PZH"},
        options={"radar": True},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    registry = er.async_get(hass)
    found = {
        key: registry.async_get_entity_id(
            "sensor", DOMAIN, f"sensor.{entry.entry_id}-{key}"
        )
        for key in KEYS
    }
    assert all(found.values()), found
    return found


def device_of(hass, entity_id):
    return er.async_get(hass).async_get(entity_id).device_id


async def automate(hass, trigger, condition=None):
    """An automation of the trigger that fires the event "fired"."""
    fired = []
    hass.bus.async_listen("fired", fired.append)
    config = {
        "triggers": [trigger],
        "actions": [{"event": "fired", "event_data": {}}],
    }
    if condition:
        config["conditions"] = [condition]
    assert await async_setup_component(hass, "automation", {"automation": config})
    await hass.async_block_till_done()
    return fired


async def set_states(hass, entity_id, *states):
    """Set states in turn, each a state or a state and attributes."""
    for state in states:
        value, attributes = state if isinstance(state, tuple) else (state, {})
        hass.states.async_set(entity_id, value, attributes)
        await hass.async_block_till_done()


def warnings(*items):
    return {"warnings": [{"type": t, "level": level} for t, level in items]}


async def test_a_warning_reaching_a_level(hass, sensors):
    sensor = sensors["weather_warning"]
    await set_states(hass, sensor, "none")
    fired = await automate(
        hass,
        {
            "trigger": "meteoswiss.warning_reached",
            "target": {"entity_id": sensor},
            "options": {"level": "orange"},
        },
    )

    await set_states(hass, sensor, "yellow")
    assert not fired
    await set_states(hass, sensor, "orange")
    assert len(fired) == 1
    # Not again while it stays at the level or above.
    await set_states(hass, sensor, "red", "orange")
    assert len(fired) == 1
    await set_states(hass, sensor, "none", "red")
    assert len(fired) == 2
    # Nor when the sensor comes back.
    await set_states(hass, sensor, "unavailable", "red")
    assert len(fired) == 2


async def test_a_warning_of_some_types(hass, sensors):
    sensor = sensors["weather_warning_next_24h"]
    await set_states(hass, sensor, ("none", warnings()))
    fired = await automate(
        hass,
        {
            "trigger": "meteoswiss.warning_reached",
            "target": {"entity_id": sensor},
            "options": {"level": "yellow", "types": ["wind", "thunderstorm"]},
        },
    )

    await set_states(hass, sensor, ("orange", warnings(("rain", "orange"))))
    assert not fired
    # The same state, now with a warning of wind.
    await set_states(
        hass, sensor, ("orange", warnings(("rain", "orange"), ("wind", "yellow")))
    )
    assert len(fired) == 1


async def test_a_device_as_target_is_narrowed_to_the_sensors(hass, sensors):
    rain = sensors["rain_rate"]
    await set_states(hass, rain, "0.0")
    await set_states(hass, sensors["max_temperature_today"], "0")
    fired = await automate(
        hass,
        {
            "trigger": "meteoswiss.rain_started",
            "target": {"device_id": device_of(hass, rain)},
            "options": {"rate": 0.5},
        },
    )

    await set_states(hass, sensors["max_temperature_today"], "2500")
    assert not fired
    await set_states(hass, rain, "0.3")
    assert not fired
    await set_states(hass, rain, "1.2", "4.0")
    assert len(fired) == 1


async def test_rain_stopping(hass, sensors):
    rain = sensors["rain_rate"]
    await set_states(hass, rain, "2.0")
    fired = await automate(
        hass,
        {"trigger": "meteoswiss.rain_stopped", "target": {"entity_id": rain}},
    )

    await set_states(hass, rain, "0.5")
    assert not fired
    await set_states(hass, rain, "0.0", "0.0")
    assert len(fired) == 1


async def test_hail_expected(hass, sensors):
    hail = sensors["hail_probability"]
    await set_states(hass, hail, "0")
    fired = await automate(
        hass,
        {
            "trigger": "meteoswiss.hail_expected",
            "target": {"entity_id": hail},
            "options": {"probability": 60},
        },
    )

    await set_states(hass, hail, "40")
    assert not fired
    await set_states(hass, hail, "70", "90")
    assert len(fired) == 1


@pytest.mark.parametrize(
    ("outlook", "states", "times"),
    [
        ("south", ["south", "strong_south", "none", "strong_south"], 2),
        ("south", ["north", "strong_north"], 0),
        ("strong_south", ["south"], 0),
        ("strong_south", ["south", "strong_south"], 1),
        ("north", ["south", "north"], 1),
    ],
)
async def test_foehn_expected(hass, sensors, outlook, states, times):
    sensor = sensors["foehn_outlook"]
    await set_states(hass, sensor, "none")
    fired = await automate(
        hass,
        {
            "trigger": "meteoswiss.foehn_expected",
            "target": {"entity_id": sensor},
            "options": {"outlook": outlook},
        },
    )

    await set_states(hass, sensor, *states)
    assert len(fired) == times


async def test_pollen_crossing_a_threshold(hass, sensors):
    pollen = sensors["pollen_birch"]
    attributes = {"unit_of_measurement": "pollen/m³"}
    await set_states(hass, pollen, ("10", attributes))
    fired = await automate(
        hass,
        {
            "trigger": "meteoswiss.pollen_crossed_threshold",
            "target": {"device_id": device_of(hass, pollen)},
            "options": {"threshold": {"type": "above", "value": {"number": 70}}},
        },
    )

    await set_states(hass, pollen, ("50", attributes))
    assert not fired
    await set_states(hass, pollen, ("80", attributes), ("120", attributes))
    assert len(fired) == 1


async def test_a_state_that_is_not_a_value(hass, sensors):
    rain = sensors["rain_rate"]
    await set_states(hass, rain, "0.0")
    fired = await automate(
        hass,
        {"trigger": "meteoswiss.rain_started", "target": {"entity_id": rain}},
    )

    await set_states(hass, rain, "heavy")
    assert not fired


async def test_conditions(hass, sensors):
    rain = sensors["rain_rate"]
    warning = sensors["weather_warning"]
    await set_states(hass, rain, "0.0")
    await set_states(hass, warning, ("orange", warnings(("rain", "orange"))))
    fired = await automate(
        hass,
        {"trigger": "event", "event_type": "check"},
        {
            "condition": "and",
            "conditions": [
                {
                    "condition": "meteoswiss.is_raining",
                    "target": {"entity_id": rain},
                    "options": {"rate": 0.5},
                },
                {
                    "condition": "meteoswiss.is_warning",
                    "target": {"device_id": device_of(hass, warning)},
                    "options": {"level": "orange", "types": ["wind"]},
                },
            ],
        },
    )

    async def check():
        hass.bus.async_fire("check")
        await hass.async_block_till_done()

    await check()
    await set_states(hass, rain, "1.0")
    await check()
    assert not fired  # the orange warning is of rain
    await set_states(
        hass, warning, ("red", warnings(("rain", "red"), ("wind", "orange")))
    )
    await check()
    assert len(fired) == 1
    await set_states(hass, warning, "yellow")
    await check()
    assert len(fired) == 1


def load_yaml(name):
    return yaml.safe_load((COMPONENT / name).read_text())


@pytest.mark.parametrize(
    ("kind", "platform", "yaml_name"),
    [
        ("triggers", TRIGGERS, "triggers.yaml"),
        ("conditions", CONDITIONS, "conditions.yaml"),
    ],
)
def test_descriptions_texts_and_icons(kind, platform, yaml_name):
    described = {k: v for k, v in load_yaml(yaml_name).items() if not k.startswith(".")}
    assert set(described) == set(platform)
    icons = json.loads((COMPONENT / "icons.json").read_text())[kind]
    assert set(icons) == set(platform)
    for language in LANGUAGES:
        translation = json.loads(
            (COMPONENT / "translations" / f"{language}.json").read_text()
        )
        texts = translation[kind]
        assert set(texts) == set(platform), language
        for key, description in described.items():
            assert set(texts[key]["fields"]) == set(description["fields"]), key
            for field in description["fields"].values():
                select = field["selector"].get("select")
                if select:
                    options = translation["selector"][select["translation_key"]]
                    assert set(options["options"]) == set(select["options"])


async def test_the_editor_lists_them(hass, sensors):
    from homeassistant.helpers.condition import (
        async_get_all_descriptions as conditions,
    )
    from homeassistant.helpers.trigger import async_get_all_descriptions as triggers

    assert await async_setup_component(hass, "automation", {})
    listed = await triggers(hass)
    assert {f"{DOMAIN}.{k}" for k in TRIGGERS} <= set(listed)
    listed = await conditions(hass)
    assert {f"{DOMAIN}.{k}" for k in CONDITIONS} <= set(listed)
