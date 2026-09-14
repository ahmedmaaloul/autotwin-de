"""``python -m autotwin_streaming.cli`` — the streaming service's command-line contract.

BUILD_SPEC §15 fixes the surface:

.. code-block:: text

    python -m autotwin_streaming.cli consume [--topic ...] [--batch-size N] [--from-beginning]
    python -m autotwin_streaming.cli create-topics

Both commands accept ``--log-level`` and ``--json-logs``, exit ``0`` on success, ``1`` on a
handled failure and ``2`` on bad arguments. The ``Makefile``, the Compose images and the Airflow
DAGs call these exact strings, so the parser is part of the contract and not an afterthought.

``argparse`` rather than Click or Typer, per §15: two commands and six flags do not justify a
runtime dependency, and argparse already gives the ``2`` exit code for a usage error for free.

Structured logs go to **stderr** (``autotwin_core.logging`` configures that), so the one-line
human summary each command prints to **stdout** stays greppable even with ``--json-logs``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from typing import Final

from autotwin_contracts import TOPIC_TELEMETRY, TOPIC_TRIP_EVENTS
from autotwin_core.config import LogFormat, get_settings
from autotwin_core.errors import AutoTwinError
from autotwin_core.logging import configure_logging, get_logger
from autotwin_streaming.admin import create_topics, describe_topics
from autotwin_streaming.config import StreamingConfig
from autotwin_streaming.consumer import run_consumer

__all__ = ["build_parser", "main"]

_LOGGER = get_logger(__name__)

EXIT_OK: Final[int] = 0
"""The command did what it was asked to do."""

EXIT_FAILURE: Final[int] = 1
"""A handled failure: the broker was down, the database refused the write."""

EXIT_USAGE: Final[int] = 2
"""Bad arguments. argparse exits with this on its own; the constant documents the contract."""

_CONSUMABLE_TOPIC_CHOICES: Final[tuple[str, ...]] = (TOPIC_TELEMETRY, TOPIC_TRIP_EVENTS)
"""Topics ``consume`` accepts. Offered as ``choices`` so a typo is a usage error at parse time
rather than a consumer that silently waits forever on a topic nobody publishes to."""


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    A function rather than a module-level constant so that the tests can build a fresh parser,
    and so importing this module has no side effects beyond defining names.
    """
    parser = argparse.ArgumentParser(
        prog="python -m autotwin_streaming.cli",
        description="AutoTwin DE streaming transport: topic management and the telemetry consumer.",
    )
    _add_common_flags(parser)
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    consume = subparsers.add_parser(
        "consume",
        help="read events from Redpanda and persist them to PostGIS",
        description=(
            "Join the autotwin-telemetry-consumer group, validate each message, batch it and "
            "bulk-insert into the telemetry table. Offsets are committed only after the "
            "database write succeeds. Stops cleanly on SIGINT/SIGTERM, flushing the batch."
        ),
    )
    _add_common_flags(consume)
    consume.add_argument(
        "--topic",
        action="append",
        dest="topics",
        choices=_CONSUMABLE_TOPIC_CHOICES,
        metavar="TOPIC",
        help=(
            "topic to consume; repeatable. "
            f"Default: {TOPIC_TELEMETRY}. Add {TOPIC_TRIP_EVENTS} to also close out trips."
        ),
    )
    consume.add_argument(
        "--batch-size",
        type=_positive_int,
        metavar="N",
        help="rows to buffer before writing them in one INSERT (default: 500)",
    )
    consume.add_argument(
        "--batch-interval",
        type=_positive_float,
        metavar="SECONDS",
        help="maximum age of a partial batch before it is written anyway (default: 1.0)",
    )
    consume.add_argument(
        "--from-beginning",
        action="store_true",
        help=(
            "start at the earliest retained offset when the group has no committed offset yet; "
            "without it a fresh group starts at the end of the log"
        ),
    )

    create = subparsers.add_parser(
        "create-topics",
        help="create AutoTwin's topics with the retention of BUILD_SPEC §8 (idempotent)",
        description=(
            "Create vehicle.telemetry.v1, vehicle.trip-events.v1, traffic.events.v1 and "
            "charging.events.v1 if they do not exist. Existing topics are reported and left "
            "untouched — retention and partition counts are never changed under a running "
            "cluster, because both discard or re-key data."
        ),
    )
    _add_common_flags(create)

    return parser


def _add_common_flags(parser: argparse.ArgumentParser) -> None:
    """Attach ``--log-level`` and ``--json-logs`` (BUILD_SPEC §15).

    Added to the top-level parser *and* to each subparser so that both
    ``cli --json-logs consume`` and ``cli consume --json-logs`` work. Operators reach for the
    second spelling and shell history produces the first.
    """
    parser.add_argument(
        "--log-level",
        default=None,
        metavar="LEVEL",
        help="log level name, e.g. DEBUG or INFO (default: AUTOTWIN_LOG_LEVEL)",
    )
    parser.add_argument(
        "--json-logs",
        action="store_true",
        default=None,
        help="render logs as one JSON object per line instead of console output",
    )


def _positive_int(raw: str) -> int:
    """Parse a strictly positive integer, or raise the error argparse turns into exit code 2."""
    try:
        value = int(raw)
    except ValueError as exc:
        msg = f"{raw!r} is not an integer"
        raise argparse.ArgumentTypeError(msg) from exc
    if value <= 0:
        msg = f"must be greater than zero, got {value}"
        raise argparse.ArgumentTypeError(msg)
    return value


def _positive_float(raw: str) -> float:
    """Parse a strictly positive float, or raise the error argparse turns into exit code 2."""
    try:
        value = float(raw)
    except ValueError as exc:
        msg = f"{raw!r} is not a number"
        raise argparse.ArgumentTypeError(msg) from exc
    if value <= 0.0:
        msg = f"must be greater than zero, got {value}"
        raise argparse.ArgumentTypeError(msg)
    return value


def _configure(namespace: argparse.Namespace) -> None:
    """Apply the logging flags on top of the configured settings.

    ``Settings`` is not mutated: a copy is built with ``model_copy`` so that the process-wide
    cached settings keep describing the environment, and only the log rendering follows the
    flag. Mutating the cached object would make ``get_settings()`` disagree with ``.env`` for
    every other module in the process.
    """
    settings = get_settings()
    overrides: dict[str, object] = {}
    if namespace.log_level:
        overrides["log_level"] = str(namespace.log_level).upper()
    if namespace.json_logs:
        overrides["log_format"] = LogFormat.json
    configure_logging(settings.model_copy(update=overrides) if overrides else settings)


async def _run_consume(namespace: argparse.Namespace) -> int:
    """Run the consumer until it is signalled, then print a one-line summary."""
    config = StreamingConfig.from_settings().with_overrides(
        batch_size=namespace.batch_size,
        batch_interval_s=namespace.batch_interval,
    )
    topics = tuple(namespace.topics) if namespace.topics else (TOPIC_TELEMETRY,)
    stats = await run_consumer(
        config,
        topics=topics,
        from_beginning=namespace.from_beginning,
    )
    # The one deliberate print: BUILD_SPEC §15 asks every command to end with a
    # human-readable summary on stdout, while the structured log goes to stderr.
    print(
        f"consumed {stats.messages_consumed} message(s) from {', '.join(topics)}: "
        f"{stats.rows_written} telemetry row(s), "
        f"{stats.trip_events_applied} trip event(s), "
        f"{stats.batches} batch(es), "
        f"{stats.invalid_messages} invalid, "
        f"{stats.errors} error(s)"
    )
    return EXIT_OK


async def _run_create_topics(_namespace: argparse.Namespace) -> int:
    """Create the topics, then describe them back so the operator sees what the broker has."""
    config = StreamingConfig.from_settings()
    statuses = await create_topics(config)
    for status in statuses:
        print(f"  {status.name:<26} {status.detail}")

    descriptions = await describe_topics(config)
    drifted = [description.name for description in descriptions if not description.matches_spec]
    for description in descriptions:
        retention = (
            f"{description.retention_ms} ms" if description.retention_ms is not None else "unknown"
        )
        marker = " (differs from BUILD_SPEC §8)" if not description.matches_spec else ""
        print(
            f"  {description.name:<26} partitions={description.partitions} "
            f"replicas={description.replication_factor} retention={retention}{marker}"
        )
    if drifted:
        # Not a failure: an operator may have resized a topic on purpose, and this command is
        # not allowed to shrink retention or re-partition a live topic behind their back.
        _LOGGER.warning("kafka.topics.drift", topics=drifted)
    created = sum(1 for status in statuses if status.created)
    print(
        f"{created} topic(s) created, {len(statuses) - created} already present "
        f"on {config.bootstrap_servers}"
    )
    return EXIT_OK


async def _dispatch(namespace: argparse.Namespace) -> int:
    """Run the selected subcommand, mapping a handled failure onto exit code 1."""
    handlers = {
        "consume": _run_consume,
        "create-topics": _run_create_topics,
    }
    handler = handlers[namespace.command]
    try:
        return await handler(namespace)
    except AutoTwinError as exc:
        # Everything AutoTwin raises deliberately is a handled failure: report it and exit 1.
        # Anything else is a bug and is allowed to propagate with its traceback intact.
        _LOGGER.error(
            "cli.command_failed",
            command=namespace.command,
            code=exc.code,
            message=exc.message,
            **exc.details,
        )
        print(f"error: {exc.message}", file=sys.stderr)
        return EXIT_FAILURE


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: parse, configure logging, dispatch, and return the process exit code."""
    parser = build_parser()
    namespace = parser.parse_args(argv)
    _configure(namespace)
    try:
        return asyncio.run(_dispatch(namespace))
    except KeyboardInterrupt:
        # Reachable when the signal arrives before the consumer has installed its own handler.
        _LOGGER.info("cli.interrupted", command=namespace.command)
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
