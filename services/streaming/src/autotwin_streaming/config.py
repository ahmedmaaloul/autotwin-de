"""Transport-level configuration for the Redpanda/Kafka client code (BUILD_SPEC §8).

:class:`~autotwin_core.config.Settings` owns everything an *operator* configures — the
bootstrap servers, the client id, whether Kafka is used at all. What it deliberately does not
own is the handful of knobs that describe how AutoTwin *uses* a Kafka client: linger windows,
batch sizes, the consumer group name, topic retention. Those are engineering decisions about
this workload, not deployment settings, and putting them in ``.env`` would invite a production
incident caused by someone "tuning" ``linger_ms`` without knowing why it was 20 ms.

So this module holds them as a frozen dataclass derived from ``Settings``. Callers that need to
override one — a test, the CLI's ``--batch-size`` flag — use
:meth:`StreamingConfig.with_overrides`, which keeps the object immutable and the override
visible at the call site. Nothing here reads ``os.environ``; that rule (BUILD_SPEC §5) has
exactly one exception in the repository and it lives in ``autotwin_core.config``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Final

from autotwin_contracts import (
    TOPIC_CHARGING_EVENTS,
    TOPIC_TELEMETRY,
    TOPIC_TRAFFIC_EVENTS,
    TOPIC_TRIP_EVENTS,
)
from autotwin_core.config import Settings, get_settings

__all__ = [
    "CONSUMER_GROUP_ID",
    "DEFAULT_BATCH_INTERVAL_S",
    "DEFAULT_BATCH_SIZE",
    "PRODUCER_NAME",
    "TOPIC_PARTITIONS",
    "TOPIC_REPLICATION_FACTOR",
    "TOPIC_RETENTION_MS",
    "StreamingConfig",
]

CONSUMER_GROUP_ID: Final[str] = "autotwin-telemetry-consumer"
"""Consumer group of the telemetry writer (BUILD_SPEC §8).

Fixed rather than derived from the hostname: the group id *is* the offset bookmark, so a
consumer that restarts under a new name would silently replay or skip the log.
"""

PRODUCER_NAME: Final[str] = "autotwin-simulator"
"""Value of ``EventEnvelope.producer`` on everything this package publishes.

Every message currently on the bus originates in the simulator — the ingestion pipelines write
straight to Postgres. If that ever changes, the producer name becomes a constructor argument
rather than a constant, and the envelope keeps telling the truth either way.
"""

DEFAULT_BATCH_SIZE: Final[int] = 500
"""Rows per bulk INSERT before the consumer flushes (BUILD_SPEC §8)."""

DEFAULT_BATCH_INTERVAL_S: Final[float] = 1.0
"""Maximum age of a partial batch before it is flushed anyway, in seconds.

Bounds end-to-end latency: the live map polls ``/api/v1/stream/telemetry`` about once a second,
so holding a half-full batch longer than that would show the operator a stale fleet.
"""

_RETENTION_6H_MS: Final[int] = 6 * 60 * 60 * 1000
_RETENTION_24H_MS: Final[int] = 24 * 60 * 60 * 1000

TOPIC_RETENTION_MS: Final[dict[str, int]] = {
    TOPIC_TELEMETRY: _RETENTION_6H_MS,
    TOPIC_TRIP_EVENTS: _RETENTION_24H_MS,
    TOPIC_TRAFFIC_EVENTS: _RETENTION_24H_MS,
    TOPIC_CHARGING_EVENTS: _RETENTION_24H_MS,
}
"""Retention per topic, exactly as BUILD_SPEC §8 tabulates it.

Short by Kafka standards on purpose: PostgreSQL is the system of record and the log is a
transport, not an archive. Six hours is enough to survive a consumer restart or a lunch break,
and not enough to fill a laptop disk with simulated telemetry.
"""

TOPIC_PARTITIONS: Final[int] = 3
"""Partitions per topic — matches ``infra/scripts/create-topics.sh``.

Three lets the telemetry consumer be scaled to three processes without re-partitioning, while
still keeping per-``vehicle_id`` ordering: a vehicle's samples always hash to one partition, so
odometer and SOC can never arrive out of order for the same vehicle.
"""

TOPIC_REPLICATION_FACTOR: Final[int] = 1
"""Single-broker development cluster. Anything else would fail to create the topic."""


@dataclass(frozen=True, slots=True)
class StreamingConfig:
    """Everything the producer, consumer and admin client need, resolved once.

    Frozen so that a long-running consumer cannot be reconfigured underneath itself, and so a
    config object can be shared between the sink and the producer without defensive copying.
    """

    bootstrap_servers: str
    """Comma-separated broker list, e.g. ``localhost:19092``."""

    client_id: str
    """Prefix reported to the broker; each component appends its own role suffix."""

    enabled: bool
    """Mirror of ``Settings.kafka_enabled`` — the switch :func:`.sinks.create_sink` reads."""

    group_id: str = CONSUMER_GROUP_ID
    """Consumer group of the telemetry writer."""

    batch_size: int = DEFAULT_BATCH_SIZE
    """Rows the consumer buffers before writing them in one INSERT."""

    batch_interval_s: float = DEFAULT_BATCH_INTERVAL_S
    """Maximum age of a partial batch, in seconds."""

    linger_ms: int = 20
    """How long the producer waits to fill a request before sending it.

    Zero — aiokafka's default — sends one request per ``send()``, which at forty vehicles
    ticking every simulated second is forty round trips a second for a few hundred bytes each.
    Twenty milliseconds is below human perception on the live map and collapses that into a
    handful of batched requests.
    """

    max_batch_size_bytes: int = 64 * 1024
    """Producer-side accumulator size per partition. Four times aiokafka's 16 KiB default,
    because a telemetry envelope is ~700 bytes of JSON and 16 KiB fills after ~23 messages."""

    compression_type: str | None = "lz4"
    """Wire compression for produced batches.

    ``lz4`` compresses this payload — highly repetitive JSON — to roughly a third at a fraction
    of gzip's CPU, which matters because the compressor runs inside the simulator's tick loop.
    The codec comes from ``cramjam``, which the service depends on for the *consumer* side
    anyway: aiokafka ships only gzip, and a consumer has no say in the codec a foreign producer
    chose (``rpk topic produce`` defaults to snappy). Having paid for the wheel once, the
    producer may as well use the better codec.
    """

    request_timeout_ms: int = 20_000
    """Broker request timeout. Matches ``Settings.http_timeout_s`` so that every remote call in
    AutoTwin gives up on roughly the same horizon."""

    session_timeout_ms: int = 30_000
    """Consumer liveness window. Comfortably larger than the batch interval plus a slow bulk
    INSERT, so a GC pause or a busy database cannot trigger a needless rebalance."""

    max_poll_interval_ms: int = 300_000
    """Upper bound on the time between two ``getmany`` calls before the group evicts us."""

    broker_probe_timeout_s: float = 3.0
    """How long ``/ready`` and :func:`.sinks.create_sink` wait for the broker to answer.

    Short on purpose: this is a "is the transport there?" question asked on a hot path, and the
    answer *no* has a working fallback. Waiting twenty seconds to discover it would turn a
    degraded mode into an outage.
    """

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> StreamingConfig:
        """Build the transport configuration from the process-wide settings."""
        resolved = settings if settings is not None else get_settings()
        return cls(
            bootstrap_servers=resolved.kafka_bootstrap_servers,
            client_id=resolved.kafka_client_id,
            enabled=resolved.kafka_enabled,
        )

    def with_overrides(
        self,
        *,
        batch_size: int | None = None,
        batch_interval_s: float | None = None,
        group_id: str | None = None,
        enabled: bool | None = None,
    ) -> StreamingConfig:
        """Return a copy with the given fields replaced, leaving this instance untouched.

        Only the knobs a CLI flag or a test legitimately overrides are exposed. Widening this
        signature is a deliberate act, which is the point.
        """
        return replace(
            self,
            batch_size=self.batch_size if batch_size is None else batch_size,
            batch_interval_s=(
                self.batch_interval_s if batch_interval_s is None else batch_interval_s
            ),
            group_id=self.group_id if group_id is None else group_id,
            enabled=self.enabled if enabled is None else enabled,
        )

    def component_client_id(self, role: str) -> str:
        """Client id reported to the broker by one component, e.g. ``autotwin-producer``.

        Redpanda's ``rpk group describe`` and the Console list clients by this id; a shared
        ``autotwin`` for producer, consumer and admin would make a misbehaving component
        impossible to pick out of the broker's own logs.
        """
        return f"{self.client_id}-{role}"

    @property
    def bootstrap_server_list(self) -> list[str]:
        """Broker list split for the aiokafka constructors, which accept either form."""
        return [part.strip() for part in self.bootstrap_servers.split(",") if part.strip()]
