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
    RAIN_RATE,
    Radar,
)

from .fakes import FakeSession

UTC = datetime.timezone.utc
# A minute after the image of 12:30 is due.
NOW = datetime.datetime(2026, 10, 21, 12, 31, 30, tzinfo=UTC)
SLOT = datetime.datetime(2026, 10, 21, 12, 30, tzinfo=UTC)
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
    assert [u for _, u, _ in session.requests] == [url(RAIN_RATE)]


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
    meteo, session = client({url(RAIN_RATE): image()})
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

    assert [u for _, u, _ in session.requests] == [url(RAIN_RATE, later, "vl")]


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
    assert meteo._variants[radar.PRECIPITATION] == "pl"


async def test_the_list_of_files_is_read_at_most_every_half_hour():
    meteo, session = client(
        {radar.ITEM_URL.format(collection=radar.PRECIPITATION, day="20261021"): index()}
    )
    item = radar.ITEM_URL.format(collection=radar.PRECIPITATION, day="20261021")

    for minutes in range(3, 60):
        await meteo.get(*ZURICH, SLOT + datetime.timedelta(minutes=minutes))

    assert session.count("GET", item) == 2
