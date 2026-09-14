"use client";

import { ArrowDown, ArrowUp, ChevronsUpDown } from "lucide-react";
import { useMemo } from "react";

import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";

import {
  metricDecimals,
  metricLabel,
  REGION_METRICS,
  type RegionRow,
  type RegionSortKey,
} from "./region-metrics";

export type SortDirection = "asc" | "desc";

/**
 * The same sixteen rows as the chart, as numbers.
 *
 * Sorting is shared state with the chart: clicking a column header re-ranks the table *and*
 * switches the chart to that metric, so the two halves of the panel can never show different
 * questions. `aria-sort` carries the same information to a screen reader.
 */
export function RegionalTable({
  rows,
  sortKey,
  direction,
  onSort,
}: {
  rows: RegionRow[];
  sortKey: RegionSortKey;
  direction: SortDirection;
  onSort: (key: RegionSortKey) => void;
}) {
  const t = useTranslations();
  const locale = useLocale();

  const sorted = useMemo(() => {
    const factor = direction === "asc" ? 1 : -1;
    return rows.slice().sort((a, b) => {
      if (sortKey === "bundesland") return factor * a.bundesland.localeCompare(b.bundesland, locale);
      return factor * (a[sortKey] - b[sortKey]);
    });
  }, [rows, sortKey, direction, locale]);

  const totals = useMemo(
    () =>
      rows.reduce(
        (sum, row) => ({
          stations: sum.stations + row.stations,
          fast_points: sum.fast_points + row.fast_points,
          total_kw: sum.total_kw + row.total_kw,
        }),
        { stations: 0, fast_points: 0, total_kw: 0 },
      ),
    [rows],
  );

  return (
    <div className="overflow-x-auto">
      <Table>
        <TableHeader>
          <TableRow>
            <SortableHead
              label={t.analytics.bundesland}
              sortKey="bundesland"
              activeKey={sortKey}
              direction={direction}
              onSort={onSort}
              align="left"
            />
            {REGION_METRICS.map((metric) => (
              <SortableHead
                key={metric}
                label={metricLabel(metric, t)}
                sortKey={metric}
                activeKey={sortKey}
                direction={direction}
                onSort={onSort}
                align="right"
              />
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {sorted.map((row) => (
            <TableRow key={row.bundesland}>
              <TableCell className="font-medium whitespace-nowrap">{row.bundesland}</TableCell>
              {REGION_METRICS.map((metric) => (
                <TableCell
                  key={metric}
                  className={cn(
                    "text-right font-mono tabular-nums",
                    sortKey === metric && "text-foreground font-medium",
                  )}
                >
                  {formatNumber(row[metric], locale, { decimals: metricDecimals(metric) })}
                </TableCell>
              ))}
            </TableRow>
          ))}
          <TableRow className="border-border border-t-2 hover:bg-transparent">
            <TableCell className="eyebrow">{t.common.all}</TableCell>
            <TableCell className="text-right font-mono font-medium tabular-nums">
              {formatNumber(totals.stations, locale)}
            </TableCell>
            <TableCell className="text-right font-mono font-medium tabular-nums">
              {formatNumber(totals.fast_points, locale)}
            </TableCell>
            <TableCell className="text-right font-mono font-medium tabular-nums">
              {formatNumber(totals.total_kw, locale)}
            </TableCell>
            <TableCell className="text-muted-foreground text-right">—</TableCell>
          </TableRow>
        </TableBody>
      </Table>
    </div>
  );
}

function SortableHead({
  label,
  sortKey,
  activeKey,
  direction,
  onSort,
  align,
}: {
  label: string;
  sortKey: RegionSortKey;
  activeKey: RegionSortKey;
  direction: SortDirection;
  onSort: (key: RegionSortKey) => void;
  align: "left" | "right";
}) {
  const t = useTranslations();
  const isActive = activeKey === sortKey;
  const Icon = !isActive ? ChevronsUpDown : direction === "asc" ? ArrowUp : ArrowDown;

  return (
    <TableHead
      aria-sort={isActive ? (direction === "asc" ? "ascending" : "descending") : "none"}
      className={align === "right" ? "text-right" : undefined}
    >
      <Button
        type="button"
        variant="ghost"
        size="sm"
        onClick={() => onSort(sortKey)}
        aria-label={
          isActive && direction === "desc" ? t.analytics.sortAscending : t.analytics.sortDescending
        }
        className={cn(
          "text-muted-foreground -mx-2 h-7 gap-1 px-2 font-normal",
          isActive && "text-foreground",
          align === "right" && "ml-auto flex-row-reverse",
        )}
      >
        {label}
        <Icon className={cn("size-3", !isActive && "opacity-40")} aria-hidden />
      </Button>
    </TableHead>
  );
}
