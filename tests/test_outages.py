"""Logging each outage once, and its end once, with a repair while it lasts."""

import logging

from homeassistant.helpers import issue_registry as ir

from custom_components.meteoswiss.const import DOMAIN
from custom_components.meteoswiss.outages import ERROR_LENGTH, Outages

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


async def test_an_outage_is_a_repair_until_it_ends(hass):
    outages = Outages(LOGGER, hass, place="Zürich", owner="entry")

    outages.failed("Station SMA", "500, message='Internal Server Error'")
    outages.failed("Station SMA", "still down")

    (issue,) = repairs(hass).values()
    assert issue.translation_key == "source_unavailable"
    assert issue.translation_placeholders == {
        "source": "Station SMA (Zürich)",
        "error": "500, message='Internal Server Error'",
    }
    assert issue.severity is ir.IssueSeverity.WARNING
    assert not issue.is_persistent
    outages.worked("Station SMA")
    assert not repairs(hass)


async def test_entries_have_repairs_of_their_own(hass):
    one = Outages(LOGGER, hass, place="Zürich", owner="one")
    other = Outages(LOGGER, hass, place="Zürich", owner="other")

    one.failed("Forecast", "down")
    other.failed("Forecast", "down")
    one.worked("Forecast")

    assert list(repairs(hass)) == [other.issue_id("Forecast")]


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


async def test_clearing_ends_every_outage_and_its_repair(hass, caplog):
    caplog.set_level(logging.WARNING, LOGGER.name)
    outages = Outages(LOGGER, hass)
    outages.failed("Station SMA", "down")
    outages.failed("Station ABE", "down")

    outages.clear()

    assert not repairs(hass)
    caplog.clear()
    outages.failed("Station SMA", "down")
    assert messages(caplog) == [(logging.WARNING, "Station SMA is unavailable: down")]


async def test_a_long_error_is_cut_short(hass):
    Outages(LOGGER, hass).failed("Station SMA", "x" * 1000)

    (issue,) = repairs(hass).values()
    assert len(issue.translation_placeholders["error"]) == ERROR_LENGTH
