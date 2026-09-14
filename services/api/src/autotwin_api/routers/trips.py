"""``/api/v1/trips`` — completed and running trips, with the series that explain them.

A trip row carries the aggregates (distance, energy, mean consumption); the *shape* of the trip
— where the SOC fell away and why — lives in the telemetry. ``GET /trips/{trip_id}`` therefore
joins the two and returns the series against **distance**, not against time: a chart plotted on
a clock compresses the interesting part of a stop-start hour into a sliver, while the same data
against kilometres shows the motorway stretch for what it is.

The response models are in :mod:`autotwin_api.schemas.vehicles`, because a trip belongs to a
vehicle and both endpoints read the same three tables.
"""

from __future__ import annotations

from typing import Annotated, Any, Final

import sqlalchemy as sa
from fastapi import APIRouter, Path, Query

from autotwin_api.deps import DbSession, Pagination
from autotwin_api.pagination import paginate_rows
from autotwin_api.schemas.common import ProvenanceOut
from autotwin_api.schemas.vehicles import TripDetail, TripSeriesPoint, TripSummary
from autotwin_contracts import Page
from autotwin_core.db.models import Route, Trip, Vehicle, VehicleModel
from autotwin_core.errors import NotFoundError

__all__ = ["router"]

router = APIRouter()

_MAX_SERIES_POINTS: Final[int] = 2000
"""Cap on the points returned with a trip.

A long trip at one sample per simulated second is tens of thousands of rows; past a couple of
thousand a line chart gains no resolution a screen can show, and the payload starts to cost more
than the insight. ``samples`` on the response reports the true count, so the cap is visible
rather than silently lossy.
"""


def _summary_statement() -> sa.Select[Any]:
    """Trips joined to the human vehicle identifier and the corridor they ran on.

    ``trips.vehicle_id`` is a surrogate key, but every other endpoint — and the live map the
    user clicked through from — speaks in fleet identifiers like ``ATW-0042``. The join happens
    here so that the wire contract never exposes the surrogate.
    """
    return (
        sa.select(
            Trip.trip_id,
            Vehicle.vehicle_id.label("vehicle_id"),
            Trip.route_id,
            Route.slug.label("route_slug"),
            Trip.started_at,
            Trip.ended_at,
            Trip.start_soc_percent,
            Trip.end_soc_percent,
            Trip.distance_m,
            Trip.energy_kwh,
            Trip.avg_consumption_kwh_100km,
            Trip.state,
        )
        .join(Vehicle, Vehicle.id == Trip.vehicle_id)
        .join(Route, Route.id == Trip.route_id, isouter=True)
    )


@router.get(
    "",
    response_model=Page[TripSummary],
    summary="List trips",
    description=(
        "Trips of the simulated fleet, most recent departure first.\n\n"
        "`?vehicle_id=ATW-0042` filters by **fleet identifier** — the same string the live map "
        "and the telemetry endpoints use — not by the surrogate key of the `vehicles` row."
    ),
)
async def list_trips(
    session: DbSession,
    page: Pagination,
    vehicle_id: Annotated[
        str | None,
        Query(description="Fleet identifier, e.g. `ATW-0042`.", examples=["ATW-0042"]),
    ] = None,
) -> Page[TripSummary]:
    """Page the trip history, newest first."""
    statement = _summary_statement().order_by(Trip.started_at.desc(), Trip.trip_id)
    if vehicle_id is not None:
        statement = statement.where(Vehicle.vehicle_id == vehicle_id)
    return await paginate_rows(
        session,
        statement,
        page,
        lambda row: TripSummary.model_validate(dict(row._mapping)),
    )


@router.get(
    "/{trip_id}",
    response_model=TripDetail,
    summary="One trip with its SOC and speed series",
    description=(
        "A trip together with its telemetry, resampled onto distance: `offset_km` is the "
        "distance from departure, so SOC and speed can be read against the route rather than "
        "against the clock.\n\n"
        f"At most {_MAX_SERIES_POINTS} points are returned; `samples` reports how many the trip "
        "actually has."
    ),
)
async def get_trip(
    session: DbSession,
    trip_id: Annotated[
        str,
        Path(
            description="Trip identifier produced by the simulator.",
            min_length=1,
            max_length=128,
        ),
    ],
) -> TripDetail:
    """Read one trip, its vehicle profile and an evenly thinned slice of its telemetry.

    Raises:
        NotFoundError: No trip carries this identifier — ``404 not_found``.
    """
    statement = (
        _summary_statement()
        .add_columns(
            Trip.id.label("row_id"),
            Route.name.label("route_name"),
            VehicleModel.code.label("model_code"),
            Trip.source,
            Trip.source_identifier,
            Trip.source_url,
            Trip.source_timestamp,
            Trip.data_origin,
            Trip.ingestion_run_id,
            Trip.ingested_at,
        )
        .join(VehicleModel, VehicleModel.id == Vehicle.vehicle_model_id, isouter=True)
        .where(Trip.trip_id == trip_id)
    )
    row = (await session.execute(statement)).mappings().first()
    if row is None:
        msg = f"no trip with identifier {trip_id!r}"
        raise NotFoundError(msg, details={"trip_id": trip_id})

    series, samples = await _trip_series(session, trip_id)
    payload = dict(row)
    return TripDetail.model_validate(
        {
            **payload,
            "samples": samples,
            "series": series,
            "provenance": ProvenanceOut.model_validate(payload),
        }
    )


async def _trip_series(
    session: DbSession,
    trip_id: str,
) -> tuple[list[TripSeriesPoint], int]:
    """Return the trip's series against distance, thinned to the cap, and the true sample count.

    The thinning is done in PostgreSQL with ``row_number() % stride`` rather than by fetching
    everything and slicing in Python: a long trip is tens of thousands of rows, and the point of
    the cap is not to transfer them. The stride is derived from the count in the same statement,
    so the result is evenly spaced across the whole trip instead of being the first N samples.
    """
    statement = sa.text(
        """
        WITH ordered AS (
            SELECT recorded_at,
                   odometer_m,
                   speed_kmh,
                   battery_soc_percent,
                   energy_consumption_kwh_100km,
                   cumulative_energy_kwh,
                   ST_Y(location::geometry) AS latitude,
                   ST_X(location::geometry) AS longitude,
                   row_number() OVER (ORDER BY recorded_at) AS position,
                   count(*) OVER () AS total,
                   min(odometer_m) OVER () AS odometer_start
              FROM telemetry
             WHERE trip_id = :trip_id
        )
        SELECT recorded_at,
               greatest(0.0, (odometer_m - odometer_start) / 1000.0) AS offset_km,
               speed_kmh,
               battery_soc_percent,
               energy_consumption_kwh_100km,
               cumulative_energy_kwh,
               latitude,
               longitude,
               total
          FROM ordered
         WHERE position % greatest(1, ceil(total::numeric / :max_points)::int) = 0
            OR position = total
         ORDER BY recorded_at
        """
    )
    rows = (
        (await session.execute(statement, {"trip_id": trip_id, "max_points": _MAX_SERIES_POINTS}))
        .mappings()
        .all()
    )
    if not rows:
        return [], 0
    total = int(rows[0]["total"])
    points = [TripSeriesPoint.model_validate(dict(row)) for row in rows]
    return points, total
