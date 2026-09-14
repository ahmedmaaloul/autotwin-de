import type { LineString, Point } from "geojson";

/**
 * Domain types mirroring the API contract in `docs/BUILD_SPEC.md` §7.
 *
 * `pnpm gen:api` regenerates `types/api.generated.ts` from the live OpenAPI schema; these
 * hand-written types are the stable, readable surface the components import, and the generated
 * file is what proves they still match. Where the two disagree, the generated file is right and
 * this one is a bug.
 *
 * Field names are snake_case because they come off the wire that way — renaming them in a
 * mapping layer would buy nothing but a place for typos to hide.
 */

export type DataOrigin = "official" | "simulated" | "derived";
export type ProviderMode = "live" | "cache" | "fixture";
export type IngestionStatus = "healthy" | "delayed" | "degraded" | "failed" | "simulation";
export type IngestionOutcome = "success" | "partial" | "failed";
export type ChargingCategory = "normal" | "fast" | "ultra_fast";
export type ConnectorType =
  | "type2"
  | "ccs"
  | "chademo"
  | "schuko"
  | "tesla"
  | "cee"
  | "other"
  | "unknown";
export type CurrentType = "ac" | "dc" | "unknown";
export type RoadClass =
  | "motorway"
  | "trunk"
  | "primary"
  | "secondary"
  | "tertiary"
  | "residential"
  | "service"
  | "unknown";
export type TrafficEventType =
  | "roadworks"
  | "closure"
  | "incident"
  | "warning"
  | "congestion"
  | "other";
export type TrafficSeverity = "low" | "moderate" | "high" | "severe";
export type WeatherCondition =
  | "clear"
  | "clouds"
  | "rain"
  | "snow"
  | "fog"
  | "storm"
  | "unknown";
export type EnergyIntensity = "low" | "medium" | "high" | "critical";
export type VehicleState = "idle" | "driving" | "charging" | "stopped" | "completed";
export type SimulationState =
  | "pending"
  | "running"
  | "paused"
  | "stopping"
  | "stopped"
  | "completed"
  | "failed";

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  has_next: boolean;
}

export interface Provenance {
  source: string;
  source_identifier?: string | null;
  source_url?: string | null;
  source_timestamp?: string | null;
  data_origin: DataOrigin;
  ingestion_run_id?: string | null;
  ingested_at?: string | null;
}

// ---------------------------------------------------------------- charging

export interface ChargingStationSummary {
  id: string;
  external_id: string;
  operator: string | null;
  city: string | null;
  postal_code: string | null;
  street: string | null;
  house_number: string | null;
  bundesland: string | null;
  latitude: number;
  longitude: number;
  max_power_kw: number | null;
  total_power_kw: number | null;
  charging_points_count: number;
  charging_category: ChargingCategory;
  is_fast_charger: boolean;
  commissioned_on: string | null;
}

export interface ChargingPoint {
  id: string;
  ordinal: number;
  connector_type: ConnectorType;
  current_type: CurrentType;
  power_kw: number | null;
}

export interface ChargingStationDetail extends ChargingStationSummary {
  charging_points: ChargingPoint[];
  provenance: Provenance;
}

export interface ChargingStatistics {
  by_bundesland: { bundesland: string; stations: number; fast_points: number; total_kw: number }[];
  by_power_class: { category: ChargingCategory; count: number }[];
  by_operator: { operator: string; stations: number }[];
  growth: { year: number; stations: number; cumulative: number }[];
}

// ---------------------------------------------------------------- coverage

export interface CoverageGap {
  start_offset_km: number;
  end_offset_km: number;
  gap_km: number;
  geometry?: LineString | null;
}

export interface CorridorCoverage {
  route_id: string;
  route_slug: string | null;
  route_name: string;
  buffer_km: number;
  min_power_kw: number;
  stations_in_corridor: number;
  fast_stations_in_corridor: number;
  stations_per_100km: number;
  max_gap_km: number;
  mean_gap_km: number;
  gaps: CoverageGap[];
  coverage_score: number;
  methodology: string;
}

export interface UnderservedCorridor {
  route_slug: string;
  name: string;
  worst_gap_km: number;
  segments: CoverageGap[];
}

export interface UnderservedReport {
  parameters: {
    min_power_kw: number;
    max_gap_km: number;
    corridor_buffer_km: number;
  };
  corridors: UnderservedCorridor[];
  generated_at: string;
  methodology: string;
}

// ---------------------------------------------------------------- weather / traffic

export interface WeatherObservation {
  id: string;
  observed_at: string;
  latitude: number;
  longitude: number;
  station_name: string | null;
  temperature_c: number | null;
  precipitation_mm: number | null;
  wind_speed_ms: number | null;
  humidity_percent: number | null;
  condition: WeatherCondition;
  provenance?: Provenance;
}

export interface TrafficEvent {
  id: string;
  external_id: string | null;
  event_type: TrafficEventType;
  severity: TrafficSeverity;
  road_name: string | null;
  direction: string | null;
  title: string;
  description: string | null;
  latitude: number;
  longitude: number;
  geometry?: LineString | null;
  starts_at: string | null;
  ends_at: string | null;
  is_blocked: boolean;
  delay_minutes: number | null;
  provenance?: Provenance;
}

// ---------------------------------------------------------------- routes

export interface VehicleProfile {
  code: string;
  display_name: string;
  vehicle_class: string;
  battery_capacity_kwh: number;
  usable_capacity_kwh: number;
  nominal_consumption_kwh_100km: number;
  max_dc_power_kw: number;
  max_ac_power_kw: number;
}

export interface RouteSummary {
  id: string;
  slug: string | null;
  name: string;
  origin_name: string;
  destination_name: string;
  distance_m: number;
  duration_s: number;
  is_demo: boolean;
}

export interface RouteSegmentAnalysis {
  ordinal: number;
  start_offset_km: number;
  distance_km: number;
  road_class: RoadClass;
  speed_limit_kmh: number | null;
  assumed_speed_kmh: number;
  temperature_c: number | null;
  traffic_severity: TrafficSeverity | null;
  kwh: number;
  kwh_per_100km: number;
  soc_at_end_percent: number;
  energy_intensity: EnergyIntensity;
  geometry?: LineString | null;
}

export interface EnergyDriver {
  factor: string;
  delta_percent: number;
  direction: "increase" | "decrease";
  label_de: string;
  label_en: string;
}

export interface RouteAnalysis {
  route: {
    id: string | null;
    slug: string | null;
    distance_m: number;
    duration_s: number;
    geometry: LineString;
    origin: { name: string; latitude: number; longitude: number };
    destination: { name: string; latitude: number; longitude: number };
  };
  vehicle: VehicleProfile;
  start_soc_percent: number;
  arrival_soc_percent: number;
  min_soc_percent_required: number;
  energy_kwh_total: number;
  avg_consumption_kwh_100km: number;
  baseline_kwh_100km: number;
  model_kwh_100km: number | null;
  model_name: string | null;
  model_version: string | null;
  weather_penalty_percent: number;
  traffic_penalty_percent: number;
  total_penalty_percent: number;
  charging_required: boolean;
  segments: RouteSegmentAnalysis[];
  traffic_events: TrafficEvent[];
  weather_points: WeatherObservation[];
  explanation: { headline: string; drivers: EnergyDriver[] };
  data_modes: { routing: ProviderMode; weather: ProviderMode; traffic: ProviderMode };
  generated_at: string;
}

export interface ChargingStop {
  station: ChargingStationSummary;
  arrival_soc_percent: number;
  departure_soc_percent: number;
  detour_km: number;
  charge_time_min: number;
  energy_added_kwh: number;
  max_power_kw: number;
  avg_power_kw: number;
  offset_km: number;
  rationale_de: string;
  rationale_en: string;
}

export interface ChargingPlan {
  feasible: boolean;
  reason: string | null;
  stops: ChargingStop[];
  total_time_min: number;
  driving_time_min: number;
  charging_time_min: number;
  detour_km_total: number;
  arrival_soc_percent: number;
  alternatives_considered: number;
  objective: string;
}

// ---------------------------------------------------------------- vehicles

export interface VehicleLive {
  vehicle_id: string;
  trip_id: string | null;
  route_id: string | null;
  model_code: string;
  recorded_at: string;
  latitude: number;
  longitude: number;
  heading_deg: number | null;
  speed_kmh: number;
  battery_soc_percent: number;
  battery_temperature_c: number;
  outside_temperature_c: number;
  instantaneous_power_kw: number;
  energy_consumption_kwh_100km: number;
  estimated_range_km: number;
  road_class: RoadClass;
  state: VehicleState;
}

export interface TelemetryPoint {
  recorded_at: string;
  latitude: number;
  longitude: number;
  speed_kmh: number;
  battery_soc_percent: number;
  energy_consumption_kwh_100km: number;
  odometer_m: number;
}

export interface TripSummary {
  trip_id: string;
  vehicle_id: string;
  route_id: string | null;
  started_at: string;
  ended_at: string | null;
  start_soc_percent: number;
  end_soc_percent: number | null;
  distance_m: number;
  energy_kwh: number;
  avg_consumption_kwh_100km: number | null;
  state: VehicleState;
}

// ---------------------------------------------------------------- simulation

export interface SimulationRun {
  id: string;
  name: string;
  state: SimulationState;
  vehicle_count: number;
  speed_factor: number;
  seed: number;
  started_at: string | null;
  stopped_at: string | null;
  events_emitted: number;
  errors: number;
}

export interface SimulatorStatus {
  state: SimulationState;
  run_id: string | null;
  vehicles_active: number;
  events_per_second: number;
  messages_processed: number;
  db_writes: number;
  errors: number;
  transport: "kafka" | "database";
  kafka: { connected: boolean; bootstrap_servers: string; topics: string[] } | null;
}

// ---------------------------------------------------------------- ml

export interface MLModelInfo {
  id: string;
  name: string;
  version: string;
  algorithm: string;
  trained_at: string;
  training_rows: number;
  feature_names: string[];
  is_active: boolean;
  training_data_origin: DataOrigin;
  notes: string | null;
}

export interface MLMetrics {
  model: MLModelInfo | null;
  metrics: {
    mae: number;
    rmse: number;
    r2: number;
    baseline_mae: number;
    baseline_rmse: number;
    baseline_r2: number;
  } | null;
  predicted_vs_actual: { actual: number; predicted: number; baseline: number }[];
  residuals: { bucket: number; count: number }[];
  feature_importance: { feature: string; importance: number }[];
}

export interface ShapContribution {
  feature: string;
  value: number;
  contribution: number;
  label_de: string;
  label_en: string;
}

// ---------------------------------------------------------------- data quality

export interface SourceQuality {
  source: string;
  status: IngestionStatus;
  data_origin: DataOrigin;
  last_run_at: string | null;
  age_minutes: number | null;
  rows_received: number;
  rows_accepted: number;
  rows_rejected: number;
  rows_duplicate: number;
  acceptance_rate: number;
  provider_mode: ProviderMode | null;
  licence: string | null;
  attribution: string | null;
  source_url: string | null;
}

export interface IngestionRun {
  id: string;
  source: string;
  pipeline: string;
  started_at: string;
  finished_at: string | null;
  status: IngestionOutcome;
  provider_mode: ProviderMode;
  rows_received: number;
  rows_accepted: number;
  rows_rejected: number;
  rows_duplicate: number;
  bytes_downloaded: number | null;
  source_url: string | null;
  error_message: string | null;
  quality_report: {
    violations?: { rule: string; field: string | null; message: string; row_index: number | null }[];
    rule_stats?: Record<string, number>;
    violations_truncated?: number;
  } | null;
}

// ---------------------------------------------------------------- dashboard

export interface DashboardSummary {
  vehicles_active: number;
  charging_stations_total: number;
  fast_charging_points_total: number;
  traffic_events_active: number;
  avg_consumption_kwh_100km: number | null;
  data_freshness: {
    source: string;
    last_run_at: string | null;
    status: IngestionStatus;
    age_minutes: number | null;
    data_origin: DataOrigin;
    mode: ProviderMode | null;
  }[];
  energy_trend: { bucket: string; kwh_100km: number }[];
  recent_traffic: TrafficEvent[];
  weather_snapshot: {
    temperature_c: number | null;
    condition: WeatherCondition;
    observed_at: string | null;
    station: string | null;
  } | null;
  charging_by_category: { category: ChargingCategory; count: number }[];
}

/** A GeoJSON point feature as the API returns it for map layers. */
export interface StationFeature {
  type: "Feature";
  geometry: Point;
  properties: {
    id: string;
    operator: string | null;
    max_power_kw: number | null;
    charging_category: ChargingCategory;
    is_fast_charger: boolean;
    city: string | null;
  };
}
