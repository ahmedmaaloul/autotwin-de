"""Geometry helpers for routes and corridors — distances, densification, segmentation.

Everything here is a pure function or a frozen value object over
:class:`~autotwin_contracts.Coordinate` sequences, deliberately free of any database
dependency so that route analysis, the simulator and the charging optimiser can share one
implementation::

    from autotwin_core.geo import segment_polyline, simplify

    segments = segment_polyline(route.geometry, target_length_m=5_000.0)
    display_geometry = simplify(route.geometry, tolerance_m=10.0)
"""

from __future__ import annotations

from autotwin_core.geo.distance import (
    METRES_PER_DEGREE_LATITUDE,
    bearing_deg,
    cross_track_distance_m,
    cumulative_distances_m,
    densify,
    distance_to_polyline_m,
    haversine_m,
    interpolate,
    point_at_offset,
    simplify,
    total_length_m,
)
from autotwin_core.geo.segmentation import (
    DEFAULT_MIN_SEGMENT_LENGTH_M,
    DEFAULT_TARGET_SEGMENT_LENGTH_M,
    PolylineSegment,
    SegmentMatch,
    match_points_to_segments,
    nearest_segment_index,
    segment_polyline,
)

__all__ = [
    "DEFAULT_MIN_SEGMENT_LENGTH_M",
    "DEFAULT_TARGET_SEGMENT_LENGTH_M",
    "METRES_PER_DEGREE_LATITUDE",
    "PolylineSegment",
    "SegmentMatch",
    "bearing_deg",
    "cross_track_distance_m",
    "cumulative_distances_m",
    "densify",
    "distance_to_polyline_m",
    "haversine_m",
    "interpolate",
    "match_points_to_segments",
    "nearest_segment_index",
    "point_at_offset",
    "segment_polyline",
    "simplify",
    "total_length_m",
]
