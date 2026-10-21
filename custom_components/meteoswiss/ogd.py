"""Client for the MeteoSwiss Open Government Data (OGD) CSV files.

Forecasts: https://opendatadocs.meteoswiss.ch/e-forecast-data/e4-local-forecast-data
Measurements: https://opendatadocs.meteoswiss.ch/a-data-groundbased

Source: MeteoSwiss.
"""

from __future__ import annotations

import asyncio
import collections
import csv
import datetime
import io
import logging
import math
import time
import zoneinfo
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypedDict

import aiohttp

from custom_components.meteoswiss import climate

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://data.geo.admin.ch"
FORECAST_URL = BASE_URL + "/ch.meteoschweiz.ogd-local-forecasting"
POINTS_URL = FORECAST_URL + "/ogd-local-forecasting_meta_point.csv"
SMN_URL = BASE_URL + "/ch.meteoschweiz.ogd-smn"
SMN_PRECIP_URL = BASE_URL + "/ch.meteoschweiz.ogd-smn-precip"
POLLEN_URL = BASE_URL + "/ch.meteoschweiz.ogd-pollen"

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
# is laid out almost the same.  Rows move as the values before them get longer
# or shorter, by up to the change in the file's size: when that was small, the
# window is the point's rows plus this margin and twice that change either
# side; otherwise the file's size is asked for first, the position scaled by
# it, and the margin is this plus a share of the change.
HINT_MARGIN = 512
# Up to this change in size, a file is read without asking for its size first.
HINT_SCALE_FROM = 1024
HINT_SCALED_SHARE = 0.4
# Files ordered by time hold a block of rows per timestep (217 hours or 9
# days), with the points in the same order in each block.  A point's row is
# read from each block with a small range request where it is expected, from
# its position in the previous file or in the blocks before.  The first read
# of the day takes the first block whole, to learn the order of the points.
BLOCK_HEAD = 192 * 1024
# Bytes either side of where a row is expected, at first; then adapted to how
# far off the guesses were in the previous file.  Each request costs about
# 650 bytes of headers, and a row that is missed is found with one more, so
# the margin covers only most of the guesses (this share of them).
BLOCK_MARGIN = 512
BLOCK_MARGIN_RANGE = (256, 8192)
BLOCK_MARGIN_SHARE = 0.75
BLOCK_ROW = 64  # longer than any row
# A row not found in a few reads, each closer, is given up on.
BLOCK_TRIES = 4
BLOCK_LIMIT = 1000  # more blocks than any file has
# Files ordered by time that cannot be read by block are downloaded whole:
# compressed if up to this size, otherwise streamed (see get_hourly_rows).
SMALL_FILE = 8 * 1024 * 1024
# Searching a file stops, and it is downloaded whole, as soon as the search has
# cost as much as that would; and it does not start if it is expected to.
# Each request costs about this many bytes of headers, and whole files up to
# SMALL_FILE are sent compressed, to about this share of their size.
REQUEST_OVERHEAD = 650
COMPRESSED_SHARE = 0.17
# A cold start costs the head and then walking the blocks: about this much
# more than the head alone, at most.
COLD_START_SHARE = 1.25
# What was downloaded is counted over this long, for the repair notice when
# it is far more than usual.
USAGE_PERIOD = datetime.timedelta(hours=24)
# Station measurements and the latest run are shared by entries this long.
STATION_CACHE_SECONDS = 60
RUN_CACHE = datetime.timedelta(minutes=5)

# Station files have a row every 10 minutes.
STATION_ROWS_LOOKBACK = 3
# Rows appear 20-40 minutes late; anything much older means the station has
# stopped reporting.
STATION_MAX_AGE = datetime.timedelta(minutes=90)
# Pollen stations report hourly means, also about half an hour late.
POLLEN_MAX_AGE = datetime.timedelta(hours=6)
POLLEN_CACHE_SECONDS = 10 * 60
# A station's daily history: up to the end of last year (about 6 MB, changing
# once a year), and the days of this year.
CLIMATE_HISTORY_REFRESH = datetime.timedelta(days=7)
CLIMATE_RECENT_REFRESH = datetime.timedelta(hours=6)

# Hourly mean concentrations (pollen per m³) by pollen type.
POLLEN_PARAMETERS = {
    "alder": "kaalnuh0",
    "ash": "kafraxh0",
    "beech": "kafaguh0",
    "birch": "kabetuh0",
    "grasses": "khpoach0",
    "hazel": "kacoryh0",
    "oak": "kaquerh0",
}

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


class RangesIgnoredError(OGDError):
    """The server sends whole files when asked for part of them.

    The forecast is then not read at all: it would mean downloading several
    hundred megabytes an hour.
    """


class StationType(str, Enum):
    """Station type."""

    WEATHER = "weather"
    PRECIPITATION = "precipitation"
    POLLEN = "pollen"


# Where the files of each type of station are, and how they are named.
STATION_FILES = {
    StationType.WEATHER: (SMN_URL, "ogd-smn"),
    StationType.PRECIPITATION: (SMN_PRECIP_URL, "ogd-smn-precip"),
    StationType.POLLEN: (POLLEN_URL, "ogd-pollen"),
}


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


def _row(line: bytes) -> tuple[tuple[int, int], str, str] | None:
    """The point, timestamp and value of a CSV row, None for a header or junk."""
    parts = line.decode("latin-1").split(";", 3)
    try:
        return (int(parts[0]), int(parts[1])), parts[2], parts[3]
    except (ValueError, IndexError):
        return None


def _whole_rows(
    chunk: bytes, start: int, total: int
) -> list[tuple[int, tuple[int, int], str, str]]:
    """The complete rows of a chunk read from start: position, point, time, value."""
    parts = chunk.split(b"\n")
    rows = []
    position = start
    for i, line in enumerate(parts):
        # The first line may have begun before the chunk, the last may go on.
        whole = (i > 0 or start == 0) and (
            i < len(parts) - 1 or start + len(chunk) >= total
        )
        if whole and (row := _row(line)):
            rows.append((position, *row))
        position += len(line) + 1
    return rows


@dataclass
class _Blocks:
    """The layout of a file ordered by time: a block of rows per timestep."""

    date: str  # the day of the runs it was read for, e.g. 20261020
    order: dict[tuple[int, int], int]  # the place of each point in a block
    first: datetime.datetime  # the time of the first block
    step: datetime.timedelta
    start: int  # where the first block starts
    length: int  # bytes of the first block
    margin: int = BLOCK_MARGIN
    # What following a point through the blocks cost the last time, in bytes.
    cost: int | None = None

    def time(self, block: int) -> str:
        return (self.first + block * self.step).strftime("%Y%m%d%H%M")

    def blocks_between(self, earlier: str, later: str) -> int | None:
        ahead = (_utc(later) - _utc(earlier)) / self.step
        return int(ahead) if ahead == int(ahead) else None


class _Lost(Exception):
    """A point's row could not be found in a block."""


class _OverBudget(_Lost):
    """Searching has cost as much as downloading the whole file."""


def _whole_cost(size: int) -> int:
    """About how many bytes downloading a whole file costs."""
    return size if size > SMALL_FILE else int(size * COMPRESSED_SHARE)


class _Budget:
    """The bytes a search may cost before the whole file is the cheaper way."""

    def __init__(self, size: int | None) -> None:
        self.limit = None if size is None else _whole_cost(size)
        self.spent = 0

    def spend(self, received: int, size: int) -> None:
        if self.limit is None:
            self.limit = _whole_cost(size)
        self.spent += received + REQUEST_OVERHEAD
        if self.spent > self.limit:
            raise _OverBudget


def _parse_blocks(
    head: bytes, total: int, date: str
) -> tuple[_Blocks, dict[tuple[int, int], dict[str, tuple[int, str]]]] | None:
    """The layout of a file from its first bytes, and the rows read already.

    None if the first block is not complete in them, or not laid out in blocks.
    """
    rows = _whole_rows(head, 0, total)
    if not rows:
        return None
    first_time = rows[0][2]
    count = next((i for i, r in enumerate(rows) if r[2] != first_time), len(rows))
    block = rows[:count]
    order = {r[1]: i for i, r in enumerate(block)}
    # The first block must be complete, and the next start with the same point.
    if (
        count < 2
        or count == len(rows)
        or len(order) != count
        or rows[count][1] != block[0][1]
    ):
        return None
    after = rows[count]
    try:
        step = _utc(after[2]) - _utc(first_time)
    except ValueError:
        return None
    if step <= datetime.timedelta(0):
        return None
    blocks = _Blocks(
        date, order, _utc(first_time), step, block[0][0], after[0] - block[0][0]
    )
    known: dict[tuple[int, int], dict[str, tuple[int, str]]] = {}
    for position, key, timestamp, value in rows:
        known.setdefault(key, {})[timestamp] = (position, value)
    return blocks, known


def _guess_position(
    blocks: _Blocks,
    key: tuple[int, int],
    positions: dict[str, int],
    old: dict[str, int],
    block: int,
) -> int | None:
    """Where a point's row in a block probably is, from the rows found already.

    Blocks differ in length by a few kB, in much the same way from one run to
    the next, so a row is expected as far from its row in the block before as
    it was in the previous file; or, for a file read for the first time, as
    far as the row before is from the one before that.
    """
    timestamp = blocks.time(block)
    if block == 0:
        if timestamp in old:
            return old[timestamp]
        # A point not read before: as far into the block as it is in the order.
        share = blocks.order[key] / len(blocks.order)
        return blocks.start + round(share * blocks.length)
    before = blocks.time(block - 1)
    if before not in positions:
        return None
    if timestamp in old and before in old:
        return old[timestamp] + positions[before] - old[before]
    if (earlier := blocks.time(block - 2)) in positions:
        return 2 * positions[before] - positions[earlier]
    return positions[before] + blocks.length


def _run_of(url: str) -> str:
    """The run of a forecast file, e.g. 202610201100."""
    return url.rsplit(".", 3)[-3]


def _rows_with_prefix(text: str, prefix: str) -> dict[str, str]:
    """Map timestamp to value for all rows starting with the prefix."""
    rows = {}
    for line in text.splitlines():
        if line.startswith(prefix):
            parts = line.split(";")
            if len(parts) >= 4:
                rows[parts[2]] = parts[3]
    return rows


def _rows_with_needle(
    rows: list[bytes], data: bytes, needle: bytes
) -> tuple[int, int] | None:
    """Collect the lines of data that start right after a match of the needle.

    Returns where the first of them starts and the last ends, if any.
    """
    start = data.find(needle)
    found = None
    while start >= 0:
        end = data.find(b"\n", start + 1)
        rows.append(data[start + 1 : end if end >= 0 else None])
        found = (found[0] if found else start + 1, end + 1 if end >= 0 else len(data))
        if end < 0:
            break
        start = data.find(needle, end)
    return found


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
        # Where each point's rows started in the last file it was read from,
        # by (parameter, point), with that file's size; and how many bytes
        # the rows took.
        self._offsets: dict[tuple[str, tuple[int, int]], tuple[int, int]] = {}
        self._spans: dict[tuple[str, tuple[int, int]], int] = {}
        # The latest file of each parameter and its size, and how much its
        # size changed from the file before.
        self._files: dict[str, tuple[str, int]] = {}
        self._size_changes: dict[str, int] = {}
        # Files ordered by time, by parameter: their layout, and where each
        # point's row was in each block of the last file, by time.
        self._blocks: dict[str, _Blocks] = {}
        self._block_positions: dict[tuple[str, tuple[int, int]], dict[str, int]] = {}
        # The day each point was last looked for in a whole file ordered by
        # point, by (parameter, point).
        self._whole_reads: dict[tuple[str, tuple[int, int]], str] = {}
        # The last small file ordered by time, by parameter: its URL and text.
        self._small_files: dict[str, tuple[str, str]] = {}
        self._small_file_lock = asyncio.Lock()
        # The ETag and Last-Modified of files fetched whole, by URL.
        self._validators: dict[str, tuple[str | None, str | None]] = {}
        # Small files ordered by time, downloaded whole on a cold start, by
        # parameter: the URL and the file, until the next run's is read.
        self._block_files: dict[str, tuple[str, bytes]] = {}
        # When files were downloaded whole because they could not be searched.
        self._whole_downloads: list[float] = []
        # When each response came, and what it cost in bytes.
        self._received: collections.deque[tuple[float, int]] = collections.deque()
        # Whether the server sent a whole file when asked for part of it, the
        # last time it was.
        self.ranges_ignored = False
        # Recently fetched station files: URL to (monotonic time, body).
        self._station_files: dict[str, tuple[float, bytes]] = {}
        self._latest_run: tuple[datetime.datetime, str] | None = None
        # By station and file: when it was last checked, when it last changed,
        # and its days.
        self._climate_files: dict[
            tuple[str, str],
            tuple[datetime.datetime, datetime.datetime, list[climate.Day]],
        ] = {}
        # By station: when the files its normals were computed from last
        # changed, and the normals.
        self._climates: dict[
            str, tuple[tuple[datetime.datetime, ...], climate.Climate]
        ] = {}
        self._climate_lock = asyncio.Lock()

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
                if byte_range:
                    # Not read: it is the whole file.
                    self.ranges_ignored = response.status != 206
                    if self.ranges_ignored:
                        self._count(response, 0)
                        raise RangesIgnoredError(f"{url} was sent whole")
                body = await response.read()
                self._count(response, len(body))
                total = len(body)
                content_range = response.headers.get("Content-Range", "")
                if "/" in content_range:
                    total = int(content_range.rsplit("/", 1)[1])
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise OGDError(f"Cannot fetch {url}: {exc}") from exc
        return body, total

    async def _get_changed(self, url: str) -> bytes | None:
        """Fetch a whole file, or None if it has not changed since the last time."""
        headers = dict(self._headers)
        validators = self._validators.get(url)
        if validators:
            etag, modified = validators
            if etag:
                headers["If-None-Match"] = etag
            if modified:
                headers["If-Modified-Since"] = modified
        try:
            async with self._session.get(
                url, headers=headers, timeout=REQUEST_TIMEOUT
            ) as response:
                if response.status == 304 and validators:
                    self._count(response, 0)
                    return None
                response.raise_for_status()
                body = await response.read()
                self._count(response, len(body))
                self._validators[url] = (
                    response.headers.get("ETag"),
                    response.headers.get("Last-Modified"),
                )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise OGDError(f"Cannot fetch {url}: {exc}") from exc
        return body

    async def _size(self, url: str) -> int | None:
        """The size of a file, uncompressed, None if the server does not say."""
        headers = {**self._headers, "Accept-Encoding": "identity"}
        try:
            async with self._session.head(
                url, headers=headers, timeout=REQUEST_TIMEOUT
            ) as response:
                self._count(response, 0)
                response.raise_for_status()
                return int(response.headers["Content-Length"])
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError):
            return None

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
            base, prefix = STATION_FILES[station_type]
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

    async def get_pollen_now(
        self, abbr: str, now: datetime.datetime | None = None
    ) -> dict[str, Any]:
        """The latest hourly pollen concentrations of a station, by pollen type.

        Empty if the station has reported nothing recently.  Also has the time
        of the measurement as "measured": like all station data, timestamps
        mark the end of the interval.
        """
        low = abbr.lower()
        body = await self._get_station_file(
            f"{POLLEN_URL}/{low}/ogd-pollen_{low}_h_now.csv", POLLEN_CACHE_SECONDS
        )
        rows = list(
            csv.DictReader(io.StringIO(body.decode("windows-1252")), delimiter=";")
        )
        now = now or datetime.datetime.now(datetime.timezone.utc)
        for row in reversed(rows):
            measured = _station_time(row.get("reference_timestamp"))
            if measured is None or now - measured > POLLEN_MAX_AGE:
                break
            values = {
                kind: _number(row.get(parameter))
                for kind, parameter in POLLEN_PARAMETERS.items()
            }
            if any(v is not None for v in values.values()):
                return {**values, "measured": measured}
        return {}

    async def get_station_today(
        self, abbr: str, now: datetime.datetime | None = None
    ) -> dict[str, float | None]:
        """The highest and lowest temperature of a weather station today (UTC).

        From the same file as get_station_now, so usually without a request.
        """
        low = abbr.lower()
        body = await self._get_station_file(f"{SMN_URL}/{low}/ogd-smn_{low}_t_now.csv")
        now = now or datetime.datetime.now(datetime.timezone.utc)
        temperatures = [
            value
            for row in csv.DictReader(
                io.StringIO(body.decode("windows-1252")), delimiter=";"
            )
            if (measured := _station_time(row.get("reference_timestamp")))
            and measured.date() == now.date()
            and (value := _number(row.get("tre200s0"))) is not None
        ]
        return {
            "high": max(temperatures, default=None),
            "low": min(temperatures, default=None),
        }

    async def _climate_days(
        self, abbr: str, kind: str, refresh: datetime.timedelta, now: datetime.datetime
    ) -> list[climate.Day]:
        cached = self._climate_files.get((abbr, kind))
        if cached and abs(now - cached[0]) < refresh:
            return cached[2]
        low = abbr.lower()
        url = f"{SMN_URL}/{low}/ogd-smn_{low}_d_{kind}.csv"
        if cached is None:
            self._validators.pop(url, None)
        body = await self._get_changed(url)
        if body is None and cached:
            # Unchanged, and so are the normals computed from it.
            self._climate_files[(abbr, kind)] = (now, cached[1], cached[2])
            return cached[2]
        days = await asyncio.get_running_loop().run_in_executor(
            None, climate.parse_days, (body or b"").decode("windows-1252")
        )
        self._climate_files[(abbr, kind)] = (now, now, days)
        return days

    async def get_climate(
        self, abbr: str, now: datetime.datetime | None = None
    ) -> tuple[climate.Climate, list[climate.Day]]:
        """A weather station's normals and records, and this year's days.

        Shared by all entries with the station; the history is fetched once a
        week, this year's days every six hours.
        """
        now = now or datetime.datetime.now(datetime.timezone.utc)
        async with self._climate_lock:
            history = await self._climate_days(
                abbr, "historical", CLIMATE_HISTORY_REFRESH, now
            )
            recent = await self._climate_days(
                abbr, "recent", CLIMATE_RECENT_REFRESH, now
            )
            key = tuple(
                self._climate_files[(abbr, k)][1] for k in ("historical", "recent")
            )
            cached = self._climates.get(abbr)
            if cached is None or cached[0] != key:
                computed = await asyncio.get_running_loop().run_in_executor(
                    None, climate.compute, history + recent
                )
                self._climates[abbr] = cached = (key, computed)
            return cached[1], recent

    async def _get_station_file(
        self, url: str, cache_seconds: float = STATION_CACHE_SECONDS
    ) -> bytes:
        """A station file, shared by entries that use the same station."""
        cached = self._station_files.get(url)
        if cached and time.monotonic() - cached[0] < cache_seconds:
            return cached[1]
        if cached is None:
            self._validators.pop(url, None)
        # A new row only every 10 minutes (or hour), so often unchanged.
        body = await self._get_changed(url)
        if body is None:
            body = cached[1]  # type: ignore[index]
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
                    self._count(response, 0)
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
            return (await self._blocked_rows(url, [point]))[point.key]
        if (hint := self._offsets.get((parameter, point.key))) is not None:
            rows = await self._hinted_point_values(url, point, prefix, *hint)
            if rows:
                return rows
        head, total = await self._get(url, (0, HEAD_BYTES - 1))
        self._note_size(url, total)
        if total > len(head):
            lines = head.decode("latin-1").splitlines()[1:3]
            keys = [_key(line) for line in lines]
            if len(keys) == 2 and keys[0] is not None and keys[0] == keys[1]:
                return await self._ordered_point_values(url, total, point, prefix)
            self._by_time.add(parameter)
            return (await self._blocked_rows(url, [point]))[point.key]
        return _rows_with_prefix(head.decode("latin-1"), prefix)

    def _note_size(self, url: str, total: int) -> None:
        """Remember a file's size, and how much it changed from the one before."""
        parameter = _parameter(url)
        last = self._files.get(parameter)
        if last is None or last[0] != url:
            if last is not None:
                self._size_changes[parameter] = total - last[1]
            self._files[parameter] = (url, total)

    def _known_size(self, url: str) -> int | None:
        """The size of a file, if it is known without asking."""
        last = self._files.get(_parameter(url))
        return last[1] if last is not None and last[0] == url else None

    async def _time_ordered_point_values(
        self, url: str, prefixes: list[str], total: int | None
    ) -> dict[str, dict[str, str]]:
        """The rows of points in a file ordered by time, downloading it whole.

        Small files are downloaded compressed, once for all entries; large ones
        are streamed.
        """
        parameter = _parameter(url)
        if total is None:
            total = await self._size(url)
        if total is None or total > SMALL_FILE:
            return await self._streamed_point_values(url, prefixes)
        async with self._small_file_lock:
            cached = self._small_files.get(parameter)
            if cached is None or cached[0] != url:
                body, _ = await self._get(url)
                cached = (url, body.decode("latin-1"))
                self._small_files[parameter] = cached
        return {prefix: _rows_with_prefix(cached[1], prefix) for prefix in prefixes}

    async def _blocked_rows(
        self, url: str, points: list[Point]
    ) -> dict[tuple[int, int], dict[str, str]]:
        """The rows of points in a file ordered by time, read block by block.

        Points that cannot be followed through the blocks are followed again
        with the order of the points read afresh, and if that fails too, or
        costs as much as the whole file, are looked up in the whole file.
        """
        parameter = _parameter(url)
        for files in (self._block_files, self._small_files):
            if (cached := files.get(parameter)) is not None and cached[0] != url:
                del files[parameter]  # an earlier run's
        if (whole := self._block_files.get(parameter)) is not None:
            # Downloaded whole for this run already.
            return self._rows_from_whole(parameter, points, whole[1])
        size = self._known_size(url) or (self._files.get(parameter) or (url, None))[1]
        blocks = self._blocks.get(parameter)
        # The order of the points is read once a day; points not read before
        # are found from it.
        fresh = blocks is None or blocks.date != _run_of(url)[:8]
        if fresh:
            if self._known_size(url) is None and (size := await self._size(url)):
                self._note_size(url, size)
            if size is not None and _whole_cost(size) <= COLD_START_SHARE * BLOCK_HEAD:
                # Learning the order would cost as much as the whole file.
                return await self._small_blocked_rows(url, points)
        if size is not None and blocks is not None and blocks.cost is not None:
            expected = len(points) * blocks.cost + (BLOCK_HEAD if fresh else 0)
            if expected > _whole_cost(size):
                _LOGGER.debug("%s is cheaper whole for %d points", url, len(points))
                return await self._whole_file_rows(url, points, size)
        budget = _Budget(size)
        found: dict[tuple[int, int], dict[str, str]] = {}
        todo = list(points)
        for attempt in range(2):
            known: dict[tuple[int, int], dict[str, tuple[int, str]]] = {}
            if fresh or attempt:
                try:
                    learned = await self._learn_blocks(url, budget)
                except _OverBudget:
                    break
                if learned is None:
                    _LOGGER.debug("%s cannot be read by block", url)
                    break
                blocks, known, size, small = learned
                if small:
                    # A small file: all of it was read already.
                    for p in todo:
                        found[p.key] = {
                            t: v for t, (_, v) in known.get(p.key, {}).items()
                        }
                    todo = []
                    break
            assert blocks is not None
            todo, over = await self._walk_points(
                url, blocks, todo, known, size, budget, found
            )
            if not todo or over:
                break
            _LOGGER.debug("Lost %s in %s, reading the order again", todo, url)
        for point in points:
            if point not in todo:
                found.setdefault(point.key, {})
        if todo:
            _LOGGER.info("Reading %s whole for %s", url, [p.key for p in todo])
            self._note_whole_download()
            found.update(await self._whole_file_rows(url, todo, size))
        return found

    async def _small_blocked_rows(
        self, url: str, points: list[Point]
    ) -> dict[tuple[int, int], dict[str, str]]:
        """Download a small file ordered by time whole, learning its layout."""
        parameter = _parameter(url)
        body, total = await self._get(url)
        self._note_size(url, total)
        parsed = _parse_blocks(body, total, _run_of(url)[:8])
        if parsed is None:
            _LOGGER.info("%s cannot be read by block", url)
            self._note_whole_download()
            text = body.decode("latin-1")
            return {
                p.key: _rows_with_prefix(text, f"{p.point_id};{p.point_type};")
                for p in points
            }
        self._adopt_blocks(parameter, parsed[0])
        self._block_files[parameter] = (url, body)
        return self._rows_from_whole(parameter, points, body)

    def _rows_from_whole(
        self, parameter: str, points: list[Point], body: bytes
    ) -> dict[tuple[int, int], dict[str, str]]:
        """The rows of points in a whole file, remembering where they were."""
        found = {}
        for point in points:
            needle = f"\n{point.point_id};{point.point_type};".encode("latin-1")
            values: dict[str, str] = {}
            positions: dict[str, int] = {}
            start = body.find(needle)
            while start >= 0:
                end = body.find(b"\n", start + 1)
                if row := _row(body[start + 1 : end if end >= 0 else None]):
                    positions[row[1]] = start + 1
                    values[row[1]] = row[2]
                start = body.find(needle, end) if end >= 0 else -1
            found[point.key] = values
            if positions:
                self._block_positions[(parameter, point.key)] = positions
        return found

    def _adopt_blocks(self, parameter: str, blocks: _Blocks) -> None:
        """Use a newly read layout, keeping what was learnt about the file."""
        old = self._blocks.get(parameter)
        if old is not None:
            blocks.margin, blocks.cost = old.margin, old.cost
            if old.date != blocks.date:
                # Where the rows were in another day's file says little.
                for key in [k for k in self._block_positions if k[0] == parameter]:
                    del self._block_positions[key]
        self._blocks[parameter] = blocks

    def _note_whole_download(self) -> None:
        self._whole_downloads.append(time.monotonic())

    @property
    def whole_downloads(self) -> int:
        """How many files were downloaded whole lately, as they could not be
        searched (not those that were cheaper whole)."""
        since = time.monotonic() - USAGE_PERIOD.total_seconds()
        self._whole_downloads = [t for t in self._whole_downloads if t >= since]
        return len(self._whole_downloads)

    def _count(self, response: aiohttp.ClientResponse, body: int) -> None:
        """Remember what a response cost, as transferred if it can be told."""
        raw = getattr(response.content, "total_raw_bytes", None)
        size = raw if isinstance(raw, int) else body
        self._received.append((time.monotonic(), REQUEST_OVERHEAD + size))

    @property
    def downloaded(self) -> int:
        """Bytes downloaded from MeteoSwiss in the last USAGE_PERIOD."""
        since = time.monotonic() - USAGE_PERIOD.total_seconds()
        while self._received and self._received[0][0] < since:
            self._received.popleft()
        return sum(size for _, size in self._received)

    async def _learn_blocks(self, url: str, budget: _Budget):
        """Read the first block of a file for the order of the points.

        Returns the layout, the rows read, the file's size and whether that was
        all of it; None if the file is not laid out in blocks.
        """
        parameter = _parameter(url)
        head, total = await self._get(url, (0, BLOCK_HEAD - 1))
        self._note_size(url, total)
        if total > len(head):  # otherwise the whole file is in hand already
            budget.spend(len(head), total)
        parsed = _parse_blocks(head, total, _run_of(url)[:8])
        if parsed is None:
            return None
        blocks, known = parsed
        self._adopt_blocks(parameter, blocks)
        return blocks, known, total, total <= len(head)

    async def _walk_points(
        self,
        url: str,
        blocks: _Blocks,
        points: list[Point],
        known: dict[tuple[int, int], dict[str, tuple[int, str]]],
        size: int | None,
        budget: _Budget,
        found: dict[tuple[int, int], dict[str, str]],
    ) -> tuple[list[Point], bool]:
        """Follow points through the blocks, adding their rows to found.

        Returns the points that were lost, and whether the budget ran out.
        """
        parameter = _parameter(url)
        walks = [p for p in points if p.key in blocks.order]
        for point in points:
            if point.key not in blocks.order:
                found[point.key] = {}  # not in the file
        spent = budget.spent
        results = await asyncio.gather(
            *(
                self._walk_blocks(
                    url, blocks, p.key, known.get(p.key, {}), size, budget
                )
                for p in walks
            ),
            return_exceptions=True,
        )
        lost: list[Point] = []
        errors: list[int] = []
        over = False
        for point, result in zip(walks, results):
            if isinstance(result, _Lost):
                lost.append(point)
                over = over or isinstance(result, _OverBudget)
            elif isinstance(result, BaseException):
                raise result
            else:
                rows, positions, misses = result
                found[point.key] = rows
                self._block_positions[(parameter, point.key)] = positions
                errors.extend(misses)
        if errors:
            low, high = BLOCK_MARGIN_RANGE
            error = sorted(errors)[int(BLOCK_MARGIN_SHARE * (len(errors) - 1))]
            blocks.margin = min(high, max(low, int(error * 1.1) + BLOCK_ROW))
        if walks and not lost:
            blocks.cost = (budget.spent - spent) // len(walks)
        return lost, over

    async def _whole_file_rows(
        self, url: str, points: list[Point], total: int | None
    ) -> dict[tuple[int, int], dict[str, str]]:
        prefixes = {p.key: f"{p.point_id};{p.point_type};" for p in points}
        rows = await self._time_ordered_point_values(
            url, list(prefixes.values()), total
        )
        return {key: rows[prefix] for key, prefix in prefixes.items()}

    async def _walk_blocks(
        self,
        url: str,
        blocks: _Blocks,
        key: tuple[int, int],
        known: dict[str, tuple[int, str]],
        total: int | None,
        budget: _Budget,
    ) -> tuple[dict[str, str], dict[str, int], list[int]]:
        """A point's rows in every block: its values and positions by time.

        Also how far off each guess was.  Raises _Lost if a row cannot be
        found.
        """
        old = self._block_positions.get((_parameter(url), key), {})
        positions = {t: p for t, (p, _) in known.items()}
        values = {t: v for t, (_, v) in known.items()}
        errors = []
        for block in range(BLOCK_LIMIT):
            timestamp = blocks.time(block)
            if timestamp in positions:
                continue
            guess = _guess_position(blocks, key, positions, old, block)
            if guess is None:
                raise _Lost
            if total is not None and guess - blocks.margin >= total:
                break
            found = await self._block_row(url, blocks, key, block, guess, total, budget)
            if found is None:
                break  # past the last block
            position, value, total = found
            errors.append(abs(position - guess))
            positions[timestamp] = position
            values[timestamp] = value
        return values, positions, errors

    async def _block_row(
        self,
        url: str,
        blocks: _Blocks,
        key: tuple[int, int],
        block: int,
        guess: int,
        total: int | None,
        budget: _Budget,
    ) -> tuple[int, str, int] | None:
        """A point's row in a block near a guessed position: position, value, size.

        None if the file has no such block.
        """
        timestamp = blocks.time(block)
        index = blocks.order[key]
        margin = blocks.margin
        for _ in range(BLOCK_TRIES):
            start = max(0, guess - margin)
            end = guess + margin + BLOCK_ROW
            if total is not None:
                if start >= total:
                    return None
                end = min(end, total)
            chunk, total = await self._get(url, (start, end - 1))
            budget.spend(len(chunk), total)
            rows = _whole_rows(chunk, start, total)
            for position, row_key, row_time, value in rows:
                if row_key == key and row_time == timestamp:
                    return position, value, total
            if not rows:
                margin *= 2
                continue
            if start + len(chunk) >= total and rows[-1][2] < timestamp:
                return None  # the file ends before the block
            # How many rows away the point is, from a row in the middle.
            position, row_key, row_time, _ = rows[len(rows) // 2]
            ahead = blocks.blocks_between(timestamp, row_time)
            if row_key not in blocks.order or ahead is None:
                raise _Lost
            away = index - blocks.order[row_key] - ahead * len(blocks.order)
            width = (
                (rows[-1][0] - rows[0][0]) / (len(rows) - 1)
                if len(rows) > 1
                else blocks.length / len(blocks.order)
            )
            guess = max(0, position + round(away * width))
        raise _Lost

    async def _hinted_point_values(
        self, url: str, point: Point, prefix: str, offset: int, old: int
    ) -> dict[str, str]:
        """The rows of a point near where they were before, if all are there.

        offset is where they were in a file of size old.
        """
        parameter = _parameter(url)
        span = self._spans.get((parameter, point.key), ROW_SPAN)
        size = self._known_size(url)
        if size is None:
            change = self._size_changes.get(parameter)
            if change is None or abs(change) > HINT_SCALE_FROM:
                size = await self._size(url)
                if size is not None:
                    self._note_size(url, size)
            else:
                # Probably about as small a change as the last time.
                margin = HINT_MARGIN + 2 * abs(change)
                rows, total = await self._hint_window(
                    url, point, prefix, offset, span, margin
                )
                if rows or total is None or total == old:
                    return rows
                size = total  # moved further than the last time: try where scaled
        if size is None:
            size = old
        rows, _ = await self._hint_window(
            url,
            point,
            prefix,
            # Rows move in proportion to their position in the file.
            round(offset * size / old),
            span,
            HINT_MARGIN + int(HINT_SCALED_SHARE * abs(size - old)),
        )
        return rows

    async def _hint_window(
        self, url: str, point: Point, prefix: str, offset: int, span: int, margin: int
    ) -> tuple[dict[str, str], int | None]:
        """The rows of a point in a window around an offset, and the file's size.

        No rows unless all of them are in the window.
        """
        start = max(0, offset - margin)
        end = offset + span + margin
        try:
            chunk, total = await self._get(url, (start, end - 1))
        except OGDError:
            return {}, None
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
            return {}, total
        self._note_size(url, total)
        self._remember_offset(url, point, chunk, start, prefix, total)
        return _rows_with_prefix(b"\n".join(lines).decode("latin-1"), prefix), total

    def _remember_offset(
        self, url: str, point: Point, chunk: bytes, start: int, prefix: str, total: int
    ) -> None:
        needle = b"\n" + prefix.encode("latin-1")
        position = chunk.find(needle)
        if position < 0:
            return
        key = (_parameter(url), point.key)
        self._offsets[key] = (start + position + 1, total)
        # The rows end with the line break before the first row of another point.
        last = position
        while (following := chunk.find(needle, last + 1)) >= 0:
            last = following
        end = chunk.find(b"\n", last + 1)
        if end >= 0:
            self._spans[key] = end - position

    async def _streamed_point_values(
        self,
        url: str,
        prefixes: list[str],
        where: dict[str, tuple[int, int]] | None = None,
    ) -> dict[str, dict[str, str]]:
        """Stream a whole file, keeping only the rows starting with the prefixes.

        where gets where each prefix's rows start and end in the file.
        """
        needles = {prefix: b"\n" + prefix.encode("latin-1") for prefix in prefixes}
        rows: dict[str, list[bytes]] = {prefix: [] for prefix in prefixes}
        where = {} if where is None else where

        def collect(prefix: str, data: bytes, offset: int) -> None:
            if found := _rows_with_needle(rows[prefix], data, needles[prefix]):
                start, end = found[0] + offset, found[1] + offset
                where[prefix] = (where.get(prefix, (start, end))[0], end)

        # Carries the incomplete last line of a chunk, after its line break.
        tail = b""
        offset = 0  # where in the file tail starts
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
                    for prefix in needles:
                        collect(prefix, data[:cut], offset)
                    tail = data[cut:]
                    offset += cut
                self._count(response, offset + len(tail))
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise OGDError(f"Cannot fetch {url}: {exc}") from exc
        for prefix in needles:
            collect(prefix, tail, offset)
        return {
            prefix: _rows_with_prefix(b"\n".join(found).decode("latin-1"), prefix)
            for prefix, found in rows.items()
        }

    async def _ordered_point_values(
        self, url: str, total: int, point: Point, prefix: str
    ) -> dict[str, str]:
        """Find a point in a file ordered by point, trying the known orders.

        If the file is in neither order, the point is looked up in the whole
        file instead.
        """
        parameter = _parameter(url)
        last = self._orders.get(parameter, 0)
        ordered = False
        for index in (last, *(i for i in range(len(POINT_ORDERS)) if i != last)):
            rows, in_order = await self._ranged_point_values(
                url, total, point, prefix, POINT_ORDERS[index]
            )
            if rows:
                self._orders[parameter] = index
                return rows
            ordered = ordered or in_order
        if ordered:
            _LOGGER.debug("Point %s not in %s", point.key, url)
            return {}
        # Read whole, at most once a day: where the rows are is remembered, so
        # the next runs look there, which needs no order.
        date = _run_of(url)[:8]
        if self._whole_reads.get((parameter, point.key)) == date:
            _LOGGER.debug("Point %s not found in %s, read whole today", point.key, url)
            return {}
        self._whole_reads[(parameter, point.key)] = date
        _LOGGER.info("%s is in no known order; reading it whole", url)
        self._note_whole_download()
        where: dict[str, tuple[int, int]] = {}
        rows = (await self._streamed_point_values(url, [prefix], where))[prefix]
        if prefix in where:
            start, end = where[prefix]
            self._offsets[(parameter, point.key)] = (start, total)
            self._spans[(parameter, point.key)] = end - start
        return rows

    async def _ranged_point_values(
        self, url: str, total: int, point: Point, prefix: str, order
    ) -> tuple[dict[str, str], bool]:
        """Binary search a file ordered by point, in the given order.

        Also whether the rows where the point would be are in that order, so
        that a point not found is really not in the file.
        """
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
        self._remember_offset(url, point, chunk, lo, prefix, total)
        lines = chunk.split(b"\n")
        if lo > 0:
            lines = lines[1:]  # starts mid-line
        if end < total:
            lines = lines[:-1]  # ends mid-line
        rows = _rows_with_prefix(b"\n".join(lines).decode("latin-1"), prefix)
        keys = [order(*k) for line in lines if (k := _key(line))]
        in_order = (
            bool(keys)
            and keys == sorted(keys)
            and (lo == 0 or keys[0] < target)
            and (end == total or keys[-1] > target)
        )
        return rows, in_order

    async def get_hourly_rows(
        self, points: list[Point], run: str, parameter: str
    ) -> dict[tuple[int, int], dict[str, str]]:
        """The rows of several points in a large file ordered by time.

        Such a file (about 33 MB) is read block by block, a couple of hundred
        requests for each point, so it is fetched separately from (and less
        often than) the rest of the forecast.
        """
        return await self._blocked_rows(self._run_url(run, parameter), points)

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
