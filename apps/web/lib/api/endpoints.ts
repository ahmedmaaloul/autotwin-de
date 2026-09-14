import { apiFetch, apiFetchWithMeta, buildQuery, type ApiResult, type QueryValue } from "./client";

import type {
  ChargingPlan,
  ChargingStatistics,
  ChargingStationDetail,
  ChargingStationSummary,
  CorridorCoverage,
  DashboardSummary,
  IngestionRun,
  MLMetrics,
  MLModelInfo,
  Page,
  RouteAnalysis,
  RouteSummary,
  SimulationRun,
  SimulatorStatus,
  SourceQuality,
  StationFeature,
  TelemetryPoint,
  TrafficEvent,
  TripSummary,
  UnderservedReport,
  VehicleLive,
  VehicleProfile,
  WeatherObservation,
} from "@/types/domain";
import type { FeatureCollection } from "geojson";

/**
 * Every AutoTwin API call, in one place.
 *
 * Components never import `apiFetch` — they use the hooks in `hooks/`, which call these. That
 * keeps the URL shapes, query-parameter names and response types in a single file that can be
 * checked against `docs/BUILD_SPEC.md` §7 line by line.
 *
 * Functions returning `ApiResult<T>` are the ones whose screens show a degraded-source notice;
 * they carry the `X-AutoTwin-Data-Mode` header through to the UI.
 */

// ---------------------------------------------------------------- dashboard

export function getDashboardSummary(): Promise<ApiResult<DashboardSummary>> {
  return apiFetchWithMeta<DashboardSummary>("/dashboard/summary");
}

// ---------------------------------------------------------------- charging

export interface ChargingFilters {
  bbox?: string;
  bundesland?: string;
  operator?: string;
  min_power_kw?: number;
  fast_only?: boolean;
  category?: string;
  commissioned_from?: number;
  commissioned_to?: number;
  q?: string;
  page?: number;
  page_size?: number;
}

export function getChargingStations(
  filters: ChargingFilters = {},
): Promise<ApiResult<Page<ChargingStationSummary>>> {
  return apiFetchWithMeta<Page<ChargingStationSummary>>(
    `/charging/stations${buildQuery(filters as Record<string, QueryValue>)}`,
  );
}

export function getChargingStationsGeoJson(
  filters: Omit<ChargingFilters, "page" | "page_size"> = {},
): Promise<FeatureCollection<StationFeature["geometry"], StationFeature["properties"]>> {
  return apiFetch(`/charging/stations/geojson${buildQuery(filters as Record<string, QueryValue>)}`);
}

export function getChargingStation(id: string): Promise<ChargingStationDetail> {
  return apiFetch(`/charging/stations/${encodeURIComponent(id)}`);
}

export function getChargingStatistics(): Promise<ChargingStatistics> {
  return apiFetch("/charging/statistics");
}

export function getCorridorCoverage(params: {
  route_slug?: string;
  route_id?: string;
  buffer_km?: number;
  min_power_kw?: number;
}): Promise<CorridorCoverage> {
  return apiFetch(`/charging/coverage${buildQuery(params as Record<string, QueryValue>)}`);
}

export function getUnderserved(params: {
  min_power_kw?: number;
  max_gap_km?: number;
  corridor_buffer_km?: number;
  route_slug?: string;
}): Promise<UnderservedReport> {
  return apiFetch(`/charging/underserved${buildQuery(params as Record<string, QueryValue>)}`);
}

// ---------------------------------------------------------------- weather & traffic

export function getWeather(params: {
  bbox?: string;
  lat?: number;
  lon?: number;
  radius_km?: number;
  page?: number;
  page_size?: number;
}): Promise<ApiResult<Page<WeatherObservation>>> {
  return apiFetchWithMeta(`/weather${buildQuery(params as Record<string, QueryValue>)}`);
}

export function getTrafficEvents(params: {
  bbox?: string;
  event_type?: string;
  severity?: string;
  road?: string;
  active_only?: boolean;
  page?: number;
  page_size?: number;
}): Promise<ApiResult<Page<TrafficEvent>>> {
  return apiFetchWithMeta(`/traffic/events${buildQuery(params as Record<string, QueryValue>)}`);
}

export function getTrafficGeoJson(params: { bbox?: string; active_only?: boolean } = {}): Promise<
  FeatureCollection
> {
  return apiFetch(`/traffic/events/geojson${buildQuery(params as Record<string, QueryValue>)}`);
}

// ---------------------------------------------------------------- routes

export function getRoutes(): Promise<Page<RouteSummary>> {
  return apiFetch("/routes?page_size=50");
}

export interface RouteAnalyzeRequest {
  route_slug?: string;
  origin?: string;
  destination?: string;
  vehicle_code: string;
  start_soc_percent: number;
  min_arrival_soc_percent: number;
}

export function analyzeRoute(body: RouteAnalyzeRequest): Promise<ApiResult<RouteAnalysis>> {
  return apiFetchWithMeta<RouteAnalysis>("/routes/analyze", { json: body, timeoutMs: 60_000 });
}

export function optimizeCharging(body: RouteAnalyzeRequest): Promise<ChargingPlan> {
  return apiFetch("/routes/optimize-charging", { json: body, timeoutMs: 60_000 });
}

export function getVehicleProfiles(): Promise<VehicleProfile[]> {
  return apiFetch("/vehicles/profiles");
}

// ---------------------------------------------------------------- vehicles & trips

export function getLiveVehicles(): Promise<VehicleLive[]> {
  return apiFetch("/vehicles/live");
}

export function getVehicleTelemetry(
  vehicleId: string,
  params: { since?: string; limit?: number } = {},
): Promise<TelemetryPoint[]> {
  return apiFetch(
    `/vehicles/${encodeURIComponent(vehicleId)}/telemetry${buildQuery(params as Record<string, QueryValue>)}`,
  );
}

export function getTrips(params: { vehicle_id?: string; page?: number; page_size?: number } = {}) {
  return apiFetch<Page<TripSummary>>(`/trips${buildQuery(params as Record<string, QueryValue>)}`);
}

// ---------------------------------------------------------------- simulation

export function getSimulatorStatus(): Promise<SimulatorStatus> {
  return apiFetch("/simulations/status", { timeoutMs: 8000 });
}

export function getSimulations(): Promise<Page<SimulationRun>> {
  return apiFetch("/simulations?page_size=20");
}

export interface SimulationCreateRequest {
  name?: string;
  vehicle_count: number;
  speed_factor: number;
  seed?: number;
  route_slugs?: string[];
  weather_mode?: string;
  traffic_intensity?: string;
  vehicle_mix?: Record<string, number>;
}

export function createSimulation(body: SimulationCreateRequest): Promise<SimulationRun> {
  return apiFetch("/simulations", { json: body });
}

export function simulationAction(
  id: string,
  action: "start" | "pause" | "resume" | "stop" | "reset",
): Promise<SimulationRun> {
  return apiFetch(`/simulations/${encodeURIComponent(id)}/${action}`, { method: "POST", json: {} });
}

// ---------------------------------------------------------------- ml

export function getMlModels(): Promise<MLModelInfo[]> {
  return apiFetch("/ml/models");
}

export function getMlMetrics(params: { model?: string; version?: string } = {}): Promise<MLMetrics> {
  return apiFetch(`/ml/metrics${buildQuery(params as Record<string, QueryValue>)}`);
}

// ---------------------------------------------------------------- data quality

export function getDataQuality(): Promise<SourceQuality[]> {
  return apiFetch("/data/quality");
}

export function getIngestionRuns(params: { source?: string; page?: number; page_size?: number } = {}) {
  return apiFetch<Page<IngestionRun>>(
    `/data/ingestions${buildQuery(params as Record<string, QueryValue>)}`,
  );
}

// ---------------------------------------------------------------- analytics

export function getEnergyAnalytics(dimension: "temperature" | "speed" | "traffic") {
  return apiFetch<{
    dimension: string;
    buckets: { bucket: number | string; mean_kwh_100km: number; samples: number }[];
  }>(`/analytics/energy${buildQuery({ dimension })}`);
}

export function getRegionalAnalytics() {
  return apiFetch<
    {
      bundesland: string;
      stations: number;
      fast_points: number;
      total_kw: number;
      stations_per_1000km2: number;
    }[]
  >("/analytics/regions");
}
