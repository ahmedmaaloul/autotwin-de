"""``seed demo`` — everything ``make demo`` needs, deterministically and idempotently.

One command has to leave a machine with a database that looks like a working product: the five
vehicle profiles, the five demo corridors with their analysis segments, real German charging
infrastructure, real weather, real traffic, and a fleet of simulated vehicles for the simulator
to pick up.

Two rules govern how it does that.

**It never lies about where a row came from.** Whatever the network allows, every ingestion
records its true :class:`~autotwin_contracts.ProviderMode` — ``live``, ``cache`` or
``fixture`` — on its ``data_ingestion_runs`` row, and the summary printed at the end says which
one answered, per source, in plain words. A demo assembled entirely from bundled samples is a
perfectly good demo; a demo that *claims* to be live when it is not is worthless. The vehicles
this module creates carry ``data_origin = simulated`` and ``source = simulator``, because they
are invented and the UI has to be able to stamp them ``SIMULIERT`` (BUILD_SPEC §0.2).

**It degrades instead of failing.** A source that cannot be reached at all costs its own tile
and nothing else: the remaining steps still run, the summary reports the failure, and the exit
code is non-zero only if nothing usable was produced. ``make demo`` on a train with no signal
should still end with a populated dashboard.

``--reset`` deletes the ingested and simulated data first — everything except the vehicle
profiles of migration ``0002``, which are reference data rather than demo data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final
from uuid import UUID

import sqlalchemy as sa

from autotwin_contracts import (
    GENERIC_VEHICLE_PROFILES,
    DataOrigin,
    ProviderMode,
    SourceSystem,
    VehicleProfile,
    VehicleState,
    utc_now,
)
from autotwin_core.config import DataMode, Settings, get_settings
from autotwin_core.db import (
    ChargingPoint,
    ChargingStation,
    DataIngestionRun,
    Route,
    RouteSegment,
    SimulationRun,
    Telemetry,
    TrafficEvent,
    Trip,
    Vehicle,
    VehicleModel,
    WeatherObservation,
    WeatherStation,
    session_scope,
)
from autotwin_core.errors import AutoTwinError
from autotwin_core.logging import get_logger
from autotwin_ingestion.pipelines import (
    ChargingIngestionPipeline,
    IngestionResult,
    RouteSeedPipeline,
    TrafficIngestionPipeline,
    WeatherIngestionPipeline,
)

__all__ = [
    "DEMO_URLS",
    "DEMO_VEHICLE_MIX",
    "DemoSummary",
    "ensure_vehicle_models",
    "reset_demo_data",
    "seed_demo",
    "seed_demo_vehicles",
]

_logger = get_logger(__name__)

DEMO_VEHICLE_MIX: Final[tuple[tuple[str, float], ...]] = (
    ("compact_ev", 0.30),
    ("sedan_ev", 0.25),
    ("suv_ev", 0.20),
    ("performance_ev", 0.125),
    ("van_ev", 0.125),
)
"""Share of the demo fleet per vehicle profile.

Roughly the shape of the German BEV fleet — compacts dominate, vans are a niche — so the
aggregate consumption on the dashboard is not the average of five equally weighted classes.
Allocated by largest remainder, so the counts are exact and the same on every machine.
"""

_VEHICLE_ID_TEMPLATE: Final[str] = "ATW-{index:04d}"
"""Human vehicle id (BUILD_SPEC §3.2). Deterministic, so re-seeding updates rather than adds."""

DEMO_URLS: Final[tuple[tuple[str, str], ...]] = (
    ("Dashboard", "http://localhost:3000"),
    ("Live-Karte", "http://localhost:3000/live"),
    ("Routen-Analyse", "http://localhost:3000/routes"),
    ("Datenqualität", "http://localhost:3000/data-quality"),
    ("API-Dokumentation", "http://localhost:8000/docs"),
)
"""What to open once the seed finishes, on the fixed ports of BUILD_SPEC §5."""

# Child rows first: the FKs cascade, but naming the order makes the intent reviewable and keeps
# the delete working if a cascade is ever tightened to RESTRICT.
_RESET_ORDER: Final[tuple[type[Any], ...]] = (
    Telemetry,
    Trip,
    Vehicle,
    SimulationRun,
    RouteSegment,
    Route,
    ChargingPoint,
    ChargingStation,
    WeatherObservation,
    WeatherStation,
    TrafficEvent,
    DataIngestionRun,
)
"""Tables ``--reset`` empties, in dependency order. ``vehicle_models`` is deliberately absent:
the five profiles are reference data seeded by migration ``0002``, not demo data."""


@dataclass(slots=True)
class DemoSummary:
    """What the seed did, per step, for the human-readable report at the end."""

    vehicle_models: int = 0
    """Profiles present in ``vehicle_models`` after the seed."""

    vehicles: int = 0
    """Simulated vehicles present after the seed."""

    reset: bool = False
    """Whether existing demo data was deleted first."""

    ingestions: list[IngestionResult] = field(default_factory=list)
    """One entry per pipeline that ran to completion."""

    failures: list[tuple[str, str]] = field(default_factory=list)
    """``(step, message)`` for every step that could not run at all."""

    counts: dict[str, int] = field(default_factory=dict)
    """Row counts per table after the seed — what the summary shows and the README promises."""

    @property
    def succeeded(self) -> bool:
        """Whether the demo is usable.

        True when at least one ingestion produced rows *and* the corridors are in place: those
        two together are what every page of the UI needs. Individual source failures are
        reported but do not make the seed a failure.
        """
        return self.counts.get("routes", 0) > 0 and self.counts.get("charging_stations", 0) > 0

    def summary_lines(self) -> list[str]:
        """Render the report: what ran, in which mode, and what is now in the database."""
        lines: list[str] = []
        if self.reset:
            lines.append("reset           existing demo data deleted")
        lines.append(f"vehicle models  {self.vehicle_models}")
        lines.append(f"vehicles        {self.vehicles} (simulated)")
        lines.append("")
        lines.append("sources")
        for result in self.ingestions:
            lines.append(
                f"  {result.source.value:<18} {_mode_phrase(result.mode):<34} "
                f"{result.report.rows_accepted:>7} accepted  "
                f"{result.report.rows_rejected:>5} rejected"
            )
        for step, message in self.failures:
            lines.append(f"  {step:<18} FAILED — {message}")

        notes = [
            f"  {result.source.value}: {key} — {value}"
            for result in self.ingestions
            for key, value in result.details.items()
            if "failed" in key
        ]
        if notes:
            lines.append("")
            lines.append("partial")
            lines.extend(notes)
            lines.append(
                "  The bundled OSRM sample covers only Frankfurt am Main → Stuttgart; "
                "re-run with --mode cached once online to seed the rest."
            )
        lines.append("")
        lines.append("database")
        for table, count in self.counts.items():
            lines.append(f"  {table:<24} {count:>8,}")
        lines.append("")
        lines.append("open")
        for label, url in DEMO_URLS:
            lines.append(f"  {label:<20} {url}")
        return lines


def _mode_phrase(mode: ProviderMode) -> str:
    """Say in words how a source answered — the honest half of the demo summary."""
    match mode:
        case ProviderMode.live:
            return "live from the official source"
        case ProviderMode.cache:
            return "from the on-disk cache (not live)"
        case _:
            return "from the bundled fixture (not live)"


async def seed_demo(
    *,
    settings: Settings | None = None,
    data_mode: DataMode | None = None,
    reset: bool = False,
    vehicle_count: int | None = None,
    charging_limit: int | None = None,
) -> DemoSummary:
    """Populate the database with everything the demo needs.

    Args:
        settings: Configuration source; defaults to the process-wide settings.
        data_mode: Provider policy for every ingestion in this seed.
        reset: Delete the ingested and simulated data before seeding.
        vehicle_count: Simulated vehicles to create; defaults to
            ``AUTOTWIN_SIM_DEFAULT_VEHICLES``.
        charging_limit: Cap on charging sites ingested, for a faster demo. ``None`` ingests
            whatever the register offers.

    Returns:
        A summary naming, per source, whether the data is live, cached or bundled.
    """
    config = settings or get_settings()
    summary = DemoSummary(reset=reset)

    if reset:
        deleted = await reset_demo_data()
        _logger.info("demo.reset", rows=deleted)

    summary.vehicle_models = await ensure_vehicle_models()
    await _run_step(summary, "routes", RouteSeedPipeline(settings=config, data_mode=data_mode))
    await _run_step(
        summary,
        "charging",
        ChargingIngestionPipeline(settings=config, data_mode=data_mode, limit=charging_limit),
    )
    await _run_step(
        summary, "weather", WeatherIngestionPipeline(settings=config, data_mode=data_mode)
    )
    await _run_step(
        summary, "traffic", TrafficIngestionPipeline(settings=config, data_mode=data_mode)
    )

    summary.vehicles = await seed_demo_vehicles(
        vehicle_count if vehicle_count is not None else config.sim_default_vehicles
    )
    summary.counts = await table_counts()
    return summary


async def _run_step(summary: DemoSummary, label: str, pipeline: Any) -> None:
    """Run one pipeline, recording either its result or why it could not run.

    Only :class:`~autotwin_core.errors.AutoTwinError` is caught: a provider outage or a source
    that changed shape is an expected, reportable condition, while a ``TypeError`` in this
    module is a bug and must not be swallowed into a tidy summary line.
    """
    try:
        summary.ingestions.append(await pipeline.run())
    except AutoTwinError as error:
        summary.failures.append((label, f"{type(error).__name__}: {error}"))
        _logger.warning(
            "demo.step_failed",
            step=label,
            error=type(error).__name__,
            detail=str(error),
        )
    finally:
        await pipeline.aclose()


async def ensure_vehicle_models() -> int:
    """Insert any missing generic vehicle profile, and report how many are present.

    Migration ``0002`` seeds these, so normally this is a no-op that confirms the schema is
    where it should be. It exists because a database restored from a dump taken before that
    migration — or one whose profiles were deleted during an experiment — would otherwise fail
    the demo at the point where vehicles are created, with a foreign-key error that says
    nothing about the cause.
    """
    async with session_scope() as session:
        existing = {code for (code,) in (await session.execute(sa.select(VehicleModel.code))).all()}
        missing = [profile for profile in GENERIC_VEHICLE_PROFILES if profile.code not in existing]
        if missing:
            await session.execute(
                sa.insert(VehicleModel),
                [_vehicle_model_values(profile) for profile in missing],
            )
            _logger.info("demo.vehicle_models_seeded", codes=[p.code for p in missing])
        return len(existing) + len(missing)


def _vehicle_model_values(profile: VehicleProfile) -> dict[str, Any]:
    """Render a profile as ``vehicle_models`` column values, with its deterministic id."""
    return {
        "id": profile.stable_id,
        "code": profile.code,
        "display_name": profile.display_name,
        "vehicle_class": profile.vehicle_class,
        "battery_capacity_kwh": profile.battery_capacity_kwh,
        "usable_capacity_kwh": profile.usable_capacity_kwh,
        "nominal_consumption_kwh_100km": profile.nominal_consumption_kwh_100km,
        "max_dc_power_kw": profile.max_dc_power_kw,
        "max_ac_power_kw": profile.max_ac_power_kw,
        "mass_kg": profile.mass_kg,
        "drag_coefficient": profile.drag_coefficient,
        "frontal_area_m2": profile.frontal_area_m2,
        "rolling_resistance": profile.rolling_resistance,
        "is_generic": profile.is_generic,
    }


async def seed_demo_vehicles(count: int) -> int:
    """Create (or refresh) ``count`` simulated vehicles and return how many now exist.

    Ids are ``ATW-0001`` upwards and the model mix is allocated by largest remainder, so the
    same ``count`` always produces the same fleet — the reproducibility BUILD_SPEC §0.5 asks
    for, without needing a random seed at all. Vehicles beyond ``count`` are left alone: they
    may belong to a simulation run that is still going.

    Raises:
        ValueError: ``count`` is negative.
    """
    if count < 0:
        msg = f"vehicle count must not be negative, got {count!r}"
        raise ValueError(msg)
    if count == 0:
        return 0

    allocation = _allocate_mix(count)
    async with session_scope() as session:
        model_rows = (await session.execute(sa.select(VehicleModel.code, VehicleModel.id))).all()
        model_ids: dict[str, UUID] = {str(row[0]): row[1] for row in model_rows}
        missing = [code for code, _ in allocation if code not in model_ids]
        if missing:
            msg = f"vehicle_models is missing the demo profiles {missing}; run migrations first"
            raise AutoTwinError(msg)

        existing = {
            vehicle_id
            for (vehicle_id,) in (await session.execute(sa.select(Vehicle.vehicle_id))).all()
        }
        ingested_at = utc_now()
        rows: list[dict[str, Any]] = []
        index = 1
        for code, share in allocation:
            for _ in range(share):
                vehicle_id = _VEHICLE_ID_TEMPLATE.format(index=index)
                index += 1
                if vehicle_id in existing:
                    continue
                rows.append(
                    {
                        "vehicle_id": vehicle_id,
                        "vehicle_model_id": model_ids[code],
                        "simulation_run_id": None,
                        "state": VehicleState.idle,
                        "source": SourceSystem.simulator,
                        "source_identifier": vehicle_id,
                        "source_url": None,
                        "source_timestamp": None,
                        # Never anything else: these vehicles do not exist, and this column is
                        # what makes the UI say so (BUILD_SPEC §0.2).
                        "data_origin": DataOrigin.simulated,
                        "ingestion_run_id": None,
                        "ingested_at": ingested_at,
                    }
                )
        if rows:
            await session.execute(sa.insert(Vehicle), rows)
        total = await session.scalar(sa.select(sa.func.count()).select_from(Vehicle))
    _logger.info("demo.vehicles_seeded", created=len(rows), total=int(total or 0))
    return int(total or 0)


def _allocate_mix(count: int) -> list[tuple[str, int]]:
    """Split ``count`` across :data:`DEMO_VEHICLE_MIX` by largest remainder.

    Largest remainder rather than rounding each share independently: rounding loses or invents
    vehicles (five shares of 0.5 round to five extra cars), and a fleet whose size differs from
    the number the operator asked for is a bug nobody notices until the dashboard count is odd.
    """
    exact = [(code, count * share) for code, share in DEMO_VEHICLE_MIX]
    floors = [(code, int(value)) for code, value in exact]
    remainder = count - sum(value for _, value in floors)
    ranked = sorted(
        range(len(exact)),
        key=lambda index: (-(exact[index][1] - floors[index][1]), index),
    )
    allocation = dict(floors)
    for position in ranked[:remainder]:
        allocation[exact[position][0]] += 1
    return [(code, allocation[code]) for code, _ in DEMO_VEHICLE_MIX]


async def reset_demo_data() -> dict[str, int]:
    """Delete every ingested and simulated row, keeping the vehicle profiles.

    Returns the number of rows deleted per table. Ordinary ``DELETE`` rather than ``TRUNCATE``:
    the row counts are what the operator is shown, and a truncate would report nothing.
    """
    deleted: dict[str, int] = {}
    async with session_scope() as session:
        for table in _RESET_ORDER:
            # ``returning(id)`` rather than ``rowcount``: the async Result type does not expose
            # a row count, and counting the returned keys is exact for every backend.
            rows = await session.execute(sa.delete(table).returning(table.id))
            deleted[table.__tablename__] = len(rows.all())
    return deleted


async def table_counts() -> dict[str, int]:
    """Row count per table the demo populates, in the order the summary prints them."""
    tables = (
        VehicleModel,
        Vehicle,
        Route,
        RouteSegment,
        ChargingStation,
        ChargingPoint,
        WeatherStation,
        WeatherObservation,
        TrafficEvent,
        DataIngestionRun,
    )
    counts: dict[str, int] = {}
    async with session_scope() as session:
        for table in tables:
            total = await session.scalar(sa.select(sa.func.count()).select_from(table))
            counts[table.__tablename__] = int(total or 0)
    return counts
