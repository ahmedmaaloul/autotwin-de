"""``/api/v1/vehicles`` — the simulated fleet, its live positions and its raw telemetry.

Route order is load-bearing. ``/profiles`` and ``/live`` are declared **before**
``/{vehicle_id}`` because Starlette matches in declaration order: with the parameterised route
first, a request for ``/vehicles/live`` would be answered by the detail handler looking for a
vehicle called "live".

The live query is exported as :func:`fetch_live_vehicles` because ``/api/v1/stream/telemetry``
serves exactly the same rows over SSE (BUILD_SPEC §8). One implementation means the snapshot the
map loads with and the deltas it then merges cannot drift apart — which they would the first
time a column was added to only one of them.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any, Final

import sqlalchemy as sa
from fastapi import APIRouter, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from autotwin_api.deps import BoundingBoxQuery, DbSession, Pagination
from autotwin_api.pagination import paginate_rows
from autotwin_api.schemas.common import ProvenanceOut
from autotwin_api.schemas.vehicles import (
    TelemetryPoint,
    VehicleDetail,
    VehicleLive,
    VehicleSummary,
)
from autotwin_contracts import BoundingBox, Page, VehicleProfile, VehicleState
from autotwin_core.db.models import Telemetry, Vehicle, VehicleModel
from autotwin_core.errors import NotFoundError

__all__ = ["fetch_live_vehicles", "router"]

router = APIRouter()

MAX_LIVE_VEHICLES: Final[int] = 1000
"""Hard cap on ``/vehicles/live`` (BUILD_SPEC §7).

The fleet is configurable and a careless run could ask for tens of thousands of vehicles; the
map cannot draw them and the browser cannot parse them. The cap is a property of the endpoint
rather than a query parameter so that no client can remove it.
"""

_DEFAULT_TELEMETRY_LIMIT: Final[int] = 200
_MAX_TELEMETRY_LIMIT: Final[int] = 5000

_LIVE_SQL: Final[str] = """
SELECT DISTINCT ON (t.vehicle_id)
       t.vehicle_id,
       t.trip_id,
       t.route_id,
       t.recorded_at,
       ST_Y(t.location::geometry) AS latitude,
       ST_X(t.location::geometry) AS longitude,
       t.heading_deg,
       t.speed_kmh,
       t.battery_soc_percent,
       t.battery_temperature_c,
       t.outside_temperature_c,
       t.instantaneous_power_kw,
       t.energy_consumption_kwh_100km,
       t.estimated_range_km,
       t.road_class,
       t.state,
       coalesce(vm.code, 'unknown') AS model_code
  FROM telemetry t
  LEFT JOIN vehicles v ON v.vehicle_id = t.vehicle_id
  LEFT JOIN vehicle_models vm ON vm.id = v.vehicle_model_id
 WHERE {conditions}
 ORDER BY t.vehicle_id, t.recorded_at DESC
 LIMIT :limit
"""
"""Latest sample per vehicle in one pass.

``DISTINCT ON`` walks the ``(vehicle_id, recorded_at DESC)`` index once and keeps the first row
of each vehicle. The obvious alternative — a correlated subquery selecting ``max(recorded_at)``
per vehicle — issues one index lookup per vehicle *and* cannot be combined with the bounding-box
filter without repeating it, so it degrades exactly where a live map needs it not to.

``{conditions}`` is filled from a fixed set of literal fragments below; every value they compare
against is a bound parameter.
"""


async def fetch_live_vehicles(
    session: AsyncSession,
    *,
    since: datetime | None = None,
    bbox: BoundingBox | None = None,
    vehicle_ids: Sequence[str] | None = None,
    limit: int = MAX_LIVE_VEHICLES,
) -> list[VehicleLive]:
    """Return the newest telemetry sample of each vehicle that matches the filters.

    Args:
        session: Session to read on.
        since: Cursor for the SSE stream — only vehicles whose newest sample is **strictly**
            newer than this are returned, which is what makes the stream a delta feed rather
            than a full retransmission every second.
        bbox: Restrict to vehicles inside a geographic box.
        vehicle_ids: Restrict to specific fleet identifiers.
        limit: Row cap, clamped to :data:`MAX_LIVE_VEHICLES`.

    Returns:
        Newest-sample rows, ordered by fleet identifier so a client sees a stable order.
    """
    conditions: list[str] = ["TRUE"]
    params: dict[str, Any] = {"limit": min(max(limit, 1), MAX_LIVE_VEHICLES)}

    if since is not None:
        conditions.append("t.recorded_at > :since")
        params["since"] = since
    if bbox is not None:
        conditions.append(
            "ST_Intersects(t.location, "
            "ST_MakeEnvelope(:west, :south, :east, :north, 4326)::geography)"
        )
        params |= {
            "west": bbox.west,
            "south": bbox.south,
            "east": bbox.east,
            "north": bbox.north,
        }
    if vehicle_ids:
        conditions.append("t.vehicle_id = ANY(:vehicle_ids)")
        params["vehicle_ids"] = list(vehicle_ids)

    statement = sa.text(_LIVE_SQL.format(conditions=" AND ".join(conditions)))
    rows = (await session.execute(statement, params)).mappings().all()
    return [VehicleLive.model_validate(dict(row)) for row in rows]


def _summary_statement() -> sa.Select[Any]:
    """Vehicles joined to their profile and to their newest sample.

    The newest sample comes from a ``DISTINCT ON`` subquery joined once, not from a correlated
    lookup per row: the list is paged, but the join is what keeps it one query whatever the page
    size.
    """
    latest = (
        sa.select(
            Telemetry.vehicle_id.label("vehicle_id"),
            Telemetry.recorded_at.label("recorded_at"),
            Telemetry.battery_soc_percent.label("battery_soc_percent"),
            Telemetry.odometer_m.label("odometer_m"),
        )
        .distinct(Telemetry.vehicle_id)
        .order_by(Telemetry.vehicle_id, Telemetry.recorded_at.desc())
        .subquery("latest")
    )
    return (
        sa.select(
            Vehicle.id,
            Vehicle.vehicle_id,
            Vehicle.state,
            Vehicle.simulation_run_id,
            VehicleModel.code.label("model_code"),
            VehicleModel.display_name.label("model_display_name"),
            VehicleModel.vehicle_class,
            latest.c.recorded_at.label("last_seen_at"),
            latest.c.battery_soc_percent,
            latest.c.odometer_m,
        )
        .join(VehicleModel, VehicleModel.id == Vehicle.vehicle_model_id)
        .join(latest, latest.c.vehicle_id == Vehicle.vehicle_id, isouter=True)
        .order_by(latest.c.recorded_at.desc().nulls_last(), Vehicle.vehicle_id)
    )


@router.get(
    "",
    response_model=Page[VehicleSummary],
    summary="List simulated vehicles",
    description=(
        "The fleet, paged, newest telemetry first. Every row is **simulated** "
        "(`data_origin = simulated`); AutoTwin holds no real vehicle data.\n\n"
        "Filter with `?state=driving` to see only what is on the road."
    ),
)
async def list_vehicles(
    session: DbSession,
    page: Pagination,
    state: Annotated[
        VehicleState | None,
        Query(description="Only vehicles in this state.", examples=["driving"]),
    ] = None,
) -> Page[VehicleSummary]:
    """Page the fleet, ordered by how recently each vehicle reported."""
    statement = _summary_statement()
    if state is not None:
        statement = statement.where(Vehicle.state == state)
    return await paginate_rows(
        session,
        statement,
        page,
        lambda row: VehicleSummary.model_validate(dict(row._mapping)),
    )


@router.get(
    "/profiles",
    response_model=list[VehicleProfile],
    summary="Vehicle profiles",
    description=(
        "The generic EV classes the platform simulates and analyses (BUILD_SPEC §9), read from "
        "`vehicle_models`. These are **class-level public-domain figures** — a 'compact EV' is "
        "any 58 kWh hatchback — and deliberately not reverse-engineered OEM data. The route "
        "form's vehicle picker is built from this list."
    ),
)
async def list_vehicle_profiles(session: DbSession) -> list[VehicleProfile]:
    """Return the seeded profiles, lightest battery first.

    Served from the database rather than from
    :data:`~autotwin_contracts.vehicles.GENERIC_VEHICLE_PROFILES` so that the ``id`` the caller
    receives is the one the rest of the API accepts, and so an operator who adds a profile sees
    it here without a redeploy.
    """
    rows = (
        (await session.execute(sa.select(VehicleModel).order_by(VehicleModel.battery_capacity_kwh)))
        .scalars()
        .all()
    )
    return [VehicleProfile.model_validate(row, from_attributes=True) for row in rows]


@router.get(
    "/live",
    response_model=list[VehicleLive],
    summary="Live fleet positions",
    description=(
        "The newest telemetry sample of every vehicle, at most "
        f"{MAX_LIVE_VEHICLES} rows — the initial state of the live map.\n\n"
        "Poll this once and then subscribe to `/api/v1/stream/telemetry`, which pushes exactly "
        "these rows as deltas. Filter with `?bbox=west,south,east,north` to load only the "
        "vehicles in view."
    ),
)
async def live_vehicles(
    session: DbSession,
    bbox: BoundingBoxQuery,
    vehicle_ids: Annotated[
        str | None,
        Query(
            description="Comma-separated fleet identifiers, e.g. `ATW-0001,ATW-0002`.",
            examples=["ATW-0001,ATW-0002"],
        ),
    ] = None,
) -> list[VehicleLive]:
    """Answer with one row per vehicle, newest sample only."""
    identifiers = _split_identifiers(vehicle_ids)
    return await fetch_live_vehicles(session, bbox=bbox, vehicle_ids=identifiers)


def _split_identifiers(raw: str | None) -> list[str] | None:
    """Parse a comma-separated ``vehicle_ids`` filter into a list, or ``None`` when absent."""
    if raw is None:
        return None
    values = [item.strip() for item in raw.split(",") if item.strip()]
    return values or None


VehicleIdPath = Annotated[
    str,
    Path(
        description="Fleet identifier of the vehicle, e.g. `ATW-0042`.",
        examples=["ATW-0042"],
        min_length=1,
        max_length=64,
    ),
]


@router.get(
    "/{vehicle_id}",
    response_model=VehicleDetail,
    summary="One vehicle",
    description=(
        "A vehicle with the physical parameters of its profile, its trip totals, its newest "
        "telemetry sample and its provenance block."
    ),
)
async def get_vehicle(session: DbSession, vehicle_id: VehicleIdPath) -> VehicleDetail:
    """Read one vehicle, its profile, its trip aggregates and its latest sample.

    Built on the same statement as the list endpoint, with the profile's physical parameters and
    the provenance block added — so the summary fields of a detail response are produced by one
    query rather than by a second, subtly different one.

    Raises:
        NotFoundError: No vehicle carries this fleet identifier — ``404 not_found``.
    """
    statement = (
        _summary_statement()
        .add_columns(
            VehicleModel.battery_capacity_kwh,
            VehicleModel.usable_capacity_kwh,
            VehicleModel.nominal_consumption_kwh_100km,
            VehicleModel.max_dc_power_kw,
            VehicleModel.max_ac_power_kw,
            VehicleModel.mass_kg,
            Vehicle.source,
            Vehicle.source_identifier,
            Vehicle.source_url,
            Vehicle.source_timestamp,
            Vehicle.data_origin,
            Vehicle.ingestion_run_id,
            Vehicle.ingested_at,
        )
        .where(Vehicle.vehicle_id == vehicle_id)
    )
    row = (await session.execute(statement)).mappings().first()
    if row is None:
        msg = f"no vehicle with identifier {vehicle_id!r}"
        raise NotFoundError(msg, details={"vehicle_id": vehicle_id})

    payload = dict(row)
    totals = (
        (
            await session.execute(
                sa.text(
                    """
                    SELECT count(*) AS trips_total,
                           coalesce(sum(distance_m), 0.0) AS distance_total_m,
                           coalesce(sum(energy_kwh), 0.0) AS energy_total_kwh
                      FROM trips
                     WHERE vehicle_id = :vehicle_row_id
                    """
                ),
                {"vehicle_row_id": payload["id"]},
            )
        )
        .mappings()
        .one()
    )
    latest = await fetch_live_vehicles(session, vehicle_ids=[vehicle_id], limit=1)

    return VehicleDetail.model_validate(
        {
            **payload,
            "trips_total": int(totals["trips_total"]),
            "distance_total_m": float(totals["distance_total_m"]),
            "energy_total_kwh": float(totals["energy_total_kwh"]),
            "latest": latest[0] if latest else None,
            "provenance": ProvenanceOut.model_validate(payload),
        }
    )


@router.get(
    "/{vehicle_id}/telemetry",
    response_model=list[TelemetryPoint],
    summary="Telemetry history of one vehicle",
    description=(
        "Raw samples for one vehicle, **oldest first**, so the series can be plotted directly.\n\n"
        "Without `?since` the most recent `limit` samples are returned; with it, the samples "
        "recorded after that moment. All of it is simulated (BUILD_SPEC §0.2)."
    ),
)
async def vehicle_telemetry(
    session: DbSession,
    vehicle_id: VehicleIdPath,
    since: Annotated[
        datetime | None,
        Query(description="Only samples recorded after this instant (ISO-8601, UTC)."),
    ] = None,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=_MAX_TELEMETRY_LIMIT,
            description=f"Maximum samples to return (1-{_MAX_TELEMETRY_LIMIT}).",
        ),
    ] = _DEFAULT_TELEMETRY_LIMIT,
) -> list[TelemetryPoint]:
    """Return a window of the vehicle's history in chronological order.

    The window is taken from the *newest* end and then reversed, because "the last 200 samples"
    is what a live chart wants; selecting the oldest 200 would show a vehicle's first minutes
    for ever.
    """
    newest: sa.Select[Any] = (
        sa.select(
            Telemetry.recorded_at,
            sa.literal_column("ST_Y(location::geometry)").label("latitude"),
            sa.literal_column("ST_X(location::geometry)").label("longitude"),
            Telemetry.speed_kmh,
            Telemetry.battery_soc_percent,
            Telemetry.energy_consumption_kwh_100km,
            Telemetry.odometer_m,
        )
        .where(Telemetry.vehicle_id == vehicle_id)
        .order_by(Telemetry.recorded_at.desc())
        .limit(limit)
    )
    if since is not None:
        newest = newest.where(Telemetry.recorded_at > since)

    rows = (await session.execute(newest)).mappings().all()
    points = [TelemetryPoint.model_validate(dict(row)) for row in rows]
    points.reverse()
    return points
