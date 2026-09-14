import type { Messages } from "@/lib/i18n/messages/de";

/**
 * The repository's documentation, as data.
 *
 * Paths are relative to the repository root and are code identifiers, not prose — they are never
 * translated. Titles and descriptions live in the catalogue and are referenced by key, so this
 * file stays a manifest rather than a second place where copy has to be maintained.
 *
 * `NEXT_PUBLIC_REPO_URL` turns the entries into links. Without it the page still works: the path
 * is shown and can be copied. A dead link would be worse than an honest path.
 */

export type DocKey = keyof Messages["docs"]["items"];
export type AdrKey = keyof Messages["docs"]["adr"];

export interface DocEntry {
  path: string;
  key: DocKey;
}

export interface AdrEntry {
  path: string;
  key: AdrKey;
  /** The ADR's number, rendered as the identifier it is. */
  id: string;
}

export const SPEC_DOCS: DocEntry[] = [
  { path: "ARCHITECTURE.md", key: "architecture" },
  { path: "docs/BUILD_SPEC.md", key: "buildSpec" },
  { path: "docs/DESIGN_SYSTEM.md", key: "designSystem" },
  { path: "IMPLEMENTATION_PLAN.md", key: "implementationPlan" },
];

export const DATA_DOCS: DocEntry[] = [
  { path: "docs/data/sources.md", key: "dataSources" },
  { path: "docs/ml/energy-model.md", key: "energyModel" },
  { path: "docs/ml/charging-optimisation.md", key: "chargingOptimisation" },
  { path: "dbt/README.md", key: "dbt" },
  { path: "docs/research/2026-09-14-data-source-verification.md", key: "research" },
];

export const LICENCE_DOCS: DocEntry[] = [
  { path: "DATA_LICENSES.md", key: "dataLicences" },
  { path: "LICENSE", key: "licence" },
];

export const ADR_INDEX: DocEntry = { path: "docs/adr/README.md", key: "adrIndex" };

export const ADR_DOCS: AdrEntry[] = [
  { id: "001", path: "docs/adr/001-postgis-for-geospatial-storage.md", key: "a001" },
  { id: "002", path: "docs/adr/002-redpanda-for-local-streaming.md", key: "a002" },
  { id: "003", path: "docs/adr/003-maplibre-for-mapping.md", key: "a003" },
  { id: "004", path: "docs/adr/004-simulation-vs-real-vehicle-data.md", key: "a004" },
  { id: "005", path: "docs/adr/005-provider-abstraction.md", key: "a005" },
  { id: "006", path: "docs/adr/006-json-events-no-schema-registry.md", key: "a006" },
  { id: "007", path: "docs/adr/007-duckdb-polars-over-spark.md", key: "a007" },
  { id: "008", path: "docs/adr/008-uv-workspace-monorepo.md", key: "a008" },
  { id: "009", path: "docs/adr/009-rejected-technologies.md", key: "a009" },
];

/** Licences of the ingested datasets, keyed to the catalogue entries that carry the wording. */
export const DATA_SOURCES = [
  {
    key: "charging" as const,
    url: "https://www.bundesnetzagentur.de/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/Ladesaeulenkarte/start.html",
    licenceUrl: "https://creativecommons.org/licenses/by/4.0/deed.de",
  },
  {
    key: "weather" as const,
    url: "https://opendata.dwd.de/",
    licenceUrl: "https://creativecommons.org/licenses/by/4.0/deed.de",
  },
  {
    key: "traffic" as const,
    url: "https://verkehr.autobahn.de/o/autobahn/",
    licenceUrl: null,
  },
  {
    key: "osm" as const,
    url: "https://www.openstreetmap.org/copyright",
    licenceUrl: "https://opendatacommons.org/licenses/odbl/",
  },
];

/** `undefined` when the repository URL is not configured — the caller then renders a path. */
export function repositoryUrl(path: string): string | undefined {
  const base = process.env.NEXT_PUBLIC_REPO_URL;
  if (!base) return undefined;
  return `${base.replace(/\/+$/, "")}/blob/main/${path}`;
}
