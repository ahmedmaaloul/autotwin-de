"use client";

import {
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type SortingState,
} from "@tanstack/react-table";
import { ArrowDown, ArrowUp, ChevronLeft, ChevronRight, ChevronsUpDown, Table2 } from "lucide-react";
import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";
import type { ChargingStationSummary, Page } from "@/types/domain";

import { chargingColumns } from "./charging-table-columns";
import { TABLE_PAGE_SIZE } from "./use-charging-filters";

/**
 * The tabular view of the current filter.
 *
 * Pagination is server-side — 90 000 rows are never in the browser — which is why the sort is
 * labelled as applying to the visible page only. Sorting a page and calling it a ranking would
 * be a lie the reader has no way to catch.
 */
export function ChargingTable({
  page,
  isLoading,
  isFetching,
  error,
  pageNumber,
  onPageChange,
  onSelect,
  onRetry,
}: {
  page: Page<ChargingStationSummary> | undefined;
  isLoading: boolean;
  isFetching: boolean;
  error: unknown;
  pageNumber: number;
  onPageChange: (next: number) => void;
  onSelect: (stationId: string) => void;
  onRetry: () => void;
}) {
  const t = useTranslations();
  const locale = useLocale();
  const [sorting, setSorting] = useState<SortingState>([]);

  const rows = useMemo(() => page?.items ?? [], [page]);
  const columns = useMemo(() => chargingColumns({ t, locale, onSelect }), [t, locale, onSelect]);

  const table = useReactTable({
    data: rows,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    manualPagination: true,
    pageCount: page ? Math.max(1, Math.ceil(page.total / (page.page_size || TABLE_PAGE_SIZE))) : -1,
  });

  if (error) return <ErrorState error={error} onRetry={onRetry} />;
  if (isLoading && !page) return <LoadingSkeleton rows={10} />;
  if (page && rows.length === 0) {
    return (
      <EmptyState
        icon={Table2}
        title={t.charging.noStationsTitle}
        description={t.charging.noStationsBody}
      />
    );
  }

  const total = page?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / (page?.page_size || TABLE_PAGE_SIZE)));

  return (
    <div className="space-y-3">
      <div
        className={cn(
          "border-border bg-card rounded border transition-opacity",
          isFetching && "opacity-60",
        )}
      >
        <Table className="text-xs">
          <TableHeader className="bg-card sticky top-0 z-10">
            {table.getHeaderGroups().map((headerGroup) => (
              <TableRow key={headerGroup.id} className="hover:bg-transparent">
                {headerGroup.headers.map((header) => {
                  const numeric = Boolean(
                    (header.column.columnDef.meta as { numeric?: boolean } | undefined)?.numeric,
                  );
                  const sorted = header.column.getIsSorted();
                  const SortIcon =
                    sorted === "asc" ? ArrowUp : sorted === "desc" ? ArrowDown : ChevronsUpDown;
                  return (
                    <TableHead
                      key={header.id}
                      className={cn("h-9 px-2", numeric && "text-right")}
                      aria-sort={
                        sorted === "asc"
                          ? "ascending"
                          : sorted === "desc"
                            ? "descending"
                            : "none"
                      }
                    >
                      <button
                        type="button"
                        onClick={header.column.getToggleSortingHandler()}
                        className={cn(
                          "font-condensed text-muted-foreground hover:text-foreground focus-visible:ring-ring inline-flex items-center gap-1 rounded-sm text-[0.6875rem] font-semibold tracking-[0.04em] uppercase focus-visible:ring-2 focus-visible:outline-none",
                          numeric && "flex-row-reverse",
                        )}
                      >
                        {flexRender(header.column.columnDef.header, header.getContext())}
                        <SortIcon
                          className={cn("size-3", sorted ? "opacity-100" : "opacity-35")}
                          aria-hidden
                        />
                      </button>
                    </TableHead>
                  );
                })}
              </TableRow>
            ))}
          </TableHeader>
          <TableBody>
            {table.getRowModel().rows.map((row) => (
              <TableRow key={row.id} className="border-border h-9">
                {row.getVisibleCells().map((cell) => {
                  const numeric = Boolean(
                    (cell.column.columnDef.meta as { numeric?: boolean } | undefined)?.numeric,
                  );
                  return (
                    <TableCell key={cell.id} className={cn("px-2 py-1.5", numeric && "text-right")}>
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </TableCell>
                  );
                })}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>

      <div className="text-muted-foreground flex flex-wrap items-center justify-between gap-3 text-xs">
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="decoration-muted-foreground/40 cursor-help underline decoration-dotted underline-offset-4">
              <span className="text-foreground font-mono tabular-nums">
                {formatNumber(total, locale)}
              </span>{" "}
              {t.charging.stations}
            </span>
          </TooltipTrigger>
          <TooltipContent className="max-w-xs">{t.charging.sortHint}</TooltipContent>
        </Tooltip>

        <div className="flex items-center gap-2">
          <span className="font-mono tabular-nums">
            {t.common.page} {formatNumber(pageNumber, locale)} {t.common.of}{" "}
            {formatNumber(pageCount, locale)}
          </span>
          <Button
            variant="outline"
            size="sm"
            className="h-7 px-2"
            disabled={pageNumber <= 1 || isFetching}
            onClick={() => onPageChange(pageNumber - 1)}
            aria-label={t.charging.previousPage}
          >
            <ChevronLeft className="size-3.5" aria-hidden />
          </Button>
          <Button
            variant="outline"
            size="sm"
            className="h-7 px-2"
            disabled={!page?.has_next || isFetching}
            onClick={() => onPageChange(pageNumber + 1)}
            aria-label={t.charging.nextPage}
          >
            <ChevronRight className="size-3.5" aria-hidden />
          </Button>
        </div>
      </div>
    </div>
  );
}
