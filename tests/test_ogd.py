"""Tests of the OGD client against an in-memory fake of the MeteoSwiss servers."""

import datetime

import pytest

from custom_components.meteoswiss import ogd
from custom_components.meteoswiss.ogd import (
    MeteoSwissOGD,
    OGDError,
    Point,
    StationType,
)

from .fakes import (
    FakeSession,
    point_major_csv,
    time_major_csv,
)

UTC = datetime.timezone.utc
RUN = "202610201200"
# 40 points so the files are far larger than the first 4 kB that is probed.
BIG_POINTS = [(i * 7 + 1, 2) for i in range(40)] + [(i * 7 + 2, 1) for i in range(40)]
HOURS = [
    (
        datetime.datetime(2026, 10, 20, tzinfo=UTC) + datetime.timedelta(hours=h)
    ).strftime("%Y%m%d%H00")
    for h in range(217)
]
DAYS = [f"202610{20 + d}0000" for d in range(9)]


def url(param: str, run: str = RUN) -> str:
    return MeteoSwissOGD._run_url(run, param)


def value(point_id: int, ts: str) -> str:
    return f"{point_id}:{ts[-6:]}"


def make_client(files: dict[str, bytes]) -> tuple[MeteoSwissOGD, FakeSession]:
    session = FakeSession(files)
    return MeteoSwissOGD(session, "tests"), session  # type: ignore[arg-type]


def forecast_files() -> dict[str, bytes]:
    pts = [(800500, 2), (100025, 2)]
    files = {}
    for kind, params, stamps in (
        ("daily", ogd.DAILY_PARAMETERS, DAYS),
        ("hourly", ogd.HOURLY_PARAMETERS, HOURS),
    ):
        for p in params.values():
            files[url(p)] = point_major_csv(p, pts, stamps, lambda i, ts: "5")
    # Daily temperatures are published ordered by time, not by point.
    for p, v in (("tre200pn", "4.5"), ("tre200px", "12.5")):
        files[url(p)] = time_major_csv(p, pts, DAYS, lambda i, ts, v=v: v)
    files[url("jp2000d0")] = point_major_csv("jp2000d0", pts, DAYS, lambda i, ts: "3")
    files[url("jww003i0")] = point_major_csv(
        "jww003i0", pts, HOURS, lambda i, ts: "101" if ts[8:10] < "06" else "1"
    )
    files[url("rp0003i0")] = point_major_csv(
        "rp0003i0", pts, HOURS, lambda i, ts: RAIN_PROBABILITY.get(ts, "0")
    )
    # Hourly temperatures are ordered by time and start a few hours earlier.
    files[url("tre200h0")] = time_major_csv(
        "tre200h0", pts, ["202610192200", *HOURS], lambda i, ts: f"{i % 7}.{ts[9]}"
    )
    return files


# By the end of the hour, in UTC; Zurich is UTC+2 until 25 October, then UTC+1.
RAIN_PROBABILITY = {
    "202610201200": "30",
    "202610202200": "40",  # 21:00-22:00 UTC is 23:00 local on the 20th
    "202610202300": "90",  # 22:00-23:00 UTC is midnight local, the 21st
    "202610252300": "70",  # 22:00-23:00 UTC is 23:00 local (winter time), the 25th
    "202610260000": "80",  # 23:00 UTC is midnight local, the 26th
}


@pytest.mark.parametrize(
    ("text", "expected"),
    [("12.5", 12.5), ("0", 0.0), ("", None), ("-", None), (" 3 ", 3.0), ("x", None)],
)
def test_number(text, expected):
    assert ogd._number(text) == expected


def test_key():
    assert ogd._key("12;2;202610200000;5") == (12, 2)
    assert ogd._key(b"12;2;2026") == (12, 2)
    assert ogd._key("point_id;point_type_id;Date;x") is None
    assert ogd._key("") is None


def test_distance_and_nearest():
    zurich = Point(1, 2, "Zürich", "8001", 47.3769, 8.5417)
    geneva = Point(2, 2, "Genève", "1201", 46.2044, 6.1432)
    assert ogd.distance_km(47.3769, 8.5417, 46.2044, 6.1432) == pytest.approx(
        224, abs=3
    )
    assert ogd.nearest([geneva, zurich], 47.4, 8.5) == zurich
    assert ogd.nearest([], 47.4, 8.5) is None


class TestPointValues:
    @pytest.mark.parametrize("target", [BIG_POINTS[0], BIG_POINTS[37], BIG_POINTS[-1]])
    async def test_point_major_uses_ranges_and_matches_full_parse(self, target):
        body = point_major_csv("p", BIG_POINTS, HOURS, value)
        client, session = make_client({url("p"): body})
        point = Point(target[0], target[1], "x", "", 0, 0)

        result = await client._point_values(url("p"), point)

        expected = ogd._rows_with_prefix(body.decode(), f"{target[0]};{target[1]};")
        assert len(expected) == 217
        assert result == expected
        # Far less than the whole file was downloaded, and no full GET was made.
        assert session.ranged_bytes < len(body) / 10
        assert all(r is not None for _, _, r in session.requests)

    async def test_point_not_in_file(self):
        body = point_major_csv("p", BIG_POINTS, HOURS, value)
        client, _ = make_client({url("p"): body})
        assert await client._point_values(url("p"), Point(5, 2, "x", "", 0, 0)) == {}

    async def test_time_major_file_is_downloaded_whole(self):
        body = time_major_csv("p", BIG_POINTS, HOURS[:50], value)
        client, session = make_client({url("p"): body})
        point = Point(BIG_POINTS[3][0], 2, "x", "", 0, 0)

        result = await client._point_values(url("p"), point)

        assert len(result) == 50
        assert result[HOURS[0]] == value(point.point_id, HOURS[0])
        assert ("GET", url("p"), None) in session.requests

    @pytest.mark.parametrize("chunk_size", [7, 1000, 1 << 16])
    @pytest.mark.parametrize("target", [BIG_POINTS[0], BIG_POINTS[-1]])
    async def test_streaming_handles_any_chunking(self, chunk_size, target):
        body = time_major_csv("p", BIG_POINTS, HOURS[:50], value)
        # The last row may lack a line break.
        files = {url("p"): body, url("q"): body.rstrip(b"\n")}
        client, _ = make_client(files)
        client._session.chunk_size = chunk_size
        point = Point(target[0], target[1], "x", "", 0, 0)
        expected = ogd._rows_with_prefix(body.decode(), f"{target[0]};{target[1]};")

        assert await client._point_values(url("p"), point) == expected
        assert await client._point_values(url("q"), point) == expected
        assert len(expected) == 50

    async def test_streaming_does_not_match_inside_a_row(self):
        # Point 1;2 must not match the row of point 11;2 or 21;2.
        body = time_major_csv("p", [(1, 2), (11, 2), (21, 2)] * 1, HOURS[:200], value)
        client, _ = make_client({url("p"): body})
        result = await client._point_values(url("p"), Point(1, 2, "x", "", 0, 0))
        assert set(result.values()) == {value(1, ts) for ts in HOURS[:200]}

    async def test_small_file_needs_one_request(self):
        body = point_major_csv("p", [(1, 1), (2, 1)], DAYS, value)
        client, session = make_client({url("p"): body})
        result = await client._point_values(url("p"), Point(2, 1, "x", "", 0, 0))
        assert len(result) == 9
        assert len(session.requests) == 1

    async def test_missing_file(self):
        client, _ = make_client({})
        with pytest.raises(OGDError, match="403"):
            await client._point_values(url("p"), Point(1, 1, "x", "", 0, 0))


class TestLatestRun:
    async def test_current_hour(self):
        client, _ = make_client({url("jp2000d0"): b"x"})
        now = datetime.datetime(2026, 10, 20, 12, 40, tzinfo=UTC)
        assert await client.latest_run(now) == RUN

    async def test_falls_back_to_previous_hours_across_midnight(self):
        previous = "202610192300"
        client, session = make_client({url("jp2000d0", previous): b"x"})
        now = datetime.datetime(2026, 10, 20, 0, 5, tzinfo=UTC)
        assert await client.latest_run(now) == previous
        assert [u for _, u, _ in session.requests] == [
            url("jp2000d0", "202610200000"),
            url("jp2000d0", previous),
        ]

    async def test_nothing_published(self):
        client, _ = make_client({})
        with pytest.raises(OGDError, match="No forecast run"):
            await client.latest_run(datetime.datetime(2026, 10, 20, 12, tzinfo=UTC))


class TestGetForecast:
    async def test_assembles_daily_and_hourly(self):
        client, _ = make_client(forecast_files())
        point = Point(800500, 2, "Zürich", "8005", 47.39, 8.52)

        forecast = await client.get_forecast(point, RUN)

        assert forecast["run"] == RUN
        assert len(forecast["daily"]) == 9
        assert forecast["daily"][0] == {
            "date": "2026-10-20",
            "temperature_min": 4.5,
            "temperature_max": 12.5,
            "precipitation": 5.0,
            "precipitation_probability": 40.0,
            "symbol": 3,
        }
        assert len(forecast["hourly"]) == 217
        # Timestamps mark the end of an interval: 01:00 is the hour starting 00:00.
        first = forecast["hourly"][0]
        assert first["time"] == datetime.datetime(2026, 10, 19, 23, tzinfo=UTC)
        assert first["symbol"] == 101
        assert forecast["hourly"][10]["symbol"] == 1
        assert first["wind_speed"] == 5.0
        assert first["temperature"] is None  # not requested
        noon = forecast["hourly"][12]
        assert noon["time"] == datetime.datetime(2026, 10, 20, 11, tzinfo=UTC)
        assert noon["precipitation_probability"] == 30.0

    async def test_daily_chance_of_rain_follows_the_local_day(self):
        client, _ = make_client(forecast_files())
        point = Point(800500, 2, "Zürich", "8005", 47.39, 8.52)

        forecast = await client.get_forecast(point, RUN)

        by_day = {d["date"]: d["precipitation_probability"] for d in forecast["daily"]}
        assert by_day["2026-10-20"] == 40.0
        assert by_day["2026-10-21"] == 90.0
        assert by_day["2026-10-22"] == 0.0
        assert by_day["2026-10-25"] == 70.0
        assert by_day["2026-10-26"] == 80.0

    async def test_hourly_temperature(self):
        client, session = make_client(forecast_files())
        point = Point(100025, 2, "Lausanne 25", "1000", 46.56, 6.68)

        temperature = await client.get_hourly_temperature(point, RUN)
        forecast = await client.get_forecast(point, RUN, temperature)

        # The whole file was streamed, as it is ordered by time.
        assert ("GET", url("tre200h0"), None) in session.requests
        assert len(temperature) == 218
        hours = {h["time"]: h for h in forecast["hourly"]}
        # The earlier hours from the temperature file are kept too.
        early = hours[datetime.datetime(2026, 10, 19, 21, tzinfo=UTC)]
        assert early["temperature"] == float(f"{100025 % 7}.2")
        assert early["symbol"] is None
        assert hours[datetime.datetime(2026, 10, 20, 11, tzinfo=UTC)][
            "temperature"
        ] == float(f"{100025 % 7}.2")  # the row ending 12:00

    async def test_unknown_point_is_an_error(self):
        client, _ = make_client(forecast_files())
        with pytest.raises(OGDError, match="no data"):
            await client.get_forecast(Point(1, 1, "x", "", 0, 0), RUN)

    async def test_missing_file_is_an_error(self):
        files = forecast_files()
        del files[url("rre150h0")]
        client, _ = make_client(files)
        with pytest.raises(OGDError):
            await client.get_forecast(Point(800500, 2, "x", "", 0, 0), RUN)


class TestMetadata:
    POINTS_CSV = (
        "point_id;point_type_id;station_abbr;postal_code;point_name;"
        "point_type_de;point_type_fr;point_type_it;point_type_en;"
        "point_height_masl;point_coordinates_lv95_east;point_coordinates_lv95_north;"
        "point_coordinates_wgs84_lat;point_coordinates_wgs84_lon\n"
        "1;1;ARO;;Arosa;S;S;S;S;1878.0;1;1;46.792661;9.679014\n"
        "100025;2;;1000;Lausanne 25;P;P;P;P;837.0;1;1;46.557622;6.683067\n"
        "100026;2;;1000;Lausanne 26;P;P;P;P;849.0;1;1;46.556072;6.696247\n"
        "800500;2;;8005;Zürich;P;P;P;P;404.0;1;1;47.387475;8.520992\n"
        "437;3;;;Glattalphütte SAC;P;P;P;P;1;1;1;47.0;9.0\n"
    )

    def client(self):
        return make_client({ogd.POINTS_URL: self.POINTS_CSV.encode("latin-1")})[0]

    async def test_postcode_points_skip_stations_and_mountains(self):
        points = await self.client().get_postcode_points(1000)
        assert [p.point_id for p in points] == [100025, 100026]

    async def test_resolve_point_defaults_to_the_first(self):
        assert (await self.client().resolve_point("1000")).point_id == 100025

    async def test_resolve_point_honours_the_configured_point(self):
        assert (await self.client().resolve_point(1000, 100026)).point_id == 100026

    async def test_resolve_point_ignores_a_point_of_another_postcode(self):
        assert (await self.client().resolve_point(1000, 800500)).point_id == 100025

    async def test_unknown_postcode(self):
        with pytest.raises(OGDError, match="9999"):
            await self.client().resolve_point(9999)

    async def test_nearest_postcode_point(self):
        point = await self.client().get_nearest_postcode_point(47.37, 8.54)
        assert point.point_id == 800500
        assert point.name == "Zürich"

    async def test_points_are_fetched_once(self):
        client = self.client()
        await client.get_points()
        await client.get_points()
        assert len(client._session.requests) == 1

    async def test_stations(self):
        header = (
            "station_abbr;station_name;station_canton;station_wigos_id;station_type_de;"
            "station_type_fr;station_type_it;station_type_en;station_dataowner;"
            "station_data_since;station_height_masl;station_height_barometer_masl;"
            "station_coordinates_lv95_east;station_coordinates_lv95_north;"
            "station_coordinates_wgs84_lat;station_coordinates_wgs84_lon\n"
        )
        rows = "SMA;Zürich / Fluntern;ZH;w;a;b;c;d;e;f;1;1;1;1;47.381003;8.567194\n"
        client, session = make_client(
            {
                f"{ogd.SMN_URL}/ogd-smn_meta_stations.csv": (header + rows).encode(
                    "cp1252"
                ),
                f"{ogd.SMN_PRECIP_URL}/ogd-smn-precip_meta_stations.csv": (
                    header + "ABE;Aarberg;BE;w;a;b;c;d;e;f;1;1;1;1;47.05;7.28\n"
                ).encode("cp1252"),
            }
        )
        weather = await client.get_stations(StationType.WEATHER)
        precipitation = await client.get_stations(StationType.PRECIPITATION)
        assert [(s.abbr, s.name) for s in weather] == [("SMA", "Zürich / Fluntern")]
        assert [(s.abbr, s.name) for s in precipitation] == [("ABE", "Aarberg")]


class TestStationNow:
    HEADER = "station_abbr;reference_timestamp;tre200s0;ure200s0;rre150z0\n"
    FILE = f"{ogd.SMN_URL}/sma/ogd-smn_sma_t_now.csv"

    async def test_newest_row_wins_but_gaps_are_filled(self):
        body = (
            self.HEADER
            + "SMA;20.10.2026 11:20;9.0;70;0\n"
            + "SMA;20.10.2026 11:30;10.0;75;0.2\n"
            + "SMA;20.10.2026 11:40;;80;\n"
        )
        client, _ = make_client({self.FILE: body.encode("cp1252")})
        assert await client.get_station_now("SMA", StationType.WEATHER) == {
            "tre200s0": 10.0,
            "ure200s0": 80.0,
            "rre150z0": 0.2,
        }

    async def test_old_rows_are_not_used(self):
        body = self.HEADER + "SMA;t;1.0;50;0\n" + "SMA;t;;;\n" * 3
        client, _ = make_client({self.FILE: body.encode("cp1252")})
        result = await client.get_station_now("SMA", StationType.WEATHER)
        assert result["tre200s0"] is None

    async def test_no_rows_yet(self):
        client, _ = make_client({self.FILE: self.HEADER.encode()})
        assert await client.get_station_now("SMA", StationType.WEATHER) == {}

    async def test_precipitation_station_url(self):
        file = f"{ogd.SMN_PRECIP_URL}/abe/ogd-smn-precip_abe_t_now.csv"
        body = "station_abbr;reference_timestamp;rre150z0\nABE;20.10.2026 11:40;0.4\n"
        client, _ = make_client({file: body.encode()})
        result = await client.get_station_now("ABE", StationType.PRECIPITATION)
        assert result == {"rre150z0": 0.4}

    async def test_unknown_station(self):
        client, _ = make_client({})
        with pytest.raises(OGDError):
            await client.get_station_now("XXX", StationType.WEATHER)
