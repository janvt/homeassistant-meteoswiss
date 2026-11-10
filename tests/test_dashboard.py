"""Serving the dashboard strategy to the frontend."""

from unittest.mock import patch

from homeassistant.setup import async_setup_component
from homeassistant.util.json import json_loads

from custom_components.meteoswiss.dashboard import STRATEGY_URL, async_setup_dashboard

COMPONENT = "custom_components/meteoswiss"


async def test_served_and_loaded_with_the_frontend(hass, hass_client):
    assert await async_setup_component(hass, "http", {})
    hass.config.components.add("frontend")
    with patch("custom_components.meteoswiss.dashboard.add_extra_js_url") as add:
        await async_setup_dashboard(hass)

    with open(f"{COMPONENT}/manifest.json") as manifest:
        version = json_loads(manifest.read())["version"]
    add.assert_called_once_with(hass, f"{STRATEGY_URL}?v={version}")
    client = await hass_client()
    response = await client.get(STRATEGY_URL)
    assert response.status == 200
    assert "ll-strategy-dashboard-meteoswiss" in await response.text()


async def test_nothing_without_the_frontend(hass):
    assert await async_setup_component(hass, "http", {})
    with patch("custom_components.meteoswiss.dashboard.add_extra_js_url") as add:
        await async_setup_dashboard(hass)
    add.assert_not_called()
