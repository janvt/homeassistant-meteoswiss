// The dashboard strategy, with Node's test runner: node --test tests/*.test.mjs
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

// As the module the frontend loads it as, which a .js file outside a package
// of modules is not to Node.
const source = await readFile(
  new URL("../custom_components/meteoswiss/frontend/meteoswiss-strategy.js", import.meta.url),
  "utf8",
);
const { dashboard } = await import(
  `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`
);

function fakeHass(places, language = "en") {
  const hass = { locale: { language }, entities: {}, devices: {}, states: {} };
  for (const { entry, device, name, entities } of places) {
    hass.devices[device] = { name, config_entries: [entry] };
    for (const [entity_id, translation_key, extra = {}] of entities) {
      hass.entities[entity_id] = {
        entity_id,
        translation_key,
        platform: "meteoswiss",
        device_id: device,
        ...extra,
      };
      hass.states[entity_id] = { entity_id, state: "1" };
    }
  }
  return hass;
}

const ZURICH = [
  {
    entry: "e1",
    device: "d1",
    name: "Zürich",
    entities: [
      ["weather.zurich", undefined],
      ["sensor.zurich_min_today", "min_temperature_today"],
      ["sensor.zurich_max_today", "max_temperature_today"],
      ["sensor.zurich_warning", "weather_warning"],
      ["image.zurich_radar", "radar_loop"],
      ["sensor.zurich_rain_rate", "radar_rain_rate"],
      ["sensor.zurich_birch", "pollen_birch"],
      ["sensor.zurich_new", "something_new"],
      ["sensor.zurich_hidden", "sunshine_today", { hidden: true }],
    ],
  },
  {
    entry: "e1",
    device: "d2",
    name: "Zürich / Fluntern",
    entities: [
      ["sensor.fluntern_wind", "wind_speed"],
      ["sensor.fluntern_temperature", "temperature"],
    ],
  },
];

function headingsOf(view) {
  return view.sections.map((section) => section.cards[0].heading);
}

function entitiesOf(view, heading) {
  return view.sections
    .find((section) => section.cards[0].heading === heading)
    .cards.slice(1)
    .map((c) => c.entity);
}

test("a view of a place with only the sections it has", () => {
  const [view] = dashboard({}, fakeHass(ZURICH)).views;
  assert.equal(view.title, "Zürich");
  assert.equal(view.path, "zurich");
  assert.deepEqual(headingsOf(view), [
    "Forecast",
    "Warnings",
    "Today",
    "Radar",
    "Pollen",
    "More",
    "Zürich / Fluntern",
  ]);
  assert.deepEqual(entitiesOf(view, "Today"), [
    "sensor.zurich_max_today",
    "sensor.zurich_min_today",
  ]);
  const radar = view.sections.find((s) => s.cards[0].heading === "Radar").cards[1];
  assert.equal(radar.type, "picture-entity");
  assert.deepEqual(entitiesOf(view, "Zürich / Fluntern"), [
    "sensor.fluntern_temperature",
    "sensor.fluntern_wind",
  ]);
  assert.deepEqual(entitiesOf(view, "More"), ["sensor.zurich_new"]);
});

test("in the language of the user", () => {
  const [view] = dashboard({}, fakeHass(ZURICH, "de-CH")).views;
  assert.equal(headingsOf(view)[0], "Prognose");
});

test("one view for each place, and the places chosen", () => {
  const bern = {
    entry: "e2",
    device: "d3",
    name: "Bern",
    entities: [["weather.bern", undefined]],
  };
  const hass = fakeHass([...ZURICH, bern]);
  assert.deepEqual(
    dashboard({}, hass).views.map((v) => v.title),
    ["Bern", "Zürich"],
  );
  // Bern's view has none of Zürich's station.
  assert.deepEqual(headingsOf(dashboard({}, hass).views[0]), ["Forecast"]);
  assert.deepEqual(
    dashboard({ places: ["Zürich", "Basel"] }, hass).views.map((v) => v.title),
    ["Zürich"],
  );
});

test("other integrations and missing states are left out", () => {
  const hass = fakeHass(ZURICH);
  hass.entities["sensor.other"] = {
    entity_id: "sensor.other",
    platform: "other",
    device_id: "d1",
  };
  hass.states["sensor.other"] = {};
  delete hass.states["sensor.zurich_birch"];
  const [view] = dashboard({}, hass).views;
  assert.ok(!headingsOf(view).includes("Pollen"));
  assert.deepEqual(entitiesOf(view, "More"), ["sensor.zurich_new"]);
});

test("without a place", () => {
  const { views } = dashboard({}, fakeHass([]));
  assert.equal(views.length, 1);
  assert.equal(views[0].cards[0].type, "markdown");
});

// Schlieren as a real entry has it: the Föhn index, the climate normals and
// snow depth are its weather station's, and the pollen its pollen station's.
const SCHLIEREN = [
  {
    entry: "e1",
    device: "forecast",
    name: "Schlieren",
    entities: [
      ["weather.schlieren", undefined],
      ["sensor.schlieren_weather_warning", "weather_warning"],
      ["sensor.schlieren_fohn_outlook", "foehn_outlook"],
      ["sensor.schlieren_highest_temperature_today", "max_temperature_today"],
      ["image.schlieren_radar_loop", "radar_loop"],
      ["sensor.schlieren_downloaded_last_24_hours", "downloaded"],
      ["switch.schlieren_downloads", "downloads"],
    ],
  },
  {
    entry: "e1",
    device: "station",
    name: "Zürich / Fluntern",
    entities: [
      ["sensor.zurich_fluntern_wind_speed", "wind_speed"],
      ["sensor.zurich_fluntern_temperature", "temperature"],
      ["sensor.zurich_fluntern_fohn", "foehn"],
      ["sensor.zurich_fluntern_high_vs_normal", "high_vs_normal"],
      ["sensor.zurich_fluntern_snow_depth", "snow_depth"],
    ],
  },
  {
    entry: "e1",
    device: "rain",
    name: "Uster",
    entities: [["sensor.uster_10_minute_rain", "10minrain"]],
  },
  {
    entry: "e1",
    device: "pollen",
    name: "Zürich pollen",
    entities: [
      ["sensor.zurich_pollen_birch", "pollen_birch"],
      ["sensor.zurich_pollen_alder", "pollen_alder"],
    ],
  },
];

test("a place's sections draw on all its devices", () => {
  const [view] = dashboard({}, fakeHass(SCHLIEREN)).views;
  assert.deepEqual(headingsOf(view), [
    "Forecast",
    "Warnings",
    "Today",
    "Radar",
    "Föhn",
    "Pollen",
    "Compared with normal",
    "Downloads",
    "Uster",
    "Zürich / Fluntern",
  ]);
  assert.deepEqual(entitiesOf(view, "Föhn"), [
    "sensor.zurich_fluntern_fohn",
    "sensor.schlieren_fohn_outlook",
  ]);
  assert.deepEqual(entitiesOf(view, "Pollen"), [
    "sensor.zurich_pollen_alder",
    "sensor.zurich_pollen_birch",
  ]);
  assert.deepEqual(entitiesOf(view, "Zürich / Fluntern"), [
    "sensor.zurich_fluntern_temperature",
    "sensor.zurich_fluntern_wind_speed",
    "sensor.zurich_fluntern_snow_depth",
  ]);
});

test("places and stations of the same name", () => {
  const andermatt = [
    {
      entry: "e1",
      device: "forecast",
      name: "Andermatt",
      entities: [["weather.andermatt", undefined]],
    },
    {
      entry: "e1",
      device: "station",
      name: "Andermatt",
      entities: [["sensor.andermatt_temperature", "temperature"]],
    },
    {
      entry: "e2",
      device: "forecast2",
      name: "Andermatt",
      entities: [["weather.andermatt_2", undefined]],
    },
  ];
  const views = dashboard({}, fakeHass(andermatt)).views;
  assert.deepEqual(
    views.map((v) => v.path),
    ["andermatt", "andermatt-2"],
  );
  assert.deepEqual(headingsOf(views[0]), ["Forecast", "Andermatt"]);
});
