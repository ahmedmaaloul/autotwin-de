"""Response pieces every other schema module builds on.

Three things live here, and nothing endpoint-specific ever should:

* :class:`ApiModel` — the base class, so ``from_attributes`` is configured once instead of
  being forgotten in the one schema that is then constructed from an ORM row.
* :class:`ProvenanceOut` — the wire form of the provenance block (BUILD_SPEC §3.1). It is the
  honesty rule made visible: every externally-sourced payload carries it, so the UI can always
  answer "where did this number come from?".
* :class:`CoordinateOut` / :class:`GeoPointOut` — the two ways this API emits a position, and
  a deliberate reminder that they use *opposite* axis orders.

Field names match ``apps/web/types/domain.ts`` exactly. They are snake_case because that is how
they come off the wire, and renaming them in a mapping layer would only create a place for
typos to hide.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from autotwin_contracts import Coordinate, DataOrigin, Position, SourceSystem

__all__ = [
    "ApiModel",
    "CoordinateOut",
    "GeoPointOut",
    "ProvenanceOut",
]


class ApiModel(BaseModel):
    """Base class of every API response model.

    ``from_attributes=True`` is the reason this class exists: response models are built from
    SQLAlchemy rows (``StationOut.model_validate(station)``) far more often than from dicts,
    and a schema that forgets the flag fails at runtime, in the one endpoint nobody tested with
    a real row.

    ``populate_by_name=True`` lets a field carry an alias for the wire name while handlers keep
    using the Python name — needed wherever the frontend contract and a Python keyword collide.
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class ProvenanceOut(ApiModel):
    """Where a row came from (BUILD_SPEC §3.1) — the ``Provenance`` type of the frontend.

    Validates straight off an ORM row carrying ``ProvenanceMixin``, so a detail endpoint writes
    ``provenance=ProvenanceOut.model_validate(station)`` and cannot drift from the schema.
    """

    source: SourceSystem = Field(
        ...,
        description="Publisher of the row: bundesnetzagentur, dwd, autobahn, osm, simulator, …",
        examples=["bundesnetzagentur"],
    )
    source_identifier: str | None = Field(
        default=None,
        description="Identifier of this record in the source system, when it has one.",
    )
    source_url: str | None = Field(
        default=None,
        description="Document or endpoint the row was read from.",
    )
    source_timestamp: datetime | None = Field(
        default=None,
        description="When the source itself considers the record valid (UTC).",
    )
    data_origin: DataOrigin = Field(
        ...,
        description=(
            "official, simulated or derived. Never null and never defaulted: it is what keeps "
            "simulated rows from being mistaken for official ones."
        ),
        examples=["official"],
    )
    ingestion_run_id: UUID | None = Field(
        default=None,
        description="The pipeline execution that wrote the row; null once that run is pruned.",
    )
    ingested_at: datetime | None = Field(
        default=None,
        description="When AutoTwin stored the row (UTC).",
    )


class CoordinateOut(ApiModel):
    """A WGS 84 position in human order — latitude first.

    This is the shape the frontend reads for markers, origins and destinations. GeoJSON
    geometry is the other order; see :class:`GeoPointOut`.
    """

    latitude: float = Field(
        ...,
        ge=-90.0,
        le=90.0,
        description="Latitude in WGS 84 decimal degrees (north positive).",
        examples=[50.1109],
    )
    longitude: float = Field(
        ...,
        ge=-180.0,
        le=180.0,
        description="Longitude in WGS 84 decimal degrees (east positive).",
        examples=[8.6821],
    )

    @classmethod
    def of(cls, coordinate: Coordinate) -> Self:
        """Adapt the shared :class:`~autotwin_contracts.geo.Coordinate` onto the wire type."""
        return cls(latitude=coordinate.latitude, longitude=coordinate.longitude)


class GeoPointOut(ApiModel):
    """A GeoJSON ``Point`` (RFC 7946) — what the map layers consume directly.

    ``coordinates`` is ``[longitude, latitude]``, the reverse of :class:`CoordinateOut`. That
    inversion is the most common defect in geospatial code, which is why the only supported way
    to build one of these is :meth:`from_coordinate`, whose arguments are named.
    """

    type: Literal["Point"] = Field(
        default="Point",
        description="GeoJSON geometry type; always 'Point'.",
    )
    coordinates: Position = Field(
        ...,
        description="[longitude, latitude] in WGS 84 decimal degrees, RFC 7946 §3.1.1 order.",
        examples=[(8.6821, 50.1109)],
    )

    @classmethod
    def from_coordinate(cls, *, latitude: float, longitude: float) -> Self:
        """Build the point from named latitude/longitude, so the order cannot be swapped."""
        return cls(coordinates=(longitude, latitude))
