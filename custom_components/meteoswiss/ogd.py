"""Client for the MeteoSwiss Open Government Data (OGD) CSV files.

Forecasts: https://opendatadocs.meteoswiss.ch/e-forecast-data/e4-local-forecast-data
Measurements: https://opendatadocs.meteoswiss.ch/a-data-groundbased

Source: MeteoSwiss.
"""

from __future__ import annotations

import asyncio
import csv
import datetime
import io
import logging
import math
from dataclasses import dataclass
from enum import Enum
from typing import TypedDict

import aiohttp

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://data.geo.admin.ch"
FORECAST_URL = BASE_URL + "/ch.meteoschweiz.ogd-local-forecasting"
POINTS_URL = FORECAST_URL + "/ogd-local-forecasting_meta_point.csv"
SMN_URL = BASE_URL + "/ch.meteoschweiz.ogd-smn"
SMN_PRECIP_URL = BASE_URL + "/ch.meteoschweiz.ogd-smn-precip"

# Forecast point types (point_type_id in the metadata).
POINT_TYPE_POSTCODE = 2

# Hourly model runs are published a few minutes past each hour.
MAX_RUN_LOOKBACK_HOURS = 12

# The forecast files hold every point (up to 33 MB).  When a file is ordered by
# point, the rows of one point are fetched with a binary search using HTTP
# Range requests; otherwise the whole file is downloaded.
HEAD_BYTES = 4096
PROBE_BYTES = 512
# Narrow the search window to this many bytes before reading the final span.
SEARCH_WINDOW = 4096
# The rows of one point (9 days, hourly) fit comfortably in this many bytes.
ROW_SPAN = 16384

# Station files have a row every 10 minutes.
STATION_ROWS_LOOKBACK = 3

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)

DAILY_PARAMETERS = {
    "temperature_min": "tre200pn",
    "temperature_max": "tre200px",
    "precipitation": "rka150p0",
    "symbol": "jp2000d0",
}
HOURLY_PARAMETERS = {
    "symbol": "jww003i0",
    "precipitation": "rre150h0",
    "wind_speed": "fu3010h0",
    "wind_bearing": "dkl010h0",
}


class OGDError(Exception):
    """Data could not be obtained from MeteoSwiss."""


class StationType(str, Enum):
    """Station type."""

    WEATHER = "weather"
    PRECIPITATION = "precipitation"


@dataclass(frozen=True)
class Point:
    """A forecast point."""

    point_id: int
    point_type: int
    name: str
    postcode: str
    lat: float
    lon: float


@dataclass(frozen=True)
class Station:
    """A measurement station."""

    abbr: str
    name: str
    lat: float
    lon: float


class DailyForecast(TypedDict):
    date: str  # ISO date of the (local) day
    temperature_min: float | None
    temperature_max: float | None
    precipitation: float | None
    symbol: int | None  # MeteoSwiss pictogram of the day


class HourlyForecast(TypedDict):
    time: datetime.datetime  # UTC, start of the hour
    symbol: int | None
    precipitation: float | None
    wind_speed: float | None
    wind_bearing: float | None


class LocalForecast(TypedDict):
    run: str  # model run, e.g. 202610201100
    daily: list[DailyForecast]
    hourly: list[HourlyForecast]


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two WGS84 coordinates."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = p2 - p1
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def nearest[T: (Point, Station)](items: list[T], lat: float, lon: float) -> T | None:
    """Return the item closest to the coordinates."""
    return min(
        items,
        key=lambda i: distance_km(lat, lon, i.lat, i.lon),
        default=None,
    )


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if value in ("", "-"):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _integer(value: str | None) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


def _key(line: bytes | str) -> tuple[int, int] | None:
    """The (point_id, point_type_id) of a CSV row, None for a header or junk."""
    parts = (line.decode("latin-1") if isinstance(line, bytes) else line).split(";", 2)
    try:
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return None


def _rows_with_prefix(text: str, prefix: str) -> dict[str, str]:
    """Map timestamp to value for all rows starting with the prefix."""
    rows = {}
    for line in text.splitlines():
        if line.startswith(prefix):
            parts = line.split(";")
            if len(parts) >= 4:
                rows[parts[2]] = parts[3]
    return rows


def _utc(timestamp: str) -> datetime.datetime:
    return datetime.datetime.strptime(timestamp, "%Y%m%d%H%M").replace(
        tzinfo=datetime.timezone.utc
    )


class MeteoSwissOGD:
    """Fetches forecasts and measurements from MeteoSwiss open data."""

    def __init__(self, session: aiohttp.ClientSession, user_agent: str) -> None:
        self._session = session
        self._headers = {"User-Agent": user_agent}
        self._points: list[Point] | None = None
        self._stations: dict[StationType, list[Station]] = {}

    async def _get(
        self, url: str, byte_range: tuple[int, int] | None = None
    ) -> tuple[bytes, int]:
        """Fetch a URL (or a byte range of it), returning the body and total size."""
        headers = dict(self._headers)
        if byte_range:
            headers["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
        try:
            async with self._session.get(
                url, headers=headers, timeout=REQUEST_TIMEOUT
            ) as response:
                response.raise_for_status()
                body = await response.read()
                total = len(body)
                content_range = response.headers.get("Content-Range", "")
                if "/" in content_range:
                    total = int(content_range.rsplit("/", 1)[1])
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise OGDError(f"Cannot fetch {url}: {exc}") from exc
        return body, total

    # Metadata

    async def get_points(self) -> list[Point]:
        """All forecast points."""
        if self._points is None:
            body, _ = await self._get(POINTS_URL)
            reader = csv.DictReader(io.StringIO(body.decode("latin-1")), delimiter=";")
            self._points = [
                Point(
                    point_id=int(row["point_id"]),
                    point_type=int(row["point_type_id"]),
                    name=row["point_name"],
                    postcode=row["postal_code"],
                    lat=float(row["point_coordinates_wgs84_lat"]),
                    lon=float(row["point_coordinates_wgs84_lon"]),
                )
                for row in reader
                if row["point_id"]
            ]
        return self._points

    async def get_postcode_points(self, postcode: str | int) -> list[Point]:
        """The forecast points of a postal code (some span several points)."""
        return [
            p
            for p in await self.get_points()
            if p.point_type == POINT_TYPE_POSTCODE and p.postcode == str(postcode)
        ]

    async def get_nearest_postcode_point(self, lat: float, lon: float) -> Point | None:
        return nearest(
            [p for p in await self.get_points() if p.point_type == POINT_TYPE_POSTCODE],
            lat,
            lon,
        )

    async def resolve_point(
        self, postcode: str | int, point_id: int | None = None
    ) -> Point:
        """The forecast point for a configuration.

        An explicit point ID wins; otherwise it is the first point of the
        postal code.
        """
        points = await self.get_postcode_points(postcode)
        if point_id is not None:
            points = [p for p in points if p.point_id == point_id] or points
        if not points:
            raise OGDError(f"No forecast point for postal code {postcode}")
        return min(points, key=lambda p: p.point_id)

    async def get_stations(self, station_type: StationType) -> list[Station]:
        """All stations of a type with real-time data."""
        if station_type not in self._stations:
            base = SMN_URL if station_type == StationType.WEATHER else SMN_PRECIP_URL
            prefix = (
                "ogd-smn" if station_type == StationType.WEATHER else "ogd-smn-precip"
            )
            body, _ = await self._get(f"{base}/{prefix}_meta_stations.csv")
            reader = csv.DictReader(
                io.StringIO(body.decode("windows-1252")), delimiter=";"
            )
            self._stations[station_type] = [
                Station(
                    abbr=row["station_abbr"],
                    name=row["station_name"],
                    lat=float(row["station_coordinates_wgs84_lat"]),
                    lon=float(row["station_coordinates_wgs84_lon"]),
                )
                for row in reader
                if row["station_abbr"]
            ]
        return self._stations[station_type]

    # Measurements

    async def get_station_now(
        self, abbr: str, station_type: StationType
    ) -> dict[str, float | None]:
        """The latest measurements of a station, empty if it has none today."""
        low = abbr.lower()
        if station_type == StationType.WEATHER:
            url = f"{SMN_URL}/{low}/ogd-smn_{low}_t_now.csv"
        else:
            url = f"{SMN_PRECIP_URL}/{low}/ogd-smn-precip_{low}_t_now.csv"
        body, _ = await self._get(url)
        rows = list(
            csv.DictReader(io.StringIO(body.decode("windows-1252")), delimiter=";")
        )
        # The newest row is often incomplete, so every parameter comes from the
        # most recent of the last few rows that has a value for it.
        latest: dict[str, float | None] = {}
        for row in reversed(rows[-STATION_ROWS_LOOKBACK:]):
            for key, value in row.items():
                if key in ("station_abbr", "reference_timestamp"):
                    continue
                if latest.get(key) is None:
                    latest[key] = _number(value)
        return latest

    # Forecasts

    @staticmethod
    def _run_url(run: str, parameter: str) -> str:
        return f"{FORECAST_URL}/{run[:8]}-ch/vnut12.lssw.{run}.{parameter}.csv"

    async def latest_run(self, now: datetime.datetime | None = None) -> str:
        """The most recent published model run, e.g. 202610201100."""
        now = now or datetime.datetime.now(datetime.timezone.utc)
        for back in range(MAX_RUN_LOOKBACK_HOURS):
            run = (now - datetime.timedelta(hours=back)).strftime("%Y%m%d%H00")
            url = self._run_url(run, DAILY_PARAMETERS["symbol"])
            try:
                async with self._session.head(
                    url, headers=self._headers, timeout=REQUEST_TIMEOUT
                ) as response:
                    # Files that do not exist (yet) are answered with 403.
                    if response.status in (403, 404):
                        continue
                    response.raise_for_status()
                    return run
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise OGDError(f"Cannot find the latest forecast run: {exc}") from exc
        raise OGDError("No forecast run published in the last few hours")

    async def _point_values(self, url: str, point: Point) -> dict[str, str]:
        """Timestamp to value of one point in a forecast file."""
        prefix = f"{point.point_id};{point.point_type};"
        head, total = await self._get(url, (0, HEAD_BYTES - 1))
        if total > len(head):
            lines = head.decode("latin-1").splitlines()[1:3]
            keys = [_key(line) for line in lines]
            if len(keys) == 2 and keys[0] is not None and keys[0] == keys[1]:
                return await self._ranged_point_values(url, total, point, prefix)
            body, _ = await self._get(url)
            return _rows_with_prefix(body.decode("latin-1"), prefix)
        return _rows_with_prefix(head.decode("latin-1"), prefix)

    async def _ranged_point_values(
        self, url: str, total: int, point: Point, prefix: str
    ) -> dict[str, str]:
        """Binary search a file ordered by (point_id, point_type_id)."""
        target = (point.point_id, point.point_type)
        # The first row of the target starts after the first line break past
        # `lo` and no later than the first line break past `hi`.
        lo, hi = 0, total
        while hi - lo > SEARCH_WINDOW:
            mid = (lo + hi) // 2
            chunk, _ = await self._get(url, (mid, min(mid + PROBE_BYTES, total) - 1))
            newline = chunk.find(b"\n")
            key = _key(chunk[newline + 1 :].split(b"\n")[0]) if newline >= 0 else None
            if key is not None and key < target:
                lo = mid
            else:
                hi = mid
        end = min(hi + ROW_SPAN, total)
        chunk, _ = await self._get(url, (lo, end - 1))
        lines = chunk.split(b"\n")
        if lo > 0:
            lines = lines[1:]  # starts mid-line
        if end < total:
            lines = lines[:-1]  # ends mid-line
        return _rows_with_prefix(
            b"\n".join(lines).decode("latin-1"),
            prefix,
        )

    async def get_forecast(self, point: Point, run: str) -> LocalForecast:
        """The daily and hourly forecast of a point for a model run."""
        names = [
            *(("daily", k, p) for k, p in DAILY_PARAMETERS.items()),
            *(("hourly", k, p) for k, p in HOURLY_PARAMETERS.items()),
        ]
        results = await asyncio.gather(
            *(self._point_values(self._run_url(run, p), point) for _, _, p in names)
        )
        daily: dict[str, dict[str, str]] = {}
        hourly: dict[str, dict[str, str]] = {}
        for (kind, key, _), values in zip(names, results):
            target = daily if kind == "daily" else hourly
            target[key] = values

        if not daily["symbol"] and not hourly["symbol"]:
            raise OGDError(f"Run {run} has no data for point {point}")

        days = sorted(set().union(*(v.keys() for v in daily.values())))
        daily_out: list[DailyForecast] = [
            {
                "date": _utc(day).date().isoformat(),
                "temperature_min": _number(daily["temperature_min"].get(day)),
                "temperature_max": _number(daily["temperature_max"].get(day)),
                "precipitation": _number(daily["precipitation"].get(day)),
                "symbol": _integer(daily["symbol"].get(day)),
            }
            for day in days
        ]

        hours = sorted(set().union(*(v.keys() for v in hourly.values())))
        hourly_out: list[HourlyForecast] = [
            {
                # Timestamps mark the end of the interval; report its start.
                "time": _utc(hour) - datetime.timedelta(hours=1),
                "symbol": _integer(hourly["symbol"].get(hour)),
                "precipitation": _number(hourly["precipitation"].get(hour)),
                "wind_speed": _number(hourly["wind_speed"].get(hour)),
                "wind_bearing": _number(hourly["wind_bearing"].get(hour)),
            }
            for hour in hours
        ]
        return {"run": run, "daily": daily_out, "hourly": hourly_out}
