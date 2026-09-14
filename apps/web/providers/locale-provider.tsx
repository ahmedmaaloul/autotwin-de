"use client";

import {
  createContext,
  use,
  useCallback,
  useEffect,
  useMemo,
  useSyncExternalStore,
  type ReactNode,
} from "react";

import {
  DEFAULT_LOCALE,
  LOCALE_COOKIE,
  LOCALE_COOKIE_MAX_AGE,
  LOCALE_TAGS,
  resolveLocale,
  type Locale,
} from "@/lib/i18n/config";
import { getMessages } from "@/lib/i18n/messages";
import type { Messages } from "@/lib/i18n/messages/de";

interface LocaleContextValue {
  locale: Locale;
  messages: Messages;
  setLocale: (next: Locale) => void;
}

const LocaleContext = createContext<LocaleContextValue | null>(null);

/**
 * The locale is an external store: a cookie, read on the client.
 *
 * Resolving it on the server would be marginally nicer (no possibility of a German first paint
 * for an English reader), but it needs a request — and the hosted demo is a fully static export
 * with no server at all. `useSyncExternalStore` gives the same result everywhere: the server
 * snapshot is the default locale, hydration matches it, and the cookie takes over immediately
 * after — without a hydration mismatch and without a `setState` in an effect body.
 */
const CHANGE_EVENT = "autotwin:locale";

function readCookieLocale(): Locale {
  if (typeof document === "undefined") return DEFAULT_LOCALE;
  const match = document.cookie.match(new RegExp(`(?:^|; )${LOCALE_COOKIE}=([^;]*)`));
  return resolveLocale(match?.[1]);
}

function subscribe(onChange: () => void): () => void {
  window.addEventListener(CHANGE_EVENT, onChange);
  return () => window.removeEventListener(CHANGE_EVENT, onChange);
}

export function LocaleProvider({ children }: { children: ReactNode }) {
  const locale = useSyncExternalStore(subscribe, readCookieLocale, () => DEFAULT_LOCALE);

  // `<html lang>` is rendered statically as the default; keep it truthful once the real locale
  // is known, for screen readers and for the language-toggle end-to-end test.
  useEffect(() => {
    document.documentElement.lang = LOCALE_TAGS[locale];
  }, [locale]);

  const setLocale = useCallback((next: Locale) => {
    document.cookie = `${LOCALE_COOKIE}=${next}; path=/; max-age=${LOCALE_COOKIE_MAX_AGE}; samesite=lax`;
    // Notify every subscriber; the whole tree re-renders in the new language with no reload.
    window.dispatchEvent(new Event(CHANGE_EVENT));
  }, []);

  const value = useMemo<LocaleContextValue>(
    () => ({ locale, messages: getMessages(locale), setLocale }),
    [locale, setLocale],
  );

  return <LocaleContext value={value}>{children}</LocaleContext>;
}

function useLocaleContext(): LocaleContextValue {
  const context = use(LocaleContext);
  if (!context) {
    // Defaulting silently would hide a missing provider until a German string appeared in an
    // English UI. Failing loudly at the boundary is cheaper to debug.
    throw new Error("useTranslations must be used inside <LocaleProvider>");
  }
  return context;
}

/** The typed catalogue. `t.routes.analyze` — autocompleted, and impossible to misspell. */
export function useTranslations(): Messages {
  return useLocaleContext().messages;
}

export function useLocale(): Locale {
  return useLocaleContext().locale;
}

export function useSetLocale(): (next: Locale) => void {
  return useLocaleContext().setLocale;
}

export { DEFAULT_LOCALE };
