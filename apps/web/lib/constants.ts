/** Response header the API uses to say whether a source answered live, from cache, or from a fixture. */
export const DATA_MODE_HEADER = "x-autotwin-data-mode";

/** Correlates a user-visible error with a backend log line. */
export const REQUEST_ID_HEADER = "x-request-id";

/** The flagship demo corridor (BUILD_SPEC §8). */
export const DEMO_ROUTE_SLUG = "frankfurt-stuttgart";

/** Germany, for the initial map viewport. */
export const GERMANY_BOUNDS: [[number, number], [number, number]] = [
  [5.8, 47.2],
  [15.1, 55.1],
];

export const GERMANY_CENTER: [number, number] = [10.45, 51.16];

/** Ordered energy-intensity levels; the CSS variables live in globals.css. */
export const ENERGY_LEVELS = ["low", "medium", "high", "critical"] as const;
export type EnergyLevel = (typeof ENERGY_LEVELS)[number];

export const ENERGY_LEVEL_VAR: Record<EnergyLevel, string> = {
  low: "var(--energy-low)",
  medium: "var(--energy-medium)",
  high: "var(--energy-high)",
  critical: "var(--energy-critical)",
};

/** The 16 federal states, in the order German tables conventionally list them. */
export const BUNDESLAENDER = [
  { code: "BW", name: "Baden-Württemberg" },
  { code: "BY", name: "Bayern" },
  { code: "BE", name: "Berlin" },
  { code: "BB", name: "Brandenburg" },
  { code: "HB", name: "Bremen" },
  { code: "HH", name: "Hamburg" },
  { code: "HE", name: "Hessen" },
  { code: "MV", name: "Mecklenburg-Vorpommern" },
  { code: "NI", name: "Niedersachsen" },
  { code: "NW", name: "Nordrhein-Westfalen" },
  { code: "RP", name: "Rheinland-Pfalz" },
  { code: "SL", name: "Saarland" },
  { code: "SN", name: "Sachsen" },
  { code: "ST", name: "Sachsen-Anhalt" },
  { code: "SH", name: "Schleswig-Holstein" },
  { code: "TH", name: "Thüringen" },
] as const;

/** Attribution that must always be rendered — see DATA_LICENSES.md. */
export const MAP_ATTRIBUTION =
  '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>-Mitwirkende';
