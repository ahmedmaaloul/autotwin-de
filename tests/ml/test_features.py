"""Feature contract of the energy model — BUILD_SPEC §10.2.

``FEATURE_NAMES`` is the single source of truth for what the regressor sees **and in which
order**, and four independent consumers depend on that order agreeing: the training-set builder,
the persisted booster (which learned column *positions*), the prediction endpoint and the SHAP
explainer. A silent reordering between any two of them produces a model that predicts
confidently and wrongly, and nothing raises — which is why the list is pinned literally below
rather than checked for length alone.
"""

from __future__ import annotations

import math

import numpy as np
import polars as pl
import pytest

from autotwin_contracts.enums import RoadClass, TrafficSeverity
from autotwin_contracts.vehicles import VehicleProfile, get_vehicle_profile
from autotwin_core.errors import ValidationError
from autotwin_ml.baseline import PhysicalEnergyModel
from autotwin_ml.constants import DEFAULT_PHYSICS
from autotwin_ml.features import (
    DEFAULT_SOC_PERCENT,
    FEATURE_LABELS,
    FEATURE_NAMES,
    GROUP_COLUMN,
    INSIGHT_FACTOR_BY_FEATURE,
    TARGET_COLUMN,
    build_feature_frame,
    default_speed_limit_kmh,
    feature_array,
    feature_batch,
    feature_matrix,
    features_from_conditions,
    validate_feature_frame,
)
from autotwin_ml.insights import explain_route_energy
from autotwin_ml.types import SegmentConditions

EXPECTED_FEATURE_NAMES = (
    "speed_kmh",
    "avg_speed_kmh",
    "speed_std_kmh",
    "acceleration_abs_mean_ms2",
    "accel_events_per_km",
    "outside_temperature_c",
    "battery_temperature_c",
    "soc_percent",
    "road_class_ordinal",
    "speed_limit_kmh",
    "traffic_severity_ordinal",
    "precipitation_mm",
    "wind_speed_ms",
    "gradient_percent",
    "segment_distance_km",
    "mass_kg",
    "drag_area",
    "nominal_consumption_kwh_100km",
    "hvac_load_kw",
    "is_motorway",
)
"""Transcribed by hand from BUILD_SPEC §10.2, not copied from the implementation."""


@pytest.fixture(scope="module")
def sedan() -> VehicleProfile:
    return get_vehicle_profile("sedan_ev")


def valid_frame(rows: int = 2, **overrides: object) -> pl.DataFrame:
    """A frame that passes validation, with named columns optionally sabotaged."""
    data: dict[str, object] = {
        name: pl.Series(name, [1.0] * rows, dtype=pl.Float64) for name in FEATURE_NAMES
    }
    data.update(overrides)
    return pl.DataFrame(data)


class TestFeatureContract:
    """The list itself: length, order, and the things that must not be in it."""

    def test_there_are_exactly_twenty_features(self) -> None:
        """BUILD_SPEC §10.2 names twenty. An artefact trained on 20 columns cannot consume 21."""
        assert len(FEATURE_NAMES) == 20

    def test_the_order_is_the_documented_one(self) -> None:
        """Pinned literally. Adding a feature is a new model version, not an edit — so this
        test *should* fail loudly when the contract changes, and the failure is the point."""
        assert FEATURE_NAMES == EXPECTED_FEATURE_NAMES

    def test_no_feature_is_listed_twice(self) -> None:
        """A duplicate would shift every later column by one and silently mispredict."""
        assert len(set(FEATURE_NAMES)) == len(FEATURE_NAMES)

    def test_the_target_and_group_columns_are_not_features(self) -> None:
        """Leaking the target into the design matrix produces a perfect model and no insight;
        leaking ``trip_id`` would let the booster memorise the grouped split."""
        assert TARGET_COLUMN not in FEATURE_NAMES
        assert GROUP_COLUMN not in FEATURE_NAMES
        assert TARGET_COLUMN == "energy_consumption_kwh_100km"
        assert GROUP_COLUMN == "trip_id"

    def test_every_feature_has_a_bilingual_label(self) -> None:
        """SHAP contributions are rendered per column; an unlabelled one is an empty table row."""
        assert set(FEATURE_LABELS) == set(FEATURE_NAMES)
        for name in FEATURE_NAMES:
            label_de, label_en = FEATURE_LABELS[name]
            assert label_de.strip()
            assert label_en.strip()

    def test_the_insight_mapping_only_names_real_features(self) -> None:
        assert set(INSIGHT_FACTOR_BY_FEATURE).issubset(set(FEATURE_NAMES))

    def test_the_insight_mapping_only_names_real_drivers(self, sedan: VehicleProfile) -> None:
        """Cross-checked against the factor ids the insight generator actually emits.

        The mapping exists so SHAP contributions can be folded into the counterfactual driver
        list; if it named a factor the ladder never produces, the fold would silently drop those
        contributions. The reference set is obtained by *running* the generator rather than by
        copying its private label table.
        """
        segments = [
            SegmentConditions(
                speed_kmh=110.0,
                distance_km=5.0,
                gradient_percent=1.0,
                outside_temperature_c=2.0,
                headwind_ms=3.0,
                traffic_severity=TrafficSeverity.moderate,
                road_class=RoadClass.motorway,
            )
            for _ in range(10)
        ]
        model = PhysicalEnergyModel()
        results = [model.segment_energy(sedan, segment) for segment in segments]
        emitted = {
            driver.factor
            for driver in explain_route_energy(
                sedan, segments, results, min_delta_percent=0.0
            ).drivers
        }
        assert set(INSIGHT_FACTOR_BY_FEATURE.values()).issubset(emitted)

    def test_the_mapping_is_deliberately_partial(self) -> None:
        """Six features describe the vehicle or the window, not a driver a trip could avoid.

        A consumer aggregating SHAP by factor must treat a missing key as "not attributable",
        so the absence is part of the contract and is asserted rather than assumed.
        """
        unmapped = set(FEATURE_NAMES) - set(INSIGHT_FACTOR_BY_FEATURE)
        assert unmapped == {
            "soc_percent",
            "segment_distance_km",
            "precipitation_mm",
            "mass_kg",
            "drag_area",
            "nominal_consumption_kwh_100km",
        }


class TestFrameValidation:
    """Garbage in must produce a named column, never a confident prediction."""

    def test_nan_is_rejected_and_the_column_is_named(self) -> None:
        """LightGBM would happily impute a NaN away and return a number — BUILD_SPEC §0.4."""
        with pytest.raises(ValidationError) as excinfo:
            build_feature_frame(valid_frame(wind_speed_ms=pl.Series([1.0, math.nan])))
        assert "wind_speed_ms" in str(excinfo.value)
        assert "NaN" in str(excinfo.value)
        assert excinfo.value.details["feature"] == "wind_speed_ms"

    def test_infinity_is_rejected_and_the_column_is_named(self) -> None:
        """An infinity is what a divide-by-zero upstream looks like by the time it gets here."""
        with pytest.raises(ValidationError) as excinfo:
            build_feature_frame(valid_frame(gradient_percent=pl.Series([1.0, math.inf])))
        assert "gradient_percent" in str(excinfo.value)
        assert "infinite" in str(excinfo.value)
        assert excinfo.value.details["feature"] == "gradient_percent"

    def test_negative_infinity_is_rejected_too(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            build_feature_frame(valid_frame(speed_kmh=pl.Series([1.0, -math.inf])))
        assert "speed_kmh" in str(excinfo.value)

    def test_a_null_is_rejected_and_counted(self) -> None:
        """A NULL from Parquet is a different failure from a NaN and gets its own message."""
        column = pl.Series("soc_percent", [50.0, None], dtype=pl.Float64)
        with pytest.raises(ValidationError) as excinfo:
            build_feature_frame(valid_frame(soc_percent=column))
        assert "soc_percent" in str(excinfo.value)
        assert excinfo.value.details["null_count"] == 1

    def test_a_missing_column_names_every_one_that_is_missing(self) -> None:
        """Naming all of them at once saves the caller a round of whack-a-mole."""
        frame = valid_frame().drop("hvac_load_kw", "is_motorway")
        with pytest.raises(ValidationError) as excinfo:
            build_feature_frame(frame)
        # Reported in FEATURE_NAMES order, so the list is deterministic.
        assert excinfo.value.details["missing_features"] == ["hvac_load_kw", "is_motorway"]

    def test_an_empty_frame_is_caught_before_the_dtype_check(self) -> None:
        """ "column 'speed_kmh' has dtype Null" is a confusing way to say "no rows"."""
        with pytest.raises(ValidationError) as excinfo:
            build_feature_frame(valid_frame(rows=0))
        assert "no rows" in str(excinfo.value)

    def test_a_string_column_is_a_caller_error_not_a_cast(self) -> None:
        """An enum that was never mapped through ``.ordinal`` arrives as a string.

        Checked *before* the cast so the caller gets a 422 naming the feature rather than
        polars' own ``InvalidOperationError`` reaching the API as a 500.
        """
        frame = valid_frame(road_class_ordinal=pl.Series(["motorway", "primary"]))
        with pytest.raises(ValidationError) as excinfo:
            build_feature_frame(frame)
        assert "road_class_ordinal" in str(excinfo.value)
        assert "non-numeric" in str(excinfo.value)

    def test_the_context_label_reaches_the_message(self) -> None:
        """So an error from the prediction endpoint does not read "training frame"."""
        with pytest.raises(ValidationError) as excinfo:
            build_feature_frame(valid_frame(rows=0), context="predict request")
        assert "predict request" in str(excinfo.value)

    def test_a_valid_frame_validates_silently(self) -> None:
        validate_feature_frame(valid_frame())


class TestFrameProjection:
    """``build_feature_frame`` is what guarantees the column order downstream."""

    def test_columns_come_back_in_the_contract_order(self) -> None:
        """Handed the columns in reverse, the projection must still emit them in §10.2 order —
        this is the single check standing between a shuffled Parquet and a wrong model."""
        shuffled = pl.DataFrame({name: [1.0] for name in reversed(FEATURE_NAMES)})
        assert tuple(build_feature_frame(shuffled).columns) == FEATURE_NAMES

    def test_extra_columns_are_dropped(self) -> None:
        """A training frame carries the target and the trip id; the matrix must not."""
        frame = valid_frame().with_columns(
            pl.lit(17.5).alias(TARGET_COLUMN), pl.lit(1.0).alias(GROUP_COLUMN)
        )
        assert tuple(build_feature_frame(frame).columns) == FEATURE_NAMES

    def test_everything_is_cast_to_one_float_dtype(self) -> None:
        """``road_class_ordinal`` and ``is_motorway`` arrive as integers from Parquet, and a
        mixed-dtype frame converted to NumPy yields an ``object`` array LightGBM rejects with a
        message about the wrong *shape*."""
        frame = valid_frame().with_columns(
            pl.col("road_class_ordinal").cast(pl.Int32), pl.col("is_motorway").cast(pl.Int8)
        )
        assert set(build_feature_frame(frame).dtypes) == {pl.Float64}

    def test_the_design_matrix_has_the_right_shape_and_dtype(self) -> None:
        matrix = feature_matrix(valid_frame(rows=7))
        assert matrix.shape == (7, 20)
        assert matrix.dtype == np.float64

    def test_the_matrix_preserves_the_column_order(self) -> None:
        """Values chosen so each column is distinguishable by position."""
        frame = pl.DataFrame(
            {name: [float(index)] for index, name in enumerate(reversed(FEATURE_NAMES))}
        )
        matrix = feature_matrix(frame)
        expected = [float(19 - index) for index in range(20)]
        assert matrix[0].tolist() == expected


@pytest.fixture(scope="module")
def conditions() -> SegmentConditions:
    """One fully-populated motorway segment on a cold day, with every optional field set."""
    return SegmentConditions(
        speed_kmh=120.0,
        distance_km=5.0,
        gradient_percent=1.5,
        outside_temperature_c=-5.0,
        battery_temperature_c=3.0,
        acceleration_ms2=-0.4,
        traffic_severity=TrafficSeverity.high,
        precipitation_mm=0.6,
        wind_speed_ms=7.0,
        road_class=RoadClass.motorway,
    )


class TestFeaturesFromConditions:
    """The dataframe-free path the route analyser and the API take."""

    def test_it_produces_exactly_the_contract_keys(
        self, sedan: VehicleProfile, conditions: SegmentConditions
    ) -> None:
        """Same 20 names as the Parquet path, or a route-analysis prediction could not be
        compared with a training row at all."""
        assert set(features_from_conditions(sedan, conditions)) == set(FEATURE_NAMES)

    def test_the_values_are_the_conditions_in_the_trained_units(
        self, sedan: VehicleProfile, conditions: SegmentConditions
    ) -> None:
        """Every value derived here independently of the builder."""
        features = features_from_conditions(sedan, conditions)
        assert features["speed_kmh"] == 120.0
        assert features["outside_temperature_c"] == -5.0
        assert features["battery_temperature_c"] == 3.0
        assert features["gradient_percent"] == 1.5
        assert features["segment_distance_km"] == 5.0
        assert features["precipitation_mm"] == 0.6
        assert features["wind_speed_ms"] == 7.0
        assert features["mass_kg"] == sedan.mass_kg
        assert features["drag_area"] == pytest.approx(0.23 * 2.30)
        assert features["nominal_consumption_kwh_100km"] == 16.5
        assert features["road_class_ordinal"] == float(RoadClass.motorway.ordinal)
        assert features["traffic_severity_ordinal"] == float(TrafficSeverity.high.ordinal)
        assert features["is_motorway"] == 1.0
        # HVAC at -5 °C interpolates the documented curve between 2.2 kW (0 °C) and
        # 3.5 kW (-10 °C): 2.2 + 0.5·1.3 = 2.85 kW.
        assert features["hvac_load_kw"] == pytest.approx(2.85)
        assert features["hvac_load_kw"] == pytest.approx(DEFAULT_PHYSICS.hvac_power_kw(-5.0))

    def test_the_net_acceleration_is_taken_as_an_absolute_value(
        self, sedan: VehicleProfile, conditions: SegmentConditions
    ) -> None:
        """The feature is ``|a|``: a braking window is as much "work" as an accelerating one,
        and a signed value would average to nothing over a trip."""
        features = features_from_conditions(sedan, conditions)
        assert features["acceleration_abs_mean_ms2"] == pytest.approx(0.4)

    def test_a_motorway_is_flagged_and_nothing_else_is(self, sedan: VehicleProfile) -> None:
        for road_class in RoadClass:
            features = features_from_conditions(
                sedan, SegmentConditions(speed_kmh=80.0, distance_km=2.0, road_class=road_class)
            )
            assert features["is_motorway"] == (1.0 if road_class is RoadClass.motorway else 0.0)

    def test_an_unknown_speed_limit_falls_back_to_the_road_class_default(
        self, sedan: VehicleProfile
    ) -> None:
        """A feature vector cannot hold "unlimited"; 130 is the Autobahn Richtgeschwindigkeit
        and describes the typical traffic there. The speed actually driven enters separately."""
        unlimited = SegmentConditions(
            speed_kmh=180.0, distance_km=5.0, road_class=RoadClass.motorway
        )
        assert features_from_conditions(sedan, unlimited)["speed_limit_kmh"] == 130.0

    def test_an_explicit_speed_limit_wins(self, sedan: VehicleProfile) -> None:
        limited = SegmentConditions(
            speed_kmh=100.0, distance_km=5.0, road_class=RoadClass.motorway, speed_limit_kmh=100.0
        )
        assert features_from_conditions(sedan, limited)["speed_limit_kmh"] == 100.0

    def test_the_dispersion_features_default_to_a_steady_cruise(
        self, sedan: VehicleProfile
    ) -> None:
        """``SegmentConditions`` describes a *homogeneous* stretch and carries no information
        about variability, so zero is the honest default — it tells the model "steady cruise",
        which is exactly the assumption the route analyser made."""
        features = features_from_conditions(
            sedan, SegmentConditions(speed_kmh=100.0, distance_km=5.0)
        )
        assert features["speed_std_kmh"] == 0.0
        assert features["accel_events_per_km"] == 0.0
        assert features["avg_speed_kmh"] == features["speed_kmh"]
        assert features["soc_percent"] == DEFAULT_SOC_PERCENT

    def test_a_caller_that_knows_better_can_override_them(self, sedan: VehicleProfile) -> None:
        """The simulator aggregating a real 60 s telemetry window does know better."""
        features = features_from_conditions(
            sedan,
            SegmentConditions(speed_kmh=100.0, distance_km=5.0),
            soc_percent=32.0,
            avg_speed_kmh=104.0,
            speed_std_kmh=11.0,
            acceleration_abs_mean_ms2=-0.9,
            accel_events_per_km=4.0,
            segment_distance_km=12.0,
        )
        assert features["soc_percent"] == 32.0
        assert features["avg_speed_kmh"] == 104.0
        assert features["speed_std_kmh"] == 11.0
        assert features["acceleration_abs_mean_ms2"] == pytest.approx(0.9)
        assert features["accel_events_per_km"] == 4.0
        assert features["segment_distance_km"] == 12.0

    def test_negative_inputs_are_floored_at_zero(self, sedan: VehicleProfile) -> None:
        """Speeds, dispersions and precipitation cannot be negative; a -1 would be a sensor
        artefact, and passing it through would put the model outside its training envelope."""
        features = features_from_conditions(
            sedan,
            SegmentConditions(speed_kmh=-5.0, distance_km=5.0),
            avg_speed_kmh=-3.0,
            speed_std_kmh=-2.0,
            accel_events_per_km=-1.0,
        )
        assert features["speed_kmh"] == 0.0
        assert features["avg_speed_kmh"] == 0.0
        assert features["speed_std_kmh"] == 0.0
        assert features["accel_events_per_km"] == 0.0

    def test_an_unknown_pack_temperature_falls_back_to_ambient(self, sedan: VehicleProfile) -> None:
        """The cold-soak assumption — the same fallback the physical model makes."""
        features = features_from_conditions(
            sedan,
            SegmentConditions(speed_kmh=90.0, distance_km=5.0, outside_temperature_c=-3.0),
        )
        assert features["battery_temperature_c"] == -3.0

    def test_the_result_is_a_valid_feature_vector(
        self, sedan: VehicleProfile, conditions: SegmentConditions
    ) -> None:
        """End to end: the mapping must survive ``feature_array`` with no missing key."""
        array = feature_array(features_from_conditions(sedan, conditions))
        assert array.shape == (1, 20)
        assert np.isfinite(array).all()


class TestSpeedLimitDefaults:
    """German StVO defaults, one per road class."""

    def test_every_road_class_has_a_default(self) -> None:
        """A ``KeyError`` here would be an unhandled 500 on a segment OSM did not classify."""
        for road_class in RoadClass:
            assert default_speed_limit_kmh(road_class) > 0.0

    @pytest.mark.parametrize(
        ("road_class", "expected_kmh"),
        [
            (RoadClass.motorway, 130.0),  # Richtgeschwindigkeit, not a limit
            (RoadClass.residential, 50.0),  # StVO §3: inside built-up areas
            (RoadClass.primary, 100.0),  # outside built-up areas
            (RoadClass.unknown, 100.0),
        ],
    )
    def test_the_documented_german_defaults(
        self, road_class: RoadClass, expected_kmh: float
    ) -> None:
        assert default_speed_limit_kmh(road_class) == expected_kmh

    def test_the_defaults_are_ordered_like_the_road_hierarchy(self) -> None:
        """A residential street cannot have a higher assumed limit than a trunk road."""
        assert (
            default_speed_limit_kmh(RoadClass.service)
            < default_speed_limit_kmh(RoadClass.residential)
            < default_speed_limit_kmh(RoadClass.tertiary)
            < default_speed_limit_kmh(RoadClass.trunk)
            < default_speed_limit_kmh(RoadClass.motorway)
        )


class TestFeatureArrays:
    """Mapping → matrix, the path a single prediction request takes."""

    def test_one_row_becomes_a_one_by_twenty_matrix(self, sedan: VehicleProfile) -> None:
        features = features_from_conditions(
            sedan, SegmentConditions(speed_kmh=100.0, distance_km=5.0)
        )
        assert feature_array(features).shape == (1, 20)

    def test_the_column_order_follows_the_contract_not_the_dict(self) -> None:
        """Python dicts preserve insertion order, so a caller building the mapping in a
        different order must still get the columns in §10.2 order."""
        row = {name: float(index) for index, name in enumerate(FEATURE_NAMES)}
        reversed_row = dict(reversed(list(row.items())))
        assert feature_array(reversed_row)[0].tolist() == [float(i) for i in range(20)]

    def test_a_batch_keeps_its_row_order(self) -> None:
        rows = [{name: float(index) for name in FEATURE_NAMES} for index in range(3)]
        matrix = feature_batch(rows)
        assert matrix.shape == (3, 20)
        assert matrix[:, 0].tolist() == [0.0, 1.0, 2.0]

    def test_extra_keys_are_ignored(self) -> None:
        """A caller may pass a richer context object; only the contract columns are read."""
        row = dict.fromkeys(FEATURE_NAMES, 1.0)
        row["something_else"] = 99.0
        assert feature_array(row).shape == (1, 20)

    def test_a_missing_key_is_fatal(self) -> None:
        """Substituting a zero would move the prediction without telling anyone."""
        row = dict.fromkeys(FEATURE_NAMES, 1.0)
        del row["mass_kg"]
        with pytest.raises(ValidationError) as excinfo:
            feature_array(row)
        assert "mass_kg" in str(excinfo.value)

    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
    def test_a_non_finite_value_names_the_feature_and_the_row(self, bad: float) -> None:
        rows = [dict.fromkeys(FEATURE_NAMES, 1.0), dict.fromkeys(FEATURE_NAMES, 1.0)]
        rows[1]["drag_area"] = bad
        with pytest.raises(ValidationError) as excinfo:
            feature_batch(rows)
        assert excinfo.value.details["feature"] == "drag_area"
        assert excinfo.value.details["row_index"] == 1

    def test_an_empty_batch_is_refused(self) -> None:
        """There is nothing to predict on, and an empty (0, 20) matrix would be handed to the
        booster only to fail deeper in the stack."""
        with pytest.raises(ValidationError):
            feature_batch([])
