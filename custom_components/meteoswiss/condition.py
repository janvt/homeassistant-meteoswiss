"""Conditions of the automation editor, for Home Assistant 2026.11 and newer.

Whether a weather warning of a level is in force, optionally of some types,
and whether it is raining at the place by the radar.  Like the triggers,
they target the integration's own sensors.

Before 2026.11 there is no such API, and there are no conditions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, override

import voluptuous as vol
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.core import HomeAssistant, State

from custom_components.meteoswiss.automation import (
    DEFAULT_RAIN_RATE,
    DEFAULT_WARNING_LEVEL,
    RAIN_RATE_KEYS,
    WARNING_KEYS,
    WARNING_LEVELS,
    WARNING_TYPES,
    number,
    own_entities,
    warning_level,
    warning_rank,
)

CONDITIONS: dict[str, Any] = {}

try:
    from homeassistant.const import CONF_OPTIONS
    from homeassistant.helpers.automation import DomainSpec
    from homeassistant.helpers.condition import (
        ENTITY_STATE_CONDITION_SCHEMA_ANY_ALL,
        ConditionConfig,
        EntityConditionBase,
    )
except ImportError:  # pragma: no cover - before Home Assistant 2026.11
    pass
else:
    _SENSORS = {SENSOR_DOMAIN: DomainSpec()}

    class IsWarning(EntityConditionBase):
        """A weather warning of a level or above, optionally of some types."""

        _domain_specs = _SENSORS
        _schema = ENTITY_STATE_CONDITION_SCHEMA_ANY_ALL.extend(
            {
                vol.Required(CONF_OPTIONS, default={}): {
                    vol.Optional("level", default=DEFAULT_WARNING_LEVEL): vol.In(
                        WARNING_LEVELS
                    ),
                    vol.Optional("types", default=[]): [vol.In(WARNING_TYPES)],
                }
            }
        )

        def __init__(self, hass: HomeAssistant, config: ConditionConfig) -> None:
            super().__init__(hass, config)
            options = config.options or {}
            self._minimum = warning_rank(options["level"])
            self._types = frozenset(options["types"])

        @override
        def entity_filter(self, entities: set[str]) -> set[str]:
            return own_entities(
                self._hass, super().entity_filter(entities), WARNING_KEYS.__contains__
            )

        @override
        def is_valid_state(self, entity_state: State) -> bool:
            level = warning_level(entity_state, self._types)
            return level is not None and level >= self._minimum

        @override
        def _state_valid_since(self, _state: State) -> datetime:
            # The warnings of a type are attributes, which a state's last
            # change does not follow.
            return _state.last_updated

    class IsRaining(EntityConditionBase):
        """The radar's rain rate at the place is at a rate or above."""

        _domain_specs = _SENSORS
        _schema = ENTITY_STATE_CONDITION_SCHEMA_ANY_ALL.extend(
            {
                vol.Required(CONF_OPTIONS, default={}): {
                    vol.Optional("rate", default=DEFAULT_RAIN_RATE): vol.All(
                        vol.Coerce(float), vol.Range(min=0)
                    ),
                }
            }
        )

        def __init__(self, hass: HomeAssistant, config: ConditionConfig) -> None:
            super().__init__(hass, config)
            self._rate = (config.options or {})["rate"]

        @override
        def entity_filter(self, entities: set[str]) -> set[str]:
            return own_entities(
                self._hass, super().entity_filter(entities), RAIN_RATE_KEYS.__contains__
            )

        @override
        def is_valid_state(self, entity_state: State) -> bool:
            rate = number(entity_state)
            return rate is not None and rate >= self._rate

    CONDITIONS.update({"is_warning": IsWarning, "is_raining": IsRaining})


async def async_get_conditions(hass: HomeAssistant) -> dict[str, Any]:
    """The conditions of MeteoSwiss."""
    return CONDITIONS
