"""An animated loop of the latest rain radar images around a place.

Each frame is the rain rate over a square centred on the place, drawn over a
lightened swisstopo map, with its time and a colour legend.  The result is an
animated GIF, which every browser and the companion apps show without help.

The frames are cut from the rain images the radar sensors download anyway,
so the loop downloads nothing of its own but the map, once.  It is drawn only
when it is looked at, at most once per new image.
"""

from __future__ import annotations

import asyncio
import datetime
import io
import logging
import zoneinfo
from typing import TYPE_CHECKING

import aiohttp
import numpy as np
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.meteoswiss.const import USER_AGENT
from custom_components.meteoswiss.radar import INTERVAL, REQUEST_TIMEOUT, Image, lv95

if TYPE_CHECKING:
    from PIL import Image as PILImage
    from PIL import ImageFont

_LOGGER = logging.getLogger(__name__)

FRAMES = 5
RADIUS_KM = 30
# Pixels per km of radar grid; the radar pixels are 1 km.
SCALE = 8
SIZE = 2 * RADIUS_KM * SCALE
LEGEND_HEIGHT = 34
FOOTER_HEIGHT = 16
FRAME_MS = 700
LAST_FRAME_MS = 2000

MAP_URL = (
    "https://wms.geo.admin.ch/?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap"
    "&LAYERS=ch.swisstopo.pixelkarte-grau&STYLES=&CRS=EPSG:2056"
    "&BBOX={0:.0f},{1:.0f},{2:.0f},{3:.0f}&WIDTH={4}&HEIGHT={4}&FORMAT=image/png"
)

# (lower bound in mm/h, colour, opacity).  Light rain lets the map show
# through; heavy rain is nearly opaque.
RAMP = (
    (0.1, (168, 200, 255), 120),
    (0.5, (110, 155, 255), 140),
    (1.0, (47, 108, 246), 160),
    (2.0, (22, 179, 164), 180),
    (5.0, (122, 209, 59), 195),
    (10.0, (245, 211, 28), 210),
    (20.0, (255, 140, 26), 220),
    (40.0, (232, 38, 42), 230),
    (70.0, (176, 25, 158), 235),
)

# Where the radars do not reach: grey hatching.
NO_DATA = (90, 90, 90, 150)

type Box = tuple[float, float, float, float]


def crop(image: Image, lat: float, lon: float) -> tuple[np.ndarray, Box]:
    """The rain rate over the square around a place, and its LV95 box.

    Parts of the square outside the radar image read as no data.
    """
    east, north = lv95(lat, lon)
    row = int((image.north - north) // image.scale) - RADIUS_KM
    column = int((east - image.west) // image.scale) - RADIUS_KM
    side = 2 * RADIUS_KM
    out = np.full((side, side), np.nan, dtype=np.float32)
    rows, columns = image.values.shape
    r0, r1 = max(row, 0), min(row + side, rows)
    c0, c1 = max(column, 0), min(column + side, columns)
    if r0 < r1 and c0 < c1:
        out[r0 - row : r1 - row, c0 - column : c1 - column] = image.values[r0:r1, c0:c1]
    box = (
        image.west + column * image.scale,
        image.north - (row + side) * image.scale,
        image.west + (column + side) * image.scale,
        image.north - row * image.scale,
    )
    return out, box


class RadarLoop:
    """The latest frames around a place, and the loop drawn from them."""

    def __init__(self, hass: HomeAssistant, location: tuple[float, float]) -> None:
        self._hass = hass
        self._location = location
        self._frames: dict[datetime.datetime, np.ndarray] = {}
        self._box: Box | None = None
        # None until a map is in hand: the loop is drawn on a blank one then.
        self._map: PILImage.Image | None = None
        self._drawn: tuple[datetime.datetime, bytes] | None = None
        self._lock = asyncio.Lock()

    @property
    def updated(self) -> datetime.datetime | None:
        """The time of the newest frame."""
        return max(self._frames, default=None)

    def add(self, image: Image) -> None:
        """Take a frame from a rain image, if it is a new one."""
        if image.time in self._frames:
            return
        self._frames[image.time], self._box = crop(image, *self._location)
        newest = max(self._frames)
        self._frames = {
            time: frame
            for time, frame in self._frames.items()
            if newest - time < FRAMES * INTERVAL
        }

    async def gif(self) -> bytes | None:
        """The loop as an animated GIF; None before the first frame."""
        async with self._lock:
            newest = self.updated
            if newest is None or self._box is None:
                return None
            if self._drawn is not None and self._drawn[0] == newest:
                return self._drawn[1]
            if self._map is None:
                # Tried again with each new frame until it works.
                content = await self._fetch_map(self._box)
                self._map = await self._hass.async_add_executor_job(
                    prepare_map, content
                )
            frames = sorted(self._frames.items())
            gif = await self._hass.async_add_executor_job(
                draw, frames, self._map, self._hass.config.time_zone
            )
            self._drawn = (newest, gif)
            return gif

    async def _fetch_map(self, box: Box) -> bytes | None:
        try:
            async with async_get_clientsession(self._hass).get(
                MAP_URL.format(*box, SIZE),
                headers={"User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT,
            ) as response:
                response.raise_for_status()
                # An error is answered with status 200 and an XML report.
                if not (response.content_type or "").startswith("image/"):
                    _LOGGER.debug(
                        "No map for the radar loop: %s %s",
                        response.content_type,
                        (await response.read())[:300],
                    )
                    return None
                return await response.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            _LOGGER.debug("Cannot fetch the map of the radar loop: %s", exc)
            return None


def prepare_map(content: bytes | None) -> PILImage.Image | None:
    """Lighten and flatten the map; None without one that can be read.

    Flattening is not only cosmetic: without the hillshade's texture the GIF
    is a third of the size.
    """
    from PIL import Image as PILImage  # noqa: PLC0415

    if not content:
        return None
    try:
        grey = PILImage.open(io.BytesIO(content)).convert("L").resize((SIZE, SIZE))
    except OSError as exc:  # PIL's UnidentifiedImageError is one
        _LOGGER.debug("Cannot read the map of the radar loop: %s", exc)
        return None
    grey = PILImage.blend(grey, PILImage.new("L", grey.size, 255), 0.45)
    step = 256 // 12
    grey = grey.point(lambda v: min(255, (v // step) * step + step // 2))
    return grey.convert("RGB")


def _colours(rate: np.ndarray) -> PILImage.Image:
    from PIL import Image as PILImage  # noqa: PLC0415

    rgba = np.zeros((*rate.shape, 4), dtype=np.uint8)
    values = np.nan_to_num(rate, nan=0.0)
    for low, rgb, alpha in RAMP:
        mask = values >= low
        rgba[mask, :3] = rgb
        rgba[mask, 3] = alpha
    rgba = rgba.repeat(SCALE, axis=0).repeat(SCALE, axis=1)
    # Hatched where the radars do not reach, so it does not look dry.
    rows, columns = np.indices(rgba.shape[:2])
    nodata = np.isnan(rate).repeat(SCALE, axis=0).repeat(SCALE, axis=1)
    rgba[nodata & ((rows + columns) % 12 < 2)] = NO_DATA
    return PILImage.fromarray(rgba, "RGBA")


def _legend(font: ImageFont.ImageFont) -> PILImage.Image:
    from PIL import Image as PILImage  # noqa: PLC0415
    from PIL import ImageDraw  # noqa: PLC0415

    strip = PILImage.new("RGB", (SIZE, LEGEND_HEIGHT), (255, 255, 255))
    draw = ImageDraw.Draw(strip)
    draw.text((6, 10), "mm/h", font=font, fill=(0, 0, 0))
    width = (SIZE - 70) / len(RAMP)
    for k, (low, rgb, _) in enumerate(RAMP):
        x = 50 + k * width
        draw.rectangle((x, 4, x + width - 2, 16), fill=rgb)
        draw.text((x, 18), f"{low:g}", font=font, fill=(60, 60, 60))
    return strip


def draw(
    frames: list[tuple[datetime.datetime, np.ndarray]],
    base: PILImage.Image | None,
    time_zone: str,
) -> bytes:
    """An animated GIF of the frames, oldest first, on a blank map without base."""
    from PIL import Image as PILImage  # noqa: PLC0415
    from PIL import ImageDraw, ImageFont  # noqa: PLC0415

    if base is None:
        base = PILImage.new("RGB", (SIZE, SIZE), (235, 235, 235))

    zone = zoneinfo.ZoneInfo(time_zone)
    font = ImageFont.load_default(size=18)
    small = ImageFont.load_default(size=12)
    legend = _legend(small)
    newest = frames[-1][0]
    centre = SIZE // 2
    images = []
    for i, (time, rate) in enumerate(frames):
        frame = base.copy()
        layer = _colours(rate)
        frame.paste(layer, (0, 0), layer)
        pen = ImageDraw.Draw(frame)
        pen.ellipse(
            (centre - 7, centre - 7, centre + 7, centre + 7), outline=0, width=3
        )
        pen.ellipse((centre - 3, centre - 3, centre + 3, centre + 3), fill=0)

        # No words, to need no translation: the newest frame is its time.
        minutes = int((newest - time).total_seconds() // 60)
        label = time.astimezone(zone).strftime("%H:%M") + (
            f"  -{minutes} min" if minutes else ""
        )
        pen.rounded_rectangle(
            (8, 8, 24 + pen.textlength(label, font=font), 40), 6, fill=(255, 255, 255)
        )
        pen.text((16, 13), label, font=font, fill=(0, 0, 0))
        for k in range(len(frames)):
            x = SIZE - 16 - (len(frames) - 1 - k) * 14
            pen.ellipse(
                (x - 4, 20, x + 4, 28),
                fill=(0, 0, 0) if k == i else (255, 255, 255),
                outline=(0, 0, 0),
            )

        full = PILImage.new(
            "RGB", (SIZE, SIZE + LEGEND_HEIGHT + FOOTER_HEIGHT), (255, 255, 255)
        )
        full.paste(frame, (0, 0))
        full.paste(legend, (0, SIZE))
        ImageDraw.Draw(full).text(
            (SIZE - 6, SIZE + LEGEND_HEIGHT + 2),
            "Radar © MeteoSwiss · map © swisstopo",
            font=small,
            fill=(110, 110, 110),
            anchor="ra",
        )
        images.append(
            full.quantize(
                colors=96,
                method=PILImage.Quantize.MEDIANCUT,
                dither=PILImage.Dither.NONE,
            )
        )

    buffer = io.BytesIO()
    images[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=images[1:],
        duration=[FRAME_MS] * (len(images) - 1) + [LAST_FRAME_MS],
        loop=0,
        disposal=1,
    )
    return buffer.getvalue()
