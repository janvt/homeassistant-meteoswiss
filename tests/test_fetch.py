"""Trying a request again when it fails for a moment."""

import asyncio
from unittest.mock import MagicMock

import aiohttp
import pytest

from custom_components.meteoswiss import fetch


def answered(status: int) -> aiohttp.ClientResponseError:
    return aiohttp.ClientResponseError(MagicMock(), (), status=status)


class Requests:
    """Fails with each of the failures in turn, then answers."""

    def __init__(self, *failures: BaseException) -> None:
        self.failures = list(failures)
        self.made = 0

    async def __call__(self) -> str:
        self.made += 1
        if self.failures:
            raise self.failures.pop(0)
        return "answer"


@pytest.mark.parametrize(
    "failure",
    [
        answered(500),
        answered(503),
        answered(429),
        aiohttp.ClientConnectionError("reset"),
        asyncio.TimeoutError(),
    ],
    ids=["500", "503", "429", "connection", "timeout"],
)
async def test_a_passing_failure_is_tried_again(failure):
    request = Requests(failure, failure)

    assert await fetch.retrying(request, "file") == "answer"
    assert request.made == 3


@pytest.mark.parametrize(
    "failure",
    [answered(404), answered(403), answered(400), ValueError("not a number")],
    ids=["404", "403", "400", "other"],
)
async def test_an_answer_the_server_means_is_not_tried_again(failure):
    request = Requests(failure)

    with pytest.raises(type(failure)):
        await fetch.retrying(request, "file")
    assert request.made == 1


async def test_the_last_failure_is_raised_when_all_tries_fail():
    request = Requests(answered(500), answered(502), answered(503))

    with pytest.raises(aiohttp.ClientResponseError) as raised:
        await fetch.retrying(request, "file")
    assert raised.value.status == 503
    assert request.made == 1 + len(fetch.RETRY_DELAYS)


async def test_more_failures_can_be_tried_again():
    request = Requests(KeyError("unparsable"))

    assert await fetch.retrying(request, "file", also=(KeyError,)) == "answer"
    assert request.made == 2
