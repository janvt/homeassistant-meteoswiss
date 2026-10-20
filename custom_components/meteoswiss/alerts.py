"""MeteoSwiss weather warnings, from the MeteoAlarm feed for Switzerland.

Each warning covers a region (a polygon) between two altitudes, so the
warnings of a place are found from its coordinates and height.

Source: MeteoSwiss, via MeteoAlarm (EUMETNET).
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import TypedDict
from urllib.parse import quote

import aiohttp

_LOGGER = logging.getLogger(__name__)

FEED_URL = "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-switzerland"
# The feed is up to about 800 kB (8 kB per warning), uncompressed, and the
# server supports neither ETags nor ranges.  So it is checked at most this
# often, once for all entries, by asking for its size; and fetched only when
# that changed, a warning in it has run out (it may have been extended), or it
# is this old.  A change that keeps the size exactly is missed until then.
FEED_CHECK = datetime.timedelta(minutes=10)
FEED_MAX_AGE = datetime.timedelta(hours=3)
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)

ATOM = "{http://www.w3.org/2005/Atom}"
CAP = "{urn:oasis:names:tc:emergency:cap:1.2}"

NO_WARNING = "none"
# MeteoAlarm's colours, from the least to the most severe.
LEVELS = ("yellow", "orange", "red")
# Used when a warning's details cannot be fetched.
SEVERITY_LEVELS = {"Moderate": "yellow", "Severe": "orange", "Extreme": "red"}
# MeteoAlarm's pages of Swiss regions are in these languages too.
PAGE_LANGUAGES = ("de", "fr", "it")


class AlertError(Exception):
    """Warnings could not be obtained."""


class WeatherWarning(TypedDict):
    identifier: str
    event: str  # e.g. "Gale force wind", in Home Assistant's language if possible
    type: str  # e.g. "wind", "rain", "thunderstorm"
    level: str  # one of LEVELS
    onset: datetime.datetime
    expires: datetime.datetime
    region: str
    headline: str
    description: str
    url: str  # MeteoAlarm's page of the warning's region, with a map


@dataclass
class _Entry:
    """A warning as listed in the feed."""

    identifier: str
    event: str
    severity: str
    region: str
    onset: datetime.datetime
    expires: datetime.datetime
    polygons: list[list[tuple[float, float]]]
    details_url: str | None


@dataclass
class _Details:
    """A warning's own document: its level, type, altitudes and texts."""

    level: str | None
    type: str
    altitude: float | None  # metres
    ceiling: float | None
    texts: dict[str, tuple[str, str, str]] = field(default_factory=dict)


def _polygon(text: str) -> list[tuple[float, float]]:
    """CAP polygons are "lat,lon lat,lon ..."."""
    points = []
    for pair in text.split():
        lat, _, lon = pair.partition(",")
        points.append((float(lat), float(lon)))
    return points


def inside(lat: float, lon: float, polygon: list[tuple[float, float]]) -> bool:
    """Whether a point is inside a polygon (ray casting)."""
    result = False
    for (lat1, lon1), (lat2, lon2) in zip(polygon, polygon[1:] + polygon[:1]):
        if (lat1 > lat) != (lat2 > lat):
            crossing = lon1 + (lat - lat1) * (lon2 - lon1) / (lat2 - lat1)
            if lon < crossing:
                result = not result
    return result


def parse_feed(xml: bytes) -> list[_Entry]:
    """The current warnings of the Atom feed, leaving out cancellations."""
    entries = []
    for entry in ET.fromstring(xml).iter(f"{ATOM}entry"):
        if entry.findtext(f"{CAP}message_type") == "Cancel":
            continue
        try:
            onset = datetime.datetime.fromisoformat(entry.findtext(f"{CAP}onset"))
            expires = datetime.datetime.fromisoformat(entry.findtext(f"{CAP}expires"))
            polygons = [
                _polygon(p.text or "") for p in entry.iter(f"{CAP}polygon") if p.text
            ]
        except (TypeError, ValueError):
            _LOGGER.debug("Skipping a warning that cannot be read")
            continue
        entries.append(
            _Entry(
                identifier=entry.findtext(f"{CAP}identifier") or "",
                event=entry.findtext(f"{CAP}event") or "",
                severity=entry.findtext(f"{CAP}severity") or "",
                region=(entry.findtext(f"{CAP}areaDesc") or "").strip(),
                onset=onset,
                expires=expires,
                polygons=polygons,
                details_url=next(
                    (
                        link.get("href")
                        for link in entry.iter(f"{ATOM}link")
                        if link.get("type") == "application/cap+xml"
                    ),
                    None,
                ),
            )
        )
    return entries


def _number(text: str | None) -> float | None:
    try:
        return float(text) if text else None
    except ValueError:
        return None


def parse_details(xml: bytes) -> _Details:
    """The level, type, altitudes (metres) and texts of a CAP document."""
    level = None
    kind = ""
    altitude = ceiling = None
    texts = {}
    for info in ET.fromstring(xml).iter(f"{CAP}info"):
        parameters = {
            p.findtext(f"{CAP}valueName"): p.findtext(f"{CAP}value") or ""
            for p in info.iter(f"{CAP}parameter")
        }
        # E.g. "2; yellow; Moderate" and "1; wind".
        colour = parameters.get("awareness_level", "").split(";")
        if len(colour) > 1 and colour[1].strip() in LEVELS:
            level = colour[1].strip()
        awareness_type = parameters.get("awareness_type", "").split(";")
        if len(awareness_type) > 1:
            kind = awareness_type[1].strip()
        area = info.find(f"{CAP}area")
        if area is not None:
            altitude = _number(area.findtext(f"{CAP}altitude"))
            ceiling = _number(area.findtext(f"{CAP}ceiling"))
        language = (info.findtext(f"{CAP}language") or "")[:2]
        texts[language] = (
            info.findtext(f"{CAP}event") or "",
            info.findtext(f"{CAP}headline") or "",
            (info.findtext(f"{CAP}description") or "").strip(),
        )
    return _Details(level, kind, altitude, ceiling, texts)


def warning_page(region: str, language: str) -> str:
    """MeteoAlarm's page of Switzerland, showing only a region's warnings.

    The links in the feed lead to the page of all of Europe instead, and the
    pages of single warnings need an ID that only MeteoAlarm's map has.
    """
    site = language[:2] if language[:2] in PAGE_LANGUAGES else "en"
    return f"https://meteoalarm.org/{site}/live/region/CH?sa={quote(region)}"


def level_between(
    warnings: list[WeatherWarning], start: datetime.datetime, end: datetime.datetime
) -> str:
    """The most severe level of the warnings in force at some time in a window."""
    levels = [w["level"] for w in in_force(warnings, start, end)]
    return max(levels, key=LEVELS.index) if levels else NO_WARNING


def in_force(
    warnings: list[WeatherWarning], start: datetime.datetime, end: datetime.datetime
) -> list[WeatherWarning]:
    return [w for w in warnings if w["onset"] < end and w["expires"] > start]


class MeteoAlarm:
    """Fetches the warnings that cover a place."""

    def __init__(self, session: aiohttp.ClientSession, user_agent: str) -> None:
        self._session = session
        self._headers = {"User-Agent": user_agent}
        self._entries: list[_Entry] | None = None
        self._fetched: datetime.datetime | None = None
        self._checked: datetime.datetime | None = None
        self._size: int | None = None
        self._details: dict[str, _Details | None] = {}
        self._lock = asyncio.Lock()

    async def _get(self, url: str) -> bytes:
        try:
            async with self._session.get(
                url, headers=self._headers, timeout=REQUEST_TIMEOUT
            ) as response:
                response.raise_for_status()
                return await response.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise AlertError(f"Cannot fetch {url}: {exc}") from exc

    async def _feed_size(self) -> int | None:
        """The size of the feed, None if it cannot be had."""
        try:
            async with self._session.head(
                FEED_URL, headers=self._headers, timeout=REQUEST_TIMEOUT
            ) as response:
                response.raise_for_status()
                return int(response.headers["Content-Length"])
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError):
            return None

    async def _feed_changed(self, now: datetime.datetime) -> bool:
        """Whether the feed must be fetched again."""
        if self._entries is None or self._fetched is None:
            return True
        # abs(): the clock may have been set back.
        if self._checked is not None and abs(now - self._checked) < FEED_CHECK:
            return False
        self._checked = now
        if abs(now - self._fetched) >= FEED_MAX_AGE or any(
            self._fetched < e.expires <= now for e in self._entries
        ):
            return True
        size = await self._feed_size()
        return size is None or size != self._size

    async def _current_entries(self, now: datetime.datetime) -> list[_Entry]:
        async with self._lock:
            if await self._feed_changed(now):
                try:
                    body = await self._get(FEED_URL)
                    self._entries = parse_feed(body)
                    self._size = len(body)
                    self._fetched = self._checked = now
                    # Details are only kept for the warnings still listed.
                    listed = {e.identifier for e in self._entries}
                    self._details = {
                        i: d for i, d in self._details.items() if i in listed
                    }
                except (AlertError, ET.ParseError) as exc:
                    if self._entries is None:
                        raise AlertError(str(exc)) from exc
                    _LOGGER.warning("Keeping the previous warnings: %s", exc)
            return self._entries

    async def _details_of(self, entry: _Entry) -> _Details | None:
        """A warning's details, fetched once; None if they cannot be."""
        if entry.identifier not in self._details:
            details = None
            if entry.details_url:
                try:
                    details = parse_details(await self._get(entry.details_url))
                except (AlertError, ET.ParseError) as exc:
                    _LOGGER.warning("Details of warning %s: %s", entry.region, exc)
                    # Try again with the next update rather than keeping None.
                    return None
            self._details[entry.identifier] = details
        return self._details[entry.identifier]

    async def get_warnings(
        self,
        lat: float,
        lon: float,
        height: float | None,
        language: str,
        now: datetime.datetime,
    ) -> list[WeatherWarning]:
        """The warnings for a place that have not expired, soonest first."""
        warnings: list[WeatherWarning] = []
        for entry in await self._current_entries(now):
            if entry.expires <= now or not any(
                inside(lat, lon, polygon) for polygon in entry.polygons
            ):
                continue
            details = await self._details_of(entry)
            if details and height is not None:
                if details.altitude is not None and height < details.altitude:
                    continue
                if details.ceiling is not None and height > details.ceiling:
                    continue
            texts = {} if details is None else details.texts
            event, headline, description = texts.get(language[:2]) or texts.get(
                "en", (entry.event, entry.event, "")
            )
            level = (details and details.level) or SEVERITY_LEVELS.get(entry.severity)
            if level is None:
                continue
            warnings.append(
                {
                    "identifier": entry.identifier,
                    "event": event,
                    "type": details.type if details else "",
                    "level": level,
                    "onset": entry.onset,
                    "expires": entry.expires,
                    "region": entry.region,
                    "headline": headline,
                    "description": description,
                    "url": warning_page(entry.region, language),
                }
            )
        return sorted(warnings, key=lambda w: w["onset"])
