# Data sources

Four external systems feed AutoTwin DE. All four are official, public, keyless and free.
This page is the curated brief; the full live-verification record — every URL fetched, every
dead link, every corrected column name — is in
[`docs/research/2026-09-14-data-source-verification.md`](../research/2026-09-14-data-source-verification.md).

Licences are in [`DATA_LICENSES.md`](../../DATA_LICENSES.md). Everything below was verified by
fetching the real bytes on **2026-09-14**.

| Source | What it gives us | Auth | Licence | Adapter |
|---|---|---|---|---|
| Bundesnetzagentur Ladesäulenregister | ~117 000 charging stations | none | CC BY 4.0 | `BundesnetzagenturChargingProvider` |
| DWD Open Data | station observations (T, wind, rain) | none | CC BY 4.0 | `DWDWeatherProvider` |
| Autobahn GmbH API | roadworks, closures, warnings | none | undeclared (see caveat) | `AutobahnTrafficProvider` |
| OSRM + OpenStreetMap | route geometry, geocoding | none | ODbL 1.0 | `OSRMRoutingProvider` |

---

## 1. Bundesnetzagentur — Ladesäulenregister

**Landing page:** <https://www.bundesnetzagentur.de/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/Ladesaeulenkarte/start.html>

### The filename rotates monthly — there is no "latest" URL

The file is published as `Ladesaeulenregister_BNetzA_<YYYY-MM-01>.csv` on
`data.bundesnetzagentur.de`, and **the previous month is deleted, not archived**. Probing
confirmed that `…_2026-08-01.csv` already 404s. There is no undated alias.

The adapter therefore **scrapes the landing page for the current absolute URL** and falls back
to a date-templated guess, then to cache, then to the bundled fixture. The URL appears in the
landing-page HTML as plain text, so a single regex resolves it.

> The widely-cited legacy URL
> `…/SharedDocs/Downloads/DE/Sachgebiete/Energie/.../Ladesaeulenregister.xlsx?__blob=publicationFile`
> is **dead (404)**. Most tutorials on the internet still point at it.

### Parsing facts that a naive implementation gets wrong

| Property | Value | Why it matters |
|---|---|---|
| Encoding | **UTF-8 with BOM** → `utf-8-sig` | Older editions were cp1252. Reading as `utf-8` glues `﻿` onto the first column name and every lookup silently fails. |
| Delimiter | `;` | |
| Preamble | **10 lines** (9 notice lines + a merged-cell group banner) | The real header is line 11 (`skiprows=10`). |
| Columns | **47** | 23 site columns + 6 × 4 connector columns. |
| Decimal separator | comma (`48,442398`) | |
| Date format | `DD.MM.YYYY` | |
| Quoting | `"` with **embedded `;`** inside quoted fields | `line.split(";")` produces a different column count per row. A real CSV parser is mandatory. |

### Column names (verified verbatim from the file, not from documentation)

```
Ladeeinrichtungs-ID · Betreiber · Anzeigename (Karte) · Status · Art der Ladeeinrichtung
Anzahl Ladepunkte · Nennleistung Ladeeinrichtung [kW] · Inbetriebnahmedatum
Straße · Hausnummer · Adresszusatz · Postleitzahl · Ort · Kreis/kreisfreie Stadt · Bundesland
Breitengrad · Längengrad · Standortbezeichnung · Informationen zum Parkraum · Bezahlsysteme
Öffnungszeiten · Öffnungszeiten: Wochentage · Öffnungszeiten: Tageszeiten
then ×6:  Steckertypen{n} · Nennleistung Stecker{n} · EVSE-ID{n} · Public Key{n}
```

Notable corrections to what the source *used* to look like:

- The historic BNetzA typo `Art der Ladeeinrichung` has been **fixed** — code special-casing the
  misspelling now fails.
- `Anschlussleistung` does not exist; the field is `Nennleistung Ladeeinrichtung [kW]`.
- There is no `P1 [kW]`; per-connector power is `Nennleistung Stecker1`, and it can hold
  **multiple values**: `"11; 3,7"`. Parse as string → split `;` → `,`→`.`.

`Art der Ladeeinrichtung` ∈ {`Normalladeeinrichtung`, `Schnellladeeinrichtung`}.

### REST API

A daily-updated JSON/XML interface exists, but BNetzA does not publish the endpoint — you must
request the OpenAPI description from `ladesaeulenregister@bnetza.de`. The ArcGIS FeatureServer
behind the official map, which several community write-ups present as a public API, returns
`{"code": 499, "message": "Token Required"}`. **Neither is usable for a self-service project**,
which is why the adapter is built on the file download.

---

## 2. Deutscher Wetterdienst — Open Data

**Server:** <https://opendata.dwd.de/> — anonymous HTTPS, no key, no User-Agent requirement.

AutoTwin DE uses the **10-minute "now" observations**, which are the real-time feed:

```
climate_environment/CDC/observations_germany/climate/10_minutes/air_temperature/now/
climate_environment/CDC/observations_germany/climate/10_minutes/wind/now/
climate_environment/CDC/observations_germany/climate/10_minutes/precipitation/now/
```

Each station is one ZIP (`10minutenwerte_TU_00044_now.zip`) containing one `produkt_*.txt`.
The station catalogue is `zehn_now_tu_Beschreibung_Stationen.txt` in the same directory.

### Parsing facts

- Files are `;`-delimited **with space padding** — strip every field. Not fixed-width, not plain CSV.
- Encoding is **ISO-8859-1**. `Großenkneten` and `Baden-Württemberg` mojibake as UTF-8.
- `-999` means missing. Convert to `None` before any arithmetic.
- `eor` is a literal end-of-row sentinel in the last column. Discard it.
- `MESS_DATUM` is **UTC**, `YYYYMMDDHHMM` for 10-minute products but `YYYYMMDDHH` for hourly ones —
  a different width per product.
- `STATIONS_ID` is space-padded in the data (`         44`) and zero-padded to 5 digits in
  filenames (`00044`). Normalise both to `int` or joins fail silently.

Fields used: `TT_10` (air temperature, °C), `FF_10` (mean wind speed, m/s),
`RWS_10` (precipitation sum, mm), `RF_10` (relative humidity, %), `PP_10` (pressure, hPa).

The station-description file is genuinely fixed-width with a dash ruler on line 2; splitting on
whitespace breaks on names like `Seebach (Nationalpark Schwarzwald)`. Slice by column offsets.

MOSMIX forecasts (KML inside KMZ, column-oriented `<dwd:Forecast>` elements) are documented in
the verification record but are **not** ingested — observations are what the energy model needs.

**Attribution:** „Quelle: Deutscher Wetterdienst" is required on anything derived from this data.

---

## 3. Autobahn GmbH des Bundes — traffic API

**Base:** `https://verkehr.autobahn.de/o/autobahn/` — keyless, anonymous, no rate limit observed.
OpenAPI spec maintained at <https://github.com/bundesAPI/autobahn-api>.

```
GET /o/autobahn/                                 -> {"roads": ["A1", "A2", ... ]}   (108 unique)
GET /o/autobahn/{roadId}/services/roadworks      -> {"roadworks": [ ... ]}
GET /o/autobahn/{roadId}/services/closure        -> {"closure":   [ ... ]}
GET /o/autobahn/{roadId}/services/warning        -> {"warning":   [ ... ]}
GET /o/autobahn/details/roadworks/{identifier}   -> single item
```

### Field facts measured over 3 327 live roadworks

| Field | Reality |
|---|---|
| `coordinate` | `{"lat": float, "long": float}` — the key is **`long`**, not `lon`/`lng` |
| `point` / `extent` | **lat-first** comma-joined strings |
| `geometry` | GeoJSON `LineString`, **lon-first** — undocumented but always present |
| `isBlocked` | the string `"false"` in **3327 of 3327** items. Effectively dead — **do not use it** |
| `impact.symbols` | the real blocking signal: `"CLOSED" in symbols` (3302 / 3975) |
| `startTimestamp` | **absent** whenever `display_type == "SHORT_TERM_ROADWORKS"` (perfect correlation) |
| `display_type` | exactly `ROADWORKS` \| `SHORT_TERM_ROADWORKS` |
| `subtitle` | direction, with a **leading space**: `" Nürnberg -> München"` |
| `routeRecommendation` | empty in all 3 327 items |

Roads are listed with trailing whitespace (`"A60 "`) — `.strip()` before use.

> **Licence caveat.** No licence statement is published with this API. AutoTwin DE treats it as
> *available for development, licence unconfirmed*: cached locally, never redistributed, low
> request rate. See [`DATA_LICENSES.md`](../../DATA_LICENSES.md).

**Mobilithek** is the licensed alternative (DATEX II), but it requires an account and
certificate-based authentication, so it cannot be the default for a zero-cost project. The
`TrafficProvider` interface exists precisely so a `MobilithekTrafficProvider` can be dropped in.

---

## 4. OSRM + OpenStreetMap

**Routing:** `https://router.project-osrm.org/route/v1/driving/{lon},{lat};{lon},{lat}`
with `?overview=full&geometries=geojson&steps=true&annotations=distance,duration,speed`.

The demo server is community infrastructure under a
[usage policy](https://github.com/Project-OSRM/osrm-backend/wiki/Api-usage-policy): low volume,
non-commercial, identify yourself. AutoTwin DE caches every route on disk and ships a
`make osrm-prepare` target for a fully local instance built from Geofabrik extracts
(`hessen-latest.osm.pbf`, `baden-wuerttemberg-latest.osm.pbf`).

**Geocoding:** Nominatim, with the same caching discipline and a required identifying User-Agent.

**Tiles:** OpenFreeMap (`https://tiles.openfreemap.org/styles/positron` and `/dark`) — MIT
project licence, OpenMapTiles schema, ODbL data, no key and no request limit. The style URL is a
single constant in `apps/web/lib/map/style.ts`; the README documents how to swap it.

**Attribution rendered on every map:** `© OpenStreetMap-Mitwirkende · Routing: OSRM`.

---

## Degradation policy

Every adapter follows the same chain, and reports truthfully which one answered:

```
live  →  on-disk cache (data/raw, TTL from AUTOTWIN_CACHE_TTL_SECONDS)  →  bundled fixture
```

The mode travels to the browser as the `X-AutoTwin-Data-Mode` header and is rendered in the UI.
A source being down degrades the page; it never blanks it and never silently substitutes
made-up numbers.
