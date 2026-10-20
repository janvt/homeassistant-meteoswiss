"""Climate normals and records computed from a station's daily values."""

import datetime

from custom_components.meteoswiss import climate
from custom_components.meteoswiss.climate import Day

HEADER = (
    "station_abbr;reference_timestamp;tre200d0;tre200dx;tre200dn;rre150d0;sre000d0\n"
)


def days(start, end, high=lambda d: 10.0, **values):
    out = []
    date = start
    while date <= end:
        out.append(
            Day(
                date,
                values.get("mean", lambda d: 5.0)(date),
                high(date),
                values.get("low", lambda d: 0.0)(date),
                values.get("rain", lambda d: 2.0)(date),
                values.get("sunshine", lambda d: 120.0)(date),
            )
        )
        date += datetime.timedelta(days=1)
    return out


def test_parse_days():
    text = (
        HEADER
        + "ALT;20.10.2026 00:00;14.0;22.3;6.6;0.0;180\nALT;21.10.2026 00:00;;;;;\n"
    )
    parsed = climate.parse_days(text)
    assert parsed[0] == Day(datetime.date(2026, 10, 20), 14.0, 22.3, 6.6, 0.0, 180.0)
    assert parsed[1].high is None


def test_normals_are_from_1991_to_2020_and_a_week_either_side():
    history = days(
        datetime.date(1985, 1, 1),
        datetime.date(2025, 12, 31),
        # Warmer outside the normal period, and a warm spike on 20 October 2000.
        high=lambda d: (
            30.0
            if not 1991 <= d.year <= 2020
            else (40.0 if d == datetime.date(2000, 10, 20) else 10.0)
        ),
    )

    normals = climate.compute(history)

    # The spike is one of 15 days x 30 years.
    assert normals.normal_high[(10, 20)] == round((449 * 10 + 40) / 450, 1)
    assert normals.normal_high[(10, 28)] == 10.0  # more than a week away
    # Records are over all the years.
    assert normals.record_high[(10, 20)] == (40.0, 2000)
    assert normals.record_high[(1, 1)][0] == 30.0
    assert normals.first_year == 1985


def test_29_february():
    normals = climate.compute(
        days(datetime.date(1991, 1, 1), datetime.date(2020, 12, 31))
    )
    assert normals.normal_high[(2, 29)] == 10.0
    assert normals.record_high[(2, 29)][1] in (1992, 1996, 2000, 2004)


def test_month_normals_leave_out_incomplete_months():
    history = days(datetime.date(1991, 1, 1), datetime.date(2020, 12, 31)) + [
        # Only part of October 2021, much wetter: not in the period anyway.
        *days(
            datetime.date(2021, 10, 1), datetime.date(2021, 10, 5), rain=lambda d: 50.0
        )
    ]
    # A gap of 5 days in October 2000 makes that month incomplete.
    history = [
        d
        for d in history
        if not (d.date.year == 2000 and d.date.month == 10 and d.date.day <= 5)
    ]

    normals = climate.compute(history)

    assert normals.month_rain[10] == 62.0  # 31 days of 2 mm
    assert normals.month_mean[10] == 5.0
    assert normals.month_sunshine[10] == 62.0  # 31 x 2 hours


class TestMonthSoFar:
    NORMALS = climate.compute(
        days(datetime.date(1991, 1, 1), datetime.date(2020, 12, 31))
    )

    def test_against_the_normal_for_as_many_days(self):
        recent = days(
            datetime.date(2026, 10, 1),
            datetime.date(2026, 10, 19),
            mean=lambda d: 8.0,
            rain=lambda d: 1.0,
            sunshine=lambda d: 180.0,
        )

        so_far = climate.month_so_far(recent, self.NORMALS, datetime.date(2026, 10, 20))

        assert so_far.days == 19
        assert so_far.mean_anomaly == 3.0
        assert (so_far.rain, so_far.rain_normal, so_far.rain_percent) == (
            19.0,
            38.0,
            50,
        )
        assert so_far.sunshine == 57.0
        assert so_far.sunshine_percent == 150

    def test_nothing_on_the_first_of_the_month(self):
        recent = days(datetime.date(2026, 9, 1), datetime.date(2026, 9, 30))
        assert (
            climate.month_so_far(recent, self.NORMALS, datetime.date(2026, 10, 1))
            is None
        )

    def test_a_missing_value_leaves_that_comparison_out(self):
        recent = days(
            datetime.date(2026, 10, 1),
            datetime.date(2026, 10, 19),
            rain=lambda d: None if d.day == 5 else 1.0,
        )

        so_far = climate.month_so_far(recent, self.NORMALS, datetime.date(2026, 10, 20))

        assert so_far.rain_percent is None
        assert so_far.sunshine_percent is not None
