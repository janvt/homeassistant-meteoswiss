"""Logging what cannot be reached once, rather than with every update, and
telling the user in Repairs while it cannot be."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.issue_registry import IssueSeverity

from custom_components.meteoswiss.const import DOMAIN

# The services files are fetched from.  One repair each, however many of
# their files fail: when a server is down, all of them do.
METEOSWISS = "meteoswiss"
METEOALARM = "meteoalarm"
SWISSTOPO = "swisstopo"
SERVICES = {
    METEOSWISS: "MeteoSwiss (data.geo.admin.ch)",
    METEOALARM: "MeteoAlarm (feeds.meteoalarm.org)",
    SWISSTOPO: "swisstopo (wms.geo.admin.ch)",
}
# The error and the list of sources are cut to this many characters.
ERROR_LENGTH = 300
SOURCES_LENGTH = 600

DATA_REPAIRS = f"{DOMAIN}_repairs"


def issue_id(service: str) -> str:
    return f"unavailable_{service}"


class ServiceRepairs:
    """The sources of each service that are down, for all entries, and the
    service's repair while any is."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        # By service: the label of each source that is down, by its key.
        self._down: dict[str, dict[tuple[str, str], str]] = {}
        self._errors: dict[str, str] = {}

    def failed(
        self, service: str, key: tuple[str, str], label: str, error: object
    ) -> None:
        self._down.setdefault(service, {})[key] = label
        self._errors[service] = str(error)[:ERROR_LENGTH]
        self._update(service)

    def worked(self, service: str, key: tuple[str, str]) -> None:
        if self._down.get(service, {}).pop(key, None) is not None:
            self._update(service)

    def _update(self, service: str) -> None:
        down = self._down.get(service)
        if not down:
            self._down.pop(service, None)
            self._errors.pop(service, None)
            ir.async_delete_issue(self._hass, DOMAIN, issue_id(service))
            return
        sources = ", ".join(sorted(set(down.values())))
        if len(sources) > SOURCES_LENGTH:
            sources = sources[: SOURCES_LENGTH - 1] + "…"
        # The same ID: the repair is updated, not added again.
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            issue_id(service),
            is_fixable=False,
            is_persistent=False,
            severity=IssueSeverity.WARNING,
            translation_key="service_unavailable",
            translation_placeholders={
                "service": SERVICES[service],
                "sources": sources,
                "error": self._errors[service],
            },
        )


class Outages:
    """The sources that are down, such as "Station SMA", each logged once.

    A source that fails is logged as a warning the first time, then only at
    debug level until it works again, which is logged once more.  The failure
    comes after the request was tried again (see fetch.retrying), so with
    hass, its service has a repair while it or another of its sources is down
    (see ServiceRepairs).  place, such as an entry's name, tells the user whose
    source it is; owner, such as its entry ID, keeps the sources of different
    entries apart.  service is that of the sources, unless a failure says.
    """

    def __init__(
        self,
        logger: logging.Logger,
        hass: HomeAssistant | None = None,
        place: str | None = None,
        owner: str = "",
        service: str = METEOSWISS,
    ) -> None:
        self._logger = logger
        self._hass = hass
        self._place = place
        self._owner = owner
        self._service = service
        # Each source that is down, with its service.
        self._down: dict[str, str] = {}

    def failed(
        self,
        source: str,
        error: object,
        trace: bool = False,
        repair: bool = True,
        log: bool = True,
        service: str | None = None,
    ) -> None:
        """Note a failure; trace logs the exception being handled with it.

        repair=False for a failure with its own repair, such as a station
        without data; log=False for one already logged elsewhere.
        """
        if source in self._down:
            self._logger.debug("%s is still unavailable: %s", source, error)
            return
        service = service or self._service
        self._down[source] = service
        if log:
            self._logger.warning("%s is unavailable: %s", source, error, exc_info=trace)
        if repair and (repairs := self._repairs()) is not None:
            repairs.failed(service, (self._owner, source), self._label(source), error)

    def worked(self, source: str, service: str | None = None) -> None:
        down = self._down.pop(source, None)
        if down is not None:
            self._logger.info("%s is available again", source)
        # Also a source left down by the entry's setup that failed and was
        # retried, with an Outages of its own.
        if (repairs := self._repairs()) is not None:
            repairs.worked(down or service or self._service, (self._owner, source))

    def clear(self) -> None:
        """Forget every outage, and its repair, as when the entry is unloaded."""
        repairs = self._repairs()
        for source, service in self._down.items():
            if repairs is not None:
                repairs.worked(service, (self._owner, source))
        self._down.clear()

    def _label(self, source: str) -> str:
        return f"{source} ({self._place})" if self._place else source

    def _repairs(self) -> ServiceRepairs | None:
        if self._hass is None:
            return None
        repairs: ServiceRepairs | None = self._hass.data.get(DATA_REPAIRS)
        if repairs is None:
            repairs = self._hass.data[DATA_REPAIRS] = ServiceRepairs(self._hass)
        return repairs
