"""Route segmentation — turning an OSRM geometry into ``route_segments`` (BUILD_SPEC §3.2).

A route is analysed segment by segment: each segment gets its own road class, speed limit,
temperature, traffic severity and predicted consumption, and the flagship
``POST /api/v1/routes/analyze`` payload (§7.3) is essentially a list of them. This module is
where a continuous polyline becomes that list.

Segments are cut at **equal offsets** rather than at OSRM step boundaries. Steps vary from
20 m (a roundabout exit) to 40 km (an Autobahn stretch), and a segment table with that spread
produces meaningless per-segment statistics: a 20 m segment's kWh/100 km is dominated by
rounding, while a 40 km one averages away every gradient and every traffic jam. Equal offsets
also make ``start_offset_m`` monotone and directly comparable between routes, which is what the
charging-gap analysis (§7.2) needs.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from autotwin_contracts import Coordinate, GeoJSONLineString
from autotwin_core.geo.distance import (
    METRES_PER_DEGREE_LATITUDE,
    cumulative_distances_m,
    distance_to_polyline_m,
    point_at_offset,
)

__all__ = [
    "DEFAULT_MIN_SEGMENT_LENGTH_M",
    "DEFAULT_TARGET_SEGMENT_LENGTH_M",
    "PolylineSegment",
    "SegmentMatch",
    "match_points_to_segments",
    "nearest_segment_index",
    "segment_polyline",
]

DEFAULT_TARGET_SEGMENT_LENGTH_M: Final[float] = 5_000.0
"""5 km: about three minutes of Autobahn driving — fine enough to resolve a weather front or a
roadworks zone, coarse enough that a 600 km corridor stays under ~120 rows."""

DEFAULT_MIN_SEGMENT_LENGTH_M: Final[float] = 1_500.0
"""Below this a segment's average speed and gradient are dominated by geometry noise."""

_LENGTH_TOLERANCE_M: Final[float] = 1.0
"""Segmentation must conserve route length to within a metre; more means a bug in the cuts."""


@dataclass(frozen=True, slots=True)
class PolylineSegment:
    """One analysis chunk of a route, ready to become a ``route_segments`` row.

    Frozen: a segment is a geometric fact derived from the route. Everything an analysis adds
    later (temperature, traffic severity, predicted kWh) is stored on the database row or on
    the API DTO, never mutated back into the geometry.
    """

    ordinal: int
    """0-based position along the route; the ``(route_id, ordinal)`` unique key of §3.2."""

    coordinates: tuple[Coordinate, ...]
    """Geometry of this chunk, starting and ending exactly on the cut offsets."""

    start_offset_m: float
    """Distance from the route origin to the start of this segment, in metres."""

    length_m: float
    """Length of this segment in metres — the segment's ``distance_m`` column."""

    midpoint: Coordinate
    """Point at half the segment's length; where weather and elevation are sampled."""

    @property
    def end_offset_m(self) -> float:
        """Distance from the route origin to the end of this segment, in metres."""
        return self.start_offset_m + self.length_m

    @property
    def start(self) -> Coordinate:
        """First vertex of the segment."""
        return self.coordinates[0]

    @property
    def end(self) -> Coordinate:
        """Last vertex of the segment."""
        return self.coordinates[-1]

    def as_linestring(self) -> GeoJSONLineString:
        """Render the geometry as RFC 7946, for the API and the map layer."""
        return GeoJSONLineString.from_coordinates(list(self.coordinates))


@dataclass(frozen=True, slots=True)
class SegmentMatch:
    """The segment a point was attached to, and how far away it was.

    Carrying the distance rather than just the index matters for honesty: a traffic event
    1.8 km from the corridor is a much weaker signal than one on the carriageway, and the
    route analysis weights it accordingly instead of treating every match as certain.
    """

    point_index: int
    """Index of the point in the input sequence."""

    segment_index: int
    """Index into the segment list; equal to that segment's ``ordinal``."""

    distance_m: float
    """Shortest distance from the point to the segment geometry, in metres."""


def segment_polyline(
    coordinates: Sequence[Coordinate],
    target_length_m: float = DEFAULT_TARGET_SEGMENT_LENGTH_M,
    min_length_m: float = DEFAULT_MIN_SEGMENT_LENGTH_M,
) -> list[PolylineSegment]:
    """Cut a route geometry into equal-length analysis segments.

    The segment count is ``round(total / target)``, reduced until every segment reaches
    ``min_length_m``, and never below one. Consequences worth relying on:

    * A route shorter than the target yields exactly **one** segment covering all of it.
    * Segment lengths sum to the route length exactly — the cuts are offsets along one
      cumulative-distance array, so no length is created or lost. The invariant is asserted.
    * No segment is ever zero-length, so ``kWh / distance`` is always defined downstream.

    Raises:
        ValueError: for fewer than two vertices, non-positive parameters, or a degenerate
            polyline whose total length is zero (every vertex identical) — a zero-length route
            is a routing-provider defect, and silently emitting an empty segment list would
            hide it behind an empty analysis page.
    """
    if len(coordinates) < 2:
        msg = f"segment_polyline needs at least two coordinates, got {len(coordinates)}"
        raise ValueError(msg)
    if target_length_m <= 0.0 or min_length_m <= 0.0:
        msg = (
            f"segment lengths must be positive, got target={target_length_m!r} min={min_length_m!r}"
        )
        raise ValueError(msg)

    cumulative = cumulative_distances_m(coordinates)
    total_m = cumulative[-1]
    if total_m <= 0.0:
        msg = "cannot segment a polyline of zero length"
        raise ValueError(msg)

    # Aim for the target length, then cap the count so no segment falls below the minimum.
    # Capping arithmetically rather than by decrementing keeps this O(1) for a route that is
    # thousands of targets long but only a handful of minimums.
    count = max(1, min(round(total_m / target_length_m), int(total_m // min_length_m)))

    segments: list[PolylineSegment] = []
    for ordinal in range(count):
        start_m = total_m * ordinal / count
        end_m = total_m * (ordinal + 1) / count if ordinal < count - 1 else total_m
        segments.append(
            _build_segment(
                ordinal=ordinal,
                coordinates=coordinates,
                cumulative=cumulative,
                start_m=start_m,
                end_m=end_m,
            )
        )

    covered_m = sum(segment.length_m for segment in segments)
    assert abs(covered_m - total_m) <= _LENGTH_TOLERANCE_M, (
        f"segmentation lost {total_m - covered_m:.3f} m of a {total_m:.1f} m route"
    )
    return segments


def nearest_segment_index(segments: Sequence[PolylineSegment], point: Coordinate) -> int:
    """Index of the segment whose geometry lies closest to ``point``.

    Ties go to the earlier segment, which keeps the mapping stable for a point sitting exactly
    on a cut — otherwise the same traffic event could jump between two segments between runs.
    """
    if not segments:
        msg = "nearest_segment_index needs at least one segment"
        raise ValueError(msg)
    best_index = 0
    best_distance = distance_to_polyline_m(point, segments[0].coordinates)
    for index in range(1, len(segments)):
        distance = distance_to_polyline_m(point, segments[index].coordinates)
        if distance < best_distance:
            best_distance = distance
            best_index = index
    return best_index


def match_points_to_segments(
    segments: Sequence[PolylineSegment],
    points: Sequence[Coordinate],
    max_distance_m: float = 2_000.0,
) -> list[SegmentMatch]:
    """Attach points to the nearest segment, dropping those farther than ``max_distance_m``.

    This is how traffic events and weather observations reach ``route_segments``. The cut-off
    is essential: the Autobahn feed covers the whole federal network, and without a corridor
    limit a jam on the A9 near Leipzig would be attached to a route running down the A5.

    Unmatched points are simply absent from the result — the caller can compare
    :attr:`SegmentMatch.point_index` against its input to see which observations found no
    corridor, which is itself a useful coverage signal. The result is ordered by point index.
    """
    if max_distance_m <= 0.0:
        msg = f"max_distance_m must be positive, got {max_distance_m!r}"
        raise ValueError(msg)
    if not segments or not points:
        return []

    bounds = [_padded_bounds(segment, max_distance_m) for segment in segments]
    matches: list[SegmentMatch] = []
    for point_index, point in enumerate(points):
        best_index = -1
        best_distance = math.inf
        for segment_index, segment in enumerate(segments):
            if not _within_bounds(point, bounds[segment_index]):
                continue
            distance = distance_to_polyline_m(point, segment.coordinates)
            if distance < best_distance:
                best_distance = distance
                best_index = segment_index
        if best_index >= 0 and best_distance <= max_distance_m:
            matches.append(
                SegmentMatch(
                    point_index=point_index,
                    segment_index=best_index,
                    distance_m=best_distance,
                )
            )
    return matches


def _build_segment(
    *,
    ordinal: int,
    coordinates: Sequence[Coordinate],
    cumulative: Sequence[float],
    start_m: float,
    end_m: float,
) -> PolylineSegment:
    """Materialise one segment between two offsets along the parent polyline."""
    first_interior = bisect_right(cumulative, start_m)
    last_interior = bisect_left(cumulative, end_m)
    geometry: list[Coordinate] = [
        point_at_offset(coordinates, start_m, cumulative_m=cumulative),
        *coordinates[first_interior:last_interior],
        point_at_offset(coordinates, end_m, cumulative_m=cumulative),
    ]
    return PolylineSegment(
        ordinal=ordinal,
        coordinates=tuple(geometry),
        start_offset_m=start_m,
        length_m=end_m - start_m,
        midpoint=point_at_offset(coordinates, (start_m + end_m) / 2.0, cumulative_m=cumulative),
    )


def _padded_bounds(
    segment: PolylineSegment,
    padding_m: float,
) -> tuple[float, float, float, float]:
    """Latitude/longitude bounds of a segment, grown by ``padding_m``.

    A cheap rejection test before the exact point-to-polyline distance: matching 20 000
    charging stations against 120 segments is 2.4 million geometry comparisons otherwise.

    Longitude padding is divided by ``cos(latitude)`` because a degree of longitude is shorter
    than a degree of latitude — at 51° N by a third. Using the latitude *farthest* from the
    equator makes the box the widest of the segment's extent, so the filter can only ever
    admit extra candidates, never drop a true one.
    """
    latitudes = [point.latitude for point in segment.coordinates]
    longitudes = [point.longitude for point in segment.coordinates]
    latitude_padding_deg = padding_m / METRES_PER_DEGREE_LATITUDE
    extreme_latitude = max(abs(min(latitudes)), abs(max(latitudes)))
    shrink = max(math.cos(math.radians(extreme_latitude)), 0.01)
    longitude_padding_deg = latitude_padding_deg / shrink
    return (
        min(latitudes) - latitude_padding_deg,
        max(latitudes) + latitude_padding_deg,
        min(longitudes) - longitude_padding_deg,
        max(longitudes) + longitude_padding_deg,
    )


def _within_bounds(point: Coordinate, bounds: tuple[float, float, float, float]) -> bool:
    """Whether a point falls inside padded ``(min_lat, max_lat, min_lon, max_lon)`` bounds."""
    min_lat, max_lat, min_lon, max_lon = bounds
    return min_lat <= point.latitude <= max_lat and min_lon <= point.longitude <= max_lon
