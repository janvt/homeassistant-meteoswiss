"""The radar loop as an image entity."""

from __future__ import annotations

import datetime
from typing import override

from homeassistant.components.image import ImageEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from custom_components.meteoswiss.coordinator import (
    MeteoSwissConfigEntry,
    MeteoSwissRadarCoordinator,
)
from custom_components.meteoswiss.entity import MeteoSwissEntity
from custom_components.meteoswiss.radar import MAX_AGE

# Updated by the radar's coordinator, so without limit.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MeteoSwissConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the radar loop, with the radar."""
    c = entry.runtime_data
    if c.radar is not None:
        async_add_entities([MeteoSwissRadarLoop(hass, c.radar)])


class MeteoSwissRadarLoop(
    MeteoSwissEntity[MeteoSwissRadarCoordinator],
    ImageEntity,
):
    """An animated GIF of the latest rain radar images around the place.

    An image entity rather than a camera: the frontend reloads an image only
    when it changes, every 5 minutes here, where a camera card would fetch it
    again every 10 seconds.
    """

    _attr_translation_key = "radar_loop"
    _attr_attribution = "Radar: MeteoSwiss, map: swisstopo"
    _attr_content_type = "image/gif"

    def __init__(self, hass: HomeAssistant, radar: MeteoSwissRadarCoordinator) -> None:
        MeteoSwissEntity.__init__(self, radar, Platform.IMAGE, "radar_loop")
        ImageEntity.__init__(self, hass)

    @property
    @override
    def available(self) -> bool:
        """Only while there is a recent frame to show, as for the sensors."""
        updated = self.coordinator.loop.updated
        return (
            super().available
            and updated is not None
            and dt_util.utcnow() - updated <= MAX_AGE
        )

    @property
    @override
    def image_last_updated(self) -> datetime.datetime | None:
        return self.coordinator.loop.updated

    @override
    async def async_image(self) -> bytes | None:
        return await self.coordinator.loop.gif()
