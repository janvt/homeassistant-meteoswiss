"""Rain and hail now, and the rain of the last hour, from MeteoSwiss's weather
radar (open data).

Each product is an image of all of Switzerland, 640 by 710 pixels of 1 km,
every 5 minutes, published a minute or so after its interval ends (the rain
of the last hour about 4 minutes).  An image is compressed as one block, 23 to
150 kB, so it is downloaded whole; once for all entries.
"""

from __future__ import annotations

import asyncio
import datetime
import io
import json
import logging
import math
from dataclasses import dataclass

import aiohttp
import numpy as np

from custom_components.meteoswiss.usage import Usage

_LOGGER = logging.getLogger(__name__)

FILE_URL = "https://data.geo.admin.ch/{collection}/{day}-ch/{name}"
ITEM_URL = (
    "https://data.geo.admin.ch/api/stac/v1/collections/{collection}/items/{day}-ch"
)
PRECIPITATION = "ch.meteoschweiz.ogd-radar-precip"
HAIL = "ch.meteoschweiz.ogd-radar-hail"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)

INTERVAL = datetime.timedelta(minutes=5)
# A reading is kept this long, so that a late image or a failed download does
# not make the sensors unavailable.
MAX_AGE = datetime.timedelta(minutes=20)
# File names end in letters telling which radars contributed: usually the same
# for days at a time, so the last that worked is tried first, then these.
VARIANTS = ("rl", "vl", "pl")
# An image this late is looked up in the day's list of files (up to 280 kB),
# at most this often for each collection.  The list lags the files by 4 to 6
# minutes, so the latest image it lists gives the variant; it is read again
# each minute while that does not find the image, rather than wait.
LOOKUP_AFTER = datetime.timedelta(minutes=3)
LOOKUP_EVERY = datetime.timedelta(minutes=1)
# After this long without rain at or next to a place, the rain of the last
# hour there is nothing, and its images are not downloaded.
DRY_HOUR = datetime.timedelta(hours=1)


@dataclass(frozen=True)
class Product:
    collection: str
    stem: str
    suffix: str
    # Values are multiplied by this: hail probability is a fraction.
    scale: float = 1.0
    # The variants to try, the last that worked first.  If these are all the
    # variants there are, a late image is not looked up in the day's list.
    variants: tuple[str, ...] = VARIANTS
    all_variants: bool = False
    # How long after its interval ends an image is usually published.
    delay: datetime.timedelta = datetime.timedelta(0)

    @property
    def family(self) -> tuple[str, tuple[str, ...]]:
        """Products whose images end in the same variant at the same time.

        The hail products share theirs, as they share the radars; the rain of
        the last hour has digits of its own.
        """
        return self.collection, self.variants


RAIN_RATE = "rain_rate"
HAIL_PROBABILITY = "hail_probability"
HAIL_SIZE = "hail_size"
RAIN_LAST_HOUR = "rain_last_hour"
PRODUCTS = {
    RAIN_RATE: Product(PRECIPITATION, "rzc", ".001.h5"),  # mm/h
    HAIL_PROBABILITY: Product(HAIL, "bzc", ".845.h5", 100.0),  # %
    HAIL_SIZE: Product(HAIL, "mzc", ".850.h5"),  # mm
    # CombiPrecip: the radar adjusted to the rain gauges, the best estimate of
    # the rain that reached the ground in the hour to the image's time, in mm.
    # Its variant is a digit that changes from hour to hour.
    RAIN_LAST_HOUR: Product(
        PRECIPITATION,
        "cpc",
        "_00060.001.h5",
        variants=tuple("0123456789"),
        all_variants=True,
        delay=datetime.timedelta(minutes=4),
    ),
}


class RadarError(Exception):
    """An image cannot be read."""


def _stamp(product: Product, slot: datetime.datetime) -> str:
    """The start of an image's file name, up to its variant: the interval's end."""
    return f"{product.stem}{slot:%y}{slot.timetuple().tm_yday:03d}{slot:%H%M}"


def _url(product: Product, slot: datetime.datetime, variant: str) -> str:
    return FILE_URL.format(
        collection=product.collection,
        day=f"{slot:%Y%m%d}",
        name=f"{_stamp(product, slot)}{variant}{product.suffix}",
    )


def lv95(lat: float, lon: float) -> tuple[float, float]:
    """WGS84 to Swiss LV95 coordinates (swisstopo's approximation, within 1 m)."""
    phi = (lat * 3600 - 169028.66) / 10000
    lam = (lon * 3600 - 26782.5) / 10000
    east = (
        2600072.37
        + 211455.93 * lam
        - 10938.51 * lam * phi
        - 0.36 * lam * phi**2
        - 44.54 * lam**3
    )
    north = (
        1200147.07
        + 308807.95 * phi
        + 3745.25 * lam**2
        + 76.63 * phi**2
        - 194.56 * lam**2 * phi
        + 119.79 * phi**3
    )
    return east, north


@dataclass
class Image:
    """A radar image: values by row (north first) and column (west first)."""

    time: datetime.datetime
    values: np.ndarray
    west: float
    north: float
    scale: float

    @classmethod
    def parse(cls, body: bytes, time: datetime.datetime, factor: float) -> Image:
        try:
            import pyfive  # noqa: PLC0415  # only needed with the radar on

            file = pyfive.File(io.BytesIO(body))
            data = file["dataset1/data1/data"][:]
            where = file["where"].attrs
            what = file["dataset1/data1/what"].attrs
            gain, offset = float(what["gain"]), float(what["offset"])
            # The north-west corner in LV95, rounded to the grid.
            west, north = lv95(float(where["UL_lat"]), float(where["UL_lon"]))
            scale = float(where["xscale"])
        except Exception as exc:  # pyfive raises many kinds on a bad file
            raise RadarError(f"Cannot read radar image: {exc}") from exc
        return cls(
            time,
            ((data * gain + offset) * factor).astype(np.float32),
            round(west / scale) * scale,
            round(north / scale) * scale,
            scale,
        )

    def _pixel(self, lat: float, lon: float) -> tuple[int, int] | None:
        east, north = lv95(lat, lon)
        row = math.floor((self.north - north) / self.scale)
        column = math.floor((east - self.west) / self.scale)
        rows, columns = self.values.shape
        if 0 <= row < rows and 0 <= column < columns:
            return row, column
        return None

    def value(self, lat: float, lon: float) -> float | None:
        """The value at a place; None outside the image or where unmeasured."""
        if (pixel := self._pixel(lat, lon)) is None:
            return None
        value = float(self.values[pixel])
        return None if math.isnan(value) else value

    def around(self, lat: float, lon: float) -> float:
        """The highest value at a place and the pixels next to it."""
        if (pixel := self._pixel(lat, lon)) is None:
            return 0.0
        row, column = pixel
        near = self.values[max(0, row - 1) : row + 2, max(0, column - 1) : column + 2]
        near = near[~np.isnan(near)]
        return float(near.max()) if near.size else 0.0


def latest_slot(now: datetime.datetime) -> datetime.datetime:
    """The end of the latest interval that has ended."""
    now = now.astimezone(datetime.timezone.utc).replace(second=0, microsecond=0)
    return now - datetime.timedelta(minutes=now.minute % 5)


class Radar:
    """Fetches the latest radar images, once for all entries."""

    def __init__(self, session: aiohttp.ClientSession, user_agent: str) -> None:
        self._session = session
        self._headers = {"User-Agent": user_agent}
        self._images: dict[str, Image] = {}
        # By Product.family.
        self._variants: dict[tuple[str, tuple[str, ...]], str] = {}
        self._looked_up: dict[tuple[str, tuple[str, ...]], datetime.datetime] = {}
        # Since when the rain images have shown no rain at or next to a place.
        self._dry_since: dict[tuple[float, float], datetime.datetime] = {}
        self._lock = asyncio.Lock()
        self.usage = Usage()

    @property
    def downloaded(self) -> int:
        """Bytes of radar images and lists downloaded in the last day."""
        return self.usage.downloaded

    def image(self, key: str) -> Image | None:
        """The latest image of a product, if one was downloaded."""
        return self._images.get(key)

    async def _fetch(self, url: str) -> bytes | None:
        """A file, or None if it is not (yet) published."""
        try:
            async with self._session.get(
                url, headers=self._headers, timeout=REQUEST_TIMEOUT
            ) as response:
                # The file store answers 403 for files it does not have.
                if response.status in (403, 404):
                    self.usage.count(response, 0)
                    return None
                response.raise_for_status()
                body = await response.read()
                self.usage.count(response, len(body))
                return body
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise RadarError(f"Cannot fetch {url}: {exc}") from exc

    async def _variant(self, product: Product, slot: datetime.datetime) -> str | None:
        """An image's likely variant: that of the latest image listed up to it.

        The day's list may not have the image yet; early in the day it may
        have none, so the day before's is read then.
        """
        stamp = _stamp(product, slot)
        for day in (slot, slot - datetime.timedelta(days=1)):
            url = ITEM_URL.format(collection=product.collection, day=f"{day:%Y%m%d}")
            if (body := await self._fetch(url)) is None:
                continue
            try:
                assets = json.loads(body).get("assets", {})
            except ValueError as exc:
                raise RadarError(f"Cannot read {url}: {exc}") from exc
            names = [
                name
                for name in assets
                if name.startswith(product.stem)
                and name.endswith(product.suffix)
                and name[: len(stamp)] <= stamp
            ]
            if names:
                name = max(names)
                return name[len(stamp) : -len(product.suffix)]
        return None

    async def _image(
        self,
        product: Product,
        slot: datetime.datetime,
        now: datetime.datetime,
        look_up: bool = True,
    ) -> bytes | None:
        """An image's file, trying the likely variants; None if not published.

        If look_up, an image that is late is looked for in the day's list.
        """
        family = product.family
        known = self._variants.get(family)
        for variant in dict.fromkeys((*((known,) if known else ()), *product.variants)):
            if (body := await self._fetch(_url(product, slot, variant))) is not None:
                self._variants[family] = variant
                return body
        looked_up = self._looked_up.get(family)
        if (
            not look_up
            or product.all_variants
            or now - slot < product.delay + LOOKUP_AFTER
            or (looked_up is not None and now - looked_up < LOOKUP_EVERY)
        ):
            return None
        self._looked_up[family] = now
        variant = await self._variant(product, slot)
        if variant is None or variant in (known, *product.variants):
            return None
        _LOGGER.debug("Radar images of %s now end in %s", product.stem, variant)
        self._variants[family] = variant
        return await self._fetch(_url(product, slot, variant))

    async def _refresh(self, key: str, now: datetime.datetime) -> None:
        """Download the latest image of a product, if it is newer."""
        product = PRODUCTS[key]
        slot = latest_slot(now - product.delay)
        current = self._images.get(key)
        # The latest image, or the one before while that is not published.
        for candidate in (slot, slot - INTERVAL):
            if current is not None and current.time >= candidate:
                return
            body = await self._image(product, candidate, now, candidate == slot)
            if body is not None:
                self._images[key] = Image.parse(body, candidate, product.scale)
                return

    async def get(
        self, lat: float, lon: float, now: datetime.datetime
    ) -> dict[str, tuple[float | None, datetime.datetime] | None]:
        """Each product's value at a place, with the time of its image.

        Hail images are only downloaded while it rains at or next to the
        place: there is no hail without rain.  Images of the rain of the last
        hour are only downloaded until it has been dry there for an hour.  A
        product without a recent image is None.
        """
        async with self._lock:
            for key in PRODUCTS:  # the rain first
                if key != RAIN_RATE and not self._wanted(key, lat, lon):
                    continue
                try:
                    await self._refresh(key, now)
                except RadarError as exc:
                    _LOGGER.warning("Radar %s: %s", key, exc)
                if key == RAIN_RATE:
                    self._note_rain(lat, lon)
        values: dict[str, tuple[float | None, datetime.datetime] | None] = {}
        rain = self._images.get(RAIN_RATE)
        for key in PRODUCTS:
            image = self._images.get(key)
            if (
                key != RAIN_RATE
                and rain is not None
                and self._dry(key, lat, lon)
                and (image is None or image.time < rain.time)
            ):
                # Nothing, as of the latest rain image.
                values[key] = (0.0, rain.time) if now - rain.time <= MAX_AGE else None
            elif image is None or now - image.time > MAX_AGE:
                values[key] = None
            else:
                values[key] = (image.value(lat, lon), image.time)
        return values

    def _note_rain(self, lat: float, lon: float) -> None:
        """Keep since when the latest rain images are dry at a place."""
        if (rain := self._images.get(RAIN_RATE)) is None:
            return
        if rain.around(lat, lon) > 0:
            self._dry_since.pop((lat, lon), None)
        else:
            # After a start, the first dry image: the hour before is unknown.
            self._dry_since.setdefault((lat, lon), rain.time)

    def _dry(self, key: str, lat: float, lon: float) -> bool:
        """Whether the rain images say a product is nothing at a place."""
        if (rain := self._images.get(RAIN_RATE)) is None:
            return False
        if key == RAIN_LAST_HOUR:
            since = self._dry_since.get((lat, lon))
            return since is not None and rain.time - since >= DRY_HOUR
        return rain.around(lat, lon) <= 0

    def _wanted(self, key: str, lat: float, lon: float) -> bool:
        """Whether to download a product's images for a place.

        Not hail without a rain image to say it rains; the rain of the last
        hour unless it is known to be nothing.
        """
        if key == RAIN_LAST_HOUR:
            return not self._dry(key, lat, lon)
        return RAIN_RATE in self._images and not self._dry(key, lat, lon)
