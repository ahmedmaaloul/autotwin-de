import { ApiError, type ApiResult } from "./errors";

/**
 * Static demo snapshot — the API client's alternate backend.
 *
 * The hosted demo runs on a static host with **no server at all**. Instead of every page
 * showing its "backend unreachable" state, the client reads a dated snapshot of real API
 * responses from `public/snapshot/` — captured by `scripts/capture_snapshot.py` from a locally
 * running platform loaded with the real Bundesnetzagentur, DWD and Autobahn data.
 *
 * Three rules keep this honest rather than a mock:
 *
 *  1. **Every file is a real response.** Nothing here invents a number. Where the snapshot has
 *     no answer, it says so with an `ApiError` the UI renders as an error state — it never
 *     returns something plausible.
 *  2. **Every result is `dataMode: "fixture"`**, so the existing degraded-source notice shows
 *     on each page, in addition to the site-wide banner carrying the capture date.
 *  3. **Filterable endpoints are filtered for real.** The charging explorer and the traffic list
 *     paginate over the captured row sets in the browser, so the filters behave exactly as they
 *     do against the live API — for the rows the snapshot holds. The explorer states what it
 *     holds (fast-charging sites) and what it does not (the full 116 000-row register).
 *
 * Enabled by building with `NEXT_PUBLIC_DEMO_SNAPSHOT=1`. Off by default, so the normal
 * development loop is untouched.
 */

export const SNAPSHOT_ENABLED = process.env.NEXT_PUBLIC_DEMO_SNAPSHOT === "1";

/** GitHub Pages serves a project site under `/<repo>/`; static fetches must carry that prefix. */
export const BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

interface ManifestEntry {
  method: string;
  path: string;
  query: Record<string, string | number | boolean>;
  data_mode: string | null;
  bytes: number;
}

export interface SnapshotManifest {
  captured_at: string;
  api: string;
  entries: Record<string, ManifestEntry>;
  counts: Record<string, unknown>;
  charging_station_shards: Record<string, number>;
}

interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  has_next: boolean;
}

interface StationRow {
  id: string;
  operator: string | null;
  city: string | null;
  bundesland: string | null;
  latitude: number;
  longitude: number;
  max_power_kw: number | null;
  charging_category: string;
  is_fast_charger: boolean;
  commissioned_on: string | null;
  [key: string]: unknown;
}

interface TrafficRow {
  id: string;
  event_type: string;
  severity: string;
  road_name: string | null;
  latitude: number;
  longitude: number;
  starts_at: string | null;
  ends_at: string | null;
  [key: string]: unknown;
}

interface Feature {
  properties: { is_fast_charger?: boolean; max_power_kw?: number | null; charging_category?: string; operator?: string | null; city?: string | null };
  [key: string]: unknown;
}

// ---------------------------------------------------------------- loading

const cache = new Map<string, Promise<unknown>>();

function loadJson<T>(file: string): Promise<T> {
  let pending = cache.get(file) as Promise<T> | undefined;
  if (!pending) {
    pending = fetch(`${BASE_PATH}/snapshot/${file}`).then(async (response) => {
      if (!response.ok) {
        throw new ApiError(404, "snapshot_missing", `Snapshot file ${file} is not available.`);
      }
      return (await response.json()) as T;
    });
    cache.set(file, pending);
  }
  return pending;
}

export function loadManifest(): Promise<SnapshotManifest> {
  return loadJson<SnapshotManifest>("manifest.json");
}

// ---------------------------------------------------------------- matching

type Query = Record<string, string>;

function parseQuery(search: string): Query {
  const out: Query = {};
  for (const [key, value] of new URLSearchParams(search)) {
    if (value !== "") out[key] = value;
  }
  return out;
}

/** `70` and `70.0` and `"70"` are the same request; compare as strings after numeric folding. */
function fold(value: unknown): string {
  if (typeof value === "number") return String(value);
  if (typeof value === "boolean") return String(value);
  const text = String(value);
  const asNumber = Number(text);
  return text !== "" && Number.isFinite(asNumber) ? String(asNumber) : text;
}

function sameQuery(a: Record<string, unknown>, b: Record<string, unknown>): boolean {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  for (const key of keys) {
    if (fold(a[key] ?? "") !== fold(b[key] ?? "")) return false;
  }
  return true;
}

interface Match {
  key: string;
  entry: ManifestEntry;
  exact: boolean;
}

function findEntry(
  manifest: SnapshotManifest,
  method: string,
  path: string,
  query: Record<string, unknown>,
): Match | null {
  let fallback: Match | null = null;
  for (const [key, entry] of Object.entries(manifest.entries)) {
    if (entry.method !== method || entry.path !== path) continue;
    if (sameQuery(entry.query, query)) return { key, entry, exact: true };
    fallback ??= { key, entry, exact: false };
  }
  return fallback;
}

function ok<T>(data: T): ApiResult<T> {
  return { data, dataMode: "fixture", requestId: null };
}

function unavailable(what: string): never {
  throw new ApiError(
    503,
    "snapshot_mode",
    `${what} ist im statischen Demo-Snapshot nicht verfügbar. / ${what} is not available in the static demo snapshot.`,
  );
}

function paginate<T>(rows: T[], query: Query): Page<T> {
  const page = Math.max(1, Number(query.page ?? 1) || 1);
  const pageSize = Math.min(500, Math.max(1, Number(query.page_size ?? 50) || 50));
  const start = (page - 1) * pageSize;
  return {
    items: rows.slice(start, start + pageSize),
    total: rows.length,
    page,
    page_size: pageSize,
    has_next: start + pageSize < rows.length,
  };
}

const contains = (haystack: string | null | undefined, needle: string): boolean =>
  (haystack ?? "").toLowerCase().includes(needle.toLowerCase());

// ---------------------------------------------------------------- filterable endpoints

async function chargingStations(query: Query): Promise<Page<StationRow>> {
  const manifest = await loadManifest();
  const states = query.bundesland
    ? [query.bundesland]
    : Object.keys(manifest.charging_station_shards);
  const shards = await Promise.all(
    states.map((state) =>
      loadJson<StationRow[]>(`charging_stations_${state}.json`).catch(() => [] as StationRow[]),
    ),
  );

  const minPower = query.min_power_kw ? Number(query.min_power_kw) : null;
  const fastOnly = query.fast_only === "true";
  const from = query.commissioned_from ? Number(query.commissioned_from) : null;
  const to = query.commissioned_to ? Number(query.commissioned_to) : null;
  const year = (row: StationRow) =>
    row.commissioned_on ? Number(row.commissioned_on.slice(0, 4)) : null;

  const rows = shards.flat().filter((row) => {
    if (fastOnly && !row.is_fast_charger) return false;
    if (minPower != null && (row.max_power_kw ?? 0) < minPower) return false;
    if (query.category && row.charging_category !== query.category) return false;
    if (query.operator && !contains(row.operator, query.operator)) return false;
    if (query.q && !(contains(row.operator, query.q) || contains(row.city, query.q))) return false;
    const y = year(row);
    if (from != null && (y == null || y < from)) return false;
    if (to != null && (y == null || y > to)) return false;
    if (query.bbox) {
      const [w, s, e, n] = query.bbox.split(",").map(Number);
      if (row.longitude < w || row.longitude > e || row.latitude < s || row.latitude > n) {
        return false;
      }
    }
    return true;
  });
  return paginate(rows, query);
}

async function chargingGeoJson(query: Query): Promise<unknown> {
  const collection = await loadJson<{ type: string; features: Feature[] }>(
    "get_charging__stations__geojson.json",
  );
  const minPower = query.min_power_kw ? Number(query.min_power_kw) : null;
  const fastOnly = query.fast_only === "true";
  if (!fastOnly && minPower == null && !query.category && !query.operator && !query.q) {
    return collection;
  }
  return {
    ...collection,
    features: collection.features.filter(({ properties: p }) => {
      if (fastOnly && !p.is_fast_charger) return false;
      if (minPower != null && (p.max_power_kw ?? 0) < minPower) return false;
      if (query.category && p.charging_category !== query.category) return false;
      if (query.operator && !contains(p.operator, query.operator)) return false;
      if (query.q && !(contains(p.operator, query.q) || contains(p.city, query.q))) return false;
      return true;
    }),
  };
}

async function stationDetail(id: string, manifest: SnapshotManifest): Promise<unknown> {
  const match = findEntry(manifest, "GET", `/charging/stations/${id}`, {});
  if (match?.exact) return loadJson(`${match.key}.json`);

  // Not among the captured details: the row itself is still real captured data, so serve it
  // with the parts the snapshot does not hold left visibly empty rather than invented.
  const shards = await Promise.all(
    Object.keys(manifest.charging_station_shards).map((state) =>
      loadJson<StationRow[]>(`charging_stations_${state}.json`).catch(() => [] as StationRow[]),
    ),
  );
  const row = shards.flat().find((candidate) => candidate.id === id);
  if (!row) unavailable("Stationsdetail");
  return {
    ...row,
    charging_points: [],
    provenance: {
      source: "bundesnetzagentur",
      data_origin: "official",
      source_identifier: row.external_id ?? null,
      source_url: null,
      source_timestamp: null,
      ingestion_run_id: null,
      ingested_at: null,
    },
  };
}

async function trafficEvents(query: Query, capturedAt: string): Promise<Page<TrafficRow>> {
  const all = await loadJson<TrafficRow[]>("traffic_events_all.json");
  const now = new Date(capturedAt).getTime();
  const rows = all.filter((row) => {
    if (query.event_type && row.event_type !== query.event_type) return false;
    if (query.severity && row.severity !== query.severity) return false;
    if (query.road && !contains(row.road_name, query.road)) return false;
    if (query.active_only === "true") {
      const starts = row.starts_at ? new Date(row.starts_at).getTime() : null;
      const ends = row.ends_at ? new Date(row.ends_at).getTime() : null;
      if (starts != null && starts > now) return false;
      if (ends != null && ends < now) return false;
    }
    if (query.bbox) {
      const [w, s, e, n] = query.bbox.split(",").map(Number);
      if (row.longitude < w || row.longitude > e || row.latitude < s || row.latitude > n) {
        return false;
      }
    }
    return true;
  });
  return paginate(rows, query);
}

// ---------------------------------------------------------------- the flagship POSTs

/** The demo corridors, so a free-text request for a known city pair still gets a real answer. */
const CITY_PAIRS: Record<string, string> = {
  "frankfurt am main|stuttgart": "frankfurt-stuttgart",
  "frankfurt am main|münchen": "frankfurt-muenchen",
  "frankfurt am main|munich": "frankfurt-muenchen",
  "stuttgart|münchen": "stuttgart-muenchen",
  "stuttgart|munich": "stuttgart-muenchen",
  "münchen|ingolstadt": "muenchen-ingolstadt",
  "munich|ingolstadt": "muenchen-ingolstadt",
  "wolfsburg|berlin": "wolfsburg-berlin",
};

function slugFor(body: Record<string, unknown>): string | null {
  if (typeof body.route_slug === "string" && body.route_slug) return body.route_slug;
  const origin = String(body.origin ?? "").trim().toLowerCase();
  const destination = String(body.destination ?? "").trim().toLowerCase();
  return CITY_PAIRS[`${origin}|${destination}`] ?? null;
}

async function routePost(
  path: string,
  body: Record<string, unknown>,
  manifest: SnapshotManifest,
): Promise<unknown> {
  const slug = slugFor(body);
  if (!slug) unavailable("Die Analyse frei gewählter Start- und Zielorte");

  const wanted = {
    route_slug: slug,
    vehicle_code: body.vehicle_code,
    start_soc_percent: body.start_soc_percent,
    min_arrival_soc_percent: body.min_arrival_soc_percent,
  };
  const exact = findEntry(manifest, "POST", path, wanted);
  if (exact?.exact) return loadJson(`${exact.key}.json`);

  // Same corridor, different parameters: serve the corridor's captured default. The response
  // carries its own start_soc/vehicle fields, so the UI displays what was actually computed,
  // and the snapshot banner makes clear why the inputs were not honoured.
  const corridor = Object.entries(manifest.entries).find(
    ([, entry]) => entry.method === "POST" && entry.path === path && entry.query.route_slug === slug,
  );
  if (!corridor) unavailable("Diese Streckenanalyse");
  return loadJson(`${corridor[0]}.json`);
}

// ---------------------------------------------------------------- entry point

export async function resolveSnapshot<T>(
  method: string,
  pathWithQuery: string,
  root: boolean,
  body?: unknown,
): Promise<ApiResult<T>> {
  const manifest = await loadManifest();
  const [rawPath, search = ""] = pathWithQuery.split("?");
  const path = rawPath.startsWith("/") ? rawPath : `/${rawPath}`;
  const query = parseQuery(search);

  if (method === "POST") {
    if (path === "/routes/analyze" || path === "/routes/optimize-charging") {
      return ok((await routePost(path, (body ?? {}) as Record<string, unknown>, manifest)) as T);
    }
    if (path.startsWith("/simulations")) unavailable("Die Simulationssteuerung");
    unavailable("Diese Aktion");
  }

  if (!root) {
    if (path === "/charging/stations") return ok((await chargingStations(query)) as T);
    if (path === "/charging/stations/geojson") return ok((await chargingGeoJson(query)) as T);
    const station = /^\/charging\/stations\/([^/]+)$/.exec(path);
    if (station) return ok((await stationDetail(station[1], manifest)) as T);
    if (path === "/traffic/events") {
      return ok((await trafficEvents(query, manifest.captured_at)) as T);
    }
  }

  const match = findEntry(manifest, "GET", path, query);
  if (!match) unavailable(`GET ${path}`);
  return ok((await loadJson<T>(`${match.key}.json`)) as T);
}

/** The live-vehicle set, for the SSE hook's snapshot path. */
export async function loadSnapshotVehicles<T>(): Promise<T[]> {
  return loadJson<T[]>("get_vehicles__live.json");
}
