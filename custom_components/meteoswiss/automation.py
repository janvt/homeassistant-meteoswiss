"""What the triggers and conditions of the automation editor read.

They work on the integration's own sensors, found by the key at the end of
their unique ID ("sensor.<entry id>-<key>"), so that a device or an area as
the target picks only the sensors a trigger is about.  The values are read
from the sensors' states, as the automation sees them.
"""

from __future__ import annotations

import typing

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er

from custom_components.meteoswiss.alerts import LEVELS, NO_WARNING
from custom_components.meteoswiss.const import DOMAIN
from custom_components.meteoswiss.foehn import OUTLOOKS
from custom_components.meteoswiss.radar import HAIL_PROBABILITY, RAIN_RATE

WARNING_KEYS = frozenset({"weather_warning", "weather_warning_next_24h"})
RAIN_RATE_KEYS = frozenset({RAIN_RATE})
HAIL_PROBABILITY_KEYS = frozenset({HAIL_PROBABILITY})
FOEHN_OUTLOOK_KEYS = frozenset({"foehn_outlook"})
POLLEN_PREFIX = "pollen_"

# MeteoAlarm's types of warning, as in a warning's "type", without coastal
# events, which Switzerland has none of.
WARNING_TYPES = (
    "wind",
    "snow-ice",
    "thunderstorm",
    "fog",
    "high-temperature",
    "low-temperature",
    "forest-fire",
    "avalanches",
    "rain",
    "flooding",
    "rain-flood",
)
# The levels a warning trigger or condition can be set to.
WARNING_LEVELS = LEVELS
# The Föhn outlooks a trigger can wait for, in either direction.
FOEHN_TARGETS = ("south", "strong_south", "north", "strong_north")

# A rain rate under which it is dry, in mm/h: the radar's smallest rate
# above 0 is about 0.16 mm/h.
DEFAULT_RAIN_RATE = 0.1
DEFAULT_HAIL_PROBABILITY = 50
DEFAULT_WARNING_LEVEL = "orange"


def _key(unique_id: str) -> str:
    """The key of a unique ID "<platform>.<entry id>-<key>", or ""."""
    return unique_id.partition("-")[2]


def own_entities(
    hass: HomeAssistant,
    entity_ids: set[str],
    match: typing.Callable[[str], bool],
) -> set[str]:
    """The entities of this integration whose key matches."""
    registry = er.async_get(hass)
    return {
        entity_id
        for entity_id in entity_ids
        if (entry := registry.async_get(entity_id)) is not None
        and entry.platform == DOMAIN
        and match(_key(entry.unique_id))
    }


def is_pollen(key: str) -> bool:
    return key.startswith(POLLEN_PREFIX)


def warning_rank(level: str) -> int:
    """0 for no warning, then 1 for yellow up to 3 for red."""
    return LEVELS.index(level) + 1 if level in LEVELS else 0


def warning_level(state: State, types: typing.Collection[str]) -> int | None:
    """The rank of the most severe warning of a warning sensor's state.

    Only of the warnings of some types, if any are given.  None when the
    state is not a level.
    """
    if state.state != NO_WARNING and state.state not in LEVELS:
        return None
    if not types:
        return warning_rank(state.state)
    warnings = state.attributes.get("warnings") or []
    return max(
        (
            warning_rank(w.get("level", ""))
            for w in warnings
            if isinstance(w, dict) and w.get("type") in types
        ),
        default=0,
    )


def number(state: State) -> float | None:
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def foehn_reached(state: State, target: str) -> bool | None:
    """Whether an outlook is the target's or stronger in its direction.

    None when the state is not an outlook.
    """
    if state.state not in OUTLOOKS:
        return None
    # From -2 for strong north Föhn to 2 for strong south Föhn.
    value = OUTLOOKS.index(state.state) - 2
    wanted = OUTLOOKS.index(target) - 2
    return value * wanted > 0 and abs(value) >= abs(wanted)
