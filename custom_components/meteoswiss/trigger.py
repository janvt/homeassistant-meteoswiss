"""Triggers of the automation editor, for Home Assistant 2026.11 and newer.

They fire when a weather warning reaches a level, rain starts or stops at
the place, hail is expected, Föhn is expected or pollen crosses a threshold.
Each targets the integration's own sensors: a device or an area as the
target is narrowed to the sensors the trigger is about.  Like Home
Assistant's own, they fire when the state enters what they wait for, not on
every update while it stays there, and not when a sensor comes back from
being unavailable.

Before 2026.11 there is no such API, and there are no triggers.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, override

import voluptuous as vol
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.core import HomeAssistant, State

from custom_components.meteoswiss.automation import (
    DEFAULT_HAIL_PROBABILITY,
    DEFAULT_RAIN_RATE,
    DEFAULT_WARNING_LEVEL,
    FOEHN_OUTLOOK_KEYS,
    FOEHN_TARGETS,
    HAIL_PROBABILITY_KEYS,
    RAIN_RATE_KEYS,
    WARNING_KEYS,
    WARNING_LEVELS,
    WARNING_TYPES,
    foehn_reached,
    is_pollen,
    number,
    own_entities,
    warning_level,
    warning_rank,
)

TRIGGERS: dict[str, Any] = {}

try:
    from homeassistant.const import CONF_OPTIONS
    from homeassistant.helpers.automation import DomainSpec
    from homeassistant.helpers.trigger import (
        ENTITY_STATE_TRIGGER_SCHEMA_WITH_BEHAVIOR,
        EntityNumericalStateCrossedThresholdTriggerBase,
        EntityTriggerBase,
        NotTriggeredReasonReporter,
        TriggerConfig,
    )
except ImportError:  # pragma: no cover - before Home Assistant 2026.11
    pass
else:
    _SENSORS = {SENSOR_DOMAIN: DomainSpec()}

    def _no_report(reason: str, /, **data: Any) -> None:
        pass

    class _EnteredTrigger(EntityTriggerBase):
        """Fires when a sensor's state enters what the trigger waits for."""

        _domain_specs = _SENSORS
        _is_key: Callable[[str], bool]

        def matches(self, state: State) -> bool | None:
            """Whether a state is what the trigger waits for, None if no value."""
            raise NotImplementedError

        @override
        def entity_filter(self, entities: set[str]) -> set[str]:
            return own_entities(
                self._hass, super().entity_filter(entities), type(self)._is_key
            )

        @override
        def is_valid_state(
            self, state: State, report_not_triggered: NotTriggeredReasonReporter
        ) -> bool:
            matches = self.matches(state)
            if matches is None:
                report_not_triggered("state_not_valid", entity_id=state.entity_id)
                return False
            return matches

        @override
        def is_valid_transition(self, from_state: State, to_state: State) -> bool:
            return not self.is_valid_state(from_state, _no_report)

    def _options_schema(options: dict[Any, Any]) -> Any:
        return ENTITY_STATE_TRIGGER_SCHEMA_WITH_BEHAVIOR.extend(
            {vol.Required(CONF_OPTIONS, default={}): options}
        )

    class WarningReached(_EnteredTrigger):
        """A weather warning reaches a level, optionally only of some types."""

        _is_key = WARNING_KEYS.__contains__
        _schema = _options_schema(
            {
                vol.Optional("level", default=DEFAULT_WARNING_LEVEL): vol.In(
                    WARNING_LEVELS
                ),
                vol.Optional("types", default=[]): [vol.In(WARNING_TYPES)],
            }
        )

        def __init__(self, hass: HomeAssistant, config: TriggerConfig) -> None:
            super().__init__(hass, config)
            self._minimum = warning_rank(self._options["level"])
            self._types = frozenset(self._options["types"])

        @override
        def matches(self, state: State) -> bool | None:
            level = warning_level(state, self._types)
            return None if level is None else level >= self._minimum

    _RATE = {
        vol.Optional("rate", default=DEFAULT_RAIN_RATE): vol.All(
            vol.Coerce(float), vol.Range(min=0)
        )
    }

    class RainStarted(_EnteredTrigger):
        """The radar's rain rate at the place rises to a rate."""

        _is_key = RAIN_RATE_KEYS.__contains__
        _schema = _options_schema(_RATE)

        @override
        def matches(self, state: State) -> bool | None:
            rate = number(state)
            return None if rate is None else rate >= float(self._options["rate"])

    class RainStopped(_EnteredTrigger):
        """The radar's rain rate at the place falls below a rate."""

        _is_key = RAIN_RATE_KEYS.__contains__
        _schema = _options_schema(_RATE)

        @override
        def matches(self, state: State) -> bool | None:
            rate = number(state)
            return None if rate is None else rate < float(self._options["rate"])

    class HailExpected(_EnteredTrigger):
        """The radar's probability of hail at the place rises to a percentage."""

        _is_key = HAIL_PROBABILITY_KEYS.__contains__
        _schema = _options_schema(
            {
                vol.Optional("probability", default=DEFAULT_HAIL_PROBABILITY): vol.All(
                    vol.Coerce(float), vol.Range(min=1, max=100)
                )
            }
        )

        @override
        def matches(self, state: State) -> bool | None:
            probability = number(state)
            if probability is None:
                return None
            return probability >= float(self._options["probability"])

    class FoehnExpected(_EnteredTrigger):
        """The Föhn outlook reaches a strength in a direction."""

        _is_key = FOEHN_OUTLOOK_KEYS.__contains__
        _schema = _options_schema(
            {vol.Optional("outlook", default="south"): vol.In(FOEHN_TARGETS)}
        )

        @override
        def matches(self, state: State) -> bool | None:
            return foehn_reached(state, self._options["outlook"])

    class PollenCrossedThreshold(EntityNumericalStateCrossedThresholdTriggerBase):
        """A concentration of pollen crosses a threshold."""

        _domain_specs = _SENSORS
        _valid_unit = "pollen/m³"

        @override
        def entity_filter(self, entities: set[str]) -> set[str]:
            return own_entities(self._hass, super().entity_filter(entities), is_pollen)

    TRIGGERS.update(
        {
            "warning_reached": WarningReached,
            "rain_started": RainStarted,
            "rain_stopped": RainStopped,
            "hail_expected": HailExpected,
            "foehn_expected": FoehnExpected,
            "pollen_crossed_threshold": PollenCrossedThreshold,
        }
    )


async def async_get_triggers(hass: HomeAssistant) -> dict[str, Any]:
    """The triggers of MeteoSwiss."""
    return TRIGGERS
