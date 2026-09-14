# Bundled provider fixtures

The last rung of the mandatory `live → cache → fixture` chain (BUILD_SPEC §4). Every file here
is a **real response** captured from the German source it is named after. They are trimmed in
*row count* only — never in shape, encoding, column order or quoting — so the parser exercised
offline is byte-for-byte the parser that runs against production.

All files were captured on **2026-09-14** from a machine in Europe. Nothing in this directory
is generated, hand-edited or synthesised.

Overriding a fixture without rebuilding the wheel: drop a file with the same name into
`$AUTOTWIN_DATA_DIR/fixtures/`. `autotwin_ingestion.fixtures.resolve_fixture` prefers it.

---

## `bnetza_ladesaeulenregister_sample.csv` — 96 460 B

| | |
|---|---|
| Source | `https://data.bundesnetzagentur.de/Bundesnetzagentur/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/Ladesaeulenregister_BNetzA_2026-09-01.csv` |
| Found via | the landing page `https://www.bundesnetzagentur.de/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/Ladesaeulenkarte/start.html` — the filename rotates monthly and the previous month is deleted |
| Fetched | 2026-09-14 |
| Original | 55 559 083 B, 116 443 data rows, sha256 `cdd8e38f1fa7e8512bd97caa8cf77e9f875436be1f8fe3fe1f2bd9789a504c5b` |
| Licence | CC BY 4.0 — © Bundesnetzagentur für Elektrizität, Gas, Telekommunikation, Post und Eisenbahnen |

**Trimmed:** 116 443 data rows → **200**, kept in their original file order. Nothing else was
altered: the UTF-8 BOM, the CRLF line endings, the **10-line preamble** (including
`Letzte Aktualisierung vom: 01.09.2026`, which the adapter reads as the edition date) and the
verbatim 47-column header are all intact, and so is the original quoting — 14 of the 200 rows
contain a newline *inside* a quoted cell, which is exactly the shape that defeats a
line-by-line reader.

**Rows were chosen to cover what the parser has to get right**, not at random:

- all **16 Bundesländer**, and both values of `Art der Ladeeinrichtung`
  (148 Normalladeeinrichtung / 52 Schnellladeeinrichtung);
- both `Status` values (196 `In Betrieb`, 4 `In Wartung`);
- multi-valued `Steckertypen1` / `Nennleistung Stecker1` cells (`"AC Typ 2 Steckdose; AC Schuko"`
  paired with `"22; 22"`);
- quoted cells containing the `;` delimiter (`"RFID-Karte;Onlinezahlungsverfahren"`);
- decimal-comma power values, four- and six-group connector layouts, HPC sites ≥ 300 kW,
  CHAdeMO and Schuko connectors, rows with an `Adresszusatz`;
- the sites nearest Frankfurt am Main, Stuttgart and München, so the demo corridors have real
  charging infrastructure along them offline.

The full register has **no** row with a blank `Ladeeinrichtungs-ID` and none without a
`Hausnummer`, so the fixture contains neither; the adapter's synthesised-id path is therefore
covered by unit tests rather than by this file.

---

## DWD — Deutscher Wetterdienst

Licence **CC BY 4.0**, attribution `Quelle: Deutscher Wetterdienst` (the adapter attaches it to
every result). Server `https://opendata.dwd.de/`, path prefix
`climate_environment/CDC/observations_germany/climate/10_minutes/`.

### `dwd_zehn_now_tu_Beschreibung_Stationen.txt` — 69 191 B

| | |
|---|---|
| Source | `…/air_temperature/now/zehn_now_tu_Beschreibung_Stationen.txt` |
| Fetched | 2026-09-14 |
| Original | 467 157 B, 466 stations |
| Encoding | **ISO-8859-1**, preserved |

**Trimmed:** *no row removed* — all 466 stations are present, including
`Seebach (Nationalpark Schwarzwald)`, the name that breaks a whitespace-split parser. The only
change is that the **trailing space padding** was removed from each line: the source pads every
data row to 1 000 characters, which is 85 % of the file and is pure filler. Leading padding,
column offsets, the header and the dash ruler on line 2 are untouched, so the fixed-width
parse path is exercised exactly as in production.

### `dwd_produkt_zehn_now_{tu,ff,rr}_{01424,04928}.txt` — 4–6 KB each

| | |
|---|---|
| Source | the single `produkt_*.txt` inside `…/{air_temperature,wind,precipitation}/now/10minutenwerte_{TU,wind,nieder}_{01424,04928}_now.zip` |
| Fetched | 2026-09-14 |
| Trimmed | **nothing** — each file is the complete extracted member, byte for byte |

Five files, not six: station **01424 Frankfurt/Main-Westend has no wind product upstream** and
its URL answers `404`. That gap is a property of the station, and the fixture set reproduces it
rather than papering over it — the adapter must return a record with a temperature and a null
wind speed.

The two stations are the ones nearest the demo corridor endpoints (01424 Frankfurt/Main-Westend,
2.0 km from the Frankfurt origin; 04928 Stuttgart-Schnarrenberg, 5.9 km from the Stuttgart
destination), so a Frankfurt → Stuttgart analysis still has real measured temperatures behind it
with the network unplugged. The files keep their space padding, their `-999` missing-value
sentinels, their `eor` end-of-row column and their ISO-8859-1 encoding.

---

## Autobahn GmbH des Bundes

| | |
|---|---|
| Source | `https://verkehr.autobahn.de/o/autobahn/` |
| Fetched | 2026-09-14 |
| Licence | **not published.** Treated as *available for development, licence unconfirmed*: cached locally, never redistributed, low request rate. See `DATA_LICENSES.md`. |

### `autobahn_roads.json` — 701 B
`GET /o/autobahn/` verbatim: 109 entries, 108 unique. The trailing whitespace of `"A60 "` is
preserved, because stripping it is the adapter's job and the fixture exists to prove it does.

### `autobahn_A5_roadworks.json` — 32 546 B
`GET /o/autobahn/A5/services/roadworks`. **Trimmed:** 127 items → **12**, chosen to cover all
four combinations of `display_type` (`ROADWORKS`, `SHORT_TERM_ROADWORKS`) × blocked
(`"CLOSED" in impact.symbols`), and re-emitted in their original order. Each kept item is
complete, including its full GeoJSON `LineString`.

The two `display_type` values matter: `SHORT_TERM_ROADWORKS` items carry **no
`startTimestamp`** and no `Beginn:`/`Ende:` line, only German prose of the form
`15.09.26 19:30 bis zum 16.09.26 05:00 Uhr.` — the shape `extract_validity` exists for.

### `autobahn_A5_closure.json` — 16 679 B
`GET /o/autobahn/A5/services/closure`. **Trimmed:** 10 items → **6**, all `CLOSURE_ENTRY_EXIT`
with `CLOSED` in their symbols (that is what the live A5 held). Includes the recurring
night-work prose `Jeden Montag, … zwischen dem 14.09.26 und dem 19.09.26 von 20:00 bis 00:00 Uhr.`

### `autobahn_A5_warning.json` — 10 169 B
`GET /o/autobahn/A5/services/warning`. **Trimmed:** nothing — all 3 live items. Two carry an
INRIX congestion report with `abnormalTrafficType`, `averageSpeed` and `delayTimeValue`, and
**none of them has an `impact` object at all**, which is why the adapter must treat a missing
`impact` as normal rather than as malformed.

This file also pins the time-zone question: one item carries
`startTimestamp: "2026-09-14T10:14:00Z"` *and* the description line
`Beginn: 14.09.26 um 12:14 Uhr`. Same instant, two hours apart — which is what proves the
German prose is `Europe/Berlin` and not UTC.

---

## `osrm_frankfurt_stuttgart.json` — 242 736 B

| | |
|---|---|
| Source | `https://router.project-osrm.org/route/v1/driving/8.6821,50.1109;9.1829,48.7758?overview=full&geometries=geojson&steps=true&annotations=distance,duration,speed` |
| Fetched | 2026-09-14 |
| Licence | ODbL 1.0 — © OpenStreetMap contributors; routing by OSRM (public demo server) |

**Trimmed: nothing.** The response was 242 736 B, comfortably under the ~600 KB budget, so it is
committed exactly as the server sent it — full 3 089-vertex geometry, all 41 steps with their
own geometries and intersections, and the complete `distance` / `duration` / `speed` annotation
arrays (3 088 entries each). Route: 204.9 km, 8 148.9 s, `weight_name: "routability"`.

---

## `nominatim_places.json` — 4 258 B

| | |
|---|---|
| Source | `https://nominatim.openstreetmap.org/search?q=<query>&format=jsonv2&countrycodes=de&limit=5&addressdetails=1` |
| Fetched | 2026-09-14, one request per second, with the project User-Agent |
| Licence | ODbL 1.0 — © OpenStreetMap contributors |

Six live responses, one per demo city, stored as a mapping from the **normalised query**
(`" ".join(query.split()).casefold()`, umlauts intact) to the raw `jsonv2` array exactly as
Nominatim returned it:

`frankfurt am main` · `stuttgart` · `münchen` · `ingolstadt` · `wolfsburg` · `berlin`

**Trimmed: nothing.** `limit=5` was requested; Nominatim returned one administrative result per
city, and all of them are kept with their `address`, `boundingbox` and `osm_type`/`osm_id`.
Wrapping six separate responses in one file is the only structural change — the array under
each key is untouched, so `parse_places` reads production bytes.

---

## Refreshing

These are snapshots, and German open data moves. To re-capture, fetch the URLs above and keep
the properties this file documents — encodings, preambles, padding, key order. A fixture that
has been "cleaned up" is worse than no fixture at all, because it makes a broken parser pass.
