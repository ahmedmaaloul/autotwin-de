"use client";

import { BarChart3, Map as MapIcon, Table2 } from "lucide-react";
import { useCallback, useMemo } from "react";

import { PageHeader } from "@/components/shared/section-header";
import { SourceBadge } from "@/components/shared/source-badge";
import { DataModeNotice } from "@/components/shared/states";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  useChargingGeoJson,
  useChargingStation,
  useChargingStations,
  useChargingStatistics,
} from "@/hooks/use-autotwin";
import { useTranslations } from "@/providers/locale-provider";

import { ChargingCharts } from "./charging-charts";
import { ChargingFilters } from "./charging-filters";
import { ChargingMap } from "./charging-map";
import { ChargingMetrics } from "./charging-metrics";
import { ChargingTable } from "./charging-table";
import { StationDetail } from "./station-detail";
import {
  FALLBACK_YEAR_RANGE,
  toApiFilters,
  toGeoJsonFilters,
  useChargingFilters,
  type ChargingTab,
} from "./use-charging-filters";

/**
 * The Ladeinfrastruktur explorer.
 *
 * One filter, three readings of it: the map answers *where*, the table answers *which exactly*,
 * and the analysis answers *how the stock is shaped*. Tabs rather than one long page because
 * ~90 000 sites deserve a full-height map and a full-width table, and stacking both turns the
 * screen into a scroll marathon where neither is usable.
 *
 * Filters, active tab and the open station all live in the URL, so any state this page can be
 * in is a link someone else can open.
 */
export function ChargingPage() {
  const t = useTranslations();
  const { filters, setFilters, resetFilters, isFiltered } = useChargingFilters();

  const apiFilters = useMemo(() => toApiFilters(filters), [filters]);
  const geoFilters = useMemo(() => toGeoJsonFilters(filters), [filters]);

  const stations = useChargingStations(apiFilters);
  const geojson = useChargingGeoJson(geoFilters, filters.tab === "map");
  const statistics = useChargingStatistics();
  const station = useChargingStation(filters.stationId);

  const selectStation = useCallback(
    (stationId: string) => setFilters({ stationId }),
    [setFilters],
  );
  const closeStation = useCallback(() => setFilters({ stationId: null }), [setFilters]);

  const operators = useMemo(
    () =>
      [...(statistics.data?.by_operator ?? [])].sort((a, b) => b.stations - a.stations),
    [statistics.data],
  );

  const yearBounds = useMemo<readonly [number, number]>(() => {
    const years = statistics.data?.growth.map((entry) => entry.year) ?? [];
    if (years.length === 0) return FALLBACK_YEAR_RANGE;
    return [Math.min(...years), Math.max(...years)];
  }, [statistics.data]);

  return (
    <div className="space-y-6">
      <PageHeader
        title={t.charging.title}
        description={t.charging.subtitle}
        actions={<SourceBadge origin="official" source="bundesnetzagentur" />}
      />

      <DataModeNotice mode={stations.data?.dataMode} />

      <ChargingMetrics
        matches={stations.data?.data.total ?? null}
        matchesLoading={stations.isPending}
        isFiltered={isFiltered}
        statistics={statistics.data}
        statisticsLoading={statistics.isPending}
      />

      <ChargingFilters
        controller={{ filters, setFilters, resetFilters, isFiltered }}
        operators={operators}
        operatorsLoading={statistics.isPending}
        yearBounds={yearBounds}
      />

      <Tabs
        value={filters.tab}
        onValueChange={(tab) => setFilters({ tab: tab as ChargingTab })}
        className="gap-4"
      >
        <TabsList variant="line" className="h-9">
          <TabsTrigger value="map" className="px-3">
            <MapIcon aria-hidden />
            {t.charging.tabMap}
          </TabsTrigger>
          <TabsTrigger value="table" className="px-3">
            <Table2 aria-hidden />
            {t.charging.tabTable}
          </TabsTrigger>
          <TabsTrigger value="charts" className="px-3">
            <BarChart3 aria-hidden />
            {t.charging.tabCharts}
          </TabsTrigger>
        </TabsList>

        <TabsContent value="map">
          <ChargingMap
            data={geojson.data}
            isLoading={geojson.isPending}
            error={geojson.error}
            selectedId={filters.stationId}
            onSelect={selectStation}
            onRetry={() => void geojson.refetch()}
          />
        </TabsContent>

        <TabsContent value="table">
          <ChargingTable
            page={stations.data?.data}
            isLoading={stations.isPending}
            isFetching={stations.isFetching}
            error={stations.error}
            pageNumber={filters.page}
            onPageChange={(page) => setFilters({ page })}
            onSelect={selectStation}
            onRetry={() => void stations.refetch()}
          />
        </TabsContent>

        <TabsContent value="charts">
          <ChargingCharts
            statistics={statistics.data}
            isLoading={statistics.isPending}
            error={statistics.error}
            onRetry={() => void statistics.refetch()}
          />
        </TabsContent>
      </Tabs>

      <StationDetail
        stationId={filters.stationId}
        station={station.data}
        isLoading={station.isPending}
        error={station.error}
        onClose={closeStation}
        onRetry={() => void station.refetch()}
      />
    </div>
  );
}
