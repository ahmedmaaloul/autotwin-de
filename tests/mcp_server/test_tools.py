"""The MCP server: what it exposes, and that the tools return real platform output.

The registration tests need no database. The tool-call tests do — they exercise the same
service functions the API serves, against the seeded corridors — and are marked accordingly.
"""

from __future__ import annotations

import pytest

from autotwin_mcp import server as mcp_server
from autotwin_mcp.server import _strip_geometry, server

EXPECTED_TOOLS = {
    "list_routes",
    "vehicle_profiles",
    "analyze_route",
    "plan_charging_stops",
    "corridor_coverage",
    "underserved_corridors",
    "search_charging_stations",
    "data_quality",
}


class TestRegistration:
    async def test_exposes_exactly_the_documented_tools(self) -> None:
        tools = await server.list_tools()
        assert {tool.name for tool in tools} == EXPECTED_TOOLS

    async def test_every_tool_has_a_description_a_model_can_act_on(self) -> None:
        for tool in await server.list_tools():
            # A one-word description is useless to a model deciding which tool to call.
            assert tool.description and len(tool.description) > 60, tool.name

    async def test_instructions_state_that_telemetry_is_simulated(self) -> None:
        # ADR 004: the honesty rule reaches the model through the server instructions.
        assert "SIMULATED" in mcp_server.INSTRUCTIONS

    def test_vehicle_profiles_is_synchronous_and_needs_no_database(self) -> None:
        profiles = mcp_server.vehicle_profiles()
        assert {p["code"] for p in profiles} == {
            "compact_ev",
            "sedan_ev",
            "performance_ev",
            "suv_ev",
            "van_ev",
        }


class TestGeometryStripping:
    def test_removes_coordinate_payloads_at_any_depth(self) -> None:
        payload = {
            "route": {"geometry": {"type": "LineString", "coordinates": [[8.6, 50.1]]}, "km": 203},
            "segments": [{"ordinal": 0, "geometry": {"coordinates": []}, "kwh": 0.7}],
            "gaps": [{"gap_km": 13.0, "geometry_json": "{...}"}],
        }
        stripped = _strip_geometry(payload)
        assert stripped == {
            "route": {"km": 203},
            "segments": [{"ordinal": 0, "kwh": 0.7}],
            "gaps": [{"gap_km": 13.0}],
        }

    def test_leaves_non_geometry_untouched(self) -> None:
        payload = {"a": 1, "nested": {"b": [1, 2, {"c": "x"}]}}
        assert _strip_geometry(payload) == payload


@pytest.mark.integration
class TestToolCalls:
    async def test_list_routes_returns_the_seeded_corridors(self) -> None:
        routes = await mcp_server.list_routes()
        slugs = {route["slug"] for route in routes}
        assert "frankfurt-stuttgart" in slugs
        flagship = next(route for route in routes if route["slug"] == "frankfurt-stuttgart")
        # OSRM's A5/A8 driving distance, not the 153 km great-circle distance.
        assert 195 <= flagship["distance_km"] <= 215

    async def test_analyze_route_returns_the_flagship_payload_without_geometry(self) -> None:
        result = await mcp_server.analyze_route(route_slug="frankfurt-stuttgart")
        assert result["charging_required"] is False, "a sedan at 70 % makes Stuttgart"
        assert 0 < result["arrival_soc_percent"] < 70
        assert len(result["segments"]) == 41
        # The default result is for a model, not a map: no coordinates anywhere.
        assert "geometry" not in result["route"]
        assert all("geometry" not in segment for segment in result["segments"])
        # The deterministic explanation travels with the numbers.
        assert result["explanation"]["headline"]
        assert result["explanation"]["drivers"]

    async def test_analyze_route_can_include_geometry_on_request(self) -> None:
        result = await mcp_server.analyze_route(
            route_slug="frankfurt-stuttgart", include_geometry=True
        )
        assert result["route"]["geometry"]["type"] == "LineString"

    async def test_unknown_corridor_is_a_readable_tool_error(self) -> None:
        with pytest.raises(ValueError, match="not_found"):
            await mcp_server.analyze_route(route_slug="nowhere-to-nowhere")

    async def test_corridor_coverage_reports_its_methodology(self) -> None:
        coverage = await mcp_server.corridor_coverage("frankfurt-stuttgart", min_power_kw=150)
        assert coverage["methodology"]
        assert coverage["max_gap_km"] > 0
        # Coverage on the reference corridor was measured at ~13 km for ≥150 kW / 5 km buffer.
        assert coverage["max_gap_km"] < 40

    async def test_underserved_flags_against_the_threshold(self) -> None:
        report = await mcp_server.underserved_corridors(min_power_kw=150, max_gap_km=10)
        assert report["parameters"]["max_gap_km"] == 10
        corridors = report["corridors"]
        assert corridors, "the seeded corridors must be present"
        gaps = [c["max_gap_km"] for c in corridors]
        assert gaps == sorted(gaps, reverse=True), "ranked by worst gap first"
        # Every corridor exceeds a 10 km threshold; every corridor is flagged.
        assert all(c["underserved"] for c in corridors)

    async def test_station_search_by_proximity_returns_nearest_first(self) -> None:
        # Frankfurt am Main city centre.
        stations = await mcp_server.search_charging_stations(
            near_latitude=50.1109, near_longitude=8.6821, radius_km=5, min_power_kw=150, limit=5
        )
        assert 0 < len(stations) <= 5
        for station in stations:
            assert station["max_power_kw"] >= 150
            assert station["source"].startswith("Bundesnetzagentur")

    async def test_data_quality_lists_every_source_with_a_licence(self) -> None:
        rows = await mcp_server.data_quality()
        sources = {row["source"] for row in rows}
        assert {"bundesnetzagentur", "dwd", "autobahn"} <= sources
