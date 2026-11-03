"""Logging each outage once, and its end once."""

import logging

from custom_components.meteoswiss.outages import Outages

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
