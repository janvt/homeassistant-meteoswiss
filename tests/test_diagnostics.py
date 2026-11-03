"""The diagnostics download of an entry."""

import contextlib
from http import HTTPStatus

import pytest
from homeassistant.setup import async_setup_component

from custom_components.meteoswiss.const import DOMAIN
from custom_components.meteoswiss.downloads import SharedDownloads
from custom_components.meteoswiss.ogd import MeteoSwissOGD

from .fakes import FakeOGD, FakeSession
from .harness import MockConfigEntry

pytestmark = pytest.mark.usefixtures("fake_ogd", "frozen_now")

# Imported before the time is frozen: the download imports it, and with the
# time frozen it fails on Home Assistant 2024.12. The test harness of newer
# versions does not install it.
with contextlib.suppress(ImportError):
    import aiohasupervisor  # noqa: F401


async def get_diagnostics_for_config_entry(hass, hass_client, entry):
    """The download, as the test harness's own helper does, which the harness
    for older Home Assistant versions lacks."""
    assert await async_setup_component(hass, "diagnostics", {})
    await hass.async_block_till_done()
    client = await hass_client()
    response = await client.get(f"/api/diagnostics/config_entry/{entry.entry_id}")
    assert response.status == HTTPStatus.OK
    return (await response.json())["data"]


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
