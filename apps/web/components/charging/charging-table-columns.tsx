"use client";

import { createColumnHelper, type ColumnDef } from "@tanstack/react-table";

import { BUNDESLAENDER } from "@/lib/constants";
import { formatDateTime, formatNumber } from "@/lib/i18n/format";
import type { Locale } from "@/lib/i18n/config";
import type { Messages } from "@/lib/i18n/messages/de";
import type { ChargingStationSummary } from "@/types/domain";

import { CategoryBadge, Measure } from "./charging-vocabulary";

const helper = createColumnHelper<ChargingStationSummary>();

/** `HE` → `Hessen`. The register stores the code; a reader wants the name. */
function bundeslandName(code: string | null): string | null {
  if (!code) return null;
  return BUNDESLAENDER.find((entry) => entry.code === code)?.name ?? code;
}

export function stationLabel(station: ChargingStationSummary, t: Messages): string {
  const street = [station.street, station.house_number].filter(Boolean).join(" ");
  return street || station.operator || station.city || t.charging.station;
}

/**
 * The table's columns — a deliberate subset.
 *
 * `charging_stations` has far more fields than this. A table that shows every column is a
 * database viewer; these seven are the ones an infrastructure planner scans for, and anything
 * else belongs in the detail drawer where there is room to explain it.
 */
export function chargingColumns({
  t,
  locale,
  onSelect,
}: {
  t: Messages;
  locale: Locale;
  onSelect: (stationId: string) => void;
}): ColumnDef<ChargingStationSummary, unknown>[] {
  return [
    helper.accessor((row) => stationLabel(row, t), {
      id: "station",
      header: t.charging.station,
      cell: (context) => {
        const station = context.row.original;
        return (
          <button
            type="button"
            onClick={() => onSelect(station.id)}
            className="focus-visible:ring-ring hover:text-primary block max-w-[18rem] truncate rounded-sm text-left font-medium focus-visible:ring-2 focus-visible:outline-none"
            aria-label={`${t.charging.selectStation}: ${context.getValue<string>()}`}
          >
            {context.getValue<string>()}
          </button>
        );
      },
    }),
    helper.accessor((row) => row.operator ?? "", {
      id: "operator",
      header: t.charging.operator,
      cell: (context) => (
        <span className="text-muted-foreground block max-w-[14rem] truncate">
          {context.getValue<string>() || "–"}
        </span>
      ),
    }),
    helper.accessor((row) => row.city ?? "", {
      id: "city",
      header: t.charging.city,
      cell: (context) => {
        const { postal_code: postalCode } = context.row.original;
        return (
          <span className="flex items-baseline gap-1.5 whitespace-nowrap">
            {postalCode ? (
              <span className="text-muted-foreground font-mono text-[0.75rem] tabular-nums">
                {postalCode}
              </span>
            ) : null}
            <span className="truncate">{context.getValue<string>() || "–"}</span>
          </span>
        );
      },
    }),
    helper.accessor((row) => row.charging_points_count, {
      id: "points",
      header: t.charging.chargingPoints,
      meta: { numeric: true },
      cell: (context) => (
        <Measure value={formatNumber(context.getValue<number>(), locale)} />
      ),
    }),
    helper.accessor((row) => row.max_power_kw ?? 0, {
      id: "power",
      header: t.charging.power,
      meta: { numeric: true },
      cell: (context) => {
        const value = context.row.original.max_power_kw;
        return value == null ? (
          <span className="text-muted-foreground">–</span>
        ) : (
          <Measure value={formatNumber(value, locale)} unit={t.units.kw} />
        );
      },
    }),
    helper.accessor((row) => row.charging_category, {
      id: "category",
      header: t.charging.category,
      cell: (context) => <CategoryBadge category={context.row.original.charging_category} />,
    }),
    helper.accessor((row) => bundeslandName(row.bundesland) ?? "", {
      id: "bundesland",
      header: t.charging.bundesland,
      cell: (context) => (
        <span className="whitespace-nowrap">{context.getValue<string>() || "–"}</span>
      ),
    }),
    helper.accessor((row) => row.commissioned_on ?? "", {
      id: "commissioned",
      header: t.charging.commissionedOn,
      meta: { numeric: true },
      cell: (context) => {
        const value = context.getValue<string>();
        return (
          <span className="text-muted-foreground font-mono text-[0.75rem] whitespace-nowrap tabular-nums">
            {value ? formatDateTime(value, locale, { dateStyle: "medium" }) : "–"}
          </span>
        );
      },
    }),
  ] as ColumnDef<ChargingStationSummary, unknown>[];
}
