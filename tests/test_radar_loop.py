"""The animated loop of the rain radar around a place."""

import math

import numpy as np
import pytest

from custom_components.meteoswiss import radar
from custom_components.meteoswiss.radar_loop import (
    FRAMES,
    HALF_KM,
    MAP_URL,
    RADIUS_KM,
    SCALE,
    SIZE,
    RadarLoop,
    _colours,
    cell_centre,
    crop,
    marker,
)
from custom_components.meteoswiss.usage import REQUEST_OVERHEAD, Usage

from .test_radar import SLOT, ZURICH, image


def rain(values=None, time=SLOT) -> radar.Image:
    return radar.Image.parse(image(values), time, 1.0)


def test_the_square_is_centred_on_the_place_s_cell():
    frame, box = crop(rain({ZURICH: 7.0}), *ZURICH)

    assert frame.shape == (2 * HALF_KM, 2 * HALF_KM)
    assert np.nansum(frame) == 7.0
    west, south, east, north = box
    assert east - west == north - south == 2 * HALF_KM * 1000
    assert ((west + east) / 2, (south + north) / 2) == cell_centre(*ZURICH)
    # The place's pixel holds its value, and at least RADIUS_KM shows around it.
    x, y = radar.lv95(*ZURICH)
    column, row = math.floor((x - west) / 1000), math.floor((north - y) / 1000)
    assert frame[row, column] == 7.0
    assert RADIUS_KM <= min(
        row, column, 2 * HALF_KM - 1 - row, 2 * HALF_KM - 1 - column
    )


def test_places_in_one_cell_ask_for_the_same_map():
    """The map's square tells swisstopo the 10 km cell, not the place."""
    near = (47.39, 8.56)  # 2 km north-east of ZURICH, in the same cell
    assert cell_centre(*near) == cell_centre(*ZURICH)

    assert crop(rain(), *near)[1] == crop(rain(), *ZURICH)[1]


def test_the_marker_is_on_the_place():
    _, box = crop(rain(), *ZURICH)
    x, y = radar.lv95(*ZURICH)
    west, _, _, north = box

    column, row = marker(box, *ZURICH)

    assert 0 <= column < SIZE and 0 <= row < SIZE
    assert abs(column - (x - west) / 1000 * SCALE) <= 0.5
    assert abs(row - (north - y) / 1000 * SCALE) <= 0.5


def test_beyond_the_image_is_no_data():
    # Near the image's north-west corner, where the radars do not reach.
    frame, _ = crop(rain(), 49.3, 2.75)

    assert np.isnan(frame[0, 0])
    assert np.isnan(frame).sum() > frame.size / 2


async def test_the_latest_frames_are_kept(hass):
    loop = RadarLoop(hass, ZURICH)
    assert loop.updated is None

    for k in range(FRAMES + 2):
        loop.add(rain(time=SLOT + k * radar.INTERVAL))

    newest = SLOT + (FRAMES + 1) * radar.INTERVAL
    assert loop.updated == newest
    assert sorted(loop._frames) == [
        newest - k * radar.INTERVAL for k in reversed(range(FRAMES))
    ]


async def test_the_loop_is_a_gif_drawn_once_per_frame(hass, aioclient_mock):
    aioclient_mock.get(MAP_URL.split("?")[0], status=403)
    loop = RadarLoop(hass, ZURICH)
    assert await loop.gif() is None

    loop.add(rain({ZURICH: 3.0}))
    loop.add(rain({ZURICH: 3.0}, SLOT + radar.INTERVAL))
    gif = await loop.gif()

    assert gif.startswith(b"GIF89a")
    assert await loop.gif() is gif
    # Without the map, a blank one stands in and the map is asked again.
    assert aioclient_mock.call_count == 1
    loop.add(rain(time=SLOT + 2 * radar.INTERVAL))
    assert await loop.gif() != gif
    assert aioclient_mock.call_count == 2


async def test_the_map_is_fetched_once(hass, aioclient_mock):
    from io import BytesIO

    from PIL import Image as PILImage

    png = BytesIO()
    PILImage.new("RGB", (16, 16), (128, 128, 128)).save(png, format="PNG")
    aioclient_mock.get(
        MAP_URL.split("?")[0],
        content=png.getvalue(),
        headers={"content-type": "image/png"},
    )
    loop = RadarLoop(hass, ZURICH)

    for k in range(3):
        loop.add(rain(time=SLOT + k * radar.INTERVAL))
        assert (await loop.gif()).startswith(b"GIF89a")

    assert aioclient_mock.call_count == 1


async def test_the_map_counts_as_downloaded(hass, aioclient_mock):
    aioclient_mock.get(
        MAP_URL.split("?")[0],
        content=b"\x89PNG" + bytes(996),
        headers={"content-type": "image/png"},
    )
    usage = Usage()
    loop = RadarLoop(hass, ZURICH, usage)
    loop.add(rain())

    await loop.gif()

    assert usage.downloaded == 1000 + REQUEST_OVERHEAD


# What wms.geo.admin.ch answers for a map it cannot draw (checked).
WMS_ERROR = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n<ServiceExceptionReport '
    b'version="1.3.0"><ServiceException code="LayerNotDefined">'
    b"Invalid layer(s)</ServiceException></ServiceExceptionReport>"
)


@pytest.mark.parametrize(
    ("content", "content_type"),
    [(WMS_ERROR, "text/xml"), (b"not a png", "image/png")],
    ids=["an error report", "an unreadable image"],
)
async def test_a_bad_map_is_a_blank_one_and_asked_again(
    hass, aioclient_mock, content, content_type
):
    aioclient_mock.get(
        MAP_URL.split("?")[0], content=content, headers={"content-type": content_type}
    )
    loop = RadarLoop(hass, ZURICH)
    loop.add(rain({ZURICH: 3.0}))

    gif = await loop.gif()

    assert gif.startswith(b"GIF89a")
    assert await loop.gif() is gif
    loop.add(rain(time=SLOT + radar.INTERVAL))
    assert (await loop.gif()).startswith(b"GIF89a")
    assert aioclient_mock.call_count == 2


def test_no_data_is_hatched_and_dry_is_clear():
    frame, _ = crop(rain(), 49.3, 2.75)

    alpha = np.array(_colours(frame))[..., 3]

    nodata = np.isnan(frame).repeat(SCALE, axis=0).repeat(SCALE, axis=1)
    assert alpha[nodata].any()
    assert not alpha[~nodata].any()
