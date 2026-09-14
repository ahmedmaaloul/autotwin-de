"""Composable validation rules (BUILD_SPEC §6).

The framework is deliberately tiny — no Great Expectations, no schema DSL. A rule is a
callable that looks at one record and returns an English error message, or ``None`` when the
record is fine. Everything else (counting, collecting, thresholds) lives in
:mod:`autotwin_core.quality.validator`, so a rule never has to know how it is being run.

Rules are small dataclasses rather than closures for one reason: a closure has no stable
identity, and :attr:`~autotwin_core.quality.report.QualityReport.rule_stats` is keyed by rule
name. A dataclass carries :attr:`~Rule.rule_name` and :attr:`~Rule.field` as data, which means
the same rule produces the same key in every run, in every pipeline, and the data-quality page
can chart it over time.

Field lookup accepts dotted paths (``"coordinate.latitude"``) and works on Pydantic records,
plain objects and mappings alike, so the same rule set validates a parsed
:class:`~autotwin_contracts.ChargingStationRecord` and the raw ``dict`` it came from.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime
from typing import Any, Final, Protocol, runtime_checkable

from autotwin_contracts import GERMANY_BBOX, Coordinate, ensure_utc, utc_now

__all__ = [
    "DUPLICATE_RULE_NAME",
    "MISSING",
    "LatitudeInRange",
    "LongitudeInRange",
    "MonotonicTimestamps",
    "NoDuplicateKey",
    "NumericRange",
    "PositivePower",
    "RequiredField",
    "Rule",
    "SocInRange",
    "StatefulRule",
    "TimestampNotFuture",
    "TimestampPresent",
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

DUPLICATE_RULE_NAME: Final[str] = "no_duplicate_key"
"""Rule name that marks a row as a duplicate rather than a reject (see the validator)."""


class _Missing:
    """Sentinel type for "the record has no such field", which is not the same as ``None``."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<missing>"


MISSING: Final[_Missing] = _Missing()
"""Returned by :func:`resolve_field` when a path does not exist on the record at all."""


class Rule(Protocol):
    """The shape every validation rule has.

    Structurally this is ``Callable[[Any, str | None], str | None]`` — the record and an
    optional human label for it, in, an error message or ``None`` out — plus two data members
    that let the report attribute the failure without the caller knowing the rule's class.
    """

    @property
    def rule_name(self) -> str:
        """Stable identifier used as the ``rule_stats`` key and in ``Violation.rule``."""

    @property
    def field(self) -> str | None:
        """Record field the rule inspects, or ``None`` for whole-record rules."""

    def __call__(self, record: Any, context: str | None = None, /) -> str | None:
        """Validate ``record``; ``context`` is an optional label used in messages."""


@runtime_checkable
class StatefulRule(Protocol):
    """A rule that remembers something across the rows of one batch.

    Duplicate detection and monotonicity are inherently cross-row checks. They must be reset
    between batches, otherwise the second ingestion run of the day reports every row of an
    unchanged source file as a duplicate of the first run.
    """

    def reset(self) -> None:
        """Forget everything seen so far; called by the validator before each batch."""


def resolve_field(record: Any, path: str) -> Any:
    """Resolve a possibly dotted ``path`` against a record, returning :data:`MISSING` if absent.

    Mapping access is tried before attribute access so that a raw source ``dict`` with a key
    named ``items`` resolves to the value rather than to ``dict.items``.
    """
    current: Any = record
    for part in path.split("."):
        if current is None:
            return MISSING
        if isinstance(current, dict):
            if part not in current:
                return MISSING
            current = current[part]
            continue
        if not hasattr(current, part):
            return MISSING
        current = getattr(current, part)
    return current


def _lookup_first(record: Any, paths: Sequence[str]) -> tuple[str | None, Any]:
    """Return the first ``(path, value)`` that resolves to something other than :data:`MISSING`."""
    for path in paths:
        value = resolve_field(record, path)
        if value is not MISSING:
            return path, value
    return None, MISSING


def _as_number(value: Any) -> float | None:
    """Coerce to ``float`` when the value is genuinely numeric.

    ``bool`` is rejected even though it subclasses ``int``: a boolean in a power or
    temperature column means the source column moved, which is exactly what these rules exist
    to catch.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _with_context(message: str, context: str | None) -> str:
    """Append the caller's record label so a reviewer can find the row in the source file."""
    return message if context is None else f"{message} (record {context})"


_LATITUDE_PATHS: Final[tuple[str, ...]] = ("latitude", "coordinate.latitude", "lat")
_LONGITUDE_PATHS: Final[tuple[str, ...]] = ("longitude", "coordinate.longitude", "lon", "lng")


# ---------------------------------------------------------------------------
# Field-scoped rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RequiredField:
    """The field must exist and carry a non-empty value."""

    field: str

    @property
    def rule_name(self) -> str:
        """Name includes the field so ``rule_stats`` shows *which* column is missing."""
        return f"required_field[{self.field}]"

    def __call__(self, record: Any, context: str | None = None, /) -> str | None:
        """Fail on a missing key, a ``None`` value or a blank string."""
        value = resolve_field(record, self.field)
        if value is MISSING:
            return _with_context(f"field {self.field!r} is missing", context)
        if value is None:
            return _with_context(f"field {self.field!r} is null", context)
        if isinstance(value, str) and not value.strip():
            return _with_context(f"field {self.field!r} is blank", context)
        return None


@dataclass(frozen=True, slots=True)
class NumericRange:
    """The field must be numeric and lie within a closed interval."""

    field: str
    minimum: float
    maximum: float
    allow_none: bool = True
    label: str = "numeric_range"

    @property
    def rule_name(self) -> str:
        """``label`` lets specialised rules (SOC, power) reuse this implementation by name."""
        return f"{self.label}[{self.field}]"

    def __call__(self, record: Any, context: str | None = None, /) -> str | None:
        """Fail on a missing, non-numeric or out-of-interval value."""
        value = resolve_field(record, self.field)
        if value is MISSING:
            return _with_context(f"field {self.field!r} is missing", context)
        if value is None:
            if self.allow_none:
                return None
            return _with_context(f"field {self.field!r} is null", context)
        number = _as_number(value)
        if number is None:
            return _with_context(f"field {self.field!r} is not numeric: {value!r}", context)
        if not self.minimum <= number <= self.maximum:
            return _with_context(
                f"field {self.field!r} = {number:g} is outside "
                f"[{self.minimum:g}, {self.maximum:g}]",
                context,
            )
        return None


@dataclass(frozen=True, slots=True)
class LatitudeInRange:
    """Latitude must be a number in ``[-90, 90]``."""

    field: str | None = None
    """Explicit path; ``None`` probes ``latitude``, ``coordinate.latitude`` and ``lat``."""

    @property
    def rule_name(self) -> str:
        """Stable name, with the field only when one was configured explicitly."""
        return "latitude_in_range" if self.field is None else f"latitude_in_range[{self.field}]"

    def __call__(self, record: Any, context: str | None = None, /) -> str | None:
        """Fail when no latitude can be found, or when it is not a valid WGS 84 latitude."""
        paths = (self.field,) if self.field is not None else _LATITUDE_PATHS
        path, value = _lookup_first(record, paths)
        if path is None:
            return _with_context(f"no latitude field found (tried {', '.join(paths)})", context)
        number = _as_number(value)
        if number is None:
            return _with_context(f"latitude {path!r} is not numeric: {value!r}", context)
        if not -90.0 <= number <= 90.0:
            return _with_context(f"latitude {number:g} is outside [-90, 90]", context)
        return None


@dataclass(frozen=True, slots=True)
class LongitudeInRange:
    """Longitude must be a number in ``[-180, 180]``."""

    field: str | None = None
    """Explicit path; ``None`` probes ``longitude``, ``coordinate.longitude``, ``lon``, ``lng``."""

    @property
    def rule_name(self) -> str:
        """Stable name, with the field only when one was configured explicitly."""
        return "longitude_in_range" if self.field is None else f"longitude_in_range[{self.field}]"

    def __call__(self, record: Any, context: str | None = None, /) -> str | None:
        """Fail when no longitude can be found, or when it is not a valid WGS 84 longitude."""
        paths = (self.field,) if self.field is not None else _LONGITUDE_PATHS
        path, value = _lookup_first(record, paths)
        if path is None:
            return _with_context(f"no longitude field found (tried {', '.join(paths)})", context)
        number = _as_number(value)
        if number is None:
            return _with_context(f"longitude {path!r} is not numeric: {value!r}", context)
        if not -180.0 <= number <= 180.0:
            return _with_context(f"longitude {number:g} is outside [-180, 180]", context)
        return None


@dataclass(frozen=True, slots=True)
class WithinGermanyBBox:
    """The record's point must lie inside Germany's bounding box.

    This is the rule that catches the classic defects of German open data: a swapped
    latitude/longitude pair (which lands the station in Somalia), a comma-decimal parsed as a
    thousands separator, and the 0/0 "Null Island" placeholder. The box comes from
    :data:`autotwin_contracts.GERMANY_BBOX` so the API's ``?bbox=`` default, the map's initial
    view and this rule can never drift apart.
    """

    coordinate_field: str = "coordinate"
    latitude_field: str | None = None
    longitude_field: str | None = None

    @property
    def rule_name(self) -> str:
        """Whole-box rule; the name carries no field because it inspects a point, not a column."""
        return "within_germany_bbox"

    @property
    def field(self) -> str | None:
        """The coordinate field, for attribution in the report."""
        return self.coordinate_field

    def __call__(self, record: Any, context: str | None = None, /) -> str | None:
        """Fail when the point is missing, unparsable or outside the German extent."""
        point = resolve_field(record, self.coordinate_field)
        if isinstance(point, Coordinate):
            latitude: float | None = point.latitude
            longitude: float | None = point.longitude
        else:
            latitude = _as_number(
                _lookup_first(
                    record,
                    (self.latitude_field,) if self.latitude_field else _LATITUDE_PATHS,
                )[1]
            )
            longitude = _as_number(
                _lookup_first(
                    record,
                    (self.longitude_field,) if self.longitude_field else _LONGITUDE_PATHS,
                )[1]
            )
        if latitude is None or longitude is None:
            return _with_context("record has no usable coordinate", context)
        if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
            return _with_context(
                f"coordinate ({latitude:g}, {longitude:g}) is not a valid WGS 84 point",
                context,
            )
        if not GERMANY_BBOX.contains(Coordinate(latitude=latitude, longitude=longitude)):
            return _with_context(
                f"coordinate ({latitude:.5f}, {longitude:.5f}) is outside Germany "
                f"[{GERMANY_BBOX.as_param()}]",
                context,
            )
        return None


@dataclass(frozen=True, slots=True)
class PositivePower:
    """A charging power must be strictly positive and physically plausible.

    Zero is rejected rather than tolerated: the Ladesäulenregister uses an empty cell for
    "unknown", so a literal ``0`` means the column was mis-parsed. The upper bound guards
    against a kW/W unit mix-up, which is the other way this column goes wrong.
    """

    field: str = "power_kw"
    maximum_kw: float = 1000.0
    allow_none: bool = True

    @property
    def rule_name(self) -> str:
        """Name includes the field — stations and points use different column names."""
        return f"positive_power[{self.field}]"

    def __call__(self, record: Any, context: str | None = None, /) -> str | None:
        """Fail on a non-numeric, non-positive or implausibly large power rating."""
        value = resolve_field(record, self.field)
        if value is MISSING:
            return _with_context(f"field {self.field!r} is missing", context)
        if value is None:
            if self.allow_none:
                return None
            return _with_context(f"field {self.field!r} is null", context)
        number = _as_number(value)
        if number is None:
            return _with_context(f"power {self.field!r} is not numeric: {value!r}", context)
        if number <= 0.0:
            return _with_context(f"power {self.field!r} = {number:g} kW is not positive", context)
        if number > self.maximum_kw:
            return _with_context(
                f"power {self.field!r} = {number:g} kW exceeds the plausible maximum of "
                f"{self.maximum_kw:g} kW",
                context,
            )
        return None


@dataclass(frozen=True, slots=True)
class SocInRange:
    """State of charge must be a percentage in ``[0, 100]``."""

    field: str = "battery_soc_percent"
    allow_none: bool = False

    @property
    def rule_name(self) -> str:
        """Name includes the field; telemetry and trips spell the column differently."""
        return f"soc_in_range[{self.field}]"

    def __call__(self, record: Any, context: str | None = None, /) -> str | None:
        """Fail when SOC is missing, non-numeric or outside 0-100 %."""
        delegate = NumericRange(
            field=self.field,
            minimum=0.0,
            maximum=100.0,
            allow_none=self.allow_none,
            label="soc_in_range",
        )
        return delegate(record, context)


@dataclass(frozen=True, slots=True)
class TimestampPresent:
    """The field must hold a real ``datetime``.

    A string that *looks* like a timestamp is a failure, not a success: by the time a record
    reaches validation it has been through Pydantic, so a leftover string means the adapter
    skipped the field entirely.
    """

    field: str

    @property
    def rule_name(self) -> str:
        """Name includes the field, since a record may carry several timestamps."""
        return f"timestamp_present[{self.field}]"

    def __call__(self, record: Any, context: str | None = None, /) -> str | None:
        """Fail when the timestamp is missing, null or not a ``datetime``."""
        value = resolve_field(record, self.field)
        if value is MISSING:
            return _with_context(f"timestamp {self.field!r} is missing", context)
        if value is None:
            return _with_context(f"timestamp {self.field!r} is null", context)
        if not isinstance(value, datetime):
            return _with_context(
                f"timestamp {self.field!r} is not a datetime: {type(value).__name__}",
                context,
            )
        return None


@dataclass(frozen=True, slots=True)
class TimestampNotFuture:
    """The field must not lie meaningfully in the future.

    A tolerance is mandatory rather than optional. DWD forecasts-as-observations, clocks that
    are a few minutes fast and the hour-granularity of some feeds all produce timestamps
    slightly ahead of ours; one hour absorbs that without letting through the real defect,
    which is a year or a timezone parsed wrongly.
    """

    field: str
    tolerance_s: float = 3600.0

    @property
    def rule_name(self) -> str:
        """Name includes the field, since a record may carry several timestamps."""
        return f"timestamp_not_future[{self.field}]"

    def __call__(self, record: Any, context: str | None = None, /) -> str | None:
        """Fail when the timestamp exceeds ``now + tolerance``."""
        value = resolve_field(record, self.field)
        if value is MISSING or value is None:
            return None
        if not isinstance(value, datetime):
            return _with_context(
                f"timestamp {self.field!r} is not a datetime: {type(value).__name__}",
                context,
            )
        ahead_s = (ensure_utc(value) - utc_now()).total_seconds()
        if ahead_s > self.tolerance_s:
            return _with_context(
                f"timestamp {self.field!r} is {ahead_s / 60.0:.1f} min in the future "
                f"(tolerance {self.tolerance_s / 60.0:.0f} min)",
                context,
            )
        return None


# ---------------------------------------------------------------------------
# Stateful, cross-row rules
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class NoDuplicateKey:
    """Each natural key may appear at most once per batch.

    Mutable and stateful by necessity. The validator calls :meth:`reset` before every batch
    and then invokes the rule exactly once per record, in batch order, which is what makes the
    reported position of the first occurrence meaningful.

    A ``None`` key is never a duplicate: "this row has no natural key" is the job of
    :class:`RequiredField`, and reporting it twice would double-count the same defect.
    """

    key_fn: Callable[[Any], object | None]
    field: str | None = None
    _seen: dict[object, int] = dataclass_field(default_factory=dict, init=False, repr=False)
    _position: int = dataclass_field(default=0, init=False, repr=False)

    @property
    def rule_name(self) -> str:
        """Fixed name — the validator keys duplicate accounting off exactly this string."""
        return DUPLICATE_RULE_NAME

    def reset(self) -> None:
        """Start a new batch."""
        self._seen.clear()
        self._position = 0

    def __call__(self, record: Any, context: str | None = None, /) -> str | None:
        """Fail when this key was already seen in the current batch."""
        position = self._position
        self._position += 1
        key = self.key_fn(record)
        if key is None:
            return None
        first = self._seen.get(key)
        if first is None:
            self._seen[key] = position
            return None
        return _with_context(f"duplicate key {key!r}, first seen at row {first}", context)


@dataclass(slots=True)
class MonotonicTimestamps:
    """Within one key, timestamps must not go backwards.

    Telemetry and weather series are appended in time order; a step back means either two
    sources were merged without sorting or a clock jumped. Equal timestamps are allowed — a
    station reporting twice in the same minute is normal — so only a strict decrease fails.
    """

    key_fn: Callable[[Any], object | None]
    ts_fn: Callable[[Any], datetime | None]
    field: str | None = None
    _last: dict[object, datetime] = dataclass_field(default_factory=dict, init=False, repr=False)

    @property
    def rule_name(self) -> str:
        """Fixed name; the offending key is named in the message instead."""
        return "monotonic_timestamps"

    def reset(self) -> None:
        """Start a new batch."""
        self._last.clear()

    def __call__(self, record: Any, context: str | None = None, /) -> str | None:
        """Fail when this record's timestamp precedes the previous one for the same key."""
        key = self.key_fn(record)
        timestamp = self.ts_fn(record)
        if key is None or timestamp is None:
            return None
        current = ensure_utc(timestamp)
        previous = self._last.get(key)
        if previous is not None and current < previous:
            return _with_context(
                f"timestamp {current.isoformat()} for key {key!r} precedes the previous "
                f"{previous.isoformat()}",
                context,
            )
        self._last[key] = current
        return None


# ---------------------------------------------------------------------------
# Factories — the names BUILD_SPEC §6 refers to
# ---------------------------------------------------------------------------


def required_field(name: str) -> Rule:
    """Require ``name`` to be present and non-empty."""
    return RequiredField(field=name)


def numeric_range(name: str, lo: float, hi: float, *, allow_none: bool = True) -> Rule:
    """Require ``name`` to be a number within the closed interval ``[lo, hi]``."""
    return NumericRange(field=name, minimum=lo, maximum=hi, allow_none=allow_none)


def latitude_in_range(field: str | None = None) -> Rule:
    """Require a valid WGS 84 latitude; probes the usual field names when ``field`` is ``None``."""
    return LatitudeInRange(field=field)


def longitude_in_range(field: str | None = None) -> Rule:
    """Require a valid WGS 84 longitude; probes the usual field names when ``field`` is ``None``."""
    return LongitudeInRange(field=field)


def within_germany_bbox(coordinate_field: str = "coordinate") -> Rule:
    """Require the record's point to fall inside :data:`autotwin_contracts.GERMANY_BBOX`."""
    return WithinGermanyBBox(coordinate_field=coordinate_field)


def positive_power(name: str = "power_kw", *, maximum_kw: float = 1000.0) -> Rule:
    """Require ``name`` to be a strictly positive, physically plausible power in kW."""
    return PositivePower(field=name, maximum_kw=maximum_kw)


def soc_in_range(field: str = "battery_soc_percent", *, allow_none: bool = False) -> Rule:
    """Require a state of charge between 0 and 100 %."""
    return SocInRange(field=field, allow_none=allow_none)


def timestamp_present(name: str) -> Rule:
    """Require ``name`` to hold a real ``datetime``."""
    return TimestampPresent(field=name)


def timestamp_not_future(name: str, tolerance_s: float = 3600.0) -> Rule:
    """Reject timestamps more than ``tolerance_s`` seconds ahead of now."""
    return TimestampNotFuture(field=name, tolerance_s=tolerance_s)


def no_duplicate_key(key_fn: Callable[[Any], object | None], *, field: str | None = None) -> Rule:
    """Reject a natural key that already appeared in this batch."""
    return NoDuplicateKey(key_fn=key_fn, field=field)


def monotonic_timestamps(
    key_fn: Callable[[Any], object | None],
    ts_fn: Callable[[Any], datetime | None],
    *,
    field: str | None = None,
) -> Rule:
    """Reject a record whose timestamp precedes the previous record for the same key."""
    return MonotonicTimestamps(key_fn=key_fn, ts_fn=ts_fn, field=field)


def reset_rules(rules: Iterable[Rule]) -> None:
    """Reset every stateful rule in ``rules``; stateless rules are left untouched."""
    for rule in rules:
        if isinstance(rule, StatefulRule):
            rule.reset()
