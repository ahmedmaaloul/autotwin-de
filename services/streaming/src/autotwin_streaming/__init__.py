"""AutoTwin DE — Redpanda/Kafka producer, consumer and topic management (BUILD_SPEC §8).

The public surface is re-exported here so that the rest of the system talks to *one* name per
concept: the simulator asks for :func:`~autotwin_streaming.sinks.create_sink` and gets whichever
transport this deployment can actually use, the API's ``/ready`` probe calls
:func:`~autotwin_streaming.admin.check_broker`, and neither imports ``aiokafka`` itself.

Import order matters in one place: :mod:`autotwin_streaming.sinks` pulls in the database models,
so importing this package touches SQLAlchemy. That is deliberate — a caller that wants the sink
abstraction wants the database path with it, because the whole point of the abstraction is that
the two transports are interchangeable.
"""

from __future__ import annotations

from autotwin_streaming.admin import (
    TOPIC_SPECS,
    TopicDescription,
    TopicSpec,
    TopicStatus,
    check_broker,
    create_topics,
    describe_topics,
)
from autotwin_streaming.config import (
    CONSUMER_GROUP_ID,
    DEFAULT_BATCH_INTERVAL_S,
    DEFAULT_BATCH_SIZE,
    PRODUCER_NAME,
    TOPIC_RETENTION_MS,
    StreamingConfig,
)
from autotwin_streaming.consumer import ConsumerStats, TelemetryConsumer, run_consumer
from autotwin_streaming.producer import ProducerStats, TelemetryProducer
from autotwin_streaming.sinks import (
    BatchPolicy,
    DatabaseTelemetrySink,
    KafkaTelemetrySink,
    NullTelemetrySink,
    SinkStats,
    TelemetryBatchWriter,
    TelemetrySink,
    apply_trip_event,
    create_sink,
    write_telemetry_batch,
)

__version__ = "0.1.0"

__all__ = [
    "CONSUMER_GROUP_ID",
    "DEFAULT_BATCH_INTERVAL_S",
    "DEFAULT_BATCH_SIZE",
    "PRODUCER_NAME",
    "TOPIC_RETENTION_MS",
    "TOPIC_SPECS",
    "BatchPolicy",
    "ConsumerStats",
    "DatabaseTelemetrySink",
    "KafkaTelemetrySink",
    "NullTelemetrySink",
    "ProducerStats",
    "SinkStats",
    "StreamingConfig",
    "TelemetryBatchWriter",
    "TelemetryConsumer",
    "TelemetryProducer",
    "TelemetrySink",
    "TopicDescription",
    "TopicSpec",
    "TopicStatus",
    "__version__",
    "apply_trip_event",
    "check_broker",
    "create_sink",
    "create_topics",
    "describe_topics",
    "run_consumer",
    "write_telemetry_batch",
]
