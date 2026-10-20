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
import zoneinfo
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
# Range requests; otherwise the whole file is streamed and only the rows of
# the point are kept.
HEAD_BYTES = 4096
PROBE_BYTES = 512
# Narrow the search window to this many bytes before reading the final span.
SEARCH_WINDOW = 4096
# The rows of one point (9 days, hourly) fit comfortably in this many bytes.
ROW_SPAN = 16384

# Station files have a row every 10 minutes.
STATION_ROWS_LOOKBACK = 3
# Rows appear 20-40 minutes late; anything much older means the station has
# stopped reporting.
STATION_MAX_AGE = datetime.timedelta(minutes=90)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)
# Streaming a whole file may take a while on a slow connection.
STREAM_TIMEOUT = aiohttp.ClientTimeout(total=240, sock_read=30)
STREAM_CHUNK = 1 << 16

# Daily values are for the local day.
LOCAL_TIME_ZONE = zoneinfo.ZoneInfo("Europe/Zurich")

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
    "precipitation_probability": "rp0003i0",
}
# Ordered by time rather than point, so it is downloaded whole: see
# MeteoSwissOGD.get_hourly_temperature.
HOURLY_TEMPERATURE = "tre200h0"


class OGDError(Exception):
    """Data could not be obtained from MeteoSwiss."""


class UnknownPostcodeError(OGDError):
    """MeteoSwiss has no forecast for a postal code."""


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
    # Highest chance of rain of any three hours of the day, in %.
    precipitation_probability: float | None
    symbol: int | None  # MeteoSwiss pictogram of the day


class HourlyForecast(TypedDict):
    time: datetime.datetime  # UTC, start of the hour
    symbol: int | None
    temperature: float | None
    precipitation: float | None
    # Chance of rain in the three hours up to the end of this hour, in %.
    precipitation_probability: float | None
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


def _rows_with_needle(rows: list[bytes], data: bytes, needle: bytes) -> None:
    """Collect the lines of data that start right after a match of the needle."""
    start = data.find(needle)
    while start >= 0:
        end = data.find(b"\n", start + 1)
        rows.append(data[start + 1 : end if end >= 0 else None])
        if end < 0:
            break
        start = data.find(needle, end)


def _station_time(timestamp: str | None) -> datetime.datetime | None:
    """The time of a station row, e.g. 20.10.2026 11:40 (UTC)."""
    try:
        return datetime.datetime.strptime(timestamp or "", "%d.%m.%Y %H:%M").replace(
            tzinfo=datetime.timezone.utc
        )
    except ValueError:
        return None


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
            raise UnknownPostcodeError(f"No forecast point for postal code {postcode}")
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
        self,
        abbr: str,
        station_type: StationType,
        now: datetime.datetime | None = None,
    ) -> dict[str, float | None]:
        """The latest measurements of a station, empty if it has none recently."""
        low = abbr.lower()
        weather_url = f"{SMN_URL}/{low}/ogd-smn_{low}_t_now.csv"
        if station_type == StationType.WEATHER:
            body, _ = await self._get(weather_url)
        else:
            try:
                body, _ = await self._get(
                    f"{SMN_PRECIP_URL}/{low}/ogd-smn-precip_{low}_t_now.csv"
                )
            except OGDError as exc:
                # Some stations (such as BIA) moved from the precipitation
                # network to the weather network, which also measures rain.
                try:
                    body, _ = await self._get(weather_url)
                except OGDError:
                    raise exc from None
        rows = list(
            csv.DictReader(io.StringIO(body.decode("windows-1252")), delimiter=";")
        )
        now = now or datetime.datetime.now(datetime.timezone.utc)
        rows = [
            row
            for row in rows
            if (measured := _station_time(row.get("reference_timestamp")))
            and now - measured <= STATION_MAX_AGE
        ]
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
            return await self._streamed_point_values(url, prefix)
        return _rows_with_prefix(head.decode("latin-1"), prefix)

    async def _streamed_point_values(self, url: str, prefix: str) -> dict[str, str]:
        """Stream a whole file, keeping only the rows starting with the prefix."""
        needle = b"\n" + prefix.encode("latin-1")
        rows: list[bytes] = []
        # Carries the incomplete last line of a chunk, after its line break.
        tail = b""
        try:
            async with self._session.get(
                url, headers=self._headers, timeout=STREAM_TIMEOUT
            ) as response:
                response.raise_for_status()
                async for chunk in response.content.iter_chunked(STREAM_CHUNK):
                    data = tail + chunk
                    cut = data.rfind(b"\n")
                    if cut < 0:
                        tail = data
                        continue
                    _rows_with_needle(rows, data[:cut], needle)
                    tail = data[cut:]
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise OGDError(f"Cannot fetch {url}: {exc}") from exc
        _rows_with_needle(rows, tail, needle)
        return _rows_with_prefix(b"\n".join(rows).decode("latin-1"), prefix)

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

    async def get_hourly_temperature(self, point: Point, run: str) -> dict[str, str]:
        """The hourly temperatures of a point, as the raw rows for get_forecast.

        This downloads about 33 MB, so it is fetched separately from (and less
        often than) the rest of the forecast.
        """
        return await self._point_values(self._run_url(run, HOURLY_TEMPERATURE), point)

    async def get_forecast(
        self,
        point: Point,
        run: str,
        hourly_temperature: dict[str, str] | None = None,
    ) -> LocalForecast:
        """The daily and hourly forecast of a point for a model run.

        The hourly temperatures come from get_hourly_temperature, possibly of
        an earlier run.
        """
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
        hourly["temperature"] = hourly_temperature or {}

        # MeteoSwiss has no daily chance of rain, so take the highest of the day.
        daily_probability: dict[str, float] = {}
        for hour, value in hourly["precipitation_probability"].items():
            probability = _number(value)
            if probability is None:
                continue
            start = _utc(hour) - datetime.timedelta(hours=1)
            day = start.astimezone(LOCAL_TIME_ZONE).date().isoformat()
            daily_probability[day] = max(probability, daily_probability.get(day, 0))

        days = sorted(set().union(*(v.keys() for v in daily.values())))
        daily_out: list[DailyForecast] = [
            {
                "date": _utc(day).date().isoformat(),
                "temperature_min": _number(daily["temperature_min"].get(day)),
                "temperature_max": _number(daily["temperature_max"].get(day)),
                "precipitation": _number(daily["precipitation"].get(day)),
                "precipitation_probability": daily_probability.get(
                    _utc(day).date().isoformat()
                ),
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
                "temperature": _number(hourly["temperature"].get(hour)),
                "precipitation": _number(hourly["precipitation"].get(hour)),
                "precipitation_probability": _number(
                    hourly["precipitation_probability"].get(hour)
                ),
                "wind_speed": _number(hourly["wind_speed"].get(hour)),
                "wind_bearing": _number(hourly["wind_bearing"].get(hour)),
            }
            for hour in hours
        ]
        return {"run": run, "daily": daily_out, "hourly": hourly_out}
