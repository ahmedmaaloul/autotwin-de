"""Batch record validation — the one place every ingestion pipeline funnels its rows through.

BUILD_SPEC §6 asks each pipeline to reject bad rows rather than write them and to persist the
evidence. :class:`RecordValidator` is that mechanism: hand it the rules for a source and an
iterable of parsed records, get back the rows worth writing and a
:class:`~autotwin_core.quality.report.QualityReport` ready for the
``data_ingestion_runs.quality_report`` column.

The ``strict`` flag is the *"the source changed shape"* alarm. German open data is republished
on its own schedule and occasionally with a renamed column; when that happens, every row fails
the same rule and the pipeline would otherwise write an empty table over a perfectly good one.
Below :attr:`RecordValidator.min_acceptance_rate` the validator raises
:class:`~autotwin_core.errors.InvalidSourceData` instead, which the caller turns into a failed
ingestion run — leaving yesterday's data in place, exactly as the degrade-never-crash rule
(BUILD_SPEC §0.3) demands.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from autotwin_core.errors import InvalidSourceData
from autotwin_core.logging import get_logger
from autotwin_core.quality.report import QualityReport, Violation
from autotwin_core.quality.rules import DUPLICATE_RULE_NAME, Rule, reset_rules

__all__ = ["RecordValidator"]

_logger = get_logger(__name__)

DEFAULT_MIN_ACCEPTANCE_RATE: float = 0.5
"""Below this share of accepted rows a batch is treated as a broken source, not bad luck."""

DEFAULT_STRICT_MIN_ROWS: int = 20
"""Smallest batch the strict alarm applies to; below it the rate is statistically meaningless."""


@dataclass(slots=True)
class RecordValidator:
    """Runs a fixed rule set over a batch of records and reports what it found.

    The validator is generic over the record type at the *method* level rather than the class
    level: rules are structurally typed (they accept ``Any``), so one validator instance
    happily validates ``ChargingStationRecord`` in one call and the raw ``dict`` it was parsed
    from in the next, and the caller still gets a precisely typed list back.
    """

    rules: Sequence[Rule]
    """Rules applied to every record, in order. All of them run; none short-circuits."""

    strict: bool = False
    """Raise when the acceptance rate collapses, instead of only logging a warning."""

    min_acceptance_rate: float = DEFAULT_MIN_ACCEPTANCE_RATE
    """Acceptance rate below which a batch is considered a broken source."""

    strict_min_rows: int = DEFAULT_STRICT_MIN_ROWS
    """Batches smaller than this never trip the alarm — too few rows to conclude anything."""

    context_fn: Callable[[object], str | None] | None = None
    """Optional labeller (usually the natural key) folded into violation messages."""

    source_label: str = "records"
    """Name of the batch used in log lines and in the raised error message."""

    max_violations: int = 5_000
    """Hard cap on collected violations; counters keep counting after it is reached.

    A source that changed shape produces one violation per row. Keeping millions of them in
    memory to then throw all but 200 away on serialisation would turn a data problem into an
    out-of-memory problem.
    """

    def validate[RecordT](self, records: Iterable[RecordT]) -> tuple[list[RecordT], QualityReport]:
        """Validate a batch, returning the accepted records and the report.

        A record is *rejected* when any non-duplicate rule fires, *duplicate* when the only
        rule that fired was :data:`~autotwin_core.quality.rules.DUPLICATE_RULE_NAME`, and
        *accepted* otherwise — so ``received == accepted + rejected + duplicate`` always holds
        and the four counters of ``data_ingestion_runs`` add up.

        Raises:
            InvalidSourceData: when ``strict`` is set and the acceptance rate of a batch of at
                least :attr:`strict_min_rows` rows falls below :attr:`min_acceptance_rate`.
        """
        reset_rules(self.rules)
        report = QualityReport()
        accepted: list[RecordT] = []

        for row_index, record in enumerate(records):
            report.rows_received += 1
            context = self.context_fn(record) if self.context_fn is not None else None
            failures = self._apply_rules(record, context, row_index, report)
            if not failures:
                accepted.append(record)
                report.rows_accepted += 1
            elif failures == {DUPLICATE_RULE_NAME}:
                report.rows_duplicate += 1
            else:
                report.rows_rejected += 1

        self._check_acceptance(report)
        return accepted, report

    def validate_one(self, record: object, *, row_index: int | None = None) -> list[Violation]:
        """Validate a single record without touching batch counters.

        Useful for API-time validation of user-supplied payloads, where there is no ingestion
        run to report against. Stateful rules keep their memory across calls, so reset them
        with :func:`~autotwin_core.quality.rules.reset_rules` when starting a new series.
        """
        context = self.context_fn(record) if self.context_fn is not None else None
        violations: list[Violation] = []
        for rule in self.rules:
            message = rule(record, context)
            if message is not None:
                violations.append(
                    Violation(
                        rule=rule.rule_name,
                        field=rule.field,
                        message=message,
                        row_index=row_index,
                    )
                )
        return violations

    def _apply_rules(
        self,
        record: object,
        context: str | None,
        row_index: int,
        report: QualityReport,
    ) -> set[str]:
        """Run every rule against one record; return the names of the rules that fired.

        Every rule runs even after the first failure: a row that is both outside Germany and
        missing its operator should say so once, so that one pass over a broken file is enough
        to understand it.
        """
        fired: set[str] = set()
        for rule in self.rules:
            message = rule(record, context)
            if message is None:
                continue
            fired.add(rule.rule_name)
            if len(report.violations) < self.max_violations:
                report.record_violation(
                    Violation(
                        rule=rule.rule_name,
                        field=rule.field,
                        message=message,
                        row_index=row_index,
                    )
                )
            else:
                report.rule_stats[rule.rule_name] = report.rule_stats.get(rule.rule_name, 0) + 1
        return fired

    def _check_acceptance(self, report: QualityReport) -> None:
        """Apply the "source changed shape" alarm to a finished batch."""
        if report.rows_received < self.strict_min_rows:
            return
        rate = report.acceptance_rate
        if rate >= self.min_acceptance_rate:
            return
        worst = self._worst_rule(report)
        summary = (
            f"{self.source_label}: only {report.rows_accepted}/{report.rows_received} rows "
            f"accepted ({rate:.1%}, minimum {self.min_acceptance_rate:.0%}); "
            f"most frequent rule: {worst}"
        )
        if self.strict:
            raise InvalidSourceData(summary)
        _logger.warning(f"Quality threshold missed — {summary}")

    @staticmethod
    def _worst_rule(report: QualityReport) -> str:
        """Name and count of the rule that fired most often, for the alarm message."""
        if not report.rule_stats:
            return "none"
        rule, count = max(report.rule_stats.items(), key=lambda item: item[1])
        return f"{rule} ({count}x)"
