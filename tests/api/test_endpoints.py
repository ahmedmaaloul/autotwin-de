"""Endpoints that genuinely need PostGIS, driven through ASGI against the seeded database.

Marked ``integration`` as a module: CI runs ``-m "not integration and not kafka"``, so none of
this is in the default suite. What it buys is the half of the contract a fixture cannot check —
that the SQL is right, that the spatial predicates measure metres rather than degrees, and that
the aggregates a page renders agree with the rows they came from.

The assertions are **cross-checks and invariants**, not snapshots of today's data. The register
is re-ingested, the simulator is re-run and the traffic feed changes hourly, so a test pinning
"116 440 stations" would be a maintenance tax that catches nothing. Instead: the categories must
partition the register, the gaps must tile the corridor, the segment energies must sum to the
route total, and the SOC series must be the integral of those energies. Every one of those is a
statement about correctness that survives the data changing underneath it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from itertools import pairwise
from typing import Any

import httpx
import pytest

from autotwin_contracts import DATA_MODE_HEADER, ChargingCategory, DataOrigin, ProviderMode

from . import api_client

pytestmark = pytest.mark.integration

FLAGSHIP_ROUTE = "frankfurt-stuttgart"
"""The demo corridor of BUILD_SPEC: 203.4 km of A5/A8 seeded into ``routes``, 41 segments."""

GERMANY_BBOX = (5.87, 47.27, 15.04, 55.06)
"""west, south, east, north of the Federal Republic, to the nearest hundredth of a degree.

Everything this platform holds is inside it; a row outside is a lat/lon swap or a unit error.
"""


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """The API, driven in-process against the developer's PostGIS instance."""
    async with api_client() as instance:
        yield instance


class TestDashboardSummary:
    """``GET /api/v1/dashboard/summary`` — one consistent read, rendered as the landing page."""

    async def test_headline_counts_agree_with_the_endpoints_they_summarise(
        self, client: httpx.AsyncClient
    ) -> None:
        """A dashboard whose numbers disagree with the pages behind it is worse than no dashboard.

        The station total is cross-checked against the paged list's own ``total``, which is a
        different query over the same table.
        """
        summary = (await client.get("/api/v1/dashboard/summary")).json()
        page = (await client.get("/api/v1/charging/stations", params={"page_size": 1})).json()

        assert summary["charging_stations_total"] == page["total"]
        assert summary["charging_stations_total"] > 0

    async def test_the_power_classes_partition_the_register(
        self, client: httpx.AsyncClient
    ) -> None:
        """Every site is normal, fast or ultra_fast — exactly one of the three.

        So the breakdown has to add up to the total. If it did not, either a site has no category
        (the ingestion's classification missed a power value) or one is counted twice.
        """
        summary = (await client.get("/api/v1/dashboard/summary")).json()
        by_category = {row["category"]: row["count"] for row in summary["charging_by_category"]}

        assert set(by_category) <= {member.value for member in ChargingCategory}
        assert sum(by_category.values()) == summary["charging_stations_total"]

    async def test_fast_charging_points_are_counted_not_stations(
        self, client: httpx.AsyncClient
    ) -> None:
        """``fast_charging_points_total`` counts connectors, of which a site has several.

        Conflating the two is the most common way an infrastructure dashboard overstates itself,
        and the register holds roughly twice as many points as sites.
        """
        summary = (await client.get("/api/v1/dashboard/summary")).json()
        fast_sites = (
            await client.get(
                "/api/v1/charging/stations", params={"fast_only": True, "page_size": 1}
            )
        ).json()

        assert summary["fast_charging_points_total"] >= fast_sites["total"]

    async def test_freshness_lists_one_entry_per_ingested_source(
        self, client: httpx.AsyncClient
    ) -> None:
        """The honesty panel: what ran, when, and whether it was live, cached or a fixture."""
        summary = (await client.get("/api/v1/dashboard/summary")).json()
        freshness = summary["data_freshness"]

        assert freshness
        assert len({entry["source"] for entry in freshness}) == len(freshness)
        for entry in freshness:
            assert entry["mode"] in {mode.value for mode in ProviderMode}
            assert entry["age_minutes"] >= 0.0

    async def test_the_energy_trend_is_ordered_oldest_first(
        self, client: httpx.AsyncClient
    ) -> None:
        """The chart plots it left to right; an unsorted series would draw a scribble."""
        summary = (await client.get("/api/v1/dashboard/summary")).json()
        buckets = [point["bucket"] for point in summary["energy_trend"]]
        assert buckets == sorted(buckets)


class TestChargingStations:
    """``GET /api/v1/charging/stations`` — paging and filtering over 116 440 rows."""

    async def test_pages_are_disjoint_and_ordered_strongest_first(
        self, client: httpx.AsyncClient
    ) -> None:
        """A stable total order is what makes paging meaningful.

        Without the ``id`` tiebreaker after ``max_power_kw`` the database may return rows of
        equal power in any order, and page 2 would repeat rows from page 1.
        """
        first = (
            await client.get("/api/v1/charging/stations", params={"page": 1, "page_size": 25})
        ).json()
        second = (
            await client.get("/api/v1/charging/stations", params={"page": 2, "page_size": 25})
        ).json()

        assert first["total"] == second["total"]
        assert len(first["items"]) == len(second["items"]) == 25
        assert {item["id"] for item in first["items"]}.isdisjoint(
            item["id"] for item in second["items"]
        )
        powers = [item["max_power_kw"] for item in first["items"] + second["items"]]
        for stronger, weaker in pairwise(powers):
            assert stronger >= weaker

    async def test_the_last_page_holds_the_remainder_and_reports_no_next(
        self, client: httpx.AsyncClient
    ) -> None:
        """``has_next`` computed centrally, so fourteen routers cannot each get it wrong.

        The last page's size is derived from the total rather than read off the response: total
        minus the rows on the pages before it.
        """
        page_size = 500
        total = (await client.get("/api/v1/charging/stations", params={"page_size": 1})).json()[
            "total"
        ]
        last_page = -(-total // page_size)  # ceiling division

        page = (
            await client.get(
                "/api/v1/charging/stations", params={"page": last_page, "page_size": page_size}
            )
        ).json()

        assert page["has_next"] is False
        assert len(page["items"]) == total - (last_page - 1) * page_size

    async def test_a_page_beyond_the_end_is_empty_but_still_reports_the_total(
        self, client: httpx.AsyncClient
    ) -> None:
        """An empty page is not an error, and it must not claim there is another one.

        The frontend's infinite scroll stops on ``has_next``; a page past the end that reported
        ``true`` would spin for ever.
        """
        total = (await client.get("/api/v1/charging/stations", params={"page_size": 1})).json()[
            "total"
        ]

        page = (
            await client.get(
                "/api/v1/charging/stations", params={"page": 100_000, "page_size": 500}
            )
        ).json()

        assert page["items"] == []
        assert page["total"] == total
        assert page["page"] == 100_000
        assert page["page_size"] == 500
        assert page["has_next"] is False

    async def test_the_boundary_page_sizes_are_served(self, client: httpx.AsyncClient) -> None:
        """1 and 500 are inside the documented range, so both must return a page."""
        smallest = (await client.get("/api/v1/charging/stations", params={"page_size": 1})).json()
        largest = (await client.get("/api/v1/charging/stations", params={"page_size": 500})).json()

        assert len(smallest["items"]) == 1
        assert len(largest["items"]) == 500
        assert smallest["total"] == largest["total"]

    async def test_publishes_the_register_s_data_mode(self, client: httpx.AsyncClient) -> None:
        """Every data endpoint says how fresh its source was (BUILD_SPEC §7)."""
        response = await client.get("/api/v1/charging/stations", params={"page_size": 1})
        assert response.headers[DATA_MODE_HEADER] in {mode.value for mode in ProviderMode}

    @pytest.mark.parametrize("bundesland", ["HE", "BW", "BY"])
    async def test_the_bundesland_filter_restricts_and_narrows(
        self, client: httpx.AsyncClient, bundesland: str
    ) -> None:
        """Every returned row carries the requested state, and the total is a strict subset."""
        unfiltered = (await client.get("/api/v1/charging/stations", params={"page_size": 1})).json()
        filtered = (
            await client.get(
                "/api/v1/charging/stations",
                params={"bundesland": bundesland, "page_size": 50},
            )
        ).json()

        assert 0 < filtered["total"] < unfiltered["total"]
        assert {item["bundesland"] for item in filtered["items"]} == {bundesland}

    async def test_min_power_is_inclusive_and_applies_to_the_strongest_connector(
        self, client: httpx.AsyncClient
    ) -> None:
        """``>= 150`` must include a site rated exactly 150 kW, not start at the next one up."""
        page = (
            await client.get(
                "/api/v1/charging/stations", params={"min_power_kw": 150.0, "page_size": 100}
            )
        ).json()

        assert page["total"] > 0
        assert all(item["max_power_kw"] >= 150.0 for item in page["items"])

    async def test_fast_only_matches_the_fifty_kilowatt_threshold(
        self, client: httpx.AsyncClient
    ) -> None:
        """``is_fast_charger`` is a stored flag; it has to agree with the power it was derived from.

        A site flagged fast at 22 kW would quietly overstate the country's fast-charging network,
        which is the single number this platform is most likely to be quoted on.
        """
        page = (
            await client.get(
                "/api/v1/charging/stations", params={"fast_only": True, "page_size": 100}
            )
        ).json()

        assert page["total"] > 0
        for item in page["items"]:
            assert item["is_fast_charger"] is True
            assert item["max_power_kw"] >= 50.0

    async def test_the_category_filter_agrees_with_the_power_class(
        self, client: httpx.AsyncClient
    ) -> None:
        """``ultra_fast`` is the >=150 kW class; the stored category must match the stored power."""
        page = (
            await client.get(
                "/api/v1/charging/stations",
                params={"category": ChargingCategory.ultra_fast.value, "page_size": 100},
            )
        ).json()

        assert page["total"] > 0
        for item in page["items"]:
            assert item["charging_category"] == ChargingCategory.ultra_fast.value
            assert item["max_power_kw"] >= 150.0

    async def test_free_text_search_matches_operator_or_city(
        self, client: httpx.AsyncClient
    ) -> None:
        """``ILIKE '%term%'``: German operator names are corporate, so the match is unanchored."""
        page = (
            await client.get("/api/v1/charging/stations", params={"q": "EnBW", "page_size": 25})
        ).json()

        assert page["total"] > 0
        for item in page["items"]:
            haystack = f"{item['operator'] or ''} {item['city'] or ''}".lower()
            assert "enbw" in haystack

    async def test_a_wildcard_in_the_search_term_is_escaped(
        self, client: httpx.AsyncClient
    ) -> None:
        """Searching for ``%`` must not return the whole register.

        Unescaped, the user's ``%`` becomes a SQL wildcard and the filter looks broken rather
        than empty — and ``100%`` is a plausible thing to type into a search box.
        """
        page = (await client.get("/api/v1/charging/stations", params={"q": "%"})).json()
        assert page["total"] == 0

    async def test_filters_combine_with_and(self, client: httpx.AsyncClient) -> None:
        """Each filter can only narrow: the intersection is never larger than either side."""
        hessen = (
            await client.get(
                "/api/v1/charging/stations", params={"bundesland": "HE", "page_size": 1}
            )
        ).json()["total"]
        powerful = (
            await client.get(
                "/api/v1/charging/stations", params={"min_power_kw": 150.0, "page_size": 1}
            )
        ).json()["total"]
        both = (
            await client.get(
                "/api/v1/charging/stations",
                params={"bundesland": "HE", "min_power_kw": 150.0, "page_size": 25},
            )
        ).json()

        assert both["total"] <= min(hessen, powerful)
        for item in both["items"]:
            assert item["bundesland"] == "HE"
            assert item["max_power_kw"] >= 150.0

    async def test_a_bounding_box_keeps_every_site_inside_it(
        self, client: httpx.AsyncClient
    ) -> None:
        """The predicate runs on ``geography``, so the box measures ground distance."""
        west, south, east, north = 8.4, 49.9, 8.9, 50.3  # the Frankfurt area
        page = (
            await client.get(
                "/api/v1/charging/stations",
                params={"bbox": f"{west},{south},{east},{north}", "page_size": 100},
            )
        ).json()

        assert page["total"] > 0
        for item in page["items"]:
            assert west <= item["longitude"] <= east
            assert south <= item["latitude"] <= north

    async def test_a_filter_matching_nothing_returns_an_empty_page_not_an_error(
        self, client: httpx.AsyncClient
    ) -> None:
        """The degenerate case every list endpoint has to survive."""
        page = (
            await client.get(
                "/api/v1/charging/stations",
                params={"min_power_kw": 99_999.0, "page_size": 10},
            )
        ).json()

        assert page == {
            "items": [],
            "total": 0,
            "page": 1,
            "page_size": 10,
            "has_next": False,
        }


class TestCorridorCoverage:
    """``GET /api/v1/charging/coverage`` — can an electric car actually drive this corridor?"""

    @pytest.fixture
    async def coverage(self, client: httpx.AsyncClient) -> dict[str, Any]:
        """The flagship corridor at a 300 kW threshold.

        The threshold is high on purpose: at 50 kW the corridor holds 888 qualifying sites and
        889 gaps, more than the 500 the response is allowed to carry, so the tiling invariant
        below could not be checked on the payload itself. At 300 kW every gap fits.
        """
        response = await client.get(
            "/api/v1/charging/coverage",
            params={"route_slug": FLAGSHIP_ROUTE, "min_power_kw": 300.0, "top_gaps": 500},
        )
        assert response.status_code == 200
        return dict(response.json())

    async def test_the_gaps_tile_the_corridor_exactly_once(self, coverage: dict[str, Any]) -> None:
        """The completeness invariant: the gaps must sum to the route's geometry length.

        This is what catches the three mistakes that leave every other number looking plausible —
        a missing edge gap, a double-counted station, and an offset measured against the wrong
        length. Ten metres of tolerance over 203 km absorbs the floating-point error of summing a
        few hundred fractions and nothing else.
        """
        assert len(coverage["gaps"]) == coverage["gap_count"]  # nothing was capped away
        total_km = sum(gap["gap_km"] for gap in coverage["gaps"])
        assert total_km == pytest.approx(coverage["route_geometry_length_km"], abs=0.01)

    async def test_the_two_edge_gaps_are_included(self, coverage: dict[str, Any]) -> None:
        """Origin to first charger, and last charger to destination.

        Omitting them is the classic error: a corridor whose only two fast chargers sit at km 10
        and km 12 has one interior gap of 2 km and looks immaculate, while the 190 km after km 12
        are the entire problem.
        """
        kinds = [gap["kind"] for gap in coverage["gaps"]]
        assert kinds.count("origin") == 1
        assert kinds.count("destination") == 1
        assert min(gap["start_offset_km"] for gap in coverage["gaps"]) == 0.0
        assert max(gap["end_offset_km"] for gap in coverage["gaps"]) == pytest.approx(
            coverage["route_geometry_length_km"], abs=0.01
        )

    async def test_the_gap_count_is_one_more_than_the_qualifying_sites(
        self, coverage: dict[str, Any]
    ) -> None:
        """N stations projected onto a line cut it into N+1 stretches. That is the whole model."""
        assert coverage["gap_count"] == coverage["qualifying_stations_in_corridor"] + 1

    async def test_the_aggregates_are_computed_over_every_gap(
        self, coverage: dict[str, Any]
    ) -> None:
        """``max`` and ``mean`` are computed in PostGIS; recomputing them here checks the SQL.

        The mean is rounded to three decimals in transport, so the reconstructed total is only
        good to half a metre per gap.
        """
        gaps = [gap["gap_km"] for gap in coverage["gaps"]]
        assert coverage["max_gap_km"] == pytest.approx(max(gaps), abs=0.001)
        assert coverage["mean_gap_km"] * coverage["gap_count"] == pytest.approx(
            coverage["route_geometry_length_km"], abs=0.0005 * coverage["gap_count"]
        )

    async def test_gaps_are_returned_longest_first_with_drawable_geometry(
        self, coverage: dict[str, Any]
    ) -> None:
        """The map draws them and the page lists the worst; both need the order and the line."""
        lengths = [gap["gap_km"] for gap in coverage["gaps"]]
        assert lengths == sorted(lengths, reverse=True)
        assert coverage["gaps"][0]["geometry"]["type"] == "LineString"
        assert len(coverage["gaps"][0]["geometry"]["coordinates"]) >= 2

    async def test_the_geometry_length_is_just_under_the_driving_distance(
        self, coverage: dict[str, Any]
    ) -> None:
        """A stored polyline is a chord approximation of the road, so it is shorter — by per mille.

        The two are different quantities and the payload keeps them apart on purpose: offsets are
        measured on the geometry, densities are quoted against the routing engine's distance.
        """
        assert coverage["route_geometry_length_km"] <= coverage["route_distance_km"]
        assert coverage["route_geometry_length_km"] > coverage["route_distance_km"] * 0.99

    async def test_a_lower_power_threshold_can_only_add_stations_and_shrink_gaps(
        self, client: httpx.AsyncClient
    ) -> None:
        """Monotonicity in the threshold — a property of the method, not of today's register."""
        strict = (
            await client.get(
                "/api/v1/charging/coverage",
                params={"route_slug": FLAGSHIP_ROUTE, "min_power_kw": 300.0},
            )
        ).json()
        lenient = (
            await client.get(
                "/api/v1/charging/coverage",
                params={"route_slug": FLAGSHIP_ROUTE, "min_power_kw": 50.0},
            )
        ).json()

        assert (
            lenient["qualifying_stations_in_corridor"] >= strict["qualifying_stations_in_corridor"]
        )
        assert lenient["max_gap_km"] <= strict["max_gap_km"]

    async def test_the_score_is_bounded_and_the_methodology_is_stated(
        self, coverage: dict[str, Any]
    ) -> None:
        """``coverage_score`` is a planning heuristic, so the payload has to say how it was made."""
        assert 0.0 <= coverage["coverage_score"] <= 100.0
        assert "50 km" in coverage["methodology"] or "50" in coverage["methodology"]
        assert coverage["buffer_km"] == 5.0

    async def test_an_unknown_corridor_is_a_404_in_the_envelope(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get(
            "/api/v1/charging/coverage", params={"route_slug": "no-such-corridor"}
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    async def test_asking_without_a_corridor_is_a_422(self, client: httpx.AsyncClient) -> None:
        """Neither ``route_slug`` nor ``route_id``: a request that cannot be answered."""
        response = await client.get("/api/v1/charging/coverage")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"


class TestRouteAnalysis:
    """``POST /api/v1/routes/analyze`` — the flagship payload of BUILD_SPEC §7.3."""

    @pytest.fixture
    async def analysis(self, client: httpx.AsyncClient) -> dict[str, Any]:
        """Frankfurt to Stuttgart in a sedan, leaving at 90 %."""
        response = await client.post(
            "/api/v1/routes/analyze",
            json={
                "route_slug": FLAGSHIP_ROUTE,
                "vehicle_code": "sedan_ev",
                "start_soc_percent": 90.0,
                "min_arrival_soc_percent": 10.0,
            },
        )
        assert response.status_code == 200
        return dict(response.json())

    async def test_returns_one_analysed_row_per_seeded_segment(
        self, client: httpx.AsyncClient, analysis: dict[str, Any]
    ) -> None:
        """41 segments — the corridor's own ``route_segments`` rows, not a re-segmentation.

        Cross-checked against ``GET /routes/{slug}``: the Streckenband and the map layer join on
        ``ordinal``, so an analysis that dropped or invented a segment would put the chart and
        the map out of step with each other.
        """
        detail = (await client.get(f"/api/v1/routes/{FLAGSHIP_ROUTE}")).json()

        assert len(analysis["segments"]) == len(detail["segments"]) == 41
        assert [segment["ordinal"] for segment in analysis["segments"]] == list(range(41))

    async def test_segment_offsets_are_contiguous_and_cover_the_route(
        self, analysis: dict[str, Any]
    ) -> None:
        """Each segment starts where the last one ended, and together they span the corridor.

        A gap or an overlap would make ``start_offset_km`` unusable as the x-axis of the
        Streckenband, which is exactly what it is.
        """
        segments = analysis["segments"]
        assert segments[0]["start_offset_km"] == 0.0
        for current, following in pairwise(segments):
            assert following["start_offset_km"] == pytest.approx(
                current["start_offset_km"] + current["distance_km"], abs=1e-6
            )
            assert current["distance_km"] > 0.0

        covered_km = segments[-1]["start_offset_km"] + segments[-1]["distance_km"]
        assert covered_km == pytest.approx(analysis["route"]["distance_m"] / 1000.0, rel=0.01)

    async def test_the_state_of_charge_falls_monotonically_along_the_route(
        self, analysis: dict[str, Any]
    ) -> None:
        """No regeneration credit can make a later segment end fuller than an earlier one.

        The physical model floors a segment at zero net energy, so the SOC series is the running
        integral of non-negative consumption. A rise here would mean the analysis had invented
        energy, and the arrival figure the charging optimiser plans against would be wrong.
        """
        socs = [segment["soc_at_end_percent"] for segment in analysis["segments"]]

        assert socs[0] < analysis["start_soc_percent"]
        for earlier, later in pairwise(socs):
            assert later <= earlier + 1e-9
        assert socs[-1] == pytest.approx(analysis["arrival_soc_percent"], abs=1e-6)

    async def test_the_arrival_soc_is_the_start_minus_the_energy_drawn(
        self, analysis: dict[str, Any]
    ) -> None:
        """Derived from the energy and the usable capacity, not read back from the payload.

        SOC is expressed against the *usable* capacity (74 kWh for the sedan), not the gross 77;
        using the wrong one would understate the drop by four percent and is invisible unless
        somebody does this arithmetic.
        """
        usable_kwh = analysis["vehicle"]["usable_capacity_kwh"]
        expected_drop = analysis["energy_kwh_total"] / usable_kwh * 100.0

        assert analysis["start_soc_percent"] - analysis["arrival_soc_percent"] == pytest.approx(
            expected_drop, rel=1e-6
        )

    async def test_the_segment_energies_sum_to_the_route_total(
        self, analysis: dict[str, Any]
    ) -> None:
        """The headline number is the integral of the rows behind it, to the last watt-hour."""
        assert sum(segment["kwh"] for segment in analysis["segments"]) == pytest.approx(
            analysis["energy_kwh_total"], rel=1e-9
        )

    async def test_the_average_consumption_is_the_energy_over_the_distance(
        self, analysis: dict[str, Any]
    ) -> None:
        """kWh/100 km recomputed from the two quantities it is made of."""
        distance_km = sum(segment["distance_km"] for segment in analysis["segments"])
        expected = analysis["energy_kwh_total"] / (distance_km / 100.0)

        assert analysis["avg_consumption_kwh_100km"] == pytest.approx(expected, rel=1e-6)
        # A motorway corridor in a mid-size EV: 10-30 kWh/100 km is the plausible envelope.
        assert 10.0 < analysis["avg_consumption_kwh_100km"] < 30.0

    async def test_every_segment_reports_a_speed_inside_its_own_traffic_state(
        self, analysis: dict[str, Any]
    ) -> None:
        """``assumed = free_flow / delay_factor``, so traffic can only ever slow a segment down."""
        for segment in analysis["segments"]:
            assert segment["assumed_speed_kmh"] <= segment["free_flow_speed_kmh"] + 1e-9
            assert segment["assumed_speed_kmh"] > 0.0
            assert segment["duration_s"] > 0.0

    async def test_the_explanation_is_deterministic_and_bilingual(
        self, analysis: dict[str, Any]
    ) -> None:
        """No language model is involved (BUILD_SPEC §11): the drivers are a counterfactual ladder.

        They are consecutive differences of the same physical model, so they sum exactly to the
        total deviation from the vehicle's nominal consumption rather than being independent
        sensitivities that happen to be near it.
        """
        explanation = analysis["explanation"]

        assert explanation["headline_de"] and explanation["headline_en"]
        assert explanation["headline_de"] != explanation["headline_en"]
        drivers = explanation["drivers"]
        assert drivers
        assert sum(driver["delta_percent"] for driver in drivers) == pytest.approx(
            explanation["total_delta_percent"], abs=0.01
        )
        for driver in drivers:
            assert driver["label_de"] and driver["label_en"]

    async def test_a_heavier_vehicle_costs_more_over_the_same_corridor(
        self, client: httpx.AsyncClient
    ) -> None:
        """The same road, the same weather, the same traffic: only the car changes.

        A van is heavier and far draggier than a compact, so it has to consume more. If it did
        not, the vehicle parameters would not be reaching the energy model at all.
        """
        results: dict[str, float] = {}
        for code in ("compact_ev", "van_ev"):
            body = (
                await client.post(
                    "/api/v1/routes/analyze",
                    json={"route_slug": FLAGSHIP_ROUTE, "vehicle_code": code},
                )
            ).json()
            results[code] = body["avg_consumption_kwh_100km"]

        assert results["van_ev"] > results["compact_ev"]

    async def test_charging_required_agrees_with_the_arrival_reserve(
        self, client: httpx.AsyncClient
    ) -> None:
        """Leaving at 15 % cannot finish a 203 km corridor with 10 % left; leaving at 90 % can.

        And ``energy_deficit_kwh`` is exactly what the charging optimiser has to supply, so it
        must be positive in the first case and zero in the second.
        """
        low = (
            await client.post(
                "/api/v1/routes/analyze",
                json={
                    "route_slug": FLAGSHIP_ROUTE,
                    "vehicle_code": "compact_ev",
                    "start_soc_percent": 15.0,
                    "min_arrival_soc_percent": 10.0,
                },
            )
        ).json()
        high = (
            await client.post(
                "/api/v1/routes/analyze",
                json={
                    "route_slug": FLAGSHIP_ROUTE,
                    "vehicle_code": "compact_ev",
                    "start_soc_percent": 90.0,
                    "min_arrival_soc_percent": 10.0,
                },
            )
        ).json()

        assert low["charging_required"] is True
        assert low["energy_deficit_kwh"] > 0.0
        assert high["charging_required"] is False
        assert high["energy_deficit_kwh"] == 0.0

    async def test_the_data_modes_and_assumptions_are_stated(
        self, analysis: dict[str, Any]
    ) -> None:
        """Every answer says where it came from and what it had to assume to be produced."""
        modes = analysis["data_modes"]
        assert set(modes) == {"routing", "weather", "traffic"}
        assert all(value in {mode.value for mode in ProviderMode} for value in modes.values())
        assert analysis["assumptions"]

    async def test_an_unknown_corridor_is_a_404(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            "/api/v1/routes/analyze", json={"route_slug": "no-such-corridor"}
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


class TestVehiclesLive:
    """``GET /api/v1/vehicles/live`` — the initial state of the live map."""

    async def test_returns_at_most_one_row_per_vehicle(self, client: httpx.AsyncClient) -> None:
        """The newest sample of each vehicle; a duplicate would draw two markers for one car."""
        live = (await client.get("/api/v1/vehicles/live")).json()

        assert live
        identifiers = [vehicle["vehicle_id"] for vehicle in live]
        assert len(identifiers) == len(set(identifiers))

    async def test_every_row_is_physically_possible_and_inside_germany(
        self, client: httpx.AsyncClient
    ) -> None:
        """SOC in range, speed non-negative, position on the map.

        The bounding box is the cheap detector of a latitude/longitude swap: 8.6, 50.1 is
        Frankfurt and 50.1, 8.6 is a field in Somalia.
        """
        west, south, east, north = GERMANY_BBOX
        for vehicle in (await client.get("/api/v1/vehicles/live")).json():
            assert 0.0 <= vehicle["battery_soc_percent"] <= 100.0
            assert vehicle["speed_kmh"] >= 0.0
            assert west <= vehicle["longitude"] <= east
            assert south <= vehicle["latitude"] <= north
            assert vehicle["recorded_at"].endswith("Z") or "+" in vehicle["recorded_at"]

    async def test_the_identifier_filter_selects_exactly_those_vehicles(
        self, client: httpx.AsyncClient
    ) -> None:
        live = (await client.get("/api/v1/vehicles/live")).json()
        wanted = [vehicle["vehicle_id"] for vehicle in live[:2]]

        filtered = (
            await client.get("/api/v1/vehicles/live", params={"vehicle_ids": ",".join(wanted)})
        ).json()

        assert sorted(vehicle["vehicle_id"] for vehicle in filtered) == sorted(wanted)

    async def test_a_bounding_box_keeps_the_vehicle_it_was_built_around(
        self, client: httpx.AsyncClient
    ) -> None:
        """Derived from a real row: a small box around one vehicle must contain that vehicle.

        The reverse — asserting a box excludes everything — would pass against a broken query
        that returns nothing at all.
        """
        live = (await client.get("/api/v1/vehicles/live")).json()
        target = live[0]
        box = (
            f"{target['longitude'] - 0.05},{target['latitude'] - 0.05},"
            f"{target['longitude'] + 0.05},{target['latitude'] + 0.05}"
        )

        inside = (await client.get("/api/v1/vehicles/live", params={"bbox": box})).json()

        assert target["vehicle_id"] in {vehicle["vehicle_id"] for vehicle in inside}
        assert len(inside) <= len(live)

    async def test_a_box_over_the_north_sea_returns_nothing_rather_than_failing(
        self, client: httpx.AsyncClient
    ) -> None:
        """An empty result is an answer; the map draws no markers and says so."""
        response = await client.get("/api/v1/vehicles/live", params={"bbox": "3.0,53.5,4.0,54.0"})
        assert response.status_code == 200
        assert response.json() == []


class TestDataQuality:
    """``GET /api/v1/data/quality`` — the honesty page, one card per source."""

    @pytest.fixture
    async def quality(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        response = await client.get("/api/v1/data/quality")
        assert response.status_code == 200
        return list(response.json())

    async def test_reports_one_card_per_source_including_the_simulator(
        self, quality: list[dict[str, Any]]
    ) -> None:
        sources = [row["source"] for row in quality]
        assert len(sources) == len(set(sources))
        assert "simulator" in sources

    async def test_the_simulator_is_never_reported_as_a_healthy_external_source(
        self, quality: list[dict[str, Any]]
    ) -> None:
        """It is not an external source at all (BUILD_SPEC §6), and its rows are simulated.

        Reporting it as ``healthy`` next to the Bundesnetzagentur would put generated telemetry
        on the same footing as an official register, which is precisely what ADR 004 forbids.
        """
        simulator = next(row for row in quality if row["source"] == "simulator")

        assert simulator["status"] == "simulation"
        assert simulator["data_origin"] == DataOrigin.simulated.value
        assert simulator["provider_mode"] is None

    async def test_ingested_sources_are_labelled_official_and_carry_their_attribution(
        self, quality: list[dict[str, Any]]
    ) -> None:
        """Attribution is a licence obligation, not a nicety: CC BY requires the credit line.

        The *licence* may legitimately be null — the Autobahn GmbH feed declares no terms at
        all, and recording that absence is a fact about the source rather than a missing value.
        An empty string would be the bug: it reads as "no licence" while meaning "we forgot".
        """
        for row in quality:
            if row["source"] == "simulator":
                continue
            assert row["data_origin"] == DataOrigin.official.value
            assert row["attribution"]
            assert row["licence"] is None or row["licence"].strip()
            assert row["source_url"]

    async def test_the_acceptance_rate_is_the_ratio_it_claims_to_be(
        self, quality: list[dict[str, Any]]
    ) -> None:
        """Recomputed from the two counters beside it, including the zero-rows edge case.

        A run that received nothing has no ratio to report; 0 is the documented answer, and a
        naive division would raise instead.
        """
        for row in quality:
            if row["rows_received"] == 0:
                assert row["acceptance_rate"] == 0.0
                continue
            expected = row["rows_accepted"] / row["rows_received"] * 100.0
            assert row["acceptance_rate"] == pytest.approx(expected, abs=0.01)
            assert 0.0 <= row["acceptance_rate"] <= 100.0

    async def test_row_counters_are_consistent(self, quality: list[dict[str, Any]]) -> None:
        """Accepted, rejected and duplicate rows are disjoint outcomes of the rows received."""
        for row in quality:
            assert row["rows_accepted"] >= 0
            assert row["rows_accepted"] <= row["rows_received"]

    async def test_the_worst_source_is_listed_first(self, quality: list[dict[str, Any]]) -> None:
        """The point of the page is the source that needs attention.

        A reader should not have to scan four healthy cards to find the failed one, so the order
        is failed, degraded, delayed, healthy, simulation.
        """
        severity = {
            "failed": 0,
            "degraded": 1,
            "delayed": 2,
            "healthy": 3,
            "simulation": 4,
        }
        ranks = [severity[row["status"]] for row in quality]
        assert ranks == sorted(ranks)

    async def test_a_failed_run_keeps_its_error_message(
        self, client: httpx.AsyncClient, quality: list[dict[str, Any]]
    ) -> None:
        """ "It failed" is not a report; the row has to say what happened.

        Skipped when the seeded database happens to hold no failed run, rather than asserting a
        failure that a re-ingestion could legitimately clear.
        """
        failed = [row for row in quality if row["status"] == "failed"]
        if not failed:
            pytest.skip("no failed ingestion run in this database")
        assert all(row["error_message"] for row in failed)
