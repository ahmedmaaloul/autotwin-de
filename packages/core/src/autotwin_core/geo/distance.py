"""Polyline geometry in metres — the maths behind route segmentation and corridor analysis.

Pure functions over :class:`~autotwin_contracts.Coordinate` sequences. Nothing here touches the
database: PostGIS owns the geometry *columns* (BUILD_SPEC §3), but an OSRM response has to be
cut into segments, densified and simplified long before it is persisted, and doing that with a
round trip to Postgres per route would be absurd.

Two modelling choices, stated once so every caller can rely on them:

* **Distances** use the spherical haversine of :func:`autotwin_contracts.haversine_km`. Over
  German distances the error against WGS 84 geodesics stays well under 0.5 % — far below the
  uncertainty of the road geometry itself.
* **Planar operations** (point-to-segment distance, Douglas-Peucker) project onto a local
  equirectangular plane scaled at the reference latitude, so their tolerances are genuinely
  metric. Working in raw degrees would make an east-west tolerance ~1.5x tighter than a
  north-south one at 51° N, which is how "simplify by 10 m" quietly becomes anisotropic.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Sequence
from typing import Final

from autotwin_contracts import EARTH_RADIUS_KM, Coordinate, haversine_km

__all__ = [
    "METRES_PER_DEGREE_LATITUDE",
    "bearing_deg",
    "cross_track_distance_m",
    "cumulative_distances_m",
    "densify",
    "distance_to_polyline_m",
    "haversine_m",
    "interpolate",
    "point_at_offset",
    "simplify",
    "total_length_m",
]

METRES_PER_DEGREE_LATITUDE: Final[float] = EARTH_RADIUS_KM * 1000.0 * math.pi / 180.0
"""Metres per degree of latitude on the sphere used by :func:`haversine_m` (~111.2 km)."""


def haversine_m(a: Coordinate, b: Coordinate) -> float:
    """Great-circle distance between two coordinates in metres.

    Metres rather than kilometres because every distance the database, the segmentation and
    the energy model deal with is metric-base (BUILD_SPEC §14); kilometres appear only where a
    human reads them.
    """
    return haversine_km(a, b) * 1000.0


def total_length_m(coordinates: Sequence[Coordinate]) -> float:
    """Length of the polyline in metres; ``0.0`` for fewer than two points."""
    if len(coordinates) < 2:
        return 0.0
    return sum(
        haversine_m(coordinates[index], coordinates[index + 1])
        for index in range(len(coordinates) - 1)
    )


def cumulative_distances_m(coordinates: Sequence[Coordinate]) -> list[float]:
    """Distance from the first vertex to each vertex, in metres.

    The result has exactly one entry per input vertex and starts at ``0.0``, which makes it
    directly indexable alongside the geometry — the property segmentation and offset lookups
    depend on.
    """
    if not coordinates:
        msg = "cumulative_distances_m needs at least one coordinate"
        raise ValueError(msg)
    cumulative = [0.0]
    running = 0.0
    for index in range(len(coordinates) - 1):
        running += haversine_m(coordinates[index], coordinates[index + 1])
        cumulative.append(running)
    return cumulative


def interpolate(a: Coordinate, b: Coordinate, fraction: float) -> Coordinate:
    """Point a given fraction of the way from ``a`` to ``b``, clamped to ``[0, 1]``.

    Linear in latitude/longitude rather than along the great circle. Callers interpolate
    *within* one polyline leg — a few hundred metres at most in an OSRM geometry — where the
    two differ by well under a millimetre.
    """
    ratio = min(1.0, max(0.0, fraction))
    return Coordinate(
        latitude=a.latitude + (b.latitude - a.latitude) * ratio,
        longitude=a.longitude + (b.longitude - a.longitude) * ratio,
    )


def point_at_offset(
    coordinates: Sequence[Coordinate],
    offset_m: float,
    *,
    cumulative_m: Sequence[float] | None = None,
) -> Coordinate:
    """Coordinate at ``offset_m`` metres along the polyline.

    Offsets before the start or past the end clamp to the endpoints instead of raising: this
    is called with accumulated floating-point offsets during segmentation, where being a
    micrometre past the end is arithmetic noise, not an error.

    Pass ``cumulative_m`` from :func:`cumulative_distances_m` when looking up many offsets on
    the same polyline. Without it every call rebuilds the prefix sums, which turns
    segmentation of a several-thousand-vertex route into quadratic work.
    """
    if not coordinates:
        msg = "point_at_offset needs at least one coordinate"
        raise ValueError(msg)
    if len(coordinates) == 1 or offset_m <= 0.0:
        return coordinates[0]
    cumulative = cumulative_m if cumulative_m is not None else cumulative_distances_m(coordinates)
    if len(cumulative) != len(coordinates):
        msg = (
            f"cumulative_m has {len(cumulative)} entries but the polyline has "
            f"{len(coordinates)} vertices"
        )
        raise ValueError(msg)
    if offset_m >= cumulative[-1]:
        return coordinates[-1]
    index = bisect_right(cumulative, offset_m)
    previous = cumulative[index - 1]
    leg_length = cumulative[index] - previous
    fraction = (offset_m - previous) / leg_length
    return interpolate(coordinates[index - 1], coordinates[index], fraction)


def bearing_deg(a: Coordinate, b: Coordinate) -> float:
    """Initial great-circle bearing from ``a`` to ``b`` in degrees, ``0`` = north, clockwise.

    The simulator writes this into ``telemetry.heading_deg`` so the map can orient a vehicle
    marker; identical points yield ``0.0`` rather than an undefined value.
    """
    lat1 = math.radians(a.latitude)
    lat2 = math.radians(b.latitude)
    delta_lon = math.radians(b.longitude - a.longitude)
    y = math.sin(delta_lon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lon)
    if x == 0.0 and y == 0.0:
        return 0.0
    return math.degrees(math.atan2(y, x)) % 360.0


def densify(coordinates: Sequence[Coordinate], max_spacing_m: float) -> list[Coordinate]:
    """Insert intermediate vertices so that no leg is longer than ``max_spacing_m``.

    OSRM returns motorway stretches as a single leg tens of kilometres long. Sampling weather,
    traffic and elevation along such a geometry needs vertices at a known resolution, and the
    energy model integrates per leg — a 30 km leg would be modelled at one constant speed.
    All original vertices are preserved; only new ones are added.
    """
    if max_spacing_m <= 0.0:
        msg = f"max_spacing_m must be positive, got {max_spacing_m!r}"
        raise ValueError(msg)
    if len(coordinates) < 2:
        return list(coordinates)

    densified: list[Coordinate] = [coordinates[0]]
    for index in range(len(coordinates) - 1):
        start = coordinates[index]
        end = coordinates[index + 1]
        leg_length = haversine_m(start, end)
        steps = math.ceil(leg_length / max_spacing_m) if leg_length > max_spacing_m else 1
        for step in range(1, steps):
            densified.append(interpolate(start, end, step / steps))
        densified.append(end)
    return densified


def cross_track_distance_m(point: Coordinate, start: Coordinate, end: Coordinate) -> float:
    """Shortest distance in metres from ``point`` to the *segment* ``start``-``end``.

    Segment, not infinite line: a traffic event 40 km beyond the end of a route segment must
    report 40 km, not its perpendicular offset from the segment's bearing. A degenerate
    segment (``start == end``) falls back to the point-to-point distance.
    """
    scale_x = math.cos(math.radians(start.latitude)) * METRES_PER_DEGREE_LATITUDE
    px = (point.longitude - start.longitude) * scale_x
    py = (point.latitude - start.latitude) * METRES_PER_DEGREE_LATITUDE
    bx = (end.longitude - start.longitude) * scale_x
    by = (end.latitude - start.latitude) * METRES_PER_DEGREE_LATITUDE
    length_sq = bx * bx + by * by
    if length_sq == 0.0:
        return math.hypot(px, py)
    ratio = min(1.0, max(0.0, (px * bx + py * by) / length_sq))
    return math.hypot(px - ratio * bx, py - ratio * by)


def distance_to_polyline_m(point: Coordinate, coordinates: Sequence[Coordinate]) -> float:
    """Shortest distance in metres from ``point`` to any part of the polyline."""
    if not coordinates:
        msg = "distance_to_polyline_m needs at least one coordinate"
        raise ValueError(msg)
    if len(coordinates) == 1:
        return haversine_m(point, coordinates[0])
    return min(
        cross_track_distance_m(point, coordinates[index], coordinates[index + 1])
        for index in range(len(coordinates) - 1)
    )


def simplify(
    coordinates: Sequence[Coordinate],
    tolerance_m: float,
    *,
    prefer_shapely: bool = True,
) -> list[Coordinate]:
    """Douglas-Peucker simplification with a tolerance in metres.

    Route geometries go to the browser as GeoJSON; an unsimplified Frankfurt-Stuttgart OSRM
    response is a few thousand vertices whose sub-metre detail no map at corridor zoom can
    show. A 10 m tolerance typically removes 80 % of them with no visible change.

    Shapely does the work when it is importable — it is a C implementation and this runs per
    API request — and the pure-Python fallback produces the identical result. Shapely is a
    declared dependency of ``autotwin_core``, so the fallback is not a compromise but a
    reference implementation: it is what makes the two paths comparable in a test, and it
    keeps this module usable in a minimal environment. Either way the output is a
    *subsequence of the input*: simplification never invents a vertex, so a simplified
    geometry can still be matched back to the original.
    """
    if tolerance_m <= 0.0 or len(coordinates) <= 2:
        return list(coordinates)

    projected = _project_local(coordinates)
    indices = _simplify_indices_shapely(projected, tolerance_m) if prefer_shapely else None
    if indices is None:
        indices = _simplify_indices_python(projected, tolerance_m)
    return [coordinates[index] for index in indices]


def _project_local(coordinates: Sequence[Coordinate]) -> list[tuple[float, float]]:
    """Project onto a local equirectangular plane in metres, centred on the polyline."""
    latitudes = [point.latitude for point in coordinates]
    reference_latitude = (min(latitudes) + max(latitudes)) / 2.0
    scale_x = math.cos(math.radians(reference_latitude)) * METRES_PER_DEGREE_LATITUDE
    origin = coordinates[0]
    return [
        (
            (point.longitude - origin.longitude) * scale_x,
            (point.latitude - origin.latitude) * METRES_PER_DEGREE_LATITUDE,
        )
        for point in coordinates
    ]


def _simplify_indices_python(
    points: Sequence[tuple[float, float]],
    tolerance_m: float,
) -> list[int]:
    """Iterative Douglas-Peucker returning the indices of the retained vertices.

    Iterative rather than recursive: a dense OSRM geometry is tens of thousands of vertices
    and the worst case (a monotone curve) recurses once per vertex, which blows the default
    recursion limit on a real route.
    """
    last = len(points) - 1
    keep = [False] * len(points)
    keep[0] = True
    keep[last] = True
    stack: list[tuple[int, int]] = [(0, last)]

    while stack:
        first, final = stack.pop()
        if final <= first + 1:
            continue
        ax, ay = points[first]
        bx, by = points[final]
        dx = bx - ax
        dy = by - ay
        length_sq = dx * dx + dy * dy
        farthest = first
        farthest_distance = -1.0
        for index in range(first + 1, final):
            px, py = points[index]
            if length_sq == 0.0:
                distance = math.hypot(px - ax, py - ay)
            else:
                ratio = min(1.0, max(0.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
                distance = math.hypot(px - ax - ratio * dx, py - ay - ratio * dy)
            if distance > farthest_distance:
                farthest_distance = distance
                farthest = index
        if farthest_distance > tolerance_m:
            keep[farthest] = True
            stack.append((first, farthest))
            stack.append((farthest, final))

    return [index for index, retained in enumerate(keep) if retained]


def _simplify_indices_shapely(
    points: Sequence[tuple[float, float]],
    tolerance_m: float,
) -> list[int] | None:
    """Simplify with Shapely, mapping the result back onto input indices.

    Returns ``None`` when Shapely is absent, or in the pathological case where a returned
    vertex cannot be matched to an input vertex — the caller then uses the Python
    implementation, so a Shapely upgrade can never silently corrupt a geometry.
    """
    try:
        from shapely.geometry import LineString
    except ImportError:
        return None

    simplified = LineString(list(points)).simplify(tolerance_m, preserve_topology=False)
    retained: list[tuple[float, float]] = [(float(x), float(y)) for x, y in simplified.coords]

    indices: list[int] = []
    cursor = 0
    for x, y in retained:
        while cursor < len(points) and not _same_point(points[cursor], (x, y)):
            cursor += 1
        if cursor >= len(points):
            return None
        indices.append(cursor)
        cursor += 1
    return indices


def _same_point(a: tuple[float, float], b: tuple[float, float], tolerance_m: float = 1e-6) -> bool:
    """Whether two projected points are the same vertex, up to floating-point noise."""
    return abs(a[0] - b[0]) <= tolerance_m and abs(a[1] - b[1]) <= tolerance_m
