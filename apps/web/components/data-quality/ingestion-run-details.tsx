"use client";

import { AlertTriangle } from "lucide-react";

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
import type { IngestionRun } from "@/types/domain";

/**
 * What the quality gate actually did to this run.
 *
 * `rule_stats` is how many rows each rule rejected; the violation sample is the evidence. Both
 * are needed: the counts say whether a problem is systematic, the samples say what it looks like
 * — an ingestion log that reports only "312 rejected" cannot be acted on (BUILD_SPEC §6).
 */
export function IngestionRunDetails({ run }: { run: IngestionRun }) {
  const t = useTranslations();
  const locale = useLocale();

  const report = run.quality_report;
  const ruleStats = Object.entries(report?.rule_stats ?? {});
  const violations = report?.violations ?? [];

  return (
    <div className="bg-muted/40 space-y-4 px-4 py-3">
      {run.error_message ? (
        <div className="border-danger/30 bg-danger/5 flex items-start gap-2 rounded border px-3 py-2">
          <AlertTriangle className="text-danger mt-0.5 size-3.5 shrink-0" aria-hidden />
          <div className="min-w-0">
            <p className="eyebrow text-danger mb-0.5">{t.dataQuality.errorMessage}</p>
            <p className="font-mono text-xs break-words">{run.error_message}</p>
          </div>
        </div>
      ) : null}

      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <p className="eyebrow">{t.dataQuality.qualityReport}</p>
        {run.bytes_downloaded != null ? (
          <p className="text-muted-foreground text-xs">
            {t.dataQuality.bytes}{" "}
            <span className="text-foreground font-mono tabular-nums">
              {formatNumber(run.bytes_downloaded / 1_048_576, locale, { decimals: 1 })}
            </span>{" "}
            <span>MB</span>
          </p>
        ) : null}
      </div>

      {!report ? (
        <p className="text-muted-foreground text-xs">{t.dataQuality.noQualityReport}</p>
      ) : (
        <>
          <section>
            <p className="eyebrow mb-2">{t.dataQuality.ruleStats}</p>
            {ruleStats.length === 0 ? (
              <p className="text-muted-foreground text-xs">{t.dataQuality.noViolations}</p>
            ) : (
              <dl className="grid gap-x-6 gap-y-1.5 sm:grid-cols-2 lg:grid-cols-3">
                {ruleStats.map(([rule, count]) => (
                  <div
                    key={rule}
                    className="border-border flex items-baseline justify-between gap-3 border-b pb-1"
                  >
                    <dt className="text-muted-foreground truncate font-mono text-xs">{rule}</dt>
                    <dd
                      className={`font-mono text-xs tabular-nums ${count > 0 ? "text-danger" : "text-muted-foreground"}`}
                    >
                      {formatNumber(count, locale)}
                    </dd>
                  </div>
                ))}
              </dl>
            )}
          </section>

          {violations.length > 0 ? (
            <section>
              <div className="mb-2 flex items-baseline justify-between gap-3">
                <p className="eyebrow">{t.dataQuality.violationSample}</p>
                <span className="text-muted-foreground font-mono text-[0.6875rem] tabular-nums">
                  {formatNumber(violations.length, locale)} {t.dataQuality.violationCount}
                </span>
              </div>
              <div className="border-border bg-card max-h-56 overflow-auto rounded border">
                <Table>
                  <TableHeader className="bg-card sticky top-0 z-10">
                    <TableRow>
                      <TableHead>{t.dataQuality.rule}</TableHead>
                      <TableHead>{t.dataQuality.field}</TableHead>
                      <TableHead>{t.dataQuality.message}</TableHead>
                      <TableHead className="text-right">{t.dataQuality.rowIndex}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {violations.map((violation, index) => (
                      <TableRow key={`${violation.rule}-${violation.row_index ?? index}-${index}`}>
                        <TableCell className="font-mono text-xs">{violation.rule}</TableCell>
                        <TableCell className="text-muted-foreground font-mono text-xs">
                          {violation.field ?? "–"}
                        </TableCell>
                        <TableCell className="text-xs">{violation.message}</TableCell>
                        <TableCell className="text-right font-mono text-xs tabular-nums">
                          {violation.row_index == null
                            ? "–"
                            : formatNumber(violation.row_index, locale)}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
              {report.violations_truncated ? (
                <p className="text-muted-foreground mt-1.5 text-[0.6875rem]">
                  {t.dataQuality.violationsTruncated}
                </p>
              ) : null}
            </section>
          ) : (
            <p className="text-muted-foreground text-xs">{t.dataQuality.noViolations}</p>
          )}
        </>
      )}
    </div>
  );
}
