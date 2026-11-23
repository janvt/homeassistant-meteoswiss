"""Logging what cannot be reached once, rather than with every update, and
telling the user in Repairs while it cannot be."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.issue_registry import IssueSeverity
from homeassistant.util import slugify

from custom_components.meteoswiss.const import DOMAIN

# A failure is cut to this many characters in the repair.
ERROR_LENGTH = 300


class Outages:
    """The sources that are down, such as "Station SMA", each logged once.

    A source that fails is logged as a warning the first time, then only at
    debug level until it works again, which is logged once more.  The failure
    comes after the request was tried again (see fetch.retrying), so with
    hass, it is also a repair while it lasts.  place, such as an entry's
    name, tells the user whose source it is; owner, such as its entry ID,
    keeps the repairs of different entries apart.
    """

    def __init__(
        self,
        logger: logging.Logger,
        hass: HomeAssistant | None = None,
        place: str | None = None,
        owner: str = "",
    ) -> None:
        self._logger = logger
        self._hass = hass
        self._place = place
        self._owner = owner
        self._down: set[str] = set()

    def failed(
        self,
        source: str,
        error: object,
        trace: bool = False,
        repair: bool = True,
        log: bool = True,
    ) -> None:
        """Note a failure; trace logs the exception being handled with it.

        repair=False for a failure with its own repair, such as a station
        without data; log=False for one already logged elsewhere.
        """
        if source in self._down:
            self._logger.debug("%s is still unavailable: %s", source, error)
            return
        self._down.add(source)
        if log:
            self._logger.warning("%s is unavailable: %s", source, error, exc_info=trace)
        if repair and self._hass is not None:
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                self.issue_id(source),
                is_fixable=False,
                is_persistent=False,
                severity=IssueSeverity.WARNING,
                translation_key="source_unavailable",
                translation_placeholders={
                    "source": self._label(source),
                    "error": str(error)[:ERROR_LENGTH],
                },
            )

    def worked(self, source: str) -> None:
        if source in self._down:
            self._down.remove(source)
            self._logger.info("%s is available again", source)
        # Also a repair left by the entry's setup that failed and was retried.
        self._delete_issue(source)

    def clear(self) -> None:
        """Forget every outage, and its repair, as when the entry is unloaded."""
        for source in self._down:
            self._delete_issue(source)
        self._down.clear()

    def issue_id(self, source: str) -> str:
        return "unavailable_" + slugify(f"{self._owner} {source}")

    def _label(self, source: str) -> str:
        return f"{source} ({self._place})" if self._place else source

    def _delete_issue(self, source: str) -> None:
        if self._hass is not None:
            ir.async_delete_issue(self._hass, DOMAIN, self.issue_id(source))
