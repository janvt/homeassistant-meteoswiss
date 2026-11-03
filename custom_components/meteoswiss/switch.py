"""The switch that pauses an entry's downloads."""

from __future__ import annotations

from typing import Any, override

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from custom_components.meteoswiss.coordinator import (
    MeteoSwissConfigEntry,
    MeteoSwissDataUpdateCoordinator,
)
from custom_components.meteoswiss.entity import MeteoSwissEntity

# Turning downloads back on refreshes the entry: one at a time.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MeteoSwissConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([MeteoSwissDownloadsSwitch(entry.runtime_data)])


class MeteoSwissDownloadsSwitch(
    MeteoSwissEntity[MeteoSwissDataUpdateCoordinator],
    SwitchEntity,
):
    """On while the entry downloads; off pauses it, e.g. from an automation
    when a monthly data allowance is used up.

    While off, the entities keep their last values, and none are fetched:
    no measurements, forecasts, warnings or radar.  The pause is kept across
    restarts.
    """

    _attr_translation_key = "downloads"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: MeteoSwissDataUpdateCoordinator) -> None:
        super().__init__(coordinator, Platform.SWITCH, "downloads")

    @property
    @override
    def is_on(self) -> bool:
        return not self.coordinator.paused

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_paused(False)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_paused(True)
