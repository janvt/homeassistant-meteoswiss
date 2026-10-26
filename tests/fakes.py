"""In-memory stand-ins for the MeteoSwiss servers and the OGD client."""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import re
from unittest.mock import MagicMock

import aiohttp

from custom_components.meteoswiss.climate import Climate, Day
from custom_components.meteoswiss.ogd import (
    LocalForecast,
    Point,
    Station,
    StationType,
    UnknownPointError,
    UnknownPostcodeError,
    distance_km,
)

UTC = datetime.timezone.utc


class FakeContent:
    def __init__(self, body: bytes, chunk_size: int) -> None:
        self.body = body
        self.chunk_size = chunk_size

    async def iter_chunked(self, n: int):
        # Ignores n: small chunks exercise rows split across chunk boundaries.
        for start in range(0, len(self.body), self.chunk_size):
            yield self.body[start : start + self.chunk_size]


class FakeResponse:
    def __init__(
        self, status: int, body: bytes = b"", headers=None, chunk_size: int = 1000
    ) -> None:
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.content = FakeContent(body, chunk_size)

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(MagicMock(), (), status=self.status)

    async def read(self) -> bytes:
        return self.body


class FakeSession:
    """Serves files by URL, with HTTP Range support; missing files are a 403."""

    def __init__(self, files: dict[str, bytes], chunk_size: int = 1000) -> None:
        self.files = files
        self.chunk_size = chunk_size
        # Like a server that sends whole files when asked for part of them.
        self.ignore_ranges = False
        self.requests: list[tuple[str, str, str | None]] = []
        self.headers: list[dict[str, str]] = []
        # The size of each response's body.
        self.served: list[int] = []

    def _respond(self, method: str, url: str, headers) -> FakeResponse:
        headers = headers or {}
        byte_range = headers.get("Range")
        self.requests.append((method, url, byte_range))
        self.headers.append(dict(headers))
        body = self.files.get(url)
        if body is None:
            return FakeResponse(403)
        if byte_range is None or self.ignore_ranges:
            # Like S3: the ETag is a hash of the content.
            etag = f'"{hashlib.md5(body).hexdigest()}"'
            if headers.get("If-None-Match") == etag:
                return FakeResponse(304)
            return FakeResponse(
                200,
                b"" if method == "HEAD" else body,
                {"Content-Length": str(len(body)), "ETag": etag},
                chunk_size=self.chunk_size,
            )
        match = re.fullmatch(r"bytes=(\d+)-(\d+)", byte_range)
        assert match, byte_range
        start, end = int(match[1]), min(int(match[2]), len(body) - 1)
        return FakeResponse(
            206,
            body[start : end + 1],
            {"Content-Range": f"bytes {start}-{end}/{len(body)}"},
        )

    def get(self, url, headers=None, timeout=None) -> FakeResponse:
        response = self._respond("GET", url, headers)
        self.served.append(len(response.body))
        return response

    def head(self, url, headers=None, timeout=None) -> FakeResponse:
        self.served.append(0)
        return self._respond("HEAD", url, headers)

    def transferred(self, overhead: int = 650) -> int:
        """Bytes received, with about as much for each response's headers as
        the real server sends (whole files are counted uncompressed)."""
        return sum(self.served) + overhead * len(self.served)

    def clear(self) -> None:
        self.requests.clear()
        self.headers.clear()
        self.served.clear()

    def count(self, method: str = "GET", url: str | None = None) -> int:
        return sum(1 for m, u, _ in self.requests if m == method and url in (None, u))

    @property
    def ranged_bytes(self) -> int:
        return sum(
            int(m[2]) - int(m[1]) + 1
            for _, _, r in self.requests
            if r and (m := re.fullmatch(r"bytes=(\d+)-(\d+)", r))
        )


def point_major_csv(param: str, points, timestamps, value) -> bytes:
    lines = [f"point_id;point_type_id;Date;{param}"]
    for point_id, point_type in sorted(points):
        for ts in timestamps:
            lines.append(f"{point_id};{point_type};{ts};{value(point_id, ts)}")
    return ("\n".join(lines) + "\n").encode("latin-1")


def type_then_text_csv(param: str, points, timestamps, value) -> bytes:
    """Ordered by point type, then by point ID as text, as some real files are."""
    lines = [f"point_id;point_type_id;Date;{param}"]
    for point_id, point_type in sorted(points, key=lambda k: (k[1], str(k[0]))):
        for ts in timestamps:
            lines.append(f"{point_id};{point_type};{ts};{value(point_id, ts)}")
    return ("\n".join(lines) + "\n").encode("latin-1")


def time_major_csv(param: str, points, timestamps, value) -> bytes:
    lines = [f"point_id;point_type_id;Date;{param}"]
    for ts in timestamps:
        for point_id, point_type in sorted(points):
            lines.append(f"{point_id};{point_type};{ts};{value(point_id, ts)}")
    return ("\n".join(lines) + "\n").encode("latin-1")


POINTS = [
    Point(100025, 2, "Lausanne 25", "1000", 46.5576, 6.6831),
    Point(100026, 2, "Lausanne 26", "1000", 46.5561, 6.6962),
    Point(800100, 2, "Zürich", "8001", 47.3723, 8.5422),
    Point(800500, 2, "Zürich", "8005", 47.3875, 8.5210),
    Point(1, 1, "Arosa", "", 46.7927, 9.6790),
    Point(864, 3, "Bristen", "", 46.7526, 8.7062, 3073.0),
    Point(442, 3, "Treschhütte SAC", "", 46.7489, 8.6814, 1475.0),
    Point(213, 3, "Ouest romand", "", 46.8116, 6.9424, 490.0),
]
WEATHER_STATIONS = [
    Station("SMA", "Zürich / Fluntern", 47.3810, 8.5672),
    Station("ARO", "Arosa", 46.7927, 9.6790),
]
PRECIPITATION_STATIONS = [Station("ABE", "Aarberg", 47.0580, 7.2854)]
POLLEN_STATIONS = [
    Station("PZH", "Zürich", 47.3781, 8.5654),
    Station("PLZ", "Luzern", 47.0577, 8.2964),
]
POLLEN_NOW = {
    "alder": 0.0,
    "ash": 1.0,
    "beech": None,
    "birch": 42.0,
    "grasses": 7.0,
    "hazel": 0.0,
    "oak": 3.0,
    "measured": datetime.datetime(2026, 10, 20, 12, tzinfo=UTC),
}

NOW = datetime.datetime(2026, 10, 20, 12, 30, tzinfo=UTC)


def make_forecast(
    run: str = "202610201200", with_temperature: bool = True, hours: int = 12
) -> LocalForecast:
    """A forecast with 3 days and the hours around NOW."""
    start = datetime.datetime(2026, 10, 20, 9, tzinfo=UTC)
    return {
        "run": run,
        "daily": [
            {
                "date": f"2026-10-{20 + i}",
                "temperature_min": 8.0 + i,
                "temperature_max": 15.0 + i,
                "precipitation": 0.5 * i,
                "precipitation_probability": 30.0 * i,
                "wind_gust": 40.0 + i,
                "symbol": 3 if i == 0 else 20,
            }
            for i in range(3)
        ],
        "hourly": [
            {
                "time": start + datetime.timedelta(hours=i),
                # Sunny by day, clear by night, one unknown symbol at the end.
                "symbol": 1 if i < 8 else (101 if i < 10 else 999),
                "temperature": 10.0 + i if with_temperature else None,
                "precipitation": 0.1 * i,
                "precipitation_probability": 5.0 * i,
                "wind_speed": 5.0 + i,
                "wind_bearing": 180.0,
                "wind_gust": 20.0 + i,
                "cloud_coverage": 10.0 * i,
                "sunshine": 30.0,
            }
            for i in range(hours)
        ],
    }


# Sea-level pressures: 6 hPa more south of the Alps, so south Föhn is likely.
FOEHN_PRESSURES = {"LUG": 1016.0, "KLO": 1010.0}

STATION_MEASUREMENTS = {
    "tre200s0": 13.4,
    "ure200s0": 79.9,
    "prestas0": 940.5,
    "pp0qffs0": 1012.3,
    "fu3010z0": 8.6,
    "dkl010z0": 31.0,
    "fu3010z1": 25.2,
    "tde200s0": 9.9,
    "wcc006s0": 2.0,  # MeteoSwiss's Föhn index: Föhn
    "rre150z0": 0.0,
}


def make_climate():
    """Like Altdorf's on 20 October 2026: a warm, dry, sunny October."""
    normals = Climate(
        normal_high={(10, 20): 14.3},
        normal_low={(10, 20): 6.0},
        record_high={(10, 20): (24.8, 2001)},
        record_low={(10, 20): (-1.4, 2009)},
        month_mean={10: 10.8},
        month_rain={10: 84.2},
        month_sunshine={10: 105.6},
        first_year=1864,
    )
    recent = [
        Day(datetime.date(2026, 10, d), 14.0, 19.0, 9.0, 0.7, 230.0)
        for d in range(1, 20)
    ]
    return normals, recent


class FakeOGD:
    """Replacement for MeteoSwissOGD that never touches the network."""

    # Class attributes so tests can change behaviour before or after setup.
    forecast_error: Exception | None = None
    station_error: Exception | None = None
    run = "202610201200"
    instances: list[FakeOGD] = []
    forecast_calls = 0
    run_checks = 0
    temperature_calls = 0
    temperature_error: Exception | None = None
    climate_calls = 0
    climate_error: Exception | None = None
    # Hours of hourly forecast, from 09:00 UTC on the 20th.
    hours = 12
    # The points each download of a large file was for, by parameter.
    large_downloads: dict[str, list[set[tuple[int, int]]]] = {}
    # When set, large downloads wait for it, like a slow connection.
    download_gate: asyncio.Event | None = None
    # Like MeteoSwissOGD's: bytes downloaded lately.
    downloaded = 0

    def __init__(self, session, user_agent) -> None:
        FakeOGD.instances.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.forecast_error = None
        cls.station_error = None
        cls.run = "202610201200"
        cls.instances = []
        cls.forecast_calls = 0
        cls.run_checks = 0
        cls.temperature_calls = 0
        cls.temperature_error = None
        cls.climate_calls = 0
        cls.climate_error = None
        cls.large_downloads = {}
        cls.download_gate = None
        cls.hours = 12
        cls.downloaded = 0

    async def get_postcode_points(self, postcode):
        return [p for p in POINTS if p.point_type == 2 and p.postcode == str(postcode)]

    async def get_nearest_postcode_point(self, lat, lon):
        return min(
            (p for p in POINTS if p.point_type == 2),
            key=lambda p: distance_km(lat, lon, p.lat, p.lon),
        )

    async def get_poi_points(self, lat, lon):
        return sorted(
            (p for p in POINTS if p.point_type == 3),
            key=lambda p: distance_km(lat, lon, p.lat, p.lon),
        )

    async def resolve_point(self, postcode, point_id=None, point_type=2):
        if point_type != 2:
            for p in POINTS:
                if p.key == (point_id, point_type):
                    return p
            raise UnknownPointError(f"No forecast point {point_id}")
        points = await self.get_postcode_points(postcode)
        if not points:
            raise UnknownPostcodeError(f"No forecast point for postal code {postcode}")
        return points[0]

    async def get_stations(self, station_type):
        if station_type == StationType.WEATHER:
            return WEATHER_STATIONS
        if station_type == StationType.POLLEN:
            return POLLEN_STATIONS
        return PRECIPITATION_STATIONS

    async def get_station_today(self, abbr, now=None):
        if self.station_error:
            raise self.station_error
        return {"high": 22.3, "low": 6.6}

    async def get_climate(self, abbr, now=None):
        FakeOGD.climate_calls += 1
        if self.climate_error:
            raise self.climate_error
        return make_climate()

    async def get_pollen_now(self, abbr, now=None):
        if self.station_error:
            raise self.station_error
        return dict(POLLEN_NOW) if abbr == "PZH" else {}

    async def get_station_now(self, abbr, station_type):
        if self.station_error:
            raise self.station_error
        if station_type == StationType.PRECIPITATION:
            return {"rre150z0": 0.2}
        if abbr in FOEHN_PRESSURES:
            return {"pp0qffs0": FOEHN_PRESSURES[abbr]}
        return dict(STATION_MEASUREMENTS)

    async def latest_run(self, now=None):
        FakeOGD.run_checks += 1
        if self.forecast_error:
            raise self.forecast_error
        return FakeOGD.run

    async def get_hourly_rows(self, points, run, parameter):
        if FakeOGD.download_gate is not None:
            await FakeOGD.download_gate.wait()
        FakeOGD.large_downloads.setdefault(parameter, []).append(
            {p.key for p in points}
        )
        if parameter == "tre200h0":
            FakeOGD.temperature_calls += 1
        if self.temperature_error:
            raise self.temperature_error
        return {p.key: {"run": run} for p in points}

    async def get_forecast(
        self, point, run, hourly_temperature=None, freezing_level=None
    ):
        FakeOGD.forecast_calls += 1
        forecast = make_forecast(
            run, with_temperature=bool(hourly_temperature), hours=FakeOGD.hours
        )
        for hour in forecast["hourly"]:
            hour["freezing_level"] = 2900.0 if freezing_level else None
        # Remember which run the large files came from.
        forecast["temperature_run"] = (hourly_temperature or {}).get("run")
        forecast["freezing_level_run"] = (freezing_level or {}).get("run")
        return forecast


def make_warning(type_, level, onset, expires, event=None):
    return {
        "identifier": f"{type_}-{level}-{onset.isoformat()}",
        "event": event or type_.capitalize(),
        "type": type_,
        "level": level,
        "onset": onset,
        "expires": expires,
        "region": "Flüelen-Unterschächen",
        "headline": f"{level.capitalize()} {type_} warning",
        "description": "Possible impacts.",
        "url": "https://meteoalarm.org/en/live/region/CH?sa=Fl%C3%BCelen",
    }


class FakeMeteoAlarm:
    """Replacement for MeteoAlarm with fixed warnings."""

    warnings: list = []
    error: Exception | None = None
    calls: list = []

    def __init__(self, session, user_agent) -> None:
        pass

    @classmethod
    def reset(cls) -> None:
        cls.warnings = []
        cls.error = None
        cls.calls = []

    async def get_warnings(self, lat, lon, height, language, now):
        FakeMeteoAlarm.calls.append((lat, lon, height, language))
        if self.error:
            raise self.error
        return [w for w in self.warnings if w["expires"] > now]


class FakeRadar:
    """Replacement for Radar with fixed readings."""

    readings: dict = {}
    calls: list = []

    def __init__(self, session, user_agent) -> None:
        pass

    @classmethod
    def reset(cls) -> None:
        cls.readings = {}
        cls.calls = []

    async def get(self, lat, lon, now):
        FakeRadar.calls.append((lat, lon))
        return dict(self.readings)
