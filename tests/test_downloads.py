"""The large files, read once for the points of all entries."""

import asyncio

from custom_components.meteoswiss.downloads import SharedDownloads
from custom_components.meteoswiss.ogd import OGDError, Point

ZURICH = Point(800500, 2, "Zürich", "8005", 47.39, 8.52)
BRISTEN = Point(864, 3, "Bristen", "", 46.75, 8.71, 3073.0)


class Client:
    def __init__(self) -> None:
        self.reads: list[tuple[set, str]] = []
        self.gate: asyncio.Event | None = None
        self.error: Exception | None = None

    async def get_hourly_rows(self, points, run, parameter):
        self.reads.append(({p.key for p in points}, run))
        if self.gate:
            await self.gate.wait()
        if self.error:
            raise self.error
        return {p.key: {"run": run} for p in points}


async def test_an_entry_registered_during_a_read_reads_only_its_point():
    client = Client()
    downloads = SharedDownloads(client)  # type: ignore[arg-type]
    downloads.register("zurich", ZURICH, {"tre200h0"})
    client.gate = asyncio.Event()

    first = asyncio.create_task(downloads.get(ZURICH, "tre200h0", "R1"))
    await asyncio.sleep(0)
    downloads.register("bristen", BRISTEN, {"tre200h0"})
    second = asyncio.create_task(downloads.get(BRISTEN, "tre200h0", "R1"))
    await asyncio.sleep(0)
    client.gate.set()

    assert await first == {"run": "R1"}
    assert await second == {"run": "R1"}
    assert client.reads == [({ZURICH.key}, "R1"), ({BRISTEN.key}, "R1")]


async def test_all_points_together_for_a_new_run():
    client = Client()
    downloads = SharedDownloads(client)  # type: ignore[arg-type]
    downloads.register("zurich", ZURICH, {"tre200h0"})
    downloads.register("bristen", BRISTEN, {"tre200h0"})
    await downloads.get(ZURICH, "tre200h0", "R1")
    assert await downloads.get(BRISTEN, "tre200h0", "R1") == {"run": "R1"}

    await downloads.get(BRISTEN, "tre200h0", "R2")

    assert client.reads == [
        ({ZURICH.key, BRISTEN.key}, "R1"),
        ({ZURICH.key, BRISTEN.key}, "R2"),
    ]


async def test_a_failed_read_keeps_the_previous_rows():
    client = Client()
    downloads = SharedDownloads(client)  # type: ignore[arg-type]
    downloads.register("zurich", ZURICH, {"tre200h0"})
    await downloads.get(ZURICH, "tre200h0", "R1")
    client.error = OGDError("offline")

    rows = await downloads.get(ZURICH, "tre200h0", "R2")

    assert rows == {"run": "R1"}
