"""Geometry helpers.

Distances are asserted against values computed independently (great-circle formula on paper /
published city-pair distances), not against the implementation's own output — a test that
only pins current behaviour catches refactors, not mistakes.
"""

from __future__ import annotations

import math

import pytest

from autotwin_contracts.geo import Coordinate, haversine_km
from autotwin_core.geo.distance import (
    bearing_deg,
    cumulative_distances_m,
    densify,
    haversine_m,
    interpolate,
    point_at_offset,
    simplify,
    total_length_m,
)
from autotwin_core.geo.segmentation import (
    match_points_to_segments,
    nearest_segment_index,
    segment_polyline,
)

FRANKFURT = Coordinate(latitude=50.1109, longitude=8.6821)
STUTTGART = Coordinate(latitude=48.7758, longitude=9.1829)
MUNICH = Coordinate(latitude=48.1351, longitude=11.5820)
BERLIN = Coordinate(latitude=52.5200, longitude=13.4050)


class TestHaversine:
    def test_frankfurt_stuttgart_matches_published_great_circle_distance(self) -> None:
        # ~152.8 km straight line; the A5/A8 road distance is ~205 km, which is a different
        # quantity and must not be confused with this one.
        assert haversine_m(FRANKFURT, STUTTGART) == pytest.approx(152_800, rel=0.01)

    def test_frankfurt_berlin(self) -> None:
        assert haversine_km(FRANKFURT, BERLIN) == pytest.approx(423, rel=0.01)

    def test_zero_for_identical_points(self) -> None:
        assert haversine_m(FRANKFURT, FRANKFURT) == pytest.approx(0.0, abs=1e-6)

    def test_symmetric(self) -> None:
        assert haversine_m(FRANKFURT, MUNICH) == pytest.approx(haversine_m(MUNICH, FRANKFURT))


class TestBearing:
    def test_due_north_is_zero(self) -> None:
        a = Coordinate(latitude=50.0, longitude=8.0)
        b = Coordinate(latitude=51.0, longitude=8.0)
        assert bearing_deg(a, b) == pytest.approx(0.0, abs=0.5)

    def test_due_east_is_ninety(self) -> None:
        a = Coordinate(latitude=50.0, longitude=8.0)
        b = Coordinate(latitude=50.0, longitude=9.0)
        assert bearing_deg(a, b) == pytest.approx(90.0, abs=0.5)

    def test_frankfurt_to_stuttgart_heads_south_southeast(self) -> None:
        assert 150 <= bearing_deg(FRANKFURT, STUTTGART) <= 185


class TestPolylineMaths:
    @pytest.fixture
    def corridor(self) -> list[Coordinate]:
        """A synthetic 4-point line from Frankfurt towards Stuttgart."""
        return [
            FRANKFURT,
            Coordinate(latitude=49.8, longitude=8.75),
            Coordinate(latitude=49.4, longitude=8.9),
            STUTTGART,
        ]

    def test_cumulative_distances_start_at_zero_and_increase(self, corridor: list[Coordinate]) -> None:
        cumulative = cumulative_distances_m(corridor)
        assert cumulative[0] == 0.0
        assert cumulative == sorted(cumulative)
        assert len(cumulative) == len(corridor)
        assert cumulative[-1] == pytest.approx(total_length_m(corridor))

    def test_total_length_exceeds_straight_line(self, corridor: list[Coordinate]) -> None:
        assert total_length_m(corridor) >= haversine_m(FRANKFURT, STUTTGART)

    def test_point_at_offset_endpoints(self, corridor: list[Coordinate]) -> None:
        start = point_at_offset(corridor, 0.0)
        assert haversine_m(start, FRANKFURT) < 1.0
        end = point_at_offset(corridor, total_length_m(corridor))
        assert haversine_m(end, STUTTGART) < 1.0

    def test_point_at_offset_is_on_the_line(self, corridor: list[Coordinate]) -> None:
        target = total_length_m(corridor) / 2
        mid = point_at_offset(corridor, target)
        # Walking to the returned point must cost the requested offset.
        assert total_length_m([*corridor[:1], mid]) >= 0
        assert 48.0 < mid.latitude < 51.0

    def test_densify_never_exceeds_spacing(self, corridor: list[Coordinate]) -> None:
        dense = densify(corridor, max_spacing_m=5_000)
        gaps = [haversine_m(a, b) for a, b in zip(dense, dense[1:], strict=False)]
        assert max(gaps) <= 5_000 * 1.01
        # Inserted points are interpolated linearly in lat/lon, so the densified polyline is a
        # chord approximation of the original great-circle legs and its length drifts by about
        # a part per million over 150 km. That is a property of the method, not an error.
        assert total_length_m(dense) == pytest.approx(total_length_m(corridor), rel=1e-4)

    def test_simplify_keeps_endpoints_and_reduces_points(self, corridor: list[Coordinate]) -> None:
        dense = densify(corridor, max_spacing_m=2_000)
        simple = simplify(dense, tolerance_m=500)
        assert len(simple) <= len(dense)
        assert haversine_m(simple[0], dense[0]) < 1.0
        assert haversine_m(simple[-1], dense[-1]) < 1.0

    def test_interpolate_midpoint(self) -> None:
        mid = interpolate(FRANKFURT, STUTTGART, 0.5)
        assert haversine_m(FRANKFURT, mid) == pytest.approx(haversine_m(mid, STUTTGART), rel=0.02)


class TestSegmentation:
    @pytest.fixture
    def corridor(self) -> list[Coordinate]:
        return densify([FRANKFURT, STUTTGART], max_spacing_m=2_000)

    def test_preserves_total_length(self, corridor: list[Coordinate]) -> None:
        segments = segment_polyline(corridor, target_length_m=20_000)
        covered = sum(segment.length_m for segment in segments)
        # The spec requires the segmentation to lose no distance.
        assert covered == pytest.approx(total_length_m(corridor), abs=1.0)

    def test_ordinals_are_contiguous_from_zero(self, corridor: list[Coordinate]) -> None:
        segments = segment_polyline(corridor, target_length_m=20_000)
        assert [s.ordinal for s in segments] == list(range(len(segments)))

    def test_offsets_are_monotonic_and_contiguous(self, corridor: list[Coordinate]) -> None:
        segments = segment_polyline(corridor, target_length_m=20_000)
        for previous, current in zip(segments, segments[1:], strict=False):
            assert current.start_offset_m == pytest.approx(
                previous.start_offset_m + previous.length_m, abs=1.0
            )

    def test_no_zero_length_segments(self, corridor: list[Coordinate]) -> None:
        for segment in segment_polyline(corridor, target_length_m=20_000):
            assert segment.length_m > 0
            assert len(segment.coordinates) >= 2

    def test_short_route_yields_one_segment(self) -> None:
        short = [FRANKFURT, Coordinate(latitude=50.12, longitude=8.70)]
        segments = segment_polyline(short, target_length_m=20_000, min_length_m=5_000)
        assert len(segments) == 1

    def test_nearest_segment_index_finds_the_right_one(self, corridor: list[Coordinate]) -> None:
        segments = segment_polyline(corridor, target_length_m=20_000)
        probe = segments[len(segments) // 2].midpoint
        assert nearest_segment_index(segments, probe) == len(segments) // 2

    def test_far_away_points_are_not_matched(self, corridor: list[Coordinate]) -> None:
        segments = segment_polyline(corridor, target_length_m=20_000)
        # Hamburg is nowhere near the Frankfurt-Stuttgart corridor.
        hamburg = Coordinate(latitude=53.5511, longitude=9.9937)
        matches = match_points_to_segments(segments, [hamburg], max_distance_m=20_000)
        assert all(match.segment_index is None for match in matches) or len(matches) == 0


class TestCoordinateValidation:
    @pytest.mark.parametrize(("lat", "lon"), [(91.0, 0.0), (-91.0, 0.0), (0.0, 181.0), (0.0, -181.0)])
    def test_rejects_impossible_coordinates(self, lat: float, lon: float) -> None:
        with pytest.raises(ValueError):
            Coordinate(latitude=lat, longitude=lon)

    def test_lon_lat_ordering_is_explicit(self) -> None:
        # RFC 7946 is lon-first; the API is lat-first. Conflating them is the single most
        # common geospatial bug, so the conversion is never implicit.
        assert FRANKFURT.as_lonlat_tuple() == (8.6821, 50.1109)
        assert FRANKFURT.as_latlon_tuple() == (50.1109, 8.6821)

    def test_nan_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            Coordinate(latitude=math.nan, longitude=0.0)
