"use client";

import {
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type SortingState,
} from "@tanstack/react-table";
import { ArrowDown, ArrowUp, ChevronsUpDown } from "lucide-react";
import { useMemo, useState } from "react";

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";
import type { RouteSegmentAnalysis } from "@/types/domain";

import { buildSegmentColumns } from "./segment-columns";

/**
 * Every segment, and every assumption behind its number.
 *
 * The point of the table is auditability: the Streckenband shows *that* kilometre 84 is
 * expensive, this shows *why* — the road class, the speed the model assumed, the temperature it
 * used and the traffic it found. Sorting by `kwh_per_100km` answers "where does the energy
 * actually go?" in one click, which is the question the whole page exists for.
 *
 * A row is a selection, shared with the map and the strip, so clicking a suspicious number puts
 * that stretch of Autobahn on screen.
 */
export function SegmentTable({
  segments,
  selectedOrdinal,
  onSelectSegment,
  className,
}: {
  segments: RouteSegmentAnalysis[];
  selectedOrdinal: number | null;
  onSelectSegment: (ordinal: number | null) => void;
  className?: string;
}) {
  const t = useTranslations();
  const locale = useLocale();
  const [sorting, setSorting] = useState<SortingState>([]);

  const columns = useMemo(() => buildSegmentColumns(t, locale), [t, locale]);

  const table = useReactTable({
    data: segments,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getRowId: (row) => String(row.ordinal),
  });

  return (
    <div className={cn("scrollbar-thin max-h-[26rem] overflow-auto", className)}>
      <Table>
        <TableHeader className="bg-card sticky top-0 z-10">
          {table.getHeaderGroups().map((headerGroup) => (
            <TableRow key={headerGroup.id} className="hover:bg-transparent">
              {headerGroup.headers.map((header) => {
                const sorted = header.column.getIsSorted();
                const numeric = header.column.columnDef.meta?.numeric ?? false;
                return (
                  <TableHead
                    key={header.id}
                    aria-sort={
                      sorted === "asc"
                        ? "ascending"
                        : sorted === "desc"
                          ? "descending"
                          : "none"
                    }
                    className={cn(
                      "font-[family-name:var(--font-condensed)] text-[0.6875rem] tracking-wide uppercase",
                      numeric && "text-right",
                    )}
                  >
                    <button
                      type="button"
                      onClick={header.column.getToggleSortingHandler()}
                      title={t.routes.sortToggle}
                      className={cn(
                        "focus-visible:ring-ring inline-flex items-center gap-1 rounded focus-visible:ring-2 focus-visible:outline-none",
                        numeric && "flex-row-reverse",
                      )}
                    >
                      {flexRender(header.column.columnDef.header, header.getContext())}
                      {sorted === "asc" ? (
                        <ArrowUp className="size-3" aria-hidden />
                      ) : sorted === "desc" ? (
                        <ArrowDown className="size-3" aria-hidden />
                      ) : (
                        <ChevronsUpDown className="size-3 opacity-40" aria-hidden />
                      )}
                    </button>
                  </TableHead>
                );
              })}
            </TableRow>
          ))}
        </TableHeader>

        <TableBody>
          {table.getRowModel().rows.map((row) => {
            const selected = row.original.ordinal === selectedOrdinal;
            const toggle = () => onSelectSegment(selected ? null : row.original.ordinal);
            return (
              <TableRow
                key={row.id}
                tabIndex={0}
                aria-selected={selected}
                data-state={selected ? "selected" : undefined}
                onClick={toggle}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    toggle();
                  }
                }}
                className={cn(
                  "focus-visible:ring-ring cursor-pointer focus-visible:ring-2 focus-visible:outline-none",
                  selected && "bg-primary/10 hover:bg-primary/10",
                )}
              >
                {row.getVisibleCells().map((cell) => (
                  <TableCell
                    key={cell.id}
                    className={cn(
                      "h-9 py-1",
                      cell.column.columnDef.meta?.numeric && "text-right font-mono tabular-nums",
                    )}
                  >
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </TableCell>
                ))}
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </div>
  );
}
