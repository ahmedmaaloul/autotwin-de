"use client";

import { useMutation, useQuery, useQueryClient, type UseQueryOptions } from "@tanstack/react-query";

import * as api from "@/lib/api/endpoints";
import type { ChargingFilters, RouteAnalyzeRequest, SimulationCreateRequest } from "@/lib/api/endpoints";

/**
 * TanStack Query wrappers, one per endpoint.
 *
 * Components import from here and nothing else. Query keys are structured
 * `[domain, ...discriminators]` so a mutation can invalidate exactly the right slice — e.g.
 * starting a simulation invalidates `["simulation"]` without touching cached charging data
 * that costs a 90 000-row query to rebuild.
 */

export const queryKeys = {
  dashboard: ["dashboard"] as const,
  chargingStations: (filters: ChargingFilters) => ["charging", "stations", filters] as const,
  chargingGeoJson: (filters: ChargingFilters) => ["charging", "geojson", filters] as const,
  chargingStation: (id: string) => ["charging", "station", id] as const,
  chargingStatistics: ["charging", "statistics"] as const,
  coverage: (params: object) => ["charging", "coverage", params] as const,
  underserved: (params: object) => ["charging", "underserved", params] as const,
  traffic: (params: object) => ["traffic", params] as const,
  weather: (params: object) => ["weather", params] as const,
  routes: ["routes"] as const,
  vehicleProfiles: ["vehicles", "profiles"] as const,
  liveVehicles: ["vehicles", "live"] as const,
  telemetry: (id: string) => ["vehicles", "telemetry", id] as const,
  trips: (params: object) => ["trips", params] as const,
  simulationStatus: ["simulation", "status"] as const,
  simulations: ["simulation", "runs"] as const,
  mlModels: ["ml", "models"] as const,
  mlMetrics: (params: object) => ["ml", "metrics", params] as const,
  dataQuality: ["data", "quality"] as const,
  ingestions: (params: object) => ["data", "ingestions", params] as const,
  energyAnalytics: (dimension: string) => ["analytics", "energy", dimension] as const,
  regionalAnalytics: ["analytics", "regions"] as const,
};

export function useDashboardSummary() {
  return useQuery({
    queryKey: queryKeys.dashboard,
    queryFn: api.getDashboardSummary,
    refetchInterval: 60_000,
  });
}

export function useChargingStations(filters: ChargingFilters) {
  return useQuery({
    queryKey: queryKeys.chargingStations(filters),
    queryFn: () => api.getChargingStations(filters),
    // Keep the previous page visible while the next one loads — a table that blanks between
    // pages reads as a bug.
    placeholderData: (previous) => previous,
  });
}

export function useChargingGeoJson(filters: ChargingFilters, enabled = true) {
  return useQuery({
    queryKey: queryKeys.chargingGeoJson(filters),
    queryFn: () => api.getChargingStationsGeoJson(filters),
    enabled,
    // The GeoJSON payload is large and the underlying dataset changes monthly, so it is worth
    // holding on to for the whole session.
    staleTime: 10 * 60_000,
    gcTime: 30 * 60_000,
  });
}

export function useChargingStation(id: string | null) {
  return useQuery({
    queryKey: queryKeys.chargingStation(id ?? ""),
    queryFn: () => api.getChargingStation(id as string),
    enabled: Boolean(id),
  });
}

export function useChargingStatistics() {
  return useQuery({
    queryKey: queryKeys.chargingStatistics,
    queryFn: api.getChargingStatistics,
    staleTime: 10 * 60_000,
  });
}

export function useCorridorCoverage(params: Parameters<typeof api.getCorridorCoverage>[0], enabled = true) {
  return useQuery({
    queryKey: queryKeys.coverage(params),
    queryFn: () => api.getCorridorCoverage(params),
    enabled,
  });
}

export function useUnderserved(params: Parameters<typeof api.getUnderserved>[0]) {
  return useQuery({
    queryKey: queryKeys.underserved(params),
    queryFn: () => api.getUnderserved(params),
  });
}

export function useTrafficEvents(params: Parameters<typeof api.getTrafficEvents>[0]) {
  return useQuery({
    queryKey: queryKeys.traffic(params),
    queryFn: () => api.getTrafficEvents(params),
    refetchInterval: 5 * 60_000,
  });
}

export function useRoutes() {
  return useQuery({ queryKey: queryKeys.routes, queryFn: api.getRoutes, staleTime: 10 * 60_000 });
}

export function useVehicleProfiles() {
  return useQuery({
    queryKey: queryKeys.vehicleProfiles,
    queryFn: api.getVehicleProfiles,
    staleTime: Infinity,
  });
}

export function useRouteAnalysis() {
  return useMutation({
    mutationKey: ["routes", "analyze"],
    mutationFn: (request: RouteAnalyzeRequest) => api.analyzeRoute(request),
  });
}

export function useChargingOptimisation() {
  return useMutation({
    mutationKey: ["routes", "optimize-charging"],
    mutationFn: (request: RouteAnalyzeRequest) => api.optimizeCharging(request),
  });
}

export function useVehicleTelemetry(vehicleId: string | null, limit = 120) {
  return useQuery({
    queryKey: queryKeys.telemetry(vehicleId ?? ""),
    queryFn: () => api.getVehicleTelemetry(vehicleId as string, { limit }),
    enabled: Boolean(vehicleId),
    refetchInterval: 10_000,
  });
}

export function useSimulatorStatus(options?: Partial<UseQueryOptions<Awaited<ReturnType<typeof api.getSimulatorStatus>>>>) {
  return useQuery({
    queryKey: queryKeys.simulationStatus,
    queryFn: api.getSimulatorStatus,
    refetchInterval: 3000,
    ...options,
  });
}

export function useSimulations() {
  return useQuery({ queryKey: queryKeys.simulations, queryFn: api.getSimulations });
}

export function useSimulationControl() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, action }: { id: string; action: Parameters<typeof api.simulationAction>[1] }) =>
      api.simulationAction(id, action),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["simulation"] });
      void client.invalidateQueries({ queryKey: queryKeys.liveVehicles });
    },
  });
}

export function useCreateSimulation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (request: SimulationCreateRequest) => api.createSimulation(request),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["simulation"] }),
  });
}

export function useMlModels() {
  return useQuery({ queryKey: queryKeys.mlModels, queryFn: api.getMlModels });
}

export function useMlMetrics(params: Parameters<typeof api.getMlMetrics>[0] = {}) {
  return useQuery({ queryKey: queryKeys.mlMetrics(params), queryFn: () => api.getMlMetrics(params) });
}

export function useDataQuality() {
  return useQuery({
    queryKey: queryKeys.dataQuality,
    queryFn: api.getDataQuality,
    refetchInterval: 60_000,
  });
}

export function useIngestionRuns(params: Parameters<typeof api.getIngestionRuns>[0] = {}) {
  return useQuery({
    queryKey: queryKeys.ingestions(params),
    queryFn: () => api.getIngestionRuns(params),
    placeholderData: (previous) => previous,
  });
}

export function useEnergyAnalytics(dimension: "temperature" | "speed" | "traffic") {
  return useQuery({
    queryKey: queryKeys.energyAnalytics(dimension),
    queryFn: () => api.getEnergyAnalytics(dimension),
    staleTime: 5 * 60_000,
  });
}

export function useRegionalAnalytics() {
  return useQuery({
    queryKey: queryKeys.regionalAnalytics,
    queryFn: api.getRegionalAnalytics,
    staleTime: 10 * 60_000,
  });
}
