"""The radar loop as an image entity."""

from __future__ import annotations

import datetime

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from custom_components.meteoswiss import (
    MeteoSwissConfigEntry,
    MeteoSwissDataUpdateCoordinator,
    MeteoSwissRadarCoordinator,
)
from custom_components.meteoswiss.radar import MAX_AGE


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MeteoSwissConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the radar loop, with the radar."""
    c = entry.runtime_data
    if c.radar is not None:
        async_add_entities([MeteoSwissRadarLoop(hass, entry.entry_id, c, c.radar)])


class MeteoSwissRadarLoop(
    CoordinatorEntity[MeteoSwissRadarCoordinator],
    ImageEntity,
):
    """An animated GIF of the latest rain radar images around the place.

    An image entity rather than a camera: the frontend reloads an image only
    when it changes, every 5 minutes here, where a camera card would fetch it
    again every 10 seconds.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "radar_loop"
    _attr_attribution = "Radar: MeteoSwiss, map: swisstopo"
    _attr_content_type = "image/gif"

    def __init__(
        self,
        hass: HomeAssistant,
        integration_id: str,
        coordinator: MeteoSwissDataUpdateCoordinator,
        radar: MeteoSwissRadarCoordinator,
    ) -> None:
        CoordinatorEntity.__init__(self, radar)
        ImageEntity.__init__(self, hass)
        self._attr_unique_id = f"image.{integration_id}-radar_loop"
        self._attr_device_info = coordinator.device_info

    @property
    def available(self) -> bool:
        """Only while there is a recent frame to show, as for the sensors."""
        updated = self.coordinator.loop.updated
        return (
            super().available
            and updated is not None
            and dt_util.utcnow() - updated <= MAX_AGE
        )

    @property
    def image_last_updated(self) -> datetime.datetime | None:
        return self.coordinator.loop.updated

    async def async_image(self) -> bytes | None:
        return await self.coordinator.loop.gif()
