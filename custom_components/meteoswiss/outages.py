"""Logging what cannot be reached once, rather than with every update."""

from __future__ import annotations

import logging


class Outages:
    """The sources that are down, such as "Station SMA", each logged once.

    A source that fails is logged as a warning the first time, then only at
    debug level until it works again, which is logged once more.
    """

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        self._down: set[str] = set()

    def failed(self, source: str, error: object, trace: bool = False) -> None:
        """Note a failure; trace logs the exception being handled with it."""
        if source in self._down:
            self._logger.debug("%s is still unavailable: %s", source, error)
            return
        self._down.add(source)
        self._logger.warning("%s is unavailable: %s", source, error, exc_info=trace)

    def worked(self, source: str) -> None:
        if source in self._down:
            self._down.remove(source)
            self._logger.info("%s is available again", source)
