"""Summaries of the hourly forecast over local time windows, for sensors.

A window holds the hours that start in it, so 18:00-08:00 is the hours from
18:00 to 07:00.  A summary is None (unknown) when any hour of its window, or a
value it needs, is missing, rather than a sum or extreme of part of it.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable

from custom_components.meteoswiss.ogd import (
    LOCAL_TIME_ZONE,
    DailyForecast,
    HourlyForecast,
)

HOUR = datetime.timedelta(hours=1)

# Precipitation falls as snow in hours this cold or colder (°C).
SNOW_TEMPERATURE = 0.5
NIGHT = (datetime.time(18), datetime.time(8))
MIDDAY = (datetime.time(9), datetime.time(15))

Window = tuple[datetime.datetime, datetime.datetime]  # UTC, end excluded


def _hour_start(now: datetime.datetime) -> datetime.datetime:
    return now.astimezone(datetime.timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )


def _local(day: datetime.date, at: datetime.time) -> datetime.datetime:
    """A local time as UTC; zoneinfo takes care of summer and winter time."""
    return datetime.datetime.combine(day, at, LOCAL_TIME_ZONE).astimezone(
        datetime.timezone.utc
    )


def tonight(now: datetime.datetime) -> Window:
    """The night (18:00-08:00) that has not ended yet, from the current hour on.

    Before 08:00 that is the night under way; hours already past are left out,
    as the forecast does not go back that far.
    """
    today = now.astimezone(LOCAL_TIME_ZONE).date()
    if now.astimezone(LOCAL_TIME_ZONE).time() < NIGHT[1]:
        start = _local(today - datetime.timedelta(days=1), NIGHT[0])
        end = _local(today, NIGHT[1])
    else:
        start = _local(today, NIGHT[0])
        end = _local(today + datetime.timedelta(days=1), NIGHT[1])
    return max(start, _hour_start(now)), end


def today(now: datetime.datetime) -> Window:
    """Today, from midnight: the forecast starts at the local midnight before
    its run."""
    day = now.astimezone(LOCAL_TIME_ZONE).date()
    return _local(day, datetime.time(0)), _local(
        day + datetime.timedelta(days=1), datetime.time(0)
    )


def tomorrow(
    now: datetime.datetime,
    start: datetime.time = datetime.time(0),
    end: datetime.time | None = None,
) -> Window:
    """Tomorrow, or part of it (end None meaning midnight)."""
    day = now.astimezone(LOCAL_TIME_ZONE).date() + datetime.timedelta(days=1)
    return (
        _local(day, start),
        _local(day + datetime.timedelta(days=1), datetime.time(0))
        if end is None
        else _local(day, end),
    )


def coming_hours(now: datetime.datetime, hours: int) -> Window:
    """From the start of the current hour on."""
    start = _hour_start(now)
    return start, start + hours * HOUR


def next_hour(now: datetime.datetime) -> Window:
    """The hour after the current one."""
    start = _hour_start(now) + HOUR
    return start, start + HOUR


def hours_in(hourly: list[HourlyForecast], window: Window) -> list[HourlyForecast]:
    """The hours of a window, or none at all if any is missing."""
    start, end = window
    found = [h for h in hourly if start <= h["time"] < end]
    if len({h["time"] for h in found}) != (end - start) // HOUR:
        return []
    return found


def _values(hours: list[HourlyForecast], key: str) -> list[float] | None:
    values = [h[key] for h in hours]  # type: ignore[literal-required]
    if not values or any(v is None for v in values):
        return None
    return values  # type: ignore[return-value]


def snow(hourly: list[HourlyForecast], window: Window) -> float | None:
    """Precipitation in the hours at or below SNOW_TEMPERATURE, in mm of water."""
    hours = hours_in(hourly, window)
    temperatures = _values(hours, "temperature")
    precipitation = _values(hours, "precipitation")
    if temperatures is None or precipitation is None:
        return None
    return round(
        sum(
            (p for t, p in zip(temperatures, precipitation) if t <= SNOW_TEMPERATURE),
            0.0,
        ),
        1,
    )


def _summarise(
    key: str, combine: Callable[[list[float]], float]
) -> Callable[[list[HourlyForecast], Window], float | None]:
    def summary(hourly: list[HourlyForecast], window: Window) -> float | None:
        values = _values(hours_in(hourly, window), key)
        return None if values is None else combine(values)

    return summary


sunshine_hours = _summarise("sunshine", lambda minutes: round(sum(minutes) / 60, 1))
max_gust = _summarise("wind_gust", max)
min_temperature = _summarise("temperature", min)
precipitation = _summarise("precipitation", sum)
precipitation_probability = _summarise("precipitation_probability", max)


def lowest_hour(
    hourly: list[HourlyForecast], window: Window, key: str
) -> HourlyForecast | None:
    """The hour of a window with the lowest value, the first if several."""
    hours = hours_in(hourly, window)
    if _values(hours, key) is None:
        return None
    return min(hours, key=lambda h: h[key])  # type: ignore[literal-required]


def day_of(daily: list[DailyForecast], now: datetime.datetime) -> DailyForecast | None:
    """Today's daily forecast (local date)."""
    date = now.astimezone(LOCAL_TIME_ZONE).date().isoformat()
    return next((d for d in daily if d["date"] == date), None)
