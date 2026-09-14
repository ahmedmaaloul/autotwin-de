"""AutoTwin DE — Model Context Protocol server.

Exposes the platform's analysis capabilities as **tools** an MCP client can call: Claude
Desktop, Cursor, Zed, an IDE agent, or anything else that speaks the protocol. The client brings
its own model; this server brings grounded answers.

Why a tool server rather than a chat endpoint with a model inside it (the original brief's
"copilot"):

- **Every number comes from the same code the API serves.** A tool call runs
  ``analyse_route`` or the PostGIS corridor query and returns the result. The model can
  paraphrase it; it cannot invent a kWh figure, because it never computes one.
- **Zero cost, no key in the repository.** The platform runs no model. Whoever connects
  chooses their own client and pays for their own tokens — or runs a local model.
- **It is the smaller, testable surface.** Tool schemas in, tool results out. The insight
  generator (``autotwin_ml.insights``) already produces the deterministic "why" that a chat
  wrapper would only have verbalised.

Run it with ``uv run python -m autotwin_mcp`` (stdio) and point a client at that command; see
``services/mcp/README.md`` for the exact client configuration. It needs the same database the
API uses, so ``make up && make db-upgrade`` first.

Tool results are plain JSON objects. Geometry is **omitted by default**: a 41-segment route
carries ~200 KB of coordinates, which is worth nothing to a language model and costs it context.
Pass ``include_geometry=True`` when a client actually renders maps.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from autotwin_api.routers.data import data_quality as _data_quality
from autotwin_api.schemas.routes import ChargingOptimizeRequest, RouteAnalyzeRequest
from autotwin_api.services.coverage import compute_corridor_coverage
from autotwin_api.services.route_analysis import analyse_route, plan_charging
from autotwin_contracts.vehicles import GENERIC_VEHICLE_PROFILES
from autotwin_core.db.models import ChargingStation, Route
from autotwin_core.db.session import dispose_engine, session_scope
from autotwin_core.db.types import from_wkb_point
from autotwin_core.errors import AutoTwinError
from autotwin_ingestion.providers.registry import get_geocoding_provider, get_routing_provider
from autotwin_mcp import __version__

INSTRUCTIONS = """\
AutoTwin DE is a digital twin of German electric road mobility built on official open data:
the Bundesnetzagentur charging register, DWD weather, Autobahn GmbH roadworks, and OSRM routing
over OpenStreetMap. Vehicle telemetry and the ML model's training labels are SIMULATED and
must always be described as such. Every tool returns real platform output; none of them
estimates anything itself. Distances are kilometres, energy is kWh, consumption is kWh/100 km,
state of charge is percent. Seeded corridors: frankfurt-stuttgart (the reference),
frankfurt-muenchen, stuttgart-muenchen, muenchen-ingolstadt, wolfsburg-berlin.
"""


@asynccontextmanager
async def _lifespan(_server: MCPServer[None]) -> AsyncIterator[None]:
    """Release the database engine when the client disconnects."""
    try:
        yield None
    finally:
        await dispose_engine()


server: MCPServer[None] = MCPServer(
    name="autotwin-de",
    title="AutoTwin DE",
    description="Energy, charging and infrastructure analysis for German electric mobility",
    instructions=INSTRUCTIONS,
    version=__version__,
    lifespan=_lifespan,
)


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _dump(value: Any) -> Any:
    """Turn a Pydantic model or dataclass (nested) into plain JSON-compatible data."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _dump(getattr(value, field.name)) for field in dataclasses.fields(value)
        }
    if isinstance(value, dict):
        return {str(key): _dump(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_dump(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


_GEOMETRY_KEYS = frozenset({"geometry", "geometry_json", "coordinates_geojson"})


def _strip_geometry(value: Any) -> Any:
    """Remove coordinate payloads from a dumped result, recursively."""
    if isinstance(value, dict):
        return {k: _strip_geometry(v) for k, v in value.items() if k not in _GEOMETRY_KEYS}
    if isinstance(value, list):
        return [_strip_geometry(item) for item in value]
    return value


def _finish(value: Any, *, include_geometry: bool) -> Any:
    dumped = _dump(value)
    return dumped if include_geometry else _strip_geometry(dumped)


def _finish_dict(value: Any, *, include_geometry: bool) -> dict[str, Any]:
    """`_finish` for results that are a single model — narrows the type honestly."""
    result = _finish(value, include_geometry=include_geometry)
    if not isinstance(result, dict):
        msg = f"expected a JSON object, got {type(result).__name__}"
        raise TypeError(msg)
    return result


def _fail(error: AutoTwinError) -> ValueError:
    """Surface the platform's typed error as a readable tool failure."""
    return ValueError(f"{error.code}: {error}")


# --------------------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------------------


@server.tool(
    title="List seeded corridors",
    description="The demo corridors that can be analysed by slug, with length and driving time.",
)
async def list_routes() -> list[dict[str, Any]]:
    async with session_scope() as session:
        rows = (
            await session.execute(select(Route).where(Route.is_demo.is_(True)).order_by(Route.name))
        ).scalars()
        return [
            {
                "slug": route.slug,
                "name": route.name,
                "origin": route.origin_name,
                "destination": route.destination_name,
                "distance_km": round(route.distance_m / 1000, 1),
                "duration_min": round(route.duration_s / 60),
            }
            for route in rows
        ]


@server.tool(
    title="Vehicle profiles",
    description=(
        "The generic EV class profiles available for analysis (compact_ev, sedan_ev, "
        "performance_ev, suv_ev, van_ev). Public class-level figures, not manufacturer data."
    ),
)
def vehicle_profiles() -> list[dict[str, Any]]:
    return [_dump(profile) for profile in GENERIC_VEHICLE_PROFILES]


@server.tool(
    title="Analyse a route's energy demand",
    description=(
        "Full journey analysis: distance, driving time, energy required, arrival state of charge, "
        "weather and traffic penalties, per-segment consumption and intensity, the traffic events "
        "on the corridor, and a deterministic explanation of what drove the consumption. Give "
        "either route_slug (a seeded corridor) or origin and destination as German place names. "
        "The energy model is trained on simulated telemetry — say so when reporting figures."
    ),
)
async def analyze_route(
    route_slug: str | None = None,
    origin: str | None = None,
    destination: str | None = None,
    vehicle_code: str = "sedan_ev",
    start_soc_percent: float = 70.0,
    min_arrival_soc_percent: float = 15.0,
    include_geometry: bool = False,
) -> dict[str, Any]:
    request = RouteAnalyzeRequest(
        route_slug=route_slug,
        origin=origin,
        destination=destination,
        vehicle_code=vehicle_code,
        start_soc_percent=start_soc_percent,
        min_arrival_soc_percent=min_arrival_soc_percent,
    )
    try:
        async with session_scope() as session:
            result = await analyse_route(
                session,
                request,
                routing=get_routing_provider(),
                geocoding=get_geocoding_provider(),
            )
    except AutoTwinError as error:
        raise _fail(error) from error
    return _finish_dict(result.response, include_geometry=include_geometry)


@server.tool(
    title="Plan charging stops",
    description=(
        "Runs the route analysis, then a beam search over the fast chargers within the corridor "
        "to find the stop sequence minimising driving + charging + detour time with a range-risk "
        "penalty. feasible=false is an answer with a reason, not an error. Each stop carries the "
        "station, arrival and departure SOC, charge time and the reason it was chosen."
    ),
)
async def plan_charging_stops(
    route_slug: str | None = None,
    origin: str | None = None,
    destination: str | None = None,
    vehicle_code: str = "sedan_ev",
    start_soc_percent: float = 40.0,
    min_arrival_soc_percent: float = 15.0,
    corridor_buffer_km: float = 5.0,
    min_power_kw: float = 50.0,
    include_geometry: bool = False,
) -> dict[str, Any]:
    request = ChargingOptimizeRequest(
        route_slug=route_slug,
        origin=origin,
        destination=destination,
        vehicle_code=vehicle_code,
        start_soc_percent=start_soc_percent,
        min_arrival_soc_percent=min_arrival_soc_percent,
        corridor_buffer_km=corridor_buffer_km,
        min_power_kw=min_power_kw,
    )
    try:
        async with session_scope() as session:
            plan = await plan_charging(
                session,
                request,
                routing=get_routing_provider(),
                geocoding=get_geocoding_provider(),
            )
    except AutoTwinError as error:
        raise _fail(error) from error
    return _finish_dict(plan, include_geometry=include_geometry)


@server.tool(
    title="Corridor charging coverage",
    description=(
        "Fast-charging coverage along a seeded corridor, computed in PostGIS: stations within "
        "buffer_km of the route reaching min_power_kw, projected onto the route, with the largest "
        "and mean gap a driver would experience between charging opportunities — including the "
        "gaps from the origin to the first charger and from the last to the destination. The "
        "methodology sentence states exactly how the numbers were produced."
    ),
)
async def corridor_coverage(
    route_slug: str,
    buffer_km: float = 5.0,
    min_power_kw: float = 150.0,
    include_geometry: bool = False,
) -> dict[str, Any]:
    try:
        async with session_scope() as session:
            coverage = await compute_corridor_coverage(
                session,
                route_slug=route_slug,
                buffer_km=buffer_km,
                min_power_kw=min_power_kw,
                include_geometry=include_geometry,
            )
    except AutoTwinError as error:
        raise _fail(error) from error
    if not coverage:
        raise ValueError(f"not_found: no seeded corridor with slug {route_slug!r}")
    return _finish_dict(coverage[0], include_geometry=include_geometry)


@server.tool(
    title="Underserved corridors",
    description=(
        "Rank every seeded corridor by the largest distance between consecutive chargers of at "
        "least min_power_kw within buffer_km of the route, and flag those whose largest gap "
        "exceeds max_gap_km. All three parameters are exposed because the answer is only "
        "meaningful next to its assumptions."
    ),
)
async def underserved_corridors(
    min_power_kw: float = 150.0,
    max_gap_km: float = 50.0,
    buffer_km: float = 5.0,
) -> dict[str, Any]:
    try:
        async with session_scope() as session:
            coverage = await compute_corridor_coverage(
                session,
                demo_only=True,
                buffer_km=buffer_km,
                min_power_kw=min_power_kw,
                include_geometry=False,
            )
    except AutoTwinError as error:
        raise _fail(error) from error
    ranked = sorted(_dump(coverage), key=lambda c: -float(c.get("max_gap_km") or 0.0))
    for corridor in ranked:
        corridor["underserved"] = float(corridor.get("max_gap_km") or 0.0) > max_gap_km
    return {
        "parameters": {
            "min_power_kw": min_power_kw,
            "max_gap_km": max_gap_km,
            "buffer_km": buffer_km,
        },
        "corridors": _strip_geometry(ranked),
    }


@server.tool(
    title="Search charging stations",
    description=(
        "Search the Bundesnetzagentur charging register (official data, CC BY 4.0). Filter by "
        "free text over operator and city, by federal state code (BW, BY, HE, ...), by minimum "
        "power, or by proximity to a coordinate. Returns at most `limit` sites, nearest first when "
        "a coordinate is given."
    ),
)
async def search_charging_stations(
    query: str | None = None,
    bundesland: str | None = None,
    min_power_kw: float | None = None,
    near_latitude: float | None = None,
    near_longitude: float | None = None,
    radius_km: float = 10.0,
    limit: int = 20,
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 100))
    stmt = select(ChargingStation)
    if query:
        pattern = f"%{query}%"
        stmt = stmt.where(
            ChargingStation.operator.ilike(pattern) | ChargingStation.city.ilike(pattern)
        )
    if bundesland:
        stmt = stmt.where(ChargingStation.bundesland == bundesland.upper())
    if min_power_kw is not None:
        stmt = stmt.where(ChargingStation.max_power_kw >= min_power_kw)
    if near_latitude is not None and near_longitude is not None:
        point = func.ST_SetSRID(func.ST_MakePoint(near_longitude, near_latitude), 4326)
        geography = func.cast(point, ChargingStation.location.type)
        stmt = stmt.where(
            func.ST_DWithin(ChargingStation.location, geography, radius_km * 1000.0)
        ).order_by(func.ST_Distance(ChargingStation.location, geography))
    else:
        stmt = stmt.order_by(ChargingStation.max_power_kw.desc().nulls_last())
    stmt = stmt.limit(limit)

    async with session_scope() as session:
        rows = (await session.execute(stmt)).scalars().all()
    return [_station_summary(row) for row in rows]


def _station_summary(row: ChargingStation) -> dict[str, Any]:
    coordinate = from_wkb_point(row.location)
    return {
        "id": str(row.id),
        "operator": row.operator,
        "address": " ".join(part for part in (row.street, row.house_number) if part),
        "postal_code": row.postal_code,
        "city": row.city,
        "bundesland": row.bundesland.value if row.bundesland else None,
        "latitude": coordinate.latitude,
        "longitude": coordinate.longitude,
        "max_power_kw": row.max_power_kw,
        "charging_points": row.charging_points_count,
        "category": row.charging_category.value,
        "commissioned_on": row.commissioned_on.isoformat() if row.commissioned_on else None,
        "source": "Bundesnetzagentur Ladesäulenregister (CC BY 4.0)",
    }


@server.tool(
    title="Data quality and provenance",
    description=(
        "Per data source: status, last ingestion, rows received/accepted/rejected, the mode the "
        "answer came from (live, cache, fixture), licence and required attribution. Use this to "
        "say how current and how trustworthy the platform's data is right now."
    ),
)
async def data_quality() -> list[dict[str, Any]]:
    async with session_scope() as session:
        rows = await _data_quality(_session_for_router(session))
    return [_dump(row) for row in rows]


def _session_for_router(session: AsyncSession) -> AsyncSession:
    """The router's dependency-annotated parameter is a plain AsyncSession at runtime."""
    return session


def main() -> None:
    """Serve over stdio — the transport every MCP client supports."""
    server.run(transport="stdio")
