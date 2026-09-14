"""``GET /api/v1/dashboard/summary`` — the landing page, in one round trip.

The whole payload is assembled by a **single** statement. That is not micro-optimisation: eight
separate queries would each see a slightly different database, and the screen would then show a
vehicle count from one instant beside an energy trend from another. One statement means one
snapshot, and ``generated_at`` is the honest timestamp for all of it.

The statement is written as ``sqlalchemy.text`` rather than assembled through the ORM because
what it does — lateral aggregates projected into ``jsonb`` arrays, ``DISTINCT ON`` for the
newest run per source, PostGIS accessors on the event geometry — is SQL that the ORM would only
obscure. Every value a caller can influence is a bound parameter; nothing is interpolated.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Final

from fastapi import APIRouter
from sqlalchemy import text

from autotwin_api.deps import DbSession
from autotwin_api.schemas.dashboard import (
    ChargingCategoryCount,
    DashboardSummary,
    DashboardTrafficEvent,
    DataFreshnessEntry,
    EnergyTrendPoint,
    WeatherSnapshot,
)
from autotwin_api.schemas.data import SOURCE_TERMS, age_minutes_of, derive_status
from autotwin_contracts import IngestionOutcome, ProviderMode, SourceSystem, utc_now
from autotwin_contracts.records import FAST_CHARGER_THRESHOLD_KW
from autotwin_core.logging import get_logger

__all__ = ["router"]

_LOGGER = get_logger(__name__)

router = APIRouter()

_ACTIVE_WINDOW_MINUTES: Final[float] = 15.0
"""Width of the activity window, measured **backwards from the newest telemetry sample**.

Not from the wall clock, and that is the whole point. The simulator starts its clock an hour in
the past so that a fresh run has a populated history, and it can then run twenty times faster
than real time (``DEFAULT_WARMUP_LOOKBACK_S``, ``speed_factor``). Its timestamps therefore sit
at an offset from *now* that depends on how long the run has been going — so a wall-clock
window would report an empty fleet seconds after a perfectly healthy run finished.

Anchoring on the newest sample answers the question the tile actually asks: how large is the
fleet in the most recent slice of the telemetry timeline.
"""

_TREND_HOURS: Final[int] = 24
"""Span of the consumption trend. A full day so the overnight/rush-hour shape is visible."""

_MOVING_SPEED_KMH: Final[float] = 5.0
"""Speed below which a telemetry sample is excluded from consumption statistics.

Consumption per 100 km divides by distance. A stationary or crawling vehicle therefore produces
an arbitrarily large kWh/100 km figure that says nothing about the vehicle's efficiency, and a
handful of them would dominate any mean they were allowed into.
"""

_RECENT_TRAFFIC_LIMIT: Final[int] = 5

_SUMMARY_SQL: Final[str] = """
WITH freshness AS (
    SELECT DISTINCT ON (source)
           source, started_at, status, provider_mode
      FROM data_ingestion_runs
     ORDER BY source, started_at DESC
),
trend AS (
    SELECT date_trunc('hour', recorded_at) AS bucket,
           avg(energy_consumption_kwh_100km) AS kwh_100km
      FROM telemetry
     WHERE recorded_at >= :trend_from
       AND speed_kmh >= :moving_kmh
     GROUP BY 1
),
traffic AS (
    SELECT id, external_id, event_type, severity, road_name, direction, title, description,
           ST_Y(location::geometry) AS latitude,
           ST_X(location::geometry) AS longitude,
           ST_AsGeoJSON(geometry)::jsonb AS geometry,
           starts_at, ends_at, is_blocked, delay_minutes,
           coalesce(starts_at, source_timestamp, ingested_at) AS sort_at
      FROM traffic_events
     ORDER BY sort_at DESC
     LIMIT :traffic_limit
),
weather AS (
    SELECT o.temperature_c, o.condition, o.observed_at, s.name AS station
      FROM weather_observations o
      LEFT JOIN weather_stations s ON s.id = o.weather_station_id
     ORDER BY o.observed_at DESC
     LIMIT 1
),
categories AS (
    SELECT charging_category AS category, count(*) AS count
      FROM charging_stations
     GROUP BY charging_category
)
SELECT
    (SELECT count(DISTINCT vehicle_id) FROM telemetry
      WHERE recorded_at >= (
              SELECT max(recorded_at) - make_interval(secs => :active_window_s) FROM telemetry
            )) AS vehicles_active,
    (SELECT count(*) FROM charging_stations) AS charging_stations_total,
    (SELECT count(*) FROM charging_points WHERE power_kw >= :fast_kw)
        AS fast_charging_points_total,
    (SELECT count(*) FROM traffic_events
      WHERE (starts_at IS NULL OR starts_at <= :now)
        AND (ends_at IS NULL OR ends_at >= :now)) AS traffic_events_active,
    (SELECT avg(energy_consumption_kwh_100km) FROM telemetry
      WHERE recorded_at >= :trend_from AND speed_kmh >= :moving_kmh)
        AS avg_consumption_kwh_100km,
    (SELECT jsonb_agg(to_jsonb(f) ORDER BY f.started_at DESC) FROM freshness f)
        AS data_freshness,
    (SELECT jsonb_agg(to_jsonb(t) ORDER BY t.bucket) FROM trend t) AS energy_trend,
    (SELECT jsonb_agg(to_jsonb(x) ORDER BY x.sort_at DESC) FROM traffic x) AS recent_traffic,
    (SELECT to_jsonb(w) FROM weather w) AS weather_snapshot,
    (SELECT jsonb_agg(to_jsonb(c) ORDER BY c.count DESC) FROM categories c)
        AS charging_by_category
"""


def _freshness_entry(row: Mapping[str, Any], *, now: datetime) -> DataFreshnessEntry | None:
    """Turn one ``data_ingestion_runs`` row into its dashboard entry.

    Returns ``None`` for a source this build does not know, rather than raising: a database
    seeded by a newer version of the ingestion service must not be able to break the landing
    page of an older API.
    """
    try:
        source = SourceSystem(row["source"])
    except ValueError:
        _LOGGER.warning("dashboard.unknown_source", source=row.get("source"))
        return None

    started_at = row["started_at"]
    last_run_at = datetime.fromisoformat(started_at) if isinstance(started_at, str) else started_at
    age = age_minutes_of(last_run_at, now=now)
    mode = ProviderMode(row["provider_mode"]) if row.get("provider_mode") else None
    outcome = IngestionOutcome(row["status"]) if row.get("status") else None
    return DataFreshnessEntry(
        source=source,
        last_run_at=last_run_at,
        status=derive_status(
            source=source,
            outcome=outcome,
            provider_mode=mode,
            age_minutes=age,
        ),
        age_minutes=age,
        data_origin=SOURCE_TERMS[source].data_origin,
        mode=mode,
    )


@router.get(
    "/summary",
    response_model=DashboardSummary,
    summary="Dashboard headline figures",
    description=(
        "Everything the landing page shows, read in a single database round trip so that the "
        "tiles cannot contradict one another (BUILD_SPEC §7.1).\n\n"
        "* `vehicles_active` counts the vehicles in the newest 15 minutes of the **telemetry "
        "timeline** — not of the wall clock, because the simulator deliberately runs its clock "
        "offset from real time, and not from `vehicles.state`, which a crashed run would leave "
        "stale.\n"
        "* `avg_consumption_kwh_100km` and `energy_trend` average **moving** vehicles only "
        "(≥ 5 km/h): a stationary sample's kWh/100 km is a division by almost zero.\n"
        "* `data_freshness` carries one entry per source that has ever run here, with the "
        "status rule of BUILD_SPEC §6 applied — a source answering from cache reports "
        "`degraded`, never `healthy`.\n"
        "* Every figure is as of `generated_at`."
    ),
)
async def dashboard_summary(session: DbSession) -> DashboardSummary:
    """Assemble the dashboard from one statement and derive the per-source health in Python.

    The status rule stays out of SQL on purpose: it depends on each publisher's refresh cadence
    (``SOURCE_TERMS``), which is editorial knowledge about the sources rather than a property of
    the rows, and it must produce exactly the same answer here as in ``/api/v1/data/quality``.
    """
    now = utc_now()
    row = (
        (
            await session.execute(
                text(_SUMMARY_SQL),
                {
                    "now": now,
                    "trend_from": now - timedelta(hours=_TREND_HOURS),
                    "active_window_s": _ACTIVE_WINDOW_MINUTES * 60.0,
                    "moving_kmh": _MOVING_SPEED_KMH,
                    "fast_kw": FAST_CHARGER_THRESHOLD_KW,
                    "traffic_limit": _RECENT_TRAFFIC_LIMIT,
                },
            )
        )
        .mappings()
        .one()
    )

    freshness_rows: list[Mapping[str, Any]] = list(row["data_freshness"] or [])
    freshness = [
        entry
        for entry in (_freshness_entry(item, now=now) for item in freshness_rows)
        if entry is not None
    ]

    weather_row = row["weather_snapshot"]
    average = row["avg_consumption_kwh_100km"]

    return DashboardSummary(
        vehicles_active=int(row["vehicles_active"]),
        charging_stations_total=int(row["charging_stations_total"]),
        fast_charging_points_total=int(row["fast_charging_points_total"]),
        traffic_events_active=int(row["traffic_events_active"]),
        avg_consumption_kwh_100km=None if average is None else float(average),
        data_freshness=freshness,
        energy_trend=[
            EnergyTrendPoint.model_validate(item) for item in (row["energy_trend"] or [])
        ],
        recent_traffic=[
            DashboardTrafficEvent.model_validate(item) for item in (row["recent_traffic"] or [])
        ],
        weather_snapshot=(
            None if weather_row is None else WeatherSnapshot.model_validate(weather_row)
        ),
        charging_by_category=[
            ChargingCategoryCount.model_validate(item)
            for item in (row["charging_by_category"] or [])
        ],
        generated_at=now,
    )
