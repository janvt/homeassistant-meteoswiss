"""Fixtures for the tests against the real MeteoSwiss and MeteoAlarm servers.

Run with the Home Assistant test plugins off, as they block the network:

    pytest -p no:homeassistant -p no:socket tests_live
"""

from __future__ import annotations

import datetime
import os
from dataclasses import dataclass, field

import aiohttp
import pytest

from custom_components.meteoswiss.ogd import MeteoSwissOGD, Point

USER_AGENT = (
    "homeassistant-meteoswiss live tests (github.com/janvt/homeassistant-meteoswiss)"
)
# What each test measured, for the summary at the end: what, case, requests,
# bytes, and what downloading the whole files instead would have cost.
MEASURED: list[tuple[str, str, int, int, int]] = []
# CloudFront compresses whole files up to this size.
COMPRESSED_UP_TO = 10_000_000


@dataclass
class Exchange:
    method: str
    url: str
    range: str | None
    status: int
    bytes: int  # body as transferred, plus headers


@dataclass
class CountingSession:
    """An aiohttp session that records what each request cost."""

    session: aiohttp.ClientSession
    log: list[Exchange] = field(default_factory=list)

    def get(self, url, **kw):
        return _Counted(self, "GET", url, kw)

    def head(self, url, **kw):
        return _Counted(self, "HEAD", url, kw)

    def clear(self) -> None:
        self.log.clear()

    @property
    def requests(self) -> int:
        return len(self.log)

    @property
    def transferred(self) -> int:
        return sum(e.bytes for e in self.log)


class _Counted:
    def __init__(self, owner: CountingSession, method: str, url: str, kw) -> None:
        self._owner, self._method, self._url, self._kw = owner, method, url, kw

    async def __aenter__(self):
        call = (
            self._owner.session.get
            if self._method == "GET"
            else self._owner.session.head
        )
        self._context = call(self._url, **self._kw)
        self._response = await self._context.__aenter__()
        return self

    async def __aexit__(self, *exc):
        r = self._response
        headers = sum(len(k) + len(v) + 4 for k, v in r.raw_headers) + 20
        # As transferred: compressed, if it was.  Empty replies (304) have no
        # count, and raise when asked for one.
        raw = getattr(r.content, "total_raw_bytes", 0) if self._method == "GET" else 0
        body = raw if isinstance(raw, int) else 0
        self._owner.log.append(
            Exchange(
                self._method,
                self._url,
                (self._kw.get("headers") or {}).get("Range"),
                r.status,
                headers + body,
            )
        )
        return await self._context.__aexit__(*exc)

    def __getattr__(self, name):
        return getattr(self._response, name)

    async def read(self):
        return await self._response.read()

    @property
    def content(self):
        return self._response.content


@pytest.fixture
async def http():
    async with aiohttp.ClientSession() as session:
        yield session


@pytest.fixture
def counting(http):
    return CountingSession(http)


@pytest.fixture
def client(counting):
    return MeteoSwissOGD(counting, USER_AGENT)  # type: ignore[arg-type]


@pytest.fixture
async def latest_run(http) -> str:
    return await MeteoSwissOGD(http, USER_AGENT).latest_run()


def runs_of_one_day(latest: str, hours_back: int) -> tuple[str, str]:
    """Two runs that many hours apart on the same (UTC) day, the later latest.

    A new day's files are laid out afresh, so they would be a cold start.
    """
    end = datetime.datetime.strptime(latest, "%Y%m%d%H%M")
    if end.hour < hours_back:
        end = end.replace(hour=23) - datetime.timedelta(days=1)
    start = end - datetime.timedelta(hours=hours_back)
    return start.strftime("%Y%m%d%H%M"), end.strftime("%Y%m%d%H%M")


@pytest.fixture
async def places(http) -> list[Point]:
    """Places spread over the files: the first and last, cities, a mountain."""
    client = MeteoSwissOGD(http, USER_AGENT)
    points = await client.get_points()
    stations = [p for p in points if p.point_type == 1]
    postcodes = [p for p in points if p.point_type == 2]
    mountains = [p for p in points if p.point_type == 3 and (p.height or 0) > 2000]
    chosen = [
        min(stations, key=lambda p: p.key),
        await client.resolve_point(1201),  # Genève
        await client.resolve_point(6460),  # Altdorf
        await client.resolve_point(8001),  # Zürich
        max(postcodes, key=lambda p: p.key),
        min(mountains, key=lambda p: p.key),
    ]
    return chosen


def whole_rows(body: bytes, point: Point) -> dict[str, str]:
    """A place's rows, from the whole file."""
    prefix = f"{point.point_id};{point.point_type};"
    rows = {}
    for line in body.decode("latin-1").splitlines():
        if line.startswith(prefix):
            parts = line.split(";")
            rows[parts[2]] = parts[3]
    return rows


async def whole_cost(http, url: str) -> int:
    """What downloading a whole file costs, compressed where the server does."""
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    async with http.head(url, headers=headers) as response:
        response.raise_for_status()
        size = int(response.headers["Content-Length"])
        overhead = sum(len(k) + len(v) + 4 for k, v in response.raw_headers) + 20
    if size > COMPRESSED_UP_TO:
        return size + overhead
    async with aiohttp.ClientSession(auto_decompress=False) as raw:
        headers["Accept-Encoding"] = "gzip"
        async with raw.get(url, headers=headers) as response:
            response.raise_for_status()
            return len(await response.read()) + overhead


async def download(http, url: str) -> bytes:
    async with http.get(url, headers={"User-Agent": USER_AGENT}) as response:
        response.raise_for_status()
        return await response.read()


def record(what: str, case: str, requests: int, transferred: int, whole: int) -> None:
    MEASURED.append((what, case, requests, transferred, whole))


def pytest_sessionfinish(session, exitstatus):
    if not MEASURED:
        return
    lines = [
        "| What | Case | Requests | kB | Whole files, kB | Share |",
        "|---|---|---:|---:|---:|---:|",
        *(
            f"| {w} | {c} | {r} | {b / 1024:.0f} | {f / 1024:.0f} | {b / f:.1%} |"
            for w, c, r, b, f in MEASURED
        ),
    ]
    text = "\n".join(lines)
    print("\n" + text)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as file:
            file.write("## Measured download volumes\n\n" + text + "\n")
