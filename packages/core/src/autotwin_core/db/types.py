"""Column types and geometry conversions shared by every mapped class.

Three concerns live here so that ``models.py`` stays a readable description of the schema:

* **PostGIS column types.** BUILD_SPEC §3 fixes the choice — points are ``geography(Point,4326)``
  so ``ST_Distance`` answers in metres, lines are ``geometry(LineString,4326)`` because
  rendering and simplification are cheaper on the planar type and a length in metres only needs
  a ``::geography`` cast at the call site.
* **Shared enum types.** One ``sa.Enum`` instance per domain enum, reused by every table that
  needs it, so PostgreSQL ends up with exactly one ``CREATE TYPE`` per enum and Alembic does not
  try to create the same type twice.
* **WKB conversion.** Nothing outside this module should know that PostGIS hands SQLAlchemy a
  ``WKBElement``; callers speak :class:`~autotwin_contracts.geo.Coordinate`.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Final

import sqlalchemy as sa
from geoalchemy2 import Geography, Geometry
from geoalchemy2.elements import WKBElement
from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import LineString, Point

from autotwin_contracts.enums import (
    Bundesland,
    ChargingCategory,
    ConnectorType,
    CurrentType,
    DataOrigin,
    EnergyIntensity,
    IngestionOutcome,
    ProviderMode,
    RoadClass,
    SimulationState,
    SourceSystem,
    TrafficEventType,
    TrafficSeverity,
    VehicleClass,
    VehicleState,
    WeatherCondition,
)
from autotwin_contracts.geo import Coordinate
from autotwin_contracts.temporal import utc_now

__all__ = [
    "BUNDESLAND_ENUM",
    "CHARGING_CATEGORY_ENUM",
    "CONNECTOR_TYPE_ENUM",
    "CURRENT_TYPE_ENUM",
    "DATA_ORIGIN_ENUM",
    "ENERGY_INTENSITY_ENUM",
    "INGESTION_OUTCOME_ENUM",
    "PROVIDER_MODE_ENUM",
    "ROAD_CLASS_ENUM",
    "SIMULATION_STATE_ENUM",
    "SOURCE_SYSTEM_ENUM",
    "SRID_WGS84",
    "TRAFFIC_EVENT_TYPE_ENUM",
    "TRAFFIC_SEVERITY_ENUM",
    "VEHICLE_CLASS_ENUM",
    "VEHICLE_STATE_ENUM",
    "WEATHER_CONDITION_ENUM",
    "JSONBDict",
    "LineStringGeometry",
    "PointGeography",
    "from_wkb_linestring",
    "from_wkb_point",
    "to_shape_linestring",
    "to_shape_point",
    "utc_now",
]

SRID_WGS84: Final[int] = 4326
"""The only spatial reference AutoTwin stores: WGS 84 lon/lat, as every source publishes."""

JSONBDict = dict[str, Any]
"""Python side of a ``JSONB`` column holding an object (``raw``, ``metrics``, ``config``, …).

A plain alias rather than a PEP 695 ``type`` statement: SQLAlchemy evaluates the string
annotations produced by ``from __future__ import annotations`` when it maps a class, and a
plain alias resolves to ``dict[str, Any]`` with no indirection to unwrap.
"""

PointGeography = Geography(
    geometry_type="POINT",
    srid=SRID_WGS84,
    spatial_index=False,
)
"""``geography(Point,4326)``.

``spatial_index=False`` everywhere: GeoAlchemy2 would otherwise emit an implicitly named GiST
index per column, and BUILD_SPEC §3.2 names each index explicitly so that Alembic autogenerate
and the hand-written migration agree. The indexes are declared in each model's
``__table_args__``.
"""

LineStringGeometry = Geometry(
    geometry_type="LINESTRING",
    srid=SRID_WGS84,
    spatial_index=False,
)
"""``geometry(LineString,4326)`` — route corridors and the affected stretch of a traffic event."""


def _pg_enum(enum_class: type[Enum], name: str) -> sa.Enum:
    """Build the shared ``sa.Enum`` for a domain enum, stored by **value**.

    SQLAlchemy persists ``Enum.name`` by default. AutoTwin persists ``Enum.value``, because the
    same lowercase strings are the wire format of the API and the frontend (BUILD_SPEC §2); a
    database that disagreed with the JSON would be a trap waiting for the first enum whose name
    and value are not identical.

    One instance per enum is shared across all tables that reference it — that is what keeps
    PostgreSQL to a single ``CREATE TYPE`` and makes ``metadata.create_all`` idempotent.
    """
    return sa.Enum(
        enum_class,
        name=name,
        native_enum=True,
        values_callable=lambda enum: [member.value for member in enum],
    )


SOURCE_SYSTEM_ENUM: Final[sa.Enum] = _pg_enum(SourceSystem, "source_system")
DATA_ORIGIN_ENUM: Final[sa.Enum] = _pg_enum(DataOrigin, "data_origin")
INGESTION_OUTCOME_ENUM: Final[sa.Enum] = _pg_enum(IngestionOutcome, "ingestion_outcome")
PROVIDER_MODE_ENUM: Final[sa.Enum] = _pg_enum(ProviderMode, "provider_mode")
CHARGING_CATEGORY_ENUM: Final[sa.Enum] = _pg_enum(ChargingCategory, "charging_category")
CONNECTOR_TYPE_ENUM: Final[sa.Enum] = _pg_enum(ConnectorType, "connector_type")
CURRENT_TYPE_ENUM: Final[sa.Enum] = _pg_enum(CurrentType, "current_type")
BUNDESLAND_ENUM: Final[sa.Enum] = _pg_enum(Bundesland, "bundesland")
WEATHER_CONDITION_ENUM: Final[sa.Enum] = _pg_enum(WeatherCondition, "weather_condition")
TRAFFIC_EVENT_TYPE_ENUM: Final[sa.Enum] = _pg_enum(TrafficEventType, "traffic_event_type")
TRAFFIC_SEVERITY_ENUM: Final[sa.Enum] = _pg_enum(TrafficSeverity, "traffic_severity")
ROAD_CLASS_ENUM: Final[sa.Enum] = _pg_enum(RoadClass, "road_class")
ENERGY_INTENSITY_ENUM: Final[sa.Enum] = _pg_enum(EnergyIntensity, "energy_intensity")
VEHICLE_CLASS_ENUM: Final[sa.Enum] = _pg_enum(VehicleClass, "vehicle_class")
SIMULATION_STATE_ENUM: Final[sa.Enum] = _pg_enum(SimulationState, "simulation_state")
VEHICLE_STATE_ENUM: Final[sa.Enum] = _pg_enum(VehicleState, "vehicle_state")


def to_shape_point(latitude: float, longitude: float) -> WKBElement:
    """Build the value to store in a ``geography(Point,4326)`` column.

    Arguments are latitude-first to match :class:`~autotwin_contracts.geo.Coordinate` and the
    way humans quote positions; the axis flip into PostGIS's ``(x=longitude, y=latitude)`` order
    happens here, once, instead of at every insert site. Silent lat/lon swaps are the classic
    geospatial defect, and this function plus ``Coordinate`` are the only two places in the
    codebase where the order is written down.
    """
    return from_shape(Point(longitude, latitude), srid=SRID_WGS84)


def to_shape_linestring(coordinates: list[Coordinate]) -> WKBElement:
    """Build the value to store in a ``geometry(LineString,4326)`` column.

    Raises ``ValueError`` for fewer than two positions: PostGIS accepts a one-point LineString
    in some code paths and then fails much later inside ``ST_Length`` or ``ST_LineLocatePoint``,
    where the cause is far harder to see.
    """
    if len(coordinates) < 2:
        msg = f"a LineString needs at least two coordinates, got {len(coordinates)}"
        raise ValueError(msg)
    return from_shape(
        LineString([coordinate.as_lonlat_tuple() for coordinate in coordinates]),
        srid=SRID_WGS84,
    )


def from_wkb_point(value: WKBElement) -> Coordinate:
    """Convert a loaded point column back into a :class:`Coordinate`."""
    shape = to_shape(value)
    if not isinstance(shape, Point):
        msg = f"expected a Point geometry, got {shape.geom_type}"
        raise ValueError(msg)
    return Coordinate.from_lonlat(longitude=shape.x, latitude=shape.y)


def from_wkb_linestring(value: WKBElement) -> list[Coordinate]:
    """Convert a loaded line column back into coordinates in travel order."""
    shape = to_shape(value)
    if not isinstance(shape, LineString):
        msg = f"expected a LineString geometry, got {shape.geom_type}"
        raise ValueError(msg)
    return [
        Coordinate.from_lonlat(longitude=longitude, latitude=latitude)
        for longitude, latitude in shape.coords
    ]
