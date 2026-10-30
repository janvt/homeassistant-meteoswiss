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

import collections
import datetime
import json

import numpy as np
import pytest

from custom_components.meteoswiss import alerts, ogd, radar
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
    # All 14 files of a run (about 380 MB): a binary search in each, every
    # run.
    ("forecast", "cold"): 0.005,
    ("forecast", "next run"): 0.005,
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
    # The warnings feed.
    async with http.get(alerts.FEED_URL, headers=headers) as response:
        assert alerts.parse_feed(await response.read()) is not None


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
        requests = collections.Counter(e.url for e in counting.log)
        parameters = (*ogd.DAILY_PARAMETERS.values(), *ogd.HOURLY_PARAMETERS.values())
        for parameter in parameters:
            file = MeteoSwissOGD._run_url(run, parameter)
            if client._by_time.get(parameter) == file:
                # A small request per block (an hour), as for tre200h0.  Some
                # runs of other files are ordered by time too, as on 28
                # October 2026 the precipitation from 00:00 to 09:00.
                limit = 300
            else:
                # A binary search, for files ordered by place: about 13
                # probes in 33 MB, then the rows.
                limit = 25
            assert requests[file] <= limit * len(places), (run, parameter)
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
    for _, transferred in warm:
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


RAIN_GAUGES = (
    "https://data.geo.admin.ch/ch.meteoschweiz.messwerte-niederschlag-10min/"
    "ch.meteoschweiz.messwerte-niederschlag-10min_en.json"
)


async def test_radar_images(http, counting):
    meteo = radar.Radar(counting, USER_AGENT)  # type: ignore[arg-type]
    now = datetime.datetime.now(datetime.timezone.utc)
    readings = await meteo.get(47.3769, 8.5417, now)  # Zürich
    assert readings[radar.RAIN_RATE] is not None, "no rain image in 20 minutes"
    image = meteo._images[radar.RAIN_RATE]
    # The Swiss composite's grid, 1 km pixels from its north-west corner.
    assert (image.west, image.north, image.scale) == (2255000, 1480000, 1000)
    assert image.values.shape == (640, 710)
    # Images are downloaded whole: the share is 100%.
    sent = counting.transferred
    record("radar", "latest images", counting.requests, sent, sent)
    # Each image is small: nothing to gain from reading part of it.
    for exchange in counting.log:
        if exchange.status == 200:
            assert exchange.bytes < 300 * KB
    # Hail images, published alongside.
    slot = image.time
    for key in (radar.HAIL_PROBABILITY, radar.HAIL_SIZE):
        product = radar.PRODUCTS[key]
        body = await meteo._image(product, slot, slot + radar.LOOKUP_AFTER)
        assert body is not None, f"no {key} image for {slot}"
        assert radar.Image.parse(body, slot, product.scale).values.shape == (640, 710)


async def test_radar_agrees_with_rain_gauges(http):
    """North is up: where gauges measure rain, the radar sees it."""
    async with http.get(RAIN_GAUGES, headers={"User-Agent": USER_AGENT}) as response:
        gauges = json.loads(await response.read())["features"]
    # Gauges report at different times, and those without a reading give
    # 99999 at "-": only those of the time most report at are compared.
    stamp = collections.Counter(
        g["properties"]["reference_ts"] for g in gauges
    ).most_common(1)[0][0]
    gauges = [g for g in gauges if g["properties"]["reference_ts"] == stamp]
    end = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    meteo = radar.Radar(http, USER_AGENT)
    rates = []
    for slot in (end, end - radar.INTERVAL):  # the gauges' 10 minutes
        body = await meteo._image(radar.PRODUCTS[radar.RAIN_RATE], slot, end)
        if body is None:
            pytest.skip("no radar image for the gauges' interval")
        rates.append(radar.Image.parse(body, slot, 1.0))
    measured, seen = [], []
    for gauge in gauges:
        east, north = gauge["geometry"]["coordinates"]
        row = int((1480000 - north) // 1000)
        column = int((east - 2255000) // 1000)
        values = [float(image.values[row, column]) for image in rates]
        if gauge["properties"]["value"] is None or any(v != v for v in values):
            continue
        measured.append(gauge["properties"]["value"])
        seen.append(sum(values) / 2 * 10 / 60)  # mm in 10 minutes
    if sum(m > 0 for m in measured) < 20:
        pytest.skip("too little rain to compare")
    correlation = np.corrcoef(measured, seen)[0, 1]
    # About 0.7 over 10 minutes; about 0.1 if the image were upside down.
    assert correlation > 0.4, f"radar and gauges correlate at {correlation:.2f}"
