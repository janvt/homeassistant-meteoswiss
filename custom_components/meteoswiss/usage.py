"""What downloads from MeteoSwiss and MeteoAlarm cost lately."""

from __future__ import annotations

import collections
import datetime
import time

import aiohttp

# Each request costs about this many bytes of headers.
REQUEST_OVERHEAD = 650
# What was downloaded is counted over this long.
USAGE_PERIOD = datetime.timedelta(hours=24)


class Usage:
    """The bytes received over the last USAGE_PERIOD."""

    def __init__(self) -> None:
        # When each response came, and what it cost in bytes.
        self._received: collections.deque[tuple[float, int]] = collections.deque()

    def count(self, response: aiohttp.ClientResponse, body: int) -> None:
        """Remember what a response cost, as transferred if it can be told."""
        raw = getattr(response.content, "total_raw_bytes", None)
        size = raw if isinstance(raw, int) else body
        self._received.append((time.monotonic(), REQUEST_OVERHEAD + size))

    @property
    def downloaded(self) -> int:
        """Bytes downloaded in the last USAGE_PERIOD."""
        since = time.monotonic() - USAGE_PERIOD.total_seconds()
        while self._received and self._received[0][0] < since:
            self._received.popleft()
        return sum(size for _, size in self._received)
