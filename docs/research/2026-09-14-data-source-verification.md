# Data sources — verified reference

> Produced by a live research + adversarial-verification pass on **2026-09-14**. Every URL below
> was fetched; the ones that failed are recorded as failures. This document is the reference the
> provider adapters in `services/ingestion` are written against.
>
> **Open-data endpoints move.** When an adapter starts returning `InvalidSourceData`, re-run the
> check here before changing parsing code — the source has probably changed, not the code.


---

## 1. Routing & Map Tiles (OSRM / OpenStreetMap / tiles)

**Verification verdict:** `PARTIAL`

**Licence:** **Routing data (OSRM, all sources):** OpenStreetMap data under **ODbL 1.0**. Attribution required — you must credit both the data (`© OpenStreetMap contributors`) and the routing engine (`Routing by OSRM`). The FOSSGIS policy additionally requires a visible **"fix the map"** link back to OSM. OSRM software itself is **BSD 2-Clause**.

**OpenFreeMap:** project licence **MIT**; map schema is unmodified **OpenMapTiles**; underlying data **ODbL** via OSM. Commercial use explicitly permitted, no request limits.

**VersaTiles:** tiles derived from OSM, **ODbL**; source-level attribution names OpenStreetMap contributors. Tooling is open source. *Unconfirmed:* no explicit public-server terms-of-service or rate-limit document found.

**OSMF raster + vector tiles:** data **ODbL**; tiles served on donated infrastructure under the OSMF Tile / Vector Tile Usage Policies. Best-effort, **no SLA**, access revocable without notice.

**OSM Deutschland (tile.openstreetmap.de):** data **ODbL**. *Unconfirmed:* no published usage policy located.

**CARTO basemaps:** free tier capped at a stated fair-use limit of **5 million tile requests/month**, and CARTO's own page states an **API key is required** (no CARTO account needed to obtain one). Requires CARTO attribution in addition to OSM.

**Geofabrik extracts:** OSM data under **ODbL 1.0**; free download, rebuilt daily.

**Net position for the app:** every recommended component is ODbL/MIT and free for commercial use, provided `© OpenStreetMap contributors` is displayed on the map and OSRM is credited for routes. ODbL's share-alike applies to *derived databases* — displaying routes and tiles does not trigger it; publishing a modified OSM-derived dataset would.

**Authentication:** **None anywhere in the recommended stack — no API key, no account, no credit card, no OAuth.** Every URL in `confirmed_urls` was fetched anonymously and returned HTTP 200.

Details:
- **OSRM demo / FOSSGIS:** no key. Requires a **valid, app-identifying `User-Agent`** — this is a hard requirement, and faking another app's UA is explicitly grounds for blocking. Browsers set a UA automatically; any server-side proxy or native client must set one deliberately (e.g. `AutoTwinDE/1.0 (+https://your-url)`). CORS is wide open (`access-control-allow-origin: *`), so no proxy is needed for browser calls.
- **OpenFreeMap:** no key, and explicitly "no registration, no user database, no API keys, and no cookies."
- **VersaTiles:** no key.
- **OSMF raster + vector tiles:** no key. Requires a unique app-identifying User-Agent and a valid `Referer` from browsers — do **not** set a restrictive `Referrer-Policy` (e.g. `no-referrer`) on pages that load these tiles, or you risk being blocked.
- **Geofabrik:** no key, anonymous HTTPS download.
- **CARTO:** ⚠️ **an API key IS now required per CARTO's stated policy**, even though the CDN currently still answers keyless requests (I verified 200 both with and without a third-party `Referer`). Obtaining a key is free and needs no CARTO account — but this is the one component in this research that is not genuinely keyless, which is why it is not recommended.

No rate-limit headers are exposed by the OSRM demo server (I checked for `X-RateLimit-*` / `Retry-After` — none present), so you must self-throttle to the documented **1 request/second** rather than react to server signals.


### Findings

## Verdict: a fully zero-cost, zero-API-key German mobility stack is achievable today.

**Recommended stack: OSRM (FOSSGIS `routing.openstreetmap.de` for dev, self-hosted Docker for anything serious) + OpenFreeMap vector tiles (`positron` light / `dark` dark) in MapLibre GL JS.** No key, no credit card, no signup, and OpenFreeMap explicitly permits commercial use with no request limits.

---

### 1. OSRM demo server — WORKS, but treat as dev-only

I fetched the real Frankfurt→Stuttgart route and got **HTTP 200, 242,736 bytes**. Critically, the response carries `access-control-allow-origin: *`, so it is **callable directly from browser JS with no proxy**.

| Property | Value |
|---|---|
| Base URL | `https://router.project-osrm.org` |
| Rate limit | **Max 1 request/second** (documented; not enforced via headers — I checked, no `X-RateLimit-*` returned) |
| Licence/use | Reasonable, **non-commercial** use only |
| Paywall | Forbidden — must not sit behind a paywall; reselling access strictly forbidden |
| Guarantees | **None.** No uptime, latency, or data-freshness SLA. "Access may be withdrawn at any time and without giving a reason" |
| User-Agent | A valid, app-identifying UA is **mandatory**. Faking another app's UA gets you blocked |
| Attribution | Must display ODbL data attribution **and** name OSRM as the routing source |

**Acceptable for low-volume development use: yes**, clearly within the intended purpose. Not acceptable for a public production app, and not acceptable for anything commercial.

**Important finding:** `router.project-osrm.org` and `routing.openstreetmap.de` are the **same FOSSGIS-operated infrastructure**. The FOSSGIS host is better documented and exposes three profiles — all three verified HTTP 200:
- `https://routing.openstreetmap.de/routed-car/route/v1/driving/...`
- `https://routing.openstreetmap.de/routed-bike/route/v1/driving/...`
- `https://routing.openstreetmap.de/routed-foot/route/v1/driving/...`

Both returned **byte-identical routing values** (204881.5 m / 8148.9 s) for the test pair. FOSSGIS policy adds: one request/second max, no scraping, valid user agent and correct referrer, and you must display attribution **plus a "fix the map" link**. Note the FOSSGIS bike/foot profiles are *not* the stock osrm-backend profiles.

---

### 2. Verified OSRM response (real German route)

`Frankfurt am Main (8.6821,50.1109) → Stuttgart (9.1829,48.7758)`:
- `routes[0].distance` = **204881.5** m (204.9 km)
- `routes[0].duration` = **8148.9** s (2.26 h)
- `routes[0].geometry` = GeoJSON `LineString` with **3089** coordinate pairs, `[lon, lat]` order
- `legs` length 1, `legs[0].summary` = **"A 6, A 81"**, `legs[0].steps` length **41**
- `annotation.distance` / `.duration` / `.speed` each length **3088** = `coords − 1` (one entry per *segment between* consecutive geometry points)

---

### 3. Self-hosting (the real answer for production)

Official image **`ghcr.io/project-osrm/osrm-backend`** (the old Docker Hub `osrm/osrm-backend` is the legacy location). Geofabrik URLs and sizes verified via HTTP HEAD with live `Content-Length` (both re-cut 2026-09-14):

- **Hessen: 344,994,933 bytes = 329 MB**
- **Baden-Württemberg: 647,326,315 bytes = 617 MB**
- Whole Germany: 4,838,703,123 bytes = **4,615 MB (~4.6 GB)**

Use **MLD** (`osrm-partition` + `osrm-customize`), not CH — faster preprocessing and it supports traffic updates.

---

### 4. Tiles — what is actually keyless

| Provider | Keyless? | Dark style? | Verdict |
|---|---|---|---|
| **OpenFreeMap** | Yes, truly | Yes (`dark`, `fiord`) | **Best choice.** No registration, no API key, no cookies, explicitly "no limits on map views or requests", commercial use allowed |
| **VersaTiles** | Yes | Yes (`eclipse`, `shadow`) | Excellent backup; 5 styles verified 200 |
| **OSMF vector** | Yes | No styles shipped | Raw Shortbread MVT only — you must supply your own style |
| **OSM Standard raster** | Yes | **No** | Permitted for low-traffic dev, but raster-only and light-only |
| **OSM DE raster** | Yes | No | Works, but no published usage policy found |
| **CARTO** | **Policy says NO** | Yes | ⚠️ See warning below |

**⚠️ CARTO caveat — be careful here.** The CDN *currently* still serves keyless: I got HTTP 200 on `dark_all` raster tiles and on `positron-gl-style/style.json`. **But carto.com/basemaps now states an API key is required** ("free to use up to a fair use limit of 5 million tile requests a month — all you need is an API key"). The keyless path is legacy behaviour that contradicts stated policy and could be shut off without notice. **Do not build on it for a zero-cost project.** Also note it requires CARTO attribution in addition to OSM.

**OpenStreetMap Standard tiles — what the policy actually permits:** a low-traffic dev/portfolio app **is permitted**. The policy says OSMF welcomes creative uses, and the vast majority of its tiles serve third-party sites. Hard requirements: a clear unique User-Agent naming your app (generic `okhttp/x.y`-style defaults are blocked), a valid `Referer` from browsers, never send `no-cache` headers, cache ≥7 days, and show `© OpenStreetMap contributors` bottom-right. Prohibited: bulk downloading/pre-seeding, offline use, "download area for later". Access can be blocked without notice if usage degrades service. For a *dashboard* it's a poor fit anyway — raster only, no dark variant, and no vector styling.

**Correction to a common assumption:** the OpenFreeMap homepage lists a **"3D"** style, but `https://tiles.openfreemap.org/styles/3d` returns **HTTP 404**. I tested `3d`, `3D`, `positron-3d`, `liberty-3d`, `dark-3d` — all 404. Only the five styles listed in schema_details are live.

### Schema / format details

## A. OSRM query parameters (the exact set you need)

```
GET https://router.project-osrm.org/route/v1/{profile}/{lon1},{lat1};{lon2},{lat2}?<params>
```

`{profile}` on the demo/FOSSGIS server is effectively ignored in the path — always use `driving` and pick the profile via the FOSSGIS host prefix (`routed-car` / `routed-bike` / `routed-foot`).

**Coordinates are `lon,lat` — longitude FIRST.** This is the single most common integration bug.

| Param | Set it to | Default | Why |
|---|---|---|---|
| `overview` | **`full`** | `simplified` | `simplified` applies Douglas-Peucker and drops detail; `full` gives every geometry point |
| `geometries` | **`geojson`** | `polyline` | Gives a ready `LineString`; default is an encoded polyline needing a decoder |
| `steps` | **`true`** | `false` | Populates `legs[].steps[]` turn-by-turn; also fills `legs[].summary` |
| `annotations` | **`distance,duration,speed`** | `false` | Per-segment arrays |
| `alternatives` | `false`, `true`, or an integer | `false` | Optional |
| `continue_straight` | `default` \| `true` \| `false` | `default` | Optional |

`annotations` also accepts `true`, `nodes`, `datasources`, `weight`. I verified `annotations=true` returns **all** of: `metadata`, `datasources`, `weight`, `nodes`, `distance`, `duration`, `speed` — with `metadata.datasource_names = ["lua profile"]` and `nodes` as raw OSM node IDs. **Prefer the explicit `distance,duration,speed` list** — `annotations=true` adds a `nodes` array of 64-bit OSM IDs that roughly doubles payload size for no UI benefit.

### Reference URL (verified HTTP 200)
```
https://router.project-osrm.org/route/v1/driving/8.6821,50.1109;9.1829,48.7758?overview=full&geometries=geojson&steps=true&annotations=distance,duration,speed
```

---

## B. Verified response shape

```jsonc
{
  "code": "Ok",                       // "Ok" | "NoRoute" | "InvalidUrl" | "InvalidQuery" | ...
  "waypoints": [                      // one per input coordinate
    {
      "hint": "bF4CgBMoAIU...",       // opaque, reusable for snapping
      "location": [8.682092, 50.110913], // SNAPPED position, [lon, lat]
      "name": "Braubachstraße",
      "distance": 1.55506113           // metres from input coord to snapped road
    }
  ],
  "routes": [
    {
      "distance": 204881.5,            // float, METRES
      "duration": 8148.9,              // float, SECONDS (free-flow; NO live traffic)
      "weight": 8148.9,
      "weight_name": "routability",
      "geometry": {                    // because geometries=geojson
        "type": "LineString",
        "coordinates": [[8.682092, 50.110913], ...]  // 3089 pairs, [lon, lat]
      },
      "legs": [
        {
          "distance": 204881.5,
          "duration": 8148.9,
          "weight": 8148.9,
          "summary": "A 6, A 81",      // only populated when steps=true
          "annotation": {              // only when annotations= is set
            "distance": [8.046216758, 5.154577976, 3.054466322, ...], // metres
            "duration": [0.9, 0.6, 0.3, ...],                         // seconds
            "speed":    [8.9, 8.6, 10.2, ...]                         // see note
          },
          "steps": [ /* 41 RouteStep objects */ ]
        }
      ]
    }
  ]
}
```

### `annotation.*` — the critical indexing rule
Each array has length **`geometry.coordinates.length − 1`** (verified: **3088 vs 3089**). Entry `i` describes the **segment from coordinate `i` to coordinate `i+1`**, not a point. To colour a polyline by speed, build segments pairwise — do not zip annotations against coordinates 1:1 or you will be off by one and drop the final segment.

**Units warning on `speed`:** OSRM computes `speed = distance / duration` from metres and seconds, so the raw unit is **m/s**, rounded to one decimal. My sample values `[8.9, 8.6, 10.2]` on inner-city Frankfurt streets are consistent with m/s (≈32 km/h), **not** km/h. Multiply by 3.6 for km/h. (Some third-party docs claim km/h — the data does not support that.)

### `legs[].steps[]` — verified keys
`intersections`, `driving_side`, `geometry`, `maneuver`, `name`, `mode`, `weight`, `duration`, `distance`, plus optional `ref`, `pronunciation`, `destinations`, `exits`, `rotary_name`, `rotary_pronunciation`.

Real first step:
```json
{
  "driving_side": "right",
  "maneuver": { "bearing_after": 249, "bearing_before": 0,
                "location": [8.682092, 50.110913], "type": "depart" },
  "name": "Braubachstraße", "mode": "driving",
  "weight": 25.2, "duration": 25.2, "distance": 190.1
}
```
Real step with a road reference (use `ref` for autobahn badges — `A 6`, `A 81`, `K 818`):
```json
{ "maneuver": { "bearing_after": 351, "bearing_before": 352,
                "location": [8.679424, 50.11121],
                "modifier": "left", "type": "turn" },
  "ref": "K 818", "name": "Berliner Straße", "distance": 69.5 }
```
`maneuver.modifier` is **absent** on `depart`/`arrive` — guard for `undefined` when rendering turn icons. `intersections[i]` = `{ "out": 0, "entry": [true], "bearings": [249], "location": [lon, lat] }` (`in` appears on non-first intersections).

---

## C. Self-hosting with Docker (exact commands)

Image: **`ghcr.io/project-osrm/osrm-backend`**

```bash
# 0) Get the extract (329 MB)
wget https://download.geofabrik.de/europe/germany/hessen-latest.osm.pbf

# 1) EXTRACT  — profile path is INSIDE the container: /opt/car.lua
docker run -t -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend \
  osrm-extract -p /opt/car.lua /data/hessen-latest.osm.pbf

# 2a) PARTITION  (MLD pipeline — recommended)
docker run -t -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend \
  osrm-partition /data/hessen-latest.osrm

# 2b) CUSTOMIZE
docker run -t -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend \
  osrm-customize /data/hessen-latest.osrm

# 3) SERVE on :5000
docker run -t -i -p 5000:5000 -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend \
  osrm-routed --algorithm mld /data/hessen-latest.osrm
```

**CH alternative** (replace steps 2a+2b, then serve with `--algorithm ch`):
```bash
docker run -t -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend \
  osrm-contract /data/hessen-latest.osrm
docker run -t -i -p 5000:5000 -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend \
  osrm-routed --algorithm ch /data/hessen-latest.osrm
```

**Gotchas:**
- Step 1 takes `.osm.pbf`; steps 2–3 take **`.osrm`** (no `.pbf`). The `.osrm` is a *prefix* for ~15 sidecar files, not one file.
- Other profiles in the image: `/opt/bicycle.lua`, `/opt/foot.lua`.
- Local URL is identical in shape: `http://localhost:5000/route/v1/driving/8.6821,50.1109;9.1829,48.7758?...` — so you can swap hosts via one env var with zero code change.
- **MLD vs CH:** MLD preprocesses far faster and supports live traffic updates via `osrm-customize --segment-speed-file`; CH gives faster queries but rebuilding requires the slow contraction step. For a mobility app, MLD.

**Covering both Hessen AND Baden-Württemberg:** OSRM cannot serve two `.osrm` datasets from one process, and the two extracts do not share a graph — a Frankfurt→Stuttgart route needs one merged dataset. Two options:
1. **Simplest:** use `germany-latest.osm.pbf` (4.6 GB) — one extract, whole country, cross-border routing works.
2. **Leaner:** merge with `osmium-tool` first: `osmium merge hessen-latest.osm.pbf baden-wuerttemberg-latest.osm.pbf -o hessen-bw.osm.pbf`, then run the pipeline on that. Note the two regions are adjacent but the merged pair still clips routes that detour via Bayern or Rheinland-Pfalz.

*Unverified:* RAM/time figures. As rough guidance, `osrm-extract` is the memory-hungry step and roughly needs several times the PBF size in RAM; budget well over 8 GB for whole-Germany. Measure before committing.

---

## D. Tile sources — exact URLs and attribution

### 1. OpenFreeMap — RECOMMENDED
MapLibre style URLs (pass straight into the `style` option):
```
https://tiles.openfreemap.org/styles/positron   ← light
https://tiles.openfreemap.org/styles/dark       ← dark
https://tiles.openfreemap.org/styles/liberty
https://tiles.openfreemap.org/styles/bright
https://tiles.openfreemap.org/styles/fiord
```
(`/styles/3d` is **404** despite being listed on the homepage.)

```js
const map = new maplibregl.Map({
  container: 'map',
  style: 'https://tiles.openfreemap.org/styles/dark',
  center: [8.6821, 50.1109],   // Frankfurt, [lon, lat]
  zoom: 11
});
```
- **Key:** none. **Limits:** none stated. **Commercial:** allowed. **Licence:** MIT (project); data ODbL via OSM; unmodified OpenMapTiles schema.
- **Attribution: automatic in MapLibre** — the TileJSON at `https://tiles.openfreemap.org/planet` carries it, and MapLibre's `AttributionControl` renders it. Do not hand-roll it unless you disable the control.
- Verified TileJSON attribution string:
  `<a href="https://openfreemap.org" target="_blank">OpenFreeMap</a> <a href="https://www.openmaptiles.org/" target="_blank">&copy; OpenMapTiles</a> Data from <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a>`
- Plain-text form (required for print/video/non-MapLibre clients): `OpenFreeMap © OpenMapTiles Data from OpenStreetMap` — the "OpenFreeMap" part is optional but appreciated.

### 2. VersaTiles — recommended fallback
```
https://tiles.versatiles.org/assets/styles/colorful/style.json
https://tiles.versatiles.org/assets/styles/eclipse/style.json     ← dark
https://tiles.versatiles.org/assets/styles/shadow/style.json      ← dark
https://tiles.versatiles.org/assets/styles/graybeard/style.json   ← neutral grey
https://tiles.versatiles.org/assets/styles/neutrino/style.json
```
Attribution is embedded at source level (auto-rendered by MapLibre):
`© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors`
Shortbread schema, maxzoom 14, no API key. Raw tiles: `https://tiles.versatiles.org/tiles/osm/{z}/{x}/{y}` (no file extension).

### 3. OSMF official vector tiles — keyless, no style included
```
https://vector.openstreetmap.org/shortbread_v1/tilejson.json
https://vector.openstreetmap.org/shortbread_v1/{z}/{x}/{y}.mvt
```
Attribution: `<a href="https://www.openstreetmap.org/copyright">© OpenStreetMap</a>`. You must author or source your own MapLibre style (Shortbread schema, maxzoom 14).

### 4. OSM Standard raster — permitted for dev, but light-only
```js
{ version: 8,
  sources: { osm: { type: 'raster', tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
                    tileSize: 256, maxzoom: 19,
                    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' } },
  layers: [{ id: 'osm', type: 'raster', source: 'osm' }] }
```
Attribution: `© OpenStreetMap contributors`. Requires an app-identifying User-Agent (browsers send this automatically; native/server clients must set it) and a valid `Referer` — so avoid `<meta name="referrer" content="no-referrer">` on the page.

### 5. OSM Deutschland (German style) raster
```
https://tile.openstreetmap.de/{z}/{x}/{y}.png
```
Verified 200 with `Access-Control-Allow-Origin: *` and a ~3-day cache header. **Use the bare host — `tile-a/-b/-c.openstreetmap.de` do NOT resolve**, so do not use an `{s}` subdomain template. Attribution: `© OpenStreetMap contributors`. German road colours/symbols (Shell-Atlas style) with German/Latin-script localisation — a genuine plus for a German-market product, but raster and light-only.

### 6. CARTO — NOT recommended for a zero-cost build
```
https://basemaps.cartocdn.com/gl/positron-gl-style/style.json
https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json
https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json
https://{a-d}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png      (raster)
https://{a-d}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png     (raster)
```
All verified 200 **without** a key today, but policy now requires one. Attribution if used:
`&copy; <a href="https://carto.com/about-carto/">CARTO</a>, &copy; <a href="http://www.openstreetmap.org/about/">OpenStreetMap</a> contributors`

### Verified endpoints

| status | URL | purpose |
|---|---|---|
| `live` | <https://router.project-osrm.org/route/v1/driving/8.6821,50.1109;9.1829,48.7758?overview=full&geometries=geojson&steps=true&annotations=distance,duration,speed> | THE reference OSRM query - full GeoJSON geometry + steps + per-segment annotations |
| `live` | <https://routing.openstreetmap.de/routed-car/route/v1/driving/8.6821,50.1109;9.1829,48.7758?overview=false> | FOSSGIS car profile - same infrastructure as the OSRM demo server, better documented |
| `live` | <https://routing.openstreetmap.de/routed-bike/route/v1/driving/8.6821,50.1109;9.1829,48.7758?overview=false> | FOSSGIS bike profile |
| `live` | <https://routing.openstreetmap.de/routed-foot/route/v1/driving/8.6821,50.1109;9.1829,48.7758?overview=false> | FOSSGIS foot profile |
| `live` | <https://github.com/Project-OSRM/osrm-backend/wiki/Api-usage-policy> | OSRM demo server usage policy: rate limit, non-commercial restriction, UA requirement |
| `live` | <https://github.com/Project-OSRM/osrm-backend/wiki/Demo-server> | Confirms demo server hostnames, FOSSGIS sponsorship, available profiles |
| `live` | <https://routing.openstreetmap.de/about.html> | FOSSGIS usage policy and attribution requirements |
| `live` | <https://github.com/Project-OSRM/osrm-backend/blob/master/docs/http.md> | Authoritative OSRM HTTP API spec: all query params, RouteObject/RouteLeg/RouteStep/Annotation shapes |
| `live` | <https://github.com/Project-OSRM/osrm-backend> | Official Docker image name and the exact extract/partition/customize/routed command sequence |
| `live` | <https://download.geofabrik.de/europe/germany/hessen-latest.osm.pbf> | Hessen OSM extract for local OSRM build |
| `live` | <https://download.geofabrik.de/europe/germany/baden-wuerttemberg-latest.osm.pbf> | Baden-Wuerttemberg OSM extract for local OSRM build |
| `live` | <https://download.geofabrik.de/europe/germany-latest.osm.pbf> | Whole-Germany extract - simplest option if you need both Hessen and BW plus cross-border routing |
| `live` | <https://tiles.openfreemap.org/styles/positron> | RECOMMENDED light basemap style for MapLibre - pass directly as style URL |
| `live` | <https://tiles.openfreemap.org/styles/dark> | RECOMMENDED dark basemap style for MapLibre |
| `live` | <https://tiles.openfreemap.org/styles/liberty> | OpenFreeMap full-colour street style (OSM Liberty fork) |
| `live` | <https://tiles.openfreemap.org/styles/bright> | OpenFreeMap bright colourful style (OSM Bright fork) |
| `live` | <https://tiles.openfreemap.org/styles/fiord> | OpenFreeMap alternative dark/blue-grey style |
| `live` | <https://tiles.openfreemap.org/planet> | OpenFreeMap TileJSON - carries the attribution string MapLibre auto-renders |
| `live` | <https://openfreemap.org/> | OpenFreeMap terms: cost, keys, limits, commercial use, licence, attribution |
| `live` | <https://openfreemap.org/quick_start/> | OpenFreeMap MapLibre integration snippet |
| `live` | <https://tiles.versatiles.org/assets/styles/colorful/style.json> | VersaTiles colourful style - keyless backup provider |
| `live` | <https://tiles.versatiles.org/assets/styles/eclipse/style.json> | VersaTiles dark style - keyless dark backup |
| `live` | <https://tiles.versatiles.org/assets/styles/graybeard/style.json> | VersaTiles neutral greyscale style - good for data-heavy dashboards |
| `live` | <https://tiles.versatiles.org/assets/styles/neutrino/style.json> | VersaTiles minimal style |
| `live` | <https://tiles.versatiles.org/assets/styles/shadow/style.json> | VersaTiles second dark style |
| `live` | <https://tiles.versatiles.org/tiles/osm/12/2138/1376> | Proof VersaTiles serves real vector tiles keyless (z12 Frankfurt) |
| `live` | <https://vector.openstreetmap.org/shortbread_v1/tilejson.json> | Official OSMF vector tiles TileJSON - keyless, but no style provided |
| `live` | <https://vector.openstreetmap.org/shortbread_v1/12/2138/1376.mvt> | Proof OSMF vector tiles serve keyless |
| `live` | <https://operations.osmfoundation.org/policies/tiles/> | OSM Standard raster tile usage policy - what a low-traffic dev app may do |
| `live` | <https://operations.osmfoundation.org/policies/vector/> | OSMF Vector Tile Usage Policy |
| `live` | <https://tile.openstreetmap.org/12/2138/1376.png> | OSM Standard raster tile (z12 Frankfurt) - proves keyless raster availability |
| `live` | <https://tile.openstreetmap.de/12/2138/1376.png> | OSM Deutschland German-style raster tile |
| `live` | <https://basemaps.cartocdn.com/gl/positron-gl-style/style.json> | CARTO Positron vector style - keyless TODAY but policy now requires a key |
| `live` | <https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json> | CARTO Dark Matter vector style - same caveat |
| `live` | <https://a.basemaps.cartocdn.com/dark_all/12/2138/1376.png> | CARTO dark raster tile - proves CDN still serves keyless despite policy |
| `live` | <https://carto.com/basemaps> | CARTO stated terms - the reason to avoid CARTO for a zero-cost build |

### Corrections applied by the verifier

- `impact` does NOT always have exactly 3 keys. The report claims `{lower, upper, symbols}` — 'exactly these 3 keys'. FALSE: 1035/3945 roadworks+closure items (26%) carry ONLY `symbols`; `lower` and `upper` are absent entirely. Example live payload: `{"symbols": ["SEPARATE", "ARROW_UP"]}`. It is NOT predictable from display_type — ROADWORKS splits 1440 with lower/upper vs 421 without. `item['impact']['lower']` will KeyError on a quarter of all items.
- `impact.symbols` can contain literal `null` ELEMENTS inside the array. Observed in 9 items, e.g. `["ARROW_DOWN","BORDER_LEFT",null,"ARROW_UP","SEPARATE","CLOSED","CLOSED","BORDER_RIGHT","CLOSED"]`. Not mentioned in the report. Any `s.upper()`/`s.startswith()` over symbols crashes on AttributeError. (`symbols` itself is never missing/None/empty — 0 occurrences of each.)
- `electric_charging_station.coordinate` is WRONG in the report, and backwards for the majority of records. The report states it is GeoJSON Point, 'NOT {lat,long}'. Live nationwide (n=508): 447 records (88%) use `{"lat":float,"long":float}` and only 61 (12%) use GeoJSON `{"type":"Point","coordinates":[lon,lat]}`. BOTH shapes appear inside a single road's response. The split is perfectly correlated: GeoJSON-Point records ⟺ have `startTimestamp` ⟺ lack `extent` and `point` (61/61 each way). So charging stations are two distinct record sub-types, not one.
- `parking_lorry` and `electric_charging_station` have NO `geometry` field at all — 0/1799 and 0/508 nationwide. The report's suggested normalised model lists `geometry: GeoJSON | null` as if generally available. Only roadworks/closure/warning carry `geometry` (always `LineString`, 3313/632/49, coordinate arrays 2–2490 points, never zero-length).
- `parking_lorry` also has no `extent`, no `point`, and no `startTimestamp` — 0/1799 for all three. Its full field set is exactly: identifier, icon, isBlocked, future, startLcPosition, display_type (always `PARKING`), subtitle, title, coordinate (always GeoJSON Point), description, routeRecommendation, footer, lorryParkingFeatureIcons.
- Warning's `abnormalTrafficType` / `delayTimeValue` / `averageSpeed` are ABSENT keys, never `null`. The report presents them as 'plus' fields and tabulates `abnormalTrafficType ... null 7`. Live (n=49): abnormalTrafficType absent=6 null=0; delayTimeValue absent=7 null=0; averageSpeed absent=26 null=0. Critically, `averageSpeed` is missing on 26/49 (53%) — the report says 'missing on 5/16 in sample', understating it by half. `source` is the only one always present (49/49).
- Warning `startTimestamp` is NOT always Zulu. Report: 'startTimestamp here is Zulu'. Live: 45 Zulu + 4 ISO-with-offset. The undocumented `source` field determines it exactly: `source="inrix"` (45) → `2026-09-14T07:05:00Z`; `source="eva"` (4) → `2026-09-09T14:43:00+02:00`. `source` vocabulary is `inrix`/`eva` — the report never documents this field's values despite listing it in the schema. `eva` records also never carry averageSpeed or abnormalTrafficType.
- `startTimestamp` omission is NOT 'perfectly correlated with display_type' as a general rule. It holds only for roadworks (ROADWORKS 1861/1861 present; SHORT_TERM_ROADWORKS 0/1452 present). For closures it is mixed and unpredictable: CLOSURE 39 present / 54 absent; CLOSURE_ENTRY_EXIT 390 present / 149 absent. Do not use display_type to decide whether to read the key.
- NEW TRAP the report misses: `/details/{service}/{badId}` returns HTTP 200 with a ZERO-BYTE body and `Content-Type: application/json`. `json.loads('')` raises JSONDecodeError. This is different from — and crashier than — the empty-array behaviour the report documents for `/services/` paths. Verified: `GET /details/roadworks/bogus-xyz` → 200, size 0.
- Road ids are case-sensitive and unvalidated. `A9` returns 96 roadworks; `a9` returns `{"roadworks":[]}` with HTTP 200; a fully invented `A9999` also returns `{"roadworks":[]}` HTTP 200. Not mentioned in the report. Combined with the empty-array trap, nothing about a 200 tells you the road id was valid.
- `parking_lorry.title` contains the literal string 'undefined' in 1799/1799 records (100%), not 'often' as the report says. Every single title is of the form `A1 | undefined` or `A1 | undefined | <name>`. Same for charging stations. Sanitising is mandatory, not optional.
- `lorryParkingFeatureIcons` is empty in 100% of records across every endpoint including `parking_lorry` itself (0/1799 non-empty). The report notes it empty for roadworks but implies it is meaningful for parking. It is dead everywhere. Same for `footer` on roadworks (0/3313 non-empty) and `routeRecommendation` (0/3313 — this one the report got right).
- Nationwide volumes for the two POI layers were extrapolated from 5 roads and are far off. Report: parking_lorry '~520', electric_charging_station '~170'. Actual full 108-road sweep: parking_lorry = 1799, electric_charging_station = 508. Also `display_type` for charging stations is STRONG_ELECTRIC_CHARGING_STATION 489 / ELECTRIC_CHARGING_STATION 19 (the report implies a more even split).
- Minor — response headers are not 'only' the three listed. Live headers also include `Access-Control-Allow-Credentials: true`, `Access-Control-Expose-Headers: Content-Range`, and `Server: nginx/1.18.0 (Ubuntu)`. Note `Access-Control-Allow-Origin: *` together with `Allow-Credentials: true` is an invalid CORS combination — browsers reject credentialed requests against it. Fine for ordinary no-credentials fetches, so the 'callable from browser JS' conclusion still stands.
- Report understates its own evidence: `details/closure/{id}` and `details/warning/{id}` are listed as verified_live=false / 'not individually fetched'. I fetched both — HTTP 200, 22 fields each, same shape as details/roadworks. They can be moved out of `unconfirmed`.
- Live counts drift (expected, not an error, but do not hardcode them in tests): report roadworks 3327 / closure 648 / warning 52; my sweep 6 hours later 3313 / 632 / 49. `abnormalTrafficType` vocabulary also drifted — report saw HEAVY_TRAFFIC(1), I saw none; I saw QUEUING_TRAFFIC 24, SLOW_TRAFFIC 15, UNSPECIFIED_ABNORMAL_TRAFFIC 4. Treat the vocabulary as open-ended.

### Recommendation for AutoTwin DE

## Concrete implementation for AutoTwin DE

### Tiles — use OpenFreeMap, with a one-line light/dark swap

```js
const STYLES = {
  light: 'https://tiles.openfreemap.org/styles/positron',
  dark:  'https://tiles.openfreemap.org/styles/dark',
};

const map = new maplibregl.Map({
  container: 'map',
  style: STYLES[theme],
  center: [8.6821, 50.1109],
  zoom: 11,
  attributionControl: { compact: true },   // attribution comes from TileJSON automatically
});

// Theme toggle — preserves camera; re-add your own sources/layers on styledata
function setTheme(next) {
  map.setStyle(STYLES[next]);
}
```

**Why OpenFreeMap over the alternatives:** it is the only provider that is simultaneously (a) genuinely keyless with no registration, (b) explicit that there are **no limits on views or requests**, (c) explicit that commercial use is allowed, and (d) ships a matched `positron`/`dark` pair sharing identical sprites, glyphs and source schema — so a theme toggle is a one-line `setStyle` with no visual drift in icon or label rendering. OSM Standard and OSM DE are raster-only with no dark variant, which rules them out for a dark dashboard. OSMF vector has no style. CARTO now wants a key.

**Register a fallback.** OpenFreeMap is run by one person on donated funding. Wrap style loading so a failure falls through to VersaTiles (`.../eclipse/style.json` dark, `.../graybeard/style.json` light) — both verified keyless and ODbL. Listen for the `error` event on the map and swap `style` once.

### Routing — env-switched host, self-hosted before launch

```js
const OSRM = import.meta.env.VITE_OSRM_URL ?? 'https://routing.openstreetmap.de/routed-car';

async function route(from, to) {           // from/to are [lon, lat]
  const coords = `${from[0]},${from[1]};${to[0]},${to[1]}`;
  const qs = 'overview=full&geometries=geojson&steps=true&annotations=distance,duration,speed';
  const r = await fetch(`${OSRM}/route/v1/driving/${coords}?${qs}`);
  const j = await r.json();
  if (j.code !== 'Ok') throw new Error(`OSRM: ${j.code}`);
  return j.routes[0];
}
```

Prefer **`https://routing.openstreetmap.de/routed-car`** over `router.project-osrm.org`: same FOSSGIS infrastructure and byte-identical results (I verified both return 204881.5 m / 8148.9 s), but it is better documented and gives you `routed-bike` and `routed-foot` on the same host — which a *mobility* app will want. Setting it via env var means moving to a self-hosted container later is a config change, not a code change.

**Non-negotiables while on the public server:**
1. **Throttle to 1 req/sec.** Serialise through a queue; no parallel fan-out. No rate-limit headers exist to warn you before a block.
2. **Debounce user input hard** (≥500 ms) — do not route on every keystroke or map drag.
3. **Cache aggressively.** Key on rounded coordinate pairs; routes are deterministic and there is no live traffic to invalidate.
4. **Set a real User-Agent** on any server-side call.
5. **Never ship this to production.** It is non-commercial-only, must not sit behind a paywall, and access is revocable without notice or reason.

### Self-host before any real traffic

Start with `hessen-latest.osm.pbf` (329 MB) for fast iteration, then switch to `germany-latest.osm.pbf` (4.6 GB) for full coverage. Use the **MLD** pipeline (`osrm-partition` + `osrm-customize`) — it preprocesses far faster than CH and is the only path that supports live traffic injection later via `osrm-customize --segment-speed-file`. Because the local endpoint is URL-identical, the cutover is one env var.

If you need both Hessen and Baden-Württemberg, do **not** build two datasets — OSRM serves one graph per process and a Frankfurt→Stuttgart route would fail across them. Use whole-Germany, or `osmium merge` the two extracts first.

### Attribution — put this in the map chrome

MapLibre renders the OpenFreeMap tile attribution automatically from TileJSON, but it will **not** credit OSRM. Add the routing credit yourself whenever a route is displayed. Exact text in the `attribution` field below.

### Watch out for these
- **`[lon, lat]` ordering** in OSRM coordinates, OSRM geometry, and MapLibre `center`/`LngLat` — all longitude-first. Most other tooling is lat-first. This is the highest-probability bug in the integration.
- **`annotation.speed` is m/s, not km/h.** Multiply by 3.6. Third-party docs claiming km/h are wrong; the Frankfurt city-street values (~8.9) only make sense as m/s.
- **`annotation.*` arrays are `n−1` long** (3088 for 3089 points) and describe segments. Build polyline segments pairwise when colouring by speed.
- **`maneuver.modifier` is absent on `depart`/`arrive`** — guard before indexing a turn-icon map.
- **Do not set `Referrer-Policy: no-referrer`** on pages using OSM/OSMF tiles; the policy requires a valid `Referer`.
- **Never hardcode the OpenFreeMap tile path** (`.../planet/20260906_080001_pt/...`) — that version string rotates. Always point at the style or TileJSON URL.
- **`/styles/3d` does not exist** (404) despite the OpenFreeMap homepage listing it.

### Implementation notes

## Verification summary

I re-fetched **every** `confirmed_urls` entry marked `verified_live=true`. **All returned HTTP 200 and matched their described purpose — `dead_urls` is genuinely empty.** Nothing requires a credit card, registration, login, or a paid tier. The zero-cost constraint is satisfied.

What reproduced *exactly*, independently:
- Keyless/anonymous access, `Access-Control-Allow-Origin: *`, no `WWW-Authenticate`, no `Cache-Control`.
- **Rate limits: 40 sequential requests in 9s → 40× HTTP 200, zero throttling, no `X-RateLimit-*`/`Retry-After`.** Identical to the report's numbers. A 108-road × 4-layer concurrent sweep (10 workers, 432 requests) finished in **6.1 seconds with 0 errors**.
- The empty-array trap: `/A1/services/bogus_endpoint_xyz` → `200 {"bogus_endpoint_xyz":[]}`. **`traffic_flow` does not exist** — confirmed, behaves identically to the bogus path.
- **Webcams: 0 nationwide across all 108 roads.** Confirmed. Do not build the feature.
- Roads list: 109 entries, 108 after strip, `"A60 "` trailing space present and returning 0 while `A60` returns 14. `A64a`/`A99a` legitimate.
- `isBlocked` is the string `"false"` in **3945/3945** — never `"true"`. `future` is a real bool. Use `"CLOSED" in impact.symbols` (3280/3945).
- OpenAPI spec: **no `license`, no `termsOfService`, no `securitySchemes`** (grepped, zero hits). Spec never mentions `geometry`/`impact`/`startLcPosition` (0 hits each) — live payload is ahead of spec. `https://www.autobahn.de/nutzungsbedingungen` → **404**, confirmed.
- Mobilithek PDF is real: 99 pages, and §"Zertifikatsbasierte M2M-Kommunikation" confirms X.509v3 certs issued by the operator, cert emailed to the org admin, **signing password sent by SMS**, client-cert mTLS. The blocker is real — do not attempt it.
- `mobilithek.info/offers/...` is a 1102-byte SPA shell with zero offer content. The licence genuinely cannot be read there. **Treat the Autobahn licence as UNDECLARED** — the report is honest about this and I could not improve on it.

Base64 identifiers confirmed broken (200, zero bytes); raw and URL-encoded identifiers both work.

---

## Python adapter guidance

### Exact URLs (all verified by me today)
```python
BASE = "https://verkehr.autobahn.de/o/autobahn"
f"{BASE}/"  # {"roads": [...]}
f"{BASE}/{road}/services/roadworks"  # {"roadworks": [...]}
f"{BASE}/{road}/services/closure"  # {"closure": [...]}
f"{BASE}/{road}/services/warning"  # {"warning": [...]}
f"{BASE}/{road}/services/parking_lorry"  # {"parking_lorry": [...]}
f"{BASE}/{road}/services/electric_charging_station"  # {"electric_charging_station": [...]}
f"{BASE}/details/{service}/{identifier}"  # raw or quote(id, safe=''); NEVER base64
```
Skip `webcam` and anything traffic-flow shaped. `http://` 301-redirects to https — always call https directly.

### The five defensive rules that actually matter

**1. Never trust a 200.** Validate structurally:
```python
def fetch_layer(road: str, service: str) -> list[dict]:
    r = session.get(f"{BASE}/{road}/services/{service}", timeout=30)
    r.raise_for_status()
    if not r.content:  # /details/ with a bad id → 200, zero bytes
        return []
    try:
        payload = r.json()
    except ValueError:
        log.warning("non-JSON body for %s/%s", road, service)
        return []
    if service not in payload:  # unknown path echoes the last segment
        raise ProviderSchemaError(f"missing key {service!r}")
    return payload[service] or []
```
Startup smoke test: `fetch_layer("A9", "roadworks")` must return `> 0` items. That is the only thing distinguishing "API healthy" from "API silently returning nothing".

**2. Road ids: strip, dedupe, preserve case.**
```python
roads = sorted({r.strip() for r in get_json(f"{BASE}/")["roads"]})  # 108
```
`a9` (lowercase) returns an empty array with a 200. Never `.upper()` — `A64a`/`A99a` need their lowercase suffix.

**3. Coordinates — dispatch on shape, never on endpoint.** This is the correction that matters most; the report's per-endpoint table is wrong for charging stations.
```python
def to_lat_lon(coord: dict | None) -> tuple[float, float] | None:
    if not coord:
        return None
    if coord.get("type") == "Point":  # GeoJSON: [lon, lat] — LON FIRST
        lon, lat = coord["coordinates"][:2]
        return float(lat), float(lon)
    if "lat" in coord:  # note the key is "long", not lng/lon
        return float(coord["lat"]), float(coord["long"])
    return None
```
Both shapes occur **inside one `electric_charging_station` response** (447 `lat/long` vs 61 GeoJSON nationwide). `extent`/`point` are comma-joined **lat-first** strings; `geometry.coordinates` is GeoJSON **lon-first**. I verified the axis order directly: `coordinate.lat == float(point.split(",")[0])` and `geometry.coordinates[0] == [long, lat]`.

**4. `impact` — every subfield is optional.**
```python
impact = item.get("impact") or {}
symbols = [s for s in (impact.get("symbols") or []) if s]  # drops embedded nulls
lanes_closed = "CLOSED" in symbols
lower, upper = impact.get("lower"), impact.get("upper")  # absent on 26% of items
```
`warning` has **no `impact` key at all** (0/49).

**5. Timestamps — three formats, and the key may be absent.**
```python
from datetime import datetime, date


def parse_ts(raw):
    if not raw:  # covers absent (via .get) and null (details endpoint)
        return None
    if raw.endswith("Z"):  # warning, source="inrix"
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    try:
        return datetime.fromisoformat(raw)  # roadworks/closure, warning source="eva"
    except ValueError:
        return datetime.combine(
            datetime.strptime(raw, "%d.%m.%Y").date(), ...
        )  # charging: "30.03.2026"
```
Always `item.get("startTimestamp")` — in *list* responses the key is **missing**; in *detail* responses it is **present but may be null**. Do not use `display_type` to predict presence (holds for roadworks, fails for closures).

### Normalised model — adjust two fields from the report
Keep the report's `TrafficEvent`, but: `geometry` is `None` for `parking_lorry`/`electric_charging_station` (they have no such key at all), and add `source: str | None` for warnings — it is always present there and tells you the timestamp format and whether speed fields will exist.

Derived fields: `road` comes from the request URL, **not** the payload. `lanes_closed` from `impact.symbols`, never `isBlocked`. `is_future` from `future` — the only trustworthy boolean.

Sanitise on ingest: `subtitle.strip()` (leading space on 3275/3313, so not universal — strip unconditionally), and strip `undefined` segments out of titles: `" | ".join(p for p in title.split(" | ") if p != "undefined")`.

### Fetch strategy and fallback
- 8–12 workers across the 108 roads. I proved 10 workers / 432 requests / 6.1s / 0 errors. Set `User-Agent: <project>/<version> (+contact)`.
- Cache the merged snapshot server-side with a 2–5 min TTL; serve the UI from cache only.
- **Fallback ladder:** stale cache (serve with an `as_of` timestamp in the UI) → bundled fixture → empty result set with an explicit `degraded: true` flag. Never let a partial sweep look like "no roadworks in Germany". Because per-road failures are independent, count successful roads and mark the snapshot degraded if `< 90%` succeeded.
- Per-road failures should be logged and skipped, not fatal — my sweep had zero errors, but a national outage should degrade, not crash.

### Fixture data
Record real responses to disk and replay them; do not hand-write fixtures. Minimum set to make the tests meaningful — each targets a specific trap I confirmed live:
1. `roadworks_A9.json` — real response, must include both `ROADWORKS` (has `startTimestamp`) and `SHORT_TERM_ROADWORKS` (key absent).
2. `impact_symbols_only.json` — an item whose `impact` is `{"symbols": [...]}` with no `lower`/`upper`.
3. `impact_null_symbol.json` — an item with `null` inside the `symbols` array.
4. `charging_mixed_coords.json` — a single road's `electric_charging_station` response containing **both** coordinate shapes (A1 works: 26 `lat/long` + 5 GeoJSON).
5. `warning_inrix.json` + `warning_eva.json` — Zulu vs `+02:00`, and the eva case missing `averageSpeed`/`abnormalTrafficType`.
6. `empty_layer.json` — `{"webcam": []}`.
7. `unknown_path.json` — `{"bogus_endpoint_xyz": []}`, asserting your validator *rejects* it.
8. `details_empty_body.txt` — a zero-byte body, asserting you return `[]`/`None` instead of raising.
9. `roads_list.json` — the raw 109-entry list with `"A60 "` intact, asserting you normalise to 108.

Assert on **shape and invariants**, never on counts — volumes drift materially within hours (roadworks moved 3327→3313 between the report's sweep and mine).

### Licence / attribution
Genuinely undeclared; the report's conclusion is correct and I could not improve on it. Ship `Quelle: Die Autobahn GmbH des Bundes (verkehr.autobahn.de)` in the UI footer, state in the README that no formal licence is published and that commercial use should be cleared with `kontakt@autobahn.de`. Do **not** assert "Datenlizenz Deutschland – Namensnennung 2.0" anywhere — that remains unconfirmed.

Scope honesty stands: **Bundesautobahnen only.** I re-checked the roads list — every one of the 108 entries is `A*`. The spec's own description says "Bundesstraßen", which is misleading. Keep everything behind a `TrafficProvider` interface; `devportal-test.autobahn.de` is live (200, sign-in gated) and is a real signal that a keyed portal may eventually replace the open endpoint.

### Unconfirmed / open questions

- OSRM demo server RATE LIMIT ENFORCEMENT: the documented limit is 1 request/second, but I confirmed the server exposes NO rate-limit headers (no X-RateLimit-*, no Retry-After). I did not stress-test it, so the actual throttling/blocking threshold and mechanism are unknown. Self-throttle; you will get no warning before a block.
- OSRM demo server LONG-TERM AVAILABILITY: policy explicitly states access may be withdrawn at any time without reason and gives no uptime/latency/data-freshness guarantee. Cannot be relied on beyond development.
- VERSATILES USAGE POLICY: no terms-of-service, rate-limit, or fair-use document found for the public tiles.versatiles.org server. versatiles.org states 'requires no API keys' and docs.versatiles.org is 'Released under Unlicense', but neither page publishes server usage terms. Tiles and attribution verified live; the operating policy is NOT confirmed. Treat capacity as unknown before depending on it.
- tile.openstreetmap.de USAGE POLICY: no published usage policy, rate limit, or third-party-embedding permission found. The openstreetmap.de/germanstyle/ page is a history/background article containing no terms. The OSMF tile policy governs tile.openstreetmap.ORG and does not necessarily extend to the .DE server, which is separately operated (style maintained by Sven Geggus). Permission for third-party app use is UNCONFIRMED.
- CARTO KEYLESS ACCESS: live CDN returns HTTP 200 without a key (raster and vector style JSON, including with a third-party Referer), which CONTRADICTS carto.com/basemaps stating an API key is required. I could not determine whether keyless access is deprecated-but-tolerated legacy behaviour or an unenforced policy. Assume it can be revoked without notice; do not build on it.
- CARTO EXACT ATTRIBUTION REQUIREMENT: I extracted the attribution string embedded in CARTO's TileJSON, but could not locate CARTO's formal Terms of Service text specifying attribution obligations. The carto.com/basemaps page links to legal documents without quoting them.
- OPENFREEMAP '3d' STYLE: listed on the openfreemap.org homepage but https://tiles.openfreemap.org/styles/3d returns HTTP 404. I also tested 3D, positron-3d, liberty-3d, dark-3d - all 404. The correct URL (if one exists) is unknown; possibly unreleased or renamed.
- OSRM HARDWARE REQUIREMENTS: I did NOT verify RAM or wall-clock time for osrm-extract/partition/customize on the Hessen, Baden-Wuerttemberg, or Germany extracts. My guidance (osrm-extract is the memory-hungry step; budget well over 8 GB for whole-Germany) is general knowledge, not measured. Benchmark before committing to an instance size.
- OSMIUM MERGE WORKFLOW: the 'osmium merge hessen-latest.osm.pbf baden-wuerttemberg-latest.osm.pbf' approach for combining two regional extracts is standard practice but I did NOT execute it, and osmium-tool is a separate install not bundled in the OSRM Docker image.
- FOSSGIS THIRD-PARTY APP PERMISSION: routing.openstreetmap.de/about.html states the usage policy (1 req/sec, no scraping, valid UA/referrer, attribution + 'fix the map' link) but does not explicitly address whether third-party applications may use the service. The OSRM wiki's non-commercial restriction is the operative constraint.
- OPENFREEMAP FINANCIAL SUSTAINABILITY: run by a single individual (Zsolt Ero) funded by GitHub Sponsors and optional support plans, with a possible future Pro plan. The 'no limits' promise is current policy, not a contractual guarantee. This is the main non-technical risk in the recommendation - hence the VersaTiles fallback.
- GEOFABRIK FILE SIZES ARE A MOVING TARGET: sizes were read from live Content-Length on 2026-09-14 (extracts re-cut that morning). Geofabrik rebuilds daily and OSM data grows steadily, so these figures drift upward over time.

---

## 2. Bundesnetzagentur — Ladesäulenregister

**Verification verdict:** `PARTIAL`

**Licence:** **Creative Commons Namensnennung 4.0 International (CC BY 4.0)** — confirmed verbatim on the official Ladesäulenkarte landing page:

> "Die Daten sind durch eine Creative Commons Namensnennung 4.0 International Lizenz lizenziert."

Required attribution string, also verbatim from the page:

> "Als Namensnennung für Daten dieser Internetseite ist **Bundesnetzagentur.de** zu verwenden."

Reuse terms, verbatim:

> "Die auf der Ladesäulenkarte und in der Liste veröffentlichten Registerdaten stehen der Öffentlichkeit zur freien Verfügung und Verwendung: Die Daten können kostenfrei heruntergeladen und gespeichert werden."

So: free download, free reuse including commercial, **attribution to `Bundesnetzagentur.de` is mandatory**. It is CC BY 4.0, **not** dl-de/by-2-0 — despite dl-de/by-2-0 being the usual default for German federal open data and being what several third-party mirrors label it as. AutoTwin DE should carry the CC BY 4.0 notice and the exact `Bundesnetzagentur.de` attribution string in its own dataset metadata and any user-facing credits.

**Authentication:** **No.** For the CSV/XLSX file downloads there is **no registration, no API key, no login, no cookie and no Referer check**. I fetched both files anonymously with plain `curl` (no headers beyond a default UA) and got HTTP 200 plus real content. The server sets `bnetza_cookie` / `TS01b2d567` session cookies in the response, but they are not required to be presented on the request. The host supports `Accept-Ranges: bytes`, so partial/resumable downloads work.

Two auth-gated things exist but are **not** needed for read-only ingestion:
- The **public REST interface**: no login as such, but BNetzA does not publish the endpoint or spec — you must email `ladesaeulenregister@bnetza.de`, and they state they will "inform you of the requirements for use". So effectively gated behind a manual request.
- The **operator/service-provider Meldeportal APIs**: require a Betreiber- or Dienstleisterkonto. Only relevant if you are a charge point operator submitting data.
- The **ArcGIS FeatureServer** behind the map: requires a token (HTTP 499 `Token Required`). Not usable.


### Findings

## Headline

The current edition is **`Ladesaeulenregister_BNetzA_2026-09-01`** (Stand 1 September 2026), published as CSV (55,559,083 bytes) and XLSX (31,215,578 bytes) on `data.bundesnetzagentur.de`. Both URLs return **HTTP 200** and I verified them directly (HEAD + HTTP Range reads of the real bytes, not just search snippets).

## Five things that will break a naive implementation

1. **The old `SharedDocs/Downloads/.../Ladesaeulenregister.xlsx?__blob=publicationFile` URL is DEAD (HTTP 404).** Many blog posts, tutorials and older scrapers still reference it. BNetzA moved the files to a separate `data.bundesnetzagentur.de` host. I confirmed the 404 directly.

2. **There is NO stable/undated "latest" URL.** The filename embeds the edition date. I probed and confirmed:
   - `Ladesaeulenregister.csv` → 404
   - `Ladesaeulenregister_BNetzA.csv` → 404
   - `Ladesaeulenregister_BNetzA_2026-08-01.csv` → **404** (previous month is deleted, not archived)
   - `Ladesaeulenregister_BNetzA_2026-10-01.csv` → 404 (not yet published)
   
   So AutoTwin DE **must scrape the landing page** for the current filename. Good news: the landing-page HTML contains the absolute URL in plain text, so a one-line regex works (see Recommendation).

3. **The encoding is UTF-8 with BOM — NOT cp1252/latin-1.** This is the single most common wrong assumption about this dataset, and it *used* to be true for older editions. The current file starts with bytes `EF BB BF`. Use `utf-8-sig`. If you use `cp1252` you get mojibake on every `ä/ö/ü/ß`; if you use plain `utf-8` you get a stray `﻿` glued onto the first column name (`'﻿Ladeeinrichtungs-ID'`), which silently breaks column lookups.

4. **There are 10 junk lines before the header, not a simple 1-row preamble.** Lines 1–9 are a German-language notice block, line 10 is a *merged-cell group banner* (`Allgemeine Informationen` … `1. Ladepunkt` … `6. Ladepunkt`), and **line 11 is the real header**. In pandas that is `skiprows=10`.

5. **Quoted fields contain the delimiter.** The delimiter is `;`, and several fields are `;`-separated *lists inside quotes*, e.g. `"RFID-Karte;Onlinezahlungsverfahren"`, `"Montag; Dienstag; Mittwoch; …"`, and connector power like `"11; 3,7"`. A naive `line.split(';')` produces a different column count per row (I measured 46 semicolons on header lines vs 59 on some data lines). **You must use a real RFC4180 CSV parser** with `quotechar='"'`.

## Format facts (all verified against the actual bytes)

| Property | Value |
|---|---|
| Delimiter | `;` (semicolon) |
| Encoding | **UTF-8 with BOM** (`EF BB BF`) → `utf-8-sig` |
| Line endings | **CRLF** (`\r\n`) |
| Preamble rows | **10** (9 notice lines + 1 group-banner row) |
| Header row | line 11 (0-based index **10**) → `skiprows=10` |
| Column count | **47** |
| Decimal separator | **comma** — `48,442398`, `7,2` |
| Date format | **`DD.MM.YYYY`** — `11.01.2020` |
| Quoting | `"` , embedded `;` inside quoted fields |
| Content-Type | `application/octet-stream` (CSV) |
| Last-Modified | Thu, 03 Sep 2026 07:39:47 GMT |

## Value domains (sampled ~3,250 real rows)

- `Status`: only **`In Betrieb`** observed across every sample. (Whether other values like `Außer Betrieb` exist is unconfirmed — see Unconfirmed.)
- `Art der Ladeeinrichtung`: **`Normalladeeinrichtung`** (~75%), **`Schnellladeeinrichtung`** (~25%)
- `Anzahl Ladepunkte`: `1`–`4` in sample (schema supports up to **6**)
- `Steckertypen1`: `AC Typ 2 Steckdose`, `DC Fahrzeugkupplung Typ Combo 2 (CCS)`, `AC Typ 2 Fahrzeugkupplung`, `AC Schuko`, `DC CHAdeMO` — multi-valued cells use `; ` separator inside quotes
- `Breitengrad` / `Längengrad`: WGS84 decimal degrees with **comma** decimal separator

## Row count

**Estimated ~117,000 data rows** (one row = one *Ladeeinrichtung* / station, not per charging point). This is a byte-sampling estimate, not an exact count — I sampled 6 × 256 KB windows spread across the file, pooled average 475.0 bytes/line, over 55,559,083 bytes. Per-window estimates ranged 103k–126k, so treat this as **~110,000–130,000**. I deliberately did not download the full 55 MB file to get an exact count.

For cross-reference, BNetzA's own published statistics (Stand 1 Aug 2026) give **156,399 Normalladepunkte + 55,665 Schnellladepunkte = 212,064 charging points** and **9.22 GW** total capacity — consistent with ~117k stations at ~1.8 points/station.

## API situation — weaker than it looks

- A **public REST interface does exist** (JSON + XML, updated **once daily** — more current than the monthly file), but BNetzA **does not publish the endpoint URL or the OpenAPI spec on the website**. You must email `ladesaeulenregister@bnetza.de` and they send the OpenAPI description plus "die Voraussetzungen für die Nutzung". So it is not self-service.
- The **operator/service-provider REST APIs** (import/export, registering new charge points) require a *Betreiber- oder Dienstleisterkonto* in the Meldeportal — irrelevant for read-only ingestion.
- The **ArcGIS FeatureServer backing the official map** (`services6.arcgis.com/6jU7RmJig2Wwo1b0/.../Ladesaeulenregister/FeatureServer/7/query`) is cited in several community write-ups as a public API. **It is not** — I queried it and it returns `{"code": 499, "message": "Token Required"}`. Do not build on it.
- **No OGC WFS/WMS** endpoint is offered by BNetzA for this dataset.

## Third-party mirrors on open-data portals (NOT BNetzA-operated)

GovData does **not** host a BNetzA-published distribution. The two GovData hits are republications by other bodies:
- `deutschland-e-ladesaulen` → data actually served by **Rhein-Kreis-Neuss** (Opendatasoft), last modified 17.06.2026. This *does* expose a real REST API with CSV/JSON/**GeoJSON**/GPX/KML/Shapefile/Parquet exports and no key — a usable fallback, but it is a **derived** copy with its own (flattened/renamed) schema and unknown lag.
- `liste-der-ladesaulen1dd35` → **Schleswig-Holstein** open-data mirror, last modified **11.03.2023** — badly stale, do not use.

I found no evidence BNetzA publishes this dataset on **mobilithek.info** itself.

### Schema / format details

## Physical layout of the CSV

```
line  1 : ﻿Ladesäulenregister Bundesnetzagentur;;;;...   <- BOM here
line  2 : (blank, all semicolons)
line  3 : Hinweis: ;;;;...
line  4 : Die Liste beinhaltet die Ladeeinrichtungen aller Betreiberinnen und Betreiber, die das Anzeigeverfahren der Bundesnetzagentur
line  5 : zum Zeitpunkt der Aktualisierung vollständig abgeschlossen haben.
line  6 : Die Zahl der öffentlich zugänglichen Ladeeinrichtungen in Deutschland ist daher größer als hier dargestellt.
line  7 : (blank)
line  8 : Letzte Aktualisierung vom: 01.09.2026
line  9 : (blank)
line 10 : Allgemeine Informationen;…;1. Ladepunkt;;;;2. Ladepunkt;;;;…;6. Ladepunkt;;;   <- GROUP BANNER, not the header
line 11 : <<< REAL HEADER — 47 columns >>>
line 12+: data
```

`skiprows=10` (0-based header index 10). The group banner on line 10 places its labels at 0-based column offsets: `0=Allgemeine Informationen`, `23=1. Ladepunkt`, `27=2. Ladepunkt`, `31=3.`, `35=4.`, `39=5.`, `43=6.` — i.e. **4 columns per charging point**.

## The 47 column headers, in exact file order

All 47 are **CONFIRMED verbatim** — I parsed them out of the actual downloaded bytes with a CSV reader, not from memory or documentation. Confidence: **certain** for every one.

| # | Exact header string |
|---|---|
| 1 | `Ladeeinrichtungs-ID` |
| 2 | `Betreiber` |
| 3 | `Anzeigename (Karte)` |
| 4 | `Status` |
| 5 | `Art der Ladeeinrichtung` |
| 6 | `Anzahl Ladepunkte` |
| 7 | `Nennleistung Ladeeinrichtung [kW]` |
| 8 | `Inbetriebnahmedatum` |
| 9 | `Straße` |
| 10 | `Hausnummer` |
| 11 | `Adresszusatz` |
| 12 | `Postleitzahl` |
| 13 | `Ort` |
| 14 | `Kreis/kreisfreie Stadt` |
| 15 | `Bundesland` |
| 16 | `Breitengrad` |
| 17 | `Längengrad` |
| 18 | `Standortbezeichnung` |
| 19 | `Informationen zum Parkraum` |
| 20 | `Bezahlsysteme` |
| 21 | `Öffnungszeiten` |
| 22 | `Öffnungszeiten: Wochentage` |
| 23 | `Öffnungszeiten: Tageszeiten` |
| 24 | `Steckertypen1` |
| 25 | `Nennleistung Stecker1` |
| 26 | `EVSE-ID1` |
| 27 | `Public Key1` |
| 28 | `Steckertypen2` |
| 29 | `Nennleistung Stecker2` |
| 30 | `EVSE-ID2` |
| 31 | `Public Key2` |
| 32 | `Steckertypen3` |
| 33 | `Nennleistung Stecker3` |
| 34 | `EVSE-ID3` |
| 35 | `Public Key3` |
| 36 | `Steckertypen4` |
| 37 | `Nennleistung Stecker4` |
| 38 | `EVSE-ID4` |
| 39 | `Public Key4` |
| 40 | `Steckertypen5` |
| 41 | `Nennleistung Stecker5` |
| 42 | `EVSE-ID5` |
| 43 | `Public Key5` |
| 44 | `Steckertypen6` |
| 45 | `Nennleistung Stecker6` |
| 46 | `EVSE-ID6` |
| 47 | `Public Key6` |

## Corrections to the column names guessed in the task brief

Several names in the request do **not** exist in the current file:

| Guessed in brief | Reality |
|---|---|
| `Art der Ladeeinrichung` (with typo) | **`Art der Ladeeinrichtung`** — the historic BNetzA typo has been **fixed**. Code special-casing the misspelling will now fail. |
| `Anschlussleistung` | does not exist → **`Nennleistung Ladeeinrichtung [kW]`** |
| `P1 [kW]`, `P2 [kW]` … | do not exist → **`Nennleistung Stecker1`** … `Stecker6` (no unit suffix in the name) |
| `Steckertypen1` | correct, exists as guessed |
| `Betreiber`, `Straße`, `Hausnummer`, `Postleitzahl`, `Ort`, `Bundesland`, `Breitengrad`, `Längengrad`, `Inbetriebnahmedatum`, `Anzahl Ladepunkte` | all correct, exist as guessed |

New columns not in the brief: `Ladeeinrichtungs-ID`, `Anzeigename (Karte)`, `Status`, `Adresszusatz`, `Kreis/kreisfreie Stadt`, `Standortbezeichnung`, `Informationen zum Parkraum`, `Bezahlsysteme`, the three `Öffnungszeiten*` columns, and the per-point `EVSE-ID*` / `Public Key*`.

## Real sample row (line 12, verbatim, truncated after the first connector block)

```
1010338;Albwerk Elektro- und Kommunikationstechnik GmbH;Albwerk Elektro- und Kommunikationstechnik GmbH;In Betrieb;Normalladeeinrichtung;2;22;11.01.2020;Am Berg;1;;72535;Heroldstatt;Landkreis Alb-Donau-Kreis;Baden-Württemberg;48,442398;9,659075;;Keine Beschränkung;"RFID-Karte;Onlinezahlungsverfahren";247;"Montag; Dienstag; …";"00:00-23:59; …";AC Typ 2 Steckdose;22;DEAEWE002501;CA49E2…
```

## Parsing gotchas, concretely

- `Bezahlsysteme` → `"RFID-Karte;Onlinezahlungsverfahren"` — quoted, `;`-joined list
- `Öffnungszeiten: Wochentage` → `"Montag; Dienstag; Mittwoch; …"` — quoted, `; `-joined
- `Öffnungszeiten` → literal values like `247` (meaning 24/7) or `Keine Angabe` — **not** a number, keep as string
- `Steckertypen1` → can hold multiple types: `AC Typ 2 Steckdose; AC Schuko`
- `Nennleistung Stecker1` → can hold multiple powers with decimal commas: `"11; 3,7"` — so this field is **not** safely numeric; parse as string, split on `;`, then replace `,`→`.`
- `Nennleistung Ladeeinrichtung [kW]` → mostly integer-looking (`22`, `44`, `150`, `400`) but decimal commas do occur (`7,2`)
- Trailing empty connector slots produce runs of consecutive `;;;;` — expected, not corruption
- `Adresszusatz`, `Standortbezeichnung`, `EVSE-ID*`, `Public Key*` are frequently empty

### Verified endpoints

| status | URL | purpose |
|---|---|---|
| `live` | <https://www.bundesnetzagentur.de/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/Ladesaeulenkarte/start.html> | CANONICAL landing page for the Ladesäulenregister dataset (map + downloads + licence statement). Scrape this for the current dated file URL. |
| `live` | <https://data.bundesnetzagentur.de/Bundesnetzagentur/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/Ladesaeulenregister_BNetzA_2026-09-01.csv> | EXACT direct CSV download, current edition (Stand 2026-09-01) |
| `live` | <https://data.bundesnetzagentur.de/Bundesnetzagentur/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/Ladesaeulenregister_BNetzA_2026-09-01.xlsx> | EXACT direct XLSX download, current edition (Stand 2026-09-01) |
| `live` | <https://www.bundesnetzagentur.de/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/start.html> | E-Mobilität overview page; carries the official headline statistics (charging point counts, GW) |
| `live` | <https://www.bundesnetzagentur.de/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/Schnittstellen/start.html> | Official page describing the public REST interface to the register |
| `live` | <https://www.bundesnetzagentur.de/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/Schnittstellen/artikel.html> | Detail page on the Meldeportal Webserviceschnittstellen (operator-facing APIs) |
| `unverified` | <https://www.bundesnetzagentur.de/SharedDocs/Downloads/DE/Sachgebiete/Energie/Unternehmen_Institutionen/E_Mobilitaet/Ladesaeulenregister.xlsx?__blob=publicationFile&v=26> | LEGACY URL still widely cited online — verified BROKEN, do not use |
| `unverified` | <https://services6.arcgis.com/6jU7RmJig2Wwo1b0/arcgis/rest/services/Ladesaeulenregister/FeatureServer/7/query> | ArcGIS FeatureServer behind the official map — cited by community sources as a public API; verified NOT publicly usable |
| `live` | <https://www.govdata.de/suche/daten/deutschland-e-ladesaulen> | GovData entry — third-party republication, useful only as a fallback with GeoJSON/REST |
| `live` | <https://www.govdata.de/suche/daten/liste-der-ladesaulen1dd35> | GovData entry — stale Schleswig-Holstein mirror, do NOT use |

**Dead / unusable URLs found during verification:**

- https://www.bundesnetzagentur.de/SharedDocs/Downloads/DE/Sachgebiete/Energie/Unternehmen_Institutionen/E_Mobilitaet/Ladesaeulenregister.xlsx?__blob=publicationFile&v=26 — UNUSABLE. Returns HTTP 200 (not 404, as the report claimed) with Content-Type text/html and a 57 KB HTML page titled 'HTTP Status 404'. Never serves the spreadsheet.
- https://services6.arcgis.com/6jU7RmJig2Wwo1b0/arcgis/rest/services/Ladesaeulenregister/FeatureServer?f=pjson — token-gated: {"code":499,"message":"Token Required","messageCode":"SB_0006"}
- https://services6.arcgis.com/6jU7RmJig2Wwo1b0/arcgis/rest/services/Ladesaeulenregister/FeatureServer/7/query?where=1%3D1&returnCountOnly=true&f=pjson — same 499 Token Required. Confirmed not publicly usable.
- https://data.bundesnetzagentur.de/Bundesnetzagentur/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/Ladesaeulenregister_BNetzA_2026-08-01.csv — HTTP 404 (previous edition deleted, confirmed)
- https://data.bundesnetzagentur.de/Bundesnetzagentur/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/Ladesaeulenregister_BNetzA_2026-07-01.csv — HTTP 404
- https://data.bundesnetzagentur.de/Bundesnetzagentur/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/Ladesaeulenregister_BNetzA_2026-06-01.csv — HTTP 404
- https://data.bundesnetzagentur.de/Bundesnetzagentur/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/Ladesaeulenregister_BNetzA_2026-10-01.csv — HTTP 404 (not yet published)
- https://data.bundesnetzagentur.de/Bundesnetzagentur/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/Ladesaeulenregister.csv — HTTP 404
- https://data.bundesnetzagentur.de/Bundesnetzagentur/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/Ladesaeulenregister_BNetzA.csv — HTTP 404
- https://data.bundesnetzagentur.de/Bundesnetzagentur/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/ — returns HTTP 200 but the body is a WAF rejection page ('Die angeforderte URL wurde abgewiesen'). No directory listing exists; you cannot enumerate editions this way.
- https://mobilithek.info/offers/842113170303512576 — REACHABLE BUT UNVERIFIABLE. Serves only a 1,102-byte JS SPA shell to any non-browser client; every /mdp-api/* path I tried returned the same shell instead of JSON. Publisher, licence, download URL and login requirement all UNCONFIRMED. Do not plan on it.
- NOTE: every entry in confirmed_urls marked verified_live=true was re-fetched by me and was genuinely live, public, anonymous and consistent with what the report claimed — the two data.bundesnetzagentur.de downloads, the Ladesaeulenkarte landing page, the E-Mobilitaet start page, both Schnittstellen pages, and both GovData pages. None of them belong in this list.

### Corrections applied by the verifier

- LEGACY URL STATUS IS WRONG. The report says https://www.bundesnetzagentur.de/SharedDocs/Downloads/DE/Sachgebiete/Energie/Unternehmen_Institutionen/E_Mobilitaet/Ladesaeulenregister.xlsx?__blob=publicationFile&v=26 'Returns HTTP 404'. It does NOT. It returns HTTP 200 with Content-Type: text/html;charset=utf-8 and a 57,757-byte HTML page whose <title> is 'Bundesnetzagentur - Homepage - HTTP Status 404'. It is a soft 404. Any adapter that relies on resp.raise_for_status() or status_code == 404 to detect breakage will silently ingest an HTML error page as if it were a spreadsheet.
- data.bundesnetzagentur.de SITS BEHIND A WAF THAT ALSO SOFT-FAILS. https://data.bundesnetzagentur.de/Bundesnetzagentur/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/ returns HTTP 200 with an HTML page reading 'Die angeforderte URL wurde abgewiesen. Ihre Support-ID ist: ...'. Combined with the point above: on BOTH bnetza hosts a 200 status does not mean success. Validate Content-Type == application/octet-stream AND the first three bytes == EF BB BF before parsing. (There is no directory listing on that host, so you cannot enumerate editions.)
- EXACT ROW COUNT IS NOW KNOWN, NOT AN ESTIMATE. I downloaded the full 55,559,083-byte CSV and parsed it: 116,454 CSV records total = 10 preamble rows + 1 header row + 116,443 data rows. Every single data row has exactly 47 fields (zero ragged rows). The report's '~117,000, honest range 110,000-130,000' is close but should be replaced with 116,443 for the 2026-09-01 edition.
- STATUS DOMAIN IS WRONG/INCOMPLETE. The report lists 'In Betrieb' only and flags the rest as unconfirmed. Full-file counts: 'In Betrieb' = 116,423, 'In Wartung' = 20. There is no 'Außer Betrieb' and no 'Geplant' in this edition. Do not filter on Status == 'In Betrieb' without deciding what to do with 'In Wartung'.
- ANZAHL LADEPUNKTE RANGE IS WRONG. The report says '1-4 in sample (schema supports up to 6)'. Actual full-file distribution: 2 = 78,762; 1 = 32,333; 4 = 2,808; 3 = 2,389; 6 = 125; 5 = 26. All six values genuinely occur.
- MISSING PARSING HAZARD — EMBEDDED BARE LF INSIDE QUOTED FIELDS. The report states 'Line endings: CRLF' and stops there. In fact the record terminator is CRLF (116,454 occurrences) but there are ALSO 30,217 bare LF bytes inside quoted fields: 15,113 fields across 8,305 distinct data rows contain a raw \n. Affected columns: EVSE-ID1 (5,181), Public Key1 (3,573), EVSE-ID2 (3,101), Public Key2 (2,792), Public Key3 (313), EVSE-ID3 (57), Adresszusatz (34), Public Key4 (28), EVSE-ID4 (22), EVSE-ID5 (5), EVSE-ID6 (5), Public Key5 (1), Public Key6 (1). Example value: 'DE*ARR*ESP165*001\n' and multi-line hex Public Keys. This means `for line in f`, `f.readlines()`, `text.split('\n')` and `wc -l` all give wrong answers. You MUST open with newline='' and use csv.reader, or use pandas (both verified working). Also: EVSE-IDs need .strip() — trailing newline/whitespace is common.
- CONNECTOR-TYPE DOMAIN IS INCOMPLETE. Report lists 5 values. Full-file atomic tokens in Steckertypen1: 'AC Typ 2 Steckdose' 75,512; 'DC Fahrzeugkupplung Typ Combo 2 (CCS)' 30,498; 'AC Typ 2 Fahrzeugkupplung' 12,911; 'AC Schuko' 2,034; 'DC CHAdeMO' 1,435; plus three the report missed: 'DC Megawatt Charging System (MCS)' 29, 'AC Typ 1 Steckdose' 9, 'AC CEE 5-polig' 2, 'DC Tesla Fahrzeugkupplung (Typ 2)' 1. Treat the vocabulary as open — map unknown tokens to a passthrough bucket, do not raise.
- ÖFFNUNGSZEITEN HAS EXACTLY THREE VALUES, ONE OF WHICH THE REPORT MISSED. 'Keine Angabe' 74,214; '247' 40,253; 'Eingeschränkt' 1,976. The report mentions only 247 and 'Keine Angabe'.
- BEZAHLSYSTEME SEPARATOR AND ORDER. The report's example "RFID-Karte;Onlinezahlungsverfahren" is real (it is literally row 1) but is not the modal form. Top values: 'Onlinezahlungsverfahren;RFID-Karte' 34,155; '' (empty) 12,009; 'Onlinezahlungsverfahren;RFID-Karte;Sonstige' 9,145; 'RFID-Karte;Onlinezahlungsverfahren' 7,709; 'Onlinezahlungsverfahren' 7,534; 'Kreditkarte (NFC);Debitkarte (NFC);Onlinezahlungsverfahren;RFID-Karte' 4,495. Note the separator here is ';' with NO space, whereas Steckertypen*, Öffnungszeiten: Wochentage and Nennleistung Stecker* use '; ' WITH a space. Split on r'\s*;\s*' and sort the tokens — the order is not stable across rows.
- THE RHEIN-KREIS-NEUSS 'FALLBACK' IS FAR WORSE THAN THE REPORT IMPLIES. Its Opendatasoft metadata (verified live) gives records_count = 61,793 — only 53% of BNetzA's 116,443 — and data_processed = 2025-05-11T22:00:08Z. The '17.06.2026' the report quotes is the metadata 'modified' timestamp, not the data date. Its field list is also a truncated OLD-schema flattening: ['betreiber','art_der_ladeeinrichung' (with the historic typo),'anzahl_ladepunkte','steckertypen1'..'steckertypen6','p1_kw'..'p6_kw','nennleistung_ladeeinrichtung_kw','kreis_kreisfreie_stadt','ort','postleitzahl','strasse','hausnummer','adresszusatz','inbetriebnahmedatum','koordinaten'{lat,lon},'anzeigename_karte','public_key5','public_key6'] — there is NO status, NO bundesland, NO bezahlsysteme, NO öffnungszeiten, NO evse_id*, and no public_key1..4. Do not describe it as a parity fallback.
- MOBILITHEK CLAIM IS NOT SAFE. The report says 'I found no evidence BNetzA publishes this dataset on mobilithek.info'. A Mobilithek offer titled 'Bundesnetzagentur Ladesäulenregister (aufbereitet)' does exist at https://mobilithek.info/offers/842113170303512576. I could NOT verify its publisher, licence, download URL or whether it needs a login — mobilithek.info is a JS-only SPA and every /mdp-api/* path I probed returns the 1,102-byte SPA shell, not JSON. Record this as unresolved, not as 'no evidence'.
- XLSX INTERNALS (report listed as unconfirmed) ARE NOW CONFIRMED, WITH ONE TRAP. The 31,215,578-byte XLSX is a single sheet named 'Ladesäulenregister', 116,454 rows x 47 columns, identical 10-row preamble and header on row 11. BUT Inbetriebnahmedatum comes back from openpyxl as a datetime (2020-01-11 00:00:00), not as the 'DD.MM.YYYY' string the CSV carries. Any code shared between the CSV and XLSX paths must branch on that.
- THE SCHLESWIG-HOLSTEIN MIRROR IS LIVE BUT IS NOT A GERMANY-WIDE DATASET. Real resource URL: https://opendata.schleswig-holstein.de/dataset/edaf2e55-097f-447a-ac0b-f91c939c4e4b/resource/09fb677a-e4ce-436a-ba32-d3c1b00f1dbd/download/ladesaeulenregister.csv — HTTP 200, 372,357 bytes, text/csv, ~1,567 rows, COMMA-delimited, UTF-8 with no BOM, no preamble. Its header is the 2023 schema: Betreiber,Straße,Hausnummer,Adresszusatz,Postleitzahl,Ort,Bundesland,Kreis/kreisfreie Stadt,Breitengrad,Längengrad,Inbetriebnahmedatum,Anschlussleistung,Art der Ladeeinrichung,Anzahl Ladepunkte,Steckertypen1,P1 [kW],Public Key1,...,P4 [kW],Public Key4. Its GovData licence metadata is 'Andere geschlossene Lizenz' with open:false — it is not even declared open data. Useless as a fallback. (Side value: this file is the origin of the column names guessed in the task brief, which confirms the report's correction table is right.)
- MINOR: the Art der Ladeeinrichtung split is 85,288 Normalladeeinrichtung (73.2%) vs 31,155 Schnellladeeinrichtung (26.8%), not 'roughly 75/25'. Also, the report's illustrative multi-power value "11; 3,7" is the right SHAPE but I did not observe that literal; real examples of multi-valued Nennleistung Stecker1 are '22; 22', '150; 150', '11; 11' (5,751 rows), and single decimal-comma values are '16,7','3,6','21,9' (2,891 rows).
- CONFIRMED CORRECT — no change needed (stated so the engineer does not re-verify): both download URLs live and anonymous with the exact byte sizes and Last-Modified values claimed; BOM EF BB BF; 10 preamble rows and header on line 11 (skiprows=10); all 47 column headers verbatim, in the exact order listed, including 'Art der Ladeeinrichtung' spelled correctly and 'Nennleistung Ladeeinrichtung [kW]'; group banner at 0-based offsets 0/23/27/31/35/39/43; delimiter ';'; decimal comma; Inbetriebnahmedatum is DD.MM.YYYY with ZERO violations and ZERO empties across all 116,443 rows; Breitengrad/Längengrad are never empty and 100% parseable after ','->'.'; Ladeeinrichtungs-ID is unique across all 116,443 rows; no stable 'latest' URL (I additionally probed 2026-07-01 and 2026-06-01 — both 404); the landing page HTML contains exactly two data.bundesnetzagentur.de matches; the CC BY 4.0 + 'Bundesnetzagentur.de' attribution wording is verbatim on the landing page; the ArcGIS FeatureServer really does return {"code":499,"message":"Token Required"} on both the root and the layer-7 query; the Schnittstellen page really does describe a daily-updated public JSON/XML REST interface with no published endpoint and only the ladesaeulenregister@bnetza.de contact; the 156.399 / 55.665 / 9,22 GW (Stand 1. August 2026) figures are verbatim on the E-Mobilität page.

### Recommendation for AutoTwin DE

## Implement: scrape landing page → resolve dated URL → stream CSV → parse

**Do not hardcode the download URL.** It is date-stamped and the previous month's file is *deleted* (verified 404), so a hardcoded URL breaks within ~30 days.

### Step 1 — resolve the current URL

Fetch the landing page and regex out the absolute URL. I verified the raw HTML contains exactly these two matches and nothing else matching the pattern:

```python
import re, requests

LANDING = (
    "https://www.bundesnetzagentur.de/DE/Fachthemen/ElektrizitaetundGas/"
    "E-Mobilitaet/Ladesaeulenkarte/start.html"
)


def resolve_csv_url() -> str:
    html = requests.get(LANDING, timeout=60).text
    m = re.findall(
        r"https://data\.bundesnetzagentur\.de/\S*?"
        r"Ladesaeulenregister_BNetzA_(\d{4}-\d{2}-\d{2})\.csv",
        html,
    )
    urls = re.findall(
        r"https://data\.bundesnetzagentur\.de/\S*?"
        r"Ladesaeulenregister_BNetzA_\d{4}-\d{2}-\d{2}\.csv",
        html,
    )
    if not urls:
        raise RuntimeError("BNetzA landing page layout changed - CSV link not found")
    return urls[0]  # edition date available in m[0]
```

Capture the edition date from the filename and store it as the dataset vintage — it is the authoritative `Stand`. Cross-check it against line 8 of the file (`Letzte Aktualisierung vom: 01.09.2026`).

### Step 2 — read it

```python
import pandas as pd

df = pd.read_csv(
    resolve_csv_url(),
    sep=";",
    encoding="utf-8-sig",  # BOM-aware. NOT cp1252.
    skiprows=10,  # 9 notice lines + 1 group-banner row
    quotechar='"',
    dtype=str,  # parse everything as text first, convert explicitly
    keep_default_na=False,
)
assert len(df.columns) == 47, f"schema drift: {len(df.columns)} cols"
assert df.columns[0] == "Ladeeinrichtungs-ID"
```

Deliberately **not** using `decimal=","` — because `dtype=str` is safer here given `Nennleistung Stecker1` can contain `"11; 3,7"`, which is neither a number nor safe to coerce. Convert explicitly instead:

```python
def de_num(s):  # "48,442398" -> 48.442398 ; "11; 3,7" -> None
    s = (s or "").strip()
    if not s or ";" in s:
        return None
    return float(s.replace(",", "."))


df["lat"] = df["Breitengrad"].map(de_num)
df["lon"] = df["Längengrad"].map(de_num)
df["power_kw"] = df["Nennleistung Ladeeinrichtung [kW]"].map(de_num)
df["commissioned"] = pd.to_datetime(df["Inbetriebnahmedatum"], format="%d.%m.%Y", errors="coerce")
```

### Step 3 — reshape the wide connector block to long

The 24 connector columns are a repeating 4-tuple. Melt them:

```python
points = []
for i in range(1, 7):
    blk = df[
        [
            "Ladeeinrichtungs-ID",
            f"Steckertypen{i}",
            f"Nennleistung Stecker{i}",
            f"EVSE-ID{i}",
            f"Public Key{i}",
        ]
    ].copy()
    blk.columns = ["station_id", "connector_type", "connector_kw", "evse_id", "public_key"]
    blk["point_index"] = i
    points.append(blk[blk["connector_type"].str.strip() != ""])
points = pd.concat(points, ignore_index=True)
# connector_type and connector_kw may each hold "; "-joined lists - split in parallel
```

### Hardening

- **Guard on schema drift**: assert 47 columns and assert the first header equals `Ladeeinrichtungs-ID`. BNetzA has already silently changed things once (they fixed the `Art der Ladeeinrichung` typo and moved hosts), so fail loudly rather than mis-map.
- **Detect the BOM rather than assuming** — older archived editions really were cp1252. If you ingest historical files: sniff the first 3 bytes for `EF BB BF`, use `utf-8-sig` if present, else fall back to `cp1252`.
- **Archive each edition yourself.** BNetzA deletes the prior month. If AutoTwin DE wants a time series, snapshot every month to your own storage — there is no official archive.
- **Schedule monthly**, a few days after the 1st. The 2026-09-01 edition had `Last-Modified` of 2026-09-03, so the file appears ~2 days after its nominal Stand date.
- **Stream, don't buffer**: 55 MB. Use `requests.get(..., stream=True)` to a temp file, or pass the URL straight to pandas.
- **Licence compliance**: store `CC BY 4.0` and the attribution string `Bundesnetzagentur.de` in your dataset metadata and surface it in any UI.

### On the API

Skip the REST API for v1 — the endpoint is unpublished and requires a manual email exchange with BNetzA. The monthly file is the pragmatic path. **If daily freshness matters to AutoTwin DE, it is worth emailing `ladesaeulenregister@bnetza.de` to request the OpenAPI spec**, since the public REST interface refreshes daily versus the file's monthly cadence. Do not use the ArcGIS FeatureServer as a shortcut — it is token-gated.

### Implementation notes

## Bottom line

The report's primary path is correct and I reproduced it end to end: scrape the landing page, pull the dated CSV URL, stream 55 MB, parse with `;` + `utf-8-sig` + `skiprows=10` + `quotechar='"'` → 116,443 rows x 47 columns. Everything load-bearing (URLs, all 47 column names, encoding, preamble size, licence, no auth) checks out byte-for-byte. The fixes below are the ones that will actually bite you.

## Zero-cost check: PASSES

No credit card, no registration, no API key, no login, no paid tier anywhere on the ingestion path. I fetched the CSV anonymously with `curl`, with `python-requests/2.32.3` as UA, with no UA at all, and with Python's `urllib` — all HTTP 200/206 with real bytes. No Referer check, no cookie required (the server sets `bnetza_cookie`/`TS01b2d567` but never demands them back). `Accept-Ranges: bytes` works (verified HTTP 206 Partial Content).

The only paid/gated things in the whole report are things you should not use anyway: the ArcGIS FeatureServer (token-gated, HTTP 499) and the BNetzA operator Meldeportal APIs (require a Betreiber-/Dienstleisterkonto). The "public REST interface" is free but requires a manual email to `ladesaeulenregister@bnetza.de` to even learn the endpoint — keep it out of v1.

## Exact URLs I verified myself (2026-09-14)

- Landing page to scrape: `https://www.bundesnetzagentur.de/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/Ladesaeulenkarte/start.html` — HTTP 200, ~65.7 KB HTML. `grep -oE 'https://data\.bundesnetzagentur\.de[^"'"'"' <)]*'` returns exactly two hits, the .csv and the .xlsx below. Nothing else on the page matches.
- Current CSV: `https://data.bundesnetzagentur.de/Bundesnetzagentur/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/Ladesaeulenregister_BNetzA_2026-09-01.csv` — 200, `Content-Length: 55559083`, `Content-Type: application/octet-stream`, `Last-Modified: Thu, 03 Sep 2026 07:39:47 GMT`, `Accept-Ranges: bytes`.
- Current XLSX: same path with `.xlsx` — 200, 31,215,578 bytes.
- Licence text (verbatim on the landing page): "Die Daten sind durch eine Creative Commons Namensnennung 4.0 International Lizenz lizenziert." / "Als Namensnennung für Daten dieser Internetseite ist Bundesnetzagentur.de zu verwenden." → store `CC BY 4.0` + attribution string `Bundesnetzagentur.de`.

## The 47 columns, verbatim, in file order (I parsed these out of the real bytes)

`Ladeeinrichtungs-ID`, `Betreiber`, `Anzeigename (Karte)`, `Status`, `Art der Ladeeinrichtung`, `Anzahl Ladepunkte`, `Nennleistung Ladeeinrichtung [kW]`, `Inbetriebnahmedatum`, `Straße`, `Hausnummer`, `Adresszusatz`, `Postleitzahl`, `Ort`, `Kreis/kreisfreie Stadt`, `Bundesland`, `Breitengrad`, `Längengrad`, `Standortbezeichnung`, `Informationen zum Parkraum`, `Bezahlsysteme`, `Öffnungszeiten`, `Öffnungszeiten: Wochentage`, `Öffnungszeiten: Tageszeiten`, then for i in 1..6: `Steckertypen{i}`, `Nennleistung Stecker{i}`, `EVSE-ID{i}`, `Public Key{i}`.

Note the exact spellings: `Art der Ladeeinrichtung` (historic typo IS fixed), `Nennleistung Ladeeinrichtung [kW]` (there is no `Anschlussleistung`), `Nennleistung Stecker1` (there is no `P1 [kW]`), `Kreis/kreisfreie Stadt` (contains a slash), `Anzeigename (Karte)` (contains parentheses), `Öffnungszeiten: Wochentage` (contains a colon and a space).

## Fetch + parse (this exact code ran green against the live file)

```python
import io, re, csv, requests

LANDING = (
    "https://www.bundesnetzagentur.de/DE/Fachthemen/ElektrizitaetundGas/"
    "E-Mobilitaet/Ladesaeulenkarte/start.html"
)
URL_RE = re.compile(
    r"https://data\.bundesnetzagentur\.de/[^\s\"'<>)]*?"
    r"Ladesaeulenregister_BNetzA_(\d{4}-\d{2}-\d{2})\.csv"
)


def resolve_csv_url(session) -> tuple[str, str]:
    r = session.get(LANDING, timeout=60)
    r.raise_for_status()
    hits = URL_RE.findall(r.text)  # -> ['2026-09-01']
    urls = [m.group(0) for m in URL_RE.finditer(r.text)]
    if not urls:
        raise SourceUnavailable("BNetzA landing page changed: no dated CSV link found")
    return urls[0], hits[0]  # url, edition date == dataset vintage


def download(session, url, dest):
    with session.get(url, stream=True, timeout=(30, 600)) as r:
        r.raise_for_status()
        # CRITICAL: status 200 is NOT proof of success on this host.
        ct = r.headers.get("Content-Type", "")
        if not ct.startswith("application/octet-stream"):
            raise SourceUnavailable(f"expected CSV bytes, got Content-Type={ct!r}")
        first = b""
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                if not first:
                    first = chunk[:3]
                    if first != b"\xef\xbb\xbf":
                        raise SourceUnavailable(f"missing UTF-8 BOM, got {first!r}")
                fh.write(chunk)
```

Then parse. Both of these are verified to produce `(116443, 47)`:

```python
# stdlib — newline='' is MANDATORY (8,305 rows carry bare \n inside quoted fields)
with open(dest, encoding="utf-8-sig", newline="") as fh:
    rows = list(csv.reader(fh, delimiter=";", quotechar='"'))
header, data = rows[10], rows[11:]

# or pandas
df = pd.read_csv(
    dest,
    sep=";",
    encoding="utf-8-sig",
    skiprows=10,
    quotechar='"',
    dtype=str,
    keep_default_na=False,
)
```

Assertions worth failing loudly on: `len(header) == 47`, `header[0] == "Ladeeinrichtungs-ID"`, `header[4] == "Art der Ladeeinrichtung"`, and `rows[7][0].startswith("Letzte Aktualisierung vom:")` (that row carries `01.09.2026` — cross-check it against the date in the filename).

## Type conversion — what actually appears in the data

```python
def de_num(s):
    """'48,442398' -> 48.442398 ; '22; 22' -> None (multi-valued) ; '' -> None"""
    s = (s or "").strip()
    if not s or ";" in s:
        return None
    return float(s.replace(",", "."))
```

- `Breitengrad` / `Längengrad`: WGS84, comma decimal. **Never empty, 100% parseable** across all 116,443 rows — you can make lat/lon non-nullable.
- `Inbetriebnahmedatum`: `%d.%m.%Y`, **zero empties and zero format violations** across all rows. Also safe as non-nullable.
- `Nennleistung Ladeeinrichtung [kW]`: never empty, never multi-valued, but 2,980 rows use a decimal comma (`16,7`, `62,5`, `3,6`). `de_num` handles it.
- `Nennleistung Stecker{i}`: 5,751 rows are `; `-joined lists (`22; 22`, `150; 150`); 2,891 are single decimal-comma values. Keep as string, split on `;`, then convert each.
- `Anzahl Ladepunkte`: int 1–6, and it **exactly equals** the number of non-empty `Steckertypen{i}` slots in all 116,443 rows — a free integrity check.
- `Ladeeinrichtungs-ID`: unique across all rows; use it as the natural key. 11,755 distinct `Betreiber`. All 16 Bundesländer present.
- `EVSE-ID{i}` populated on only 37,316 of 116,443 rows and frequently has trailing whitespace/newlines — `.strip()` it.

## Connector reshape (wide 6x4 → long)

```python
for i in range(1, 7):
    typ = row[f"Steckertypen{i}"].strip()
    if not typ:
        continue  # slot unused
    types = [t.strip() for t in typ.split(";") if t.strip()]
    powers = [de_num(p) for p in row[f"Nennleistung Stecker{i}"].split(";")]
    yield dict(
        station_id=row["Ladeeinrichtungs-ID"],
        point_index=i,
        connector_types=types,
        connector_kw=powers,
        evse_id=row[f"EVSE-ID{i}"].strip() or None,
        public_key="".join(row[f"Public Key{i}"].split()) or None,
    )
```

One `Ladepunkt` slot can list several connector types (`AC Typ 2 Steckdose; AC Schuko`), so `connector_types` is a list, not a scalar. Strip all whitespace out of `Public Key` — many are multi-line hex blocks.

## Multi-value separators — they are NOT consistent

- `Bezahlsysteme`: `;` with **no** space → `Onlinezahlungsverfahren;RFID-Karte`
- `Steckertypen*`, `Nennleistung Stecker*`, `Öffnungszeiten: Wochentage`, `Öffnungszeiten: Tageszeiten`: `;` **with** a space

Use `re.split(r"\s*;\s*", value)` everywhere and sort tokens before comparing — token order varies row to row.

## Fallback strategy when the source is unreachable

1. **Primary**: landing page → dated CSV. If the regex finds nothing or the Content-Type/BOM guard trips, **do not** fall through to a mirror — fail the run and alert. A silent fallback to half-stale data is worse than a missed ingest.
2. **Same-edition retry**: cache the last successfully resolved URL + edition date. If the landing page is down but the previously resolved dated URL still 200s, reuse it. This buys you up to a month.
3. **Your own archive is the only real fallback.** BNetzA deletes the previous edition (2026-08-01, 2026-07-01 and 2026-06-01 all 404). Snapshot every successful download (raw bytes + sha256 + resolved URL + edition date) to your own object storage on day one. There is no official archive and no way to backfill later.
4. **Do NOT use the Rhein-Kreis-Neuss Opendatasoft copy as a data fallback** — 61,793 records vs 116,443, data processed 2025-05-11, old flattened schema with no status/bundesland/bezahlsysteme/öffnungszeiten. At most use its GeoJSON export for a low-stakes map placeholder, clearly labelled stale. Never for counts or analytics.
5. **Do NOT use** the Schleswig-Holstein mirror (SH-only, 2023, closed licence) or the ArcGIS FeatureServer (token-gated).

## Scheduling

Monthly, run on the 4th–6th of each month. The 2026-09-01 edition has `Last-Modified: 2026-09-03`, i.e. the file lands ~2 days after its nominal Stand. Make the job idempotent on `(edition_date, sha256)` so a re-run before publication is a no-op rather than a failure.

## Fixture data

I generated a byte-faithful fixture from the real file at `/private/tmp/claude-501/-Users-ahmedmaaloul-Documents-me-projects-autotwin/38df7c03-f47d-410a-acab-934b16e6817d/scratchpad/bnetza_fixture.csv` (9,567 bytes, 24 CSV records = 10 preamble + 1 header + 13 data, all 47 columns). Copy it into the repo as `tests/fixtures/bnetza_ladesaeulenregister_2026-09-01_sample.csv`. It preserves the real BOM, the real 10-line German preamble, the real group-banner row, CRLF terminators, and deliberately includes: a `Status == In Wartung` row, a 6-Ladepunkte row, a 5-Ladepunkte row, a row with a bare `\n` inside `Public Key1`, a row with a bare `\n` inside `EVSE-ID1`, a `; `-joined multi-type `Steckertypen1`, a decimal-comma `Nennleistung Ladeeinrichtung [kW]`, an `Öffnungszeiten == Eingeschränkt` row, an empty-`Bezahlsysteme` row, and a `DC Megawatt Charging System (MCS)` row.

Minimum tests to hang off it: parses to exactly 13 rows x 47 columns; `header[0] == "Ladeeinrichtungs-ID"` with no stray BOM; the embedded-newline row round-trips without splitting into two records; `Anzahl Ladepunkte == len(filled Steckertypen slots)` for every row; lat/lon and date conversion succeed on every row. Add two negative fixtures too: a saved copy of the bnetza HTML 404 page and of the WAF "URL wurde abgewiesen" page, asserting your downloader raises on both despite the HTTP 200.

## Metadata to persist per ingest

`source = "Bundesnetzagentur Ladesäulenregister"`, `edition_date` (from the filename, cross-checked against preamble line 8), `source_url`, `sha256`, `bytes`, `row_count`, `fetched_at`, `licence = "CC BY 4.0"`, `attribution = "Bundesnetzagentur.de"`. Surface the attribution string in any user-facing credit — it is mandatory under the licence.

### Unconfirmed / open questions

- EXACT row count. My ~117,000 figure is a byte-sampling estimate (6 x 256 KB windows, pooled 475.0 bytes/line over 55,559,083 bytes), not a counted value. Per-window estimates spanned 103k-126k, so the honest range is ~110,000-130,000. I did not download the full 55 MB file to count exactly. The official 212,064 charging-POINT figure is confirmed, but that is points, not rows.
- Full domain of the `Status` column. Every row in all my samples (~3,250 rows across two windows) showed only `In Betrieb`. Whether values such as `Außer Betrieb` or `Geplant` occur elsewhere in the file is NOT confirmed - do not assume `Status == 'In Betrieb'` is the only case without validating on the full file.
- The public REST API endpoint URL, its exact shape (REST vs OGC), its authentication requirements, rate limits, and its OpenAPI schema. BNetzA publishes none of this; it is only obtainable by emailing ladesaeulenregister@bnetza.de. I confirmed the API exists and is JSON+XML with daily updates, nothing more.
- Whether the REST API's field names match the CSV column names. Unknown - assume they may differ.
- The exact publication-date rule. I observed the 2026-09-01 edition with Last-Modified 2026-09-03, and confirmed 2026-08-01 is already deleted and 2026-10-01 does not yet exist. That strongly implies a monthly cadence with files dated the 1st, but I could not verify the retention policy or whether BNetzA ever keeps more than one edition online.
- Whether BNetzA publishes this dataset on mobilithek.info. I found no evidence it does, but I did not exhaustively search that portal.
- Whether the XLSX has the same 10-row preamble and 47-column layout as the CSV. I confirmed the XLSX URL is live and its size, but did not open it. I inferred nothing about its internal sheet structure - verify before using the XLSX path.
- Historical encoding of older editions. The CURRENT file is definitively UTF-8 with BOM (I read the bytes). My statement that earlier editions used cp1252 is background knowledge, not verified in this session - relevant only if you ingest archived files.
- Whether the landing-page HTML structure is stable enough for the regex to survive. It works today (I grepped the live HTML and got exactly the two expected URLs), but it is a scrape and should fail loudly rather than silently.
- Freshness and schema fidelity of the Rhein-Kreis-Neuss Opendatasoft mirror suggested as a GeoJSON fallback. I confirmed the URLs are listed on GovData but did not fetch them or compare their schema against the BNetzA original.

---

## 3. Deutscher Wetterdienst — Open Data

**Verification verdict:** `SOLID`

**Licence:** **Creative Commons Attribution 4.0 International (CC BY 4.0)** — NOT GeoNutzV.

GeoNutzV is superseded. Verified at `https://opendata.dwd.de/climate_environment/CDC/Nutzungsbedingungen_German.txt` (149 bytes, *Stand: Mai 2024*): "Es gelten die Bedingungen der Lizenz Creative Commons BY 4.0 'CC BY 4.0'. Einzelheiten unter https://www.dwd.de/copyright". A grep for "GeoNutzV" across the current DWD legal-notices page returns **0 hits**. The CDC `Nutzungsbedingungen_German.pdf` and `Terms_of_use.pdf` are both dated 15-May-2024, consistent with that switch.

The DWD legal page states all freely accessible geodata, geodata services, and High Value Datasets "duerfen unter den Bedingungen der Lizenz Creative Commons BY 4.0 (CC BY 4.0) unter Beigabe eines Quellenvermerks weiterverwendet werden" — reusable, **including commercially**, with a source note.

**REQUIRED ATTRIBUTION (exact wording from the official Vorlagen page, legal basis section 7 DWD-Gesetz):**

- Unmodified data, extracts, or mere format conversion:
  > **`Quelle: Deutscher Wetterdienst`**
  (Displaying the DWD logo instead is explicitly sufficient; min. 127x34 px at 72 dpi.)

- Modified / processed / reworked data — DWD expects at minimum a mention in a central source list or the imprint, with a change note, e.g.:
  > **`Datenbasis: Deutscher Wetterdienst, eigene Elemente ergänzt`**
  > `Datenbasis: Deutscher Wetterdienst, Einzelwerte gemittelt`
  > `Datenbasis: Deutscher Wetterdienst, Rasterdaten bildlich wiedergegeben`

  For AutoTwin DE — which resamples, interpolates, or fuses DWD values into a twin — the **`Datenbasis:` form with a change note is the correct one**, not the bare `Quelle:` form.

**Additional binding rules:**
- The source note must be placed **immediately adjacent to** the DWD-derived information.
- It **may** hyperlink to DWD web pages.
- **If official DWD severe-weather warnings (amtliche Wetterwarnungen) are modified, the accompanying source note must be DELETED** — you may not present altered warnings as DWD's. This matters if AutoTwin DE ever touches the CAP alerts feed.
- Third-party content within DWD output (Geobasisdaten from BKG/Laender; satellite data from EUMETSAT/NOAA) carries its own rights and is marked separately.

**Authentication:** **None.** No API key, no token, no registration, no account, no `User-Agent` requirement. Plain anonymous HTTPS GET.

Verified directly: every one of the ~30 URLs above was fetched with bare `curl` and no credentials. `LIESMICH.txt`/`README.txt` state it explicitly ("Access is granted without registration"), and the Open Data FAQ repeats it ("Fuer den Zugang ist keine Registrierung noetig").

Two caveats worth recording:
1. **Implicit consent to IP logging.** The README states that by using the server you agree DWD stores your IP for up to 7 days for operational security. Not shared with third parties. No opt-out; relevant for a GDPR record of processing if AutoTwin DE polls from identifiable infrastructure.
2. The unofficial `app-prod-ws.warnwetter.de` JSON endpoint also requires no key today — but as an undocumented internal endpoint, that could change without notice, including by adding app attestation.


### Findings

## Verification method

Every path below was fetched with `curl` against the live server on **2026-09-14**, directory listings parsed, and sample ZIP/KMZ files actually downloaded and unpacked. Byte sizes and field headers are copied from real responses, not from memory. Two things I initially assumed turned out **wrong** and are corrected below (licence is no longer GeoNutzV; the station catalogue is NOT in decimal degrees).

## 1. Base URL and access

- **`https://opendata.dwd.de/`** — HTTP/2, nginx, no key, no registration, no token, no `User-Agent` gate. Anonymous HTTPS GET only.
- Official `README.txt` confirms: *"Access is granted without registration."*
- Top-level dirs: `climate_environment/` (CDC — observations/climate), `weather/` (forecasts, radar, NWP, alerts), `test/`.
- **Privacy note to record in your compliance docs:** the README states DWD stores the client **IP address for a maximum of 7 days** to secure/optimise server operation, and that using the server constitutes agreement to this. Not shared with third parties.

## 2. Observations — two useful cadences

**10-minute (`now` = current day, updated ~every 10 min)** and **hourly (`recent` = last ~500 days, updated daily ~08:40 UTC)**. For a digital twin, `10_minutes/.../now/` is the real-time feed; `hourly/.../recent/` is the backfill.

Critical structural point: **there is no "all stations" bulk file for observations.** One ZIP per station per parameter. ~490 stations for 10-min TU. Fetching all of them is ~500 requests per parameter per cycle — this drives the etiquette recommendation in §7.

## 3. MOSMIX forecasts — verified figures

| | MOSMIX_S | MOSMIX_L |
|---|---|---|
| Stations | **5,648** (counted placemarks) | ~6,045 single-station dirs |
| Elements | **40** (counted) | **114** (counted) |
| Timesteps | **240** (hourly, +240 h) | **247** (hourly→3-hourly, ~+240 h) |
| Runs | **hourly, 24×/day** | **4×/day, 03/09/15/21 UTC** |
| `all_stations` KMZ | 36 MB zip / **627 MB** unzipped | ~80 MB zip |
| `single_stations` | **DOES NOT EXIST** | Yes, ~18 KB per station |
| Retention on server | ~48 h of runs | ~8 runs (~2 days) |

**The decisive operational fact:** MOSMIX_S has **no per-station files**. To get one station hourly from MOSMIX_S you must download and stream-parse a 36 MB KMZ (627 MB uncompressed). MOSMIX_L *does* have per-station files at ~18 KB — a **~4,500× smaller** transfer, at the cost of 4×/day refresh instead of hourly.

## 4. Corrections to the premise of the question

**(a) Licence is CC BY 4.0, not GeoNutzV.** The question assumed GeoNutzV. That is superseded. `https://opendata.dwd.de/climate_environment/CDC/Nutzungsbedingungen_German.txt` (dated *Stand: Mai 2024*) reads: *"Es gelten die Bedingungen der Lizenz Creative Commons BY 4.0."* A grep for "GeoNutzV" on the current DWD legal page returns **0 hits**. GeoNutzV was the pre-2024 regime; CC BY 4.0 is strictly more permissive and removes the old legal ambiguity.

**(b) The MOSMIX station catalogue is in degrees-and-decimal-minutes, not decimal degrees.** This is a silent data-corruption trap. Verified by cross-checking the same station against the KML:

- Catalogue line: `10865 ---- MUENCHEN STADT        48.10   11.32   515`
- KML `<kml:coordinates>`: `11.53,48.17,515.0` (lon, lat, decimal)
- `48.10` in the catalogue means **48° 10′ = 48.167°**, not 48.10°.

Parsing the catalogue as decimal degrees puts Munich ~7 km off — an error small enough to pass a smoke test and survive into production. **Recommendation: take station coordinates from the KML `<kml:Point>` instead, which is already decimal degrees.**

## 5. JSON alternative — works, but is not official

`https://app-prod-ws.warnwetter.de/v30/stationOverviewExtended?stationIds=10865` returns HTTP 200, `application/json`, 10,435 bytes, with clean hourly arrays. It is tempting and it works today. But:

- The host is **`warnwetter.de`** — the DWD WarnWetter *app* backend — **not** `opendata.dwd.de` and not `dwd.de`.
- DWD's own Open Data FAQ states plainly: **"Der DWD bietet zurzeit keine dedizierte API an"** (DWD currently offers no dedicated API).
- No DWD-published schema, changelog, versioning policy, or stability guarantee exists. The only documentation is community reverse-engineering (bundesAPI/dwd-api on GitHub, `dwd.api.bund.dev`). DWD's own API even ships a typo in a field name (`precipitationProbablity`), which is a fair signal of an internal, undocumented interface.

**Honest assessment: it is not officially documented or officially permitted for third-party use.** It is an internal app endpoint that happens to be reachable. It can change or close without notice. I would not put it on the critical path of AutoTwin DE.

## 6. What I verified vs. what I did not

Confirmed by actually downloading and unpacking: 10-min TU/wind/precip schemas, hourly TU schema, MOSMIX_L single-station and MOSMIX_S all-stations KMZ internals, element definitions, station catalogue format, licence text, attribution wording, conditional-GET behaviour. Listed under `unconfirmed` are the few items I could not establish from primary sources — notably that **no documented rate limit exists**, which is an absence of evidence, not a licence to hammer the server.

### Schema / format details

## A. Observation files — semicolon-delimited, fixed-width-padded CSV inside ZIP

Not fixed-width text and not plain CSV: it is **`;`-delimited with space padding around values**, so you must strip every field. One `produkt_*.txt` per ZIP (hourly ZIPs additionally carry ~12 `Metadaten_*` sidecars — ignore them or use for QC).

**Universal conventions (verified in every file I opened):**
- `-999` = missing value. Must be converted to null before any arithmetic.
- `eor` = literal end-of-row sentinel, final column. Discard.
- `MESS_DATUM` is **UTC**, zero-padded, no separators, no timezone marker.
- `STATIONS_ID` is space-padded in the data (`         44`) but **zero-padded to 5 digits in filenames** (`00044`). This asymmetry breaks naive joins — normalise to `int` on both sides.
- Encoding **ISO-8859-1 (latin-1)**, not UTF-8. German names (`Großenkneten`, `Baden-Württemberg`) mojibake if read as UTF-8.

### 10-minute air temperature — `produkt_zehn_now_tu_*.txt`
```
STATIONS_ID;MESS_DATUM;  QN;PP_10;TT_10;TM5_10;RF_10;TD_10;eor
         44;202609140000;    3;   -999;  10.5;   7.9;  97.9;  10.2;eor
```
| Field | Meaning | Unit |
|---|---|---|
| `MESS_DATUM` | timestamp | `YYYYMMDDHHMM` UTC |
| `QN` | quality level | code |
| `PP_10` | pressure at station level | hPa |
| **`TT_10`** | **air temperature 2 m** | **degC** |
| `TM5_10` | air temperature 5 cm | degC |
| `RF_10` | relative humidity | % |
| `TD_10` | dew point 2 m | degC |

### 10-minute wind — `produkt_zehn_now_ff_*.txt`
```
STATIONS_ID;MESS_DATUM;  QN;FF_10;DD_10;eor
         96;202609140000;    2;   1.7; 290;eor
```
**`FF_10`** = mean wind speed, **m/s**. `DD_10` = wind direction, degrees.

### 10-minute precipitation — `produkt_zehn_now_rr_*.txt`
```
STATIONS_ID;MESS_DATUM;  QN;RWS_DAU_10;RWS_10;RWS_IND_10;eor
         44;202609140000;    3;-999;   0.00;-999;eor
```
**`RWS_10`** = precipitation sum over the 10 min, **mm**. `RWS_DAU_10` = duration (min). `RWS_IND_10` = precipitation indicator (0/1).

### Hourly air temperature — `produkt_tu_stunde_*.txt`
```
STATIONS_ID;MESS_DATUM;QN_9;TT_TU;RF_TU;eor
         44;2025031200;    3;   4.4;  85.0;eor
```
`MESS_DATUM` here is **`YYYYMMDDHH`** (10 chars, no minutes) — a *different width* from the 10-minute products. **`TT_TU`** = temperature degC, `RF_TU` = humidity %. Quality column is named `QN_9`, not `QN`.

Hourly wind (`FF`) and precipitation (`RR`) ZIP **names** are verified; I did not unpack them, so their internal column names are listed as unconfirmed.

## B. Station description files — true fixed-width

```
Stations_id von_datum bis_datum Stationshoehe geoBreite geoLaenge Stationsname Bundesland Abgabe
----------- --------- --------- ------------- --------- --------- --------...- ---------- ------
00044 20070208 20260914             44     52.9336    8.2370 Großenkneten     Niedersachsen   Frei
```
- ISO-8859-1, **CRLF**, rows right-padded with spaces to ~1000 chars — always `.rstrip()`.
- `von_datum`/`bis_datum` = `YYYYMMDD` station operating window.
- `geoBreite`/`geoLaenge` = **decimal degrees** (unlike the MOSMIX catalogue).
- `Abgabe` = `Frei` (free to release).
- Line 2 is a dash ruler — skip rows 0 and 1. Parsing by `split()` breaks on multi-word names (`Aldersbach-Kramersepp` is fine, `Seebach (Nationalpark Schwarzwald)` is not) — slice by column offsets or use a regex anchored on the numeric prefix.

## C. MOSMIX — KML inside KMZ (a plain ZIP)

`.kmz` is a ZIP with exactly **one** `.kml` member, named for the actual run (`MOSMIX_L_2026091403_10865.kml`) even when fetched via the `LATEST` alias — so **the KMZ filename does not tell you the run; the member name and `<dwd:IssueTime>` do.**

XML declares `encoding="ISO-8859-1"`. Namespaces: `kml=http://www.opengis.net/kml/2.2`, `dwd=https://opendata.dwd.de/weather/lib/pointforecast_dwd_extension_V1_0.xsd`.

### Structure — the column-oriented layout is the key gotcha
```xml
<dwd:ProductDefinition>
  <dwd:IssueTime>2026-09-14T03:00:00.000Z</dwd:IssueTime>
  <dwd:ForecastTimeSteps>
    <dwd:TimeStep>2026-09-14T04:00:00.000Z</dwd:TimeStep>   <!-- 247 of these for L -->
  </dwd:ForecastTimeSteps>
</dwd:ProductDefinition>

<kml:Placemark>
  <kml:name>10865</kml:name>                                 <!-- station ID -->
  <kml:description>MUENCHEN STADT</kml:description>
  <kml:Point><kml:coordinates>11.53,48.17,515.0</kml:coordinates></kml:Point>
  <kml:ExtendedData>
    <dwd:Forecast dwd:elementName="TTT">
      <dwd:value>      288.15     288.15     288.55  ...</dwd:value>
    </dwd:Forecast>
  </kml:ExtendedData>
</kml:Placemark>
```

**Parsing rules, all verified against real files:**
1. `<dwd:value>` is **one whitespace-separated string**, positionally aligned 1:1 with `<dwd:TimeStep>`. `value.split()` then `zip()` with the timesteps. Length **must** equal the timestep count — assert this.
2. **Missing values are the literal `-` character**, not `-999` and not empty. `TX`/`TN` are mostly `-` because they are 12-hourly fields carried on an hourly axis.
3. Timestamps are **ISO-8601 UTC with explicit `Z`**, millisecond precision — directly parseable, no timezone guessing. (Contrast with the observation files' bare `YYYYMMDDHHMM`.)
4. `<kml:coordinates>` is **lon,lat,elevation** — GeoJSON/KML order, i.e. **longitude first**. Reversing this is the classic bug.
5. Read elements **streaming** for MOSMIX_S: 627 MB uncompressed will not fit comfortably in memory via `ElementTree.parse`. Use `iterparse` + `element.clear()`, or read the ZIP member as a stream.

### Element names for the four requested quantities (units from `MetElementDefinition.xml`)
| Element | Description | Unit | Conversion needed |
|---|---|---|---|
| **`TTT`** | Temperature 2 m above surface | **K** | **degC = TTT - 273.15** |
| `Td` | Dewpoint 2 m | K | minus 273.15 |
| `TX` / `TN` | Max / min temp, last 12 h | K | minus 273.15; sparse (`-`) |
| **`FF`** | **Wind speed** | **m/s** | none (x3.6 for km/h) |
| `DD` | Wind direction | 0..360 deg | none |
| `FX1` | Max wind gust, last hour | m/s | none |
| **`RR1c`** | **Total precipitation, last hour**, consistent w/ significant weather | **kg/m2 (= mm)** | none |
| `RRhc` | Total precipitation, last 12 h | kg/m2 | none |
| `Neff` | Effective cloud cover | % (0..100) | none |
| `PPPP` | Surface pressure, reduced | **Pa** | **hPa = PPPP / 100** |
| `ww` | Significant weather | WMO code | lookup table |

**Two unit traps:** `TTT` is **Kelvin** (a raw `288.15` looks plausible as nothing else, so it fails loudly — good), and `PPPP` is **Pascal** (`102440.00`, i.e. 1024.4 hPa — a raw value that looks like a plausible-but-wrong number if mistaken for hPa).

Full MOSMIX_S 40-element set (verified by counting): `DD FF FX1 FX3 FXh FXh25 FXh40 FXh55 N N05 Neff Nh Nl Nm PPPP R602 R650 RR1c RR3c RRS1c RRS3c Rad1h Rd02 Rd50 Rh00 Rh02 Rh10 Rh50 SunD1 T5cm TN TTT TX Td VV W1W2 ww wwM wwM6 wwMh`. MOSMIX_L adds 74 more (error estimates `E_*`, `PEvap`, `RSunD`, `PSd*`, `WPc*`, extended `ww*` breakdowns).

## D. MOSMIX station catalogue — fixed-width, degrees+minutes
```
ID    ICAO NAME                 LAT    LON     ELEV
----- ---- -------------------- -----  ------- -----
01001 ENJA JAN MAYEN             70.56   -8.40    10
10865 ---- MUENCHEN STADT        48.10   11.32   515
```
- `ICAO` is `----` when absent.
- `ID` is right-aligned in 5 chars and can be alphanumeric with a leading space (` Z949`, ` E525`, `E5203`) — **strip before use**; 2,696 of 5,649 IDs are purely numeric.
- **`LAT`/`LON` are DD.MM (degrees + decimal minutes).** Convert: `deg = trunc(v) + (v - trunc(v)) * 100 / 60`. Negative values (`-8.40`) need sign-aware handling: apply to the absolute value, then restore the sign.
- Safer: ignore these columns and take coordinates from the KML `<kml:Point>`, which is already decimal degrees.

## E. Unofficial JSON shape (for completeness, not recommended)
`{"10865": {"forecast1": {...}, "forecast2": {...}, "days": [...], "warnings": [], ...}}`. Inside `forecast1`: `start` (epoch **milliseconds**), `timeStep` (3600000 ms), and parallel arrays `temperature`, `dewPoint2m`, `surfacePressure`, `humidity`, `precipitationTotal`, `sunshine`, `isDay`, `icon`, `icon1h`. **All numerics are integers scaled by 10**: `temperature: 156` = 15.6 degC, `surfacePressure: 10253` = 1025.3 hPa, `humidity: 793` = 79.3 %. `windSpeed`/`windDirection`/`windGust`/`cloudCoverTotal` came back `None`/`[]` for the station I tested — i.e. **the fields are unreliably populated**, another reason not to depend on it.

### Verified endpoints

| status | URL | purpose |
|---|---|---|
| `live` | <https://opendata.dwd.de/> | Base URL of the DWD open data server. No key, no registration. |
| `live` | <https://opendata.dwd.de/README.txt> | Official English statement of access terms and IP-retention policy. |
| `live` | <https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/10_minutes/air_temperature/> | 10-minute air temperature observations root. |
| `live` | <https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/10_minutes/air_temperature/now/> | CURRENT 10-minute air temperature — the real-time observation feed. |
| `live` | <https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/10_minutes/air_temperature/now/zehn_now_tu_Beschreibung_Stationen.txt> | Station list for the 10-minute air temperature 'now' product. |
| `live` | <https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/10_minutes/air_temperature/now/10minutenwerte_TU_00044_now.zip> | Concrete sample 10-min temperature file (station 00044 Grossenkneten). |
| `live` | <https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/10_minutes/wind/now/> | 10-minute wind observations (speed + direction). |
| `live` | <https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/10_minutes/precipitation/now/> | 10-minute precipitation observations. |
| `live` | <https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/hourly/air_temperature/recent/> | HOURLY air temperature observations, recent (~500 days rolling). |
| `live` | <https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/hourly/air_temperature/recent/stundenwerte_TU_00044_akt.zip> | Concrete sample hourly temperature file. |
| `live` | <https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/hourly/> | All hourly observation parameters. |
| `live` | <https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/hourly/wind/recent/> | Hourly wind observations. |
| `live` | <https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/hourly/precipitation/recent/> | Hourly precipitation observations. |
| `live` | <https://opendata.dwd.de/weather/local_forecasts/mos/> | MOSMIX product root. |
| `live` | <https://opendata.dwd.de/weather/local_forecasts/mos/MOSMIX_L/all_stations/kml/MOSMIX_L_LATEST.kmz> | MOSMIX_L all-stations KMZ, stable 'latest' alias. |
| `live` | <https://opendata.dwd.de/weather/local_forecasts/mos/MOSMIX_L/all_stations/kml/> | MOSMIX_L all-stations run archive — confirms the 4x/day cadence. |
| `live` | <https://opendata.dwd.de/weather/local_forecasts/mos/MOSMIX_L/single_stations/10865/kml/MOSMIX_L_LATEST_10865.kmz> | SINGLE-STATION MOSMIX_L KMZ — the efficient per-station forecast route. |
| `live` | <https://opendata.dwd.de/weather/local_forecasts/mos/MOSMIX_L/single_stations/> | Enumerate MOSMIX_L stations that have per-station files. |
| `live` | <https://opendata.dwd.de/weather/local_forecasts/mos/MOSMIX_S/all_stations/kml/MOSMIX_S_LATEST_240.kmz> | MOSMIX_S all-stations KMZ, hourly-updated, stable 'latest' alias. |
| `live` | <https://opendata.dwd.de/weather/local_forecasts/mos/MOSMIX_S/> | Proves MOSMIX_S has NO single_stations directory. |
| `live` | <https://www.dwd.de/DE/leistungen/met_verfahren_mosmix/mosmix_stationskatalog.cfg?view=nasPublication&nn=16102> | Official MOSMIX station catalogue (ID, ICAO, name, lat, lon, elevation). |
| `live` | <https://opendata.dwd.de/weather/lib/MetElementDefinition.xml> | AUTHORITATIVE machine-readable definitions of every MOSMIX element (name, unit, description). |
| `live` | <https://opendata.dwd.de/weather/lib/pointforecast_dwd_extension_V1_0.xsd> | XSD for the dwd: KML extension namespace used by MOSMIX. |
| `live` | <https://opendata.dwd.de/weather/lib/> | Library dir holding MOSMIX schema and element definitions. |
| `live` | <https://opendata.dwd.de/climate_environment/CDC/Nutzungsbedingungen_German.txt> | PRIMARY licence statement on the open data server itself. |
| `live` | <https://www.dwd.de/DE/service/rechtliche_hinweise/vorlagen_quellenangabe.html?nn=450672> | Exact required attribution wording (Quellenvermerk templates). |
| `live` | <https://www.dwd.de/copyright> | Canonical licence landing page. |
| `live` | <https://opendata.dwd.de/climate_environment/CDC/Terms_of_use.pdf> | English CDC terms of use. |
| `live` | <https://www.dwd.de/DE/leistungen/opendata/faqs_opendata.html> | Official Open Data FAQ — the source for the 'no dedicated API' statement. |
| `live` | <https://www.dwd.de/EN/ourservices/met_application_mosmix/met_application_mosmix.html> | Official MOSMIX product description confirming cadence and parameter counts. |
| `live` | <https://app-prod-ws.warnwetter.de/v30/stationOverviewExtended?stationIds=10865> | The JSON alternative — functional but UNOFFICIAL. |
| `live` | <https://opendata.dwd.de/weather/alerts/cap/COMMUNEUNION_DWD_STAT/> | Official severe-weather alerts in CAP format (the supported alerts route). |

### Corrections applied by the verifier

- MOSMIX_L POLLING SCHEDULE IS WRONG — the single most damaging error. The report says 'Schedule MOSMIX_L polls a few minutes after 03:20 / 09:20 / 15:20 / 21:20 UTC (observed publication lag was 13-16 min past the hour).' The 13-16 min figure is applied to the wrong hour. Measured Last-Modified headers on the actual run files: run 2026091303 -> published 13-Sep 04:16:32Z; 2026091309 -> 10:13:17Z; 2026091315 -> 16:15:46Z; 2026091321 -> 22:12:47Z; 2026091403 -> 14-Sep 04:16:01Z. Real lag is ~72-76 minutes after the run hour. Correct poll times are 04:20 / 10:20 / 16:20 / 22:20 UTC. Polling at 03:20 as recommended returns the 21Z run from the previous evening — a silently 6-hour-stale forecast.
- MOSMIX AND CDC USE DIFFERENT STATION ID NAMESPACES — the report never mentions this and it is a worse silent-corruption trap than the DD.MM issue it does flag. MOSMIX 10865 = MUENCHEN STADT; the same physical site in CDC is 03379 'München-Stadt' (both elev 515 m, coords 48.1632/11.5429 vs catalogue 48°10'/11°32' = 48.1667/11.5333). CDC 00044 = Großenkneten; the MOSMIX catalogue calls that area ' E426 GROSSENKNETEN-AHL.'. Of 466 CDC 10-min TU ids and 5,649 MOSMIX ids only 19 numeric values coincide, and the ones I sampled are all FALSE matches in different countries: id 01001 is JAN MAYEN (70.56N, Norway) in MOSMIX but Doberlug-Kirchhain (51.6451N, Germany) in CDC; 01052 is HAMMERFEST vs Möckern-Drewitz; 02559 is GLADHAMMAR (Sweden) vs Kempten; 03158 is CHARTERHALL (Scotland) vs Manschnow. Joining forecast to observation on station id would attach Norwegian forecasts to German observations and pass every smoke test. Match by coordinates/name, never by id.
- HOURLY 'recent' LAGS ~2 DAYS AND DOES NOT JOIN TO 10-MINUTE 'now' — the report's backfill recommendation ('hourly recent for backfill') leaves a hole. Measured on 2026-09-14: hourly/air_temperature/recent/stundenwerte_TU_00044_akt.zip ends at 2026091223 (Last-Modified Sun 13-Sep 08:40:09 GMT), while 10_minutes/.../now/ starts at 202609140000. All of 2026-09-13 falls in neither.
- THE REPORT OMITS THE 10_minutes/.../recent/ TIER ENTIRELY, which is the correct bridge and closes that gap exactly. Verified: https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/10_minutes/air_temperature/recent/10minutenwerte_TU_00044_akt.zip (712,144 bytes) contains member produkt_zehn_min_tu_20250313_20260913_00044.txt, 79,201 rows, spanning 202503130000 -> 202609132350. It ends at 23:50 yesterday and 'now' starts 00:00 today — zero gap. NOTE the member prefix is produkt_zehn_min_ (recent) vs produkt_zehn_now_ (now); code that globs on 'zehn_now' will miss the recent tier.
- HOURLY WIND AND PRECIPITATION COLUMN NAMES — the report correctly listed these as unconfirmed; I unpacked them and they are NOT the names anyone would guess from the 10-minute products. Hourly wind (stundenwerte_FF_00096_akt.zip) header is exactly: STATIONS_ID;MESS_DATUM;QN_3;   F;   D;eor — the fields are F (wind speed m/s) and D (direction deg), NOT FF/DD or FF_10/DD_10. Hourly precipitation (stundenwerte_RR_00044_akt.zip) header is exactly: STATIONS_ID;MESS_DATUM;QN_8;  R1;RS_IND;WRTR;eor — R1 (precip mm), RS_IND (indicator), WRTR (precipitation form code).
- QUALITY-COLUMN NAME VARIES PER PRODUCT, more than the report states. It notes only QN vs QN_9. Verified across four products: 10-min TU/wind/precip use 'QN'; hourly air_temperature uses 'QN_9'; hourly wind uses 'QN_3'; hourly precipitation uses 'QN_8'. Never hardcode the quality column name — select it positionally (index 2) or by regex ^QN.
- STATION COUNTS ARE WRONG AND THE REQUEST-VOLUME ARITHMETIC THAT DEPENDS ON THEM IS WRONG. Counted from live directory listings on 2026-09-14: 10_minutes/air_temperature/now = 466 zips (report claims '~490 stations'); 10_minutes/wind/now = 277; 10_minutes/precipitation/now = 1,372 (report claims '~500 per parameter'). Hourly recent: air_temperature 503, wind 296, precipitation 1,396. The report's 'about 9,000 requests/hour for three parameters' understates precipitation by ~2.7x; the true figure for all three 10-min parameters is ~2,115 files per cycle, ~12,700 requests/hour.
- PER-PARAMETER STATION COVERAGE IS NOT UNIFORM — the report's examples imply one station set. Station 00044 has 10-minute air temperature and precipitation but NO wind file: .../10_minutes/wind/now/10minutenwerte_wind_00044_now.zip returns HTTP 404 with a 146-byte nginx error page. Any adapter that assumes a station present in one parameter exists in all three will fetch 404s. Note the 404 body is 146 bytes of HTML, so code that checks only 'did I get bytes' rather than the status code will hand a nginx error page to the zip parser.
- MOSMIX_S PUBLICATION LAG IS NOT STATED in the report but matters if MOSMIX_S is ever used. Measured across 96 retained runs: each hourly run publishes ~39-41 minutes after its run hour (e.g. 2026091407 -> Last-Modified 14-Sep 07:40:07 GMT). Poll at HH:45 UTC, not HH:05. Retention verified at 96 runs = exactly 48 h, consistent with the report's '~48 h'.
- THE KML COORDINATE FIX IS CORRECT ABOUT FORMAT BUT NOT ABOUT PRECISION. The report recommends taking coordinates from the KML kml:Point 'which is already decimal degrees' — true, and it does avoid the DD.MM bug. But the KML value for 10865 is '11.53,48.17,515.0', only 2 decimal places, i.e. ~1.1 km resolution — no better than the catalogue's 2-decimal-minute (~0.6 km) resolution. Both are adequate for station matching, neither is survey-grade; the CDC station-description file is the more precise source at 4 dp (48.1632/11.5429).
- The summary text references '§7' ('this drives the etiquette recommendation in §7') but the summary contains only sections 1-6. Dangling cross-reference, no factual impact.
- Minor: the report's observed 'now' directory mtimes (temperature 07:20, wind 07:15, precipitation 07:10 UTC) are presented as a per-parameter offset pattern. I measured temperature 07:20:01, wind 07:15:01, precipitation 07:40:00 — precipitation did not hold its claimed offset, so the per-parameter stagger is not a fixed schedule you can rely on. Use conditional GET rather than predicting mtimes.
- RESOLVED FROM THE 'unconfirmed' LIST — the DWD GeoWebService is real and live, and the FAQ sentence the report quotes continues past where the report stopped. Full text: 'Der DWD bietet zurzeit keine dedizierte API an. Über den DWD-GeoWebService https://maps.dwd.de ist aber ein API-artiger Zugriff auf ausgewählte Datensätze möglich.' Verified https://maps.dwd.de/ -> 200, redirects to https://maps.dwd.de/geoserver/index.html; WFS GetCapabilities at https://maps.dwd.de/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities returns HTTP 200, 169,173 bytes. This is an officially-referenced structured-query route and a far better fallback than the unofficial warnwetter endpoint.
- ZERO-COST CONFIRMED WITH ONE NUANCE WORTH RECORDING. The FAQ states German geodata are provided 'entgeltfrei' (fee-free) and 'Für den Zugang ist keine Registrierung nötig'. However FAQ section 1.4 does reference a Preisliste: 'Für individuelle Zusammenstellungen, Aufbereitungen und Auslieferungen...' — i.e. custom compilations and bespoke delivery are chargeable. Nothing reachable over opendata.dwd.de is. No credit card, no registration, no paid tier, no quota on any of the 31 URLs I fetched.

### Recommendation for AutoTwin DE

## Recommended implementation for AutoTwin DE

### Forecasts — use MOSMIX_L single-station, not MOSMIX_S
```
https://opendata.dwd.de/weather/local_forecasts/mos/MOSMIX_L/single_stations/{ID}/kml/MOSMIX_L_LATEST_{ID}.kmz
```
~18 KB per station, ~247 hourly steps, 114 elements, refreshed 4x/day at **03/09/15/21 UTC**.

The tempting alternative is MOSMIX_S for hourly updates — but it has **no per-station files**, so one station costs a 36 MB download and a 627 MB XML parse. Only switch to `MOSMIX_S_LATEST_240.kmz` if you genuinely need hourly refresh **and** are ingesting most of the 5,648 stations; then download once per hour and fan out to all stations from that single parse. **Never** fetch the MOSMIX_S all-stations file per station — that is the single worst mistake available here.

Schedule MOSMIX_L polls a few minutes after **03:20 / 09:20 / 15:20 / 21:20 UTC** (observed publication lag was 13-16 min past the hour). Polling more often than 4x/day returns identical bytes.

### Observations — 10-minute `now` for live, hourly `recent` for backfill
```
https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/10_minutes/air_temperature/now/10minutenwerte_TU_{ID:05d}_now.zip
https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/10_minutes/wind/now/10minutenwerte_wind_{ID:05d}_now.zip
https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/10_minutes/precipitation/now/10minutenwerte_nieder_{ID:05d}_now.zip
```
Backfill and gap-repair from `hourly/{param}/recent/stundenwerte_{TU|FF|RR}_{ID:05d}_akt.zip` (~500 days), and `.../historical/` for the long archive.

**Restrict to the stations you actually model.** There is no bulk observation file, so a naive "sync everything" is ~500 requests per parameter per 10 minutes — ~9,000 requests/hour for three parameters. Pick your station subset first.

### Mandatory correctness items (each one is a silent-failure bug)
1. **Decode as `latin-1`**, never UTF-8, for all `.txt` and `.kml`.
2. **`TTT` is Kelvin** — subtract 273.15. **`PPPP` is Pascal** — divide by 100.
3. **KML coordinates are `lon,lat,elev`** — longitude first.
4. **MOSMIX missing value is `-`; observation missing value is `-999`.** Two different sentinels in one pipeline.
5. **Assert `len(values.split()) == len(timesteps)`** before zipping — a silent length mismatch shifts your entire forecast series in time.
6. **Take station coordinates from the KML `<kml:Point>`, not from the MOSMIX catalogue columns** (which are degrees+minutes and will put stations kilometres off). If you must parse the catalogue, convert `DD.MM` sign-aware.
7. **Station ID formatting differs between filename (`00044`, zero-padded 5) and file content (`         44`, space-padded).** Normalise to `int` before joining.
8. `MESS_DATUM` width differs: **12 chars** for 10-minute, **10 chars** for hourly. Parse per-product, not with one shared format string.
9. All observation timestamps are **UTC** with no marker — attach `tzinfo=UTC` explicitly at ingest rather than letting a local-time default creep in.

### Etiquette — implement conditional GET (verified working)
The server returns `ETag` + `Last-Modified` and honours `If-Modified-Since` / `If-None-Match` with **HTTP 304, 0 bytes** (I tested this directly). Store the `Last-Modified`/`ETag` per URL and always send it back. This is the difference between a well-behaved client and an abusive one, and it is the concrete substitute for the rate limit DWD never published. Also set a descriptive `User-Agent` identifying AutoTwin DE with a contact address, keep concurrency modest (2-4 connections), retry with exponential backoff, and never retry a 304 or a 404 tightly.

### Do NOT build on the JSON endpoint
`app-prod-ws.warnwetter.de/v30/stationOverviewExtended` is convenient and works today, but it is the WarnWetter **app backend**, not an open-data service: different host, no DWD-published schema, no versioning or deprecation policy, a typo in its own field name, and `windSpeed`/`windGust`/`cloudCoverTotal` returned null for the station I tested. DWD's own FAQ says it offers **no dedicated API**. Use it at most as an optional convenience fallback behind a feature flag, clearly labelled unofficial — never as the primary source. If you need alerts as structured data, use the **official CAP feed** at `https://opendata.dwd.de/weather/alerts/cap/COMMUNEUNION_DWD_STAT/` instead.

### Attribution to ship in the product
Because AutoTwin DE processes and derives from the data, use the modified-data form, placed adjacent to any displayed DWD-derived value:
> **`Datenbasis: Deutscher Wetterdienst, eigene Elemente ergänzt`**

Use bare `Quelle: Deutscher Wetterdienst` only where values are passed through unchanged. Record CC BY 4.0 in your third-party licence manifest.

### Implementation notes

## Verification result

I independently fetched **all 31 `confirmed_urls`. Every one returned HTTP 200. Zero dead URLs, zero logins, zero paywalls.** I did not take the report's word on schemas — I downloaded and unpacked the ZIPs and KMZs and diffed the headers byte-for-byte. The report's two self-corrections (CC BY 4.0, and the DD.MM catalogue) are both **correct and independently reproduced**. This is an unusually trustworthy report; the errors below are refinements plus **one scheduling bug that would silently ship stale forecasts**.

Confirmed verbatim, by me, on 2026-09-14:
- `TTT` = `288.15` (Kelvin), `PPPP` = `102440.00` (Pascal), `FF` = `2.06` (m/s), `RR1c` = `0.70` (kg/m2) — units read out of `MetElementDefinition.xml`, not assumed.
- MOSMIX_S: **5,648 placemarks, 240 timesteps, 40 elements**, 36,615,583 bytes zipped → **627,662,773 bytes** uncompressed. The 40-element list matches the report's exactly.
- MOSMIX_L single-station: 17,994 bytes, **247 timesteps, 114 elements**, `IssueTime` `2026-09-14T03:00:00.000Z`.
- `MOSMIX_S/single_stations/` really does **404**. The report's headline operational claim holds.
- Conditional GET: both `If-Modified-Since` and `If-None-Match` return **HTTP 304, 0 bytes**.
- MOSMIX missing value is the literal `-` (TX: 226 of 247 values are `-`); observation missing value is `-999`.
- `len(TTT.split()) == 247 == len(timesteps)`.

---

## Fetch these exact URLs

```python
CDC = "https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate"
MOS = "https://opendata.dwd.de/weather/local_forecasts/mos"

# Forecast — one file per station, ~18 KB
f"{MOS}/MOSMIX_L/single_stations/{sid}/kml/MOSMIX_L_LATEST_{sid}.kmz"

# Live observations (today, from 00:00 UTC)
f"{CDC}/10_minutes/air_temperature/now/10minutenwerte_TU_{sid:05d}_now.zip"
f"{CDC}/10_minutes/wind/now/10minutenwerte_wind_{sid:05d}_now.zip"
f"{CDC}/10_minutes/precipitation/now/10minutenwerte_nieder_{sid:05d}_now.zip"

# Backfill — THIS is the one the report omitted; it closes the gap to `now`
f"{CDC}/10_minutes/air_temperature/recent/10minutenwerte_TU_{sid:05d}_akt.zip"
f"{CDC}/10_minutes/wind/recent/10minutenwerte_wind_{sid:05d}_akt.zip"
f"{CDC}/10_minutes/precipitation/recent/10minutenwerte_nieder_{sid:05d}_akt.zip"

# Units — parse, never hardcode
"https://opendata.dwd.de/weather/lib/MetElementDefinition.xml"
```

**Fix the poll schedule.** MOSMIX_L publishes ~73-76 min after the run hour. Poll at **04:20, 10:20, 16:20, 22:20 UTC** — not 03:20/09:20/15:20/21:20 as the report says. Use `MOSMIX_L_LATEST_{sid}.kmz` and read `<dwd:IssueTime>` to learn which run you actually got; the KMZ filename never tells you (the `LATEST` alias unpacks to a member named `MOSMIX_L_2026091403_10865.kml`).

## Parsing

Observations — `;`-delimited with space padding, **ISO-8859-1**, `-999` = null, `eor` sentinel to drop. Verified headers:

| product | header |
|---|---|
| 10-min TU (`now` + `recent`) | `STATIONS_ID;MESS_DATUM;  QN;PP_10;TT_10;TM5_10;RF_10;TD_10;eor` |
| 10-min wind | `STATIONS_ID;MESS_DATUM;  QN;FF_10;DD_10;eor` |
| 10-min precip | `STATIONS_ID;MESS_DATUM;  QN;RWS_DAU_10;RWS_10;RWS_IND_10;eor` |
| hourly TU | `STATIONS_ID;MESS_DATUM;QN_9;TT_TU;RF_TU;eor` |
| hourly wind | `STATIONS_ID;MESS_DATUM;QN_3;   F;   D;eor` |
| hourly precip | `STATIONS_ID;MESS_DATUM;QN_8;  R1;RS_IND;WRTR;eor` |

```python
import pandas as pd, zipfile, io, re


def read_produkt(zbytes):
    z = zipfile.ZipFile(io.BytesIO(zbytes))
    member = next(n for n in z.namelist() if n.startswith("produkt_"))  # NOT "zehn_now_"
    df = pd.read_csv(
        io.BytesIO(z.read(member)),
        sep=";",
        encoding="latin-1",
        skipinitialspace=True,
        na_values=["-999", "-999.0"],
    )
    df.columns = [c.strip() for c in df.columns]
    df = df.drop(columns=[c for c in df.columns if c == "eor"])
    fmt = "%Y%m%d%H%M" if df["MESS_DATUM"].astype(str).str.len().iloc[0] == 12 else "%Y%m%d%H"
    df["ts"] = pd.to_datetime(df["MESS_DATUM"].astype(str), format=fmt, utc=True)
    df["STATIONS_ID"] = df["STATIONS_ID"].astype(
        int
    )  # file has "         44", filename has "00044"
    return df
```
Select the quality column as `[c for c in df.columns if c.startswith("QN")][0]` — it is `QN`, `QN_9`, `QN_3` or `QN_8` depending on product.

MOSMIX — KMZ is a ZIP with exactly one KML member; stream it.
```python
NS = {
    "kml": "http://www.opengis.net/kml/2.2",
    "dwd": "https://opendata.dwd.de/weather/lib/pointforecast_dwd_extension_V1_0.xsd",
}
steps = [t.text for t in root.iterfind(".//dwd:ForecastTimeSteps/dwd:TimeStep", NS)]  # ISO-8601 Z
vals = fc.find("dwd:value", NS).text.split()
assert len(vals) == len(steps)  # non-negotiable; a mismatch time-shifts everything
v = [None if x == "-" else float(x) for x in vals]  # MOSMIX null is "-", NOT -999
# TTT, Td, TX, TN, T5cm: Kelvin -> subtract 273.15
# PPPP: Pascal -> divide by 100
# FF, FX1: m/s already;  RR1c: kg/m2 == mm
lon, lat, elev = map(float, pm.find(".//kml:coordinates", NS).text.split(","))  # lon FIRST
```

## Station identity — build a crosswalk, do not join on id

MOSMIX ids and CDC ids are **different namespaces**, and the 19 numeric collisions are all false. Resolve once at build time by nearest-neighbour on coordinates with an elevation sanity check, and freeze the mapping as a checked-in table:

```python
STATION_MAP = {  # verified by coordinates + elevation, not by id
    "muenchen_stadt": {"mosmix": "10865", "cdc": 3379},  # both elev 515 m
}
```
Take MOSMIX coordinates from the KML `<kml:Point>` (decimal degrees). If you must parse the catalogue at `https://www.dwd.de/DE/leistungen/met_verfahren_mosmix/mosmix_stationskatalog.cfg?view=nasPublication&nn=16102` (299,502 bytes, 5,651 lines, 5,649 data rows, LF, ASCII), it is **DD.MM**: `deg = trunc(v) + (v-trunc(v))*100/60`, applied to `abs(v)` then re-signed. Ids are right-aligned in 5 chars and may be alphanumeric with a leading space — strip them; stripped alphanumeric ids work in URLs (`E426`, `A051` both return 200, `99999` returns 404). CDC station-description files are the opposite format: true fixed-width, CRLF, rows padded to 1000 chars, coordinates in **decimal degrees** at 4 dp — slice by offset, never `split()`, because names contain spaces.

## Fallback ladder when a source is unreachable

1. `MOSMIX_L_LATEST_{sid}.kmz` → on 404/timeout, walk back through the retained timestamped runs in the same directory (8 runs, ~2 days: `MOSMIX_L_{YYYYMMDDHH}_{sid}.kmz`) and mark the record stale using `IssueTime`.
2. Observations: `now` → on 404 fall through to `recent` (it runs to 23:50 yesterday) → `historical/`. Treat a 404 as "station lacks this parameter", not as an outage: 00044 has temperature and precipitation but no wind.
3. Never fan out to `MOSMIX_S_LATEST_240.kmz` per station. Use it only if you ingest most of the 5,648 stations, once per hour at ~HH:45 UTC, parsed with `iterparse` + `elem.clear()` against 627 MB.
4. Prefer the **GeoWebService** (`https://maps.dwd.de/geoserver/ows?service=WFS&version=2.0.0&request=GetCapabilities`, verified 200) over the unofficial `app-prod-ws.warnwetter.de` endpoint. I confirmed the warnwetter endpoint works exactly as the report describes — 10,435 bytes, integers scaled by 10 (`temperature: 156` = 15.6 °C), the `precipitationProbablity` typo, and `windSpeed`/`windGust`/`windDirection` all `None` with `cloudCoverTotal: []`. Its wind fields being null for Munich is disqualifying for a mobility twin. Keep it behind a feature flag or drop it.
5. Alerts: `https://opendata.dwd.de/weather/alerts/cap/COMMUNEUNION_DWD_STAT/` — live, 1,445 CAP zips, multilingual (`_DE`/`_EN`/`_ES`/`_FR`). Twelve sibling variants exist under `/weather/alerts/cap/` (`DISTRICT_*`, `*_CELLS_*`, `*_DIFF` for deltas vs `*_STAT` for full state).

## Etiquette

Persist `ETag` + `Last-Modified` per URL and always replay them — I measured 304/0 bytes, and with 466+277+1,372 files per 10-minute cycle this is the whole ballgame. Restrict to your modelled stations. Set a `User-Agent` naming AutoTwin DE with a contact address, 2-4 connections, exponential backoff. No rate limit is documented anywhere (I re-checked the FAQ: no limits, quotas, or bulk policy) — absence of documentation, not permission.

## Fixture data

Commit these real artefacts; they are small, and every one is a byte-for-byte capture I made:
- `10minutenwerte_TU_00044_now.zip` (**763 B**) → `produkt_zehn_now_tu_20260914_20260914_00044.txt`, 43 rows, `202609140000`→`202609140640`. First data row: `         44;202609140000;    3;   -999;  10.5;   7.9;  97.9;  10.2;eor`
- `10minutenwerte_wind_00096_now.zip` (**517 B**) → `         96;202609140000;    2;   1.7; 290;eor`
- `10minutenwerte_nieder_00044_now.zip` (**457 B**) → `         44;202609140000;    3;-999;   0.00;-999;eor`
- `stundenwerte_FF_00096_akt.zip` and `stundenwerte_RR_00044_akt.zip` — needed precisely because these two headers are unguessable.
- `MOSMIX_L_LATEST_10865.kmz` (**17,994 B**) — the single best fixture: exercises KMZ-in-ZIP, ISO-8859-1 XML, 247-step axis, Kelvin `TTT`, Pascal `PPPP`, `lon,lat,elev` order, and `-` nulls (`TX` has 226 of them) all at once.
- A **404 fixture**: the 146-byte nginx HTML page from `.../10_minutes/wind/now/10minutenwerte_wind_00044_now.zip`. Assert your loader rejects it on status rather than feeding it to `zipfile`.
- A trimmed `MetElementDefinition.xml` (26,484 B full) and a ~20-line slice of the station catalogue including `10865 ---- MUENCHEN STADT        48.10   11.32   515` and ` A051 ---- WEESBY                54.50    9.09    22` to pin both the DD.MM conversion and leading-space id stripping.

Write a regression test asserting `mosmix_to_cdc("10865") == 3379` and that no code path joins the two id spaces numerically — that is the failure this data set is most likely to ship silently.

## Attribution

Verified verbatim on the official Vorlagen page (legal basis §7 DWD-Gesetz): unmodified use → `Quelle: Deutscher Wetterdienst`; modified/derived → `Datenbasis: Deutscher Wetterdienst, eigene Elemente ergänzt`. AutoTwin DE derives, so use the `Datenbasis:` form, placed immediately adjacent to the displayed value. Licence is **CC BY 4.0** (`Nutzungsbedingungen_German.txt`, 149 bytes, *Stand: Mai 2024*); "GeoNutzV" returns **0 hits** across all three current DWD legal pages. One rule to encode if you ever render CAP alerts: if official warnings are modified, the source note must be **deleted**, not adapted.

### Unconfirmed / open questions

- RATE LIMITS: I could NOT find any documented rate limit, request quota, throttling rule, concurrent-connection cap, or bulk-download policy anywhere on opendata.dwd.de or the dwd.de Open Data pages. The FAQ is silent on the subject. This is an ABSENCE OF DOCUMENTATION, not a confirmed absence of enforcement — nginx-level limits could exist and simply be undocumented. Do not read this as permission for unlimited polling. I did not attempt any load test, which would have been inappropriate.
- The English CDC 'Terms_of_use.pdf' (61,006 bytes) returns HTTP 200 and is dated 15-May-2024, but my text extraction failed on its embedded font subsets. I verified its existence, size, and date but did NOT read its contents. My licence findings rest on the German 'Nutzungsbedingungen_German.txt' and the dwd.de legal pages, both of which I did read in full.
- Internal column names of the HOURLY wind (stundenwerte_FF_*_akt.zip) and HOURLY precipitation (stundenwerte_RR_*_akt.zip) produkt files. I verified the directory paths, exact filename patterns, and the station-description filenames, but did not unpack these two archives. Only the 10-minute wind/precipitation and the hourly/10-minute temperature schemas were opened and read. Verify these two before coding against them.
- MOSMIX element counts differ slightly between my direct count and DWD's documentation: I counted 114 distinct dwd:elementName values in a real MOSMIX_L file, while the official product page states 115. Likely an element absent from the particular station/run I sampled, or a documentation rounding. Immaterial for the four quantities requested, but do not hardcode 115.
- The MOSMIX station catalogue lives on www.dwd.de (a .cfg CMS endpoint with a view= query parameter), NOT on opendata.dwd.de. I searched /weather/local_forecasts/ and found no catalogue file there. A CMS URL is inherently less stable than an opendata path, and the nn=16102 parameter looks like a CMS node id that could change. Treat the catalogue URL as the most fragile link in this report; the directory listing at MOSMIX_L/single_stations/ is a more durable way to enumerate available station IDs, and the KML files carry names and coordinates anyway.
- Whether the 'now' observation directories update at a strictly fixed cadence. Directory mtimes on the fetch day were staggered (temperature 07:20, wind 07:15, precipitation 07:10 UTC), consistent with ~10-minute cycles offset per parameter, but I observed only one snapshot and did not poll over time to confirm the interval or its jitter.
- Exact retention windows. Observed on the fetch day: MOSMIX_S held runs from 12-Sep-07Z to 14-Sep-06Z (~48 h) and MOSMIX_L held 8 runs (~2 days). These are single-snapshot observations, not a documented DWD retention policy.
- Whether DWD has ever formally objected to, or tacitly tolerates, third-party use of app-prod-ws.warnwetter.de. I confirmed it is undocumented by DWD and that DWD states it offers no dedicated API, but I found no official statement either permitting or prohibiting external use. 'Not officially documented or permitted' is my honest reading of the evidence; it is not a quoted DWD prohibition.
- I did not verify the DWD GeoWebService / CQL-filtered access route mentioned in passing by the FAQ as an API-like alternative. It may be a better-supported structured-query option than the unofficial JSON endpoint and is worth a follow-up look if AutoTwin DE wants server-side filtering.

---

## 4. Autobahn GmbH — Verkehrsdaten-API

**Verification verdict:** `PARTIAL`

**Licence:** UNDECLARED — this is a real gap, not an oversight in my research. The official OpenAPI spec at https://raw.githubusercontent.com/bundesAPI/autobahn-api/main/openapi.yaml contains NO `license` field and NO `termsOfService` field (verified by grepping the raw YAML). https://www.autobahn.de/nutzungsbedingungen returns HTTP 404. The bundesAPI README states no licence, no terms. The only contact points declared are `info.contact.email: kontakt@autobahn.de` and `info.contact.url: https://www.autobahn.de/datenschutz/` (a privacy policy, not a data licence), with `x-office: Bundesministerium für Digitales und Verkehr`. Web search surfaced a claim that Autobahn GmbH offers traffic data under "usage conditions including a license with restricted use, available for free" via a Mobilithek offer page, but that page is an SPA and I could NOT read it to confirm the licence name verbatim — treat as unconfirmed. PRACTICAL GUIDANCE: attribute as "Quelle: Die Autobahn GmbH des Bundes (verkehr.autobahn.de)"; German federal open data is commonly Datenlizenz Deutschland – Namensnennung 2.0, but I could not confirm that this specific dataset carries it. Email kontakt@autobahn.de before any commercial use. For a non-commercial portfolio project with clear attribution, the risk is low.

**Authentication:** NO — the Autobahn API is fully keyless and anonymous, confirmed by live fetch. No API key, token, header, or cookie of any kind. Responses carry no `WWW-Authenticate`. `Access-Control-Allow-Origin: *` is set, so it is callable directly from browser JavaScript with no backend proxy required. Rate limiting: none documented and none observed — 40 sequential requests in 9 seconds (~4.4 req/s) returned 40x HTTP 200 with zero throttling and no `X-RateLimit-*` or `Retry-After` headers; concurrent sweeps with 12 workers across all 109 roads also completed cleanly. Absence of an enforced limit is not permission to hammer it: cache server-side and refresh every 2-5 minutes. CONTRAST — Mobilithek: brokered real-time DATEX II REQUIRES (a) prior registration of a legal organisation, and (b) an X.509v3 machine certificate issued by the Mobilithek operator, requested through the admin GUI by the organisation's administrator, delivered by email with the signing password sent via SMS to a registered mobile number, then used for client-certificate mTLS (TLS 1.2/1.3). Non-brokered "offene Daten" offers on Mobilithek can be browsed and downloaded with no account at all.


### Findings

## Verdict

**Implement the Autobahn API as the primary `TrafficProvider`.** It is live, keyless, CORS-open, unthrottled in practice, and returns GeoJSON geometry. Mobilithek's real-time DATEX II is gated behind organisational registration + an X.509 machine certificate issued by the platform operator — not usable for a portfolio project.

All findings below were verified by live HTTP on **2026-09-14**, not from documentation alone.

## 1. Autobahn API — confirmed live

Base URL: `https://verkehr.autobahn.de/o/autobahn`

No API key, no token, no header, no cookie. Response headers contain only:
```
Access-Control-Allow-Origin: *
X-Powered-By: Express
ETag: W/"..."
```
No `WWW-Authenticate`, no `X-RateLimit-*`, no `Cache-Control`. `Access-Control-Allow-Origin: *` means it is callable **directly from browser JS** with no proxy.

### CRITICAL TRAP: every unknown path returns HTTP 200 with an empty array

```
GET /o/autobahn/A1/services/bogus_endpoint_xyz
→ 200 {"bogus_endpoint_xyz":[]}
```
The server echoes the last path segment as a top-level key with `[]`. **A 200 does not prove an endpoint exists.** This matters directly for your question about a "traffic flow" endpoint:

- **There is NO traffic-flow endpoint.** `/services/traffic_flow` returns `{"traffic_flow":[]}` — identical behaviour to `bogus_endpoint_xyz`. It is not in the OpenAPI spec. Same for `trafficflow`, `lorry_parking`, `charging_station`. Do not implement these.
- Traffic-flow-like data lives inside the **`warning`** endpoint instead, via `abnormalTrafficType`, `averageSpeed`, `delayTimeValue`.

### CRITICAL: webcams are documented but empty nationwide

I swept `/services/webcam` across **all 109 road ids**: **total webcams = 0**. The endpoint is in the official OpenAPI spec but returns `{"webcam":[]}` for every single Autobahn. Do not build a webcam feature; it will render empty.

### Live nationwide volumes (2026-09-14)

| Endpoint | Items nationwide |
|---|---|
| roadworks | **3,327** |
| closure | **648** |
| warning | **52** |
| webcam | **0** |
| parking_lorry | ~520 (5 roads sampled) |
| electric_charging_station | ~170 (5 roads sampled) |

### The roads list is dirty — strip and dedupe

`GET /o/autobahn/` returns 109 entries but **108 unique after trimming**. `"A60 "` has a **trailing space** and returns 0 roadworks, while `"A60"` returns 14. Lowercase suffixes `A64a`, `A99a` are legitimate. Always `.strip()` and dedupe, or you silently lose a motorway.

## 2. Schema gotchas that will bite the implementation

- **`isBlocked` is a STRING `"false"`, not a boolean** — and across **3,975 roadworks + closures it was `"true"` exactly ZERO times**. The field is effectively dead. The real lane-closure signal is `impact.symbols` containing `"CLOSED"` (3,302/3,975 items). Do not branch on `isBlocked`.
- **`startTimestamp` is OPTIONAL and the omission is perfectly correlated with `display_type`**: `ROADWORKS` → present 1875/1875; `SHORT_TERM_ROADWORKS` → absent 1452/1452. In the *list* endpoint the key is **missing entirely**; in the *detail* endpoint it is present but `null`. Parsing must tolerate both.
- **`coordinate` has TWO INCOMPATIBLE SHAPES across endpoints** — the single nastiest trap:
  - roadworks / closure / warning → `{"lat": 48.40, "long": 11.59}` (note `long`, **not** `lng` or `lon`)
  - parking_lorry / electric_charging_station → GeoJSON `{"type":"Point","coordinates":[lon, lat]}` (**lon first**)
- **Three different timestamp formats**: roadworks `2026-08-10T11:00:00+02:00` (local offset), warning `2026-09-14T04:41:00Z` (Zulu), electric_charging_station `30.03.2026` (German `DD.MM.YYYY`).
- **`extent` / `point` are comma-joined STRINGS in lat,lon order**, while `geometry.coordinates` is GeoJSON **lon,lat**. Mixed axis order within a single object.
- **The bund.dev OpenAPI spec is out of date.** Live responses include `geometry`, `impact`, and `startLcPosition`, which the spec's schema does not list. Trust the live payload over the spec.
- `routeRecommendation` was **empty in all 3,327 roadworks**. Model it, don't rely on it.

## 3. Rate limits and licence

- **Rate limit: none documented, none observed.** 40 sequential requests in 9s (~4.4 req/s) → 40× HTTP 200, zero throttling, no rate-limit headers. Be a good citizen anyway: cache server-side, ~1 refresh/2–5 min. A full national sweep is 108 roads × 1 request per layer; my concurrent sweeps (12 workers) completed without error.
- **Licence: NOT formally declared — this is a genuine gap.** The OpenAPI spec has **no `license` and no `termsOfService` field** (verified by grepping the raw YAML). `https://www.autobahn.de/nutzungsbedingungen` returns **404**. `info.contact` is `kontakt@autobahn.de`, `x-office: Bundesministerium für Digitales und Verkehr`. Practical guidance: attribute as *"Quelle: Die Autobahn GmbH des Bundes (verkehr.autobahn.de)"*, and email `kontakt@autobahn.de` before any commercial use.
- Noted: **`https://devportal-test.autobahn.de/`** exists and returns 200 — an official *sign-in-gated* "API Testumgebung / Single Point of Contact für den Zugang zu Autobahn-APIs". This suggests Autobahn GmbH may be moving toward a keyed official portal. Keep the provider behind an interface so a future auth requirement is a one-class change.

## 4. Mobilithek — not viable for a portfolio project

From the authoritative **Technische Schnittstellenbeschreibung v1.3.2 (07.11.2025)**, §8 "Zertifikatsbasierte M2M-Kommunikation", which I downloaded and extracted (99 pages):

- The Security component **requires certificate-based exchange** between provider/consumer systems and the platform. Client auth uses **X.509v3 certificates issued by the Mobilithek operator**, over TLS 1.2/1.3 mTLS.
- Onboarding: your **organisation must already be registered**; the org administrator requests a *Maschinenzertifikat* via the admin GUI; the certificate is **emailed to the org admin** and the **signing password is sent by SMS** to a registered mobile number.
- **Blocker:** this requires a registered legal organisation, a named administrator, and an SMS-verifiable phone number. A solo portfolio project cannot realistically clear it.

Nuance worth keeping: Mobilithek is **browsable without an account**, and **non-brokered "offene Daten" offers can be downloaded without registration**. So static/bulk open-data files are reachable — but the **brokered real-time DATEX II traffic feed you'd actually want is the certificate-gated path**.

There is also **no documented public REST API** for Mobilithek. Its frontend is an Angular/Vite SPA calling an internal gateway (`/mdp-api/mdp-msa-{users,metadata,contracts,files,reports,...}/v{N}/...`). I confirmed one unauthenticated internal endpoint responds (`/mdp-api/mdp-msa-metadata/v1/vocabs` returns real JSON), but this is an **undocumented private contract with no stability guarantee** — do not build on it.

## 5. Recommendation

Primary: **Autobahn API**. Rationale: zero-auth (works in a demo the moment someone clicks the link), official federal source, national coverage (3,327 roadworks + 648 closures live), GeoJSON `LineString` geometry ready for map rendering, CORS-open, no observed throttling.

Implement layers in this order: `roadworks` → `closure` → `warning`. **Skip `webcam` (empty nationwide) and skip any traffic-flow endpoint (does not exist).**

Scope limit to state honestly in the project README: the API covers **Bundesautobahnen only** — no Bundesstraßen, no urban/municipal roads. (The bund.dev blurb saying "Bundesstraßen" is inaccurate; the roads list is exclusively `A*`.)

### Schema / format details

## `GET /o/autobahn/` — list Autobahnen

```json
{"roads": ["A1","A2","A3", ..., "A60 ", "A64a", "A99a"]}
```
Top-level key: **`roads`**. 109 entries, **108 unique after `.strip()`**.

## `GET /o/autobahn/{roadId}/services/roadworks`

Top-level key: **`roadworks`** (array).

| Field | Type | Always present? | Notes |
|---|---|---|---|
| `identifier` | string | yes (3327/3327) | Opaque; contains dots/dashes. Used as-is in the detail URL. |
| `icon` | string | yes | Only 2 values, 1:1 with `display_type`: `"123"` ↔ ROADWORKS, `"warnkegel"` ↔ SHORT_TERM_ROADWORKS |
| `isBlocked` | **string** | yes | `"false"` in **3327/3327**. Never `"true"`. Effectively dead — do not use. |
| `future` | **bool** | yes | Real boolean. 2252 false / 1075 true. Planned-but-not-started works. |
| `extent` | string | yes | `"lat1,lon1,lat2,lon2"` — comma-joined **lat-first** |
| `point` | string | yes | `"lat,lon"` — **lat-first** |
| `startLcPosition` | string | yes | Numeric-as-string, e.g. `"14"` |
| `impact` | object | yes | `{lower: string, upper: string, symbols: string[]}` — exactly these 3 keys |
| `display_type` | string | yes | **Exactly 2 values**: `ROADWORKS` (1875), `SHORT_TERM_ROADWORKS` (1452) |
| `subtitle` | string | yes | Direction, e.g. `" Nürnberg -> München"` — **note leading space** |
| `title` | string | yes | `"A9 \| Allershausen - Aster Moos"` |
| `startTimestamp` | string | **NO — 1875/3327** | ISO8601 w/ offset `2026-08-10T11:00:00+02:00`. **Key absent** in list when `display_type == SHORT_TERM_ROADWORKS` (perfect correlation). |
| `coordinate` | object | yes | `{"lat": float, "long": float}` — field is **`long`**, not `lng`/`lon` |
| `description` | string[] | yes | German free text lines; `""` used as blank separators |
| `routeRecommendation` | string[] | yes | **Empty in all 3327 items** |
| `footer` | string[] | yes | Usually empty for roadworks |
| `lorryParkingFeatureIcons` | array | yes | Empty for roadworks |
| `geometry` | object | yes | `{"type":"LineString","coordinates":[[lon,lat],...]}` — **GeoJSON lon-first**. Not in the OpenAPI spec but always present live. |

### `impact.symbols` vocabulary (measured over 3,975 roadworks+closures)
`CLOSED` (5842), `ARROW_UP` (5669), `SEPARATE` (4492), `BORDER_RIGHT` (2916), `ARROW_DOWN` (1954), `BREAKDOWN_LANE` (1299), `BORDER_LEFT` (879). A lane diagram, read left→right.

**Use `impact.symbols.includes("CLOSED")` as the blocking signal** (3302/3975 items), never `isBlocked`.

### REAL example item, fetched live 2026-09-14 from `/o/autobahn/A1/services/roadworks`

```json
{
  "identifier": "2023-000357--vi-bs.2026-08-10_11-00-00-000.devi-zus.2024-10-01_00-00-00-000.f.de13",
  "icon": "123",
  "isBlocked": "false",
  "future": false,
  "extent": "49.38191589297737,7.002339290843191,49.41258498396637,6.993871225155498",
  "point": "49.38191589297737,7.002339290843191",
  "startLcPosition": "8",
  "impact": {
    "lower": "Eppelborn",
    "upper": "Saarbrücken",
    "symbols": ["BREAKDOWN_LANE","BORDER_LEFT","CLOSED","ARROW_DOWN","SEPARATE","ARROW_UP","CLOSED","BORDER_RIGHT","BREAKDOWN_LANE"]
  },
  "display_type": "ROADWORKS",
  "subtitle": " Saarbrücken -> Trier",
  "title": "A1 | Saarbrücken - Eppelborn",
  "startTimestamp": "2026-08-10T11:00:00+02:00",
  "coordinate": { "lat": 49.38191589297737, "long": 7.002339290843191 },
  "description": [
    "Zeitraum dieser Bauphase:",
    "Beginn: 10.08.26 um 11:00 Uhr",
    "Ende: 10.10.26 um 20:00 Uhr",
    "(Ende der Gesamtmaßnahme: 10.10.26)",
    "",
    "A1: Saarbrücken -> Trier, zwischen 3.6 km hinter AK Saarbrücken und 0.3 km vor AS Eppelborn",
    "",
    "Länge: 4.87 km | Maximale Durchfahrtsbreite: 3.25 m",
    "",
    "A1 Arbeiten an Schutzeinrichtungen A.05226.00"
  ],
  "routeRecommendation": [],
  "footer": [],
  "lorryParkingFeatureIcons": [],
  "geometry": {
    "type": "LineString",
    "coordinates": [[7.002339291,49.381915893],[7.0025404,49.382234101],[7.0030646,49.383050301]]
  }
}
```
(geometry truncated for brevity; the live array is longer.)

## `GET /o/autobahn/details/roadworks/{identifier}`

Same 18 fields **plus 4**, all `null` for roadworks: `abnormalTrafficType`, `averageSpeed`, `delayTimeValue`, `source`.
Also: `startTimestamp` is **always present here but may be `null`** — differs from the list endpoint where the key is absent.

## `GET /{roadId}/services/closure` — key `closure`

Identical field set to roadworks. `display_type`: `CLOSURE` (95) / `CLOSURE_ENTRY_EXIT` (553). `startTimestamp` optional. `isBlocked` `"false"` in 648/648.

## `GET /{roadId}/services/warning` — key `warning`

Roadworks fields **minus** `impact`, **plus** `abnormalTrafficType`, `averageSpeed`, `delayTimeValue`, `source`.

- `display_type`: `WARNING` only. `icon`: `"101"` only (52/52).
- `abnormalTrafficType` (n=52): `QUEUING_TRAFFIC` 22, `SLOW_TRAFFIC` 16, `null` 7, `UNSPECIFIED_ABNORMAL_TRAFFIC` 6, `HEAVY_TRAFFIC` 1
- `delayTimeValue`: numeric string, e.g. `"5"` (minutes). `averageSpeed` missing on 5/16 in sample.
- `startTimestamp` here is **Zulu**: `"2026-09-14T04:41:00Z"`

```json
{"identifier":"INRIX--vi-avl.2026-09-14_04-41-00-000_017.de0","icon":"101","isBlocked":"false",
 "future":false,"display_type":"WARNING","subtitle":" Osnabrück -> Bremen",
 "title":"A1 | Engelmannsbäke-Nord - Wildeshausen",
 "startTimestamp":"2026-09-14T04:41:00Z","delayTimeValue":"5"}
```

## `GET /{roadId}/services/parking_lorry` — key `parking_lorry`

**`coordinate` is GeoJSON, not `{lat,long}`:**
```json
{"identifier":"DE-SL-000009","icon":"314-50","isBlocked":"false","future":false,
 "startLcPosition":"5","display_type":"PARKING","subtitle":"Neuhaus W","title":"A1 | undefined",
 "coordinate":{"type":"Point","coordinates":[6.968336,49.310742]},
 "description":["PKW Stellplätze: 0","LKW Stellplätze: 6"],
 "routeRecommendation":[],"footer":["Koordinaten: undefined"]}
```
Note the literal string `"undefined"` leaking into `title` and `footer` — upstream bug; sanitise it.

## `GET /{roadId}/services/electric_charging_station` — key `electric_charging_station`

`coordinate` also GeoJSON Point. `display_type`: `STRONG_ELECTRIC_CHARGING_STATION` / `ELECTRIC_CHARGING_STATION`. `point` and `extent` missing on 21/170. **`startTimestamp` here is German `"30.03.2026"`, not ISO.**

## Suggested normalised model

```
TrafficEvent {
  id: identifier
  kind: ROADWORKS | SHORT_TERM_ROADWORKS | CLOSURE | CLOSURE_ENTRY_EXIT | WARNING
  road: roadId                      # from request, NOT in payload
  title, subtitle (trim!)
  lat, lon                          # normalise BOTH coordinate shapes here
  geometry: GeoJSON | null
  startsAt: datetime | null         # 3 formats; tolerate missing key
  isFuture: future                  # the only trustworthy boolean
  lanesClosed: "CLOSED" in impact.symbols
  descriptionLines: description[]
}
```

### Verified endpoints

| status | URL | purpose |
|---|---|---|
| `live` | <https://verkehr.autobahn.de/o/autobahn/> | List all Autobahnen. Returns {"roads":["A1","A2",...]} |
| `live` | <https://verkehr.autobahn.de/o/autobahn/A9/services/roadworks> | Roadworks (Baustellen) for one road. Pattern: /{roadId}/services/roadworks |
| `live` | <https://verkehr.autobahn.de/o/autobahn/A1/services/closure> | Closures (Sperrungen). Pattern: /{roadId}/services/closure |
| `live` | <https://verkehr.autobahn.de/o/autobahn/A1/services/warning> | Traffic warnings (Warnungen/Verkehrsmeldungen). Pattern: /{roadId}/services/warning |
| `live` | <https://verkehr.autobahn.de/o/autobahn/A1/services/parking_lorry> | Lorry parking areas. Pattern: /{roadId}/services/parking_lorry |
| `live` | <https://verkehr.autobahn.de/o/autobahn/A1/services/electric_charging_station> | EV charging stations. Pattern: /{roadId}/services/electric_charging_station |
| `live` | <https://verkehr.autobahn.de/o/autobahn/A1/services/webcam> | Webcams. Pattern: /{roadId}/services/webcam |
| `live` | <https://verkehr.autobahn.de/o/autobahn/details/roadworks/2026-034009--vi-bs.2026-09-14_07-00-00-000.devi-zus.2026-07-13_06-00-00-000_003.de3> | Single roadwork detail. Pattern: /details/roadworks/{identifier} |
| `unverified` | <https://verkehr.autobahn.de/o/autobahn/details/closure/{closureId}> | Closure detail |
| `unverified` | <https://verkehr.autobahn.de/o/autobahn/details/warning/{warningId}> | Warning detail |
| `live` | <https://raw.githubusercontent.com/bundesAPI/autobahn-api/main/openapi.yaml> | Official OpenAPI 3.0.0 spec. servers: https://verkehr.autobahn.de/o/autobahn |
| `live` | <https://autobahn.api.bund.dev/> | Human-readable bund.dev documentation portal |
| `live` | <https://github.com/bundesAPI/autobahn-api> | Community repo maintaining the spec |
| `live` | <https://devportal-test.autobahn.de/> | Official Autobahn GmbH developer portal (test environment) |
| `live` | <https://mobilithek.info/cms/assets/65545c25-c155-488c-9c7c-b9da1c7685b5?download=> | Mobilithek Technische Schnittstellenbeschreibung v1.3.2 (07.11.2025) — authoritative on auth |
| `live` | <https://mobilithek.info/mdp-api/mdp-msa-metadata/v1/vocabs> | Mobilithek internal metadata vocabulary endpoint |
| `unverified` | <https://mobilithek.info/offers/748580849261105152> | A Mobilithek data offer detail page |

### Corrections applied by the verifier

- OSRM demo server is NOT 'non-commercial only'. The report states 'Reasonable, non-commercial use only', 'not acceptable for anything commercial', and 'It is non-commercial-only'. The actual policy (raw wiki text) forbids only SELLING ACCESS and PAYWALLING, and explicitly contemplates commercial products using it: 'Access to the demo server shall not be behind a paywall: If the Demo Server is used in a commercial product, it needs to be publicly accessible and featuring proper attribution.' The practical advice (self-host before launch) still stands, but the stated legal reason is wrong.
- The '1 request/second' limit is misattributed. The report's confirmed_urls note for github.com/Project-OSRM/osrm-backend/wiki/Api-usage-policy says 'Max 1 req/sec'. That string does not appear anywhere in that page. I pulled the raw wiki markdown (2327 bytes): it says only 'Excessive use is not allowed. If your requests are impacting the service stability, we will block you.' The 1 req/sec figure comes solely from routing.openstreetmap.de/about.html. The wiki also self-describes as stale: 'The following terms applied to the previous demo server but are still good practice' and redirects to routing.openstreetmap.de/about.html + fossgis.de/arbeitsgruppen/osm-server/nutzungsbedingungen/ as the operative policy.
- annotation.speed provenance is wrong (the m/s conclusion itself is CORRECT). The report says 'Some third-party docs claim km/h - the data does not support that.' In fact OSRM's own docs/http.md line 783 defines it with no unit at all: 'speed: Convenience field, calculation of distance / duration rounded to one decimal place'. The 'km/h' at line 652 of the same file belongs to the TILE SERVICE speeds layer (/tile/v1/), a different endpoint. So no official doc claims the route annotation is km/h. I confirmed m/s arithmetically: distance[0]=8.046216758 m / duration[0]=0.9 s = 8.94 -> speed[0]=8.9.
- annotation array lengths are NOT uniformly n-1. With annotations=true, 'nodes' has length n (= geometry coordinate count), while distance/duration/speed/weight/datasources have n-1. Verified on a short Frankfurt route: nodes=131, all others=130. The report states the n-1 rule without this exception, which will cause an off-by-one if anyone zips nodes against the other arrays.
- 'Both returned byte-identical routing values (204881.5 m / 8148.9 s)' is placed immediately after listing routed-car/routed-bike/routed-foot and reads as if all three agree. They do not. Verified: routed-car 204881.5 m / 8148.9 s; routed-bike 212360.2 m / 33278.4 s; routed-foot 184839.9 m / 147645 s. Only router.project-osrm.org == routed-car.
- weight_name is not always 'routability' and weight does not always equal duration. The report's documented response shape hardcodes weight_name:'routability' with weight==duration. Verified: routed-bike returns weight_name='duration' (weight 33278.4); routed-foot returns weight_name='routability' with weight=17744.35 but duration=147645. An adapter that treats weight as a duration proxy breaks on the foot profile.
- Tile 12/2138/1376 is NOT Frankfurt. The report labels it 'z12 Frankfurt' for the VersaTiles, OSMF-vector, OSM-raster and CARTO smoke tests. That tile's NW corner is lon 7.9102, lat 50.7365 (Hunsrueck/Bingen area). Frankfurt (8.6821, 50.1109) at z12 is 12/2146/1387. Cosmetic, but misleading if used as a named fixture.
- The OSM Standard tile block manifests as a SILENT HTTP 200, not an error - the report never says this. Verified: with User-Agent 'python-requests/2.32.3' (or curl default, or any UA the CDN dislikes) tile.openstreetmap.org returns HTTP 200 with a fixed 6987-byte placeholder PNG, md5 c069a15b2cc2d6b6f527ad09eb93c61a, byte-identical for EVERY z/x/y I tried (0/0/0, 5/16/10, 10/536/346, 12/2138/1376, 12/2146/1387, 14/8586/5551). With a compliant UA the same tile returns the real 24960 bytes the report claimed. The OSMF policy corroborates this by name: 'Many HTTP clients and SDKs use a generic User-Agent header (e.g. okhttp/x.y, Go-http-client/1.1, python-requests/x.y, Java/1.8, curl/x.y). Traffic that uses these defaults will be blocked.' Status-code checks cannot detect this.
- Only tile.openstreetmap.org UA-gates. I re-ran every tile/route host under both 'python-requests/2.32.3' and a compliant UA: tile.openstreetmap.de, tiles.versatiles.org, vector.openstreetmap.org, tiles.openfreemap.org, basemaps.cartocdn.com and routing.openstreetmap.de all returned byte-identical responses under both. The report's blanket UA advice is only load-bearing for tile.openstreetmap.org.
- The OpenFreeMap '3d' style is a non-issue the report invented. It presents '/styles/3d returns 404' as a 'Correction to a common assumption' and lists it under unconfirmed as 'possibly unreleased or renamed'. The homepage lists exactly five styles - Positron, Bright, Liberty, Dark, Fiord. '3D' appears only as a section heading about 3D/terrain capability, never as a style entry. The 404 is expected; nothing is missing or renamed.
- CARTO's attribution string is not where the report says. The report claims 'I extracted the attribution string embedded in CARTO's TileJSON.' The GL style.json files at basemaps.cartocdn.com have sources.carto.attribution = null. The attribution actually lives one hop away in https://tiles.basemaps.cartocdn.com/vector/carto.streets/v1/tiles.json, verified as: '&copy; <a href="https://carto.com/about-carto/" target="_blank" rel="noopener">CARTO</a>, &copy; <a href="http://www.openstreetmap.org/about/" target="_blank">OpenStreetMap</a> contributors' (the report dropped the target/rel attributes).
- The OSMF tile policy quote 'the vast majority of its tiles serve third-party sites' does not exist in the current policy text. The current page says 'We welcome creative uses and do not require you to use a specific API.' Likewise the policy contains no explicit permission for 'low-traffic hobby/portfolio apps' - that is the report's inference, not a quote. It states availability is best-effort with no SLA and that access may be blocked without notice.
- The report omits four current OSMF tile-policy requirements: (1) the plain-HTTP URL http://tile.openstreetmap.org/{z}/{x}/{y}.png is explicitly prohibited, HTTPS only; (2) use conditional requests with If-None-Match / If-Modified-Since for expired tiles; (3) recommended 'Report a map issue' link to https://www.openstreetmap.org/fixthemap; (4) recommended not to hard-code the tile URL so it can be switched without a software update.
- code == 'Ok' is not sufficient validation and the report does not warn about this. Verified against routed-car: mid-Atlantic coordinates (-30,-40;-31,-41) return code 'Ok' with distance 0, duration 0, and both waypoints snapped to the same point 1,652,324 m away. Swapped lat/lon input (50.1109,8.6821;48.7758,9.1829) returns code 'Ok' with a plausible-looking 239771.9 m route. The report lists 'NoRoute' as a possible code but an adapter checking only code != 'Ok' will silently accept garbage.

### Recommendation for AutoTwin DE

Implement the **Autobahn API** as the primary `TrafficProvider`. Concretely:

**1. Endpoint constants**
```
BASE = "https://verkehr.autobahn.de/o/autobahn"
GET  {BASE}/                                                  -> {"roads": [...]}
GET  {BASE}/{roadId}/services/roadworks                       -> {"roadworks": [...]}
GET  {BASE}/{roadId}/services/closure                         -> {"closure": [...]}
GET  {BASE}/{roadId}/services/warning                         -> {"warning": [...]}
GET  {BASE}/{roadId}/services/parking_lorry                   -> {"parking_lorry": [...]}
GET  {BASE}/{roadId}/services/electric_charging_station        -> {"electric_charging_station": [...]}
GET  {BASE}/details/roadworks/{identifier}                    -> single object (raw identifier, no base64)
```
Do **NOT** implement `webcam` (0 items nationwide) or any traffic-flow endpoint (**does not exist**).

**2. Validate responses structurally, never by status code.** Because unknown paths return `200 {"<segment>": []}`, assert the expected top-level key is present *and* that the provider is actually returning data. Add a startup smoke test: `GET {BASE}/A9/services/roadworks` must yield > 0 items.

**3. Sanitise the roads list on ingest:** `{r.strip() for r in roads}` → 108 ids. Skipping this silently drops A60.

**4. Write one normalisation layer** that absorbs the schema inconsistencies:
- Two `coordinate` shapes → detect `"lat" in coord` vs `coord["type"] == "Point"`; remember GeoJSON is `[lon, lat]`.
- Three timestamp formats → ISO-with-offset, ISO-Zulu, and `DD.MM.YYYY`. Use `startTimestamp` only if the key exists *and* is non-null.
- `isBlocked` → **ignore entirely** (never `"true"` in 3,975 observed items). Derive blocking from `"CLOSED" in impact.symbols`.
- `future` is the one reliable boolean — use it to separate active from planned works.
- Strip leading whitespace from `subtitle`; filter the literal string `"undefined"` out of parking/charging titles.

**5. Fetch strategy:** fan out across 108 roads concurrently (8–12 workers is proven safe), cache the merged result server-side with a 2–5 minute TTL, and serve your UI from the cache. A full national roadworks snapshot is ~3,300 items. Set a descriptive `User-Agent`.

**6. Attribution:** render "Quelle: Die Autobahn GmbH des Bundes" in the UI footer, and note in the README that the licence is not formally declared and that commercial use should be cleared with kontakt@autobahn.de.

**7. Future-proofing:** keep everything behind a `TrafficProvider` interface. `devportal-test.autobahn.de` signals Autobahn GmbH may introduce a keyed official portal; if the open endpoint is ever retired, you want to swap one implementation class.

**Do not attempt Mobilithek** for real-time data — the certificate onboarding needs a registered organisation, a named admin, and SMS verification. If you later want DATEX II for depth, the only realistic no-account path is downloading a non-brokered open-data offer manually and shipping it as a static enrichment file; treat that as a nice-to-have, not the live provider.

**Scope honesty for the README:** this covers Bundesautobahnen only — no Bundesstraßen, no urban roads. The bund.dev description mentioning "Bundesstraßen" is inaccurate; the roads list is exclusively `A*`.

### Implementation notes

## Verification result up front

**All 36 `confirmed_urls` are live and public. Zero dead URLs.** I fetched every one anonymously (200, or 206 where I used a range request). Every byte count in the report reproduced *exactly*: OSRM 242736; FOSSGIS 626/619/622; Geofabrik 344994933 / 647326315 / 4838703123; OpenFreeMap 25153/20959/43079/48713/22234; VersaTiles 168597/168044/168312/101306/168085 and 20125; OSMF 8127/28878; CARTO 106878/70431/10628; OSM raster 24960. The empirical layer of this report is unusually trustworthy. The **policy-interpretation layer is where the errors are** (see corrections — especially the "non-commercial" misreading).

**Zero-cost check: passes.** No credit card, account, or registration anywhere in the recommended stack. `ghcr.io/project-osrm/osrm-backend` also pulls anonymously — I got an anonymous registry token and a 200 on the `latest` manifest, 100 tags listed (current: v26.6.x). No Docker Hub login needed. CARTO is the only component requiring a key per its own policy (free, no account, no card) and is correctly excluded.

---

## 1. Routing adapter — verified contract

Base URL (prefer FOSSGIS; it gives you three profiles on one host):

```
https://routing.openstreetmap.de/{routed-car|routed-bike|routed-foot}/route/v1/driving/{lon},{lat};{lon},{lat}
```

The **path profile segment is genuinely ignored and fails silently**. I verified on `router.project-osrm.org` that `/route/v1/driving/`, `/bike/`, `/foot/`, `/cycling/`, `/walking/` and even `/nonsense/` all return the identical car result (204881.5 m). Never select mode via the path — always via the FOSSGIS host prefix. An unknown host prefix (`routed-scooter`) returns **HTTP 404 with an HTML body from nginx**, so your error path must tolerate non-JSON.

Query string (verified against `docs/http.md` and live):
`overview=full&geometries=geojson&steps=true&annotations=distance,duration,speed`

### Verified field names (exact, from the live 242736-byte response)

- top level: `code`, `routes`, `waypoints`
- `routes[0]`: `legs`, `weight_name`, `geometry`, `weight`, `duration`, `distance`
- `routes[0].legs[0]`: `distance`, `annotation`, `duration`, `summary`, `weight`, `steps`
- `annotation`: `distance`, `duration`, `speed` (+ `weight`, `datasources`, `nodes`, `metadata` when `annotations=true`)
- `waypoints[i]`: `hint`, `location`, `name`, `distance`
- `steps[i]` observed union on this route: `distance`, `driving_side`, `duration`, `geometry`, `intersections`, `maneuver`, `mode`, `name`, `ref`, `weight` (`destinations` appears on some). `pronunciation`, `exits`, `rotary_name`, `rotary_pronunciation` are spec-optional and absent here — treat all as `.get()`.
- `intersections[i]`: `out`, `entry`, `bearings`, `location`, plus `in` on non-first.

### Parsing rules that are load-bearing

```python
COORDS = "{lon1},{lat1};{lon2},{lat2}"  # LONGITUDE FIRST, both here and in geometry

n = len(route["geometry"]["coordinates"])  # 3089 on the reference route
ann = route["legs"][0]["annotation"]
assert len(ann["distance"]) == n - 1  # 3088 — segment i is coord i -> i+1
# BUT, only with annotations=true:
# len(ann["nodes"]) == n, NOT n-1.  Verified 131 vs 130.

speed_kmh = [s * 3.6 for s in ann["speed"]]  # raw values are m/s
```

Prefer the explicit `annotations=distance,duration,speed` over `annotations=true` — `true` adds a `nodes` array of 64-bit OSM ids plus `datasources`/`weight`/`metadata` (`metadata.datasource_names == ["lua profile"]`, verified) for no UI benefit.

### Validation — do NOT trust `code == "Ok"`

```python
def validate(resp, max_snap_m=200.0):
    if resp.get("code") != "Ok":
        raise OsrmError(resp.get("code"), resp.get("message"))
    for wp in resp["waypoints"]:  # THE real guard
        if wp["distance"] > max_snap_m:
            raise OsrmError("SnapTooFar", f"{wp['distance']:.0f} m from road")
    r = resp["routes"][0]
    if r["distance"] <= 0 or r["duration"] <= 0:
        raise OsrmError("EmptyRoute")
    return r
```
Verified failure modes this catches: mid-Atlantic input returns `code:"Ok"`, distance 0, snap distance 1652324.6 m; swapped lat/lon returns `code:"Ok"` with a believable 239771.9 m route. Both pass a naive `code == "Ok"` check.

Real error codes, all **HTTP 400 + JSON** with `code` and `message`: `InvalidOptions` ("Number of coordinates needs to be at least two."), `InvalidQuery` ("Query string malformed close to position 18"), `InvalidService` ("Service routez not found!").

### Do not assume `weight == duration`
Verified per profile on the Frankfurt→Stuttgart pair:

| host prefix | distance (m) | duration (s) | weight_name | weight |
|---|---|---|---|---|
| `routed-car` | 204881.5 | 8148.9 | `routability` | 8148.9 |
| `routed-bike` | 212360.2 | 33278.4 | `duration` | 33278.4 |
| `routed-foot` | 184839.9 | 147645 | `routability` | 17744.35 |

Read `duration` for time. Treat `weight` as opaque unless `weight_name == "duration"`.

### HTTP hygiene
Verified `access-control-allow-origin: *`, `access-control-allow-methods: GET` on the demo server, and **no** `X-RateLimit-*` / `Retry-After` headers — the report is right that you get no warning. Serialise to ≤1 req/s (FOSSGIS policy), set a real UA, cache on rounded coordinates.

```python
HEADERS = {"User-Agent": "AutoTwinDE/1.0 (+https://your-url; contact: you@example.org)"}
```

---

## 2. Tile adapter — the one real trap

**If your Python code ever fetches `tile.openstreetmap.org` directly, you must validate the body, not the status code.**

```python
OSM_BLOCKED_MD5 = "c069a15b2cc2d6b6f527ad09eb93c61a"  # 6987-byte placeholder PNG


def fetch_osm_tile(z, x, y, session):
    # HTTPS only — the policy explicitly prohibits the http:// URL
    r = session.get(
        f"https://tile.openstreetmap.org/{z}/{x}/{y}.png", headers=HEADERS, timeout=20
    )  # never a default UA
    r.raise_for_status()  # will NOT fire when blocked
    if hashlib.md5(r.content).hexdigest() == OSM_BLOCKED_MD5:
        raise TileBlocked("UA rejected by OSMF CDN; response was 200 + placeholder")
    return r.content
```

I reproduced this deterministically: `python-requests/2.32.3` → 200 + 6987 bytes, identical md5 across six wildly different tiles; `AutoTwinDE/1.0 (contact)` → 200 + the real 24960 bytes. The OSMF policy names `python-requests/x.y` in its blocked-defaults list. **No other host in this stack does this** — I checked all of them under both UAs and got byte-identical responses.

Other OSMF requirements to encode: honour `Cache-Control`/`Expires`/`ETag` (≥7 days if you cannot read them), use `If-None-Match`/`If-Modified-Since` on expiry, never send `Cache-Control: no-cache`, no prefetch/bulk download, attribution bottom-right.

### Verified keyless endpoints

```
# OpenFreeMap (recommended) — style URLs, MapLibre-ready
https://tiles.openfreemap.org/styles/{positron|dark|liberty|bright|fiord}
https://tiles.openfreemap.org/planet                     # TileJSON 3.0.0
# VersaTiles (fallback)
https://tiles.versatiles.org/assets/styles/{colorful|eclipse|graybeard|neutrino|shadow}/style.json
https://tiles.versatiles.org/tiles/osm/{z}/{x}/{y}       # no extension, ct: vnd.mapbox-vector-tile
# OSMF vector (no style shipped)
https://vector.openstreetmap.org/shortbread_v1/tilejson.json
https://vector.openstreetmap.org/shortbread_v1/{z}/{x}/{y}.mvt
# Raster
https://tile.openstreetmap.org/{z}/{x}/{y}.png           # UA-gated, see above
https://tile.openstreetmap.de/{z}/{x}/{y}.png            # bare host only, ACAO:*, max-age ~3.1 d
```

`tile-a/-b/-c.openstreetmap.de` and `a/b/c.openstreetmap.de` all fail DNS — report confirmed, no `{s}` template. OSMF policy likewise warns other subdomains "may be slower or withdrawn without notice".

Verified attribution strings (copy verbatim):
- OpenFreeMap TileJSON: `<a href="https://openfreemap.org" target="_blank">OpenFreeMap</a> <a href="https://www.openmaptiles.org/" target="_blank">&copy; OpenMapTiles</a> Data from <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a>`
- OSMF vector TileJSON: `<a href="https://www.openstreetmap.org/copyright">© OpenStreetMap</a>` (name `OpenStreetMap Shortbread`, maxzoom 14, 26 vector_layers)
- VersaTiles source-level: `© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors`

All five OpenFreeMap styles share `sprite: https://tiles.openfreemap.org/sprites/ofm_f384/ofm` and `glyphs: .../fonts/{fontstack}/{range}.pbf` and the single `openmaptiles` vector source → the report's "seamless theme toggle" claim is confirmed. Current planet version string is `20260906_080001_pt`; treat as volatile, always resolve via the style or TileJSON.

One JS-side note the report missed: VersaTiles styles use the **array form** of `sprite` (`[{"id":"basics","url":"..."}]`), which needs MapLibre GL JS v3+.

---

## 3. Fallback chain

```python
TILE_STYLES = [
    (
        "openfreemap",
        {
            "light": "https://tiles.openfreemap.org/styles/positron",
            "dark": "https://tiles.openfreemap.org/styles/dark",
        },
    ),
    (
        "versatiles",
        {
            "light": "https://tiles.versatiles.org/assets/styles/graybeard/style.json",
            "dark": "https://tiles.versatiles.org/assets/styles/eclipse/style.json",
        },
    ),
]
ROUTING_HOSTS = [
    os.environ.get("OSRM_URL"),  # self-hosted, wins if set
    "https://routing.openstreetmap.de/routed-car",
    "https://router.project-osrm.org",  # same FOSSGIS infra, car only
]
```
Probe a style URL with a HEAD/GET and check `json["version"] == 8` and non-empty `sources` before committing. For routing, on connection error or 5xx, fall to the next host; on 400-class `InvalidQuery`/`InvalidOptions`, do **not** retry — it is your bug.

---

## 4. Fixture data

Freeze these so tests never touch the network:

- `fixtures/osrm_route_ffm_stuttgart.json` — the full 242736-byte response. Assert: `code=="Ok"`, `distance==204881.5`, `duration==8148.9`, `weight_name=="routability"`, `len(geometry.coordinates)==3089`, `legs[0].summary=="A 6, A 81"`, `len(legs[0].steps)==41`, `len(annotation.speed)==3088`, `geometry.coordinates[0]==[8.682092, 50.110913]`, `waypoints[0].name=="Braubachstraße"` and `waypoints[0].distance==1.55506113`. First step: `maneuver.type=="depart"`, `bearing_after==249`, `name=="Braubachstraße"`, `distance==190.1`, and **no** `maneuver.modifier` (same for the final `arrive` step — guard for `None` in turn-icon lookup). Steps with `ref`: index 2/3/4 `"K 818"`, 5/6 `"B 44"`, 11 `"A 5"`.
- `fixtures/osrm_error_*.json` — the three 400 bodies above, plus the nginx 404 HTML for an unknown host prefix.
- `fixtures/osrm_ok_but_garbage.json` — the mid-Atlantic `code:"Ok"` / distance 0 / snap 1652324.6 m response. This is the most valuable fixture you can have; it is the one that catches a naive validator.
- `fixtures/osrm_route_bike.json` / `_foot.json` — to pin the `weight_name` variance.
- `fixtures/osm_tile_blocked.png` — the 6987-byte placeholder (md5 `c069a15b2cc2d6b6f527ad09eb93c61a`) so you can unit-test `TileBlocked`.
- `fixtures/openfreemap_planet.tilejson` and `fixtures/osmf_shortbread.tilejson` — assert attribution strings and `maxzoom == 14`.

Do **not** assert exact byte lengths on raster tiles: `tile.openstreetmap.de/12/2138/1376.png` gave me 25041 and 25048 minutes apart (tiles re-render). Geofabrik `Content-Length` drifts daily too — assert "within ±20% of expected", not equality.

If you want a genuinely Frankfurt-centred tile fixture, use **12/2146/1387**, not 12/2138/1376.

---

## 5. Self-hosting — verified

Image name and command sequence confirmed against the live README: `ghcr.io/project-osrm/osrm-backend`, `osrm-extract -p /opt/car.lua`, then `osrm-partition` + `osrm-customize`, serve with `osrm-routed --algorithm mld`. CH alternative is `osrm-contract` + `--algorithm ch`. Repo `profiles/` contains `car.lua`, `bicycle.lua`, `foot.lua` (plus `foot_area.lua`) — so the report's `/opt/bicycle.lua`, `/opt/foot.lua` are the right names. One README detail the report omitted: the `.osrm` suffix can be dropped entirely (`osrm-partition /data/berlin-latest`). The report's RAM/time guidance remains unmeasured — it says so, and I did not measure it either.

### Unconfirmed / open questions

- The exact licence name for Autobahn API data. The OpenAPI spec has no `license`/`termsOfService` field, and https://www.autobahn.de/nutzungsbedingungen is a 404. A search result claimed 'a license with restricted use, available for free' on a Mobilithek offer page, but that page renders client-side and I could not read it to quote the licence verbatim. Whether 'Datenlizenz Deutschland – Namensnennung 2.0' applies is NOT confirmed.
- Detail endpoints for closure, warning, parking_lorry, electric_charging_station and webcam (`/details/{service}/{id}`). They are in the official OpenAPI spec and I verified the pattern works for `/details/roadworks/{identifier}`, but I did not individually fetch the other five.
- Whether the roadworks/closure `identifier` is stable over time. It embeds timestamps (e.g. `...2026-08-10_11-00-00-000...`), which suggests it may change when a construction phase is updated — meaning it may not be a durable primary key across polls. Not tested longitudinally; verify before using it as a DB primary key.
- Whether webcams are permanently removed or temporarily empty. I confirmed 0 across all 109 roads on 2026-09-14, but cannot say whether this is a deliberate retirement or a transient outage.
- Whether Mobilithek exposes any documented, supported, unauthenticated JSON REST API for browsing/downloading offers. I confirmed its frontend uses an internal gateway (`/mdp-api/mdp-msa-metadata/v1/...`) and that at least one endpoint (`vocabs`) answers unauthenticated, but the offer-search route lives in a lazily-loaded JS chunk I did not fully resolve. Either way it is an undocumented private contract.
- The actual licence and exact content of Mobilithek offer 748580849261105152 (surfaced in search as an Autobahn GmbH data offer) — the SPA prevented reading it.
- Whether the Autobahn API enforces a rate limit above the ~4.4 req/s I tested, or applies IP-based throttling over longer windows. I deliberately kept the burst modest (40 requests) rather than probing for the ceiling.
- What `devportal-test.autobahn.de` will become — whether it presages a keyed replacement for the open `verkehr.autobahn.de` endpoint, and on what timeline. It is sign-in gated and lists no APIs publicly.
- The meaning/units of `startLcPosition` (numeric string, e.g. '14', '171'). Likely a linear-referencing or chainage position, but not documented anywhere I found.