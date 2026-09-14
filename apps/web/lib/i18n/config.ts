/**
 * AutoTwin DE speaks German first.
 *
 * The product is about German infrastructure, read by German engineers, using German
 * technical vocabulary (`Ladeinfrastruktur`, `Streckenabschnitt`, `Ladezustand`). English is a
 * full translation, not a fallback with gaps — the type system enforces that below.
 */

export const LOCALES = ["de", "en"] as const;

export type Locale = (typeof LOCALES)[number];

export const DEFAULT_LOCALE: Locale = "de";

/** Cookie name; read on the server in the root layout, written by the language switch. */
export const LOCALE_COOKIE = "autotwin_locale";

/** One year — a language choice is not a session preference. */
export const LOCALE_COOKIE_MAX_AGE = 60 * 60 * 24 * 365;

export const LOCALE_LABELS: Record<Locale, string> = {
  de: "Deutsch",
  en: "English",
};

/** BCP-47 tags for `<html lang>`, `Intl.NumberFormat` and `Intl.DateTimeFormat`. */
export const LOCALE_TAGS: Record<Locale, string> = {
  de: "de-DE",
  en: "en-GB",
};

export function isLocale(value: string | undefined | null): value is Locale {
  return value != null && (LOCALES as readonly string[]).includes(value);
}

export function resolveLocale(value: string | undefined | null): Locale {
  return isLocale(value) ? value : DEFAULT_LOCALE;
}
