"""The data-quality framework.

These rules are what stand between a shifting German open-data file and the database. Their
job is to *reject* bad rows loudly rather than write plausible nonsense, so the tests assert
both halves: good rows survive untouched, bad rows are counted and named.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from autotwin_core.errors import InvalidSourceData
from autotwin_core.quality.report import QualityReport, Violation
from autotwin_core.quality.rules import (
    latitude_in_range,
    longitude_in_range,
    monotonic_timestamps,
    no_duplicate_key,
    numeric_range,
    positive_power,
    required_field,
    soc_in_range,
    timestamp_not_future,
    timestamp_present,
    within_germany_bbox,
)
from autotwin_core.quality.validator import RecordValidator


@dataclass
class Station:
    external_id: str
    operator: str | None
    latitude: float
    longitude: float
    power_kw: float | None


@dataclass
class Reading:
    vehicle_id: str
    recorded_at: datetime
    battery_soc_percent: float


def station(**overrides: object) -> Station:
    base = {
        "external_id": "bnetza:1010338",
        "operator": "Albwerk Elektro- und Kommunikationstechnik GmbH",
        "latitude": 48.442398,
        "longitude": 9.659075,
        "power_kw": 22.0,
    }
    return Station(**{**base, **overrides})  # type: ignore[arg-type]


class TestRules:
    def test_latitude_and_longitude_bounds(self) -> None:
        lat, lon = latitude_in_range("latitude"), longitude_in_range("longitude")
        assert lat(station(), None) is None
        assert lat(station(latitude=91.0), None) is not None
        assert lon(station(longitude=-181.0), None) is not None

    def test_germany_bbox_rejects_plausible_but_wrong_coordinates(self) -> None:
        rule = within_germany_bbox("latitude")
        # Paris: a valid coordinate, and definitively not a German charging station. Swapped
        # lat/lon in a source file looks exactly like this.
        assert rule(station(latitude=48.8566, longitude=2.3522), None) is not None
        assert rule(station(), None) is None

    def test_swapped_lat_lon_is_caught(self) -> None:
        rule = within_germany_bbox("latitude")
        swapped = station(latitude=9.659075, longitude=48.442398)
        assert rule(swapped, None) is not None

    def test_positive_power(self) -> None:
        rule = positive_power("power_kw")
        assert rule(station(), None) is None
        assert rule(station(power_kw=0.0), None) is not None
        assert rule(station(power_kw=-5.0), None) is not None
        # An implausibly large value is a unit error (W mistaken for kW), not a real charger.
        assert rule(station(power_kw=350_000.0), None) is not None

    def test_required_field(self) -> None:
        rule = required_field("operator")
        assert rule(station(), None) is None
        assert rule(station(operator=None), None) is not None
        assert rule(station(operator="   "), None) is not None

    def test_soc_bounds(self) -> None:
        rule = soc_in_range("battery_soc_percent")
        reading = Reading("ATW-0001", datetime.now(UTC), 55.0)
        assert rule(reading, None) is None
        assert rule(Reading("ATW-0001", datetime.now(UTC), 101.0), None) is not None
        assert rule(Reading("ATW-0001", datetime.now(UTC), -1.0), None) is not None

    def test_timestamp_not_future(self) -> None:
        rule = timestamp_not_future("recorded_at", tolerance_s=60)
        now = datetime.now(UTC)
        assert rule(Reading("a", now, 50.0), None) is None
        assert rule(Reading("a", now + timedelta(hours=2), 50.0), None) is not None
        # Small clock skew between a source and us is normal and must not reject a row.
        assert rule(Reading("a", now + timedelta(seconds=30), 50.0), None) is None

    def test_timestamp_present(self) -> None:
        rule = timestamp_present("recorded_at")
        assert rule(Reading("a", datetime.now(UTC), 50.0), None) is None

    def test_numeric_range_allows_none_by_default(self) -> None:
        rule = numeric_range("power_kw", 0.0, 400.0)
        assert rule(station(power_kw=None), None) is None
        assert rule(station(power_kw=500.0), None) is not None

    def test_duplicate_detection_is_stateful(self) -> None:
        rule = no_duplicate_key(lambda record: record.external_id)
        first = station()
        assert rule(first, None) is None
        assert rule(station(), None) is not None, "the same external_id twice is a duplicate"
        assert rule(station(external_id="bnetza:other"), None) is None

    def test_monotonic_timestamps_per_vehicle(self) -> None:
        rule = monotonic_timestamps(key_fn=lambda r: r.vehicle_id, ts_fn=lambda r: r.recorded_at)
        base = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
        assert rule(Reading("A", base, 90.0), None) is None
        assert rule(Reading("A", base + timedelta(seconds=1), 89.0), None) is None
        # Time going backwards for one vehicle is a real telemetry defect.
        assert rule(Reading("A", base - timedelta(seconds=5), 88.0), None) is not None
        # A different vehicle has its own clock and must not be affected.
        assert rule(Reading("B", base, 50.0), None) is None


class TestRecordValidator:
    def test_counts_and_separates_good_from_bad(self) -> None:
        validator = RecordValidator(
            rules=[
                required_field("operator"),
                within_germany_bbox("latitude"),
                positive_power("power_kw"),
                no_duplicate_key(lambda record: record.external_id),
            ]
        )
        records = [
            station(),
            station(external_id="b", operator=None),  # rejected: no operator
            station(external_id="c", latitude=48.8566, longitude=2.3522),  # rejected: Paris
            station(external_id="d", power_kw=-1.0),  # rejected: negative power
            station(external_id="e"),  # accepted
            station(external_id="e"),  # rejected: duplicate
        ]

        accepted, report = validator.validate(records)

        assert len(accepted) == 2
        assert report.rows_received == 6
        assert report.rows_accepted == 2
        # Duplicates are their own category, not a subset of rejects, so the four counters
        # partition the batch exactly: accepted + rejected + duplicate == received.
        assert report.rows_rejected == 3
        assert report.rows_duplicate == 1
        assert (
            report.rows_accepted + report.rows_rejected + report.rows_duplicate
            == report.rows_received
        )
        assert report.acceptance_rate == pytest.approx(2 / 6)
        assert {violation.rule for violation in report.violations}

    def test_strict_mode_raises_when_the_source_changes_shape(self) -> None:
        """A collapse in acceptance rate means the file changed, not that the data got worse."""
        validator = RecordValidator(
            rules=[required_field("operator")], strict=True, min_acceptance_rate=0.5
        )
        # Above `strict_min_rows`, otherwise the rate is statistically meaningless and the
        # validator deliberately stays quiet.
        records = [station(external_id=str(i), operator=None) for i in range(30)]
        with pytest.raises(InvalidSourceData):
            validator.validate(records)

    def test_strict_mode_stays_quiet_on_a_batch_too_small_to_judge(self) -> None:
        validator = RecordValidator(
            rules=[required_field("operator")], strict=True, min_acceptance_rate=0.5
        )
        accepted, report = validator.validate(
            [station(external_id=str(i), operator=None) for i in range(5)]
        )
        assert accepted == []
        assert report.rows_rejected == 5

    def test_empty_input_is_not_an_error(self) -> None:
        validator = RecordValidator(rules=[required_field("operator")])
        accepted, report = validator.validate([])
        assert accepted == []
        assert report.rows_received == 0

    def test_rules_are_reset_between_batches(self) -> None:
        """A stateful duplicate rule must not leak keys from a previous ingestion run."""
        validator = RecordValidator(rules=[no_duplicate_key(lambda r: r.external_id)])
        first, _ = validator.validate([station()])
        second, report = validator.validate([station()])
        assert len(first) == 1
        assert len(second) == 1, "the same row in a later run is not a duplicate"
        assert report.rows_duplicate == 0


class TestQualityReport:
    def test_serialises_to_json_compatible_dict(self) -> None:
        report = QualityReport(rows_received=3, rows_accepted=2, rows_rejected=1, rows_duplicate=0)
        report.record_violation(
            Violation(rule="required_field", field="operator", message="missing", row_index=1)
        )
        payload = report.as_dict()
        import json

        json.dumps(payload)  # must not raise — this goes into a JSONB column
        assert payload["rows_received"] == 3

    def test_caps_stored_violations(self) -> None:
        """A catastrophic source file must not bloat the ingestion-run row."""
        report = QualityReport(
            rows_received=10_000, rows_accepted=0, rows_rejected=10_000, rows_duplicate=0
        )
        for index in range(1_000):
            report.record_violation(
                Violation(
                    rule="required_field", field="operator", message="missing", row_index=index
                )
            )
        payload = report.as_dict(max_violations=200)
        assert len(payload["violations"]) <= 200
        assert payload["violations_truncated"] >= 800

    def test_merge_adds_counts(self) -> None:
        a = QualityReport(rows_received=2, rows_accepted=2, rows_rejected=0, rows_duplicate=0)
        b = QualityReport(rows_received=3, rows_accepted=1, rows_rejected=2, rows_duplicate=1)
        merged = a.merge(b)
        assert merged.rows_received == 5
        assert merged.rows_accepted == 3
        assert merged.rows_rejected == 2
