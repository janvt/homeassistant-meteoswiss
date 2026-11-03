"""The base of the entities of a MeteoSwiss entry."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from custom_components.meteoswiss.coordinator import (
    MeteoSwissDataUpdateCoordinator,
    MeteoSwissRadarCoordinator,
)
from custom_components.meteoswiss.ogd import StationType


class MeteoSwissEntity[
    _CoordinatorT: MeteoSwissDataUpdateCoordinator | MeteoSwissRadarCoordinator
](CoordinatorEntity[_CoordinatorT]):
    """An entity of an entry, on the device of its forecast or of a station.

    Its unique ID is the platform, the entry and a key, e.g.
    "sensor.<entry id>-foehn", or without a key "weather.<entry id>"; they
    must not change, or the entities would lose their settings and history.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: _CoordinatorT,
        platform: Platform,
        key: str | None = None,
        station_type: StationType | None = None,
    ) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{platform}.{entry.entry_id}"
        if key:
            self._attr_unique_id += f"-{key}"
        # The entry's coordinator, also for the entities of its radar.
        forecast = entry.runtime_data
        self._attr_device_info = (
            forecast.device_info
            if station_type is None
            else forecast.station_device_info(station_type)
        )
