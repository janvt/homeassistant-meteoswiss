"""Trying a request again when it fails for a moment."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

import aiohttp

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")

# Seconds to wait before each try after the first: a server that answers 500
# or drops the connection is often back seconds later (MeteoAlarm on
# 23 November 2026).  A failure that lasts longer is left to the next update.
RETRY_DELAYS: tuple[float, ...] = (2, 10)


def transient(exc: BaseException) -> bool:
    """Whether a failure may well be gone if the request is made again.

    A server error, too many requests, a connection that failed or a timeout.
    Not a missing file or any other answer the server means.
    """
    if isinstance(exc, aiohttp.ClientResponseError):
        return exc.status == 429 or exc.status >= 500
    return isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError))


async def retrying(
    request: Callable[[], Awaitable[T]],
    what: str,
    also: tuple[type[BaseException], ...] = (),
) -> T:
    """The result of a request, made again after a transient failure.

    also: more failures to try again after, such as an answer that cannot be
    parsed.  The last failure is raised once all tries failed.
    """
    for delay in RETRY_DELAYS:
        try:
            return await request()
        except Exception as exc:
            if not (transient(exc) or isinstance(exc, also)):
                raise
            _LOGGER.debug(
                "%s failed (%s); trying again in %s s",
                what,
                str(exc) or repr(exc),
                delay,
            )
        await asyncio.sleep(delay)
    return await request()
