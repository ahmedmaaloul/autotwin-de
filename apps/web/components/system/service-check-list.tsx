"use client";

import { StatusBadge } from "@/components/shared/status-badge";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";

import { normaliseCheck, type ReadinessCheck } from "./use-system-health";

/**
 * The dependency checks behind `/ready`, one row each.
 *
 * A readiness probe that collapses to a single boolean tells an operator nothing: "not ready"
 * because Postgres is down and "not ready" because no model has been trained are different
 * mornings. So every check is listed by name, with whatever detail the backend attached.
 */
export function ServiceCheckList({ checks }: { checks: Record<string, ReadinessCheck> }) {
  const t = useTranslations();
  const locale = useLocale();

  const entries = Object.entries(checks).map(([name, value]) => normaliseCheck(name, value));

  if (entries.length === 0) {
    return <p className="text-muted-foreground px-4 py-3 text-xs">{t.common.noData}</p>;
  }

  return (
    <ul className="divide-border divide-y">
      {entries.map((check) => (
        <li key={check.name} className="flex items-center justify-between gap-4 px-4 py-2.5">
          <div className="min-w-0">
            <p className="truncate font-mono text-sm">{check.name}</p>
            {check.detail ? (
              <p className="text-muted-foreground truncate text-xs">{check.detail}</p>
            ) : null}
          </div>
          <div className="flex shrink-0 items-center gap-3">
            {check.latencyMs != null ? (
              <span className="text-muted-foreground font-mono text-xs tabular-nums">
                {formatNumber(check.latencyMs, locale, { decimals: 0 })}
                <span className="ml-1">ms</span>
              </span>
            ) : null}
            <StatusBadge
              status={check.ok == null ? "neutral" : check.ok ? "healthy" : "failed"}
              label={
                check.ok == null
                  ? t.common.unknown
                  : check.ok
                    ? t.system.checkOk
                    : t.system.checkFailed
              }
            />
          </div>
        </li>
      ))}
    </ul>
  );
}
