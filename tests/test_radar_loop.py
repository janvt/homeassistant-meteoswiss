"""The animated loop of the rain radar around a place."""

import math

import numpy as np
import pytest

from custom_components.meteoswiss import radar
from custom_components.meteoswiss.radar_loop import (
    FRAMES,
    MAP_URL,
    RADIUS_KM,
    SCALE,
    RadarLoop,
    _colours,
    crop,
)

from .test_radar import SLOT, ZURICH, image


def rain(values=None, time=SLOT) -> radar.Image:
    return radar.Image.parse(image(values), time, 1.0)


def test_the_place_is_in_the_middle_of_the_square():
    frame, box = crop(rain({ZURICH: 7.0}), *ZURICH)

    assert frame.shape == (2 * RADIUS_KM, 2 * RADIUS_KM)
    assert frame[RADIUS_KM, RADIUS_KM] == 7.0
    assert np.nansum(frame) == 7.0
    west, south, east, north = box
    assert east - west == north - south == 2 * RADIUS_KM * 1000
    # The place's pixel is the one east and south of the box's centre.
    x, y = radar.lv95(*ZURICH)
    assert math.floor((x - west) / 1000) == RADIUS_KM
    assert math.floor((north - y) / 1000) == RADIUS_KM


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
