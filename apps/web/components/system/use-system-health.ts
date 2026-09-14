"use client";

import { useQuery } from "@tanstack/react-query";

import { apiFetchRoot } from "@/lib/api/client";

/**
 * The operational endpoints.
 *
 * `/health`, `/ready` and `/metrics` deliberately sit outside the versioned API — they are
 * infrastructure, not product surface (BUILD_SPEC §7) — so they are reached through
 * `apiFetchRoot` and get their own query keys rather than joining the `/api/v1` cache.
 *
 * `retry: false` is the point of a status page: a readiness probe that quietly retries three
 * times before admitting failure is a status page that lies for ten seconds.
 */

export interface HealthResponse {
  status: string;
  version: string;
  uptime_s: number;
}

/** The shape of an individual check is not fixed by the contract, so accept all of them. */
export type ReadinessCheck =
  | boolean
  | string
  | null
  | {
      status?: string | boolean | null;
      ok?: boolean | null;
      healthy?: boolean | null;
      detail?: string | null;
      message?: string | null;
      latency_ms?: number | null;
    };

export interface ReadinessResponse {
  status: string;
  checks?: Record<string, ReadinessCheck>;
}

const OK_WORDS = new Set(["ok", "ready", "up", "healthy", "connected", "available", "true"]);

export interface NormalisedCheck {
  name: string;
  ok: boolean | null;
  detail: string | null;
  latencyMs: number | null;
}

export function normaliseCheck(name: string, value: ReadinessCheck): NormalisedCheck {
  if (typeof value === "boolean") return { name, ok: value, detail: null, latencyMs: null };
  if (typeof value === "string") {
    return { name, ok: OK_WORDS.has(value.toLowerCase()), detail: value, latencyMs: null };
  }
  if (value && typeof value === "object") {
    const status = value.status;
    const ok =
      typeof value.ok === "boolean"
        ? value.ok
        : typeof value.healthy === "boolean"
          ? value.healthy
          : typeof status === "boolean"
            ? status
            : typeof status === "string"
              ? OK_WORDS.has(status.toLowerCase())
              : null;
    return {
      name,
      ok,
      detail: value.detail ?? value.message ?? (typeof status === "string" ? status : null),
      latencyMs: value.latency_ms ?? null,
    };
  }
  return { name, ok: null, detail: null, latencyMs: null };
}

export function useServiceHealth() {
  return useQuery({
    queryKey: ["system", "health"] as const,
    queryFn: () => apiFetchRoot<HealthResponse>("/health", { timeoutMs: 5000 }),
    refetchInterval: 30_000,
    retry: false,
  });
}

export function useReadiness() {
  return useQuery({
    queryKey: ["system", "ready"] as const,
    queryFn: () => apiFetchRoot<ReadinessResponse>("/ready", { timeoutMs: 5000 }),
    refetchInterval: 15_000,
    retry: false,
  });
}
