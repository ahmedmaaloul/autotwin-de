"""The enums are a wire format shared by PostgreSQL, the API and the TypeScript frontend.

These tests pin the *values*, not just the behaviour: renaming a member is a breaking change to
three layers at once, and it should fail here loudly rather than at runtime in a migration.
"""

from __future__ import annotations

import pytest

from autotwin_contracts.enums import (
    Bundesland,
    ChargingCategory,
    ConnectorType,
    DataOrigin,
    EnergyIntensity,
    RoadClass,
    TrafficSeverity,
)


class TestWireValues:
    @pytest.mark.parametrize(
        ("member", "value"),
        [
            (DataOrigin.official, "official"),
            (DataOrigin.simulated, "simulated"),
            (DataOrigin.derived, "derived"),
            (ChargingCategory.ultra_fast, "ultra_fast"),
            (TrafficSeverity.severe, "severe"),
            (RoadClass.motorway, "motorway"),
        ],
    )
    def test_serialises_as_its_value(self, member: str, value: str) -> None:
        assert member == value
        assert str(member) == value


class TestBundesland:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Baden-Württemberg", Bundesland.BW),
            ("Baden-Wuerttemberg", Bundesland.BW),
            ("baden-wurttemberg", Bundesland.BW),
            ("BW", Bundesland.BW),
            ("DE-BW", Bundesland.BW),
            ("Nordrhein-Westfalen", Bundesland.NW),
            ("NRW", Bundesland.NW),
            ("Bayern", Bundesland.BY),
            ("Bavaria", Bundesland.BY),
            ("Mecklenburg-Vorpommern", Bundesland.MV),
            ("Thüringen", Bundesland.TH),
        ],
    )
    def test_normalises_real_spellings(self, raw: str, expected: Bundesland) -> None:
        """The Bundesnetzagentur file, DWD station list and OSM all spell these differently."""
        assert Bundesland.from_name(raw) is expected

    def test_unknown_returns_none_rather_than_guessing(self) -> None:
        # An unmapped state is a data-quality finding, not a crash — and never a wrong guess.
        assert Bundesland.from_name("Elsass") is None
        assert Bundesland.from_name("") is None
        assert Bundesland.from_name(None) is None

    def test_every_member_has_a_german_label(self) -> None:
        for member in Bundesland:
            assert member.label_de
            assert Bundesland.from_name(member.label_de) is member

    def test_there_are_sixteen(self) -> None:
        assert len(list(Bundesland)) == 16


class TestChargingCategory:
    @pytest.mark.parametrize(
        ("kw", "expected"),
        [
            (None, ChargingCategory.normal),
            (3.7, ChargingCategory.normal),
            (11.0, ChargingCategory.normal),
            (21.9, ChargingCategory.normal),
            (22.0, ChargingCategory.fast),
            (50.0, ChargingCategory.fast),
            (149.9, ChargingCategory.fast),
            (150.0, ChargingCategory.ultra_fast),
            (350.0, ChargingCategory.ultra_fast),
        ],
    )
    def test_boundaries(self, kw: float | None, expected: ChargingCategory) -> None:
        assert ChargingCategory.from_power(kw) is expected


class TestConnectorType:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # Verbatim spellings from the Bundesnetzagentur Ladesäulenregister.
            ("AC Typ 2 Steckdose", ConnectorType.type2),
            ("AC Typ 2 Fahrzeugkupplung", ConnectorType.type2),
            ("DC Fahrzeugkupplung Typ Combo 2 (CCS)", ConnectorType.ccs),
            ("DC CHAdeMO", ConnectorType.chademo),
            ("AC Schuko", ConnectorType.schuko),
            ("AC CEE 5 polig", ConnectorType.cee),
            ("", ConnectorType.unknown),
            (None, ConnectorType.unknown),
        ],
    )
    def test_parses_german_source_spellings(self, raw: str | None, expected: ConnectorType) -> None:
        assert ConnectorType.parse(raw) is expected

    def test_dc_standard_wins_over_its_signalling_connector(self) -> None:
        """A CCS cell mentions Type 2 too; the DC standard is the useful classification."""
        assert ConnectorType.parse("DC Kupplung Combo, AC Typ 2") is ConnectorType.ccs


class TestRoadClass:
    def test_ordinal_is_monotonic_by_road_importance(self) -> None:
        assert RoadClass.motorway.ordinal > RoadClass.trunk.ordinal
        assert RoadClass.trunk.ordinal > RoadClass.primary.ordinal
        assert RoadClass.primary.ordinal > RoadClass.secondary.ordinal
        assert RoadClass.unknown.ordinal == 0

    @pytest.mark.parametrize(
        ("tag", "expected"),
        [
            ("motorway", RoadClass.motorway),
            ("motorway_link", RoadClass.motorway),
            ("trunk", RoadClass.trunk),
            ("residential", RoadClass.residential),
            ("footway", RoadClass.unknown),
            (None, RoadClass.unknown),
        ],
    )
    def test_from_osm(self, tag: str | None, expected: RoadClass) -> None:
        assert RoadClass.from_osm(tag) is expected


class TestTrafficSeverity:
    def test_delay_factor_increases_with_severity(self) -> None:
        factors = [s.delay_factor for s in (
            TrafficSeverity.low, TrafficSeverity.moderate, TrafficSeverity.high, TrafficSeverity.severe
        )]
        assert factors == sorted(factors)
        # Free-flow traffic must not make a journey faster than the routing engine said.
        assert TrafficSeverity.low.delay_factor >= 1.0


class TestEnergyIntensity:
    @pytest.mark.parametrize(
        ("actual", "nominal", "expected"),
        [
            (14.0, 16.0, EnergyIntensity.low),
            (16.0, 16.0, EnergyIntensity.medium),
            (17.5, 16.0, EnergyIntensity.medium),
            (18.0, 16.0, EnergyIntensity.high),
            (25.0, 16.0, EnergyIntensity.critical),
        ],
    )
    def test_thresholds(self, actual: float, nominal: float, expected: EnergyIntensity) -> None:
        assert EnergyIntensity.from_ratio(actual, nominal) is expected
