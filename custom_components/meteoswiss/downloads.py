"""The large forecast files, downloaded once for all entries."""

from __future__ import annotations

import asyncio
import datetime
import logging

from custom_components.meteoswiss.ogd import MeteoSwissOGD, OGDError, Point

_LOGGER = logging.getLogger(__name__)

# The files are about 33 MB each, so they are downloaded at most this often
# rather than for every (hourly) forecast run.
REFRESH_INTERVAL = datetime.timedelta(hours=3)


class SharedDownloads:
    """Downloads each large file once, keeping the rows of every entry's point."""

    def __init__(self, client: MeteoSwissOGD) -> None:
        self._client = client
        self._lock = asyncio.Lock()
        # The point and parameters each entry needs, by entry ID.
        self._wanted: dict[str, tuple[Point, frozenset[str]]] = {}
        # By parameter: the rows of each point downloaded, and when.
        self._rows: dict[str, dict[tuple[int, int], dict[str, str]]] = {}
        self._fetched: dict[str, datetime.datetime] = {}

    def register(self, owner: str, point: Point, parameters: set[str]) -> None:
        self._wanted[owner] = (point, frozenset(parameters))

    def unregister(self, owner: str) -> None:
        self._wanted.pop(owner, None)

    async def get(
        self, point: Point, parameter: str, run: str, now: datetime.datetime
    ) -> dict[str, str] | None:
        """The rows of a point, downloading the file if they are old or missing.

        A failed download keeps the previous rows, if any.
        """
        async with self._lock:
            rows = self._rows.get(parameter, {})
            fetched = self._fetched.get(parameter)
            if (
                fetched is None
                or now - fetched >= REFRESH_INTERVAL
                # An entry that was added since.
                or point.key not in rows
            ):
                points = {
                    p.key: p
                    for p, parameters in self._wanted.values()
                    if parameter in parameters
                }
                points[point.key] = point
                try:
                    self._rows[parameter] = await self._client.get_hourly_rows(
                        list(points.values()), run, parameter
                    )
                    self._fetched[parameter] = now
                    _LOGGER.debug(
                        "%s for run %s obtained for %d points",
                        parameter,
                        run,
                        len(points),
                    )
                except OGDError as exc:
                    _LOGGER.warning("Cannot update %s: %s", parameter, exc)
            return self._rows.get(parameter, {}).get(point.key)
