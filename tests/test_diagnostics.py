"""The diagnostics download of an entry."""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.meteoswiss.const import DOMAIN
from custom_components.meteoswiss.downloads import SharedDownloads
from custom_components.meteoswiss.ogd import MeteoSwissOGD

from .fakes import FakeOGD, FakeSession

pytestmark = pytest.mark.usefixtures("fake_ogd", "frozen_now")


async def test_diagnostics(hass, hass_client, entry_data):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**entry_data, "latitude": 47.38, "longitude": 8.54},
        options={"radar": True},
        title="Zurich",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    # Where the user lives is not in it.
    assert diagnostics["entry"]["data"]["postcode"] == "**REDACTED**"
    assert diagnostics["entry"]["data"]["latitude"] == "**REDACTED**"
    assert diagnostics["entry"]["data"]["longitude"] == "**REDACTED**"
    assert diagnostics["radar"]["location"] == "**REDACTED**"
    assert "47.38" not in str(diagnostics)
    # What is needed to make sense of a problem is.
    assert diagnostics["entry"]["data"]["station"] == "SMA"
    assert diagnostics["coordinator"]["last_update_success"] is True
    assert diagnostics["point"]["point_id"] == 800500
    assert diagnostics["forecast"]["run"] == FakeOGD.run
    assert diagnostics["forecast"]["days"] > 0
    assert diagnostics["stations"]["SMA"]["tre200s0"] == 13.4
    assert diagnostics["client"] == {"downloaded_last_24h": 0}
    assert diagnostics["shared_downloads"]["entries"] == 1


def test_shared_downloads_diagnostics():
    downloads = SharedDownloads(None)
    assert downloads.diagnostics() == {"entries": 0, "parameters": {}}


def test_client_diagnostics():
    client = MeteoSwissOGD(FakeSession({}), "test")
    assert client.diagnostics() == {
        "downloaded_last_24h": 0,
        "whole_downloads_last_24h": 0,
        "ranges_ignored": False,
        "latest_run": None,
        "files": {},
        "ordered_by_time": [],
        "point_orders": {},
    }
