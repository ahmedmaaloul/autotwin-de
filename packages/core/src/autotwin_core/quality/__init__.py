"""Data-quality framework: rules, batch validation and the persisted report (BUILD_SPEC §6).

Import from here rather than from the submodules — the split between ``rules``, ``validator``
and ``report`` is an implementation detail that pipelines should not have to track::

    from autotwin_core.quality import RecordValidator, required_field, within_germany_bbox

    validator = RecordValidator(
        rules=[required_field("external_id"), within_germany_bbox()],
        strict=True,
        source_label="bnetza_charging_stations",
    )
    stations, report = validator.validate(parsed_records)
"""

from __future__ import annotations

from autotwin_core.quality.report import MAX_STORED_VIOLATIONS, QualityReport, Violation
from autotwin_core.quality.rules import (
    DUPLICATE_RULE_NAME,
    MISSING,
    LatitudeInRange,
    LongitudeInRange,
    MonotonicTimestamps,
    NoDuplicateKey,
    NumericRange,
    PositivePower,
    RequiredField,
    Rule,
    SocInRange,
    StatefulRule,
    TimestampNotFuture,
    TimestampPresent,
    WithinGermanyBBox,
    latitude_in_range,
    longitude_in_range,
    monotonic_timestamps,
    no_duplicate_key,
    numeric_range,
    positive_power,
    required_field,
    reset_rules,
    resolve_field,
    soc_in_range,
    timestamp_not_future,
    timestamp_present,
    within_germany_bbox,
)
from autotwin_core.quality.validator import RecordValidator

__all__ = [
    "DUPLICATE_RULE_NAME",
    "MAX_STORED_VIOLATIONS",
    "MISSING",
    "LatitudeInRange",
    "LongitudeInRange",
    "MonotonicTimestamps",
    "NoDuplicateKey",
    "NumericRange",
    "PositivePower",
    "QualityReport",
    "RecordValidator",
    "RequiredField",
    "Rule",
    "SocInRange",
    "StatefulRule",
    "TimestampNotFuture",
    "TimestampPresent",
    "Violation",
    "WithinGermanyBBox",
    "latitude_in_range",
    "longitude_in_range",
    "monotonic_timestamps",
    "no_duplicate_key",
    "numeric_range",
    "positive_power",
    "required_field",
    "reset_rules",
    "resolve_field",
    "soc_in_range",
    "timestamp_not_future",
    "timestamp_present",
    "within_germany_bbox",
]
