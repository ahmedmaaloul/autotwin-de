"""Aggregation of simulated telemetry into the ML training set (BUILD_SPEC §10.2).

One row is one **60-second driving window** of one trip. The window is the unit of aggregation
because the target — ``energy_consumption_kwh_100km`` — is a *rate*, and a rate needs a distance
to be defined over; a single 10-second telemetry snapshot carries an instantaneous speed but no
meaningful consumption.

The column set is a contract shared with :mod:`autotwin_ml`, which reads this Parquet file and
never sees the simulator. It is spelled out once, here, in :data:`TRAINING_SCHEMA`, and the
twenty model features of BUILD_SPEC §10.2 are a subset of it.

Three decisions in this module are worth knowing before reading a trained model's metrics.

**Only fully-driving windows are kept.** A window in which the vehicle was charging, idle or
stopped is dropped, because energy flowing *into* the pack from a charger has nothing to do with
the consumption the model is asked to predict, and a window containing a standstill at a charging
pillar would teach it that low speed means high consumption for entirely the wrong reason.
Standstills *in traffic* are kept — the vehicle is still in ``driving`` state approaching a
stopped target — which is exactly the stop-and-go signal the model should learn.

**Windows shorter than :data:`MIN_WINDOW_DISTANCE_KM` are dropped.** Dividing by a distance that
approaches zero produces a target that approaches infinity, and a handful of such rows dominates
any squared-error objective.

**The distributional features come from the physics step, not from the samples.** The simulator
accumulates speed, speed², |acceleration| and the acceleration-event count at 1 s resolution
inside :class:`~autotwin_simulator.vehicle.IntervalStatistics`, and this module sums those. A
standard deviation computed from six 10-second snapshots would be a different, much blunter
quantity — and it is precisely the stop-and-go structure it would blur that the learned model
adds over the physical baseline.

Every row carries ``data_origin = "simulated"``. That is not decoration: BUILD_SPEC §0.2 and
ADR 004 make it the mechanism by which a model trained on this file can never be mistaken for one
trained on fleet data.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import polars as pl

from autotwin_contracts import (
    DataOrigin,
    RoadClass,
    TrafficSeverity,
    VehicleProfile,
    VehicleState,
)
from autotwin_core.logging import get_logger
from autotwin_ml.constants import DEFAULT_PHYSICS
from autotwin_simulator.engine import SimulationConfig, SimulationEngine
from autotwin_simulator.environment import RouteEnvironment
from autotwin_simulator.vehicle import IntervalStatistics, TelemetrySample

__all__ = [
    "DEFAULT_TRAINING_BATCHES",
    "DEFAULT_TRAINING_DATA_PATH",
    "MIN_WINDOW_DISTANCE_KM",
    "MODEL_FEATURE_COLUMNS",
    "TARGET_COLUMN",
    "TRAINING_REFERENCE_YEAR",
    "TRAINING_SCHEMA",
    "UNRESTRICTED_SPEED_LIMIT_KMH",
    "WINDOW_SECONDS",
    "TrainingDataResult",
    "TrainingWindow",
    "WindowAggregator",
    "build_generator_engine",
    "generate_seasonal_training_data",
    "generate_training_data",
    "seasonal_start_times",
    "windows_to_frame",
    "write_training_parquet",
]

_LOGGER = get_logger(__name__)

WINDOW_SECONDS: Final[float] = 60.0
"""Length of one aggregation window in simulated seconds.

A minute of motorway driving is 2 km — long enough for the road class, the weather and the
traffic state to be meaningfully constant, short enough that a 200 km corridor yields ~100 rows
per vehicle. It is also the resolution at which the route analyser's 5 km segments and this
training set describe the same kind of thing, which is what makes the model transferable from
one to the other."""

MIN_WINDOW_DISTANCE_KM: Final[float] = 0.05
"""Shortest window that gets a row. Below 50 m the target is numerically meaningless."""

UNRESTRICTED_SPEED_LIMIT_KMH: Final[float] = 130.0
"""Numeric stand-in for ``speed_limit_kmh`` on an unrestricted Autobahn stretch.

The training contract types this column as a plain float, but a third of the German Autobahn
network genuinely has no limit and OSM reports ``None`` there. Encoding that as 130 — the
*Richtgeschwindigkeit*, the advisory speed — rather than as 0 or as a sentinel keeps the feature
monotone and keeps a tree model from learning that "no limit" means "stationary". The loss is
real and is stated here: the model cannot distinguish an unrestricted stretch from one posted at
130."""

DEFAULT_TRAINING_DATA_PATH: Final[Path] = Path("data/gold/training/energy_windows.parquet")
"""Where ``generate-training-data`` writes, and where ``autotwin_ml.cli train`` reads."""

DEFAULT_TRAINING_BATCHES: Final[int] = 6
"""How many seasonal batches a training-set generation is split into.

**This is the single most consequential choice in this module.** One continuous run covers a few
simulated days, and the synthetic climatology moves by less than two Kelvin over that span — so
a model trained on it would see one temperature and would learn nothing at all about the winter
penalty that is the whole point of the exercise. Splitting the same trip budget across six
anchor dates spread through the year produces a training set spanning roughly -5 °C to +30 °C,
which is the range a German EV actually operates in."""

TRAINING_REFERENCE_YEAR: Final[int] = 2025
"""Calendar year the seasonal batches are anchored in.

Fixed and in the past, so that the generated file is identical whenever it is generated and its
timestamps never claim to be newer than the fleet data around them."""

MODEL_FEATURE_COLUMNS: Final[tuple[str, ...]] = (
    "speed_kmh",
    "avg_speed_kmh",
    "speed_std_kmh",
    "acceleration_abs_mean_ms2",
    "accel_events_per_km",
    "outside_temperature_c",
    "battery_temperature_c",
    "soc_percent",
    "road_class_ordinal",
    "speed_limit_kmh",
    "traffic_severity_ordinal",
    "precipitation_mm",
    "wind_speed_ms",
    "gradient_percent",
    "segment_distance_km",
    "mass_kg",
    "drag_area",
    "nominal_consumption_kwh_100km",
    "hvac_load_kw",
    "is_motorway",
)
"""The twenty features of BUILD_SPEC §10.2, in the specified order.

The authoritative definition is ``autotwin_ml.features.FEATURE_NAMES``; this copy exists so that
:func:`windows_to_frame` can assert that every feature the model will ask for is actually a
column of the file it writes. Duplicating the *list* to check the *file* is the point — a
training set silently missing a feature is a bug that only surfaces as a confusing KeyError
inside somebody else's trainer."""

TARGET_COLUMN: Final[str] = "energy_consumption_kwh_100km"
"""The regression target."""

TRAINING_SCHEMA: Final[dict[str, Any]] = {
    "trip_id": pl.Utf8,
    "vehicle_id": pl.Utf8,
    "vehicle_code": pl.Utf8,
    "window_start": pl.Datetime(time_unit="us", time_zone="UTC"),
    "window_end": pl.Datetime(time_unit="us", time_zone="UTC"),
    "distance_km": pl.Float64,
    "duration_s": pl.Float64,
    "speed_kmh": pl.Float64,
    "avg_speed_kmh": pl.Float64,
    "speed_std_kmh": pl.Float64,
    "acceleration_abs_mean_ms2": pl.Float64,
    "accel_events_per_km": pl.Float64,
    "outside_temperature_c": pl.Float64,
    "battery_temperature_c": pl.Float64,
    "soc_percent": pl.Float64,
    "road_class": pl.Utf8,
    "road_class_ordinal": pl.Int32,
    "speed_limit_kmh": pl.Float64,
    "traffic_severity": pl.Utf8,
    "traffic_severity_ordinal": pl.Int32,
    "precipitation_mm": pl.Float64,
    "wind_speed_ms": pl.Float64,
    "gradient_percent": pl.Float64,
    "segment_distance_km": pl.Float64,
    "mass_kg": pl.Float64,
    "drag_area": pl.Float64,
    "nominal_consumption_kwh_100km": pl.Float64,
    "hvac_load_kw": pl.Float64,
    "is_motorway": pl.Int32,
    "energy_kwh": pl.Float64,
    TARGET_COLUMN: pl.Float64,
    "data_origin": pl.Utf8,
}
"""Column order and dtype of ``energy_windows.parquet`` — the contract with :mod:`autotwin_ml`.

Explicit rather than inferred. Polars would happily infer ``Int64`` for ``is_motorway`` in one
run and ``Int32`` in another depending on the values it saw first, and a dtype that drifts
between training sets is the kind of defect that only shows up as a silent cast much later."""

_SEVERITY_BY_ORDINAL: Final[dict[int, TrafficSeverity]] = {
    severity.ordinal: severity for severity in TrafficSeverity
}
"""Inverse of :attr:`~autotwin_contracts.enums.TrafficSeverity.ordinal`, for decoding a mean."""


def seasonal_start_times(
    batches: int = DEFAULT_TRAINING_BATCHES,
    *,
    year: int = TRAINING_REFERENCE_YEAR,
) -> list[datetime]:
    """Evenly spaced anchor instants across one year, for the seasonal batches.

    The offsets are whole days plus a rotating hour of the day, so the batches differ in season
    *and* in the part of the diurnal cycle they start on. Deterministic: same arguments, same
    list, no clock involved.
    """
    if batches < 1:
        msg = f"batches must be at least 1, got {batches}"
        raise ValueError(msg)
    origin = datetime(year, 1, 8, 5, 0, tzinfo=UTC)
    spacing_days = 365.0 / batches
    return [
        origin + timedelta(days=round(index * spacing_days), hours=(index * 5) % 24)
        for index in range(batches)
    ]


# --------------------------------------------------------------------------------------
# One aggregated window
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrainingWindow:
    """One row of the training set, before it becomes a Parquet record."""

    trip_id: str
    vehicle_id: str
    vehicle_code: str
    window_start: datetime
    window_end: datetime
    distance_km: float
    duration_s: float
    speed_kmh: float
    avg_speed_kmh: float
    speed_std_kmh: float
    acceleration_abs_mean_ms2: float
    accel_events_per_km: float
    outside_temperature_c: float
    battery_temperature_c: float
    soc_percent: float
    road_class: RoadClass
    speed_limit_kmh: float
    traffic_severity: TrafficSeverity
    precipitation_mm: float
    wind_speed_ms: float
    gradient_percent: float
    mass_kg: float
    drag_area: float
    nominal_consumption_kwh_100km: float
    hvac_load_kw: float
    energy_kwh: float

    @property
    def energy_consumption_kwh_100km(self) -> float:
        """The target: kWh per 100 km over this window."""
        return self.energy_kwh / self.distance_km * 100.0

    def as_row(self) -> dict[str, Any]:
        """Project onto :data:`TRAINING_SCHEMA`, in that exact order."""
        return {
            "trip_id": self.trip_id,
            "vehicle_id": self.vehicle_id,
            "vehicle_code": self.vehicle_code,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "distance_km": self.distance_km,
            "duration_s": self.duration_s,
            "speed_kmh": self.speed_kmh,
            "avg_speed_kmh": self.avg_speed_kmh,
            "speed_std_kmh": self.speed_std_kmh,
            "acceleration_abs_mean_ms2": self.acceleration_abs_mean_ms2,
            "accel_events_per_km": self.accel_events_per_km,
            "outside_temperature_c": self.outside_temperature_c,
            "battery_temperature_c": self.battery_temperature_c,
            "soc_percent": self.soc_percent,
            "road_class": self.road_class.value,
            "road_class_ordinal": self.road_class.ordinal,
            "speed_limit_kmh": self.speed_limit_kmh,
            "traffic_severity": self.traffic_severity.value,
            "traffic_severity_ordinal": self.traffic_severity.ordinal,
            "precipitation_mm": self.precipitation_mm,
            "wind_speed_ms": self.wind_speed_ms,
            "gradient_percent": self.gradient_percent,
            "segment_distance_km": self.distance_km,
            "mass_kg": self.mass_kg,
            "drag_area": self.drag_area,
            "nominal_consumption_kwh_100km": self.nominal_consumption_kwh_100km,
            "hvac_load_kw": self.hvac_load_kw,
            "is_motorway": int(self.road_class is RoadClass.motorway),
            "energy_kwh": self.energy_kwh,
            TARGET_COLUMN: self.energy_consumption_kwh_100km,
            "data_origin": DataOrigin.simulated.value,
        }


# --------------------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class _OpenWindow:
    """Accumulator for one trip's current window."""

    trip_id: str
    vehicle_id: str
    profile: VehicleProfile
    index: int
    window_start: datetime
    intervals: list[IntervalStatistics]
    driving_only: bool = True


class WindowAggregator:
    """Folds a stream of telemetry samples into 60-second windows.

    Streaming rather than batch: a training run generates millions of samples, and holding them
    all to group them afterwards would need gigabytes for a file that is tens of megabytes. A
    window is emitted the moment a sample belonging to the *next* window arrives, so memory is
    one open window per trip in flight — a few hundred objects, whatever the run's length.
    """

    __slots__ = ("_open", "_window_seconds", "completed_trips", "dropped_windows", "windows")

    def __init__(self, *, window_seconds: float = WINDOW_SECONDS) -> None:
        """Start with no open windows."""
        self._window_seconds = window_seconds
        self._open: dict[str, _OpenWindow] = {}
        self.windows: list[TrainingWindow] = []
        self.completed_trips: set[str] = set()
        self.dropped_windows = 0

    def add(self, sample: TelemetrySample) -> None:
        """Fold one telemetry sample into its trip's current window.

        Samples from an idle vehicle — no trip id — are ignored outright: there is no trip to
        group them under and nothing to learn from a parked car.
        """
        trip_id = sample.event.trip_id
        if trip_id is None:
            return
        if sample.event.state is VehicleState.completed:
            self.completed_trips.add(trip_id)

        current = self._open.get(trip_id)
        if current is None:
            current = self._start_window(trip_id, sample, index=0, anchor=sample.event.recorded_at)
            self._open[trip_id] = current

        index = self._window_index(current.window_start, sample.event.recorded_at, current.index)
        if index != current.index:
            self._close(current)
            anchor = current.window_start + timedelta(
                seconds=self._window_seconds * (index - current.index)
            )
            current = self._start_window(trip_id, sample, index=index, anchor=anchor)
            self._open[trip_id] = current

        current.intervals.append(sample.interval)
        if sample.interval.driving_steps != sample.interval.steps:
            current.driving_only = False

    def _start_window(
        self,
        trip_id: str,
        sample: TelemetrySample,
        *,
        index: int,
        anchor: datetime,
    ) -> _OpenWindow:
        """Open a fresh window for a trip."""
        return _OpenWindow(
            trip_id=trip_id,
            vehicle_id=sample.event.vehicle_id,
            profile=sample.profile,
            index=index,
            window_start=anchor,
            intervals=[],
        )

    def _window_index(self, anchor: datetime, recorded_at: datetime, current_index: int) -> int:
        """Index of the window ``recorded_at`` belongs to, relative to the open window."""
        elapsed_s = (recorded_at - anchor).total_seconds()
        return current_index + int(elapsed_s // self._window_seconds)

    def flush(self) -> None:
        """Close every open window. Call once the simulation has finished."""
        for window in list(self._open.values()):
            self._close(window)
        self._open.clear()

    def _close(self, window: _OpenWindow) -> None:
        """Aggregate an open window and keep it if it qualifies."""
        aggregated = _aggregate(window, window_seconds=self._window_seconds)
        if aggregated is None:
            self.dropped_windows += 1
            return
        self.windows.append(aggregated)

    def sorted_windows(self) -> list[TrainingWindow]:
        """Every kept window, ordered by trip and time.

        Sorting is what makes the output deterministic *as a file*: the aggregator emits windows
        in the order trips happen to close, which depends on nothing meaningful, while
        ``(trip_id, window_start)`` is a total order derived from the simulation itself.
        """
        return sorted(self.windows, key=lambda window: (window.trip_id, window.window_start))


def _aggregate(window: _OpenWindow, *, window_seconds: float) -> TrainingWindow | None:
    """Turn one window's accumulated intervals into a row, or ``None`` if it does not qualify."""
    if not window.driving_only or not window.intervals:
        return None
    steps = sum(interval.steps for interval in window.intervals)
    distance_km = sum(interval.distance_km for interval in window.intervals)
    duration_s = sum(interval.duration_s for interval in window.intervals)
    if steps == 0 or distance_km < MIN_WINDOW_DISTANCE_KM or duration_s <= 0.0:
        return None

    speed_sum = sum(interval.speed_sum_kmh for interval in window.intervals)
    speed_square_sum = sum(interval.speed_square_sum_kmh2 for interval in window.intervals)
    mean_speed_kmh = speed_sum / steps
    variance = max(0.0, speed_square_sum / steps - mean_speed_kmh * mean_speed_kmh)
    temperature_c = sum(interval.temperature_sum_c for interval in window.intervals) / steps
    profile = window.profile

    return TrainingWindow(
        trip_id=window.trip_id,
        vehicle_id=window.vehicle_id,
        vehicle_code=profile.code,
        window_start=window.window_start,
        window_end=window.window_start + timedelta(seconds=window_seconds),
        distance_km=distance_km,
        duration_s=duration_s,
        speed_kmh=mean_speed_kmh,
        avg_speed_kmh=distance_km / (duration_s / 3600.0),
        speed_std_kmh=math.sqrt(variance),
        acceleration_abs_mean_ms2=(
            sum(interval.absolute_acceleration_sum_ms2 for interval in window.intervals) / steps
        ),
        accel_events_per_km=(
            sum(interval.acceleration_events for interval in window.intervals) / distance_km
        ),
        outside_temperature_c=temperature_c,
        battery_temperature_c=(
            sum(interval.battery_temperature_sum_c for interval in window.intervals) / steps
        ),
        soc_percent=sum(interval.soc_sum_percent for interval in window.intervals) / steps,
        road_class=_dominant_road_class(window.intervals),
        speed_limit_kmh=(
            sum(interval.speed_limit_sum_kmh for interval in window.intervals) / steps
        ),
        traffic_severity=_mean_severity(window.intervals, steps),
        precipitation_mm=(
            sum(interval.precipitation_sum_mm for interval in window.intervals) / steps
        ),
        wind_speed_ms=sum(interval.wind_speed_sum_ms for interval in window.intervals) / steps,
        gradient_percent=(
            sum(interval.gradient_sum_percent for interval in window.intervals) / steps
        ),
        mass_kg=profile.mass_kg,
        drag_area=profile.drag_area,
        nominal_consumption_kwh_100km=profile.nominal_consumption_kwh_100km,
        hvac_load_kw=DEFAULT_PHYSICS.hvac_power_kw(temperature_c),
        energy_kwh=sum(interval.energy_kwh for interval in window.intervals),
    )


def _dominant_road_class(intervals: Sequence[IntervalStatistics]) -> RoadClass:
    """The road class the window spent most of its steps on.

    Ties break toward the *higher* class (motorway over primary), because a window that
    straddles a motorway junction is better described as motorway driving than as the ramp it
    briefly touched, and because the tie-break has to be deterministic.
    """
    counts: dict[RoadClass, int] = {}
    for interval in intervals:
        for road_class, steps in interval.road_class_steps:
            counts[road_class] = counts.get(road_class, 0) + steps
    if not counts:
        return RoadClass.unknown
    return max(counts, key=lambda road_class: (counts[road_class], road_class.ordinal))


def _mean_severity(intervals: Sequence[IntervalStatistics], steps: int) -> TrafficSeverity:
    """Decode the mean traffic-severity ordinal back onto the enum.

    Rounded rather than floored: a window that was half ``moderate`` and half ``high`` is closer
    to ``high`` than to ``moderate``, and flooring would bias every mixed window downwards.
    """
    total = sum(interval.traffic_severity_ordinal_sum for interval in intervals)
    ordinal = round(total / steps) if steps else TrafficSeverity.low.ordinal
    return _SEVERITY_BY_ORDINAL.get(max(1, min(4, ordinal)), TrafficSeverity.low)


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------


def windows_to_frame(windows: Iterable[TrainingWindow]) -> pl.DataFrame:
    """Build the Parquet-ready frame, asserting the feature contract on the way out.

    The assertion is cheap and worth having: it turns "the trainer crashed on a missing column"
    into "the generator refused to write a file that does not match BUILD_SPEC §10.2".
    """
    rows = [window.as_row() for window in windows]
    frame = pl.DataFrame(rows, schema=TRAINING_SCHEMA)
    missing = [name for name in MODEL_FEATURE_COLUMNS if name not in frame.columns]
    if missing:  # pragma: no cover — guarded contract, not a runtime path
        msg = f"training frame is missing model features: {', '.join(missing)}"
        raise ValueError(msg)
    return frame


def write_training_parquet(
    windows: Iterable[TrainingWindow],
    path: Path = DEFAULT_TRAINING_DATA_PATH,
) -> Path:
    """Write the training set, creating the directory if it does not exist.

    Zstandard compression: the file is committed-adjacent, read once per training run, and
    zstd at default level is both smaller and faster to decompress than snappy on this shape of
    data (many float columns, high cardinality in none of them).
    """
    frame = windows_to_frame(windows)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(path, compression="zstd")
    return path


@dataclass(frozen=True, slots=True, eq=False)
class TrainingDataResult:
    """What one ``generate-training-data`` invocation produced."""

    path: Path | None
    rows: int
    trips: int
    vehicles: int
    dropped_windows: int
    simulated_hours: float
    distance_km: float
    frame: pl.DataFrame

    def summary(self) -> str:
        """One human-readable line for the CLI to print."""
        destination = str(self.path) if self.path is not None else "(not written)"
        return (
            f"{self.rows} window(s) from {self.trips} trip(s) across {self.vehicles} vehicle(s), "
            f"{self.simulated_hours:.1f} simulated hour(s), {self.distance_km:.0f} km "
            f"-> {destination}"
        )


async def generate_training_data(
    engine: SimulationEngine,
    aggregator: WindowAggregator,
    *,
    trips: int,
    out: Path | None = DEFAULT_TRAINING_DATA_PATH,
    max_simulated_hours: float = 240.0,
) -> TrainingDataResult:
    """Run a simulation offline until ``trips`` trips have finished, and aggregate the result.

    ``engine`` and ``aggregator`` are the pair :func:`build_generator_engine` returns: the engine
    is wired to feed the aggregator, and the aggregator is what knows how many trips have closed.
    The engine's configuration must have ``realtime=False`` — otherwise the loop below would run
    in real time and a hundred simulated hours would take a hundred hours.

    ``max_simulated_hours`` is a stop that normal operation never reaches. It exists so that a
    configuration which can never complete a trip — a corridor shorter than the arrival
    tolerance, a fleet that strands itself — terminates with a diagnosable result and a warning
    instead of spinning for ever.
    """
    await engine.start()
    horizon_s = max_simulated_hours * 3600.0
    while len(aggregator.completed_trips) < trips and engine.stats.simulated_seconds < horizon_s:
        await engine.tick()
    await engine.stop()
    aggregator.flush()

    windows = aggregator.sorted_windows()
    frame = windows_to_frame(windows)
    path = write_training_parquet(windows, out) if out is not None else None
    if len(aggregator.completed_trips) < trips:
        _LOGGER.warning(
            "simulator.training_data.horizon_reached",
            requested_trips=trips,
            completed_trips=len(aggregator.completed_trips),
            simulated_hours=round(engine.stats.simulated_seconds / 3600.0, 1),
        )
    result = TrainingDataResult(
        path=path,
        rows=len(windows),
        trips=len(aggregator.completed_trips),
        vehicles=len({window.vehicle_id for window in windows}),
        dropped_windows=aggregator.dropped_windows,
        simulated_hours=engine.stats.simulated_seconds / 3600.0,
        distance_km=sum(window.distance_km for window in windows),
        frame=frame,
    )
    _LOGGER.info(
        "simulator.training_data.written",
        rows=result.rows,
        trips=result.trips,
        dropped_windows=result.dropped_windows,
        path=str(path) if path else None,
    )
    return result


def build_generator_engine(
    config: SimulationConfig,
    environments: Sequence[RouteEnvironment],
    *,
    window_seconds: float = WINDOW_SECONDS,
) -> tuple[SimulationEngine, WindowAggregator]:
    """Build an offline engine and the aggregator it feeds.

    The pairing lives here rather than in :mod:`autotwin_simulator.engine` because the
    aggregator is a training-set concern: the engine takes any
    ``Callable[[TelemetrySample], None]`` as its observer and has no business knowing what one
    does with a sample.
    """
    aggregator = WindowAggregator(window_seconds=window_seconds)
    engine = SimulationEngine(config, environments, observer=aggregator.add)
    return engine, aggregator


async def generate_seasonal_training_data(
    config: SimulationConfig,
    environments: Sequence[RouteEnvironment],
    *,
    trips: int,
    batches: int = DEFAULT_TRAINING_BATCHES,
    out: Path | None = DEFAULT_TRAINING_DATA_PATH,
    window_seconds: float = WINDOW_SECONDS,
    max_simulated_hours: float = 240.0,
) -> TrainingDataResult:
    """Generate the training set as several batches spread across a year, then write it once.

    The corridors are resolved once and shared by every batch: the synthetic terrain is a
    function of the corridor, and the synthetic weather is a function of the *instant it is
    asked about*, so the same :class:`~autotwin_simulator.environment.RouteEnvironment` answers
    correctly for January and for July. Only the clock origin changes between batches — which is
    exactly the variable that has to change (see :data:`DEFAULT_TRAINING_BATCHES`).

    Deterministic for a given ``config.seed`` and ``batches``: the anchors come from
    :func:`seasonal_start_times`, and each batch's fleet is seeded from the same master seed, so
    the same vehicles drive the same corridors in every season.
    """
    per_batch = max(1, math.ceil(trips / batches))
    windows: list[TrainingWindow] = []
    completed_trips = 0
    dropped = 0
    simulated_seconds = 0.0
    for index, start_time in enumerate(seasonal_start_times(batches)):
        batch_config = replace(
            config,
            start_time=start_time,
            realtime=False,
            name=f"{config.name} (batch {index + 1}/{batches})",
        )
        engine, aggregator = build_generator_engine(
            batch_config,
            environments,
            window_seconds=window_seconds,
        )
        batch = await generate_training_data(
            engine,
            aggregator,
            trips=per_batch,
            out=None,
            max_simulated_hours=max_simulated_hours,
        )
        windows.extend(aggregator.sorted_windows())
        completed_trips += batch.trips
        dropped += batch.dropped_windows
        simulated_seconds += engine.stats.simulated_seconds
        _LOGGER.info(
            "simulator.training_data.batch",
            batch=index + 1,
            batches=batches,
            start_time=start_time.isoformat(),
            rows=batch.rows,
            trips=batch.trips,
        )

    ordered = sorted(windows, key=lambda window: (window.window_start, window.trip_id))
    frame = windows_to_frame(ordered)
    path = write_training_parquet(ordered, out) if out is not None else None
    return TrainingDataResult(
        path=path,
        rows=len(ordered),
        trips=completed_trips,
        vehicles=len({window.vehicle_id for window in ordered}),
        dropped_windows=dropped,
        simulated_hours=simulated_seconds / 3600.0,
        distance_km=sum(window.distance_km for window in ordered),
        frame=frame,
    )
