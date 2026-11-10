"""The dashboard strategy, which the frontend loads as a module.

It is served from the integration's "frontend" folder, with the version in
its URL so that browsers fetch it again after an update.  Without the
frontend, as in a setup without default_config, there is nothing to load it
into.
"""

from __future__ import annotations

from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from custom_components.meteoswiss.const import DOMAIN

STRATEGY_FILE = Path(__file__).parent / "frontend" / "meteoswiss-strategy.js"
STRATEGY_URL = f"/{DOMAIN}/meteoswiss-strategy.js"


async def async_setup_dashboard(hass: HomeAssistant) -> None:
    """Serve the strategy and have the frontend load it."""
    if hass.http is None or "frontend" not in hass.config.components:
        return
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STRATEGY_URL, str(STRATEGY_FILE), cache_headers=False)]
    )
    version = (await async_get_integration(hass, DOMAIN)).version
    add_extra_js_url(hass, f"{STRATEGY_URL}?v={version}")
