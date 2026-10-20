"""Climate normals and records of a station, from its daily measurements.

MeteoSwiss publishes its official 1991-2020 normals only as grids, so they
are computed here from the station's own daily values: the normal high and low
of a date are their mean over 1991-2020 within a week either side of it, and
a month's normals are the means of its complete months.  Daily values are for
UTC days, rain from 06:00 to 06:00 UTC.
"""

from __future__ import annotations

import calendar
import csv
import datetime
import io
import statistics
from dataclasses import dataclass, field

NORMAL_PERIOD = (1991, 2020)
# Days either side of a date for its normal high and low.
WINDOW = 7
# A month counts for its normals with at most this many days missing.
MAX_MISSING_DAYS = 1

# Every date of a year, including 29 February.
_LEAP = 2000
DATES = [datetime.date(_LEAP, 1, 1) + datetime.timedelta(days=i) for i in range(366)]


@dataclass(frozen=True)
class Day:
    date: datetime.date
    mean: float | None
    high: float | None
    low: float | None
    rain: float | None  # mm
    sunshine: float | None  # minutes


@dataclass
class Climate:
    """What a station's history says, by (month, day) and by month."""

    normal_high: dict[tuple[int, int], float] = field(default_factory=dict)
    normal_low: dict[tuple[int, int], float] = field(default_factory=dict)
    # The value and its year.
    record_high: dict[tuple[int, int], tuple[float, int]] = field(default_factory=dict)
    record_low: dict[tuple[int, int], tuple[float, int]] = field(default_factory=dict)
    month_mean: dict[int, float] = field(default_factory=dict)  # °C
    month_rain: dict[int, float] = field(default_factory=dict)  # mm
    month_sunshine: dict[int, float] = field(default_factory=dict)  # hours
    first_year: int | None = None


def _number(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "", "-") else None
    except ValueError:
        return None


def parse_days(text: str) -> list[Day]:
    """The daily values of a station file (ogd-smn_<abbr>_d_*.csv)."""
    days = []
    for row in csv.DictReader(io.StringIO(text), delimiter=";"):
        try:
            date = datetime.datetime.strptime(
                row["reference_timestamp"][:10], "%d.%m.%Y"
            ).date()
        except (KeyError, ValueError):
            continue
        days.append(
            Day(
                date,
                _number(row.get("tre200d0")),
                _number(row.get("tre200dx")),
                _number(row.get("tre200dn")),
                _number(row.get("rre150d0")),
                _number(row.get("sre000d0")),
            )
        )
    return days


_SLOTS = {(d.month, d.day): i for i, d in enumerate(DATES)}


def _slot(date: datetime.date) -> int:
    """The position of a date in DATES."""
    return _SLOTS[(date.month, date.day)]


def compute(days: list[Day]) -> Climate:
    """Normals over NORMAL_PERIOD and records over all the days."""
    climate = Climate()
    if not days:
        return climate
    climate.first_year = min(d.date.year for d in days)
    start, end = NORMAL_PERIOD
    highs: list[list[float]] = [[] for _ in DATES]
    lows: list[list[float]] = [[] for _ in DATES]
    by_month: dict[tuple[int, int], list[Day]] = {}
    for day in days:
        key = (day.date.month, day.date.day)
        slot = _slot(day.date)
        if day.high is not None:
            if key not in climate.record_high or day.high > climate.record_high[key][0]:
                climate.record_high[key] = (day.high, day.date.year)
        if day.low is not None:
            if key not in climate.record_low or day.low < climate.record_low[key][0]:
                climate.record_low[key] = (day.low, day.date.year)
        if start <= day.date.year <= end:
            if day.high is not None:
                highs[slot].append(day.high)
            if day.low is not None:
                lows[slot].append(day.low)
            by_month.setdefault((day.date.year, day.date.month), []).append(day)

    for i, date in enumerate(DATES):
        around = [(i + offset) % len(DATES) for offset in range(-WINDOW, WINDOW + 1)]
        high = [v for j in around for v in highs[j]]
        low = [v for j in around for v in lows[j]]
        if high:
            climate.normal_high[(date.month, date.day)] = round(
                statistics.mean(high), 1
            )
        if low:
            climate.normal_low[(date.month, date.day)] = round(statistics.mean(low), 1)

    means: dict[int, list[float]] = {}
    rains: dict[int, list[float]] = {}
    sunshine: dict[int, list[float]] = {}
    for (year, month), month_days in by_month.items():
        length = calendar.monthrange(year, month)[1]
        for values, key in ((means, "mean"), (rains, "rain"), (sunshine, "sunshine")):
            measured = [
                getattr(d, key) for d in month_days if getattr(d, key) is not None
            ]
            if len(measured) < length - MAX_MISSING_DAYS:
                continue
            total = statistics.mean(measured) if key == "mean" else sum(measured)
            values.setdefault(month, []).append(total)
    climate.month_mean = {m: round(statistics.mean(v), 1) for m, v in means.items()}
    climate.month_rain = {m: round(statistics.mean(v), 1) for m, v in rains.items()}
    climate.month_sunshine = {
        m: round(statistics.mean(v) / 60, 1) for m, v in sunshine.items()
    }
    return climate


@dataclass(frozen=True)
class MonthSoFar:
    """This month's complete days so far, against its normals for as many days."""

    days: int
    mean: float | None
    mean_anomaly: float | None  # °C
    rain: float | None  # mm
    rain_normal: float | None  # mm, for as many days
    rain_percent: float | None
    sunshine: float | None  # hours
    sunshine_normal: float | None
    sunshine_percent: float | None


def month_so_far(
    days: list[Day], climate: Climate, today: datetime.date
) -> MonthSoFar | None:
    """None on the first of a month, when there is nothing to compare yet."""
    month_days = [
        d
        for d in days
        if d.date.year == today.year and d.date.month == today.month and d.date < today
    ]
    if not month_days:
        return None
    share = len(month_days) / calendar.monthrange(today.year, today.month)[1]

    def against(key: str, normals: dict[int, float], scale: float = 1):
        measured = [getattr(d, key) for d in month_days if getattr(d, key) is not None]
        if len(measured) < len(month_days) or today.month not in normals:
            return None, None, None
        total = sum(measured) / scale
        normal = normals[today.month] * share
        percent = round(100 * total / normal) if normal else None
        return round(total, 1), round(normal, 1), percent

    means = [d.mean for d in month_days if d.mean is not None]
    mean = round(statistics.mean(means), 1) if len(means) == len(month_days) else None
    anomaly = (
        round(mean - climate.month_mean[today.month], 1)
        if mean is not None and today.month in climate.month_mean
        else None
    )
    rain = against("rain", climate.month_rain)
    sun = against("sunshine", climate.month_sunshine, scale=60)
    return MonthSoFar(len(month_days), mean, anomaly, *rain, *sun)
