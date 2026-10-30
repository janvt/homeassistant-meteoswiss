# MeteoSwiss for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Tests](https://github.com/janvt/homeassistant-meteoswiss/actions/workflows/tests.yaml/badge.svg)](https://github.com/janvt/homeassistant-meteoswiss/actions/workflows/tests.yaml)

Forecasts, measurements, warnings, pollen, Föhn and climate normals for
Switzerland in Home Assistant, from MeteoSwiss's official
[open data](https://opendatadocs.meteoswiss.ch/).  No API key and no account
are needed.

This started as a fork of [Rudd-O's integration](https://github.com/Rudd-O/homeassistant-meteoswiss)
(itself a fork of [websylv's original](https://github.com/websylv/homeassistant-meteoswiss)),
and is now maintained on its own: see [Credits](#credits).

![The forecast of a postal code, with its sensors](docs/forecast-device.png)

## Features

* **Weather entity** with the daily (nine days) and hourly forecast of a postal
  code, or of one of MeteoSwiss's mountains, huts and passes, at its altitude.
  The forecast includes temperatures, precipitation and its probability,
  wind and gusts, cloud cover, sunshine and dew point.
* **Current conditions** from a SwissMetNet weather station every 10 minutes,
  and optionally rain from a separate precipitation station closer to you.
* **Forecast summaries** such as snow tonight, sunshine tomorrow or rain in the
  next hour, as sensors for dashboards and automations.
* **Weather warnings** of MeteoSwiss for the place, at its height, with a link to
  the warning on a map.
* **Pollen** measurements from the closest pollen station.
* **Föhn**: an outlook from the pressure difference across the Alps, and
  MeteoSwiss's Föhn index measured at the station.
* **Climate normals**: today and this month against the station's 1991–2020
  normals, and the record for the date.
* **Freezing level** forecast.
* **Radar** (optional): rain and hail right now at the place, every 5 minutes.
* Guided setup that suggests the postal code and stations closest to your Home
  Assistant location.  Stations, place and options can be changed later without
  losing entities or history.
* Repair notices when a station or a forecast stops publishing.
* Translations in English, German, French, Italian, Spanish and Norwegian.

## Installation

### HACS

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=janvt&repository=homeassistant-meteoswiss&category=integration)

The button opens this repository in your HACS, adding it as a custom
repository if needed, so you can skip step 1:

1. In HACS, open the menu → *Custom repositories*, add
   `https://github.com/janvt/homeassistant-meteoswiss` with the type
   *Integration*.
2. Search for *MeteoSwiss* in HACS and download it.  Beta versions are listed
   when *Show beta versions* is turned on for the repository.
3. Restart Home Assistant.

### Manually

Copy `custom_components/meteoswiss` from the
[latest release](https://github.com/janvt/homeassistant-meteoswiss/releases) to
the `custom_components` folder of your Home Assistant configuration, and restart
Home Assistant.

Either way, it needs Home Assistant 2024.11 or newer.

### Coming from Rudd-O's or websylv's version

This integration has the same domain (`meteoswiss`) as Rudd-O's, so it replaces
it in place: remove Rudd-O's repository from HACS's custom repositories, add this
one and download it.  Existing entries keep working, with their entities and
history.  A few stations of the old data source no longer publish open data
(such as MRP and VSOXE); their sensors become unavailable and a repair notice
appears after an hour.  Choose *Reconfigure* to pick another station.

websylv's integration (*Meteo Swiss*, folder `meteo-swiss`) is a different
integration: remove it and its YAML configuration first, then set this one up.
YAML configuration of this integration is imported once and raises a repair
notice; remove it from `configuration.yaml` afterwards.

## Setup

Make sure your Home Assistant location is set (*Settings → System → General*),
as setup uses it to suggest a postal code and stations.  Then go to
*Settings → Devices & services → Add integration* and search for *MeteoSwiss*.

1. **Location.**  Confirm or change the coordinates used to find what is
   closest to you.

   <img src="docs/setup-location.png" alt="Location step" width="460">

2. **Forecast for** a postal code, or a mountain, hut or pass.

   <img src="docs/setup-forecast-for.png" alt="Choice between a postal code and a mountain" width="460">

3. **Forecast.**  For a postal code: the postal code (the closest is suggested),
   the name of the weather entity, the update interval of the station sensors,
   and whether to download hourly temperatures (see
   [Download volumes](#download-volumes)).

   <img src="docs/setup-postal-code.png" alt="Postal code step" width="460">

   For a mountain, hut or pass, pick it from the 100 closest, sorted by distance,
   with each one's altitude.  Its forecast is for that altitude.

   <img src="docs/setup-mountain.png" alt="Mountain step" width="460">

4. **Stations.**  The weather station gives the current conditions, the
   precipitation station the 10-minute rain, and the pollen station the pollen
   sensors.  The closest are suggested; the first option of each list is
   *none*.  Summits rarely have a station nearby, so none is suggested for
   mountains: their weather entity then shows the forecast for the current hour
   as its current conditions.

   <img src="docs/setup-stations.png" alt="Stations step" width="460">

The forecast and each station become a device of the entry:

![The entry with its forecast and station devices](docs/integration-entry.png)

Home Assistant names entities after their device, so a station sensor shows as
e.g. *Altdorf Temperature*.  Add the integration once more for each further place.

### Changing the setup

* **Reconfigure** (in the entry's menu) changes the postal code or place and the
  stations; entities and their history are kept.
* **Configure** (the cog) turns hourly temperatures, the freezing level, climate
  normals and Föhn on or off, and sets the Föhn stations and thresholds.

  <img src="docs/options.png" alt="Options" width="460">

## Entities

### Weather station

![A weather station with its sensors](docs/station-device.png)

Temperature, humidity, dew point, pressure (station, QFF and QNH), wind speed,
gusts and direction, 10-minute rain and sunshine and sun irradiation, from the
station's latest 10-minute measurement.  Values older than 90 minutes are not
shown; a value missing from the latest measurement is taken from the half
hour before.  A
precipitation station has only the 10-minute rain.

The station pressure, QNH and dew point (which the weather entity also has)
are created turned off: turn them on from the device's page, under *entities
not shown*.

Some stations measure more, and their sensors are created for the stations
that report them when the entry is set up:

* *Snow depth*, in cm, measured automatically.
* *Temperature 5 cm above ground*, which shows ground frost before the
  temperature at 2 m does.
* *Soil temperature 5 cm*, *10 cm* and *20 cm* (turned off).
* *Wind chill*, the temperature felt in the wind (turned off).
* *Diffuse radiation* and *Longwave radiation*, in W/m² (turned off).

*Measurement time* (diagnostic, turned off) is when the station's latest
measurement was made.

### Forecast summaries

Every forecast has sensors summarising it.  All times are local
(Europe/Zurich), and a window holds the hours that start in it.

| Sensor | Value |
|---|---|
| Snow tonight | mm of water from 18:00 to 08:00 (from the current hour on), counting the precipitation of hours at 0.5 °C or colder |
| Snow next 24 hours, Snow next 48 hours | The same, from the current hour on |
| Sunshine tomorrow 9–15, Sunshine tomorrow | Hours of sunshine |
| Strongest gust tomorrow 9–15 | km/h |
| Precipitation next hour, Chance of rain next hour | For the hour after the current one; the chance of rain is MeteoSwiss's for the three hours up to its end |
| Lowest temperature tonight | °C, 18:00 to 08:00 from the current hour on |
| Highest temperature today, Lowest temperature today, Precipitation today | MeteoSwiss's forecast for the day |
| Sunshine today | Hours of sunshine over the whole day, the hours already past included |
| Freezing level | Forecast height of the 0 °C line for the current hour, in m (when turned on) |
| Lowest freezing level next 48 hours | The lowest of those heights from the current hour on, in m, with the hour it is forecast for as `at` (when turned on) |

The snow and temperature sensors need *Hourly temperatures*.  A sensor is
unavailable when any hour of its window, or a value it needs, is missing,
rather than showing a partial result.  Being forecasts, these sensors keep no
long-term statistics.

To use the forecast itself in a template, call `weather.get_forecasts`:

```yaml
template:
  - trigger:
      - trigger: time_pattern
        minutes: /15
    actions:
      - action: weather.get_forecasts
        data:
          type: daily
        target:
          entity_id: weather.altdorf_ur
        response_variable: daily
    sensor:
      - name: Tomorrow max temperature
        unique_id: tomorrow_max_temperature
        unit_of_measurement: °C
        state: "{{ daily['weather.altdorf_ur'].forecast[1].temperature }}"
```

### Weather warnings

*Weather warning* (the most severe warning in force now) and *Weather warning
next 24 hours* are `none`, `yellow`, `orange` or `red`.  Their `warnings`
attribute lists each warning with its type (such as `wind`, `rain` or
`thunderstorm`), level, start, end, region, headline and text in Home
Assistant's language, and a `url` of MeteoAlarm's page of its region with the
warning on a map; `types` lists just the types, for templates such as a Föhn
warning (`wind` in Altdorf).

Each warning covers a region between two altitudes, so a place gets the
warnings of its region at its height: a ridge can be warned of wind while the
village below is not.  MeteoSwiss's warnings currently stop at 3000 m, so
summits above that get none.  The texts of warnings are not stored in the
history, only their types.

### Föhn

* *Föhn pressure difference* (forecast device): the sea-level pressure south
  of the Alps minus north of them, in hPa.  It rises hours before the Föhn
  breaks through: on 20 October 2026 it passed 4 hPa at 06:30 UTC, and the Föhn
  reached Altdorf at 12:30, at about 6 hPa.
* *Föhn outlook* (forecast device): from that difference, `strong_south`,
  `south` (likely), `none`, `north` or `strong_north` (north Föhn in Ticino).
* *Föhn* (weather station): MeteoSwiss's Föhn index measured at the station,
  `none`, `mixed` or `foehn`, for stations that have one.

The defaults are the classic pair Lugano and Zürich/Kloten, with Föhn likely
from 4 hPa and strong from 8 hPa; all can be changed under *Configure*.

### Climate normals

With a weather station, these sensors compare it with its normals for
1991–2020, computed from the station's own history (MeteoSwiss publishes its
official normals only as maps):

* *High vs normal* and *Low vs normal*: today's highest and lowest temperature
  so far against the normal for the date (the mean over 1991–2020 of a week
  either side of it), in °C above or below.
* *Month vs normal*: this month's mean temperature so far, in °C above or below.
* *Month's rain vs normal* and *Month's sunshine vs normal*: this month so far,
  in % of the normal for as many days.
* *Record high for the date*, with its year, and the record low as attributes.

Days are UTC days, as in MeteoSwiss's data, so early in the morning today's
high is still low.

### Pollen

One sensor per type of pollen (alder, ash, beech, birch, grasses, hazel and
oak) with the latest hourly concentration in pollen/m³ at the chosen station,
one of MeteoSwiss's 15.  They are measurements, not forecasts, and are
unavailable when the station has reported nothing for six hours.
Birch and grasses are turned on; the others are created turned off, to be
turned on from the device's page by those who need them.

### Radar

Turn on *Radar* under *Configure* for four sensors and an image from
MeteoSwiss's weather radar, every 5 minutes, at the 1 km square of your Home Assistant home if the
place is within 10 km of it, and otherwise of the place (for a mountain, its
summit):

* *Radar rain rate*, in mm/h.
* *Radar hail probability*, in %.
* *Radar hail size*: the largest hail expected, in mm; 0 below 20 mm, the
  smallest size the radar reports.
* *Radar rain last hour*: the rain of the hour to the image's time, in mm,
  from CombiPrecip, the radar adjusted to the rain gauges.  It comes about 4
  minutes after its hour ends, a new one every 5 minutes.
* *Radar loop*: an animated image of the rain rate at least 30 km around the
  place, the last 20 minutes, on a swisstopo map; hatched where the radars do
  not reach.  It is cut from the rain images the sensors download anyway, and
  drawn only when it is looked at.  The image is 15 to 300 kB, more with more
  rain, and each dashboard showing it loads it again every 5 minutes: worth
  knowing for a phone on mobile data.  The map (about 250 kB) is downloaded
  from swisstopo once per start, for each place, and counts in the download
  sensors.

The radar knows within a minute or two that rain has started at your place,
where the nearest precipitation station is typically 5 km away and reports
10-minute totals 10 minutes later.  That makes it the sensor for automations
like closing windows or retracting an awning:

```yaml
trigger:
  - platform: numeric_state
    entity_id: sensor.zurich_radar_rain_rate
    above: 0.5
```

Its amounts are estimates, often off by a factor of two over a few minutes,
and less reliable in the mountains: for how much it rained, use the
precipitation station, or the rain of the last hour, which is checked against
the gauges.  Hail is only looked at while it rains at or next to the place,
and the rain of the last hour until it has been dry there for an hour.  Each
image of the rain of the last hour is 30 to 60 kB on a dry day in Switzerland
and up to about 625 kB on a wet one: while it rains at the place that is up to
7.5 MB an hour.

## Station history

The action *MeteoSwiss: Import station history* (`meteoswiss.import_history`)
fills the long-term statistics of the weather station's sensors with the
station's hourly measurements from MeteoSwiss, so that their history graphs
reach back before you installed the integration:

```yaml
action: meteoswiss.import_history
data:
  config_entry_id: 0123456789abcdef0123456789abcdef
  since: "2020-01-01"
```

* It imports only the hours before Home Assistant started recording each
  sensor: what Home Assistant recorded itself is left as it is, and importing
  again adds nothing.  A sensor needs to have been recorded for an hour first.
* `since` is the first day; by default the start of this decade.  Each year
  adds about 9 MB to Home Assistant's database for all the station's sensors,
  and long-term statistics are kept for good, so go further back only if you
  want it.
* It downloads the station's history files: compressed, about 0.4 MB a year.
* Temperatures get the hour's mean, lowest and highest; the other sensors the
  mean only, so their lowest and highest show from your install on.  Gusts,
  the wind direction and wind chill are not imported.
* It is the history of the weather station the entry has now.  If you chose
  another station before, the older part of the graphs is the new station's.
* It is refused while the entry's *Downloads* switch is off.

## Download volumes

MeteoSwiss publishes its forecast as large files covering all of
Switzerland.  The integration reads only the part for your place, so one
entry with the default settings downloads **about 20 MB a day**, up to 55 MB
on days with many weather warnings, and each further place about 15 MB more.
Starting Home Assistant costs a few MB extra.  The radar, when turned on, adds
15 to 35 MB a day, the same for any number of places, and up to about 150 MB
more on a day of steady rain, for the rain of the last hour.  The radar loop's
map adds about 250 kB per place at each start.

The diagnostic sensor *Downloaded last 24 hours*, on each forecast's device,
shows what the integration actually downloaded over the last 24 hours, for all
places together: forecasts, measurements, warnings and the radar.
*Downloaded this month* does the same from the first of the month (local
time), and is kept across restarts, for automations on a monthly data
allowance.

Each forecast's device also has a *Downloads* switch.  Turned off, that place
downloads nothing (no measurements, forecasts, warnings or radar) and its
entities keep their last values, until it is turned on again; a restart
keeps it off.  For example, to stop at 3 GB a month and start again on the
first:

```yaml
automation:
  - alias: MeteoSwiss data allowance
    triggers:
      - trigger: numeric_state
        entity_id: sensor.zurich_downloaded_this_month
        above: 3000  # MB
        id: over
      - trigger: time
        at: "00:05:00"
        id: day
    conditions:
      - condition: or
        conditions:
          - condition: trigger
            id: over
          - condition: template
            value_template: "{{ now().day == 1 }}"
    actions:
      - action: "switch.turn_{{ 'off' if trigger.id == 'over' else 'on' }}"
        target:
          entity_id: switch.zurich_downloads
```

Weather warnings stop too while it is off, so think twice before relying on
them with a tight allowance.

If MeteoSwiss changes how it publishes its files, the integration downloads
them whole rather than stop working, until it is updated: **at worst several GB
a day**.  It is checked against MeteoSwiss's files twice a day, so
such changes are noticed and fixed quickly; and if it downloads far more than
usual, a repair notice asks you to update it.

On a metered connection, turn off *Hourly temperatures* (and the freezing
level) under *Configure*: those large files are then not downloaded at all.

## Data sources

All data is from MeteoSwiss (Source: MeteoSwiss), under its
[open data terms](https://opendatadocs.meteoswiss.ch/general/terms-of-use):

* [Local forecasts](https://opendatadocs.meteoswiss.ch/e-forecast-data/e4-local-forecast-data)
  of postal codes, stations and mountains, updated hourly.
* [Automatic weather stations](https://opendatadocs.meteoswiss.ch/a-data-groundbased/a1-automatic-weather-stations)
  and [precipitation stations](https://opendatadocs.meteoswiss.ch/a-data-groundbased/a2-automatic-precipitation-stations)
  (SwissMetNet), for the current conditions, Föhn and climate normals.
* [Pollen stations](https://opendatadocs.meteoswiss.ch/a-data-groundbased/a7-pollen-stations).
* [Weather radar](https://opendatadocs.meteoswiss.ch/d-radar-data): the rain
  rate (RZC), probability of hail (POH), maximum expected hail size (MESHS)
  and the rain of the last hour (CombiPrecip); the map under the radar loop
  is swisstopo's.
* Weather warnings, from MeteoSwiss's warnings as published by
  [MeteoAlarm](https://meteoalarm.org) (EUMETNET).

The daily chance of rain and gusts are the highest of the day.  Cloud cover
combines MeteoSwiss's low, medium and high cloud forecasts, assuming the layers
overlap at random; thin high cloud counts fully, so a sunny symbol can come
with high cloud cover.  The measured values are described in the
[parameter list](https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/ogd-smn_meta_parameters.csv).

### Privacy

The integration contacts only `data.geo.admin.ch` (forecasts, measurements and
the lists of places and stations) and `feeds.meteoalarm.org` (warnings), and,
with the radar turned on, `wms.geo.admin.ch` (the radar loop's map).  Your
coordinates never leave Home Assistant: the closest postal code, stations and
warnings are worked out locally from those lists.
What it downloads still hints
at your approximate area: the files of the stations you chose, the parts of the
forecast files holding your place, and the details of warnings that cover you.
Radar images cover all of Switzerland and are downloaded whole.  The radar
loop's map is a 70 km square centred on the 10 km square the place is in, not
on the place, so that request, once per start, tells swisstopo only which
10 km square that is.

## Troubleshooting

Please report problems in the
[issues](https://github.com/janvt/homeassistant-meteoswiss/issues), with the
entry's diagnostics (*Download diagnostics* in the entry's menu: what it read
from MeteoSwiss and how, without your coordinates or postal code) and a debug
log.  To get one, turn on debug logging in the entry's menu, or add this to
`configuration.yaml` and restart:

```yaml
logger:
  logs:
    custom_components.meteoswiss: debug
```

## Development

```bash
pip install -r requirements_test.txt
pytest
ruff check
```

The tests run against in-memory copies of MeteoSwiss's files, so they need no
network.  CI runs them, ruff, hassfest and the HACS checks on every push.

## Credits

* [websylv](https://github.com/websylv/homeassistant-meteoswiss) wrote the
  original MeteoSwiss integration.
* [Rudd-O (Manuel Amador)](https://github.com/Rudd-O/homeassistant-meteoswiss)
  took it over, rewrote it with the setup flow, unique IDs, separate
  precipitation stations and repair notices, and maintained it for years with
  its contributors.
* This version replaces the MeteoSwiss app API those used with MeteoSwiss open
  data, and adds the hourly forecast, mountain forecasts, summaries, warnings,
  pollen, Föhn and climate normals.

Thanks to all of them, and to MeteoSwiss for publishing its data openly.
