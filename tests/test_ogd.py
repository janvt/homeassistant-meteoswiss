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
    type_then_text_csv,
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
    for p, v in (("nprolohs", "0.50"), ("npromths", "0.00"), ("nprohihs", "0.50")):
        files[url(p)] = point_major_csv(p, pts, HOURS, lambda i, ts, v=v: v)
    # Gusts by the hour the interval ends, to check the daily maximum.
    files[url("fu3010h1")] = point_major_csv(
        "fu3010h1", pts, HOURS, lambda i, ts: str(int(ts[8:10]))
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


@pytest.mark.parametrize(
    ("layers", "expected"),
    [
        ((0.0, 0.0, 0.0), 0),
        ((1.0, 0.0, 0.0), 100),
        ((0.0, 0.0, 0.8), 80),
        ((0.5, 0.5, 0.0), 75),
        ((0.5, 0.5, 0.5), 88),
        ((1.2, -0.1, 0.0), 100),  # out of range values are clamped
        ((0.5, None, 0.5), None),
    ],
)
def test_cloud_coverage(layers, expected):
    assert ogd._cloud_coverage(*layers) == expected


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

    # IDs whose order as text differs from their order as numbers.
    MIXED_POINTS = [
        *((i, 1) for i in (1, 2, 10, 101, 1024, 10239, 20, 3, 912, 99)),
        *((i, 2) for i in (100025, 120000, 800500, 965800)),
        *((i, 3) for i in (2, 213, 864, 1061, 99)),
    ]

    @pytest.mark.parametrize("target", MIXED_POINTS)
    async def test_files_ordered_by_type_then_id_as_text(self, target):
        body = type_then_text_csv("p", self.MIXED_POINTS, HOURS, value)
        client, _ = make_client({url("p"): body})
        point = Point(target[0], target[1], "x", "", 0, 0)

        result = await client._point_values(url("p"), point)

        expected = ogd._rows_with_prefix(body.decode(), f"{target[0]};{target[1]};")
        assert len(expected) == 217
        assert result == expected

    async def test_the_order_of_a_file_is_remembered(self):
        body = type_then_text_csv("p", self.MIXED_POINTS, HOURS, value)
        client, session = make_client({url("p"): body})

        await client._point_values(url("p"), Point(864, 3, "x", "", 0, 0))
        first = len(session.requests)
        await client._point_values(url("p"), Point(1024, 1, "x", "", 0, 0))

        # The second lookup searched only once, in the order that worked.
        assert len(session.requests) - first < first
        assert client._orders == {"p": 1}

    async def test_point_not_in_file(self):
        body = point_major_csv("p", BIG_POINTS, HOURS, value)
        client, _ = make_client({url("p"): body})
        assert await client._point_values(url("p"), Point(5, 2, "x", "", 0, 0)) == {}

    async def test_a_small_time_major_file_is_read_at_once(self):
        body = time_major_csv("p", BIG_POINTS, HOURS[:50], value)
        client, session = make_client({url("p"): body})
        point = Point(BIG_POINTS[3][0], 2, "x", "", 0, 0)

        result = await client._point_values(url("p"), point)

        assert len(result) == 50
        assert result[HOURS[0]] == value(point.point_id, HOURS[0])
        # The probe of the order, then the whole file, compressed: it is no
        # larger than reading enough to learn its layout.
        assert [r for _, _, r in session.requests] == [
            f"bytes=0-{ogd.HEAD_BYTES - 1}",
            None,
        ]

    @pytest.mark.parametrize("chunk_size", [7, 1000, 1 << 16])
    @pytest.mark.parametrize("target", [BIG_POINTS[0], BIG_POINTS[-1]])
    async def test_streaming_handles_any_chunking(self, chunk_size, target):
        body = time_major_csv("p", BIG_POINTS, HOURS[:50], value)
        # The last row may lack a line break.
        files = {url("p"): body, url("q"): body.rstrip(b"\n")}
        client, _ = make_client(files)
        client._session.chunk_size = chunk_size
        expected = ogd._rows_with_prefix(body.decode(), f"{target[0]};{target[1]};")

        prefix = f"{target[0]};{target[1]};"
        for file in ("p", "q"):
            rows = await client._streamed_point_values(url(file), [prefix])
            assert rows[prefix] == expected
        assert len(expected) == 50

    async def test_streaming_does_not_match_inside_a_row(self):
        # Point 1;2 must not match the row of point 11;2 or 21;2.
        body = time_major_csv("p", [(1, 2), (11, 2), (21, 2)] * 1, HOURS[:200], value)
        client, _ = make_client({url("p"): body})
        result = (await client._streamed_point_values(url("p"), ["1;2;"]))["1;2;"]
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
            "wind_gust": 22.0,  # the hour ending 22:00 UTC is the last of the day
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
        assert noon["wind_gust"] == 12.0
        # Half the sky at two levels: three quarters covered.
        assert noon["cloud_coverage"] == 75
        assert forecast["daily"][1]["wind_gust"] == 23.0

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

        rows = await client.get_hourly_rows([point], RUN, "tre200h0")
        forecast = await client.get_forecast(point, RUN, rows[point.key])

        assert len(rows[point.key]) == 218
        hours = {h["time"]: h for h in forecast["hourly"]}
        # The earlier hours from the temperature file are kept too.
        early = hours[datetime.datetime(2026, 10, 19, 21, tzinfo=UTC)]
        assert early["temperature"] == float(f"{100025 % 7}.2")
        assert early["symbol"] is None
        assert hours[datetime.datetime(2026, 10, 20, 11, tzinfo=UTC)][
            "temperature"
        ] == float(f"{100025 % 7}.2")  # the row ending 12:00

    async def test_one_download_serves_several_points(self):
        client, session = make_client(forecast_files())
        points = [
            Point(100025, 2, "Lausanne 25", "1000", 46.56, 6.68),
            Point(800500, 2, "Zürich", "8005", 47.39, 8.52),
            Point(864, 3, "Bristen", "", 46.75, 8.71),  # not in the file
        ]

        rows = await client.get_hourly_rows(points, RUN, "tre200h0")

        # One read for all three points, as the file is small.
        # Its size, then the whole file for all three, as it is small.
        assert [r for r in session.requests if r[1] == url("tre200h0")] == [
            ("HEAD", url("tre200h0"), None),
            ("GET", url("tre200h0"), None),
        ]
        assert {k: len(v) for k, v in rows.items()} == {
            (100025, 2): 218,
            (800500, 2): 218,
            (864, 3): 0,
        }
        assert rows[(800500, 2)]["202610201200"] == f"{800500 % 7}.2"

    async def test_freezing_level(self):
        client, _ = make_client(forecast_files())
        point = Point(800500, 2, "Zürich", "8005", 47.39, 8.52)

        forecast = await client.get_forecast(
            point, RUN, freezing_level={"202610201200": "3572"}
        )

        hours = {h["time"]: h for h in forecast["hourly"]}
        assert (
            hours[datetime.datetime(2026, 10, 20, 11, tzinfo=UTC)]["freezing_level"]
            == 3572.0
        )
        assert (
            hours[datetime.datetime(2026, 10, 20, 12, tzinfo=UTC)]["freezing_level"]
            is None
        )

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

    async def test_mountains_closest_first(self):
        points = await self.client().get_poi_points(47.0, 9.0)
        assert [(p.point_id, p.name, p.height) for p in points] == [
            (437, "Glattalphütte SAC", 1.0)
        ]

    async def test_resolve_a_mountain(self):
        point = await self.client().resolve_point(None, 437, ogd.POINT_TYPE_POI)
        assert point.name == "Glattalphütte SAC"

    async def test_unknown_mountain(self):
        with pytest.raises(ogd.UnknownPointError) as exc:
            await self.client().resolve_point(None, 1, ogd.POINT_TYPE_POI)
        # Point 1 exists, but is a station rather than a mountain.
        assert not isinstance(exc.value, ogd.UnknownPostcodeError)

    async def test_heights(self):
        points = {p.key: p for p in await self.client().get_points()}
        assert points[(1, 1)].height == 1878.0
        assert points[(800500, 2)].height == 404.0

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


# Stations publish 20-40 minutes late, so 11:40 is the newest row at 12:05.
STATIONS_NOW = datetime.datetime(2026, 10, 20, 12, 5, tzinfo=UTC)


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
        assert await client.get_station_now(
            "SMA", StationType.WEATHER, now=STATIONS_NOW
        ) == {
            "tre200s0": 10.0,
            "ure200s0": 80.0,
            "rre150z0": 0.2,
            # The time of the newest row.
            "measured": datetime.datetime(2026, 10, 20, 11, 40, tzinfo=UTC),
        }

    async def test_old_rows_are_not_used(self):
        body = (
            self.HEADER
            + "SMA;20.10.2026 11:10;1.0;50;0\n"
            + "SMA;20.10.2026 11:20;;;\n" * 3
        )
        client, _ = make_client({self.FILE: body.encode("cp1252")})
        result = await client.get_station_now(
            "SMA", StationType.WEATHER, now=STATIONS_NOW
        )
        assert result["tre200s0"] is None

    @pytest.mark.parametrize(
        ("measured", "fresh"),
        [("20.10.2026 10:40", True), ("20.10.2026 10:30", False)],
    )
    async def test_measurements_older_than_90_minutes_are_ignored(
        self, measured, fresh
    ):
        body = self.HEADER + f"SMA;{measured};10.0;75;0.2\n"
        client, _ = make_client({self.FILE: body.encode()})
        result = await client.get_station_now(
            "SMA", StationType.WEATHER, now=STATIONS_NOW
        )
        assert result == (
            {
                "tre200s0": 10.0,
                "ure200s0": 75.0,
                "rre150z0": 0.2,
                "measured": datetime.datetime(2026, 10, 20, 10, 40, tzinfo=UTC),
            }
            if fresh
            else {}
        )

    async def test_rows_without_a_time_are_ignored(self):
        body = self.HEADER + "SMA;soon;10.0;75;0.2\n"
        client, _ = make_client({self.FILE: body.encode()})
        assert (
            await client.get_station_now("SMA", StationType.WEATHER, now=STATIONS_NOW)
            == {}
        )

    async def test_no_rows_yet(self):
        client, _ = make_client({self.FILE: self.HEADER.encode()})
        assert (
            await client.get_station_now("SMA", StationType.WEATHER, now=STATIONS_NOW)
            == {}
        )

    async def test_precipitation_station_url(self):
        file = f"{ogd.SMN_PRECIP_URL}/abe/ogd-smn-precip_abe_t_now.csv"
        body = "station_abbr;reference_timestamp;rre150z0\nABE;20.10.2026 11:40;0.4\n"
        client, _ = make_client({file: body.encode()})
        result = await client.get_station_now(
            "ABE", StationType.PRECIPITATION, now=STATIONS_NOW
        )
        assert result["rre150z0"] == 0.4

    async def test_unknown_station(self):
        client, _ = make_client({})
        with pytest.raises(OGDError):
            await client.get_station_now("XXX", StationType.WEATHER, now=STATIONS_NOW)


async def test_precipitation_station_in_the_weather_network():
    weather_file = f"{ogd.SMN_URL}/bia/ogd-smn_bia_t_now.csv"
    body = (
        "station_abbr;reference_timestamp;tre200s0;rre150z0\n"
        "BIA;20.10.2026 11:40;5.0;0.3\n"
    )
    client, session = make_client({weather_file: body.encode()})

    result = await client.get_station_now(
        "BIA", StationType.PRECIPITATION, now=STATIONS_NOW
    )

    assert result["rre150z0"] == 0.3
    assert session.requests[0][1].endswith("ogd-smn-precip_bia_t_now.csv")


async def test_missing_precipitation_station_reports_its_own_file():
    client, _ = make_client({})
    with pytest.raises(OGDError, match="ogd-smn-precip_xyz"):
        await client.get_station_now("XYZ", StationType.PRECIPITATION, now=STATIONS_NOW)


class TestFewerRequests:
    """Lookups reuse what they learnt, as each run's files are laid out alike."""

    async def test_range_requests_ask_for_uncompressed_data(self):
        body = point_major_csv("p", BIG_POINTS, HOURS, value)
        client, session = make_client({url("p"): body})

        await client._point_values(url("p"), Point(*BIG_POINTS[5], "x", "", 0, 0))

        # Otherwise the server sends small files whole, compressed.
        ranged = [h for h in session.headers if "Range" in h]
        assert ranged
        assert all(h["Accept-Encoding"] == "identity" for h in ranged)

    async def test_the_latest_run_is_shared_for_a_few_minutes(self):
        client, session = make_client({url("jp2000d0"): b"x"})
        now = datetime.datetime(2026, 10, 20, 12, 40, tzinfo=UTC)

        assert await client.latest_run(now) == RUN
        assert await client.latest_run(now + datetime.timedelta(minutes=4)) == RUN
        assert len(session.requests) == 1
        await client.latest_run(now + datetime.timedelta(minutes=6))
        assert len(session.requests) == 2

    async def test_station_files_are_shared_for_a_minute(self, monkeypatch):
        file = f"{ogd.SMN_URL}/sma/ogd-smn_sma_t_now.csv"
        body = "station_abbr;reference_timestamp;tre200s0\nSMA;20.10.2026 11:40;5\n"
        client, session = make_client({file: body.encode()})
        clock = [1000.0]
        monkeypatch.setattr(ogd.time, "monotonic", lambda: clock[0])

        for _ in range(3):
            await client.get_station_now("SMA", StationType.WEATHER, now=STATIONS_NOW)
        assert len(session.requests) == 1
        clock[0] += 61
        await client.get_station_now("SMA", StationType.WEATHER, now=STATIONS_NOW)
        assert len(session.requests) == 2

    async def test_station_files_without_a_new_row_are_not_fetched_again(
        self, monkeypatch
    ):
        file = f"{ogd.SMN_URL}/sma/ogd-smn_sma_t_now.csv"
        body = "station_abbr;reference_timestamp;tre200s0\nSMA;20.10.2026 11:40;5\n"
        client, session = make_client({file: body.encode()})
        clock = [1000.0]
        monkeypatch.setattr(ogd.time, "monotonic", lambda: clock[0])
        await client.get_station_now("SMA", StationType.WEATHER, now=STATIONS_NOW)

        clock[0] += 300
        now = await client.get_station_now("SMA", StationType.WEATHER, now=STATIONS_NOW)

        # Asked whether it changed, and answered that it did not.
        assert "If-None-Match" in session.headers[1]
        assert now["tre200s0"] == 5.0

        session.files[file] = (body + "SMA;20.10.2026 11:50;6\n").encode()
        clock[0] += 300
        now = await client.get_station_now("SMA", StationType.WEATHER, now=STATIONS_NOW)
        assert now["tre200s0"] == 6.0


class TestBlocks:
    """Files ordered by time are read a row per block, near where it is expected."""

    # Enough points that a block is far larger than the margins.
    POINTS = [(i, 1) for i in range(1, 400)] + [(i * 100, 2) for i in range(1, 400)]

    @staticmethod
    def temperature(run: int):
        """Values of varying length, a little different in each run."""

        def value(point_id: int, ts: str) -> str:
            hour = int(ts[8:10]) + int(ts[6:8]) * 24
            degrees = ((point_id * 37 + hour * 11 + run * 3) % 400 - 150) / 10
            return f"{degrees:.1f}"

        return value

    def files(self, runs, stamps=HOURS):
        return {
            url("tre200h0", run): time_major_csv(
                "tre200h0", self.POINTS, stamps, self.temperature(n)
            )
            for n, run in enumerate(runs)
        }

    @pytest.fixture(autouse=True)
    def small_head(self, monkeypatch):
        # A head that holds the first block and a little more, as in real files.
        block = len(self.files([RUN], HOURS[:1])[url("tre200h0")])
        monkeypatch.setattr(ogd, "BLOCK_HEAD", block + 2000)

    def expected(self, body: bytes, key) -> dict[str, str]:
        return ogd._rows_with_prefix(body.decode(), f"{key[0]};{key[1]};")

    @pytest.mark.parametrize("target", [POINTS[0], POINTS[200], POINTS[-1]])
    async def test_the_rows_match_the_whole_file(self, target):
        files = self.files([RUN])
        client, session = make_client(files)
        point = Point(*target, "x", "", 0, 0)

        rows = await client.get_hourly_rows([point], RUN, "tre200h0")

        body = files[url("tre200h0")]
        assert rows[point.key] == self.expected(body, target)
        assert len(rows[point.key]) == 217
        assert all(r is not None for m, _, r in session.requests if m == "GET")
        # The head, then a window around the row in each block.
        window = 2 * ogd.BLOCK_MARGIN + ogd.BLOCK_ROW
        assert session.ranged_bytes < ogd.BLOCK_HEAD + 220 * window

    async def test_the_next_run_needs_a_small_range_per_block(self):
        runs = ["202610201100", RUN]
        files = self.files(runs)
        client, session = make_client(files)
        point = Point(*self.POINTS[300], "x", "", 0, 0)
        await client.get_hourly_rows([point], runs[0], "tre200h0")
        session.requests.clear()

        rows = await client.get_hourly_rows([point], RUN, "tre200h0")

        assert rows[point.key] == self.expected(files[url("tre200h0")], point.key)
        # No head again, and about one request per block.
        assert f"bytes=0-{ogd.BLOCK_HEAD - 1}" not in [
            r for _, _, r in session.requests
        ]
        assert len(session.requests) <= 217 + 5
        assert session.ranged_bytes < 217 * (2 * ogd.BLOCK_MARGIN + ogd.BLOCK_ROW)

    async def test_a_new_day_reads_the_layout_again(self):
        tomorrow = "202610210000"
        files = self.files(["202610202300", tomorrow])
        client, session = make_client(files)
        point = Point(*self.POINTS[5], "x", "", 0, 0)
        await client.get_hourly_rows([point], "202610202300", "tre200h0")
        session.requests.clear()

        rows = await client.get_hourly_rows([point], tomorrow, "tre200h0")

        assert f"bytes=0-{ogd.BLOCK_HEAD - 1}" in [r for _, _, r in session.requests]
        assert rows[point.key] == self.expected(
            files[url("tre200h0", tomorrow)], point.key
        )

    async def test_several_points_and_one_not_in_the_file(self):
        files = self.files([RUN])
        client, _ = make_client(files)
        points = [Point(*k, "x", "", 0, 0) for k in (self.POINTS[3], self.POINTS[-3])]

        rows = await client.get_hourly_rows(
            [*points, Point(864, 3, "Bristen", "", 0, 0)], RUN, "tre200h0"
        )

        body = files[url("tre200h0")]
        for point in points:
            assert rows[point.key] == self.expected(body, point.key)
        assert rows[(864, 3)] == {}

    async def test_a_point_that_cannot_be_followed_is_read_from_the_whole_file(self):
        files = self.files([RUN])
        body = files[url("tre200h0")]
        # A block in the middle lists the points in another order.
        lines = body.decode().split("\n")
        per_block = len(self.POINTS)
        start = 1 + 100 * per_block
        lines[start : start + per_block] = reversed(lines[start : start + per_block])
        files[url("tre200h0")] = body = "\n".join(lines).encode()
        client, session = make_client(files)
        point = Point(*self.POINTS[10], "x", "", 0, 0)

        rows = await client.get_hourly_rows([point], RUN, "tre200h0")

        assert rows[point.key] == self.expected(body, point.key)
        assert ("GET", url("tre200h0"), None) in session.requests

    async def test_daily_files_too(self):
        body = time_major_csv("tre200pn", self.POINTS, DAYS, self.temperature(0))
        client, session = make_client({url("tre200pn"): body})
        point = Point(*self.POINTS[123], "x", "", 0, 0)

        rows = await client._point_values(url("tre200pn"), point)

        assert rows == self.expected(body, point.key)
        assert len(rows) == 9
        window = 2 * ogd.BLOCK_MARGIN + ogd.BLOCK_ROW
        assert session.ranged_bytes < ogd.HEAD_BYTES + ogd.BLOCK_HEAD + 9 * window


class TestPollen:
    FILE = f"{ogd.POLLEN_URL}/pzh/ogd-pollen_pzh_h_now.csv"
    HEADER = (
        "station_abbr;reference_timestamp;kabetuh0;khpoach0;kaalnuh0;kacoryh0;"
        "kafaguh0;kafraxh0;kaquerh0\n"
    )

    def client(self, rows):
        return make_client({self.FILE: (self.HEADER + rows).encode("cp1252")})

    async def test_the_newest_hour(self):
        client, _ = self.client(
            "PZH;20.10.2026 10:00;5;1;0;0;0;0;0\nPZH;20.10.2026 11:00;42;7;0;0;;1;3\n"
        )

        pollen = await client.get_pollen_now("PZH", now=STATIONS_NOW)

        assert pollen == {
            "alder": 0.0,
            "ash": 1.0,
            "beech": None,
            "birch": 42.0,
            "grasses": 7.0,
            "hazel": 0.0,
            "oak": 3.0,
            # The end of the hour, like all station data.
            "measured": datetime.datetime(2026, 10, 20, 11, tzinfo=UTC),
        }

    async def test_an_hour_without_values_is_skipped(self):
        client, _ = self.client(
            "PZH;20.10.2026 10:00;5;1;0;0;0;0;0\nPZH;20.10.2026 11:00;;;;;;;\n"
        )
        pollen = await client.get_pollen_now("PZH", now=STATIONS_NOW)
        assert pollen["birch"] == 5.0

    async def test_old_measurements_are_not_used(self):
        # Luzern stopped reporting the day before.
        client, _ = self.client("PZH;19.10.2026 16:00;5;1;0;0;0;0;0\n")
        assert await client.get_pollen_now("PZH", now=STATIONS_NOW) == {}

    async def test_pollen_stations(self):
        header = (
            "station_abbr;station_name;station_canton;station_wigos_id;station_type_de;"
            "station_type_fr;station_type_it;station_type_en;station_dataowner;"
            "station_data_since;station_height_masl;station_height_barometer_masl;"
            "station_coordinates_lv95_east;station_coordinates_lv95_north;"
            "station_coordinates_wgs84_lat;station_coordinates_wgs84_lon\n"
        )
        row = "PZH;Zürich;ZH;;a;b;c;d;e;f;1;;1;1;47.378;8.565\n"
        client, _ = make_client(
            {
                f"{ogd.POLLEN_URL}/ogd-pollen_meta_stations.csv": (header + row).encode(
                    "cp1252"
                )
            }
        )
        stations = await client.get_stations(StationType.POLLEN)
        assert [(s.abbr, s.name) for s in stations] == [("PZH", "Zürich")]


class TestClimate:
    NOW_FILE = f"{ogd.SMN_URL}/alt/ogd-smn_alt_t_now.csv"
    HISTORY = f"{ogd.SMN_URL}/alt/ogd-smn_alt_d_historical.csv"
    RECENT = f"{ogd.SMN_URL}/alt/ogd-smn_alt_d_recent.csv"
    HEADER = (
        "station_abbr;reference_timestamp;"
        "tre200d0;tre200dx;tre200dn;rre150d0;sre000d0\n"
    )

    async def test_today_is_the_current_utc_day(self):
        body = (
            "station_abbr;reference_timestamp;tre200s0\n"
            "ALT;19.10.2026 23:50;3.0\n"
            "ALT;20.10.2026 00:10;6.6\n"
            "ALT;20.10.2026 11:40;22.3\n"
            "ALT;20.10.2026 11:50;\n"
        )
        client, _ = make_client({self.NOW_FILE: body.encode()})

        today = await client.get_station_today("ALT", now=STATIONS_NOW)

        assert today == {"high": 22.3, "low": 6.6}

    async def test_the_history_is_fetched_weekly_and_this_year_every_six_hours(self):
        history = self.HEADER + "ALT;20.10.2001 00:00;15;24.8;7;0;300\n"
        recent = self.HEADER + "ALT;19.10.2026 00:00;14;19;9;0.7;230\n"
        client, session = make_client(
            {self.HISTORY: history.encode(), self.RECENT: recent.encode()}
        )

        normals, days = await client.get_climate("ALT", now=STATIONS_NOW)
        assert normals.record_high[(10, 20)] == (24.8, 2001)
        assert days[0].date == datetime.date(2026, 10, 19)

        hour = datetime.timedelta(hours=1)
        await client.get_climate("ALT", now=STATIONS_NOW + 5 * hour)
        assert len(session.requests) == 2
        await client.get_climate("ALT", now=STATIONS_NOW + 7 * hour)
        assert [r[1] for r in session.requests][2:] == [self.RECENT]
        await client.get_climate("ALT", now=STATIONS_NOW + 8 * 24 * hour)
        assert [r[1] for r in session.requests][3:] == [self.HISTORY, self.RECENT]

    async def test_unchanged_files_are_not_fetched_again(self, monkeypatch):
        history = self.HEADER + "ALT;20.10.2001 00:00;15;24.8;7;0;300\n"
        recent = self.HEADER + "ALT;19.10.2026 00:00;14;19;9;0.7;230\n"
        files = {self.HISTORY: history.encode(), self.RECENT: recent.encode()}
        client, session = make_client(files)
        computed = []
        compute = ogd.climate.compute
        monkeypatch.setattr(
            ogd.climate, "compute", lambda days: computed.append(1) or compute(days)
        )
        await client.get_climate("ALT", now=STATIONS_NOW)

        # A week later the history is the same, and today's file too.
        later = STATIONS_NOW + datetime.timedelta(days=8)
        normals, days = await client.get_climate("ALT", now=later)

        assert normals.record_high[(10, 20)] == (24.8, 2001)
        assert len(days) == 1
        asked = session.headers[2:]
        assert all("If-None-Match" in h for h in asked) and len(asked) == 2
        # Nothing new to compute the normals from.
        assert computed == [1]

        files[self.RECENT] = (recent + "ALT;20.10.2026 00:00;15;20;8;0;100\n").encode()
        _, days = await client.get_climate(
            "ALT", now=later + datetime.timedelta(days=1)
        )
        assert len(days) == 2
        assert computed == [1, 1]
