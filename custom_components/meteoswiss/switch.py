"""The switch that pauses an entry's downloads."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from custom_components.meteoswiss import (
    MeteoSwissConfigEntry,
    MeteoSwissDataUpdateCoordinator,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MeteoSwissConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([MeteoSwissDownloadsSwitch(entry.entry_id, entry.runtime_data)])


class MeteoSwissDownloadsSwitch(
    CoordinatorEntity[MeteoSwissDataUpdateCoordinator],
    SwitchEntity,
):
    """On while the entry downloads; off pauses it, e.g. from an automation
    when a monthly data allowance is used up.

    While off, the entities keep their last values, and none are fetched:
    no measurements, forecasts, warnings or radar.  The pause is kept across
    restarts.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "downloads"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, integration_id: str, coordinator: MeteoSwissDataUpdateCoordinator
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"switch.{integration_id}-downloads"
        self._attr_device_info = coordinator.device_info

    @property
    def is_on(self) -> bool:
        return not self.coordinator.paused

    @property
    def icon(self) -> str:
        return "mdi:download-network" if self.is_on else "mdi:download-off"

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_paused(False)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_paused(True)
