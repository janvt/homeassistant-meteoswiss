"""Days worth skiing: fresh snow, then sun and little wind, from the forecast."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, override

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from custom_components.meteoswiss import summaries
from custom_components.meteoswiss.const import (
    BLUEBIRD_DEFAULTS,
    CONF_BLUEBIRD_GUST,
    CONF_BLUEBIRD_SNOW,
    CONF_BLUEBIRD_SUNSHINE,
    CONF_PTARMIGAN_GUST,
    CONF_PTARMIGAN_SNOW,
    CONF_PTARMIGAN_SUNSHINE,
)
from custom_components.meteoswiss.coordinator import (
    MeteoSwissConfigEntry,
    MeteoSwissDataUpdateCoordinator,
)
from custom_components.meteoswiss.entity import MeteoSwissEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class BluebirdDescription(BinarySensorEntityDescription):
    """On with at least the snow (mm of water) in the snow window, at least
    the hours of sun between 09:00 and 15:00 on the day, and gusts below the
    limit (km/h) then.  The limits are the entry's options of these names.
    """

    snow: Callable[[datetime], summaries.Window]
    day: Callable[..., summaries.Window]
    min_snow: str
    min_sunshine: str
    max_gust: str


BLUEBIRDS: tuple[BluebirdDescription, ...] = (
    # Fresh powder tonight, then a sunny, calm day.
    BluebirdDescription(
        key="bluebird_tomorrow",
        translation_key="bluebird_tomorrow",
        snow=summaries.tonight,
        day=summaries.tomorrow,
        min_snow=CONF_BLUEBIRD_SNOW,
        min_sunshine=CONF_BLUEBIRD_SUNSHINE,
        max_gust=CONF_BLUEBIRD_GUST,
    ),
    # A day further ahead and looser: the snow from today's midnight to that
    # morning, and some sun.
    BluebirdDescription(
        key="ptarmigan",
        translation_key="ptarmigan",
        snow=summaries.until_day_after_tomorrow,
        day=summaries.day_after_tomorrow,
        min_snow=CONF_PTARMIGAN_SNOW,
        min_sunshine=CONF_PTARMIGAN_SUNSHINE,
        max_gust=CONF_PTARMIGAN_GUST,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MeteoSwissConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities(
        [MeteoSwissBluebirdSensor(entry.runtime_data, d) for d in BLUEBIRDS], True
    )


class MeteoSwissBluebirdSensor(
    MeteoSwissEntity[MeteoSwissDataUpdateCoordinator],
    BinarySensorEntity,
):
    """A bluebird day at the place, with the values it was judged on.

    Unavailable when any of them is unknown, e.g. without hourly
    temperatures, rather than off.
    """

    entity_description: BluebirdDescription

    def __init__(
        self,
        coordinator: MeteoSwissDataUpdateCoordinator,
        description: BluebirdDescription,
    ) -> None:
        super().__init__(coordinator, Platform.BINARY_SENSOR, description.key)
        self.entity_description = description

    def _values(self) -> dict[str, float | None]:
        forecast = self.coordinator.data["forecast"]
        if not forecast:
            return {"snow": None, "sunshine": None, "max_gust": None}
        hourly = forecast["hourly"]
        now = datetime.now(timezone.utc)
        d = self.entity_description
        midday = d.day(now, *summaries.MIDDAY)
        return {
            "snow": summaries.snow(hourly, d.snow(now)),
            "sunshine": summaries.sunshine_hours(hourly, midday),
            "max_gust": summaries.max_gust(hourly, midday),
        }

    @property
    @override
    def is_on(self) -> bool | None:
        v = self._values()
        snow, sunshine, gust = v["snow"], v["sunshine"], v["max_gust"]
        if snow is None or sunshine is None or gust is None:
            return None
        d = self.entity_description
        options = {**BLUEBIRD_DEFAULTS, **self.coordinator.config_entry.options}

        def limit(key: str) -> float:
            return float(options[key])

        return (
            snow >= limit(d.min_snow)
            and sunshine >= limit(d.min_sunshine)
            and gust < limit(d.max_gust)
        )

    @property
    @override
    def available(self) -> bool:
        return super().available and self.is_on is not None

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._values()
