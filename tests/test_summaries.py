"""The time windows and summaries behind the forecast summary sensors."""

import datetime

import pytest

from custom_components.meteoswiss import summaries

UTC = datetime.timezone.utc
HOUR = datetime.timedelta(hours=1)


def utc(*args) -> datetime.datetime:
    return datetime.datetime(*args, tzinfo=UTC)


def hours(start, end, **values):
    """Hourly forecasts from start to end (UTC); values are constants or f(time)."""
    out = []
    time = start
    while time < end:
        hour = {
            "time": time,
            "temperature": -2.0,
            "precipitation": 1.0,
            "precipitation_probability": 50.0,
            "wind_gust": 30.0,
            "sunshine": 30.0,
        }
        for key, value in values.items():
            hour[key] = value(time) if callable(value) else value
        out.append(hour)
        time += HOUR
    return out


def length(window) -> int:
    return (window[1] - window[0]) // HOUR


class TestTonight:
    def test_in_the_afternoon_it_is_the_coming_night(self):
        # 14:30 in Zurich (summer time, UTC+2).
        start, end = summaries.tonight(utc(2026, 10, 20, 12, 30))
        assert (start, end) == (utc(2026, 10, 20, 16), utc(2026, 10, 21, 6))

    def test_in_the_evening_it_starts_with_the_current_hour(self):
        # 21:15 local: the hours since 18:00 are no longer forecast.
        start, end = summaries.tonight(utc(2026, 10, 20, 19, 15))
        assert (start, end) == (utc(2026, 10, 20, 19), utc(2026, 10, 21, 6))

    def test_after_local_midnight_it_is_still_the_same_night(self):
        # 00:30 local on the 21st, while it is still the 20th in UTC.
        start, end = summaries.tonight(utc(2026, 10, 20, 22, 30))
        assert (start, end) == (utc(2026, 10, 20, 22), utc(2026, 10, 21, 6))

    def test_from_eight_in_the_morning_it_is_the_next_night(self):
        start, end = summaries.tonight(utc(2026, 10, 21, 6, 0))
        assert (start, end) == (utc(2026, 10, 21, 16), utc(2026, 10, 22, 6))

    def test_the_night_the_clocks_go_back_has_fifteen_hours(self):
        # 25 October 2026: 03:00 summer time becomes 02:00 winter time.
        window = summaries.tonight(utc(2026, 10, 24, 12))
        assert window == (utc(2026, 10, 24, 16), utc(2026, 10, 25, 7))
        assert length(window) == 15

    def test_the_night_the_clocks_go_forward_has_thirteen_hours(self):
        # 28 March 2027: 02:00 winter time becomes 03:00 summer time.
        window = summaries.tonight(utc(2027, 3, 27, 12))
        assert window == (utc(2027, 3, 27, 17), utc(2027, 3, 28, 6))
        assert length(window) == 13


class TestTomorrow:
    def test_whole_day(self):
        window = summaries.tomorrow(utc(2026, 10, 20, 12))
        assert window == (utc(2026, 10, 20, 22), utc(2026, 10, 21, 22))

    def test_tomorrow_follows_the_local_date(self):
        # 00:30 local on the 21st (still the 20th in UTC): tomorrow is the 22nd.
        window = summaries.tomorrow(utc(2026, 10, 20, 22, 30))
        assert window == (utc(2026, 10, 21, 22), utc(2026, 10, 22, 22))

    def test_midday(self):
        window = summaries.tomorrow(utc(2026, 10, 20, 12), *summaries.MIDDAY)
        assert window == (utc(2026, 10, 21, 7), utc(2026, 10, 21, 13))
        assert length(window) == 6

    def test_midday_in_winter_time(self):
        window = summaries.tomorrow(utc(2026, 10, 25, 12), *summaries.MIDDAY)
        assert window == (utc(2026, 10, 26, 8), utc(2026, 10, 26, 14))

    @pytest.mark.parametrize(
        ("now", "hours"),
        [
            (utc(2026, 10, 24, 12), 25),  # tomorrow the clocks go back
            (utc(2027, 3, 27, 12), 23),  # tomorrow the clocks go forward
            (utc(2026, 10, 20, 12), 24),
        ],
    )
    def test_days_when_the_clocks_change(self, now, hours):
        assert length(summaries.tomorrow(now)) == hours


def test_coming_hours_start_with_the_current_hour():
    assert summaries.coming_hours(utc(2026, 10, 20, 12, 30), 24) == (
        utc(2026, 10, 20, 12),
        utc(2026, 10, 21, 12),
    )


def test_next_hour_is_the_one_after_the_current_hour():
    assert summaries.next_hour(utc(2026, 10, 20, 12, 30)) == (
        utc(2026, 10, 20, 13),
        utc(2026, 10, 20, 14),
    )


class TestSnow:
    WINDOW = (utc(2026, 10, 20, 16), utc(2026, 10, 20, 20))

    def test_only_cold_hours_count(self):
        temperatures = {16: 3.0, 17: 0.6, 18: 0.5, 19: -4.0}
        hourly = hours(
            utc(2026, 10, 20, 12),
            utc(2026, 10, 21, 0),
            temperature=lambda t: temperatures.get(t.hour, 9.0),
            precipitation=lambda t: t.hour / 10,
        )
        # 18:00 (at 0.5 °C) and 19:00 count: 1.8 + 1.9 mm.
        assert summaries.snow(hourly, self.WINDOW) == 3.7

    def test_no_snow_when_warm(self):
        hourly = hours(utc(2026, 10, 20, 12), utc(2026, 10, 21), temperature=4.0)
        snow = summaries.snow(hourly, self.WINDOW)
        assert snow == 0.0
        assert isinstance(snow, float)  # shown as 0.0, not 0

    def test_dry_and_cold_is_no_snow(self):
        hourly = hours(utc(2026, 10, 20, 12), utc(2026, 10, 21), precipitation=0.0)
        assert summaries.snow(hourly, self.WINDOW) == 0.0

    def test_a_missing_hour_makes_it_unknown(self):
        hourly = [
            h
            for h in hours(utc(2026, 10, 20, 12), utc(2026, 10, 21))
            if h["time"] != utc(2026, 10, 20, 18)
        ]
        assert summaries.snow(hourly, self.WINDOW) is None

    def test_beyond_the_forecast_it_is_unknown(self):
        hourly = hours(utc(2026, 10, 20, 12), utc(2026, 10, 20, 18))
        assert summaries.snow(hourly, self.WINDOW) is None

    @pytest.mark.parametrize("key", ["temperature", "precipitation"])
    def test_a_missing_value_makes_it_unknown(self, key):
        hourly = hours(
            utc(2026, 10, 20, 12),
            utc(2026, 10, 21),
            **{key: lambda t: None if t.hour == 17 else 0.0},
        )
        assert summaries.snow(hourly, self.WINDOW) is None

    def test_without_hourly_temperatures_it_is_unknown(self):
        hourly = hours(utc(2026, 10, 20, 12), utc(2026, 10, 21), temperature=None)
        assert summaries.snow(hourly, self.WINDOW) is None

    def test_the_night_the_clocks_go_back(self):
        hourly = hours(utc(2026, 10, 24), utc(2026, 10, 26), precipitation=0.5)
        # 15 hours of 0.5 mm.
        assert summaries.snow(hourly, summaries.tonight(utc(2026, 10, 24, 12))) == 7.5


class TestOtherSummaries:
    DAY = summaries.tomorrow(utc(2026, 10, 20, 12), *summaries.MIDDAY)
    HOURLY = hours(
        utc(2026, 10, 20),
        utc(2026, 10, 23),
        sunshine=lambda t: 60.0 if t.hour < 10 else 15.0,
        wind_gust=lambda t: 20.0 + t.hour,
        temperature=lambda t: 5.0 - t.hour / 2,
    )

    def test_sunshine_in_hours(self):
        # 07-09 UTC fully sunny, 10-12 UTC a quarter: 3 h + 0.75 h.
        assert summaries.sunshine_hours(self.HOURLY, self.DAY) == 3.8

    def test_sunshine_over_a_whole_day(self):
        window = summaries.tomorrow(utc(2026, 10, 20, 12))
        # 22:00 UTC on the 20th to 22:00 UTC on the 21st: the 10 hours from
        # 00:00 to 09:00 UTC are sunny, the other 14 have a quarter of an hour.
        assert summaries.sunshine_hours(self.HOURLY, window) == 10 + 14 / 4

    def test_strongest_gust(self):
        assert summaries.max_gust(self.HOURLY, self.DAY) == 32.0

    def test_lowest_temperature_tonight(self):
        window = summaries.tonight(utc(2026, 10, 20, 12))
        # The coldest hour of 16:00-05:00 UTC is 23:00 UTC.
        assert summaries.min_temperature(self.HOURLY, window) == -6.5

    def test_next_hour(self):
        hourly = hours(
            utc(2026, 10, 20),
            utc(2026, 10, 21),
            precipitation=lambda t: t.hour / 10,
            precipitation_probability=lambda t: t.hour * 2.0,
        )
        window = summaries.next_hour(utc(2026, 10, 20, 12, 30))
        assert summaries.precipitation(hourly, window) == 1.3
        assert summaries.precipitation_probability(hourly, window) == 26.0

    def test_missing_sunshine_makes_it_unknown(self):
        hourly = hours(
            utc(2026, 10, 20),
            utc(2026, 10, 23),
            sunshine=lambda t: None if t == utc(2026, 10, 21, 8) else 60.0,
        )
        assert summaries.sunshine_hours(hourly, self.DAY) is None


class TestToday:
    def test_is_the_local_day(self):
        # 00:30 local on the 21st (UTC+2), while it is still the 20th in UTC.
        start, end = summaries.today(utc(2026, 10, 20, 22, 30))
        assert (start, end) == (utc(2026, 10, 20, 22), utc(2026, 10, 21, 22))

    def test_sunshine_today_counts_the_whole_day(self):
        hourly = hours(utc(2026, 10, 19, 22), utc(2026, 10, 21, 22))
        window = summaries.today(utc(2026, 10, 20, 15))
        assert summaries.sunshine_hours(hourly, window) == 12.0

    def test_day_of(self):
        daily = [{"date": "2026-10-20"}, {"date": "2026-10-21"}]
        assert summaries.day_of(daily, utc(2026, 10, 20, 22, 30)) == daily[1]
        assert summaries.day_of(daily, utc(2026, 10, 23, 12)) is None


class TestLowestHour:
    def test_the_first_lowest_hour(self):
        levels = {13: 1800.0, 20: 1500.0, 30: 1500.0}
        hourly = hours(
            utc(2026, 10, 20, 12),
            utc(2026, 10, 22, 12),
            freezing_level=lambda t: levels.get(t.hour + (t.day - 20) * 24, 2500.0),
        )
        window = summaries.coming_hours(utc(2026, 10, 20, 12, 30), 48)
        lowest = summaries.lowest_hour(hourly, window, "freezing_level")
        assert lowest["freezing_level"] == 1500.0
        assert lowest["time"] == utc(2026, 10, 20, 20)

    def test_none_when_an_hour_is_missing(self):
        hourly = hours(
            utc(2026, 10, 20, 12), utc(2026, 10, 21, 12), freezing_level=2500.0
        )
        window = summaries.coming_hours(utc(2026, 10, 20, 12), 48)
        assert summaries.lowest_hour(hourly, window, "freezing_level") is None
