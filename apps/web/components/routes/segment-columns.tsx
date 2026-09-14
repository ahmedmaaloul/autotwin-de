"use client";

import { createColumnHelper, type RowData } from "@tanstack/react-table";

import { ENERGY_LEVEL_VAR } from "@/lib/constants";
import type { Locale } from "@/lib/i18n/config";
import { formatNumber } from "@/lib/i18n/format";
import type { Messages } from "@/lib/i18n/messages/de";
import type { RouteSegmentAnalysis } from "@/types/domain";

import { intensityLabel, roadClassLabel, trafficSeverityLabel } from "./labels";

declare module "@tanstack/react-table" {
  /**
   * Right-alignment is a property of the column, not of each cell, so it is declared once on
   * the column definition and read by both the header and the body.
   */
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  interface ColumnMeta<TData extends RowData, TValue> {
    numeric?: boolean;
  }
}

const columnHelper = createColumnHelper<RouteSegmentAnalysis>();

/**
 * The nine columns of the segment table, in the order the drive happens.
 *
 * Column definitions are built per locale rather than declared once at module scope: every
 * header is a translation and every cell is a locale-formatted number, and a German reader must
 * see `18,4` where an English one sees `18.4`.
 */
export function buildSegmentColumns(t: Messages, locale: Locale) {
  const dash = "–";
  return [
    columnHelper.accessor("ordinal", {
      header: t.routes.segment,
      cell: (info) => (
        <span className="font-mono tabular-nums">{formatNumber(info.getValue() + 1, locale)}</span>
      ),
    }),
    columnHelper.accessor("start_offset_km", {
      header: t.units.km,
      meta: { numeric: true },
      cell: (info) => formatNumber(info.getValue(), locale, { decimals: 1 }),
    }),
    columnHelper.accessor("road_class", {
      header: t.routes.roadClass,
      cell: (info) => roadClassLabel(info.getValue(), t),
    }),
    columnHelper.accessor("speed_limit_kmh", {
      // The full "Zulässige Höchstgeschwindigkeit" would be wider than the column it heads.
      header: t.routes.speedLimitShort,
      meta: { numeric: true },
      cell: (info) => {
        const value = info.getValue();
        return value == null ? dash : formatNumber(value, locale);
      },
    }),
    columnHelper.accessor("assumed_speed_kmh", {
      header: t.routes.assumedSpeed,
      meta: { numeric: true },
      cell: (info) => formatNumber(info.getValue(), locale),
    }),
    columnHelper.accessor("temperature_c", {
      header: t.routes.temperature,
      meta: { numeric: true },
      cell: (info) => {
        const value = info.getValue();
        return value == null ? dash : formatNumber(value, locale, { decimals: 1 });
      },
    }),
    columnHelper.accessor("traffic_severity", {
      header: t.routes.trafficSeverity,
      cell: (info) => trafficSeverityLabel(info.getValue(), t) ?? dash,
    }),
    columnHelper.accessor("kwh_per_100km", {
      header: t.units.kwhPer100km,
      meta: { numeric: true },
      cell: (info) => (
        <IntensityCell
          value={info.getValue()}
          intensity={info.row.original.energy_intensity}
          locale={locale}
          t={t}
        />
      ),
    }),
    columnHelper.accessor("soc_at_end_percent", {
      header: t.routes.socShort,
      meta: { numeric: true },
      cell: (info) => formatNumber(info.getValue(), locale),
    }),
  ];
}

/** Colour plus the word, because the swatch alone would be the only channel. */
function IntensityCell({
  value,
  intensity,
  locale,
  t,
}: {
  value: number;
  intensity: RouteSegmentAnalysis["energy_intensity"];
  locale: Locale;
  t: Messages;
}) {
  return (
    <span className="inline-flex items-center justify-end gap-1.5">
      <span
        className="border-border/60 size-2 shrink-0 rounded-[1px] border"
        style={{ backgroundColor: ENERGY_LEVEL_VAR[intensity] }}
        aria-hidden
      />
      <span className="sr-only">{intensityLabel(intensity, t)}: </span>
      {formatNumber(value, locale, { decimals: 1 })}
    </span>
  );
}
