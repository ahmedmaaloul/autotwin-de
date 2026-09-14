"""Quality reports — the persisted evidence that an ingestion run was honest about its data.

BUILD_SPEC §6 requires every pipeline to reject bad rows rather than write them, and to keep
the reason on the ``data_ingestion_runs.quality_report`` JSONB column so that
``GET /api/v1/data/quality`` can show *why* a source is degraded rather than just *that* it is.

Two design decisions are worth stating:

* :meth:`QualityReport.as_dict` caps the stored violations. A source file that changed shape
  produces one violation per row; a 300 000-row Ladesäulenregister would otherwise write a
  hundred-megabyte JSONB value and take the ingestion table down with it. The cap keeps the
  first :data:`MAX_STORED_VIOLATIONS` examples — enough to diagnose the problem — and records
  how many were dropped, so the number is never silently wrong.
* :attr:`QualityReport.rule_stats` is a histogram over rule names, not over rows. It answers
  "which rule is firing" in one glance, which is the question an on-call engineer actually
  asks; the individual violations answer "on which row".
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Final, Self

from autotwin_contracts import IngestionOutcome

__all__ = [
    "MAX_STORED_VIOLATIONS",
    "QualityReport",
    "Violation",
]

MAX_STORED_VIOLATIONS: Final[int] = 200
"""Upper bound on violations written into ``data_ingestion_runs.quality_report``."""


@dataclass(frozen=True, slots=True)
class Violation:
    """A single rule failure on a single row (BUILD_SPEC §6).

    Frozen because a violation is a statement about a row that was already read: nothing
    downstream may edit the evidence, only aggregate it.
    """

    rule: str
    """Stable name of the rule that fired, e.g. ``required_field[external_id]``."""

    field: str | None
    """Record field the rule inspected, or ``None`` for whole-record rules."""

    message: str
    """Human-readable English explanation, safe to show in the data-quality UI."""

    row_index: int | None = None
    """0-based position of the row in the source batch, when the caller tracks positions."""

    def as_dict(self) -> dict[str, Any]:
        """Render as a JSON-serialisable mapping for the ``quality_report`` column."""
        return {
            "rule": self.rule,
            "field": self.field,
            "message": self.message,
            "row_index": self.row_index,
        }


@dataclass(slots=True)
class QualityReport:
    """Counters and evidence for one validation pass (BUILD_SPEC §6).

    Mutable on purpose: a pipeline streams rows through a validator and accumulates into a
    single report. Use :meth:`merge` to combine reports produced by chunked or parallel
    passes over the same source.
    """

    rows_received: int = 0
    """Rows handed to the validator, including duplicates and rejects."""

    rows_accepted: int = 0
    """Rows that passed every rule and will be written."""

    rows_rejected: int = 0
    """Rows dropped because at least one non-duplicate rule fired."""

    rows_duplicate: int = 0
    """Rows dropped solely because their natural key had already been seen."""

    violations: list[Violation] = field(default_factory=list)
    """Every violation collected, in encounter order; capped only on serialisation."""

    rule_stats: dict[str, int] = field(default_factory=dict)
    """How often each rule fired, keyed by :attr:`Violation.rule`."""

    @property
    def acceptance_rate(self) -> float:
        """Share of received rows that survived validation, in ``[0.0, 1.0]``.

        An empty batch scores ``1.0`` rather than ``0.0``: "nothing arrived" is a freshness
        problem for :class:`~autotwin_contracts.IngestionStatus`, not a quality problem, and
        scoring it as a total quality failure would mask the real cause.
        """
        if self.rows_received <= 0:
            return 1.0
        return self.rows_accepted / self.rows_received

    @property
    def outcome(self) -> IngestionOutcome:
        """Map the counters onto ``data_ingestion_runs.status``.

        Duplicates count as success — re-reading yesterday's Ladesäulenregister is the normal
        case, not a defect — so only genuine rejects downgrade a run to ``partial``.
        """
        if self.rows_rejected <= 0:
            return IngestionOutcome.success
        if self.rows_accepted > 0:
            return IngestionOutcome.partial
        return IngestionOutcome.failed

    def record_violation(self, violation: Violation) -> None:
        """Append a violation and keep :attr:`rule_stats` in step with it."""
        self.violations.append(violation)
        self.rule_stats[violation.rule] = self.rule_stats.get(violation.rule, 0) + 1

    def merge(self, other: QualityReport) -> QualityReport:
        """Combine two reports over the same source into a new report.

        Returns a fresh instance rather than mutating either operand so that a caller may keep
        per-chunk reports for debugging while reporting the total.
        """
        merged_stats = dict(self.rule_stats)
        for rule, count in other.rule_stats.items():
            merged_stats[rule] = merged_stats.get(rule, 0) + count
        return QualityReport(
            rows_received=self.rows_received + other.rows_received,
            rows_accepted=self.rows_accepted + other.rows_accepted,
            rows_rejected=self.rows_rejected + other.rows_rejected,
            rows_duplicate=self.rows_duplicate + other.rows_duplicate,
            violations=[*self.violations, *other.violations],
            rule_stats=merged_stats,
        )

    def copy(self) -> Self:
        """Shallow copy with independent violation list and statistics."""
        return replace(
            self,
            violations=list(self.violations),
            rule_stats=dict(self.rule_stats),
        )

    def as_dict(self, *, max_violations: int = MAX_STORED_VIOLATIONS) -> dict[str, Any]:
        """Render as the JSON document stored in ``data_ingestion_runs.quality_report``.

        Only plain ``str``/``int``/``float``/``bool``/``None``/``list``/``dict`` values are
        emitted, because psycopg serialises this straight into JSONB and a stray enum or
        ``datetime`` would fail the INSERT at the end of a long ingestion run.

        ``violations_truncated`` is always present (``0`` when nothing was dropped) so a
        consumer never has to guess whether the list it sees is complete.
        """
        kept = self.violations[:max_violations] if max_violations >= 0 else list(self.violations)
        return {
            "rows_received": self.rows_received,
            "rows_accepted": self.rows_accepted,
            "rows_rejected": self.rows_rejected,
            "rows_duplicate": self.rows_duplicate,
            "acceptance_rate": round(self.acceptance_rate, 6),
            "outcome": str(self.outcome),
            "rule_stats": dict(self.rule_stats),
            "violations": [violation.as_dict() for violation in kept],
            "violations_truncated": max(0, len(self.violations) - len(kept)),
        }
