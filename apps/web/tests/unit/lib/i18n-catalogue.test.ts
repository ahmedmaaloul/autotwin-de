import { describe, expect, it } from "vitest";

import { DEFAULT_LOCALE, LOCALES, isLocale, resolveLocale, type Locale } from "@/lib/i18n/config";
import { getMessages, translate } from "@/lib/i18n/messages";
import { de } from "@/lib/i18n/messages/de";
import { en } from "@/lib/i18n/messages/en";

/**
 * Catalogue integrity.
 *
 * `en.ts` is declared as `Messages`, so TypeScript already refuses a missing translation — but
 * only while nobody widens a branch, casts, or generates part of the catalogue. These tests are
 * the runtime backstop, and they are the ones that stop a half-translated release: a key that
 * exists in German and not in English renders as the raw key path in the English UI.
 */

/** Flatten a nested catalogue into `dotted.key -> string`. */
function flattenMessages(node: unknown, prefix = ""): Map<string, string> {
  const flat = new Map<string, string>();

  if (typeof node === "string") {
    flat.set(prefix, node);
    return flat;
  }

  if (typeof node === "object" && node !== null) {
    for (const [key, value] of Object.entries(node)) {
      const path = prefix ? `${prefix}.${key}` : key;
      for (const [childPath, childValue] of flattenMessages(value, path)) {
        flat.set(childPath, childValue);
      }
    }
  }

  return flat;
}

/** The set of `{placeholder}` names a string expects, sorted so two strings compare as equal. */
function placeholdersOf(value: string): string[] {
  return [...value.matchAll(/\{(\w+)\}/g)].map((match) => match[1]).sort();
}

const GERMAN = flattenMessages(de);
const ENGLISH = flattenMessages(en);

const CATALOGUES: [Locale, Map<string, string>][] = [
  ["de", GERMAN],
  ["en", ENGLISH],
];

describe("catalogue key symmetry", () => {
  it("has exactly the same key paths in both languages", () => {
    const onlyGerman = [...GERMAN.keys()].filter((key) => !ENGLISH.has(key)).sort();
    const onlyEnglish = [...ENGLISH.keys()].filter((key) => !GERMAN.has(key)).sort();

    // A key present in only one catalogue renders as the literal key path in the other UI.
    expect({ onlyGerman, onlyEnglish }).toEqual({ onlyGerman: [], onlyEnglish: [] });
  });

  it("walks a catalogue deep enough to be a real check", () => {
    // Guards the test itself: if `flattenMessages` ever stopped recursing, the symmetry check
    // above would compare two trivially equal sets and pass for the wrong reason.
    expect(GERMAN.size).toBeGreaterThan(500);
    expect([...GERMAN.keys()].some((key) => key.split(".").length >= 3)).toBe(true);
    expect(GERMAN.get("provenance.simulated")).toBe("SIMULIERT");
  });
});

describe.each(CATALOGUES)("%s catalogue leaves", (locale, catalogue) => {
  it("contains no empty string", () => {
    // An empty string renders as an invisible node — the reader sees a gap and cannot tell
    // whether the value is missing or genuinely blank.
    const empty = [...catalogue].filter(([, value]) => value.length === 0).map(([key]) => key);
    expect(empty).toEqual([]);
  });

  it("contains no whitespace-only string", () => {
    const blank = [...catalogue]
      .filter(([, value]) => value.length > 0 && value.trim().length === 0)
      .map(([key]) => key);
    expect(blank).toEqual([]);
  });

  it("contains no untranslated marker", () => {
    // TODO/TBD/FIXME in a catalogue means a string shipped before it was written.
    const markers = [...catalogue]
      .filter(([, value]) => /\b(TODO|TBD|FIXME|XXX|LOREM)\b/i.test(value))
      .map(([key]) => key);
    expect(markers).toEqual([]);
  });

  it("has no leading or trailing whitespace", () => {
    // Stray padding shows up as a misaligned label next to an icon, and is invisible in review.
    const padded = [...catalogue]
      .filter(([, value]) => value !== value.trim())
      .map(([key]) => key);
    expect(padded).toEqual([]);
  });

  it("is reachable through getMessages", () => {
    expect(flattenMessages(getMessages(locale))).toEqual(catalogue);
  });
});

describe("cross-language consistency", () => {
  it("uses the same {placeholders} in both languages for every key", () => {
    // `translate()` interpolates by name. If German says {value} and English says {wert}, the
    // English string renders the literal braces on screen and the number is simply lost.
    const mismatched = [...GERMAN]
      .filter(([key, german]) => {
        const english = ENGLISH.get(key);
        return english !== undefined && placeholdersOf(german).join(",") !== placeholdersOf(english).join(",");
      })
      .map(([key]) => key);

    expect(mismatched).toEqual([]);
  });

  it("is a real translation rather than a copy of the German file", () => {
    // Proper nouns and abbreviations legitimately coincide ("AutoTwin DE", "CCS", "Live Twin"),
    // so some overlap is expected — but if most leaves were identical, en.ts would be a stub.
    const identical = [...GERMAN].filter(([key, value]) => ENGLISH.get(key) === value);
    const ratio = identical.length / GERMAN.size;
    expect(ratio).toBeLessThan(0.25);
  });
});

describe("locale configuration", () => {
  it("defaults to German, because the product speaks German first", () => {
    expect(DEFAULT_LOCALE).toBe("de");
    expect(LOCALES).toEqual(["de", "en"]);
  });

  it.each<[string | undefined | null, boolean]>([
    ["de", true],
    ["en", true],
    ["fr", false],
    ["DE", false],
    ["", false],
    [undefined, false],
    [null, false],
  ])("isLocale(%s) is %s", (value, expected) => {
    expect(isLocale(value)).toBe(expected);
  });

  it.each<[string | undefined | null, Locale]>([
    ["en", "en"],
    ["de", "de"],
    // An unsupported or absent cookie must fall back to German, never to an empty UI.
    ["fr", "de"],
    ["", "de"],
    [undefined, "de"],
    [null, "de"],
  ])("resolveLocale(%s) is %s", (value, expected) => {
    expect(resolveLocale(value)).toBe(expected);
  });

  it("returns a different catalogue per locale", () => {
    expect(getMessages("de").nav.overview).toBe("Übersicht");
    expect(getMessages("en").nav.overview).toBe("Overview");
  });
});

describe("translate", () => {
  it("resolves a dotted key", () => {
    expect(translate(de, "nav.overview")).toBe(de.nav.overview);
    expect(translate(en, "nav.overview")).toBe(en.nav.overview);
  });

  it("resolves a deeply nested key", () => {
    expect(translate(de, "provenance.dataMode.cache")).toBe(de.provenance.dataMode.cache);
  });

  it("interpolates named placeholders", () => {
    // "vor {value} aktualisiert" — the relative age is computed elsewhere and injected here.
    expect(translate(de, "provenance.updatedAgo", { value: "18 Min." })).toBe(
      "vor 18 Min. aktualisiert",
    );
    expect(translate(en, "provenance.updatedAgo", { value: "18 min" })).toBe(
      "updated 18 min ago",
    );
  });

  it("stringifies a numeric value", () => {
    // Note: `translate` does not localise numbers — callers pass an already-formatted string
    // when the number needs grouping. 7 has no separator, so this is safe either way.
    expect(translate(de, "live.vehiclesActive", { count: 7 })).toBe("7 Fahrzeuge aktiv");
  });

  it("leaves an unsupplied placeholder literal instead of printing 'undefined'", () => {
    // A visible "{count}" tells a developer exactly which value the caller forgot. "undefined"
    // in the middle of a sentence tells them nothing.
    expect(translate(de, "live.vehiclesActive")).toBe("{count} Fahrzeuge aktiv");
    expect(translate(de, "live.vehiclesActive", { other: 3 })).toBe("{count} Fahrzeuge aktiv");
  });

  it.each<[string, string]>([
    // A key that does not exist at all.
    ["nav.doesNotExist", "nav.doesNotExist"],
    ["completelyUnknown", "completelyUnknown"],
    // A key that resolves to a branch rather than a leaf must not render "[object Object]".
    ["nav", "nav"],
    ["provenance.dataMode", "provenance.dataMode"],
    // A path that tries to walk *through* a string.
    ["nav.overview.deeper", "nav.overview.deeper"],
    // Degenerate input.
    ["", ""],
    ["nav.", "nav."],
    // Prototype walking must not leak an object either.
    ["__proto__", "__proto__"],
    ["constructor.name", "constructor.name"],
  ])("returns the key itself for %s", (key, expected) => {
    // Returning the key makes a missing string loud in the UI. Returning "" would make it
    // invisible, and the bug would ship.
    expect(translate(de, key)).toBe(expected);
  });

  it("returns the key unchanged even when interpolation values are supplied", () => {
    expect(translate(de, "nav.doesNotExist", { value: "x" })).toBe("nav.doesNotExist");
  });
});
