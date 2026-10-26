"""Weather warnings from the MeteoAlarm feed, against an in-memory server."""

import datetime

import pytest

from custom_components.meteoswiss import alerts
from custom_components.meteoswiss.alerts import AlertError, MeteoAlarm

from .fakes import FakeSession

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 20, 12, 30, tzinfo=UTC)
HOUR = datetime.timedelta(hours=1)

# A square around Altdorf (lat 46.85-46.95, lon 8.55-8.75) and one far away.
URI = "46.85,8.55 46.95,8.55 46.95,8.75 46.85,8.75 46.85,8.55"
TICINO = "46.0,8.8 46.1,8.8 46.1,9.0 46.0,9.0 46.0,8.8"
ALTDORF = (46.8806, 8.6444, 450.0)


def entry(uuid, polygon, onset, expires, event="Gale force wind", message="Alert"):
    return f"""
  <entry>
    <cap:polygon>{polygon}</cap:polygon>
    <cap:areaDesc>Region {uuid}</cap:areaDesc>
    <cap:event>{event}</cap:event>
    <cap:onset>{onset.isoformat()}</cap:onset>
    <cap:expires>{expires.isoformat()}</cap:expires>
    <cap:severity>Severe</cap:severity>
    <cap:message_type>{message}</cap:message_type>
    <cap:identifier>id-{uuid}</cap:identifier>
    <link title="Region {uuid}" href="https://meteoalarm.org?polygon={uuid},0,0,0"/>
    <link type="application/cap+xml" href="https://cap.test/{uuid}"/>
    <link title="Switzerland" rel="related" href="https://meteoalarm.org?region=CH"/>
    <title>Warning {uuid}</title>
  </entry>"""


def feed(*entries) -> bytes:
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:cap="urn:oasis:names:tc:emergency:cap:1.2">'
        + "".join(entries)
        + "</feed>"
    ).encode()


def details(level="2; yellow; Moderate", kind="1; wind", altitude=0, ceiling=3000):
    def info(language, event, headline):
        return f"""
  <info>
    <language>{language}</language>
    <event>{event}</event>
    <headline>{headline}</headline>
    <description>About {event}</description>
    <parameter><valueName>awareness_level</valueName><value>{level}</value></parameter>
    <parameter><valueName>awareness_type</valueName><value>{kind}</value></parameter>
    <area>
      <areaDesc>Somewhere</areaDesc>
      <altitude>{altitude}</altitude>
      <ceiling>{ceiling}</ceiling>
    </area>
  </info>"""

    return (
        '<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">'
        + info("en-GB", "Gale force wind", "Gale force wind")
        + info("de-CH", "Stürmischer Wind", "Stürmischer Wind")
        + "</alert>"
    ).encode()


def client(files):
    session = FakeSession(files)
    return MeteoAlarm(session, "tests"), session  # type: ignore[arg-type]


WIND = entry("wind", URI, NOW - 5 * HOUR, NOW + 10 * HOUR)


def test_inside():
    square = alerts._polygon(URI)
    assert alerts.inside(46.8806, 8.6444, square)
    assert not alerts.inside(46.8806, 8.80, square)
    assert not alerts.inside(47.0, 8.6444, square)


def test_parse_feed_leaves_out_cancellations():
    entries = alerts.parse_feed(
        feed(WIND, entry("old", URI, NOW, NOW + HOUR, message="Cancel"))
    )
    assert [e.identifier for e in entries] == ["id-wind"]
    assert entries[0].details_url == "https://cap.test/wind"
    assert entries[0].polygons[0][0] == (46.85, 8.55)


@pytest.mark.parametrize(
    ("language", "site"), [("de", "de"), ("fr-CH", "fr"), ("en", "en"), ("nb", "en")]
)
def test_warning_page(language, site):
    assert alerts.warning_page("Flüelen-Unterschächen", language) == (
        f"https://meteoalarm.org/{site}/live/region/CH"
        "?sa=Fl%C3%BCelen-Untersch%C3%A4chen"
    )


def test_parse_details():
    parsed = alerts.parse_details(details(level="3; orange; Severe", altitude=1800))
    assert (parsed.level, parsed.type, parsed.altitude, parsed.ceiling) == (
        "orange",
        "wind",
        1800.0,
        3000.0,
    )
    assert parsed.texts["de"][0] == "Stürmischer Wind"


def warning(level, onset, expires):
    return {"level": level, "onset": onset, "expires": expires}


def test_level_between_takes_the_most_severe_in_force():
    warnings = [
        warning("yellow", NOW - HOUR, NOW + HOUR),
        warning("red", NOW + 5 * HOUR, NOW + 6 * HOUR),
        warning("orange", NOW - 9 * HOUR, NOW - 8 * HOUR),  # over
    ]
    now = (NOW, NOW + datetime.timedelta(seconds=1))
    assert alerts.level_between(warnings, *now) == "yellow"
    assert alerts.level_between(warnings, NOW, NOW + 24 * HOUR) == "red"
    assert alerts.level_between([], *now) == "none"


class TestGetWarnings:
    async def test_what_is_downloaded_is_counted(self):
        files = {
            alerts.FEED_URL: feed(WIND),
            "https://cap.test/wind": details(),
        }
        meteoalarm, session = client(files)

        await meteoalarm.get_warnings(*ALTDORF, "de", NOW)

        assert meteoalarm.downloaded == session.transferred() > 0

    async def test_the_warnings_of_the_region_in_the_language(self):
        files = {
            alerts.FEED_URL: feed(
                WIND,
                entry("ticino", TICINO, NOW, NOW + HOUR),
                entry("over", URI, NOW - 9 * HOUR, NOW - HOUR),
            ),
            "https://cap.test/wind": details(),
            "https://cap.test/ticino": details(),
            "https://cap.test/over": details(),
        }
        meteoalarm, _ = client(files)

        warnings = await meteoalarm.get_warnings(*ALTDORF, "de", NOW)

        assert [(w["identifier"], w["type"], w["level"]) for w in warnings] == [
            ("id-wind", "wind", "yellow")
        ]
        assert warnings[0]["event"] == "Stürmischer Wind"
        assert warnings[0]["region"] == "Region wind"
        assert warnings[0]["url"] == (
            "https://meteoalarm.org/de/live/region/CH?sa=Region%20wind"
        )
        assert warnings[0]["onset"] == NOW - 5 * HOUR

    async def test_english_when_the_language_is_not_offered(self):
        files = {alerts.FEED_URL: feed(WIND), "https://cap.test/wind": details()}
        meteoalarm, _ = client(files)
        warnings = await meteoalarm.get_warnings(*ALTDORF, "nb", NOW)
        assert warnings[0]["headline"] == "Gale force wind"

    @pytest.mark.parametrize(
        ("height", "warned"),
        [(450.0, False), (1800.0, True), (2500.0, True), (3073.0, False), (None, True)],
    )
    async def test_only_between_the_altitudes_of_the_warning(self, height, warned):
        files = {
            alerts.FEED_URL: feed(WIND),
            "https://cap.test/wind": details(altitude=1800, ceiling=3000),
        }
        meteoalarm, _ = client(files)
        warnings = await meteoalarm.get_warnings(46.88, 8.64, height, "en", NOW)
        assert bool(warnings) is warned

    async def test_the_feed_is_shared_for_half_an_hour(self):
        files = {alerts.FEED_URL: feed(WIND), "https://cap.test/wind": details()}
        meteoalarm, session = client(files)

        for minutes in (0, 10, 29):
            later = NOW + datetime.timedelta(minutes=minutes)
            await meteoalarm.get_warnings(*ALTDORF, "en", later)
        assert [r[1] for r in session.requests] == [
            alerts.FEED_URL,
            "https://cap.test/wind",
        ]

        await meteoalarm.get_warnings(
            *ALTDORF, "en", NOW + 31 * datetime.timedelta(minutes=1)
        )
        # The feed again, but a warning's details only once.
        assert [r[1] for r in session.requests][2:] == [alerts.FEED_URL]

    async def test_without_details_the_level_comes_from_the_feed(self):
        meteoalarm, _ = client({alerts.FEED_URL: feed(WIND)})

        warnings = await meteoalarm.get_warnings(*ALTDORF, "en", NOW)

        assert [(w["level"], w["event"], w["type"]) for w in warnings] == [
            ("orange", "Gale force wind", "")  # Severe
        ]

    async def test_a_feed_that_cannot_be_fetched(self):
        meteoalarm, session = client({})
        with pytest.raises(AlertError):
            await meteoalarm.get_warnings(*ALTDORF, "en", NOW)

    async def test_the_previous_warnings_are_kept(self):
        files = {alerts.FEED_URL: feed(WIND), "https://cap.test/wind": details()}
        meteoalarm, _ = client(files)
        await meteoalarm.get_warnings(*ALTDORF, "en", NOW)
        del files[alerts.FEED_URL]

        warnings = await meteoalarm.get_warnings(*ALTDORF, "en", NOW + 2 * HOUR)

        assert [w["identifier"] for w in warnings] == ["id-wind"]
