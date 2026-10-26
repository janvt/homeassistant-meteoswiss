"""What downloads from MeteoSwiss and MeteoAlarm cost lately."""

from __future__ import annotations

import collections
import datetime
import time
from collections.abc import Callable
from typing import Any

import aiohttp
from homeassistant.helpers.storage import Store

# Each request costs about this many bytes of headers.
REQUEST_OVERHEAD = 650
# What was downloaded is counted over this long.
USAGE_PERIOD = datetime.timedelta(hours=24)
# The month's total is written to disk at most this often while downloading.
MONTH_SAVE_DELAY = 60


class Usage:
    """The bytes received over the last USAGE_PERIOD."""

    def __init__(self) -> None:
        # When each response came, and what it cost in bytes.
        self._received: collections.deque[tuple[float, int]] = collections.deque()
        # Told the size of each response too, e.g. MonthToDate.add.
        self.on_count: Callable[[int], None] | None = None

    def count(self, response: aiohttp.ClientResponse, body: int) -> None:
        """Remember what a response cost, as transferred if it can be told."""
        raw = getattr(response.content, "total_raw_bytes", None)
        size = raw if isinstance(raw, int) else body
        size += REQUEST_OVERHEAD
        self._received.append((time.monotonic(), size))
        if self.on_count is not None:
            self.on_count(size)

    @property
    def downloaded(self) -> int:
        """Bytes downloaded in the last USAGE_PERIOD."""
        since = time.monotonic() - USAGE_PERIOD.total_seconds()
        while self._received and self._received[0][0] < since:
            self._received.popleft()
        return sum(size for _, size in self._received)


class MonthToDate:
    """The bytes downloaded this calendar month, kept across restarts.

    What was downloaded in the last MONTH_SAVE_DELAY seconds before Home
    Assistant stops uncleanly is lost.
    """

    def __init__(
        self, store: Store[dict[str, Any]], now: Callable[[], datetime.datetime]
    ) -> None:
        self._store = store
        # The local time, for when the month starts.
        self._now = now
        self._month = ""
        self._bytes = 0

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self._month = data.get("month", "")
        self._bytes = data.get("downloaded", 0)

    async def async_save(self) -> None:
        await self._store.async_save(self._data())

    def add(self, size: int) -> None:
        self._roll()
        self._bytes += size
        self._store.async_delay_save(self._data, MONTH_SAVE_DELAY)

    @property
    def downloaded(self) -> int:
        self._roll()
        return self._bytes

    def _roll(self) -> None:
        month = f"{self._now():%Y-%m}"
        if month != self._month:
            self._month, self._bytes = month, 0

    def _data(self) -> dict[str, Any]:
        return {"month": self._month, "downloaded": self._bytes}
