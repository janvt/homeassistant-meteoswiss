"""Logging each outage once, and its end once, with a repair while it lasts."""

import logging

from homeassistant.helpers import issue_registry as ir

from custom_components.meteoswiss.const import DOMAIN
from custom_components.meteoswiss.outages import (
    ERROR_LENGTH,
    METEOALARM,
    METEOSWISS,
    SERVICES,
    SOURCES_LENGTH,
    SWISSTOPO,
    Outages,
    issue_id,
)

LOGGER = logging.getLogger("tests.outages")


def messages(caplog):
    return [(r.levelno, r.getMessage()) for r in caplog.records]


def test_a_failure_is_a_warning_once_then_debug(caplog):
    caplog.set_level(logging.DEBUG, LOGGER.name)
    outages = Outages(LOGGER)

    outages.failed("Station SMA", "down")
    outages.failed("Station SMA", "still down")

    assert messages(caplog) == [
        (logging.WARNING, "Station SMA is unavailable: down"),
        (logging.DEBUG, "Station SMA is still unavailable: still down"),
    ]


def test_the_end_of_an_outage_is_logged_once(caplog):
    caplog.set_level(logging.DEBUG, LOGGER.name)
    outages = Outages(LOGGER)
    outages.failed("Station SMA", "down")
    caplog.clear()

    outages.worked("Station SMA")
    outages.worked("Station SMA")

    assert messages(caplog) == [(logging.INFO, "Station SMA is available again")]


def test_working_without_an_outage_logs_nothing(caplog):
    caplog.set_level(logging.DEBUG, LOGGER.name)

    Outages(LOGGER).worked("Station SMA")

    assert not caplog.records


def test_sources_are_apart(caplog):
    caplog.set_level(logging.INFO, LOGGER.name)
    outages = Outages(LOGGER)

    outages.failed("Station SMA", "down")
    outages.failed("Station ABE", "down")
    outages.worked("Station ABE")
    outages.failed("Station SMA", "down")

    assert messages(caplog) == [
        (logging.WARNING, "Station SMA is unavailable: down"),
        (logging.WARNING, "Station ABE is unavailable: down"),
        (logging.INFO, "Station ABE is available again"),
    ]


def test_an_outage_after_the_end_of_one_is_logged_again(caplog):
    caplog.set_level(logging.INFO, LOGGER.name)
    outages = Outages(LOGGER)

    outages.failed("Station SMA", "down")
    outages.worked("Station SMA")
    outages.failed("Station SMA", "down again")

    assert messages(caplog) == [
        (logging.WARNING, "Station SMA is unavailable: down"),
        (logging.INFO, "Station SMA is available again"),
        (logging.WARNING, "Station SMA is unavailable: down again"),
    ]


def test_trace_keeps_the_exception(caplog):
    outages = Outages(LOGGER)

    try:
        raise RuntimeError("bug")
    except RuntimeError as exc:
        outages.failed("Forecast", exc, trace=True)

    (record,) = caplog.records
    assert record.exc_info[1].args == ("bug",)


def repairs(hass):
    return {
        issue_id: issue
        for (domain, issue_id), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN
    }


def sources(hass, service=METEOSWISS):
    """The sources listed in a service's repair."""
    return repairs(hass)[issue_id(service)].translation_placeholders["sources"]


async def test_an_outage_is_a_repair_until_it_ends(hass):
    outages = Outages(LOGGER, hass, place="Zürich", owner="entry")

    outages.failed("Station SMA", "500, message='Internal Server Error'")
    outages.failed("Station SMA", "still down")

    (issue,) = repairs(hass).values()
    assert issue.translation_key == "service_unavailable"
    assert issue.translation_placeholders == {
        "service": "MeteoSwiss (data.geo.admin.ch)",
        "sources": "Station SMA (Zürich)",
        "error": "500, message='Internal Server Error'",
    }
    assert issue.severity is ir.IssueSeverity.WARNING
    assert not issue.is_persistent
    outages.worked("Station SMA")
    assert not repairs(hass)


async def test_a_server_down_is_one_repair_listing_what_failed(hass):
    zurich = Outages(LOGGER, hass, place="Zürich", owner="one")
    bern = Outages(LOGGER, hass, place="Bern", owner="two")
    shared = Outages(LOGGER, hass)

    zurich.failed("Station SMA", "down")
    zurich.failed("Forecast", "down")
    bern.failed("Forecast", "timed out")
    shared.failed("Radar rain_rate", "down")

    assert list(repairs(hass)) == [issue_id(METEOSWISS)]
    assert sources(hass) == (
        "Forecast (Bern), Forecast (Zürich), Radar rain_rate, Station SMA (Zürich)"
    )
    # The last error.
    assert repairs(hass)[issue_id(METEOSWISS)].translation_placeholders == {
        "service": "MeteoSwiss (data.geo.admin.ch)",
        "sources": sources(hass),
        "error": "down",
    }


async def test_the_repair_stays_until_every_source_works(hass):
    zurich = Outages(LOGGER, hass, place="Zürich", owner="one")
    bern = Outages(LOGGER, hass, place="Bern", owner="two")
    zurich.failed("Forecast", "down")
    bern.failed("Forecast", "down")

    zurich.worked("Forecast")
    assert sources(hass) == "Forecast (Bern)"
    bern.worked("Forecast")
    assert not repairs(hass)


async def test_each_service_has_a_repair_of_its_own(hass):
    Outages(LOGGER, hass).failed("Station SMA", "down")
    Outages(LOGGER, hass, service=METEOALARM).failed("Weather warning feed", "down")
    warnings = Outages(LOGGER, hass, place="Zürich")
    warnings.failed("Weather warnings", "down", service=METEOALARM)
    Outages(LOGGER, hass, service=SWISSTOPO).failed("Map of the radar loop", "down")

    assert set(repairs(hass)) == {issue_id(s) for s in SERVICES}
    assert sources(hass, METEOALARM) == (
        "Weather warning feed, Weather warnings (Zürich)"
    )
    warnings.worked("Weather warnings", service=METEOALARM)
    assert sources(hass, METEOALARM) == "Weather warning feed"


async def test_a_failure_with_a_repair_of_its_own_is_only_logged(hass, caplog):
    outages = Outages(LOGGER, hass)

    outages.failed("Station SMA", "no real-time data", repair=False)

    assert not repairs(hass)
    assert messages(caplog) == [
        (logging.WARNING, "Station SMA is unavailable: no real-time data")
    ]


async def test_a_failure_logged_elsewhere_is_only_a_repair(hass, caplog):
    outages = Outages(LOGGER, hass)

    outages.failed("Forecast", "down", log=False)

    assert len(repairs(hass)) == 1
    assert not caplog.records


async def test_a_repair_from_a_setup_that_failed_goes_when_it_works(hass):
    Outages(LOGGER, hass, owner="entry").failed("Forecast", "down")

    # The entry's setup is retried, with a new coordinator.
    Outages(LOGGER, hass, owner="entry").worked("Forecast")

    assert not repairs(hass)


async def test_clearing_ends_every_outage_but_not_those_of_others(hass, caplog):
    caplog.set_level(logging.WARNING, LOGGER.name)
    outages = Outages(LOGGER, hass, owner="one")
    outages.failed("Station SMA", "down")
    outages.failed("Station ABE", "down")
    Outages(LOGGER, hass, owner="two").failed("Station SMA", "down")

    outages.clear()

    assert sources(hass) == "Station SMA"
    caplog.clear()
    outages.failed("Station SMA", "down")
    assert messages(caplog) == [(logging.WARNING, "Station SMA is unavailable: down")]


async def test_a_long_error_and_list_are_cut_short(hass):
    outages = Outages(LOGGER, hass)
    for station in range(200):
        outages.failed(f"Station {station:03}", "x" * 1000)

    placeholders = repairs(hass)[issue_id(METEOSWISS)].translation_placeholders
    assert len(placeholders["error"]) == ERROR_LENGTH
    assert len(placeholders["sources"]) == SOURCES_LENGTH
    assert placeholders["sources"].endswith("…")
