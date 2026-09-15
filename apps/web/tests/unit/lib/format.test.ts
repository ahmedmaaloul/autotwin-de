import { afterEach, describe, expect, it, vi } from "vitest";

import type { Locale } from "@/lib/i18n/config";
import {
  formatCoordinate,
  formatDateTime,
  formatDelta,
  formatDistanceKm,
  formatDuration,
  formatNumber,
  formatPercent,
  formatRelativeAge,
} from "@/lib/i18n/format";

/**
 * Number formatting is a correctness concern, not a cosmetic one.
 *
 * German writes `1.234,5` where English writes `1,234.5`. The two strings are each other's
 * mirror image, which means a formatter that silently falls back to the wrong locale does not
 * produce a garbled string — it produces a *plausible* string that is off by a factor of a
 * thousand. Every assertion below is derived from the published convention (DIN 5008 / CLDR)
 * or from arithmetic done by hand in the comment, never from what the module happened to
 * return when the test was written.
 */

/** U+2013 EN DASH. The "no value" mark; deliberately never the string "NaN" or an empty node. */
const PLACEHOLDER = "–";

/** U+00A0 NO-BREAK SPACE — German compact notation never breaks between number and "Mio.". */
const NBSP = " ";

describe("formatNumber", () => {
  it.each<[number, Locale, number, string]>([
    // The headline case: the same value, the two separators swapped.
    [1234.5, "de", 1, "1.234,5"],
    [1234.5, "en", 1, "1,234.5"],
    // Two group separators, so a single-replacement bug shows up.
    [1234567, "de", 0, "1.234.567"],
    [1234567, "en", 0, "1,234,567"],
    // 999 is the last integer with no group separator; 1000 the first with one.
    [999, "de", 0, "999"],
    [1000, "de", 0, "1.000"],
    [1000, "en", 0, "1,000"],
    // A minus sign must survive grouping.
    [-1234.5, "de", 1, "-1.234,5"],
    // minimumFractionDigits pads, so a column of consumption figures stays aligned.
    [0, "de", 1, "0,0"],
    // 18.74 kWh/100 km rounds down, 18.75 rounds away from zero (halfExpand, not banker's).
    [18.74, "de", 1, "18,7"],
    [18.75, "de", 1, "18,8"],
    [0.5, "de", 0, "1"],
  ])("formats %s in %s with %s decimals as %s", (value, locale, decimals, expected) => {
    expect(formatNumber(value, locale, { decimals })).toBe(expected);
  });

  it("abbreviates large numbers per locale in compact notation", () => {
    // 1 234 567 charging points would be unreadable in full in a metric tile. German CLDR
    // abbreviates millions as "Mio." after a non-breaking space; English uses a bare "M".
    expect(formatNumber(1_234_567, "de", { compact: true })).toBe(`1${NBSP}Mio.`);
    // Case-tolerant on purpose: ICU formats a million as "1M" in one release and "1m" in the
    // next for en-GB (Node 24/macOS vs Node 22/Linux runners). The value and the unit
    // letter are what matter; its case is an ICU data detail, not our behaviour.
    expect(formatNumber(1_234_567, "en", { compact: true })).toMatch(/^1[mM]$/);
    // `decimals` still applies inside compact notation: 1 234 567 → 1.2M.
    expect(formatNumber(1_234_567, "en", { compact: true, decimals: 1 })).toMatch(/^1\.2[mM]$/);
  });

  it("keeps the two locales genuinely distinct", () => {
    // If this ever passes, LOCALE_TAGS is being ignored and both locales share a formatter.
    expect(formatNumber(1234.5, "de", { decimals: 1 })).not.toBe(
      formatNumber(1234.5, "en", { decimals: 1 }),
    );
  });
});

describe("formatPercent", () => {
  it.each<[number, Locale, number | undefined, string]>([
    [12.5, "de", undefined, "12,5 %"],
    [12.5, "en", undefined, "12.5 %"],
    // Zero decimals for a share that is exact.
    [100, "de", 0, "100 %"],
    [0, "de", undefined, "0,0 %"],
    // A share above 100 % is a legitimate reading (consumption vs. baseline), not an error.
    [137.2, "de", 1, "137,2 %"],
  ])("formats %s in %s as %s", (value, locale, decimals, expected) => {
    expect(decimals === undefined ? formatPercent(value, locale) : formatPercent(value, locale, decimals)).toBe(
      expected,
    );
  });
});

describe("formatDelta", () => {
  it.each<[number, Locale, string]>([
    // The sign is the message: +5,7 % means consumption went UP.
    [5.7, "de", "+5,7 %"],
    [5.7, "en", "+5.7 %"],
    [-14, "de", "-14,0 %"],
    [-14, "en", "-14.0 %"],
    // Exactly zero gets no "+": claiming a rise that did not happen is worse than no sign.
    [0, "de", "0,0 %"],
    // Rounds to zero but keeps its sign — the reader sees the direction even at this scale.
    [-0.04, "de", "-0,0 %"],
  ])("formats %s in %s as %s", (value, locale, expected) => {
    expect(formatDelta(value, locale)).toBe(expected);
  });

  it("refuses to print a delta computed from a zero baseline", () => {
    // (x - 0) / 0 is Infinity. Rendering "∞ %" or "NaN %" on a dashboard would be worse than
    // rendering nothing, so the non-finite guard is the divide-by-zero backstop.
    expect(formatDelta(Number.POSITIVE_INFINITY, "de")).toBe(PLACEHOLDER);
    expect(formatDelta(Number.NaN, "de")).toBe(PLACEHOLDER);
  });
});

describe("formatDuration", () => {
  it.each<[number, Locale, string]>([
    // 8040 s = 134 min = 2 h + 14 min. Identical in both locales because neither number groups.
    [8040, "de", "2 h 14 min"],
    [8040, "en", "2 h 14 min"],
    // 2820 s = 47 min exactly — under an hour, so no hour component at all.
    [2820, "de", "47 min"],
    // Zero is a real duration (a charging stop that has not started), not a missing value.
    [0, "de", "0 min"],
    // Minutes are zero-padded so a column of durations lines up.
    [3600, "de", "1 h 00 min"],
    // 3599 s = 59.98 min, which rounds to 60 — that must carry into the hour rather than
    // render the impossible "0 h 60 min".
    [3599, "de", "1 h 00 min"],
    // Rounding boundary at half a minute.
    [29, "de", "0 min"],
    [30, "de", "1 min"],
    // 3 600 000 s = 1000 h exactly; the hour count is grouped in the local convention.
    [3_600_000, "de", "1.000 h 00 min"],
    [3_600_000, "en", "1,000 h 00 min"],
  ])("formats %s seconds in %s as %s", (seconds, locale, expected) => {
    expect(formatDuration(seconds, locale)).toBe(expected);
  });

  it("rejects a negative duration", () => {
    // A negative travel time means the caller subtracted in the wrong order. Showing
    // "-1 h 00 min" would hide that; the placeholder makes it visible.
    expect(formatDuration(-1, "de")).toBe(PLACEHOLDER);
    expect(formatDuration(-8040, "de")).toBe(PLACEHOLDER);
  });
});

describe("formatDistanceKm", () => {
  it.each<[number, Locale, boolean, string]>([
    // Frankfurt–Stuttgart is 203.4 km (BUILD_SPEC §8) and the API reports metres.
    [203_400, "de", true, "203"],
    [203_400, "en", true, "203"],
    // The same distance handed over already in kilometres must format identically.
    [203.4, "de", false, "203"],
    // Below 10 km a decimal is kept, because 100 m matters at that scale.
    [9.44, "de", false, "9,4"],
    [9.44, "en", false, "9.4"],
    // Exactly on the 10 km threshold: `km < 10` is false, so the decimal is dropped.
    [10, "de", false, "10"],
    // Just under it: the decimal branch is chosen before rounding, so it reads "10,0" not "10".
    [9.999, "de", false, "10,0"],
    [1500, "de", true, "1,5"],
    [0, "de", false, "0,0"],
    // A negative offset (a detour measured backwards) keeps its sign and its decimal.
    [-500, "de", true, "-0,5"],
  ])("formats %s (%s, fromMetres=%s) as %s", (value, locale, fromMetres, expected) => {
    expect(formatDistanceKm(value, locale, { fromMetres })).toBe(expected);
  });

  it("treats the input as kilometres unless told otherwise", () => {
    // Getting this default wrong is a 1000x error that still looks like a plausible distance.
    expect(formatDistanceKm(203_400, "de")).toBe("203.400");
    expect(formatDistanceKm(203_400, "de", { fromMetres: true })).toBe("203");
  });
});

describe("formatCoordinate", () => {
  it.each<[number, number, Locale, string]>([
    // Frankfurt am Main. German writes Ost as "O"; English writes East as "E".
    [50.1109, 8.6821, "de", "50,1109° N · 8,6821° O"],
    [50.1109, 8.6821, "en", "50.1109° N · 8.6821° E"],
    // Berlin — the trailing zero is kept, coordinates are fixed-width by design.
    [52.52, 13.405, "en", "52.5200° N · 13.4050° E"],
    // Southern hemisphere: the minus sign becomes an S, it is not printed twice.
    [-33.8688, 151.2093, "de", "33,8688° S · 151,2093° O"],
    // Western longitude is W in both locales — only the eastern letter differs.
    [51.4779, -0.0015, "de", "51,4779° N · 0,0015° W"],
    [51.4779, -0.0015, "en", "51.4779° N · 0.0015° W"],
    // Exactly on the equator and the prime meridian: `>= 0` puts both on the positive side.
    [0, 0, "de", "0,0000° N · 0,0000° O"],
  ])("formats %s / %s in %s as %s", (latitude, longitude, locale, expected) => {
    expect(formatCoordinate(latitude, longitude, locale)).toBe(expected);
  });
});

describe("formatRelativeAge", () => {
  /** A fixed instant. Everything below is expressed as an offset from it. */
  const NOW = new Date("2026-09-14T12:00:00Z");

  function ago(seconds: number): Date {
    return new Date(NOW.getTime() - seconds * 1000);
  }

  afterEach(() => {
    vi.useRealTimers();
  });

  it.each<[number, Locale, string]>([
    // 18 minutes — the canonical "data is fresh enough" reading.
    [18 * 60, "de", "vor 18 Min."],
    [18 * 60, "en", "18 min ago"],
    // Under a minute stays in seconds.
    [30, "de", "vor 30 Sek."],
    [30, "en", "30 sec ago"],
    // 59 s is the last second-valued age; 60 s is the first minute-valued one.
    [59, "de", "vor 59 Sek."],
    [60, "de", "vor 1 Min."],
    // 59 min is the last minute-valued age; 3600 s is the first hour-valued one.
    [59 * 60, "de", "vor 59 Min."],
    [3600, "de", "vor 1 Std."],
    // 23 h is the last hour-valued age; 24 h crosses into days, where German CLDR has a word.
    [23 * 3600, "de", "vor 23 Std."],
    [24 * 3600, "de", "gestern"],
    [48 * 3600, "de", "vorgestern"],
    // English has no single word for the day before yesterday, which is exactly why the
    // numeric:"auto" behaviour has to be exercised in both locales.
    [48 * 3600, "en", "2 days ago"],
    [7 * 24 * 3600, "de", "vor 7 Tagen"],
  ])("renders an age of %s seconds in %s as %s", (seconds, locale, expected) => {
    expect(formatRelativeAge(ago(seconds), locale, NOW)).toBe(expected);
  });

  it("renders a timestamp in the future as future, not as a huge negative age", () => {
    // Clock skew between the ingestion host and the browser is normal; "in 5 Min." is honest,
    // "vor -5 Min." is a bug on screen.
    expect(formatRelativeAge(new Date(NOW.getTime() + 5 * 60_000), "de", NOW)).toBe("in 5 Min.");
    expect(formatRelativeAge(new Date(NOW.getTime() + 2 * 3600_000), "en", NOW)).toBe("in 2 hr");
  });

  it("says 'jetzt' rather than 'vor 0 Sek.' for the current instant", () => {
    expect(formatRelativeAge(NOW, "de", NOW)).toBe("jetzt");
    expect(formatRelativeAge(NOW, "en", NOW)).toBe("now");
  });

  it("accepts an ISO string as well as a Date", () => {
    // Timestamps arrive off the wire as ISO strings; both paths must agree.
    expect(formatRelativeAge("2026-09-14T11:42:00Z", "de", NOW)).toBe(
      formatRelativeAge(ago(18 * 60), "de", NOW),
    );
  });

  it.each<[string, string | Date | null | undefined]>([
    ["null", null],
    ["undefined", undefined],
    ["an empty string", ""],
    ["an unparseable string", "not-a-timestamp"],
  ])("returns null for %s so the caller can omit the element entirely", (_label, value) => {
    // null rather than the dash placeholder: an age with no timestamp should render no node,
    // not an empty-looking one next to a real reading.
    expect(formatRelativeAge(value, "de", NOW)).toBeNull();
  });

  it("uses the injected clock and never the system clock", () => {
    // The `now` parameter exists so these tests are deterministic. If an implementation ever
    // reached for `new Date()` internally, this assertion would move with the wall clock.
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2031-01-01T00:00:00Z"));
    expect(formatRelativeAge(ago(18 * 60), "de", NOW)).toBe("vor 18 Min.");
  });
});

describe("formatDateTime", () => {
  // Pinned to Europe/Berlin so the assertion does not depend on the CI runner's TZ.
  const BERLIN: Intl.DateTimeFormatOptions = {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "Europe/Berlin",
  };

  it("renders an instant in German local time", () => {
    // Germany observes CEST (UTC+2) in September, so 08:00 UTC is 10:00 in Berlin.
    expect(formatDateTime("2026-09-14T08:00:00Z", "de", BERLIN)).toBe("14.09.2026, 10:00");
  });

  it("renders the same instant in the English date order", () => {
    expect(formatDateTime("2026-09-14T08:00:00Z", "en", BERLIN)).toBe("14 Sept 2026, 10:00");
  });

  it("applies the winter offset on a January instant", () => {
    // CET (UTC+1) — proof the formatter resolves the zone rather than adding a fixed offset.
    expect(formatDateTime("2026-01-14T08:00:00Z", "de", BERLIN)).toBe("14.01.2026, 09:00");
  });

  it.each<[string, string | Date | null | undefined]>([
    ["null", null],
    ["undefined", undefined],
    ["an empty string", ""],
    ["an unparseable string", "14.09.2026"],
    ["an Invalid Date", new Date("nonsense")],
  ])("renders the placeholder for %s", (_label, value) => {
    expect(formatDateTime(value, "de", BERLIN)).toBe(PLACEHOLDER);
  });
});

describe("missing and non-finite values", () => {
  /**
   * The single most important property of this module: no arithmetic accident ever reaches the
   * screen as text. `NaN`, `Infinity`, `null` and `undefined` all collapse to one dash.
   */
  const FORMATTERS: [string, (value: number | null | undefined, locale: Locale) => string][] = [
    ["formatNumber", (value, locale) => formatNumber(value, locale, { decimals: 1 })],
    ["formatNumber(compact)", (value, locale) => formatNumber(value, locale, { compact: true })],
    ["formatPercent", (value, locale) => formatPercent(value, locale)],
    ["formatDelta", (value, locale) => formatDelta(value, locale)],
    ["formatDuration", (value, locale) => formatDuration(value, locale)],
    ["formatDistanceKm", (value, locale) => formatDistanceKm(value, locale)],
    ["formatDistanceKm(fromMetres)", (value, locale) =>
      formatDistanceKm(value, locale, { fromMetres: true })],
  ];

  const BAD_VALUES: [string, number | null | undefined][] = [
    ["null", null],
    ["undefined", undefined],
    ["NaN", Number.NaN],
    ["Infinity", Number.POSITIVE_INFINITY],
    ["-Infinity", Number.NEGATIVE_INFINITY],
  ];

  const cases: [string, (value: number | null | undefined, locale: Locale) => string, string, number | null | undefined][] =
    FORMATTERS.flatMap(([name, fn]) =>
      BAD_VALUES.map(
        ([label, value]) =>
          [name, fn, label, value] as [
            string,
            (value: number | null | undefined, locale: Locale) => string,
            string,
            number | null | undefined,
          ],
      ),
    );

  it.each(cases)("%s renders %s as the placeholder", (_name, fn, _label, value) => {
    for (const locale of ["de", "en"] as const) {
      const rendered = fn(value, locale);
      expect(rendered).toBe(PLACEHOLDER);
      // Belt and braces: the two strings a reader must never see on a measurement dashboard.
      expect(rendered).not.toContain("NaN");
      expect(rendered).not.toContain("Infinity");
      expect(rendered).not.toBe("");
    }
  });
});
