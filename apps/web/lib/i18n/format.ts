import { LOCALE_TAGS, type Locale } from "./config";

/**
 * Number and date formatting.
 *
 * Every figure in AutoTwin DE is a measurement, so formatting is part of the data contract:
 * German uses a comma as the decimal separator and a dot as the thousands separator, and
 * getting that wrong makes `1.234` mean two different things in two languages.
 *
 * `Intl` formatters are expensive to construct and are used on every row of every table, so
 * they are memoised.
 */
const numberFormatters = new Map<string, Intl.NumberFormat>();

function numberFormatter(locale: Locale, options: Intl.NumberFormatOptions): Intl.NumberFormat {
  const key = `${locale}:${JSON.stringify(options)}`;
  let formatter = numberFormatters.get(key);
  if (!formatter) {
    formatter = new Intl.NumberFormat(LOCALE_TAGS[locale], options);
    numberFormatters.set(key, formatter);
  }
  return formatter;
}

export function formatNumber(
  value: number | null | undefined,
  locale: Locale,
  { decimals = 0, compact = false }: { decimals?: number; compact?: boolean } = {},
): string {
  if (value == null || !Number.isFinite(value)) return "–";
  return numberFormatter(locale, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
    notation: compact ? "compact" : "standard",
  }).format(value);
}

export function formatPercent(
  value: number | null | undefined,
  locale: Locale,
  decimals = 1,
): string {
  if (value == null || !Number.isFinite(value)) return "–";
  return `${formatNumber(value, locale, { decimals })} %`;
}

/** Signed percentage — used for the weather/traffic penalties, where the sign is the message. */
export function formatDelta(
  value: number | null | undefined,
  locale: Locale,
  decimals = 1,
): string {
  if (value == null || !Number.isFinite(value)) return "–";
  const sign = value > 0 ? "+" : "";
  return `${sign}${formatNumber(value, locale, { decimals })} %`;
}

export function formatDistanceKm(
  metresOrKm: number | null | undefined,
  locale: Locale,
  { fromMetres = false }: { fromMetres?: boolean } = {},
): string {
  if (metresOrKm == null || !Number.isFinite(metresOrKm)) return "–";
  const km = fromMetres ? metresOrKm / 1000 : metresOrKm;
  return formatNumber(km, locale, { decimals: km < 10 ? 1 : 0 });
}

/** `2h 19m` / `47 min` — the format a driver reads, not raw seconds. */
export function formatDuration(seconds: number | null | undefined, locale: Locale): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return "–";
  const total = Math.round(seconds / 60);
  const hours = Math.floor(total / 60);
  const minutes = total % 60;
  if (hours === 0) return `${formatNumber(minutes, locale)} min`;
  return `${formatNumber(hours, locale)} h ${String(minutes).padStart(2, "0")} min`;
}

export function formatDateTime(
  value: string | Date | null | undefined,
  locale: Locale,
  options: Intl.DateTimeFormatOptions = { dateStyle: "medium", timeStyle: "short" },
): string {
  if (!value) return "–";
  const date = typeof value === "string" ? new Date(value) : value;
  if (Number.isNaN(date.getTime())) return "–";
  return new Intl.DateTimeFormat(LOCALE_TAGS[locale], options).format(date);
}

/**
 * Coarse relative age — `vor 18 Min.` / `18 min ago`.
 *
 * Deliberately coarse: data freshness is an at-a-glance signal, and second-level precision on
 * a source that updates every ten minutes would be false precision.
 */
export function formatRelativeAge(
  value: string | Date | null | undefined,
  locale: Locale,
  now: Date = new Date(),
): string | null {
  if (!value) return null;
  const date = typeof value === "string" ? new Date(value) : value;
  if (Number.isNaN(date.getTime())) return null;

  const seconds = Math.round((now.getTime() - date.getTime()) / 1000);
  const rtf = new Intl.RelativeTimeFormat(LOCALE_TAGS[locale], { numeric: "auto", style: "short" });

  if (Math.abs(seconds) < 60) return rtf.format(-seconds, "second");
  const minutes = Math.round(seconds / 60);
  if (Math.abs(minutes) < 60) return rtf.format(-minutes, "minute");
  const hours = Math.round(minutes / 60);
  if (Math.abs(hours) < 24) return rtf.format(-hours, "hour");
  return rtf.format(-Math.round(hours / 24), "day");
}

/** `50,1109° N · 8,6821° O` — coordinates read as coordinates, not as two floats. */
export function formatCoordinate(
  latitude: number,
  longitude: number,
  locale: Locale,
): string {
  const ns = latitude >= 0 ? "N" : "S";
  const ew = longitude >= 0 ? (locale === "de" ? "O" : "E") : "W";
  const lat = formatNumber(Math.abs(latitude), locale, { decimals: 4 });
  const lon = formatNumber(Math.abs(longitude), locale, { decimals: 4 });
  return `${lat}° ${ns} · ${lon}° ${ew}`;
}
