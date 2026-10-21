"""How much reading the forecast files costs, in requests and bytes.

The files here are laid out like MeteoSwiss's, a third of their size: 2,000
places, values of varying length that change a little from run to run.  Each
case states the band its cost must stay in, so that a change to how files are
read, or a layout it no longer copes with, shows up as a failure here rather
than as a larger bill.  tests_live checks the same against the real files.
"""

from __future__ import annotations

import datetime
import functools
import random

import pytest

from custom_components.meteoswiss import ogd
from custom_components.meteoswiss.ogd import MeteoSwissOGD, Point

from .fakes import FakeSession

UTC = datetime.timezone.utc
# Stations, postal codes and mountains, numbered as MeteoSwiss does.
PLACES = (
    [(i, 1) for i in range(1, 151)]
    + [(100000 + i * 400, 2) for i in range(1800)]
    + [(i, 3) for i in range(100, 150)]
)
PLACES.sort()
FIRST, MIDDLE, LAST = PLACES[0], PLACES[1000], PLACES[-1]
HOURS = [
    (
        datetime.datetime(2026, 10, 19, 21, tzinfo=UTC) + datetime.timedelta(hours=h)
    ).strftime("%Y%m%d%H%M")
    for h in range(220)
]
DAYS = [f"202610{20 + d}0000" for d in range(9)]
# The runs of a day: the next one an hour later, another three hours later.
RUNS = ["202610200900", "202610201000", "202610201200"]


def temperature(key, stamp: str, run: int) -> str:
    """About as long as MeteoSwiss's values, and a little different each run."""
    # Changes every other run, differently for each place and hour.
    noise = ((key[0] * 31 + int(stamp[6:10]) * 17 + (run // 2) * 13) % 13 - 6) / 10
    hour = int(stamp[8:10])
    base = (key[0] * 7 % 23) - 6 + 6 * (1 if 9 <= hour <= 17 else -1)
    return f"{base + noise + run * 0.1:.1f}"


@functools.cache
def by_time(param: str, run: int, stamps=tuple(HOURS), order=None) -> bytes:
    order = order or tuple(PLACES)
    lines = [f"point_id;point_type_id;Date;{param}"]
    for stamp in stamps:
        for key in order:
            lines.append(f"{key[0]};{key[1]};{stamp};{temperature(key, stamp, run)}")
    return ("\n".join(lines) + "\n").encode()


@functools.cache
def by_place(param: str, run: int, stamps=tuple(HOURS), order=None) -> bytes:
    order = order or tuple(PLACES)
    lines = [f"point_id;point_type_id;Date;{param}"]
    for key in order:
        for stamp in stamps:
            lines.append(f"{key[0]};{key[1]};{stamp};{temperature(key, stamp, run)}")
    return ("\n".join(lines) + "\n").encode()


def url(param: str, run: str) -> str:
    return MeteoSwissOGD._run_url(run, param)


def point(key) -> Point:
    return Point(key[0], key[1], "x", "", 0, 0)


def expected(body: bytes, key) -> dict[str, str]:
    return ogd._rows_with_prefix(body.decode(), f"{key[0]};{key[1]};")


@pytest.fixture
def head_like_the_real_files(monkeypatch):
    # The real head holds the first block (150 kB) and a quarter more.
    monkeypatch.setattr(ogd, "BLOCK_HEAD", int(len(by_time("t", 0)) / 220 * 1.3))


@pytest.mark.usefixtures("head_like_the_real_files")
class TestHourlyFilesOrderedByTime:
    """Like the hourly temperatures and the freezing level (tre200h0, zprfr0hs)."""

    def setup(self, runs=RUNS, **kw):
        files = {url("t", run): by_time("t", n, **kw) for n, run in enumerate(runs)}
        session = FakeSession(files)
        return MeteoSwissOGD(session, "tests"), session, files  # type: ignore[arg-type]

    @pytest.mark.parametrize("key", [FIRST, MIDDLE, LAST])
    async def test_cold_start(self, key):
        client, session, files = self.setup()

        rows = await client.get_hourly_rows([point(key)], RUNS[0], "t")

        assert rows[key] == expected(files[url("t", RUNS[0])], key)
        # The head, then about one request per block.
        assert 220 <= len(session.requests) <= 300
        assert session.transferred() <= ogd.BLOCK_HEAD + 700_000

    @pytest.mark.parametrize("key", [FIRST, MIDDLE, LAST])
    @pytest.mark.parametrize("later", [1, 2], ids=["next run", "three hours later"])
    async def test_normal_operation(self, key, later):
        client, session, files = self.setup()
        await client.get_hourly_rows([point(key)], RUNS[0], "t")
        session.clear()

        rows = await client.get_hourly_rows([point(key)], RUNS[later], "t")

        assert rows[key] == expected(files[url("t", RUNS[later])], key)
        assert 219 <= len(session.requests) <= 260
        assert session.transferred() <= 450_000

    async def test_several_places(self):
        keys = [FIRST, PLACES[400], MIDDLE, PLACES[1500], LAST]
        client, session, files = self.setup()
        await client.get_hourly_rows([point(k) for k in keys], RUNS[0], "t")
        session.clear()

        rows = await client.get_hourly_rows([point(k) for k in keys], RUNS[2], "t")

        for key in keys:
            assert rows[key] == expected(files[url("t", RUNS[2])], key)
        assert len(session.requests) <= 5 * 260
        assert session.transferred() <= 5 * 450_000

    @pytest.mark.parametrize("key", [FIRST, MIDDLE, LAST])
    async def test_a_place_added_later_the_same_day(self, key):
        client, session, files = self.setup()
        await client.get_hourly_rows([point(PLACES[700])], RUNS[0], "t")
        session.clear()

        rows = await client.get_hourly_rows([point(key)], RUNS[1], "t")

        assert rows[key] == expected(files[url("t", RUNS[1])], key)
        # Found from the order of the places, without reading the head again.
        assert f"bytes=0-{ogd.BLOCK_HEAD - 1}" not in [
            r for _, _, r in session.requests
        ]
        assert len(session.requests) <= 260
        assert session.transferred() <= 500_000

    async def test_a_place_not_in_the_file_costs_nothing_more(self):
        client, session, _ = self.setup()

        rows = await client.get_hourly_rows([point((999, 3))], RUNS[0], "t")

        assert rows == {(999, 3): {}}
        assert [m for m, _, _ in session.requests] == ["HEAD", "GET"]  # the head

    async def test_so_many_places_that_the_whole_file_is_cheaper(self):
        keys = PLACES[::20]  # 100 places
        client, session, files = self.setup()
        await client.get_hourly_rows([point(k) for k in keys[:2]], RUNS[0], "t")
        session.clear()

        rows = await client.get_hourly_rows([point(k) for k in keys], RUNS[1], "t")

        assert rows[keys[50]] == expected(files[url("t", RUNS[1])], keys[50])
        # No search at all: the whole file, as it is (here) under 8 MB.
        assert [r[2] for r in session.requests] == [None]
        assert client.whole_downloads == 0  # by choice, not for lack of a way

    async def test_the_order_of_the_places_changed(self):
        shuffled = list(PLACES)
        random.Random(1).shuffle(shuffled)
        client, session, files = self.setup()
        files[url("t", RUNS[1])] = by_time("t", 1, order=tuple(shuffled))
        await client.get_hourly_rows([point(MIDDLE)], RUNS[0], "t")
        session.clear()

        rows = await client.get_hourly_rows([point(MIDDLE)], RUNS[1], "t")

        assert rows[MIDDLE] == expected(files[url("t", RUNS[1])], MIDDLE)
        # The order is read again, and the place followed as on a cold start.
        assert session.transferred() <= 2 * ogd.BLOCK_HEAD + 800_000

    async def test_each_block_in_another_order(self):
        # Cannot be searched: the whole file, after at most as much searching.
        client, session, files = self.setup()
        lines = files[url("t", RUNS[0])].decode().split("\n")
        rng = random.Random(2)
        for block in range(220):
            part = lines[1 + block * len(PLACES) : 1 + (block + 1) * len(PLACES)]
            rng.shuffle(part)
            lines[1 + block * len(PLACES) : 1 + (block + 1) * len(PLACES)] = part
        body = files[url("t", RUNS[0])] = "\n".join(lines).encode()

        rows = await client.get_hourly_rows([point(MIDDLE)], RUNS[0], "t")

        assert rows[MIDDLE] == expected(body, MIDDLE)
        whole = ogd._whole_cost(len(body))
        assert session.transferred() - len(body) <= whole + 2000
        assert client.whole_downloads == 1  # for the repair notice

    async def test_longer_values(self):
        client, session, files = self.setup()
        await client.get_hourly_rows([point(MIDDLE)], RUNS[0], "t")
        session.clear()
        # Every value gains two digits: the blocks are a third longer.
        body = files[url("t", RUNS[1])] = by_time("t", 1).replace(b"\n", b"00\n")
        body = files[url("t", RUNS[1])] = body.replace(b"tre00", b"tre")

        rows = await client.get_hourly_rows([point(MIDDLE)], RUNS[1], "t")

        assert rows[MIDDLE] == expected(body, MIDDLE)
        assert session.transferred() <= ogd.BLOCK_HEAD + 900_000


@pytest.mark.usefixtures("head_like_the_real_files")
class TestDailyFilesOrderedByTime:
    """Like the daily lows and highs (tre200pn, tre200px), read every run."""

    def setup(self):
        files = {
            url("d", run): by_time("d", n, stamps=tuple(DAYS))
            for n, run in enumerate(RUNS)
        }
        session = FakeSession(files)
        return MeteoSwissOGD(session, "tests"), session, files  # type: ignore[arg-type]

    @pytest.mark.parametrize("key", [FIRST, MIDDLE, LAST])
    async def test_cold_start_and_normal_operation(self, key):
        client, session, files = self.setup()

        rows = await client._point_values(url("d", RUNS[0]), point(key))
        assert rows == expected(files[url("d", RUNS[0])], key)
        # The probe and the whole file (counted uncompressed here), as
        # learning its layout would cost about as much.
        assert [r for _, _, r in session.requests][1:] == [None]
        whole = len(files[url("d", RUNS[0])])
        assert session.transferred() - whole <= ogd.HEAD_BYTES + 2000
        session.clear()

        rows = await client._point_values(url("d", RUNS[1]), point(key))
        assert rows == expected(files[url("d", RUNS[1])], key)
        assert len(session.requests) <= 9 + 3
        assert session.transferred() <= 25_000

    async def test_searching_stops_when_the_whole_file_would_be_cheaper(self):
        # 67 places on a first read, so nothing tells in advance what it costs.
        client, session, files = self.setup()
        keys = PLACES[::30]

        rows = await client.get_hourly_rows([point(k) for k in keys], RUNS[0], "d")

        body = files[url("d", RUNS[0])]
        for key in keys:
            assert rows[key] == expected(body, key)
        # The search, cut off at what the file costs whole (compressed), and
        # then the whole file (counted uncompressed here).
        searched = session.transferred() - len(body)
        assert searched <= 2 * ogd._whole_cost(len(body)) + 2000 * len(keys)

    async def test_a_cold_start_downloads_a_small_file_whole(self, monkeypatch):
        # When learning the layout would cost as much as the whole file.
        client, session, files = self.setup()
        monkeypatch.setattr(ogd, "BLOCK_HEAD", len(files[url("d", RUNS[0])]))

        rows = await client.get_hourly_rows([point(MIDDLE)], RUNS[0], "d")

        assert rows[MIDDLE] == expected(files[url("d", RUNS[0])], MIDDLE)
        assert [(m, r) for m, _, r in session.requests] == [
            ("HEAD", None),
            ("GET", None),
        ]
        session.clear()

        # Where the rows were is known from it, so the next run searches.
        rows = await client.get_hourly_rows([point(MIDDLE)], RUNS[1], "d")
        assert rows[MIDDLE] == expected(files[url("d", RUNS[1])], MIDDLE)
        assert all(r is not None for _, _, r in session.requests)
        assert len(session.requests) <= 9 + 3
        # And the file is no longer kept.
        assert "d" not in client._block_files


class TestServerThatIgnoresRanges:
    async def test_the_files_are_downloaded_whole_instead(self):
        files = {
            url("p", RUNS[0]): by_place("p", 0),
            url("t", RUNS[0]): by_time("t", 0),
        }
        session = FakeSession(files)
        session.ignore_ranges = True
        client = MeteoSwissOGD(session, "tests")  # type: ignore[arg-type]

        rows = await client._point_values(url("p", RUNS[0]), point(MIDDLE))
        assert rows == expected(files[url("p", RUNS[0])], MIDDLE)
        hourly = await client.get_hourly_rows([point(MIDDLE)], RUNS[0], "t")
        assert hourly[MIDDLE] == expected(files[url("t", RUNS[0])], MIDDLE)

        assert client.ranges_ignored
        assert client.whole_downloads == 2
        # Each file once whole, after a range that was sent whole and not read.
        assert session.count("GET", url("p", RUNS[0])) == 2
        assert session.count("GET", url("t", RUNS[0])) == 2
        assert client.downloaded < 1.1 * sum(len(f) for f in files.values())

        session.ignore_ranges = False
        session.clear()
        rows = await client._point_values(url("p", RUNS[0]), point(MIDDLE))
        assert rows == expected(files[url("p", RUNS[0])], MIDDLE)
        assert not client.ranges_ignored
        assert all(r is not None for _, _, r in session.requests)


class TestFilesOrderedByPlace:
    """Like the rest of the forecast: 12 files, read every run."""

    def setup(self, runs=RUNS, stamps=tuple(HOURS)):
        files = {
            url("p", run): by_place("p", n, stamps=stamps) for n, run in enumerate(runs)
        }
        session = FakeSession(files)
        return MeteoSwissOGD(session, "tests"), session, files  # type: ignore[arg-type]

    @pytest.mark.parametrize("key", [FIRST, MIDDLE, LAST])
    async def test_cold_start(self, key):
        client, session, files = self.setup()

        rows = await client._point_values(url("p", RUNS[0]), point(key))

        assert rows == expected(files[url("p", RUNS[0])], key)
        # A binary search: about log2(9 MB / 4 kB) probes.
        assert len(session.requests) <= 16
        assert session.transferred() <= 50_000

    @pytest.mark.parametrize("key", [FIRST, MIDDLE, LAST])
    async def test_normal_operation(self, key):
        client, session, files = self.setup()
        for run in RUNS[:2]:
            await client._point_values(url("p", run), point(key))
        session.clear()

        rows = await client._point_values(url("p", RUNS[2]), point(key))

        assert rows == expected(files[url("p", RUNS[2])], key)
        assert len(session.requests) <= 3
        assert session.transferred() <= 20_000

    async def test_several_places(self):
        keys = [FIRST, PLACES[400], MIDDLE, PLACES[1500], LAST]
        client, session, files = self.setup()
        for run in RUNS[:2]:
            for key in keys:
                await client._point_values(url("p", run), point(key))
        session.clear()

        for key in keys:
            rows = await client._point_values(url("p", RUNS[2]), point(key))
            assert rows == expected(files[url("p", RUNS[2])], key)

        # Each place where it was, the file's size asked for once at most.
        assert len(session.requests) <= 2 * len(keys) + 1
        assert session.transferred() <= 20_000 * len(keys)

    async def test_a_place_not_in_the_file_is_not_looked_for_in_the_whole_file(self):
        client, session, _ = self.setup()

        rows = await client._point_values(url("p", RUNS[0]), point((999, 3)))

        assert rows == {}
        assert all(r is not None for _, _, r in session.requests)
        assert session.transferred() <= 100_000

    async def test_places_in_an_unknown_order(self):
        shuffled = list(PLACES)
        random.Random(3).shuffle(shuffled)
        client, session, files = self.setup()
        body = files[url("p", RUNS[0])] = by_place("p", 0, order=tuple(shuffled))

        rows = await client._point_values(url("p", RUNS[0]), point(MIDDLE))

        # Found in the whole file, after two binary searches that failed.
        assert rows == expected(body, MIDDLE)
        assert session.transferred() <= len(body) + 100_000

        # The next runs look where it was, which needs no order.
        files[url("p", RUNS[1])] = by_place("p", 1, order=tuple(shuffled))
        session.clear()
        rows = await client._point_values(url("p", RUNS[1]), point(MIDDLE))
        assert rows == expected(files[url("p", RUNS[1])], MIDDLE)
        assert len(session.requests) <= 3
        assert session.transferred() <= 20_000

    async def test_a_place_lost_in_an_unknown_order_is_read_whole_once_a_day(self):
        shuffled = list(PLACES)
        random.Random(4).shuffle(shuffled)
        client, session, files = self.setup()
        files[url("p", RUNS[0])] = by_place("p", 0, order=tuple(shuffled))
        # Entirely different the next run, so where it was says nothing.
        random.Random(5).shuffle(shuffled)
        files[url("p", RUNS[1])] = by_place("p", 1, order=tuple(shuffled))
        await client._point_values(url("p", RUNS[0]), point(MIDDLE))
        session.clear()

        rows = await client._point_values(url("p", RUNS[1]), point(MIDDLE))

        assert rows == {}  # rather than another whole file the same day
        assert all(r is not None for m, _, r in session.requests if m == "GET")
        assert session.transferred() <= 100_000


async def test_what_is_downloaded_is_counted_for_a_day(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ogd.time, "monotonic", lambda: clock[0])
    files = {url("p", RUNS[0]): by_place("p", 0), url("t", RUNS[0]): by_time("t", 0)}
    session = FakeSession(files)
    client = MeteoSwissOGD(session, "tests")  # type: ignore[arg-type]

    await client._point_values(url("p", RUNS[0]), point(MIDDLE))
    await client.get_hourly_rows([point(MIDDLE)], RUNS[0], "t")

    # Each response with its headers, as the server sent it.
    assert client.downloaded == session.transferred(ogd.REQUEST_OVERHEAD)
    clock[0] += ogd.USAGE_PERIOD.total_seconds() + 1
    assert client.downloaded == 0


async def test_rows_longer_than_expected_are_read_to_their_end(monkeypatch):
    # As if MeteoSwiss forecast much further ahead.
    monkeypatch.setattr(ogd, "ROW_SPAN", 1024)
    files = {url("p", RUNS[0]): by_place("p", 0)}
    client = MeteoSwissOGD(FakeSession(files), "tests")  # type: ignore[arg-type]

    rows = await client._point_values(url("p", RUNS[0]), point(MIDDLE))

    assert rows == expected(files[url("p", RUNS[0])], MIDDLE)
    assert len(rows) == len(HOURS)
