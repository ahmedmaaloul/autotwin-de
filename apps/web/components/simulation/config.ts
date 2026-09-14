import type { SimulationCreateRequest } from "@/lib/api/endpoints";

/**
 * The shape of the form on `/simulation`, plus the translation into the API request.
 *
 * Kept out of the components so the normalisation of the vehicle mix — the one piece of real
 * logic in the form — is testable and stated once: the sliders carry arbitrary weights, the
 * API is sent proportions that sum to one.
 */

export const WEATHER_MODES = ["live", "clear", "rain", "snow", "storm"] as const;
export type WeatherMode = (typeof WEATHER_MODES)[number];

export const TRAFFIC_LEVELS = ["live", "low", "moderate", "high", "severe"] as const;
export type TrafficLevel = (typeof TRAFFIC_LEVELS)[number];

export const VEHICLE_COUNT_RANGE = { min: 1, max: 500, step: 1 } as const;
export const SPEED_FACTOR_RANGE = { min: 1, max: 60, step: 1 } as const;

/** Slider weight used for a vehicle class the operator has not touched yet. */
export const DEFAULT_MIX_WEIGHT = 20;

export interface SimulationConfig {
  vehicleCount: number;
  speedFactor: number;
  routeSlugs: string[];
  weatherMode: WeatherMode;
  trafficIntensity: TrafficLevel;
  /** Vehicle-profile code → relative weight. Normalised on submit. */
  vehicleMix: Record<string, number>;
  seed: number;
}

/**
 * A fixed seed rather than a random one: the default run has to be reproducible, and a random
 * value in `useState` would differ between the server render and hydration.
 */
export const DEFAULT_CONFIG: SimulationConfig = {
  vehicleCount: 25,
  speedFactor: 1,
  routeSlugs: [],
  weatherMode: "live",
  trafficIntensity: "live",
  vehicleMix: {},
  seed: 42,
};

export function randomSeed(): number {
  return Math.floor(Math.random() * 1_000_000);
}

export function mixWeight(config: SimulationConfig, code: string): number {
  return config.vehicleMix[code] ?? DEFAULT_MIX_WEIGHT;
}

/** Weights → shares in percent, so the form can show what it will actually send. */
export function mixShares(
  config: SimulationConfig,
  codes: string[],
): Record<string, number> {
  const total = codes.reduce((sum, code) => sum + mixWeight(config, code), 0);
  if (total <= 0) return Object.fromEntries(codes.map((code) => [code, 0]));
  return Object.fromEntries(
    codes.map((code) => [code, (mixWeight(config, code) / total) * 100]),
  );
}

export function toCreateRequest(
  config: SimulationConfig,
  { name, vehicleCodes }: { name: string; vehicleCodes: string[] },
): SimulationCreateRequest {
  const shares = mixShares(config, vehicleCodes);
  const total = Object.values(shares).reduce((sum, share) => sum + share, 0);

  return {
    name,
    vehicle_count: config.vehicleCount,
    speed_factor: config.speedFactor,
    seed: config.seed,
    route_slugs: config.routeSlugs.length > 0 ? config.routeSlugs : undefined,
    weather_mode: config.weatherMode,
    traffic_intensity: config.trafficIntensity,
    vehicle_mix:
      total > 0
        ? Object.fromEntries(
            Object.entries(shares)
              .filter(([, share]) => share > 0)
              .map(([code, share]) => [code, Number((share / 100).toFixed(4))]),
          )
        : undefined,
  };
}
