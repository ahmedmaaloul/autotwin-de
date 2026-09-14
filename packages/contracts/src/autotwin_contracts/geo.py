"""Geospatial value objects and GeoJSON transport types.

PostGIS owns the heavy geometry work (BUILD_SPEC §3); these types exist so that everything
*around* the database — provider adapters, the simulator, the API, the generated TypeScript —
agrees on one coordinate order and one bounding-box convention.

The single most common defect in geospatial code is silently swapping latitude and longitude.
The mitigation here is that :class:`Coordinate` is never a bare tuple: conversion to a tuple
is an explicit call whose name states the order (:meth:`Coordinate.as_lonlat_tuple`), and
GeoJSON positions are documented as ``[longitude, latitude]`` per RFC 7946 §3.1.1.
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "EARTH_RADIUS_KM",
    "GERMANY_BBOX",
    "BoundingBox",
    "Coordinate",
    "GeoJSONFeature",
    "GeoJSONFeatureCollection",
    "GeoJSONGeometry",
    "GeoJSONLineString",
    "GeoJSONPoint",
    "Position",
    "haversine_km",
]

EARTH_RADIUS_KM: Final[float] = 6371.0088
"""IUGG mean Earth radius. Good to ~0.5 % for the distances this project measures."""

Position = tuple[float, float]
"""A GeoJSON position: ``(longitude, latitude)`` in WGS 84 decimal degrees, RFC 7946 order."""


class Coordinate(BaseModel):
    """A WGS 84 point on Earth, stored in human order (latitude first) and validated."""

    model_config = ConfigDict(frozen=True)

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
    def from_lonlat(cls, longitude: float, latitude: float) -> Self:
        """Build from a GeoJSON/PostGIS position, where longitude comes first."""
        return cls(latitude=latitude, longitude=longitude)

    def as_lonlat_tuple(self) -> Position:
        """``(longitude, latitude)`` — GeoJSON, PostGIS ``ST_MakePoint`` and MapLibre order."""
        return (self.longitude, self.latitude)

    def as_latlon_tuple(self) -> tuple[float, float]:
        """``(latitude, longitude)`` — the order used by most routing and weather APIs."""
        return (self.latitude, self.longitude)

    def as_geojson(self) -> GeoJSONPoint:
        """Render as an RFC 7946 Point geometry."""
        return GeoJSONPoint(coordinates=self.as_lonlat_tuple())


class BoundingBox(BaseModel):
    """An axis-aligned WGS 84 bounding box in ``west, south, east, north`` order.

    That order is the one used by the API's ``?bbox=`` query parameter, by GeoJSON's ``bbox``
    member and by the Autobahn and Overpass APIs, so it is the one the whole project speaks.
    Boxes crossing the antimeridian are rejected; AutoTwin's area of interest is Germany.
    """

    model_config = ConfigDict(frozen=True)

    west: float = Field(..., ge=-180.0, le=180.0, description="Western longitude bound.")
    south: float = Field(..., ge=-90.0, le=90.0, description="Southern latitude bound.")
    east: float = Field(..., ge=-180.0, le=180.0, description="Eastern longitude bound.")
    north: float = Field(..., ge=-90.0, le=90.0, description="Northern latitude bound.")

    @model_validator(mode="after")
    def _check_ordering(self) -> Self:
        """Reject inverted boxes early — they otherwise yield silently empty query results."""
        if self.west >= self.east:
            msg = f"west ({self.west}) must be smaller than east ({self.east})"
            raise ValueError(msg)
        if self.south >= self.north:
            msg = f"south ({self.south}) must be smaller than north ({self.north})"
            raise ValueError(msg)
        return self

    @classmethod
    def parse(cls, raw: str) -> Self:
        """Parse the ``"west,south,east,north"`` query-parameter form.

        Raises ``ValueError`` with a message meant to be shown to an API client, since this is
        reached straight from user input.
        """
        parts = [part.strip() for part in raw.split(",")]
        if len(parts) != 4:
            msg = f"bbox must have four comma-separated values (west,south,east,north), got {raw!r}"
            raise ValueError(msg)
        try:
            west, south, east, north = (float(part) for part in parts)
        except ValueError as exc:
            msg = f"bbox values must be numbers, got {raw!r}"
            raise ValueError(msg) from exc
        return cls(west=west, south=south, east=east, north=north)

    def contains(self, coordinate: Coordinate) -> bool:
        """Whether the point lies inside the box, bounds inclusive."""
        return (
            self.west <= coordinate.longitude <= self.east
            and self.south <= coordinate.latitude <= self.north
        )

    def as_tuple(self) -> tuple[float, float, float, float]:
        """``(west, south, east, north)`` — the GeoJSON ``bbox`` member order."""
        return (self.west, self.south, self.east, self.north)

    def as_param(self) -> str:
        """The ``"west,south,east,north"`` string form used in query parameters."""
        return f"{self.west},{self.south},{self.east},{self.north}"

    @property
    def center(self) -> Coordinate:
        """Geometric centre, e.g. for a single-point weather lookup covering the box."""
        return Coordinate(
            latitude=(self.south + self.north) / 2.0,
            longitude=(self.west + self.east) / 2.0,
        )


GERMANY_BBOX: Final[BoundingBox] = BoundingBox(west=5.8, south=47.2, east=15.1, north=55.1)
"""Germany's extent (BUILD_SPEC §6): the ``within_germany_bbox`` quality rule's reference."""


class GeoJSONPoint(BaseModel):
    """RFC 7946 Point geometry."""

    model_config = ConfigDict(frozen=True)

    type: Literal["Point"] = "Point"
    coordinates: Position = Field(
        ...,
        description="Position as [longitude, latitude] in WGS 84 decimal degrees.",
    )

    def as_coordinate(self) -> Coordinate:
        """Convert back to the latitude-first domain type."""
        longitude, latitude = self.coordinates
        return Coordinate(latitude=latitude, longitude=longitude)


class GeoJSONLineString(BaseModel):
    """RFC 7946 LineString geometry — route and traffic-event geometries travel as this."""

    model_config = ConfigDict(frozen=True)

    type: Literal["LineString"] = "LineString"
    coordinates: list[Position] = Field(
        ...,
        min_length=2,
        description="At least two [longitude, latitude] positions in WGS 84 decimal degrees.",
    )

    @classmethod
    def from_coordinates(cls, coordinates: list[Coordinate]) -> Self:
        """Build from domain coordinates, flipping each into GeoJSON position order."""
        return cls(coordinates=[point.as_lonlat_tuple() for point in coordinates])

    def as_coordinates(self) -> list[Coordinate]:
        """Convert back to latitude-first domain coordinates."""
        return [Coordinate.from_lonlat(lon, lat) for lon, lat in self.coordinates]


GeoJSONGeometry = Annotated[GeoJSONPoint | GeoJSONLineString, Field(discriminator="type")]
"""The geometry types AutoTwin transports; discriminated on the RFC 7946 ``type`` member."""


class GeoJSONFeature(BaseModel):
    """RFC 7946 Feature.

    ``properties`` is an open mapping on purpose: each endpoint decides what a feature carries
    (station attributes, traffic-event attributes, …) and the frontend map layers read those
    keys. Constraining it here would force a contract change for every new map layer.
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["Feature"] = "Feature"
    geometry: GeoJSONGeometry | None = Field(
        ...,
        description="The feature's geometry, or null when it is not locatable.",
    )
    properties: dict[str, Any] = Field(
        default_factory=dict,
        description="Feature attributes rendered by the map layer that consumes them.",
    )
    id: str | None = Field(
        default=None,
        description="Stable feature identifier, used by MapLibre for feature-state hover.",
    )


class GeoJSONFeatureCollection(BaseModel):
    """RFC 7946 FeatureCollection — the payload of every ``*/geojson`` endpoint."""

    model_config = ConfigDict(frozen=True)

    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[GeoJSONFeature] = Field(
        default_factory=list,
        description="The features in this collection.",
    )
    bbox: tuple[float, float, float, float] | None = Field(
        default=None,
        description="Optional extent as [west, south, east, north], for initial map fitting.",
    )


def haversine_km(a: Coordinate, b: Coordinate) -> float:
    """Great-circle distance between two coordinates in kilometres.

    Used for corridor buffers, charging-gap measurements and detour estimates. A spherical
    model is deliberate: over German distances the error against WGS 84 ellipsoidal geodesics
    stays well under 0.5 %, far below the uncertainty of the underlying road network data, and
    it keeps this package free of a geodesy dependency. Where exactness matters — corridor
    queries over real geometries — PostGIS ``geography`` does the work instead.
    """
    lat1 = math.radians(a.latitude)
    lat2 = math.radians(b.latitude)
    delta_lat = lat2 - lat1
    delta_lon = math.radians(b.longitude - a.longitude)
    inner = (
        math.sin(delta_lat / 2.0) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_KM * math.asin(math.sqrt(inner))
