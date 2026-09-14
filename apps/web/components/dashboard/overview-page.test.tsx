import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { ApiError } from "@/lib/api/client";
import { LocaleProvider } from "@/providers/locale-provider";
import type { DashboardSummary } from "@/types/domain";

const summaryQuery = vi.hoisted(() => ({ current: {} as Record<string, unknown> }));

vi.mock("@/hooks/use-autotwin", () => ({
  useDashboardSummary: () => summaryQuery.current,
  useChargingGeoJson: () => ({ data: undefined, isLoading: false, error: null, refetch: vi.fn() }),
  useTrafficEvents: () => ({ data: undefined, isLoading: false, error: null, refetch: vi.fn() }),
  useRoutes: () => ({ data: undefined, isLoading: false }),
}));

vi.mock("@/hooks/use-telemetry-stream", () => ({
  useTelemetryStream: () => ({ vehicles: [], status: "closed", eventsReceived: 0 }),
}));

const { OverviewPage } = await import("./overview-page");

function renderPage(): void {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <LocaleProvider locale="de">
      <TooltipProvider>{children}</TooltipProvider>
    </LocaleProvider>
  );
  render(<OverviewPage />, { wrapper });
}

const EMPTY_SUMMARY: DashboardSummary = {
  vehicles_active: 0,
  charging_stations_total: 0,
  fast_charging_points_total: 0,
  traffic_events_active: 0,
  avg_consumption_kwh_100km: null,
  data_freshness: [],
  energy_trend: [],
  recent_traffic: [],
  weather_snapshot: null,
  charging_by_category: [],
};

describe("OverviewPage", () => {
  beforeEach(() => {
    summaryQuery.current = {};
  });

  it("shows the error state when the API is unavailable", () => {
    summaryQuery.current = {
      data: undefined,
      isLoading: false,
      isError: true,
      error: new ApiError(500, "http_500", "500 Internal Server Error"),
      refetch: vi.fn(),
    };
    renderPage();
    expect(screen.getByText("Daten konnten nicht geladen werden")).toBeInTheDocument();
    expect(screen.queryByText("Karte")).not.toBeInTheDocument();
  });

  it("shows the offline state when the backend cannot be reached at all", () => {
    summaryQuery.current = {
      data: undefined,
      isLoading: false,
      isError: true,
      error: new ApiError(0, "network_error", "unreachable"),
      refetch: vi.fn(),
    };
    renderPage();
    expect(screen.getByText("Backend nicht erreichbar")).toBeInTheDocument();
  });

  it("tells the reader to run `make demo` when the database is empty", () => {
    summaryQuery.current = {
      data: { data: EMPTY_SUMMARY, dataMode: "live", requestId: null },
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    };
    renderPage();
    expect(screen.getByText("Die Datenbank ist noch leer")).toBeInTheDocument();
    expect(screen.getByText("make demo")).toBeInTheDocument();
  });

  it("renders the six metrics with grouped German numerals once data arrives", () => {
    summaryQuery.current = {
      data: {
        data: {
          ...EMPTY_SUMMARY,
          vehicles_active: 312,
          charging_stations_total: 80_432,
          fast_charging_points_total: 24_118,
          traffic_events_active: 96,
          avg_consumption_kwh_100km: 18.74,
          data_freshness: [
            {
              source: "bundesnetzagentur",
              last_run_at: "2026-09-14T08:00:00Z",
              status: "healthy",
              age_minutes: 42,
              data_origin: "official",
              mode: "live",
            },
          ],
        } satisfies DashboardSummary,
        dataMode: "live",
        requestId: null,
      },
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    };
    renderPage();
    expect(screen.getByText("80.432")).toBeInTheDocument();
    expect(screen.getByText("24.118")).toBeInTheDocument();
    expect(screen.getByText("18,7")).toBeInTheDocument();
    // Datenaktualität reports the oldest source, in minutes below two hours.
    expect(screen.getByText("42")).toBeInTheDocument();
  });

  it("warns when the API answered from cache instead of the live source", () => {
    summaryQuery.current = {
      data: { data: { ...EMPTY_SUMMARY, charging_stations_total: 5 }, dataMode: "cache", requestId: null },
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    };
    renderPage();
    expect(screen.getByText("Live-Quelle nicht verfügbar")).toBeInTheDocument();
  });
});
