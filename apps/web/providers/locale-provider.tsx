"use client";

import { createContext, use, useCallback, useMemo, type ReactNode } from "react";

import {
  DEFAULT_LOCALE,
  LOCALE_COOKIE,
  LOCALE_COOKIE_MAX_AGE,
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

export function LocaleProvider({
  locale,
  children,
}: {
  locale: Locale;
  children: ReactNode;
}) {
  const setLocale = useCallback((next: Locale) => {
    // Persisted in a cookie rather than localStorage so the server component that renders
    // <html lang> sees the same value on the very first paint — no flash of the wrong language.
    document.cookie = `${LOCALE_COOKIE}=${next}; path=/; max-age=${LOCALE_COOKIE_MAX_AGE}; samesite=lax`;
    window.location.reload();
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
