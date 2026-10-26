"""Which entries have their downloads paused, kept across restarts.

Paused by the user (a switch, e.g. from an automation on a monthly data
allowance), never by the integration itself.
"""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.storage import Store


class Pauses:
    """The IDs of the entries whose downloads are paused."""

    def __init__(self, store: Store[dict[str, Any]]) -> None:
        self._store = store
        self._paused: set[str] = set()

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self._paused = set(data.get("paused", []))

    def __contains__(self, entry_id: str) -> bool:
        return entry_id in self._paused

    async def async_set(self, entry_id: str, paused: bool) -> None:
        if paused:
            self._paused.add(entry_id)
        else:
            self._paused.discard(entry_id)
        # Right away: a restart soon after must not resume the downloads.
        await self._store.async_save({"paused": sorted(self._paused)})
