"""``python -m autotwin_ingestion.cli`` — the ingestion command line (BUILD_SPEC §15).

```
ingest charging  [--mode live|cached|fixture] [--dry-run] [--limit N]
ingest weather   [--mode ...] [--stations N] [--dry-run]
ingest traffic   [--mode ...] [--roads A5,A8] [--dry-run]
seed routes                      # the demo corridors, via the routing provider
seed demo        [--reset]       # everything `make demo` needs
quality report                   # the latest report per source
```

``argparse`` rather than Click or Typer: the surface is six commands, and the Makefile, the
Airflow DAGs and the Docker images all invoke these strings verbatim, so the dependency would
buy nothing and cost a pin.

Three conventions hold for every command:

* **Exit codes.** ``0`` on success, ``1`` on a handled failure — a source that is down, a
  file that changed shape — and ``2`` on bad arguments, which argparse produces itself. A
  ``partial`` ingestion exits ``0``: German open data ships a handful of broken rows most
  weeks, and failing the nightly job over twelve stations out of 117 000 trains everyone to
  ignore the exit code.
* **Logs go to stderr, the summary to stdout.** Structured logging is configured from the
  settings and from ``--log-level`` / ``--json-logs``; the only thing written to stdout is the
  deliberate human-readable summary each command ends with, so ``… | jq`` and ``… > report``
  both stay useful.
* **Every command that touches an external source leaves a ``data_ingestion_runs`` row**,
  including when it fails. That is the pipeline template's job, not this module's, but it is
  the reason a failing command still exits through the same summary path.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from typing import Any, Final

import sqlalchemy as sa

from autotwin_contracts import IngestionOutcome, SourceSystem
from autotwin_core.config import DataMode, LogFormat, Settings, get_settings
from autotwin_core.db import DataIngestionRun, dispose_engine, session_scope
from autotwin_core.errors import AutoTwinError
from autotwin_core.logging import configure_logging, get_logger
from autotwin_ingestion.demo import seed_demo
from autotwin_ingestion.lake import default_lake
from autotwin_ingestion.pipelines import (
    ChargingIngestionPipeline,
    IngestionResult,
    RouteSeedPipeline,
    TrafficIngestionPipeline,
    WeatherIngestionPipeline,
)

__all__ = ["build_parser", "main"]

_logger = get_logger(__name__)

EXIT_OK: Final[int] = 0
"""The command did what it was asked to do."""

EXIT_FAILURE: Final[int] = 1
"""A handled failure: the source was unreachable, or its shape no longer parses."""

EXIT_USAGE: Final[int] = 2
"""Bad arguments. argparse exits with this itself; named here so the contract is visible."""

_RULE: Final[str] = "─" * 72
"""Separator of the human-readable summary block."""


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for every command of BUILD_SPEC §15."""
    parser = argparse.ArgumentParser(
        prog="python -m autotwin_ingestion.cli",
        description="Ingest German open data into AutoTwin DE and seed the demo.",
    )
    _add_global_flags(parser)
    commands = parser.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser("ingest", help="Ingest one external source.")
    sources = ingest.add_subparsers(dest="source", required=True)

    charging = sources.add_parser(
        "charging",
        help="Bundesnetzagentur Ladesäulenregister → charging_stations, charging_points.",
    )
    _add_ingest_flags(charging)
    charging.add_argument(
        "--limit",
        type=_positive_int,
        default=None,
        metavar="N",
        help="Stop after N sites. Useful against the full 117 000-row register.",
    )

    weather = sources.add_parser(
        "weather",
        help="DWD 10-minute observations → weather_stations, weather_observations.",
    )
    _add_ingest_flags(weather)
    weather.add_argument(
        "--stations",
        type=_positive_int,
        default=None,
        metavar="N",
        help="Contact at most N DWD stations (default: the adapter's own cap).",
    )

    traffic = sources.add_parser(
        "traffic",
        help="Autobahn GmbH roadworks, closures and warnings → traffic_events.",
    )
    _add_ingest_flags(traffic)
    traffic.add_argument(
        "--roads",
        type=str,
        default=None,
        metavar="A5,A8",
        help="Comma-separated road ids to poll (default: A1 through A9).",
    )

    seed = commands.add_parser("seed", help="Seed derived and demo data.")
    targets = seed.add_subparsers(dest="target", required=True)

    routes = targets.add_parser("routes", help="The demo corridors, via the routing provider.")
    _add_ingest_flags(routes)

    demo = targets.add_parser("demo", help="Everything `make demo` needs.")
    _add_mode_flag(demo)
    demo.add_argument(
        "--reset",
        action="store_true",
        help="Delete the ingested and simulated data first (vehicle profiles are kept).",
    )
    demo.add_argument(
        "--vehicles",
        type=_positive_int,
        default=None,
        metavar="N",
        help="Simulated vehicles to create (default: AUTOTWIN_SIM_DEFAULT_VEHICLES).",
    )
    demo.add_argument(
        "--charging-limit",
        type=_positive_int,
        default=None,
        metavar="N",
        help="Cap the charging sites ingested, for a faster demo.",
    )

    quality = commands.add_parser("quality", help="Report on ingested data quality.")
    reports = quality.add_subparsers(dest="report", required=True)
    report = reports.add_parser("report", help="The latest ingestion report per source.")
    report.add_argument(
        "--source",
        type=str,
        default=None,
        metavar="SOURCE",
        help="Restrict to one source system, e.g. bundesnetzagentur.",
    )
    report.add_argument(
        "--limit",
        type=_positive_int,
        default=10,
        metavar="N",
        help="Recent runs to list below the per-source summary (default: 10).",
    )
    return parser


def _add_global_flags(parser: argparse.ArgumentParser) -> None:
    """Add the flags BUILD_SPEC §15 requires on every command."""
    parser.add_argument(
        "--log-level",
        type=str,
        default=None,
        metavar="LEVEL",
        help="Override AUTOTWIN_LOG_LEVEL for this run, e.g. DEBUG.",
    )
    parser.add_argument(
        "--json-logs",
        action="store_true",
        help="Emit JSON log lines on stderr instead of the console renderer.",
    )


def _add_mode_flag(parser: argparse.ArgumentParser) -> None:
    """Add ``--mode``, the per-run override of ``AUTOTWIN_DATA_MODE``."""
    parser.add_argument(
        "--mode",
        type=DataMode,
        choices=list(DataMode),
        default=None,
        help=(
            "Provider policy: live (fail if the source is down), cached (try live, fall back) "
            "or fixture (never leave the machine). Default: AUTOTWIN_DATA_MODE."
        ),
    )


def _add_ingest_flags(parser: argparse.ArgumentParser) -> None:
    """Add the flags shared by every ingesting command."""
    _add_mode_flag(parser)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and validate, write nothing — not even a data_ingestion_runs row.",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns the process exit code rather than calling ``sys.exit``."""
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = _configure(args)
    try:
        return asyncio.run(_dispatch(args, settings))
    except KeyboardInterrupt:  # pragma: no cover - interactive use
        print("\nInterrupted.", file=sys.stderr)
        return EXIT_FAILURE


def _configure(args: argparse.Namespace) -> Settings:
    """Apply ``--log-level`` and ``--json-logs`` on top of the configured settings.

    The overrides are applied to a *copy* of the settings rather than to the environment: the
    ban on reading ``os.environ`` outside :mod:`autotwin_core.config` (BUILD_SPEC §5) would be
    a dead letter if the CLI wrote to it.
    """
    settings = get_settings()
    overrides: dict[str, Any] = {}
    if args.log_level:
        overrides["log_level"] = args.log_level
    if args.json_logs:
        overrides["log_format"] = LogFormat.json
    if overrides:
        settings = settings.model_copy(update=overrides)
    configure_logging(settings)
    return settings


async def _dispatch(args: argparse.Namespace, settings: Settings) -> int:
    """Route to the requested command and always release the database engine."""
    try:
        match (args.command, getattr(args, "source", None) or getattr(args, "target", None)):
            case ("ingest", "charging"):
                return await _run_pipeline(
                    ChargingIngestionPipeline(
                        settings=settings,
                        data_mode=args.mode,
                        dry_run=args.dry_run,
                        limit=args.limit,
                    )
                )
            case ("ingest", "weather"):
                return await _run_pipeline(
                    WeatherIngestionPipeline(
                        settings=settings,
                        data_mode=args.mode,
                        dry_run=args.dry_run,
                        max_stations=args.stations,
                    )
                )
            case ("ingest", "traffic"):
                return await _run_pipeline(
                    TrafficIngestionPipeline(
                        settings=settings,
                        data_mode=args.mode,
                        dry_run=args.dry_run,
                        roads=_parse_roads(args.roads),
                    )
                )
            case ("seed", "routes"):
                return await _run_pipeline(
                    RouteSeedPipeline(
                        settings=settings,
                        data_mode=args.mode,
                        dry_run=args.dry_run,
                    )
                )
            case ("seed", "demo"):
                return await _seed_demo(args, settings)
            case ("quality", _):
                return await _quality_report(args)
            case _:  # pragma: no cover - argparse rejects unknown commands first
                print(f"unknown command: {args.command}", file=sys.stderr)
                return EXIT_USAGE
    finally:
        await dispose_engine()


async def _run_pipeline(pipeline: Any) -> int:
    """Run one pipeline and print its summary, whatever the outcome."""
    try:
        result: IngestionResult = await pipeline.run()
    except AutoTwinError as error:
        # The run row has already been closed as failed by the template; this is only the
        # human-facing half of the same fact.
        _print_block(
            f"{pipeline.name} FAILED",
            [
                f"source          {pipeline.source.value}",
                f"error           {type(error).__name__}: {error}",
                "",
                "The failure is recorded in data_ingestion_runs and shown on /data-quality.",
            ],
        )
        return EXIT_FAILURE
    finally:
        await pipeline.aclose()

    _print_block(f"{result.pipeline} {result.status.value}", result.summary_lines())
    return EXIT_OK if result.succeeded else EXIT_FAILURE


async def _seed_demo(args: argparse.Namespace, settings: Settings) -> int:
    """Seed the whole demo and print what it produced, per source."""
    summary = await seed_demo(
        settings=settings,
        data_mode=args.mode,
        reset=args.reset,
        vehicle_count=args.vehicles,
        charging_limit=args.charging_limit,
    )
    _print_block("demo seeded" if summary.succeeded else "demo incomplete", summary.summary_lines())
    if not summary.succeeded:
        print(
            "The demo is missing corridors or charging infrastructure. "
            "Re-run with --mode fixture to build it entirely from the bundled samples.",
            file=sys.stderr,
        )
        return EXIT_FAILURE
    return EXIT_OK


async def _quality_report(args: argparse.Namespace) -> int:
    """Print the latest ingestion report per source, then the recent run history.

    Reads the same rows ``GET /api/v1/data/quality`` serves, so a number questioned on the
    dashboard can be checked from a terminal without running the API.
    """
    source = _parse_source(args.source)
    if args.source and source is None:
        known = ", ".join(member.value for member in SourceSystem)
        print(f"unknown source {args.source!r}; known: {known}", file=sys.stderr)
        return EXIT_USAGE

    async with session_scope() as session:
        latest = await _latest_per_source(session, source)
        recent = await _recent_runs(session, source, args.limit)

    if not latest:
        _print_block(
            "data quality",
            [
                "No ingestion has run yet.",
                "",
                "Run: python -m autotwin_ingestion.cli seed demo",
            ],
        )
        return EXIT_OK

    lines: list[str] = [
        f"{'source':<18} {'status':<9} {'mode':<8} {'received':>9} {'accepted':>9} "
        f"{'rejected':>9} {'dupes':>7}  finished",
    ]
    for run in latest:
        report = run.quality_report or {}
        lines.append(
            f"{run.source.value:<18} {_status_of(run):<9} {run.provider_mode.value:<8} "
            f"{run.rows_received:>9,} {run.rows_accepted:>9,} {run.rows_rejected:>9,} "
            f"{run.rows_duplicate:>7,}  "
            f"{run.finished_at.isoformat(timespec='seconds') if run.finished_at else 'in flight'}"
        )
        for rule, count in sorted(
            dict(report.get("rule_stats") or {}).items(),
            key=lambda item: -int(item[1]),
        )[:3]:
            lines.append(f"    rule {rule:<40} x{count}")
        if run.error_message:
            lines.append(f"    error {run.error_message}")

    lines.append("")
    lines.append(f"recent runs (newest first, {len(recent)})")
    for run in recent:
        lines.append(
            f"  {run.started_at.isoformat(timespec='seconds')}  {run.pipeline:<26} "
            f"{_status_of(run):<9} {run.provider_mode.value:<8} "
            f"{run.rows_accepted:>8,} accepted"
        )

    datasets = default_lake().describe()
    if datasets:
        lines.append("")
        lines.append("lake")
        for dataset in datasets:
            lines.append(
                f"  {dataset.view_name:<32} {dataset.files:>3} part(s)  "
                f"{dataset.size_bytes / 1024:>9,.0f} KiB  "
                f"partitions: {', '.join(dataset.partitions) or 'none'}"
            )

    _print_block("data quality", lines)
    failed = [
        run
        for run in latest
        if run.finished_at is not None and run.status is IngestionOutcome.failed
    ]
    return EXIT_FAILURE if failed else EXIT_OK


def _status_of(run: DataIngestionRun) -> str:
    """Render a run's status, distinguishing "still going" from "finished badly".

    A row opens as ``failed`` with ``finished_at`` null so that a killed process leaves
    evidence of failure rather than a phantom success (see the pipeline template). Printing
    that raw would call a running ingestion a failure, so the two are told apart here.
    """
    return "running" if run.finished_at is None else run.status.value


async def _latest_per_source(
    session: Any,
    source: SourceSystem | None,
) -> list[DataIngestionRun]:
    """The most recent run for each source system, newest source first.

    ``DISTINCT ON`` rather than a window function or a correlated subquery: PostgreSQL walks
    the ``(source, started_at DESC)`` index of BUILD_SPEC §3.2 and stops at the first row per
    source, which is exactly the shape of this question.
    """
    statement = (
        sa.select(DataIngestionRun)
        .distinct(DataIngestionRun.source)
        .order_by(DataIngestionRun.source, DataIngestionRun.started_at.desc())
    )
    if source is not None:
        statement = statement.where(DataIngestionRun.source == source)
    return list((await session.execute(statement)).scalars().all())


async def _recent_runs(
    session: Any,
    source: SourceSystem | None,
    limit: int,
) -> list[DataIngestionRun]:
    """The most recent runs across all sources."""
    statement = (
        sa.select(DataIngestionRun).order_by(DataIngestionRun.started_at.desc()).limit(limit)
    )
    if source is not None:
        statement = statement.where(DataIngestionRun.source == source)
    return list((await session.execute(statement)).scalars().all())


def _parse_roads(raw: str | None) -> tuple[str, ...] | None:
    """Parse ``--roads A5,A8`` into road ids, tolerating the API's own trailing spaces."""
    if not raw:
        return None
    roads = tuple(part.strip() for part in raw.split(",") if part.strip())
    return roads or None


def _parse_source(raw: str | None) -> SourceSystem | None:
    """Resolve ``--source`` to a :class:`SourceSystem`, or ``None`` when it is unknown."""
    if not raw:
        return None
    try:
        return SourceSystem(raw.strip().lower())
    except ValueError:
        return None


def _positive_int(raw: str) -> int:
    """argparse type for a count that must be at least one."""
    try:
        value = int(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{raw!r} is not an integer") from error
    if value < 1:
        raise argparse.ArgumentTypeError(f"{raw!r} must be at least 1")
    return value


def _print_block(title: str, lines: Sequence[str]) -> None:
    """Print the deliberate human-readable summary — the one thing that goes to stdout."""
    print(_RULE)
    print(f"  {title}")
    print(_RULE)
    for line in lines:
        print(f"  {line}" if line else "")
    print(_RULE)


if __name__ == "__main__":
    raise SystemExit(main())
