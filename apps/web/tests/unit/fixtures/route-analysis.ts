import type { LineString } from "geojson";

import type {
  ChargingStationSummary,
  ChargingStop,
  EnergyIntensity,
  RouteAnalysis,
  RouteSegmentAnalysis,
  TrafficEvent,
  TrafficSeverity,
  VehicleProfile,
} from "@/types/domain";

/**
 * A small synthetic `RouteAnalysis`, built so every number in it can be checked by hand.
 *
 * The default route is 200 km in four equal 50 km segments. That is not an arbitrary choice:
 * the Streckenband draws into a 1000-unit viewBox, so a 200 km route scales at exactly
 * 5 units/km and every rect the component emits has an x and a width that can be predicted
 * with mental arithmetic.
 */

export const VEHICLE: VehicleProfile = {
  code: "vw-id4-pro",
  display_name: "VW ID.4 Pro",
  vehicle_class: "suv",
  battery_capacity_kwh: 82,
  usable_capacity_kwh: 77,
  nominal_consumption_kwh_100km: 18.5,
  max_dc_power_kw: 135,
  max_ac_power_kw: 11,
};

export interface SegmentSpec {
  ordinal: number;
  start_offset_km: number;
  distance_km: number;
  kwh_per_100km: number;
  soc_at_end_percent: number;
  energy_intensity: EnergyIntensity;
  geometry?: LineString | null;
  temperature_c?: number | null;
  traffic_severity?: TrafficSeverity | null;
}

export function makeSegment(spec: SegmentSpec): RouteSegmentAnalysis {
  return {
    ordinal: spec.ordinal,
    start_offset_km: spec.start_offset_km,
    distance_km: spec.distance_km,
    road_class: "motorway",
    speed_limit_kmh: null,
    assumed_speed_kmh: 120,
    // `?? ` would swallow an explicit null, and a null temperature is exactly what these
    // tests need to exercise: DWD coverage is not universal, so the column is nullable.
    temperature_c: spec.temperature_c === undefined ? 8.4 : spec.temperature_c,
    traffic_severity: spec.traffic_severity === undefined ? null : spec.traffic_severity,
    // Energy for the segment, consistent with its rate: kWh = rate * km / 100.
    kwh: (spec.kwh_per_100km * spec.distance_km) / 100,
    kwh_per_100km: spec.kwh_per_100km,
    soc_at_end_percent: spec.soc_at_end_percent,
    energy_intensity: spec.energy_intensity,
    geometry: spec.geometry ?? null,
  };
}

/** Four 50 km segments climbing through all four intensity levels. */
export const FOUR_SEGMENTS: RouteSegmentAnalysis[] = [
  makeSegment({
    ordinal: 0,
    start_offset_km: 0,
    distance_km: 50,
    kwh_per_100km: 17.2,
    soc_at_end_percent: 78,
    energy_intensity: "low",
  }),
  makeSegment({
    ordinal: 1,
    start_offset_km: 50,
    distance_km: 50,
    kwh_per_100km: 21.4,
    soc_at_end_percent: 63,
    energy_intensity: "medium",
  }),
  makeSegment({
    ordinal: 2,
    start_offset_km: 100,
    distance_km: 50,
    kwh_per_100km: 24.9,
    soc_at_end_percent: 46,
    energy_intensity: "high",
  }),
  makeSegment({
    ordinal: 3,
    start_offset_km: 150,
    distance_km: 50,
    kwh_per_100km: 31.6,
    soc_at_end_percent: 25,
    energy_intensity: "critical",
  }),
];

export function makeStation(overrides: Partial<ChargingStationSummary> = {}): ChargingStationSummary {
  return {
    id: "station-1",
    external_id: "BNA-0001",
    operator: "EnBW mobility+",
    city: "Heilbronn",
    postal_code: "74072",
    street: "Autobahnrastplatz",
    house_number: "1",
    bundesland: "BW",
    latitude: 49.1427,
    longitude: 9.2109,
    max_power_kw: 300,
    total_power_kw: 600,
    charging_points_count: 4,
    charging_category: "ultra_fast",
    is_fast_charger: true,
    commissioned_on: "2023-05-17",
    ...overrides,
  };
}

export function makeStop(overrides: Partial<ChargingStop> = {}): ChargingStop {
  return {
    station: makeStation(),
    arrival_soc_percent: 18,
    departure_soc_percent: 80,
    detour_km: 1.2,
    charge_time_min: 24,
    energy_added_kwh: 47.7,
    max_power_kw: 300,
    avg_power_kw: 119,
    offset_km: 100,
    rationale_de: "Schnellster Halt vor dem kritischen Abschnitt.",
    rationale_en: "Fastest stop before the critical segment.",
    ...overrides,
  };
}

export function makeTrafficEvent(overrides: Partial<TrafficEvent> = {}): TrafficEvent {
  return {
    id: "event-1",
    external_id: "AB-1",
    event_type: "roadworks",
    severity: "high",
    road_name: "A5",
    direction: "Süd",
    title: "Baustelle Frankfurter Kreuz",
    description: null,
    latitude: 50.1109,
    longitude: 8.6821,
    geometry: null,
    starts_at: null,
    ends_at: null,
    is_blocked: false,
    delay_minutes: 12,
    ...overrides,
  };
}

export interface AnalysisSpec {
  distance_m?: number;
  segments?: RouteSegmentAnalysis[];
  traffic_events?: TrafficEvent[];
  start_soc_percent?: number;
}

export function makeAnalysis(spec: AnalysisSpec = {}): RouteAnalysis {
  const segments = spec.segments ?? FOUR_SEGMENTS;
  const distanceM = spec.distance_m ?? 200_000;

  return {
    route: {
      id: "route-1",
      slug: "frankfurt-stuttgart",
      distance_m: distanceM,
      duration_s: 7_320,
      geometry: {
        type: "LineString",
        coordinates: [
          [8.6821, 50.1109],
          [9.1829, 48.7758],
        ],
      },
      origin: { name: "Frankfurt am Main", latitude: 50.1109, longitude: 8.6821 },
      destination: { name: "Stuttgart", latitude: 48.7758, longitude: 9.1829 },
    },
    vehicle: VEHICLE,
    start_soc_percent: spec.start_soc_percent ?? 90,
    arrival_soc_percent: segments.at(-1)?.soc_at_end_percent ?? 90,
    min_soc_percent_required: 10,
    energy_kwh_total: segments.reduce((sum, segment) => sum + segment.kwh, 0),
    avg_consumption_kwh_100km: 23.8,
    baseline_kwh_100km: 21.0,
    model_kwh_100km: 23.8,
    model_name: "gradient_boosting",
    model_version: "1.0.0",
    weather_penalty_percent: 8.1,
    traffic_penalty_percent: 3.4,
    total_penalty_percent: 11.5,
    charging_required: true,
    segments,
    traffic_events: spec.traffic_events ?? [],
    weather_points: [],
    explanation: { headline: "Kälte und Verkehr erhöhen den Verbrauch.", drivers: [] },
    data_modes: { routing: "live", weather: "live", traffic: "live" },
    generated_at: "2026-09-14T10:00:00Z",
  };
}

/** A three-point LineString whose middle coordinate is the point the component projects from. */
export function lineThrough(latitude: number, longitude: number): LineString {
  return {
    type: "LineString",
    coordinates: [
      [longitude - 0.01, latitude - 0.01],
      [longitude, latitude],
      [longitude + 0.01, latitude + 0.01],
    ],
  };
}
