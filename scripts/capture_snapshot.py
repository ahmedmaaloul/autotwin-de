"""Capture a static demo snapshot of the AutoTwin DE API for the hosted frontend.

The public demo runs on a static host with no backend. Instead of showing every page in its
"backend unreachable" state, the Next.js app can serve a **dated snapshot** of real API
responses — captured from a locally running platform loaded with the real Bundesnetzagentur,
DWD and Autobahn data — and label every page as exactly that.

This script produces that snapshot. Run it against a local API with demo data loaded:

    uv run python scripts/capture_snapshot.py --api http://localhost:8000

It writes ``apps/web/public/snapshot/`` — a manifest plus one JSON file per captured response.
These are plain static files: the frontend's API client reads them directly when it is built
with ``NEXT_PUBLIC_DEMO_SNAPSHOT=1``, so the demo needs no server of any kind and deploys to
GitHub Pages, Vercel, Cloudflare Pages or any static host unchanged.

Design notes:

- The snapshot is a **mirror, not a fake**. Every file is a real response from the real API.
  The manifest records the capture time and the counts, and the UI banner shows the date.
- Paginated, filterable endpoints (charging stations, traffic events) are captured as complete
  row sets so the static handler can filter and paginate them faithfully in JavaScript, rather
  than freezing the explorer on page one.
- The flagship analysis is captured for every seeded corridor at the route form's default
  parameters, plus one low-SOC variant that exercises the charging optimiser.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "apps" / "web" / "public" / "snapshot"

CORRIDORS = (
    "frankfurt-stuttgart",
    "frankfurt-muenchen",
    "stuttgart-muenchen",
    "muenchen-ingolstadt",
    "wolfsburg-berlin",
)

# (route_slug, vehicle_code, start_soc, min_soc) — the form default for every corridor, and
# one low-charge case on the flagship corridor so the charging-stop cards have real content.
ANALYSES: tuple[tuple[str, str, float, float], ...] = (
    *((slug, "sedan_ev", 70.0, 15.0) for slug in CORRIDORS),
    ("frankfurt-stuttgart", "compact_ev", 40.0, 15.0),
    ("frankfurt-stuttgart", "van_ev", 30.0, 15.0),
)


def _key(method: str, path: str, query: dict[str, Any] | None = None) -> str:
    """A filesystem-safe name for one captured response."""
    parts = [method.lower(), path.strip("/").replace("/", "__")]
    if query:
        parts.append("&".join(f"{k}={query[k]}" for k in sorted(query)))
    name = "_".join(parts)
    return "".join(ch if ch.isalnum() or ch in "-_.=&" else "-" for ch in name)


class Capture:
    def __init__(self, api: str, out: Path) -> None:
        self.api = api.rstrip("/")
        self.out = out
        self.client = httpx.Client(timeout=120.0)
        self.manifest: dict[str, Any] = {
            "captured_at": datetime.now(UTC).isoformat(),
            "api": self.api,
            "entries": {},
            "counts": {},
        }

    # ---------------------------------------------------------------- primitives
    def _save(
        self,
        key: str,
        payload: Any,
        *,
        method: str,
        path: str,
        query: dict[str, Any] | None,
        data_mode: str | None,
    ) -> None:
        target = self.out / f"{key}.json"
        target.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        self.manifest["entries"][key] = {
            "method": method,
            "path": path,
            "query": query or {},
            "data_mode": data_mode,
            "bytes": target.stat().st_size,
        }

    def get(self, path: str, query: dict[str, Any] | None = None, *, root: bool = False) -> Any:
        base = self.api if root else f"{self.api}/api/v1"
        response = self.client.get(f"{base}{path}", params=query)
        # /ready answers 503 while it is honestly "degraded" (Kafka off, say). That is a real,
        # informative response and belongs in the snapshot — only a transport failure aborts.
        if not (root and response.status_code == 503):
            response.raise_for_status()
        payload = response.json()
        self._save(
            _key("get", ("root" + path) if root else path, query),
            payload,
            method="GET",
            path=path,
            query=query,
            data_mode=response.headers.get("x-autotwin-data-mode"),
        )
        print(
            f"  GET  {path}{'?' + '&'.join(f'{k}={v}' for k, v in (query or {}).items()) if query else ''}"
        )
        return payload

    def post(self, path: str, body: dict[str, Any], key_fields: tuple[str, ...]) -> Any:
        response = self.client.post(f"{self.api}/api/v1{path}", json=body)
        response.raise_for_status()
        payload = response.json()
        query = {k: body[k] for k in key_fields}
        self._save(
            _key("post", path, query),
            payload,
            method="POST",
            path=path,
            query=query,
            data_mode=response.headers.get("x-autotwin-data-mode"),
        )
        print(f"  POST {path} {query}")
        return payload

    def paginate_all(
        self, path: str, base_query: dict[str, Any], page_size: int = 500
    ) -> list[Any]:
        """Walk every page of a Page[T] endpoint and return the concatenated items."""
        items: list[Any] = []
        page = 1
        while True:
            response = self.client.get(
                f"{self.api}/api/v1{path}",
                params={**base_query, "page": page, "page_size": page_size},
            )
            response.raise_for_status()
            payload = response.json()
            items.extend(payload["items"])
            if not payload.get("has_next"):
                break
            page += 1
        return items

    # ---------------------------------------------------------------- the capture
    def run(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        for stale in self.out.glob("*.json"):
            stale.unlink()

        print("root")
        self.get("/health", root=True)
        self.get("/ready", root=True)

        print("dashboard")
        summary = self.get("/dashboard/summary")
        self.manifest["counts"]["charging_stations_total"] = summary.get("charging_stations_total")
        self.manifest["counts"]["traffic_events_active"] = summary.get("traffic_events_active")

        print("charging")
        self.get("/charging/stations", {"page": 1, "page_size": 50})
        # One GeoJSON file: the fast-only variant is the same 20 000-feature cap and the client
        # derives it from properties.is_fast_charger, so shipping it twice would be 6 MB for nothing.
        self.get("/charging/stations/geojson")
        self.get("/charging/statistics")
        for slug in CORRIDORS:
            self.get(
                "/charging/coverage", {"route_slug": slug, "buffer_km": 5, "min_power_kw": 150}
            )
        self.get(
            "/charging/underserved",
            {"min_power_kw": 150, "max_gap_km": 50, "corridor_buffer_km": 5},
        )

        # Complete station rows so the explorer can filter and paginate in the static handler.
        print("  walking every charging station page …")
        rows = self.paginate_all("/charging/stations", {})
        # Fast-charging sites only (max_power_kw >= 50). The full register is 116 000+ rows of
        # street addresses — 47 MB of JSON — which is too much to commit and too much for a
        # browser to page through. The headline counts still come from the captured statistics
        # endpoints, so the dashboard shows the real totals; the explorer says what it holds.
        full_count = len(rows)
        rows = [row for row in rows if row.get("is_fast_charger")]
        # Sharded by Bundesland: a browser filtering on one state downloads one shard, and
        # "all states" downloads sixteen in parallel — never one 13 MB file.
        shards: dict[str, list[Any]] = {}
        for row in rows:
            shards.setdefault(row.get("bundesland") or "unknown", []).append(row)
        shard_index: dict[str, int] = {}
        for state, state_rows in sorted(shards.items()):
            target = self.out / f"charging_stations_{state}.json"
            target.write_text(json.dumps(state_rows, ensure_ascii=False, separators=(",", ":")))
            shard_index[state] = len(state_rows)
        self.manifest["entries"]["charging_stations_shards"] = {
            "method": "GET",
            "path": "/charging/stations",
            "query": {"_sharded_by": "bundesland"},
            "data_mode": None,
            "bytes": sum((self.out / f"charging_stations_{s}.json").stat().st_size for s in shards),
        }
        self.manifest["counts"]["charging_stations_rows"] = len(rows)
        self.manifest["counts"]["charging_stations_rows_note"] = (
            f"snapshot explorer holds the {len(rows):,} sites with max_power_kw >= 50; "
            f"the full register of {full_count:,} sites needs a running backend"
        )
        self.manifest["charging_station_shards"] = shard_index
        print(
            f"  {len(rows):,} of {full_count:,} station rows (fast charging) in {len(shards)} shards"
        )

        # Details for the first pages of the default listing, so the drawer works on click.
        print("  station details …")
        for row in rows[:300]:
            self.get(f"/charging/stations/{row['id']}")

        print("traffic")
        self.get("/traffic/events", {"active_only": "true", "page": 1, "page_size": 250})
        self.get("/traffic/events/geojson")
        self.get("/traffic/events/geojson", {"active_only": "true"})
        events = self.paginate_all("/traffic/events", {})
        (self.out / "traffic_events_all.json").write_text(
            json.dumps(events, ensure_ascii=False, separators=(",", ":"))
        )
        self.manifest["entries"]["traffic_events_all"] = {
            "method": "GET",
            "path": "/traffic/events",
            "query": {"_all": True},
            "data_mode": None,
            "bytes": (self.out / "traffic_events_all.json").stat().st_size,
        }
        self.manifest["counts"]["traffic_events_rows"] = len(events)

        print("weather")
        self.get("/weather/latest", {"lat": 50.1109, "lon": 8.6821})
        self.get("/weather", {"page": 1, "page_size": 50})

        print("routes")
        self.get("/routes", {"page_size": 50})
        self.get("/vehicles/profiles")
        for slug, vehicle, start, minimum in ANALYSES:
            body = {
                "route_slug": slug,
                "vehicle_code": vehicle,
                "start_soc_percent": start,
                "min_arrival_soc_percent": minimum,
            }
            fields = ("route_slug", "vehicle_code", "start_soc_percent", "min_arrival_soc_percent")
            self.post("/routes/analyze", body, fields)
            self.post("/routes/optimize-charging", body, fields)

        print("vehicles")
        live = self.get("/vehicles/live")
        self.get("/vehicles", {"page": 1, "page_size": 50})
        for vehicle in live:
            vid = vehicle["vehicle_id"]
            self.get(f"/vehicles/{vid}")
            self.get(f"/vehicles/{vid}/telemetry", {"limit": 120})
        self.manifest["counts"]["vehicles_live"] = len(live)

        print("trips")
        trips = self.get("/trips", {"page": 1, "page_size": 50})
        for trip in trips.get("items", [])[:25]:
            self.get(f"/trips/{trip['trip_id']}")

        print("simulation")
        self.get("/simulations/status")
        self.get("/simulations", {"page_size": 20})

        print("ml")
        self.get("/ml/models")
        self.get("/ml/metrics")
        self.get("/ml/explain/global")

        print("data")
        self.get("/data/quality")
        self.get("/data/ingestions", {"page": 1, "page_size": 50})
        self.get("/data/sources")

        print("analytics")
        for dimension in ("temperature", "speed", "traffic"):
            self.get("/analytics/energy", {"dimension": dimension})
        self.get("/analytics/regions")

        (self.out / "manifest.json").write_text(
            json.dumps(self.manifest, indent=2, ensure_ascii=False)
        )
        total = sum(entry["bytes"] for entry in self.manifest["entries"].values())
        print(f"\n{len(self.manifest['entries'])} responses, {total / 1e6:.1f} MB -> {self.out}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    try:
        Capture(args.api, args.out).run()
    except httpx.HTTPError as error:
        print(f"capture failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
