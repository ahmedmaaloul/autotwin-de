"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";

import { ApiError } from "@/lib/api/client";

/**
 * TanStack Query is the only place server state lives. Components never call `fetch`.
 *
 * The defaults below are tuned for an engineering dashboard rather than a content site:
 * data is measured, so a stale reading is misleading rather than merely old, but refetching
 * a 90 000-row charging dataset on every window focus would be wasteful. Per-hook overrides
 * tighten this where freshness actually matters (telemetry, simulation status).
 */
function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        gcTime: 5 * 60_000,
        refetchOnWindowFocus: false,
        retry: (failureCount, error) => {
          // A 4xx will not fix itself by asking again; a 5xx or a network blip might.
          if (error instanceof ApiError && error.status < 500 && error.status !== 429) return false;
          return failureCount < 2;
        },
        retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 8000),
      },
      mutations: { retry: false },
    },
  });
}

export function QueryProvider({ children }: { children: ReactNode }) {
  // useState so the client is created once per browser session, never on every render, and
  // never shared between users during SSR.
  const [client] = useState(createQueryClient);
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
