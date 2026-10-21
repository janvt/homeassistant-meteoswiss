"""The real servers: what reading them relies on, and what it costs.

Each test reads the forecast as the integration does, compares the values
with the whole files, and checks its cost against what downloading the whole
files instead would cost (the fallback, and what versions before 2.0.0b9 did):
for each case, the share in BUDGET, of the whole files' size as transferred.
The cases are the expensive ones: a cold start (the first read after starting
Home Assistant, or of a new day), the next runs, and places spread over the
files, the first and last among them.  The tests fail when MeteoSwiss or
MeteoAlarm change something the reading relies on, or when it costs more than
its budget: see .github/workflows/live.yaml, which runs them twice a day.
"""

from __future__ import annotations

import datetime

import pytest

from custom_components.meteoswiss import alerts, ogd
from custom_components.meteoswiss.ogd import MeteoSwissOGD, StationType

from .conftest import (
    USER_AGENT,
    download,
    record,
    runs_of_one_day,
    whole_cost,
    whole_rows,
)

KB = 1024
# The most each case may cost, per place, as a share of downloading the whole
# files once.  Measured in October 2026: about half of each.
BUDGET = {
    # Hourly temperatures and the freezing level (33 MB each): about 220
    # small requests per place.
    ("hourly", "cold"): 0.03,
    ("hourly", "3 hours later"): 0.025,
    # The daily lows and highs (220 kB compressed): the whole file when cold,
    # as learning its layout would cost as much; a request per day after that.
    ("daily", "cold"): 1.1,
    ("daily", "next run"): 0.15,
    # All 14 files of a run (about 380 MB): a binary search in each when
    # cold, a request or two each after that.
    ("forecast", "cold"): 0.005,
    ("forecast", "next run"): 0.0015,
}


def within_budget(kind: str, case: str, transferred: int, whole: int, places: int):
    share = transferred / places / whole
    assert share <= BUDGET[(kind, case)], (
        f"{kind}, {case}: {share:.2%} of the whole files per place, "
        f"budget {BUDGET[(kind, case)]:.2%}"
    )


async def test_the_servers_support_what_the_reading_relies_on(http, latest_run):
    headers = {"User-Agent": USER_AGENT}
    forecast = MeteoSwissOGD._run_url(latest_run, "jww003i0")
    # Ranges of the forecast files, and their size without downloading them.
    async with http.get(
        forecast,
        headers={**headers, "Range": "bytes=100-199", "Accept-Encoding": "identity"},
    ) as response:
        assert response.status == 206
        assert len(await response.read()) == 100
        total = int(response.headers["Content-Range"].rsplit("/", 1)[1])
    async with http.head(
        forecast, headers={**headers, "Accept-Encoding": "identity"}
    ) as response:
        assert int(response.headers["Content-Length"]) == total
    # Station files answer whether they changed.
    station = f"{ogd.SMN_URL}/alt/ogd-smn_alt_t_now.csv"
    async with http.get(station, headers=headers) as response:
        etag = response.headers["ETag"]
    async with http.get(
        station, headers={**headers, "If-None-Match": etag}
    ) as response:
        assert response.status in (304, 200)  # 200 if a new row came just now
    # The size of the warnings feed is its length.
    async with http.head(alerts.FEED_URL, headers=headers) as response:
        size = int(response.headers["Content-Length"])
    async with http.get(alerts.FEED_URL, headers=headers) as response:
        feed = await response.read()
    assert abs(len(feed) - size) < 50 * KB  # it may have changed in between
    assert alerts.parse_feed(feed) is not None


@pytest.mark.parametrize("parameter", ["tre200h0", "zprfr0hs"])
async def test_hourly_file_ordered_by_time(
    http, client, counting, places, latest_run, parameter
):
    earlier, later = runs_of_one_day(latest_run, 3)

    counting.clear()
    cold = await client.get_hourly_rows(places, earlier, parameter)
    cold_cost = counting.requests, counting.transferred
    counting.clear()
    warm = await client.get_hourly_rows(places, later, parameter)
    warm_cost = counting.requests, counting.transferred

    blocks = 0
    for run, rows in ((earlier, cold), (later, warm)):
        body = await download(http, MeteoSwissOGD._run_url(run, parameter))
        blocks = len({line.split(b";", 3)[2] for line in body.splitlines()[1:] if line})
        for place in places:
            assert rows[place.key] == whole_rows(body, place), (run, place)
    found = sum(1 for p in places if warm[p.key])
    assert found >= len(places) - 1  # a mountain may have no hourly temperatures
    whole = await whole_cost(http, MeteoSwissOGD._run_url(later, parameter))
    record(parameter, f"cold, {len(places)} places", *cold_cost, whole)
    record(parameter, f"3 hours later, {len(places)} places", *warm_cost, whole)
    # About one request per block (some 220) and place, a few more to find
    # rows, fewer for the blocks in the head.
    for requests, _ in (cold_cost, warm_cost):
        assert (blocks - 2) * found <= requests <= 1.5 * blocks * found + 10
    within_budget("hourly", "cold", cold_cost[1], whole, found)
    within_budget("hourly", "3 hours later", warm_cost[1], whole, found)
    assert client.whole_downloads == 0 and not client.ranges_ignored


async def test_daily_file_ordered_by_time(http, client, counting, places, latest_run):
    earlier, later = runs_of_one_day(latest_run, 1)
    place = places[2]
    url = MeteoSwissOGD._run_url

    counting.clear()
    cold = await client._point_values(url(earlier, "tre200pn"), place)
    cold_cost = counting.requests, counting.transferred
    counting.clear()
    warm = await client._point_values(url(later, "tre200pn"), place)
    warm_cost = counting.requests, counting.transferred

    assert cold == whole_rows(await download(http, url(earlier, "tre200pn")), place)
    assert warm == whole_rows(await download(http, url(later, "tre200pn")), place)
    assert len(warm) >= 8
    whole = await whole_cost(http, url(later, "tre200pn"))
    record("tre200pn", "cold, 1 place", *cold_cost, whole)
    record("tre200pn", "next run", *warm_cost, whole)
    assert warm_cost[0] <= 9 + 4
    within_budget("daily", "cold", cold_cost[1], whole, 1)
    within_budget("daily", "next run", warm_cost[1], whole, 1)
    assert client.whole_downloads == 0 and not client.ranges_ignored


async def test_forecast(http, client, counting, places, latest_run):
    """Three runs in a row for every place: a cold start, then normal operation."""
    first, _ = runs_of_one_day(latest_run, 2)
    end = runs_of_one_day(latest_run, 2)[1]
    runs = [first, runs_of_one_day(end, 1)[0], end]
    costs = []
    for run in runs:
        counting.clear()
        for place in places:
            await client.get_forecast(place, run)
        costs.append((counting.requests, counting.transferred))
    whole = 0
    for parameter in (*ogd.DAILY_PARAMETERS.values(), *ogd.HOURLY_PARAMETERS.values()):
        whole += await whole_cost(http, MeteoSwissOGD._run_url(end, parameter))
    for name, cost in zip(("cold", "next run", "run after"), costs):
        record("forecast", f"{name}, {len(places)} places", *cost, whole)
    # Values as in the whole files, for a file of each kind.
    for parameter in ("jww003i0", "jp2000d0", "tre200px"):
        file = MeteoSwissOGD._run_url(end, parameter)
        body = await download(http, file)
        for place in places:
            assert await client._point_values(file, place) == whole_rows(body, place)
    cold, *warm = costs
    within_budget("forecast", "cold", cold[1], whole, len(places))
    for requests, transferred in warm:
        # A request or two per file, more when a file changed much.
        assert requests <= 70 * len(places)
        within_budget("forecast", "next run", transferred, whole, len(places))
    # Nothing downloaded whole for lack of a way to search it.
    assert client.whole_downloads == 0 and not client.ranges_ignored
    # What the integration counts for its repair notice is what was sent: the
    # first two runs, and the last one with the reads for the comparison.
    sent = costs[0][1] + costs[1][1] + counting.transferred
    assert abs(client.downloaded - sent) < 0.1 * sent


async def test_station_files_are_fetched_only_when_they_changed(
    client, counting, monkeypatch
):
    clock = [0.0]
    monkeypatch.setattr(ogd.time, "monotonic", lambda: clock[0])
    await client.get_station_now("ALT", StationType.WEATHER)
    counting.clear()
    statuses = []
    for _ in range(3):
        clock[0] += 120
        await client.get_station_now("ALT", StationType.WEATHER)
        statuses.append(counting.log[-1].status)
    whole = 3 * await whole_cost(
        client._session.session, f"{ogd.SMN_URL}/alt/ogd-smn_alt_t_now.csv"
    )
    record("station ALT", "3 updates", counting.requests, counting.transferred, whole)
    assert 304 in statuses  # a new row comes only every 10 minutes


async def test_warnings_are_checked_by_size(counting):
    meteoalarm = alerts.MeteoAlarm(counting, USER_AGENT)  # type: ignore[arg-type]
    now = datetime.datetime.now(datetime.timezone.utc)
    await meteoalarm.get_warnings(46.88, 8.64, 450, "en", now)
    counting.clear()
    later = now + alerts.FEED_CHECK
    if any(now < e.expires <= later for e in meteoalarm._entries or []):
        pytest.skip("a warning runs out meanwhile, so the feed is fetched")

    await meteoalarm.get_warnings(46.88, 8.64, 450, "en", later)

    whole = await whole_cost(counting.session, alerts.FEED_URL)
    record("warnings", "a check", counting.requests, counting.transferred, whole)
    assert [e.method for e in counting.log][:1] == ["HEAD"]
    assert counting.log[0].bytes < 2 * KB
