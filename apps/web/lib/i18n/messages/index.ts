import type { Locale } from "../config";
import { de, type Messages } from "./de";
import { en } from "./en";

const CATALOGUES: Record<Locale, Messages> = { de, en };

export function getMessages(locale: Locale): Messages {
  return CATALOGUES[locale];
}

/**
 * Resolve a dotted key against a catalogue and interpolate `{placeholders}`.
 *
 * The typed `useTranslations()` hook is the normal way to read a string; this exists for the
 * handful of places that need a runtime key (table columns generated from an enum, for
 * example). It returns the key itself when nothing matches, which makes a missing string
 * obvious in the UI instead of rendering as an empty node.
 */
export function translate(
  messages: Messages,
  key: string,
  values?: Record<string, string | number>,
): string {
  const resolved = key
    .split(".")
    .reduce<unknown>(
      (node, part) =>
        typeof node === "object" && node !== null
          ? (node as Record<string, unknown>)[part]
          : undefined,
      messages,
    );

  if (typeof resolved !== "string") return key;
  if (!values) return resolved;

  return resolved.replace(/\{(\w+)\}/g, (match, name: string) =>
    name in values ? String(values[name]) : match,
  );
}
