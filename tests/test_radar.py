"""Rain and hail from the weather radar, against an in-memory file store."""

import datetime
import io
import json

import h5py
import numpy as np
import pytest

from custom_components.meteoswiss import radar
from custom_components.meteoswiss.radar import (
    HAIL_PROBABILITY,
    HAIL_SIZE,
    PRODUCTS,
    RAIN_LAST_HOUR,
    RAIN_RATE,
    Radar,
)

from .fakes import FakeSession

UTC = datetime.timezone.utc
# A minute after the image of 12:30 is due.
NOW = datetime.datetime(2026, 10, 21, 12, 31, 30, tzinfo=UTC)
SLOT = datetime.datetime(2026, 10, 21, 12, 30, tzinfo=UTC)
# The rain of the last hour comes about 4 minutes after its slot.
HOUR_SLOT = SLOT - radar.INTERVAL
ZURICH = (47.3769, 8.5417)


def pixel(lat, lon):
    east, north = radar.lv95(lat, lon)
    return int((1480000 - north) // 1000), int((east - 2255000) // 1000)


def image(values=None, quantity="RATE") -> bytes:
    """A radar image laid out as MeteoSwiss's: 640 by 710 pixels of 1 km."""
    data = np.zeros((640, 710))
    data[:20, :] = np.nan  # outside the radars' reach
    for (lat, lon), value in (values or {}).items():
        data[pixel(lat, lon)] = value
    buffer = io.BytesIO()
    with h5py.File(buffer, "w") as file:
        file.create_dataset("dataset1/data1/data", data=data, compression="gzip")
        what = file["dataset1/data1"].create_group("what")
        what.attrs.update(
            {"gain": 1.0, "offset": 0.0, "quantity": quantity, "nodata": np.nan}
        )
        file.create_group("where").attrs.update(
            {"UL_lat": 49.3744010925293, "UL_lon": 2.689419984817505, "xscale": 1000.0}
        )
    return buffer.getvalue()


def url(key, slot=SLOT, variant="rl"):
    return radar._url(PRODUCTS[key], slot, variant)


def downloads(session, key):
    """The URLs requested for one product's images."""
    stem = PRODUCTS[key].stem
    return [u for _, u, _ in session.requests if u.rsplit("/", 1)[-1].startswith(stem)]


def client(files):
    session = FakeSession(files)
    return Radar(session, "tests"), session  # type: ignore[arg-type]


def test_lv95_matches_swisstopo():
    # swisstopo's example: 46° 2' 38.87", 8° 43' 49.79".
    east, north = radar.lv95(46 + 2 / 60 + 38.87 / 3600, 8 + 43 / 60 + 49.79 / 3600)
    assert abs(east - 2699999.76) < 1 and abs(north - 1099999.97) < 1


def test_file_names_end_with_the_interval():
    assert url(RAIN_RATE).endswith("ogd-radar-precip/20261021-ch/rzc262941230rl.001.h5")
    assert url(HAIL_PROBABILITY, variant="vl").endswith("bzc262941230vl.845.h5")


async def test_the_rain_at_the_place():
    meteo, _ = client({url(RAIN_RATE): image({ZURICH: 2.5})})

    readings = await meteo.get(*ZURICH, NOW)

    assert readings[RAIN_RATE] == (2.5, SLOT)


async def test_outside_the_radars_reach_there_is_no_reading():
    meteo, _ = client({url(RAIN_RATE): image()})
    # In the rows the radars do not reach, and beyond the image.
    assert (await meteo.get(49.3, 6.0, NOW))[RAIN_RATE] == (None, SLOT)
    assert (await meteo.get(52.0, 13.0, NOW))[RAIN_RATE] == (None, SLOT)


async def test_no_hail_images_while_it_is_dry():
    meteo, session = client({url(RAIN_RATE): image()})

    readings = await meteo.get(*ZURICH, NOW)

    assert readings[HAIL_PROBABILITY] == (0.0, SLOT)
    assert readings[HAIL_SIZE] == (0.0, SLOT)
    assert downloads(session, RAIN_RATE) == [url(RAIN_RATE)]
    assert downloads(session, HAIL_PROBABILITY) == []
    assert downloads(session, HAIL_SIZE) == []


async def test_hail_while_it_rains():
    files = {
        url(RAIN_RATE): image({ZURICH: 40.0}),
        url(HAIL_PROBABILITY): image({ZURICH: 0.8}, "POH"),
        url(HAIL_SIZE): image({ZURICH: 25.0}, "MESH"),
    }
    meteo, _ = client(files)

    readings = await meteo.get(*ZURICH, NOW)

    assert readings[HAIL_PROBABILITY] == (pytest.approx(80.0), SLOT)
    assert readings[HAIL_SIZE] == (25.0, SLOT)


async def test_an_image_not_published_yet_gives_the_one_before():
    earlier = SLOT - radar.INTERVAL
    meteo, _ = client({url(RAIN_RATE, earlier): image({ZURICH: 1.0})})

    readings = await meteo.get(*ZURICH, NOW)

    assert readings[RAIN_RATE] == (1.0, earlier)


async def test_each_image_is_downloaded_once():
    meteo, session = client(
        {
            url(RAIN_RATE): image(),
            url(RAIN_LAST_HOUR, HOUR_SLOT, "0"): image(quantity="ACRR"),
        }
    )
    await meteo.get(*ZURICH, NOW)
    session.clear()

    await meteo.get(*ZURICH, NOW + datetime.timedelta(minutes=1))

    assert session.requests == []


async def test_a_reading_too_old_is_none():
    meteo, _ = client({url(RAIN_RATE): image({ZURICH: 1.0})})
    await meteo.get(*ZURICH, NOW)

    later = NOW + radar.MAX_AGE + radar.INTERVAL
    readings = await meteo.get(*ZURICH, later)

    assert readings[RAIN_RATE] is None
    assert readings[HAIL_PROBABILITY] is None


async def test_the_variant_that_worked_is_tried_first():
    later = SLOT + radar.INTERVAL
    files = {
        url(RAIN_RATE, SLOT, "vl"): image(),
        url(RAIN_RATE, later, "vl"): image(),
    }
    meteo, session = client(files)
    await meteo.get(*ZURICH, NOW)
    session.clear()

    await meteo.get(*ZURICH, NOW + radar.INTERVAL)

    assert downloads(session, RAIN_RATE) == [url(RAIN_RATE, later, "vl")]


def index(*names) -> bytes:
    return json.dumps({"assets": {n: {} for n in names}}).encode()


async def test_an_unusual_variant_is_looked_up_once_the_image_is_late():
    name = "rzc262941230pl.001.h5"
    files = {
        url(RAIN_RATE, SLOT, "pl"): image({ZURICH: 3.0}),
        radar.ITEM_URL.format(collection=radar.PRECIPITATION, day="20261021"): index(
            "rzc262941225pl.001.h5", name
        ),
    }
    meteo, session = client(files)

    item = list(files)[1]
    # Not yet: it may just be late.
    assert (await meteo.get(*ZURICH, NOW))[RAIN_RATE] is None
    assert session.count("GET", item) == 0

    late = SLOT + radar.LOOKUP_AFTER
    readings = await meteo.get(*ZURICH, late)

    assert readings[RAIN_RATE] == (3.0, SLOT)
    assert meteo._variants[PRODUCTS[RAIN_RATE].family] == "pl"


async def test_the_list_of_files_is_read_at_most_every_half_hour():
    meteo, session = client(
        {radar.ITEM_URL.format(collection=radar.PRECIPITATION, day="20261021"): index()}
    )
    item = radar.ITEM_URL.format(collection=radar.PRECIPITATION, day="20261021")

    for minutes in range(3, 60):
        await meteo.get(*ZURICH, SLOT + datetime.timedelta(minutes=minutes))

    assert session.count("GET", item) == 2


def hourly(slot, value=0.0, variant="0"):
    return {url(RAIN_LAST_HOUR, slot, variant): image({ZURICH: value}, "ACRR")}


async def test_the_rain_of_the_last_hour():
    meteo, _ = client({url(RAIN_RATE): image(), **hourly(HOUR_SLOT, 4.2)})

    readings = await meteo.get(*ZURICH, NOW)

    assert readings[RAIN_LAST_HOUR] == (pytest.approx(4.2), HOUR_SLOT)


def test_the_rain_of_the_last_hour_is_named_with_a_digit():
    assert url(RAIN_LAST_HOUR, HOUR_SLOT, "9").endswith(
        "ogd-radar-precip/20261021-ch/cpc2629412259_00060.001.h5"
    )


async def test_its_digit_is_found_and_tried_first_after():
    later = HOUR_SLOT + radar.INTERVAL
    files = {
        url(RAIN_RATE): image({ZURICH: 1.0}),
        url(RAIN_RATE, SLOT + radar.INTERVAL): image({ZURICH: 1.0}),
        **hourly(HOUR_SLOT, 1.0, "9"),
        **hourly(later, 2.0, "9"),
    }
    meteo, session = client(files)
    assert (await meteo.get(*ZURICH, NOW))[RAIN_LAST_HOUR] == (1.0, HOUR_SLOT)
    session.clear()

    readings = await meteo.get(*ZURICH, NOW + radar.INTERVAL)

    assert readings[RAIN_LAST_HOUR] == (2.0, later)
    assert downloads(session, RAIN_LAST_HOUR) == [url(RAIN_LAST_HOUR, later, "9")]


async def test_its_digits_are_never_looked_up_in_the_list_of_files():
    # The rain images are all there; those of the last hour never come.
    meteo, session = client(
        {
            url(RAIN_RATE, SLOT + k * radar.INTERVAL): image({ZURICH: 1.0})
            for k in range(7)
        }
    )
    item = radar.ITEM_URL.format(collection=radar.PRECIPITATION, day="20261021")

    for minutes in range(0, 30):
        await meteo.get(*ZURICH, NOW + datetime.timedelta(minutes=minutes))

    assert session.count("GET", item) == 0


async def test_none_after_a_dry_hour_and_no_more_downloads():
    files = {}
    for k in range(14):
        files[url(RAIN_RATE, SLOT + k * radar.INTERVAL)] = image()
        files.update(hourly(HOUR_SLOT + k * radar.INTERVAL, 0.3 if k < 2 else 0.0))
    meteo, session = client(files)
    for k in range(12):
        readings = await meteo.get(*ZURICH, NOW + k * radar.INTERVAL)
        assert readings[RAIN_LAST_HOUR][1] == HOUR_SLOT + k * radar.INTERVAL
    session.clear()

    # An hour after the first dry rain image.
    readings = await meteo.get(*ZURICH, NOW + 12 * radar.INTERVAL)

    assert readings[RAIN_LAST_HOUR] == (0.0, SLOT + 12 * radar.INTERVAL)
    assert downloads(session, RAIN_LAST_HOUR) == []


async def test_downloaded_again_once_it_rains():
    files = {}
    for k in range(14):
        files[url(RAIN_RATE, SLOT + k * radar.INTERVAL)] = image(
            {ZURICH: 2.0} if k == 13 else None
        )
        files.update(hourly(HOUR_SLOT + k * radar.INTERVAL))
    meteo, session = client(files)
    for k in range(13):
        await meteo.get(*ZURICH, NOW + k * radar.INTERVAL)
    session.clear()

    readings = await meteo.get(*ZURICH, NOW + 13 * radar.INTERVAL)

    later = HOUR_SLOT + 13 * radar.INTERVAL
    assert readings[RAIN_LAST_HOUR] == (0.0, later)
    assert downloads(session, RAIN_LAST_HOUR) == [url(RAIN_LAST_HOUR, later, "0")]
