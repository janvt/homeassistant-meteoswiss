"""Föhn from the pressure difference across the Alps, and MeteoSwiss's index.

When the sea-level pressure south of the Alps is a few hPa higher than in the
north, air flows over the main ridge and comes down the northern valleys as
south Föhn: warm, dry and gusty.  The other way round, north Föhn reaches
Ticino.  The difference rises hours before the Föhn breaks through.
"""

from __future__ import annotations

# The outlook, from strong north Föhn to strong south Föhn.
OUTLOOKS = ("strong_north", "north", "none", "south", "strong_south")
# MeteoSwiss's Föhn index of a station (wcc006s0), by its code.
STATION_INDEX = {0: "none", 1: "mixed", 2: "foehn"}


def outlook(
    difference: float | None, threshold: float, strong_threshold: float
) -> str | None:
    """The Föhn outlook for a pressure difference south minus north (hPa)."""
    if difference is None:
        return None
    if difference >= strong_threshold:
        return "strong_south"
    if difference >= threshold:
        return "south"
    if difference <= -strong_threshold:
        return "strong_north"
    if difference <= -threshold:
        return "north"
    return "none"


def difference(south: float | None, north: float | None) -> float | None:
    if south is None or north is None:
        return None
    return round(south - north, 1)


def station_index(code: float | None) -> str | None:
    """MeteoSwiss's measured Föhn at a station: none, mixed or foehn."""
    return None if code is None else STATION_INDEX.get(int(code))
