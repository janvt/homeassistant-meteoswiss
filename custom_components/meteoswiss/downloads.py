"""The large forecast files, read for all entries at once."""

from __future__ import annotations

import asyncio
import datetime
import logging

from custom_components.meteoswiss.ogd import MeteoSwissOGD, OGDError, Point

_LOGGER = logging.getLogger(__name__)

# Reading a point's rows takes a couple of hundred requests of the files of
# about 33 MB each, so they are read at most this often rather than for every
# (hourly) forecast run.
REFRESH_INTERVAL = datetime.timedelta(hours=3)


class SharedDownloads:
    """Reads each large file for all entries, keeping the rows of their points."""

    def __init__(self, client: MeteoSwissOGD) -> None:
        self._client = client
        self._lock = asyncio.Lock()
        # The point and parameters each entry needs, by entry ID.
        self._wanted: dict[str, tuple[Point, frozenset[str]]] = {}
        # By parameter: the rows of each point downloaded, and when.
        self._rows: dict[str, dict[tuple[int, int], dict[str, str]]] = {}
        self._fetched: dict[str, datetime.datetime] = {}
        self._runs: dict[str, str] = {}

    def register(self, owner: str, point: Point, parameters: set[str]) -> None:
        self._wanted[owner] = (point, frozenset(parameters))

    def unregister(self, owner: str) -> None:
        self._wanted.pop(owner, None)

    async def get(
        self, point: Point, parameter: str, run: str, now: datetime.datetime
    ) -> dict[str, str] | None:
        """The rows of a point, reading the file if they are old or missing.

        A failed read keeps the previous rows, if any.
        """
        async with self._lock:
            rows = self._rows.get(parameter, {})
            fetched = self._fetched.get(parameter)
            try:
                if fetched is None or now - fetched >= REFRESH_INTERVAL:
                    points = {
                        p.key: p
                        for p, parameters in self._wanted.values()
                        if parameter in parameters
                    }
                    points[point.key] = point
                    self._rows[parameter] = await self._client.get_hourly_rows(
                        list(points.values()), run, parameter
                    )
                    self._fetched[parameter] = now
                    self._runs[parameter] = run
                    _LOGGER.debug(
                        "%s for run %s obtained for %d points",
                        parameter,
                        run,
                        len(points),
                    )
                elif point.key not in rows:
                    # An entry added since: just its point, from the same run.
                    more = await self._client.get_hourly_rows(
                        [point], self._runs[parameter], parameter
                    )
                    self._rows[parameter] = {**rows, **more}
            except OGDError as exc:
                _LOGGER.warning("Cannot update %s: %s", parameter, exc)
            return self._rows.get(parameter, {}).get(point.key)
