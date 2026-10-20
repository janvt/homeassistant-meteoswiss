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
import time
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
# Mountains, huts, passes and a few regions.
POINT_TYPE_POI = 3

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
# A point is first looked for where it was in the previous run's file, which
# is laid out almost the same: this far before and after that position.
HINT_BEFORE = 4096
HINT_AFTER = 16384
# Files ordered by time up to this size are downloaded whole, compressed, once
# for all entries; larger ones are streamed (see get_hourly_rows).
SMALL_FILE = 8 * 1024 * 1024
# Station measurements and the latest run are shared by entries this long.
STATION_CACHE_SECONDS = 60
RUN_CACHE = datetime.timedelta(minutes=5)

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
    "wind_gust": "fu3010h1",
    # Fractions (0-1) of the sky covered by each layer.
    "cloud_low": "nprolohs",
    "cloud_medium": "npromths",
    "cloud_high": "nprohihs",
    # Minutes of sunshine in the hour; Home Assistant forecasts have no field
    # for it, so it is only used for sensors.
    "sunshine": "sre000h0",
}
# Ordered by time rather than point, so they are downloaded whole (about
# 33 MB each): see MeteoSwissOGD.get_hourly_rows.
HOURLY_TEMPERATURE = "tre200h0"
FREEZING_LEVEL = "zprfr0hs"


class OGDError(Exception):
    """Data could not be obtained from MeteoSwiss."""


class UnknownPointError(OGDError):
    """MeteoSwiss has no forecast for a configured place."""


class UnknownPostcodeError(UnknownPointError):
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
    height: float | None = None  # metres above sea level

    @property
    def key(self) -> tuple[int, int]:
        """IDs are only unique within a point type."""
        return (self.point_id, self.point_type)


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
    wind_gust: float | None  # strongest gust of the day, km/h
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
    wind_gust: float | None  # strongest gust of the hour, km/h
    cloud_coverage: float | None  # %, all layers together
    freezing_level: float | None  # metres above sea level
    sunshine: float | None  # minutes


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


# The orders of the files that are ordered by point, as sort keys of
# (point_id, point_type_id).  Some files are sorted by number; others by type,
# then by point ID as text (1, 10, 10101, ..., 2).
POINT_ORDERS = (
    lambda point_id, point_type: (point_id, point_type),
    lambda point_id, point_type: (point_type, str(point_id)),
)


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


def _cloud_coverage(*layers: float | None) -> float | None:
    """The share of the sky covered by any layer, in %.

    MeteoSwiss forecasts low, medium and high cloud separately, and no total,
    so the layers are assumed to overlap at random.
    """
    if any(layer is None for layer in layers):
        return None
    clear = 1.0
    for layer in layers:
        clear *= 1 - min(max(layer, 0.0), 1.0)  # type: ignore[arg-type]
    return round((1 - clear) * 100)


def _local_daily_max(values: dict[str, str]) -> dict[str, float]:
    """The highest hourly value of each local day, by ISO date."""
    highest: dict[str, float] = {}
    for hour, value in values.items():
        number = _number(value)
        if number is None:
            continue
        start = _utc(hour) - datetime.timedelta(hours=1)
        day = start.astimezone(LOCAL_TIME_ZONE).date().isoformat()
        highest[day] = max(number, highest.get(day, number))
    return highest


def current_hour(
    hourly: list[HourlyForecast], now: datetime.datetime
) -> HourlyForecast | None:
    """The forecast of the hour we are in."""
    current = None
    for forecast in hourly:
        if forecast["time"] > now:
            break
        current = forecast
    return current


def _parameter(url: str) -> str:
    """The parameter of a forecast file, e.g. jww003i0."""
    return url.rsplit(".", 2)[-2]


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
        # The index in POINT_ORDERS that worked last, by parameter.
        self._orders: dict[str, int] = {}
        # Parameters whose files are ordered by time rather than by point.
        self._by_time: set[str] = set()
        # Where each point's rows started in the last file, by (parameter, point).
        self._offsets: dict[tuple[str, tuple[int, int]], int] = {}
        # The last small file ordered by time, by parameter: its URL and text.
        self._small_files: dict[str, tuple[str, str]] = {}
        self._small_file_lock = asyncio.Lock()
        # Recently fetched station files: URL to (monotonic time, body).
        self._station_files: dict[str, tuple[float, bytes]] = {}
        self._latest_run: tuple[datetime.datetime, str] | None = None

    async def _get(
        self, url: str, byte_range: tuple[int, int] | None = None
    ) -> tuple[bytes, int]:
        """Fetch a URL (or a byte range of it), returning the body and total size."""
        headers = dict(self._headers)
        if byte_range:
            headers["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
            # The server ignores ranges of files it would compress and sends
            # them whole instead.
            headers["Accept-Encoding"] = "identity"
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
                    height=_number(row.get("point_height_masl")),
                )
                for row in reader
                if row["point_id"]
            ]
        return self._points

    async def get_poi_points(self, lat: float, lon: float) -> list[Point]:
        """Mountains, huts and passes, closest first."""
        return sorted(
            (p for p in await self.get_points() if p.point_type == POINT_TYPE_POI),
            key=lambda p: distance_km(lat, lon, p.lat, p.lon),
        )

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
        self,
        postcode: str | int | None,
        point_id: int | None = None,
        point_type: int = POINT_TYPE_POSTCODE,
    ) -> Point:
        """The forecast point for a configuration.

        Mountains are configured by point. For postal codes an explicit point
        ID wins; otherwise it is the first point of the postal code.
        """
        if point_type != POINT_TYPE_POSTCODE:
            for point in await self.get_points():
                if point.key == (point_id, point_type):
                    return point
            raise UnknownPointError(
                f"No forecast point {point_id} of type {point_type}"
            )
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
            body = await self._get_station_file(weather_url)
        else:
            try:
                body = await self._get_station_file(
                    f"{SMN_PRECIP_URL}/{low}/ogd-smn-precip_{low}_t_now.csv"
                )
            except OGDError as exc:
                # Some stations (such as BIA) moved from the precipitation
                # network to the weather network, which also measures rain.
                try:
                    body = await self._get_station_file(weather_url)
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

    async def _get_station_file(self, url: str) -> bytes:
        """A station file, shared by entries that use the same station."""
        cached = self._station_files.get(url)
        if cached and time.monotonic() - cached[0] < STATION_CACHE_SECONDS:
            return cached[1]
        body, _ = await self._get(url)
        self._station_files[url] = (time.monotonic(), body)
        return body

    # Forecasts

    @staticmethod
    def _run_url(run: str, parameter: str) -> str:
        return f"{FORECAST_URL}/{run[:8]}-ch/vnut12.lssw.{run}.{parameter}.csv"

    async def latest_run(self, now: datetime.datetime | None = None) -> str:
        """The most recent published model run, e.g. 202610201100."""
        now = now or datetime.datetime.now(datetime.timezone.utc)
        # abs(): the clock may have been set back.
        if self._latest_run and abs(now - self._latest_run[0]) < RUN_CACHE:
            return self._latest_run[1]
        run = await self._find_latest_run(now)
        self._latest_run = (now, run)
        return run

    async def _find_latest_run(self, now: datetime.datetime) -> str:
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
        parameter = _parameter(url)
        if parameter in self._by_time:
            return await self._time_ordered_point_values(url, prefix, None)
        if (hint := self._offsets.get((parameter, point.key))) is not None:
            rows = await self._hinted_point_values(url, point, prefix, hint)
            if rows:
                return rows
        head, total = await self._get(url, (0, HEAD_BYTES - 1))
        if total > len(head):
            lines = head.decode("latin-1").splitlines()[1:3]
            keys = [_key(line) for line in lines]
            if len(keys) == 2 and keys[0] is not None and keys[0] == keys[1]:
                return await self._ordered_point_values(url, total, point, prefix)
            self._by_time.add(parameter)
            return await self._time_ordered_point_values(url, prefix, total)
        return _rows_with_prefix(head.decode("latin-1"), prefix)

    async def _time_ordered_point_values(
        self, url: str, prefix: str, total: int | None
    ) -> dict[str, str]:
        """The rows of a point in a file ordered by time.

        Small files are downloaded whole (compressed) once for all entries;
        large ones are streamed.
        """
        parameter = _parameter(url)
        if total is not None and total > SMALL_FILE:
            self._by_time.discard(parameter)
            return (await self._streamed_point_values(url, [prefix]))[prefix]
        async with self._small_file_lock:
            cached = self._small_files.get(parameter)
            if cached is None or cached[0] != url:
                body, _ = await self._get(url)
                cached = (url, body.decode("latin-1"))
                self._small_files[parameter] = cached
        return _rows_with_prefix(cached[1], prefix)

    async def _hinted_point_values(
        self, url: str, point: Point, prefix: str, offset: int
    ) -> dict[str, str]:
        """The rows of a point near where they were before, if all are there."""
        start = max(0, offset - HINT_BEFORE)
        end = offset + HINT_AFTER
        try:
            chunk, total = await self._get(url, (start, end - 1))
        except OGDError:
            return {}
        lines = chunk.split(b"\n")
        if start > 0:
            lines = lines[1:]  # starts mid-line
        if start + len(chunk) < total:
            lines = lines[:-1]  # ends mid-line
        keys = [_key(line) for line in lines]
        found = [i for i, key in enumerate(keys) if key == point.key]
        # Only if the rows before and after the point's are in the window too.
        if (
            not found
            or found[0] == 0
            or (found[-1] == len(keys) - 1 and start + len(chunk) < total)
        ):
            return {}
        self._remember_offset(url, point, chunk, start, prefix)
        return _rows_with_prefix(b"\n".join(lines).decode("latin-1"), prefix)

    def _remember_offset(
        self, url: str, point: Point, chunk: bytes, start: int, prefix: str
    ) -> None:
        position = chunk.find(b"\n" + prefix.encode("latin-1"))
        if position >= 0:
            self._offsets[(_parameter(url), point.key)] = start + position + 1

    async def _streamed_point_values(
        self, url: str, prefixes: list[str]
    ) -> dict[str, dict[str, str]]:
        """Stream a whole file, keeping only the rows starting with the prefixes."""
        needles = {prefix: b"\n" + prefix.encode("latin-1") for prefix in prefixes}
        rows: dict[str, list[bytes]] = {prefix: [] for prefix in prefixes}
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
                    for prefix, needle in needles.items():
                        _rows_with_needle(rows[prefix], data[:cut], needle)
                    tail = data[cut:]
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise OGDError(f"Cannot fetch {url}: {exc}") from exc
        for prefix, needle in needles.items():
            _rows_with_needle(rows[prefix], tail, needle)
        return {
            prefix: _rows_with_prefix(b"\n".join(found).decode("latin-1"), prefix)
            for prefix, found in rows.items()
        }

    async def _ordered_point_values(
        self, url: str, total: int, point: Point, prefix: str
    ) -> dict[str, str]:
        """Find a point in a file ordered by point, trying the known orders."""
        parameter = _parameter(url)
        last = self._orders.get(parameter, 0)
        for index in (last, *(i for i in range(len(POINT_ORDERS)) if i != last)):
            rows = await self._ranged_point_values(
                url, total, point, prefix, POINT_ORDERS[index]
            )
            if rows:
                self._orders[parameter] = index
                return rows
        _LOGGER.debug("Point %s not found in %s", point.key, url)
        return {}

    async def _ranged_point_values(
        self, url: str, total: int, point: Point, prefix: str, order
    ) -> dict[str, str]:
        """Binary search a file ordered by point, in the given order."""
        target = order(point.point_id, point.point_type)
        # The first row of the target starts after the first line break past
        # `lo` and no later than the first line break past `hi`.
        lo, hi = 0, total
        while hi - lo > SEARCH_WINDOW:
            mid = (lo + hi) // 2
            chunk, _ = await self._get(url, (mid, min(mid + PROBE_BYTES, total) - 1))
            newline = chunk.find(b"\n")
            key = _key(chunk[newline + 1 :].split(b"\n")[0]) if newline >= 0 else None
            if key is not None and order(*key) < target:
                lo = mid
            else:
                hi = mid
        end = min(hi + ROW_SPAN, total)
        chunk, _ = await self._get(url, (lo, end - 1))
        self._remember_offset(url, point, chunk, lo, prefix)
        lines = chunk.split(b"\n")
        if lo > 0:
            lines = lines[1:]  # starts mid-line
        if end < total:
            lines = lines[:-1]  # ends mid-line
        return _rows_with_prefix(
            b"\n".join(lines).decode("latin-1"),
            prefix,
        )

    async def get_hourly_rows(
        self, points: list[Point], run: str, parameter: str
    ) -> dict[tuple[int, int], dict[str, str]]:
        """The rows of several points in a file ordered by time, for get_forecast.

        Such a file is downloaded whole (about 33 MB), so it is fetched
        separately from (and less often than) the rest of the forecast, once
        for all points that need it.
        """
        prefixes = {p.key: f"{p.point_id};{p.point_type};" for p in points}
        found = await self._streamed_point_values(
            self._run_url(run, parameter), list(prefixes.values())
        )
        return {key: found[prefix] for key, prefix in prefixes.items()}

    async def get_forecast(
        self,
        point: Point,
        run: str,
        hourly_temperature: dict[str, str] | None = None,
        freezing_level: dict[str, str] | None = None,
    ) -> LocalForecast:
        """The daily and hourly forecast of a point for a model run.

        Hourly temperatures and the freezing level come from get_hourly_rows,
        possibly of an earlier run.
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
        hourly["freezing_level"] = freezing_level or {}

        # MeteoSwiss has no daily chance of rain or gusts: take the highest.
        daily_probability = _local_daily_max(hourly["precipitation_probability"])
        daily_gust = _local_daily_max(hourly["wind_gust"])

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
                "wind_gust": daily_gust.get(_utc(day).date().isoformat()),
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
                "wind_gust": _number(hourly["wind_gust"].get(hour)),
                "cloud_coverage": _cloud_coverage(
                    _number(hourly["cloud_low"].get(hour)),
                    _number(hourly["cloud_medium"].get(hour)),
                    _number(hourly["cloud_high"].get(hour)),
                ),
                "freezing_level": _number(hourly["freezing_level"].get(hour)),
                "sunshine": _number(hourly["sunshine"].get(hour)),
            }
            for hour in hours
        ]
        return {"run": run, "daily": daily_out, "hourly": hourly_out}
