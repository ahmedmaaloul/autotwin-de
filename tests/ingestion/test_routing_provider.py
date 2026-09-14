"""The OSRM routing adapter.

Everything here turns on one fact: **OSRM speaks ``lon,lat``** — in the request path and in the
GeoJSON it returns — while AutoTwin's :class:`~autotwin_contracts.Coordinate` is latitude-first.
Get the conversion backwards and the Frankfurt → Stuttgart demo route runs from (8.68, 50.11)
to (9.18, 48.78) read as *latitudes* 8.68 and 9.18: a route through Somalia and the Indian
Ocean. It is the single most common geospatial bug, so it is asserted from both ends.

The committed response is a real, untrimmed answer for the demo corridor. Its numbers are
cross-checked here against quantities computed in this file: the great-circle distance between
the endpoints, the summed length of the returned polyline, and the implied mean speed.
"""

from __future__ import annotations

import json
import math
from itertools import pairwise
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
import respx

from autotwin_contracts import (
    Coordinate,
    DataOrigin,
    ProviderMode,
    RoadClass,
    RouteResult,
    SourceSystem,
)
from autotwin_core.config import DataMode, get_settings
from autotwin_core.errors import InvalidSourceData, ProviderUnavailable
from autotwin_core.providers import AutoTwinHTTPClient, FileCache
from autotwin_ingestion.providers.routing import (
    DEFAULT_PROFILE,
    FIXTURE_DESTINATION,
    FIXTURE_MATCH_KM,
    FIXTURE_ORIGIN,
    OSRMRoutingProvider,
    parse_route,
    road_class_from_ref,
)

ROUTE_FIXTURE: Final[str] = "osrm_frankfurt_stuttgart.json"

# Published city coordinates, used as the request the fixture answers.
FRANKFURT: Final[Coordinate] = Coordinate(latitude=50.1109, longitude=8.6821)
STUTTGART: Final[Coordinate] = Coordinate(latitude=48.7758, longitude=9.1829)

EARTH_RADIUS_M: Final[float] = 6_371_008.8
"""IUGG mean Earth radius in metres."""


def haversine_m(a: Coordinate, b: Coordinate) -> float:
    """Great-circle distance, written out here so no expectation reuses the code under test."""
    phi_1, phi_2 = math.radians(a.latitude), math.radians(b.latitude)
    d_phi = phi_2 - phi_1
    d_lambda = math.radians(b.longitude - a.longitude)
    h = math.sin(d_phi / 2) ** 2 + math.cos(phi_1) * math.cos(phi_2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def payload(fixtures_dir: Path) -> dict[str, Any]:
    """The committed OSRM response, decoded."""
    decoded: dict[str, Any] = json.loads((fixtures_dir / ROUTE_FIXTURE).read_text(encoding="utf-8"))
    return decoded


@pytest.fixture
def route(payload: dict[str, Any]) -> RouteResult:
    return parse_route(payload, source_url="http://osrm.invalid/route/v1/driving/...")


@pytest.fixture
def provider(tmp_path: Path) -> OSRMRoutingProvider:
    """Fixture-mode adapter with an empty, private cache directory."""
    return OSRMRoutingProvider(data_mode=DataMode.fixture, cache=FileCache(tmp_path / "cache"))


# --------------------------------------------------------------------------- coordinate order


class TestCoordinateOrder:
    """``[lon, lat]`` in, latitude-first out. Both directions, both ends of the route."""

    def test_raw_positions_are_longitude_first(self, payload: dict[str, Any]) -> None:
        # The premise, asserted before anything depends on it: OSRM emits RFC 7946 positions.
        first = payload["routes"][0]["geometry"]["coordinates"][0]
        assert first[0] == pytest.approx(8.682092)
        assert first[1] == pytest.approx(50.110913)

    def test_parsed_origin_is_at_fifty_north_not_eight_north(self, route: RouteResult) -> None:
        # 50.1 N, 8.7 E is Frankfurt. 8.7 N, 50.1 E is 400 km off the Somali coast. Reading the
        # position tuple in the wrong order produces the second one without any error.
        origin = route.geometry[0]
        assert origin.latitude == pytest.approx(50.1, abs=0.05)
        assert origin.longitude == pytest.approx(8.68, abs=0.05)
        assert origin.latitude != pytest.approx(8.68, abs=0.05)

    def test_parsed_destination_is_stuttgart(self, route: RouteResult) -> None:
        destination = route.geometry[-1]
        assert destination.latitude == pytest.approx(48.78, abs=0.05)
        assert destination.longitude == pytest.approx(9.18, abs=0.05)

    def test_endpoints_match_the_requested_cities(self, route: RouteResult) -> None:
        # OSRM snaps to the nearest routable way, so a few hundred metres of drift is expected;
        # a swapped pair would be thousands of kilometres.
        assert haversine_m(route.geometry[0], FRANKFURT) < 500
        assert haversine_m(route.geometry[-1], STUTTGART) < 500

    def test_the_whole_geometry_is_inside_germany(self, route: RouteResult) -> None:
        # Germany's latitude and longitude ranges are disjoint (47-55 versus 6-15), so this
        # single check catches a swap anywhere along 3089 points, not just at the ends.
        for point in route.geometry:
            assert 47.2 <= point.latitude <= 55.1
            assert 5.8 <= point.longitude <= 15.1

    def test_the_corridor_stays_between_the_two_cities(self, route: RouteResult) -> None:
        # The A5/A67/A6/A81 corridor never goes north of Frankfurt or south of Stuttgart by
        # more than a few kilometres, and never leaves the Rhine-Neckar-Stuttgart longitudes.
        latitudes = [point.latitude for point in route.geometry]
        longitudes = [point.longitude for point in route.geometry]
        assert min(latitudes) >= 48.6 and max(latitudes) <= 50.2
        assert min(longitudes) >= 8.4 and max(longitudes) <= 9.4

    async def test_the_request_url_is_longitude_first(self, tmp_path: Path) -> None:
        # The other half of the same bug: OSRM would happily route from latitude 8.68 in the
        # Gulf of Guinea and answer "NoSegment", which reads like an outage.
        base = get_settings().osrm_base_url.rstrip("/")
        async with httpx.AsyncClient() as raw_client:
            client = AutoTwinHTTPClient(client=raw_client, max_attempts=1)
            with respx.mock(assert_all_called=True) as router:
                mocked = router.get(url__startswith=base).mock(
                    return_value=httpx.Response(200, json={"code": "NoRoute"})
                )
                adapter = OSRMRoutingProvider(
                    http_client=client,
                    cache=FileCache(tmp_path / "cache"),
                    data_mode=DataMode.live,
                )
                with pytest.raises(ProviderUnavailable):
                    await adapter.route(FRANKFURT, STUTTGART)
            path = mocked.calls[0].request.url.path
        assert path.endswith("/route/v1/driving/8.68210,50.11090;9.18290,48.77580")
        # Stated the other way round for the same reason as the parse-side assertion: the first
        # number in each pair is the longitude.
        assert "50.11090,8.68210" not in path


# --------------------------------------------------------------------------- the demo route


class TestBundledRoute:
    """The committed answer, cross-checked against quantities derived in this file."""

    def test_distance_is_about_205_km(self, route: RouteResult) -> None:
        # The straight line is ~152.8 km and the motorway detour factor for this corridor is
        # ~1.34, which puts the road distance around 205 km. Published A5/A67/A6/A81 figures
        # for Frankfurt → Stuttgart are 200-210 km depending on the exact endpoints.
        straight_line = haversine_m(FRANKFURT, STUTTGART)
        assert straight_line == pytest.approx(152_800, rel=0.01)
        assert route.distance_m == pytest.approx(205_000, rel=0.03)
        assert 1.2 <= route.distance_m / straight_line <= 1.45

    def test_distance_matches_the_summed_polyline(self, route: RouteResult) -> None:
        # Independent recomputation: walking the returned geometry must cost what OSRM says the
        # route costs. A truncated or mis-decoded geometry shows up here and nowhere else.
        walked = sum(haversine_m(a, b) for a, b in pairwise(route.geometry))
        assert walked == pytest.approx(route.distance_m, rel=0.005)

    def test_duration_is_about_two_and_a_quarter_hours(self, route: RouteResult) -> None:
        assert route.duration_s == pytest.approx(2.25 * 3600, rel=0.05)

    def test_implied_mean_speed_is_plausible_for_a_motorway_route(self, route: RouteResult) -> None:
        # 205 km in 2.26 h is 90.5 km/h door to door — right for a route that is mostly
        # Autobahn with city streets at both ends. A units error (metres for kilometres,
        # minutes for seconds) lands orders of magnitude away from this.
        mean_kmh = route.distance_m / route.duration_s * 3.6
        assert 70.0 <= mean_kmh <= 110.0

    def test_geometry_has_many_points(self, route: RouteResult) -> None:
        # ``overview=full`` is requested because the segmentation code needs every vertex;
        # ``overview=simplified`` would return a few dozen and silently coarsen every segment.
        assert len(route.geometry) == 3089
        assert len(route.geometry) > 1000

    def test_consecutive_points_are_close_together(self, route: RouteResult) -> None:
        # No gap larger than a couple of kilometres: a dropped vertex would cut a corner and
        # shorten the segment it belongs to.
        gaps = [haversine_m(a, b) for a, b in pairwise(route.geometry)]
        assert max(gaps) < 2_500

    def test_steps_account_for_the_whole_route(self, route: RouteResult) -> None:
        assert len(route.steps) == 41
        assert sum(step.distance_m for step in route.steps) == pytest.approx(route.distance_m)
        assert sum(step.duration_s for step in route.steps) == pytest.approx(route.duration_s)
        assert [step.ordinal for step in route.steps] == list(range(len(route.steps)))

    def test_annotations_are_per_edge_not_per_node(self, route: RouteResult) -> None:
        assert route.annotations is not None
        # OSRM annotates the *edges* between nodes, so each array is exactly one shorter than
        # the geometry. Zipping them against the nodes one-to-one would silently drop the last
        # segment's speed.
        for values in route.annotations.values():
            assert len(values) == len(route.geometry) - 1
        assert set(route.annotations) == {"distance", "duration", "speed"}

    def test_annotated_speeds_are_metres_per_second(self, route: RouteResult) -> None:
        speeds = route.annotations["speed"] if route.annotations else []
        # 6-46 m/s is 23-165 km/h: city streets to an unrestricted Autobahn stretch. Read as
        # km/h the same numbers would be a 6 km/h motorway.
        assert min(speeds) >= 1.0
        assert max(speeds) <= 70.0

    def test_metadata(self, route: RouteResult) -> None:
        assert route.profile == DEFAULT_PROFILE
        assert route.leg_count == 1
        assert route.weight_name == "routability"
        assert route.provenance is not None
        assert route.provenance.source is SourceSystem.osrm
        # A route is computed, not observed: it is `derived`, never `official`.
        assert route.provenance.data_origin is DataOrigin.derived

    def test_speed_limits_are_left_unset(self, route: RouteResult) -> None:
        # OSRM carries no maxspeed. Filling this with the engine's assumed speed would label an
        # assumption as a posted limit and poison the energy model's feature vector.
        assert all(step.speed_limit_kmh is None for step in route.steps)

    def test_motorway_steps_dominate_the_distance(self, route: RouteResult) -> None:
        motorway_m = sum(
            step.distance_m for step in route.steps if step.road_class is RoadClass.motorway
        )
        # The corridor is an Autobahn route; if the ref-to-class mapping broke, this collapses.
        assert motorway_m / route.distance_m > 0.8


class TestRoadClassFromRef:
    """The letter of a German road number *is* its functional class."""

    @pytest.mark.parametrize(
        ("ref", "expected"),
        [
            ("A 5", RoadClass.motorway),
            ("A5", RoadClass.motorway),
            ("B 27", RoadClass.primary),
            ("L 1100", RoadClass.secondary),
            ("S 177", RoadClass.secondary),
            ("K 818", RoadClass.tertiary),
            # Shared stretches: the highest class wins, because that is the road being driven.
            ("B 10; B 27", RoadClass.primary),
            ("A 8; B 27", RoadClass.motorway),
            ("B 27, A 81", RoadClass.motorway),
            # E-numbers are always co-signed on an A- or B-road in Germany, so they add nothing
            # and must not demote a Bundesstraße that carries one.
            ("E 41", RoadClass.unknown),
            # An unsigned street: `unknown`, not a guessed "residential" — OSRM has no highway
            # tag, and guessing would put city ring roads and farm tracks in one bucket.
            (None, RoadClass.unknown),
            ("", RoadClass.unknown),
            ("   ", RoadClass.unknown),
            ("Hauptstraße", RoadClass.unknown),
        ],
    )
    def test_mapping(self, ref: str | None, expected: RoadClass) -> None:
        assert road_class_from_ref(ref) is expected

    def test_the_fixture_exercises_the_mapping(self, payload: dict[str, Any]) -> None:
        refs = {
            step.get("ref")
            for leg in payload["routes"][0]["legs"]
            for step in leg["steps"]
            if step.get("ref")
        }
        # The real answer contains A-, B- and K-roads and one multi-ref step, so the
        # parametrised cases above are not hypotheticals.
        assert "A 5" in refs
        assert "B 10; B 27" in refs
        assert any(ref.startswith("K ") for ref in refs)


# --------------------------------------------------------------------------- failure modes


class TestMalformedResponses:
    """A broken body must raise a typed provider error, never an ``IndexError``."""

    @pytest.mark.parametrize(
        ("body", "match"),
        [
            # An empty routes list: OSRM said Ok but sent nothing to read.
            ({"code": "Ok", "routes": []}, "carries no routes"),
            ({"code": "Ok"}, "carries no routes"),
            ({"code": "Ok", "routes": {}}, "carries no routes"),
            ({"code": "Ok", "routes": ["not an object"]}, "not an object"),
            # A geometry with fewer than two points is not a line; segmenting it divides by a
            # zero length further down the pipeline.
            (
                {"code": "Ok", "routes": [{"geometry": {"type": "LineString", "coordinates": []}}]},
                "0 point",
            ),
            (
                {
                    "code": "Ok",
                    "routes": [{"geometry": {"type": "LineString", "coordinates": [[8.6, 50.1]]}}],
                },
                "1 point",
            ),
            ({"code": "Ok", "routes": [{}]}, "0 point"),
            # The engine rejecting the request is our mistake, not an outage.
            ({"code": "InvalidQuery", "message": "Query string malformed"}, "rejected"),
            ({}, "carries no routes"),
        ],
    )
    def test_raises_invalid_source_data(self, body: Any, match: str) -> None:
        with pytest.raises(InvalidSourceData, match=match):
            parse_route(body)

    @pytest.mark.parametrize("body", ["a string", 42, None, [], [{"code": "Ok"}]])
    def test_a_non_object_body_is_invalid_source_data(self, body: Any) -> None:
        # An HTML error page served with status 200 decodes to a string, and must not crash
        # somewhere inside the parser.
        with pytest.raises(InvalidSourceData):
            parse_route(body)

    @pytest.mark.parametrize("code", ["NoRoute", "NoSegment", "NoMatch"])
    def test_no_route_is_unavailable_not_invalid(self, code: str) -> None:
        # A correct answer to a legitimate question that happens to be "there is no route" is
        # an availability problem (503), not a schema problem (502). The API maps the two to
        # different status codes, so the distinction is visible to clients.
        with pytest.raises(ProviderUnavailable, match="found no route"):
            parse_route({"code": code, "message": "no route found"})

    def test_a_truncated_route_degrades_to_zero_lengths(self) -> None:
        # Distance and duration missing entirely: the record model rejects negatives and OSRM
        # never emits them, so a truncated body becomes a zero-length route rather than a
        # validation traceback halfway through an ingestion run.
        parsed = parse_route(
            {
                "code": "Ok",
                "routes": [
                    {"geometry": {"type": "LineString", "coordinates": [[8.6, 50.1], [9.1, 48.8]]}}
                ],
            }
        )
        assert parsed.distance_m == 0.0
        assert parsed.duration_s == 0.0
        assert parsed.leg_count == 1
        assert parsed.annotations is None

    @pytest.mark.parametrize(
        "geometry",
        [
            {"type": "Point", "coordinates": [8.6, 50.1]},
            {"type": "LineString", "coordinates": "8.6,50.1"},
            {"type": "LineString", "coordinates": [[8.6], ["x", "y"], [None, None]]},
        ],
    )
    def test_unreadable_geometry_is_invalid_source_data(self, geometry: Any) -> None:
        with pytest.raises(InvalidSourceData):
            parse_route({"code": "Ok", "routes": [{"geometry": geometry}]})


class TestFixtureMatching:
    """The fixture answers only for the journey it actually describes."""

    async def test_the_demo_corridor_is_answered(self, provider: OSRMRoutingProvider) -> None:
        result = await provider.route(FRANKFURT, STUTTGART)
        assert result.mode is ProviderMode.fixture
        assert result.is_degraded is True
        assert result.data.distance_m == pytest.approx(205_000, rel=0.03)
        assert any("bundled" in warning for warning in result.warnings)

    async def test_endpoints_inside_the_match_radius_are_accepted(
        self, provider: OSRMRoutingProvider
    ) -> None:
        # Frankfurt Hauptbahnhof and a Stuttgart park-and-ride are the same journey as the
        # bundled one for a demo's purposes.
        hauptbahnhof = Coordinate(latitude=50.1070, longitude=8.6634)
        vaihingen = Coordinate(latitude=48.7350, longitude=9.1130)
        assert haversine_m(hauptbahnhof, FIXTURE_ORIGIN) < FIXTURE_MATCH_KM * 1000
        assert haversine_m(vaihingen, FIXTURE_DESTINATION) < FIXTURE_MATCH_KM * 1000
        result = await provider.route(hauptbahnhof, vaihingen)
        assert result.mode is ProviderMode.fixture

    @pytest.mark.parametrize(
        ("origin", "destination"),
        [
            # Berlin → Stuttgart: a completely different journey.
            (Coordinate(latitude=52.5200, longitude=13.4050), STUTTGART),
            # Frankfurt → Munich: right start, wrong end.
            (FRANKFURT, Coordinate(latitude=48.1351, longitude=11.5820)),
            # The demo corridor driven backwards is not the same route either.
            (STUTTGART, FRANKFURT),
        ],
    )
    async def test_a_different_journey_raises_rather_than_inventing_one(
        self,
        provider: OSRMRoutingProvider,
        origin: Coordinate,
        destination: Coordinate,
    ) -> None:
        # The one place a provider is required to fail rather than degrade: returning the
        # Frankfurt → Stuttgart geometry for a Berlin request would be inventing data, which
        # BUILD_SPEC §0.2 forbids more strongly than it demands availability.
        with pytest.raises(ProviderUnavailable, match="bundled fixture covers only"):
            await provider.route(origin, destination)

    async def test_the_error_names_both_endpoints(self, provider: OSRMRoutingProvider) -> None:
        berlin = Coordinate(latitude=52.5200, longitude=13.4050)
        with pytest.raises(ProviderUnavailable) as raised:
            await provider.route(berlin, berlin)
        # An operator reading the log needs to know how far off the request was, not just that
        # it was refused.
        assert raised.value.details["origin"] == berlin.as_latlon_tuple()
        assert raised.value.details["fixture_origin"] == FIXTURE_ORIGIN.as_latlon_tuple()

    def test_the_match_radius_is_documented_and_generous_but_finite(self) -> None:
        assert FIXTURE_MATCH_KM == 30.0
        # 30 km covers a city and its park-and-rides; it must not cover the next city. Mainz is
        # ~30 km from Frankfurt and Heilbronn ~45 km from Stuttgart, so the radius sits between
        # "same journey" and "different journey".
        assert haversine_m(FIXTURE_ORIGIN, FIXTURE_DESTINATION) > FIXTURE_MATCH_KM * 1000 * 4
