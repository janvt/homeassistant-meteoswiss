"""Importing a station's hourly history into long-term statistics."""

import datetime

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.meteoswiss import history
from custom_components.meteoswiss.const import DOMAIN
from custom_components.meteoswiss.ogd import OGDError

from .fakes import NOW, FakeOGD
from .harness import (
    MockConfigEntry,
    async_wait_recording_done,
    do_adhoc_statistics,
)

pytestmark = pytest.mark.usefixtures("fake_ogd", "frozen_now")

UTC = datetime.timezone.utc
HOUR = datetime.timedelta(hours=1)


@pytest.fixture(autouse=True)
def _enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """The recorder has to be set up before Home Assistant (conftest's order)."""


def entity(hass, entry, key):
    return er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"sensor.{entry.entry_id}-{key}"
    )


async def recorded_entry(hass, entry_data, frozen_now):
    """An entry whose sensors Home Assistant recorded from 12:00 on the 20th."""
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    # The last five minutes of the hour, which compiles the hour too.
    frozen_now.move_to(NOW.replace(minute=59))
    do_adhoc_statistics(hass, start=NOW.replace(minute=55))
    await async_wait_recording_done(hass)
    return entry


async def statistics(recorder_mock, hass, statistic_id):
    from homeassistant.components.recorder.statistics import (
        statistics_during_period,
    )

    rows = await recorder_mock.async_add_executor_job(
        statistics_during_period,
        hass,
        datetime.datetime(2026, 10, 1, tzinfo=UTC),
        None,
        {statistic_id},
        "hour",
        None,
        {"mean", "min", "max"},
    )
    return {
        datetime.datetime.fromtimestamp(r["start"], UTC): r
        for r in rows.get(statistic_id, [])
    }


async def import_history(hass, entry, **data):
    return await hass.services.async_call(
        DOMAIN,
        "import_history",
        {"config_entry_id": entry.entry_id, **data},
        blocking=True,
        return_response=True,
    )


async def test_the_hours_before_home_assistant_recorded_them(
    recorder_mock, hass, entry_data, frozen_now
):
    entry = await recorded_entry(hass, entry_data, frozen_now)
    temperature = entity(hass, entry, "temperature")
    rain = entity(hass, entry, "10minrain")
    own = (await statistics(recorder_mock, hass, temperature))[NOW.replace(minute=0)]

    response = await import_history(hass, entry, since="2026-10-19")
    await async_wait_recording_done(hass)

    # Midnight on the 19th to 12:00 on the 20th, when its own begin.
    assert response["since"] == "2026-10-19"
    assert response["imported_hours"][temperature] == 36
    rows = await statistics(recorder_mock, hass, temperature)
    assert len(rows) == 37
    first = rows[datetime.datetime(2026, 10, 19, tzinfo=UTC)]
    assert (first["mean"], first["min"], first["max"]) == (5.0, 4.0, 6.5)
    # Home Assistant's own hour is left as it was.
    assert rows[NOW.replace(minute=0)] == own
    # The 10-minute rain: an hour's six values average its total over 6.
    rain_rows = await statistics(recorder_mock, hass, rain)
    assert rain_rows[datetime.datetime(2026, 10, 19, tzinfo=UTC)]["mean"] == (
        pytest.approx(0.1)
    )
    # Without hourly values in the file, a sensor gets nothing.
    assert response["imported_hours"][entity(hass, entry, "pressure_qff")] == 0

    # Importing again adds nothing.
    response = await import_history(hass, entry, since="2026-10-19")
    assert set(response["imported_hours"].values()) == {0}


async def test_by_default_from_the_start_of_the_decade(
    recorder_mock, hass, entry_data, frozen_now
):
    entry = await recorded_entry(hass, entry_data, frozen_now)

    response = await import_history(hass, entry)

    assert response["since"] == "2020-01-01"
    assert FakeOGD.history_calls[0][2] == datetime.datetime(2020, 1, 1, tzinfo=UTC)


async def test_not_while_downloads_are_paused(
    recorder_mock, hass, entry_data, frozen_now
):
    entry = await recorded_entry(hass, entry_data, frozen_now)
    await entry.runtime_data.async_set_paused(True)

    with pytest.raises(ServiceValidationError) as raised:
        await import_history(hass, entry)
    assert raised.value.translation_key == "downloads_paused"
    assert FakeOGD.history_calls == []


def test_file_start():
    now = datetime.datetime(2026, 10, 20, tzinfo=UTC)
    assert history._file_start(
        "https://x/ber/ogd-smn_ber_h_historical_2010-2019.csv", now
    ) == datetime.datetime(2010, 1, 1, tzinfo=UTC)
    assert history._file_start(
        "https://x/ber/ogd-smn_ber_h_recent.csv", now
    ) == datetime.datetime(2026, 1, 1, tzinfo=UTC)


async def test_nothing_before_home_assistant_recorded_anything(
    recorder_mock, hass, entry_data
):
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    response = await import_history(hass, entry)

    assert response["imported_hours"] == {}
    assert FakeOGD.history_calls == []


async def test_a_statistic_kept_in_another_kind_of_unit_is_left_alone(
    recorder_mock, hass, entry_data, frozen_now, monkeypatch, caplog
):
    entry = await recorded_entry(hass, entry_data, frozen_now)
    temperature = entity(hass, entry, "temperature")
    # As if the file had it in bar, which °C cannot be converted from.
    monkeypatch.setitem(
        history.SENSOR_COLUMNS,
        "temperature",
        history.SENSOR_COLUMNS["temperature"]._replace(unit="bar"),
    )

    response = await import_history(hass, entry, since="2026-10-19")

    assert temperature not in response["imported_hours"]
    assert f"Not importing {temperature}, kept in °C rather than bar" in caplog.text
    assert response["imported_hours"][entity(hass, entry, "humidity")] == 36


async def test_not_for_an_entry_that_is_not_loaded(recorder_mock, hass, entry_data):
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)

    with pytest.raises(ServiceValidationError) as raised:
        await import_history(hass, entry)
    assert raised.value.translation_key == "entry_not_loaded"


async def test_not_without_a_weather_station(recorder_mock, hass, entry_data):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            k: v
            for k, v in entry_data.items()
            if k not in ("station", "real_time_name")
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError) as raised:
        await import_history(hass, entry)
    assert raised.value.translation_key == "no_weather_station"


async def test_not_without_the_recorder(
    recorder_mock, hass, entry_data, frozen_now, monkeypatch
):
    entry = await recorded_entry(hass, entry_data, frozen_now)
    monkeypatch.setattr(
        hass.config, "components", set(hass.config.components) - {"recorder"}
    )

    with pytest.raises(ServiceValidationError) as raised:
        await import_history(hass, entry)
    assert raised.value.translation_key == "no_recorder"


async def test_a_history_file_that_cannot_be_downloaded(
    recorder_mock, hass, entry_data, frozen_now, monkeypatch
):
    entry = await recorded_entry(hass, entry_data, frozen_now)

    async def unavailable(self, url, columns, since):
        raise OGDError("down")

    monkeypatch.setattr(FakeOGD, "get_station_hours", unavailable)

    with pytest.raises(HomeAssistantError) as raised:
        await import_history(hass, entry)
    assert raised.value.translation_key == "history_unavailable"
    assert raised.value.translation_placeholders == {"error": "down"}


async def test_values_are_converted_to_the_unit_a_statistic_is_kept_in(
    recorder_mock, hass, entry_data, frozen_now, monkeypatch
):
    entry = await recorded_entry(hass, entry_data, frozen_now)
    temperature = entity(hass, entry, "temperature")
    # As if the file had it in °F, while the statistic is kept in °C.
    monkeypatch.setitem(
        history.SENSOR_COLUMNS,
        "temperature",
        history.SENSOR_COLUMNS["temperature"]._replace(unit="°F"),
    )

    await import_history(hass, entry, since="2026-10-19")
    await async_wait_recording_done(hass)

    first = (await statistics(recorder_mock, hass, temperature))[
        datetime.datetime(2026, 10, 19, tzinfo=UTC)
    ]
    # 5 °F, 4 °F and 6.5 °F.
    assert first["mean"] == pytest.approx(-15.0)
    assert first["min"] == pytest.approx(-15.556, abs=0.001)
    assert first["max"] == pytest.approx(-14.167, abs=0.001)
