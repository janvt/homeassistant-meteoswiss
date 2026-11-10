// A dashboard of the MeteoSwiss integration, made from the entities it has.
//
// Home Assistant loads this module on every page.  It defines the dashboard
// strategy "custom:meteoswiss", which "Add dashboard" offers, and which builds
// one view for each place: its forecast and warnings, the radar, Föhn,
// pollen, the climate normals and its stations.  Only the sections of the
// entities a place has are shown, so the options and stations an entry is set
// up with decide them, and the dashboard follows when they change.
//
// Its settings, all optional: "places", the names of the places to show, in
// their order, for one view each.

const DOMAIN = "meteoswiss";

// The sections of a place's view, with the translation keys of their
// entities in the order they are shown.  The station sensors are their own
// sections.
const SECTIONS = [
  ["warnings", ["weather_warning", "weather_warning_next_24h"]],
  [
    "today",
    [
      "max_temperature_today",
      "min_temperature_today",
      "min_temperature_tonight",
      "precipitation_today",
      "precipitation_next_hour",
      "precipitation_probability_next_hour",
      "sunshine_today",
      "snow_tonight",
      "snow_next_24h",
      "snow_next_48h",
      "freezing_level",
      "lowest_freezing_level_48h",
    ],
  ],
  [
    "tomorrow",
    ["sunshine_tomorrow", "sunshine_tomorrow_midday", "max_gust_tomorrow_midday"],
  ],
  [
    "radar",
    [
      "radar_loop",
      "radar_rain_rate",
      "radar_rain_last_hour",
      "radar_hail_probability",
      "radar_hail_size",
    ],
  ],
  ["foehn", ["foehn", "foehn_outlook", "foehn_pressure_difference"]],
  [
    "pollen",
    [
      "pollen_alder",
      "pollen_ash",
      "pollen_beech",
      "pollen_birch",
      "pollen_grasses",
      "pollen_hazel",
      "pollen_oak",
    ],
  ],
  [
    "climate",
    [
      "high_vs_normal",
      "low_vs_normal",
      "month_vs_normal",
      "month_rain_vs_normal",
      "month_sunshine_vs_normal",
      "record_high",
    ],
  ],
  ["downloads", ["downloads", "downloaded", "downloaded_this_month"]],
];

// The headings, in the integration's languages.  Romansh is in German.
const HEADINGS = {
  en: {
    forecast: "Forecast",
    warnings: "Warnings",
    today: "Today",
    tomorrow: "Tomorrow",
    radar: "Radar",
    foehn: "Föhn",
    pollen: "Pollen",
    climate: "Compared with normal",
    downloads: "Downloads",
    more: "More",
    nothing: "There are no MeteoSwiss places set up yet.",
  },
  de: {
    forecast: "Prognose",
    warnings: "Warnungen",
    today: "Heute",
    tomorrow: "Morgen",
    radar: "Radar",
    foehn: "Föhn",
    pollen: "Pollen",
    climate: "Im Vergleich zur Norm",
    downloads: "Downloads",
    more: "Weitere",
    nothing: "Es ist noch kein MeteoSwiss-Ort eingerichtet.",
  },
  fr: {
    forecast: "Prévisions",
    warnings: "Alertes",
    today: "Aujourd'hui",
    tomorrow: "Demain",
    radar: "Radar",
    foehn: "Fœhn",
    pollen: "Pollen",
    climate: "Par rapport à la norme",
    downloads: "Téléchargements",
    more: "Autres",
    nothing: "Aucun lieu MeteoSwiss n'est encore configuré.",
  },
  it: {
    forecast: "Previsioni",
    warnings: "Allerte",
    today: "Oggi",
    tomorrow: "Domani",
    radar: "Radar",
    foehn: "Favonio",
    pollen: "Pollini",
    climate: "Rispetto alla norma",
    downloads: "Download",
    more: "Altro",
    nothing: "Non è ancora configurato alcun luogo MeteoSwiss.",
  },
  es: {
    forecast: "Previsión",
    warnings: "Avisos",
    today: "Hoy",
    tomorrow: "Mañana",
    radar: "Radar",
    foehn: "Foehn",
    pollen: "Polen",
    climate: "Comparado con lo normal",
    downloads: "Descargas",
    more: "Más",
    nothing: "Todavía no hay ningún lugar de MeteoSwiss configurado.",
  },
  nb: {
    forecast: "Værvarsel",
    warnings: "Farevarsler",
    today: "I dag",
    tomorrow: "I morgen",
    radar: "Radar",
    foehn: "Føhn",
    pollen: "Pollen",
    climate: "Sammenlignet med normalen",
    downloads: "Nedlastinger",
    more: "Mer",
    nothing: "Ingen MeteoSwiss-steder er satt opp ennå.",
  },
};
HEADINGS.rm = HEADINGS.de;

export function headings(hass) {
  const language = (hass.locale?.language || hass.language || "en").split("-")[0];
  return HEADINGS[language] || HEADINGS.en;
}

function slug(text) {
  return (
    text
      .normalize("NFD")
      .replace(/[̀-ͯ]/g, "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-|-$/g, "") || "place"
  );
}

function deviceName(hass, id) {
  const device = hass.devices[id];
  return device?.name_by_user || device?.name || "";
}

// The places of the integration: for each weather entity, its device and the
// devices of the entry's stations, with their entities that are shown.
export function places(hass) {
  const shown = Object.values(hass.entities).filter(
    (entity) =>
      entity.platform === DOMAIN &&
      !entity.hidden &&
      entity.entity_id in hass.states,
  );
  const byDevice = new Map();
  for (const entity of shown) {
    if (!entity.device_id) continue;
    if (!byDevice.has(entity.device_id)) byDevice.set(entity.device_id, []);
    byDevice.get(entity.device_id).push(entity);
  }
  const found = [];
  for (const entity of shown) {
    if (!entity.entity_id.startsWith("weather.") || !entity.device_id) continue;
    const entries = hass.devices[entity.device_id]?.config_entries || [];
    const stations = [...byDevice.keys()]
      .filter(
        (id) =>
          id !== entity.device_id &&
          (hass.devices[id]?.config_entries || []).some((e) => entries.includes(e)),
      )
      .sort((a, b) => deviceName(hass, a).localeCompare(deviceName(hass, b)));
    found.push({
      name: deviceName(hass, entity.device_id) || entity.entity_id,
      weather: entity.entity_id,
      entities: byDevice.get(entity.device_id) || [],
      stations: stations.map((id) => ({
        name: deviceName(hass, id),
        entities: byDevice.get(id),
      })),
    });
  }
  return found.sort((a, b) => a.name.localeCompare(b.name));
}

// The station sensors in the order of the integration's sensor types, then
// the others by their entity ID.
const STATION_ORDER = [
  "temperature",
  "humidity",
  "dew_point",
  "10minrain",
  "10minsun",
  "sun_radiant",
  "wind_speed",
  "wind_speed_max",
  "wind_direction",
  "pressure",
  "pressure_qff",
  "pressure_qnh",
];

function byStationOrder(a, b) {
  const rank = (entity) => {
    const index = STATION_ORDER.indexOf(entity.translation_key);
    return index === -1 ? STATION_ORDER.length : index;
  };
  return rank(a) - rank(b) || a.entity_id.localeCompare(b.entity_id);
}

function heading(text) {
  return { type: "heading", heading: text };
}

function card(entity) {
  if (entity.entity_id.startsWith("image.")) {
    return {
      type: "picture-entity",
      entity: entity.entity_id,
      show_state: false,
      show_name: false,
      grid_options: { columns: "full" },
    };
  }
  return { type: "tile", entity: entity.entity_id };
}

export function placeView(hass, place, used = new Set()) {
  const words = headings(hass);
  const sections = [
    {
      type: "grid",
      cards: [
        heading(words.forecast),
        {
          type: "weather-forecast",
          entity: place.weather,
          forecast_type: "daily",
          grid_options: { columns: "full" },
        },
        {
          type: "weather-forecast",
          entity: place.weather,
          forecast_type: "hourly",
          show_current: false,
          grid_options: { columns: "full" },
        },
      ],
    },
  ];
  // The sections of what MeteoSwiss forecasts and measures for the place,
  // from all its devices: the climate normals and the Föhn index, say, are
  // the weather station's.
  const all = [place.entities, ...place.stations.map((s) => s.entities)].flat();
  const byKey = new Map();
  for (const entity of all) {
    if (!byKey.has(entity.translation_key)) byKey.set(entity.translation_key, entity);
  }
  const placed = new Set();
  for (const [name, keys] of SECTIONS) {
    const entities = keys.filter((key) => byKey.has(key)).map((key) => byKey.get(key));
    if (entities.length) {
      entities.forEach((entity) => placed.add(entity.entity_id));
      sections.push({ type: "grid", cards: [heading(words[name]), ...entities.map(card)] });
    }
  }
  // What a newer version may add, so that it is not left out.
  const more = place.entities.filter(
    (entity) => !entity.entity_id.startsWith("weather.") && !placed.has(entity.entity_id),
  );
  if (more.length) {
    sections.push({ type: "grid", cards: [heading(words.more), ...more.map(card)] });
  }
  // The rest of each station's, which are its measurements.
  for (const station of place.stations) {
    const cards = station.entities
      .filter((entity) => !placed.has(entity.entity_id))
      .sort(byStationOrder)
      .map(card);
    if (cards.length) {
      sections.push({ type: "grid", cards: [heading(station.name), ...cards] });
    }
  }
  const base = slug(place.name);
  let path = base;
  for (let n = 2; used.has(path); n++) path = `${base}-${n}`;
  used.add(path);
  return {
    title: place.name,
    path,
    icon: "mdi:weather-partly-cloudy",
    type: "sections",
    max_columns: 4,
    sections,
  };
}

export function dashboard(config, hass) {
  let found = places(hass);
  if (Array.isArray(config.places) && config.places.length) {
    found = config.places
      .map((name) => found.find((place) => place.name === name))
      .filter(Boolean);
  }
  if (!found.length) {
    return {
      views: [
        {
          title: "MeteoSwiss",
          cards: [{ type: "markdown", content: headings(hass).nothing }],
        },
      ],
    };
  }
  const used = new Set();
  return { views: found.map((place) => placeView(hass, place, used)) };
}

// Outside a browser, as in the tests, only the functions above.
if (typeof customElements !== "undefined") {
  class MeteoSwissDashboardStrategy extends HTMLElement {
    static async generate(config, hass) {
      return dashboard(config, hass);
    }

    static getCreateSuggestions() {
      return { title: "MeteoSwiss", icon: "mdi:weather-partly-cloudy" };
    }
  }

  const tag = "ll-strategy-dashboard-meteoswiss";
  if (!customElements.get(tag)) {
    customElements.define(tag, MeteoSwissDashboardStrategy);
    window.customStrategies = window.customStrategies || [];
    window.customStrategies.push({
      type: DOMAIN,
      strategyType: "dashboard",
      name: "MeteoSwiss",
      description: "The forecast, warnings, radar, pollen and stations of each place.",
      documentationURL: "https://github.com/janvt/homeassistant-meteoswiss#dashboard",
    });
  }
}
