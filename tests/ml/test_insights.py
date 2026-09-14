"""Deterministic energy-explanation generator — BUILD_SPEC §11.

The generator is a **sequential counterfactual ladder**: it re-runs the physical model with one
group of real conditions restored at a time and reads the differences off. Two properties make
that testable without pinning implementation output:

1. **The drivers sum exactly to the total deviation from nominal.** A single-factor scheme drops
   the interaction terms and does not have this property, so it is the sharpest available check
   that the ladder is still a ladder.
2. **Each driver has a physically predictable sign and rank.** A cold trip must blame the
   temperature; a fast one the speed profile; a hilly one the gradient; and a congested motorway
   must show a *negative* traffic term, because crawling uses less energy per kilometre than
   130 km/h does.

Every route below is synthetic and homogeneous, so the expected attribution can be reasoned out
from the road-load equations rather than looked up.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from autotwin_contracts.enums import (
    DataOrigin,
    RoadClass,
    SourceSystem,
    TrafficEventType,
    TrafficSeverity,
    WeatherCondition,
)
from autotwin_contracts.geo import Coordinate
from autotwin_contracts.records import (
    ProvenanceInfo,
    TrafficEventRecord,
    WeatherRecord,
)
from autotwin_contracts.vehicles import VehicleProfile, get_vehicle_profile
from autotwin_core.errors import ValidationError
from autotwin_ml.baseline import PhysicalEnergyModel
from autotwin_ml.insights import (
    DriverDirection,
    EnergyExplanation,
    TrafficContext,
    WeatherContext,
    explain_route_energy,
)
from autotwin_ml.types import SegmentConditions

MODEL = PhysicalEnergyModel()
ROUTE_SEGMENTS = 40
SEGMENT_KM = 5.0
"""40 x 5 km = 200 km, the order of a Frankfurt-Stuttgart corridor (203.4 km in the seed data)."""


@pytest.fixture(scope="module")
def sedan() -> VehicleProfile:
    """``sedan_ev``: 16.5 kWh/100 km nominal — the reference every driver is measured against."""
    return get_vehicle_profile("sedan_ev")


def uniform_route(**overrides: object) -> list[SegmentConditions]:
    """A 200 km route of identical segments, so the attribution has one cause to find."""
    kwargs: dict[str, object] = {
        "distance_km": SEGMENT_KM,
        "road_class": RoadClass.motorway,
    }
    kwargs.update(overrides)
    return [SegmentConditions(**kwargs) for _ in range(ROUTE_SEGMENTS)]  # type: ignore[arg-type]


def explain(
    profile: VehicleProfile,
    segments: list[SegmentConditions],
    **kwargs: object,
) -> EnergyExplanation:
    """Explain a route whose energies come from the physical model itself.

    With the physical model on both sides the ``model_correction`` residual is exactly zero,
    which isolates the ladder from any question about the served prediction.
    """
    results = [MODEL.segment_energy(profile, segment) for segment in segments]
    return explain_route_energy(profile, segments, results, **kwargs)  # type: ignore[arg-type]


def driver_by_factor(explanation: EnergyExplanation, factor: str) -> float:
    """Signed contribution of one factor in percentage points, or 0.0 when it was filtered."""
    for driver in explanation.drivers:
        if driver.factor == factor:
            return driver.delta_percent
    return 0.0


def largest_positive_factor(explanation: EnergyExplanation) -> str:
    """The factor with the biggest *upward* contribution."""
    positives = [d for d in explanation.drivers if d.delta_percent > 0.0]
    assert positives, "expected at least one driver that raised consumption"
    return max(positives, key=lambda driver: driver.delta_percent).factor


class TestAttribution:
    """Each synthetic route must be blamed on the condition that actually made it expensive."""

    def test_a_cold_trip_blames_the_temperature(self, sedan: VehicleProfile) -> None:
        """200 km at 120 km/h and -10 °C.

        Independent reasoning: the trip takes 1.67 h, so the 3.5 kW heater alone adds 5.8 kWh —
        about 18 % of the 33 kWh nominal for this distance — before the +20 % cold-battery
        factor and the +11 % air density are counted. Nothing else on this route moved, so the
        temperature has to be the dominant driver.
        """
        explanation = explain(sedan, uniform_route(speed_kmh=120.0, outside_temperature_c=-10.0))
        assert largest_positive_factor(explanation) == "temperature"
        assert driver_by_factor(explanation, "temperature") > 30.0

    def test_a_fast_trip_blames_the_speed_profile(self, sedan: VehicleProfile) -> None:
        """200 km at 160 km/h, mild and flat.

        Drag power grows with v³, so a 160 km/h cruise costs roughly (160/120)³ = 2.4x the aero
        power of a 120 km/h one. With the weather neutral there is nothing else to blame.
        """
        explanation = explain(sedan, uniform_route(speed_kmh=160.0))
        assert largest_positive_factor(explanation) == "speed_profile"
        assert driver_by_factor(explanation, "speed_profile") > 40.0
        assert driver_by_factor(explanation, "temperature") == pytest.approx(0.0, abs=1e-9)

    def test_a_hilly_trip_blames_the_gradient(self, sedan: VehicleProfile) -> None:
        """Alternating +5 % and -5 % over 200 km — the route ends where it started.

        Up-and-down is not free: 250 m of climb per 5 km segment costs ``mgh/0.90`` and the
        matching descent returns at most ``mgh·0.65``, with the segment total floored at zero.
        A model that netted the elevation out would report no gradient driver at all.
        """
        segments = [
            SegmentConditions(
                speed_kmh=100.0,
                distance_km=SEGMENT_KM,
                gradient_percent=5.0 if index % 2 == 0 else -5.0,
                road_class=RoadClass.primary,
            )
            for index in range(ROUTE_SEGMENTS)
        ]
        explanation = explain(sedan, segments)
        assert largest_positive_factor(explanation) == "gradient"
        assert driver_by_factor(explanation, "gradient") > 20.0

    def test_a_headwind_route_blames_the_wind(self, sedan: VehicleProfile) -> None:
        """A steady 10 m/s headwind at 100 km/h raises the air speed from 27.8 to 37.8 m/s,
        so the drag force grows by (37.8/27.8)² = 1.85x — visible, and attributable."""
        explanation = explain(
            sedan, uniform_route(speed_kmh=100.0, headwind_ms=10.0, wind_speed_ms=10.0)
        )
        assert driver_by_factor(explanation, "wind") > 5.0

    def test_the_auxiliary_driver_is_the_base_load_times_the_driving_time(
        self, sedan: VehicleProfile
    ) -> None:
        """0.35 kW over 200 km at 120 km/h = 0.583 kWh, i.e. 1.77 % of the 33 kWh nominal.

        Computed here from the constant and the duration, with no reference to the ladder.
        """
        explanation = explain(sedan, uniform_route(speed_kmh=120.0))
        hours = 200.0 / 120.0
        nominal_kwh = sedan.nominal_consumption_kwh_100km * 200.0 / 100.0
        expected_percent = 0.35 * hours / nominal_kwh * 100.0
        assert expected_percent == pytest.approx(1.77, abs=0.01)
        assert driver_by_factor(explanation, "auxiliary") == pytest.approx(
            expected_percent, rel=1e-6
        )


class TestCongestionLowersConsumption:
    """The counter-intuitive one, and exactly what a regression would silently invert."""

    def test_congestion_on_a_motorway_produces_a_negative_traffic_contribution(
        self, sedan: VehicleProfile
    ) -> None:
        """Crawling at 65 km/h uses far less energy per kilometre than 130 km/h.

        The ladder builds the no-traffic counterfactual by inverting the delay factor: a
        ``severe`` segment observed at 65 km/h would have been driven at 65 · 2.0 = 130 km/h in
        free flow. Drag power falls with the cube of speed, so the jam is an energy *saving* —
        it costs time, not kilowatt-hours. Reporting congestion as a consumption *increase*
        (the intuitive but wrong answer, and what a sign error would give) would make the route
        page contradict itself, because the same page shows the trip getting cheaper.
        """
        segments = uniform_route(speed_kmh=65.0, traffic_severity=TrafficSeverity.severe)
        explanation = explain(sedan, segments)
        traffic_percent = driver_by_factor(explanation, "traffic")
        assert traffic_percent < 0.0
        assert explanation.actual_kwh_100km < sedan.nominal_consumption_kwh_100km

    @pytest.mark.parametrize(
        "severity",
        [TrafficSeverity.moderate, TrafficSeverity.high, TrafficSeverity.severe],
    )
    def test_worse_traffic_saves_more_energy_and_costs_more_time(
        self, sedan: VehicleProfile, severity: TrafficSeverity
    ) -> None:
        """Monotone in the delay factor: 1.15 / 1.4 / 2.0 all point the same way."""
        explanation = explain(sedan, uniform_route(speed_kmh=60.0, traffic_severity=severity))
        assert driver_by_factor(explanation, "traffic") < 0.0

    def test_free_flowing_traffic_contributes_nothing(self, sedan: VehicleProfile) -> None:
        """``low`` has a delay factor of 1.0, so the counterfactual equals the actual.

        Self-consistency: if this produced a non-zero driver, the ladder would be inventing a
        traffic effect on a clear road.
        """
        explanation = explain(
            sedan, uniform_route(speed_kmh=120.0, traffic_severity=TrafficSeverity.low)
        )
        assert driver_by_factor(explanation, "traffic") == pytest.approx(0.0, abs=1e-9)

    def test_the_traffic_detail_names_the_extra_time(self, sedan: VehicleProfile) -> None:
        """A jam costs time; the clause has to say how much, in both languages."""
        segments = uniform_route(speed_kmh=65.0, traffic_severity=TrafficSeverity.severe)
        results = [MODEL.segment_energy(sedan, segment) for segment in segments]
        traffic = TrafficContext(
            event_count=3, worst_severity=TrafficSeverity.severe, blocked_count=1
        )
        explanation = explain_route_energy(sedan, segments, results, traffic=traffic)
        detail_de = next(d.detail_de for d in explanation.drivers if d.factor == "traffic")
        detail_en = next(d.detail_en for d in explanation.drivers if d.factor == "traffic")
        assert "3 Meldung(en)" in detail_de
        assert "severe" in detail_de
        assert "min Mehrzeit" in detail_de
        assert "3 report(s)" in detail_en
        assert "min of extra time" in detail_en


@pytest.fixture(scope="module")
def cold_explanation(sedan: VehicleProfile) -> EnergyExplanation:
    """200 km of Autobahn at -10 °C — a winter corridor with one dominant cause."""
    return explain(sedan, uniform_route(speed_kmh=120.0, outside_temperature_c=-10.0))


class TestDriverListShape:
    """Ordering, labels, filtering — the contract the frontend renders against."""

    def test_drivers_are_sorted_by_absolute_contribution(
        self, cold_explanation: EnergyExplanation
    ) -> None:
        """Largest first, regardless of sign — a -40 % driver outranks a +5 % one."""
        magnitudes = [abs(driver.delta_percent) for driver in cold_explanation.drivers]
        assert magnitudes == sorted(magnitudes, reverse=True)

    def test_every_driver_is_bilingual(self, cold_explanation: EnergyExplanation) -> None:
        """BUILD_SPEC §12: no hardcoded user-facing string, and both locales are first class.

        A missing ``label_en`` would render as an empty chip on the English page.
        """
        for driver in cold_explanation.drivers:
            assert driver.label_de.strip()
            assert driver.label_en.strip()
            assert driver.label_de != driver.label_en
            assert driver.detail_de.strip()
            assert driver.detail_en.strip()

    def test_german_details_use_a_comma_decimal_separator(
        self, cold_explanation: EnergyExplanation
    ) -> None:
        """BUILD_SPEC §14. A point-separated number in German copy is the classic i18n leak."""
        temperature = next(d for d in cold_explanation.drivers if d.factor == "temperature")
        assert "," in temperature.detail_de
        assert "°C" in temperature.detail_de
        assert "." in temperature.detail_en

    def test_direction_matches_the_sign(self, cold_explanation: EnergyExplanation) -> None:
        """The frontend switches on this for the arrow and the colour token."""
        for driver in cold_explanation.drivers:
            if driver.delta_percent > 0.5:
                assert driver.direction is DriverDirection.increase
            elif driver.delta_percent < -0.5:
                assert driver.direction is DriverDirection.decrease
            else:
                assert driver.direction is DriverDirection.neutral

    def test_no_driver_below_the_reporting_threshold_is_emitted(
        self, sedan: VehicleProfile
    ) -> None:
        """Noise is dropped: on this route the wind and gradient terms are exactly zero."""
        explanation = explain(
            sedan,
            uniform_route(speed_kmh=120.0, outside_temperature_c=-10.0),
            min_delta_percent=1.0,
        )
        assert explanation.drivers
        for driver in explanation.drivers:
            assert abs(driver.delta_percent) >= 1.0
        emitted = {driver.factor for driver in explanation.drivers}
        assert "wind" not in emitted
        assert "gradient" not in emitted

    def test_the_list_is_never_empty_even_when_everything_is_noise(
        self, sedan: VehicleProfile
    ) -> None:
        """An explanation with no drivers would render an empty panel and explain nothing.

        The documented fallback keeps the single largest driver whatever the threshold.
        """
        explanation = explain(sedan, uniform_route(speed_kmh=120.0), min_delta_percent=999.0)
        assert len(explanation.drivers) == 1

    def test_factors_are_unique(self, cold_explanation: EnergyExplanation) -> None:
        """Each rung of the ladder appears once; a duplicate would double-count its delta."""
        factors = [driver.factor for driver in cold_explanation.drivers]
        assert len(factors) == len(set(factors))

    def test_the_same_inputs_always_explain_the_same_way(self, sedan: VehicleProfile) -> None:
        """Determinism applies to the explanation as much as to the charging plan."""
        segments = uniform_route(speed_kmh=120.0, outside_temperature_c=-10.0)
        assert explain(sedan, segments) == explain(sedan, segments)


class TestAdditiveIdentity:
    """The property a naive single-factor decomposition does not have."""

    @pytest.mark.parametrize(
        ("label", "overrides"),
        [
            ("cold-motorway", {"speed_kmh": 120.0, "outside_temperature_c": -10.0}),
            ("hot-slow", {"speed_kmh": 50.0, "outside_temperature_c": 35.0}),
            ("windy", {"speed_kmh": 110.0, "headwind_ms": 8.0, "wind_speed_ms": 8.0}),
            (
                "jam",
                {"speed_kmh": 60.0, "traffic_severity": TrafficSeverity.high},
            ),
            ("climb", {"speed_kmh": 90.0, "gradient_percent": 3.0}),
        ],
    )
    def test_the_drivers_sum_to_the_total_deviation(
        self, sedan: VehicleProfile, label: str, overrides: dict[str, object]
    ) -> None:
        """Every interaction term is assigned to a rung; nothing is left over.

        Checked with the filter switched off, because filtering deliberately drops noise. If
        this ever failed, the route page would show a set of contributions that does not add up
        to the headline number it sits under.
        """
        explanation = explain(sedan, uniform_route(**overrides), min_delta_percent=0.0)
        total = sum(driver.delta_percent for driver in explanation.drivers)
        assert total == pytest.approx(explanation.total_delta_percent, abs=1e-9)

    def test_the_kwh_deltas_sum_too(self, sedan: VehicleProfile) -> None:
        """The percentages share a denominator, so the kWh have to reconcile as well."""
        explanation = explain(
            sedan, uniform_route(speed_kmh=120.0, outside_temperature_c=-5.0), min_delta_percent=0.0
        )
        nominal_kwh = sedan.nominal_consumption_kwh_100km * explanation.distance_km / 100.0
        actual_kwh = explanation.actual_kwh_100km * explanation.distance_km / 100.0
        assert sum(driver.delta_kwh for driver in explanation.drivers) == pytest.approx(
            actual_kwh - nominal_kwh, abs=1e-9
        )

    def test_a_served_prediction_that_differs_surfaces_as_a_model_correction(
        self, sedan: VehicleProfile
    ) -> None:
        """Whatever separates the caller's energies from the physical model is *named*.

        The ML regressor does not have to agree with the baseline, but the difference must not
        be hidden inside another driver — BUILD_SPEC §0.4. Here the served energies are 10 %
        above the physical model, so ``model_correction`` must carry that 10 %.
        """
        segments = uniform_route(speed_kmh=120.0)
        physical = [MODEL.segment_energy(sedan, segment) for segment in segments]
        served = [replace(result, kwh=result.kwh * 1.1) for result in physical]
        explanation = explain_route_energy(sedan, segments, served, min_delta_percent=0.0)

        physical_kwh = sum(result.kwh for result in physical)
        nominal_kwh = sedan.nominal_consumption_kwh_100km * explanation.distance_km / 100.0
        expected_percent = physical_kwh * 0.1 / nominal_kwh * 100.0
        assert driver_by_factor(explanation, "model_correction") == pytest.approx(
            expected_percent, rel=1e-9
        )

    def test_no_correction_when_the_served_energies_are_the_physical_ones(
        self, sedan: VehicleProfile
    ) -> None:
        """The last rung's conditions are the caller's conditions field for field, so a
        physical-model input must leave a residual of exactly zero."""
        explanation = explain(
            sedan,
            uniform_route(
                speed_kmh=95.0,
                outside_temperature_c=2.0,
                gradient_percent=1.5,
                headwind_ms=4.0,
                traffic_severity=TrafficSeverity.moderate,
            ),
            min_delta_percent=0.0,
        )
        assert driver_by_factor(explanation, "model_correction") == pytest.approx(0.0, abs=1e-9)


class TestHeadline:
    """One German sentence that is the true summary of the list below it."""

    def test_the_headline_is_german_and_carries_the_consumption_figure(
        self, sedan: VehicleProfile
    ) -> None:
        """Default locale is German (BUILD_SPEC §12), with a comma decimal separator."""
        explanation = explain(sedan, uniform_route(speed_kmh=120.0, outside_temperature_c=-10.0))
        figure_de = f"{explanation.actual_kwh_100km:.1f}".replace(".", ",")
        assert figure_de in explanation.headline_de
        assert "kWh/100 km" in explanation.headline_de
        assert "Normverbrauch" in explanation.headline_de
        assert explanation.headline == explanation.headline_de

    def test_the_english_headline_mirrors_it_with_a_point_separator(
        self, sedan: VehicleProfile
    ) -> None:
        explanation = explain(sedan, uniform_route(speed_kmh=120.0, outside_temperature_c=-10.0))
        assert f"{explanation.actual_kwh_100km:.1f}" in explanation.headline_en
        assert "nominal" in explanation.headline_en
        assert explanation.headline_en != explanation.headline_de

    def test_the_headline_names_the_direction_it_moved(self, sedan: VehicleProfile) -> None:
        """ "über" for a costly trip, "unter" for a cheap one — never the wrong one."""
        costly = explain(sedan, uniform_route(speed_kmh=160.0))
        cheap = explain(sedan, uniform_route(speed_kmh=60.0))
        assert costly.total_delta_percent > 0.0
        assert "über Normverbrauch" in costly.headline_de
        assert cheap.total_delta_percent < 0.0
        assert "unter Normverbrauch" in cheap.headline_de
        assert "above nominal" in costly.headline_en
        assert "below nominal" in cheap.headline_en

    def test_a_trip_close_to_nominal_says_so_instead_of_naming_a_direction(
        self, sedan: VehicleProfile
    ) -> None:
        """Below 3 % the sign is noise, and claiming a direction would overstate the model.

        The speed is tuned so the physical model lands within a couple of percent of the
        16.5 kWh/100 km datasheet figure — a steady 114 km/h, which is also a fair reading of
        what "nominal" means for a car whose WLTP figure averages a mixed cycle. The assertion
        is on the *behaviour at the threshold*, not on the particular speed.
        """
        explanation = explain(sedan, uniform_route(speed_kmh=114.0))
        assert abs(explanation.total_delta_percent) < 3.0
        assert "Normniveau" in explanation.headline_de
        assert "essentially at the nominal figure" in explanation.headline_en

    def test_the_headline_quotes_the_leading_drivers_from_the_list_below_it(
        self, sedan: VehicleProfile
    ) -> None:
        """The headline's job is to be the true summary of the list, not to be interesting.

        Every factor it names must also appear as a driver, and the top one must be named — a
        headline that blamed something the list does not show would be the worst kind of
        plausible-sounding output.
        """
        explanation = explain(sedan, uniform_route(speed_kmh=120.0, outside_temperature_c=-10.0))
        named = [
            driver for driver in explanation.drivers if driver.label_de in explanation.headline_de
        ]
        assert explanation.drivers[0] in named
        assert 1 <= len(named) <= 2
        for driver in named:
            assert driver.label_en in explanation.headline_en
            assert driver.direction is not DriverDirection.neutral

    def test_the_method_note_disclaims_the_llm(self, sedan: VehicleProfile) -> None:
        """BUILD_SPEC §11: no LLM is involved, and the UI says so where a reader can check it."""
        explanation = explain(sedan, uniform_route(speed_kmh=120.0))
        assert "No language model" in explanation.method
        assert "counterfactual" in explanation.method


class TestExplanationValidation:
    """Failure modes that would otherwise produce a confidently wrong explanation."""

    def test_misaligned_sequences_are_refused(self, sedan: VehicleProfile) -> None:
        """An index-alignment bug silently attributes one segment's energy to another's
        conditions — the kind of mistake that never raises on its own."""
        segments = uniform_route(speed_kmh=120.0)
        results = [MODEL.segment_energy(sedan, segment) for segment in segments[:-1]]
        with pytest.raises(ValidationError):
            explain_route_energy(sedan, segments, results)

    def test_a_route_with_no_distance_cannot_be_explained(self, sedan: VehicleProfile) -> None:
        """Every driver is a percentage of a per-distance nominal; there is no denominator."""
        segments = [SegmentConditions(speed_kmh=0.0, distance_km=0.0, duration_s=60.0)]
        results = [MODEL.segment_energy(sedan, segments[0])]
        with pytest.raises(ValidationError):
            explain_route_energy(sedan, segments, results)

    @pytest.mark.parametrize("baseline_nominal", [0.0, -5.0])
    def test_a_non_positive_reference_is_refused(
        self, sedan: VehicleProfile, baseline_nominal: float
    ) -> None:
        """Dividing by it would produce infinities or sign-flipped drivers."""
        segments = uniform_route(speed_kmh=120.0)
        results = [MODEL.segment_energy(sedan, segment) for segment in segments]
        with pytest.raises(ValidationError):
            explain_route_energy(sedan, segments, results, baseline_nominal=baseline_nominal)

    def test_an_explicit_reference_overrides_the_datasheet(self, sedan: VehicleProfile) -> None:
        """A fleet's own measured average is a legitimate baseline to explain against."""
        segments = uniform_route(speed_kmh=120.0)
        results = [MODEL.segment_energy(sedan, segment) for segment in segments]
        explanation = explain_route_energy(sedan, segments, results, baseline_nominal=25.0)
        assert explanation.nominal_kwh_100km == 25.0
        assert explanation.total_delta_percent < 0.0  # 17.6 against 25.0 is well below

    def test_an_empty_route_is_refused_rather_than_returning_zeroes(
        self, sedan: VehicleProfile
    ) -> None:
        with pytest.raises(ValidationError):
            explain_route_energy(sedan, [], [])


class TestWeatherContext:
    """The corridor weather summary that enriches the detail text."""

    @staticmethod
    def observation(
        *,
        temperature_c: float | None,
        precipitation_mm: float | None = None,
        wind_speed_ms: float | None = None,
        condition: WeatherCondition = WeatherCondition.unknown,
    ) -> WeatherRecord:
        return WeatherRecord(
            observed_at=datetime(2026, 9, 14, 12, tzinfo=UTC),
            coordinate=Coordinate(latitude=50.0, longitude=8.0),
            temperature_c=temperature_c,
            precipitation_mm=precipitation_mm,
            wind_speed_ms=wind_speed_ms,
            condition=condition,
            provenance=ProvenanceInfo(
                source=SourceSystem.dwd,
                data_origin=DataOrigin.official,
                ingested_at=datetime(2026, 9, 14, 12, tzinfo=UTC),
            ),
        )

    def test_missing_values_are_skipped_rather_than_read_as_zero(self) -> None:
        """A station reporting nothing is not a station reporting 0.0 °C.

        Averaging the difference away is how a sensor outage turns into a confident wrong
        statement: with -4 and -6 observed and one station silent, the mean is -5, not -3.33.
        """
        context = WeatherContext.from_records(
            [
                self.observation(temperature_c=-4.0),
                self.observation(temperature_c=-6.0),
                self.observation(temperature_c=None),
            ]
        )
        assert context.mean_temperature_c == pytest.approx(-5.0)
        assert context.min_temperature_c == pytest.approx(-6.0)

    def test_an_empty_corridor_reports_unknown_rather_than_zero(self) -> None:
        """``None`` means "not observed"; 0.0 °C would be a claim about a freezing corridor."""
        context = WeatherContext.from_records([])
        assert context.mean_temperature_c is None
        assert context.min_temperature_c is None
        assert context.condition is WeatherCondition.unknown

    def test_a_single_observation_is_its_own_mean_and_minimum(self) -> None:
        context = WeatherContext.from_records([self.observation(temperature_c=7.5)])
        assert context.mean_temperature_c == pytest.approx(7.5)
        assert context.min_temperature_c == pytest.approx(7.5)

    def test_precipitation_is_summed_and_wind_is_averaged(self) -> None:
        """Rain over a corridor accumulates; wind speed does not."""
        context = WeatherContext.from_records(
            [
                self.observation(temperature_c=5.0, precipitation_mm=0.4, wind_speed_ms=4.0),
                self.observation(temperature_c=5.0, precipitation_mm=0.6, wind_speed_ms=8.0),
            ]
        )
        assert context.precipitation_mm == pytest.approx(1.0)
        assert context.mean_wind_speed_ms == pytest.approx(6.0)

    def test_the_worst_condition_wins_not_the_most_frequent(self) -> None:
        """One snow observation matters more to a driver than twenty clear ones."""
        context = WeatherContext.from_records(
            [
                self.observation(temperature_c=1.0, condition=WeatherCondition.clear),
                self.observation(temperature_c=1.0, condition=WeatherCondition.clear),
                self.observation(temperature_c=1.0, condition=WeatherCondition.clear),
                self.observation(temperature_c=1.0, condition=WeatherCondition.snow),
            ]
        )
        assert context.condition is WeatherCondition.snow

    @pytest.mark.parametrize(
        ("observed", "expected"),
        [
            ((WeatherCondition.rain, WeatherCondition.storm), WeatherCondition.storm),
            ((WeatherCondition.fog, WeatherCondition.rain), WeatherCondition.fog),
            ((WeatherCondition.clouds, WeatherCondition.clear), WeatherCondition.clouds),
            ((WeatherCondition.unknown,), WeatherCondition.unknown),
        ],
    )
    def test_the_severity_order_is_total_and_deterministic(
        self, observed: tuple[WeatherCondition, ...], expected: WeatherCondition
    ) -> None:
        context = WeatherContext.from_records(
            [self.observation(temperature_c=1.0, condition=condition) for condition in observed]
        )
        assert context.condition is expected

    def test_the_corridor_summary_wins_over_the_per_segment_temperature(
        self, sedan: VehicleProfile
    ) -> None:
        """The explanation talks about the *trip*; reconstructing that from segments would
        weight a 1 km urban stretch like a 12 km motorway one."""
        segments = uniform_route(speed_kmh=120.0, outside_temperature_c=-10.0)
        results = [MODEL.segment_energy(sedan, segment) for segment in segments]
        explanation = explain_route_energy(
            sedan, segments, results, weather=WeatherContext(mean_temperature_c=-7.3)
        )
        detail = next(d.detail_de for d in explanation.drivers if d.factor == "temperature")
        assert "-7 °C" in detail


class TestTrafficContext:
    """The corridor traffic summary."""

    @staticmethod
    def event(severity: TrafficSeverity, *, is_blocked: bool = False) -> TrafficEventRecord:
        return TrafficEventRecord(
            event_type=TrafficEventType.congestion,
            severity=severity,
            title="Stau zwischen zwei Anschlussstellen",
            coordinate=Coordinate(latitude=50.0, longitude=8.0),
            is_blocked=is_blocked,
            provenance=ProvenanceInfo(
                source=SourceSystem.autobahn,
                data_origin=DataOrigin.official,
                ingested_at=datetime(2026, 9, 14, 12, tzinfo=UTC),
            ),
        )

    def test_an_empty_corridor_has_no_worst_severity(self) -> None:
        """``None`` rather than ``low``: no report is not the same as a report of clear roads."""
        context = TrafficContext.from_records([])
        assert context.event_count == 0
        assert context.worst_severity is None
        assert context.blocked_count == 0

    def test_the_worst_severity_is_taken_not_the_first(self) -> None:
        """Ordered by ``TrafficSeverity.ordinal``, so ``severe`` beats ``moderate`` wherever
        it appears in the list."""
        context = TrafficContext.from_records(
            [
                self.event(TrafficSeverity.severe),
                self.event(TrafficSeverity.low),
                self.event(TrafficSeverity.moderate),
            ]
        )
        assert context.event_count == 3
        assert context.worst_severity is TrafficSeverity.severe

    def test_full_closures_are_counted_separately(self) -> None:
        """A Vollsperrung is a different fact from a jam and the UI reports it separately."""
        context = TrafficContext.from_records(
            [
                self.event(TrafficSeverity.high, is_blocked=True),
                self.event(TrafficSeverity.high, is_blocked=False),
                self.event(TrafficSeverity.severe, is_blocked=True),
            ]
        )
        assert context.blocked_count == 2

    def test_a_single_event_is_its_own_worst(self) -> None:
        context = TrafficContext.from_records([self.event(TrafficSeverity.moderate)])
        assert context.worst_severity is TrafficSeverity.moderate


class TestRegressions:
    """Defects found while writing this suite, now fixed and pinned."""

    def test_an_explicit_duration_must_not_be_attributed_to_the_wind(
        self, sedan: VehicleProfile
    ) -> None:
        """With no wind anywhere, the wind driver has to be zero.

        The segments below are 5 km at 100 km/h — 180 s of free-flow travel — declared as 600 s
        windows, the shape a stop-and-go telemetry aggregate has. ``headwind_ms`` is 0.0 on
        every one of them, so a non-zero ``wind`` contribution cannot be anything but a
        misattribution, and it would be rendered to a driver as "Gegenwind".
        """
        segments = [
            SegmentConditions(
                speed_kmh=100.0,
                distance_km=SEGMENT_KM,
                duration_s=600.0,
                headwind_ms=0.0,
                road_class=RoadClass.motorway,
            )
            for _ in range(ROUTE_SEGMENTS)
        ]
        explanation = explain(sedan, segments, min_delta_percent=0.0)
        assert driver_by_factor(explanation, "wind") == pytest.approx(0.0, abs=1e-9)
