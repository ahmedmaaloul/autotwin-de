"""Topic management and broker reachability (BUILD_SPEC §8).

``infra/scripts/create-topics.sh`` does the same job with ``rpk`` inside Compose. This module
exists because the shell script cannot be called from three of the places that need it: the
``/ready`` probe, ``python -m autotwin_streaming.cli create-topics`` on a developer machine
with no ``rpk`` binary, and the integration tests. Both paths derive their topic list from
:data:`~autotwin_contracts.ALL_TOPICS` and their retention from
:data:`~autotwin_streaming.config.TOPIC_RETENTION_MS`, so they cannot drift apart.

Everything here is idempotent. Creating a topic that already exists is the normal path on every
restart, not an error, and is reported as such rather than swallowed — an operator who asked
for a topic deserves to know whether they got a new one or an old one.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Final

from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.admin.config_resource import ConfigResource, ConfigResourceType
from aiokafka.errors import KafkaError, TopicAlreadyExistsError, for_code

from autotwin_contracts import ALL_TOPICS
from autotwin_core.errors import ProviderUnavailable
from autotwin_core.logging import get_logger
from autotwin_streaming.config import (
    TOPIC_PARTITIONS,
    TOPIC_REPLICATION_FACTOR,
    TOPIC_RETENTION_MS,
    StreamingConfig,
)

__all__ = [
    "TOPIC_SPECS",
    "TopicDescription",
    "TopicSpec",
    "TopicStatus",
    "check_broker",
    "create_topics",
    "describe_topics",
]

_LOGGER = get_logger(__name__)

_RETENTION_CONFIG_KEY: Final[str] = "retention.ms"
_CLEANUP_POLICY_KEY: Final[str] = "cleanup.policy"
_NO_ERROR: Final[int] = 0


@dataclass(frozen=True, slots=True)
class TopicSpec:
    """The desired shape of one AutoTwin topic."""

    name: str
    """Topic name, e.g. ``vehicle.telemetry.v1``."""

    partitions: int
    """Partition count; also the maximum useful consumer parallelism for the topic."""

    replication_factor: int
    """Replicas per partition. One on the single-broker development cluster."""

    retention_ms: int
    """How long the broker keeps a record before deleting it."""

    @property
    def topic_configs(self) -> dict[str, str]:
        """Per-topic broker configuration.

        ``cleanup.policy=delete`` is stated rather than inherited: these are event streams, and
        a cluster whose default happened to be ``compact`` would silently collapse a vehicle's
        telemetry history down to its latest sample per key — exactly the data the consumer
        exists to persist.
        """
        return {
            _RETENTION_CONFIG_KEY: str(self.retention_ms),
            _CLEANUP_POLICY_KEY: "delete",
        }

    def as_new_topic(self) -> NewTopic:
        """The aiokafka request object for this specification."""
        return NewTopic(
            name=self.name,
            num_partitions=self.partitions,
            replication_factor=self.replication_factor,
            topic_configs=self.topic_configs,
        )


TOPIC_SPECS: Final[tuple[TopicSpec, ...]] = tuple(
    TopicSpec(
        name=topic,
        partitions=TOPIC_PARTITIONS,
        replication_factor=TOPIC_REPLICATION_FACTOR,
        retention_ms=TOPIC_RETENTION_MS[topic],
    )
    for topic in ALL_TOPICS
)
"""Every topic AutoTwin owns, in the order BUILD_SPEC §8 tabulates them."""


@dataclass(frozen=True, slots=True)
class TopicStatus:
    """Outcome of trying to create one topic."""

    name: str
    """Topic the attempt was about."""

    created: bool
    """True when this call created the topic; False when it already existed."""

    detail: str
    """Human-readable outcome, printed by the CLI summary."""


@dataclass(frozen=True, slots=True)
class TopicDescription:
    """What the broker actually has, as opposed to what :data:`TOPIC_SPECS` asks for."""

    name: str
    """Topic name as reported by the cluster metadata."""

    partitions: int
    """Number of partitions the broker reports."""

    replication_factor: int
    """Replicas of the first partition; uniform across partitions on this cluster."""

    retention_ms: int | None
    """Effective ``retention.ms``, or ``None`` when the broker did not report it."""

    matches_spec: bool
    """Whether partitions and retention agree with the corresponding :class:`TopicSpec`.

    A mismatch is never repaired automatically: shrinking retention discards data and changing
    partition counts re-keys every future message, so both are decisions for an operator.
    """


def _admin_client(config: StreamingConfig) -> AIOKafkaAdminClient:
    """Build (but do not start) the admin client for this configuration."""
    return AIOKafkaAdminClient(
        bootstrap_servers=config.bootstrap_servers,
        client_id=config.component_client_id("admin"),
        request_timeout_ms=config.request_timeout_ms,
    )


def _topic_error_code(entry: object) -> tuple[str, int, str | None]:
    """Unpack one entry of ``CreateTopicsResponse.topic_errors``.

    The tuple grew a message field in protocol v1, and aiokafka picks the response version from
    what the broker advertises. Unpacking positionally by length keeps this working against
    both an ancient Kafka and a current Redpanda without pinning an API version.
    """
    fields = tuple(entry) if isinstance(entry, tuple | list) else ()
    if len(fields) < 2:
        msg = f"unexpected create-topics response entry: {entry!r}"
        raise ProviderUnavailable(msg)
    name = str(fields[0])
    code = int(fields[1])
    message = str(fields[2]) if len(fields) > 2 and fields[2] is not None else None
    return name, code, message


async def create_topics(
    config: StreamingConfig | None = None,
    *,
    specs: tuple[TopicSpec, ...] = TOPIC_SPECS,
) -> list[TopicStatus]:
    """Create every topic in ``specs`` that does not exist yet.

    Topics are requested one at a time rather than in a single batch. A batched
    ``CreateTopics`` gives back per-topic error codes too, but a broker that rejects the batch
    as a whole — the controller moved, say — leaves no way to tell which topics were created,
    and re-running then reports every topic as pre-existing whether or not it is.

    Raises :class:`~autotwin_core.errors.ProviderUnavailable` if the broker cannot be reached or
    refuses a topic for any reason other than "already exists".
    """
    resolved = config if config is not None else StreamingConfig.from_settings()
    client = _admin_client(resolved)
    statuses: list[TopicStatus] = []
    try:
        await client.start()
    except (KafkaError, OSError) as exc:
        msg = f"Kafka broker at {resolved.bootstrap_servers} is unreachable"
        raise ProviderUnavailable(msg, details={"error": type(exc).__name__}) from exc
    try:
        for spec in specs:
            statuses.append(await _create_one(client, spec, resolved))
    finally:
        await client.close()
    return statuses


async def _create_one(
    client: AIOKafkaAdminClient,
    spec: TopicSpec,
    config: StreamingConfig,
) -> TopicStatus:
    """Create a single topic, treating ``TOPIC_ALREADY_EXISTS`` as success."""
    try:
        response = await client.create_topics([spec.as_new_topic()])
    except TopicAlreadyExistsError:
        # Some broker/protocol combinations raise instead of returning error code 36.
        _LOGGER.info("kafka.topic.exists", topic=spec.name)
        return TopicStatus(name=spec.name, created=False, detail="already exists")
    except (KafkaError, OSError) as exc:
        msg = f"could not create topic {spec.name} on {config.bootstrap_servers}"
        raise ProviderUnavailable(msg, details={"error": type(exc).__name__}) from exc

    for entry in getattr(response, "topic_errors", ()):
        name, code, message = _topic_error_code(entry)
        if code == _NO_ERROR:
            _LOGGER.info(
                "kafka.topic.created",
                topic=name,
                partitions=spec.partitions,
                retention_ms=spec.retention_ms,
            )
            return TopicStatus(
                name=name,
                created=True,
                detail=(
                    f"created (partitions={spec.partitions}, "
                    f"replicas={spec.replication_factor}, retention.ms={spec.retention_ms})"
                ),
            )
        if code == TopicAlreadyExistsError.errno:
            _LOGGER.info("kafka.topic.exists", topic=name)
            return TopicStatus(name=name, created=False, detail="already exists")
        error_class = for_code(code)
        detail = message or error_class.__name__
        failure = f"broker refused topic {name}: {detail}"
        raise ProviderUnavailable(failure, details={"topic": name, "error_code": code})

    msg = f"broker returned no result for topic {spec.name}"
    raise ProviderUnavailable(msg, details={"topic": spec.name})


async def describe_topics(
    config: StreamingConfig | None = None,
    *,
    specs: tuple[TopicSpec, ...] = TOPIC_SPECS,
) -> list[TopicDescription]:
    """Report what the broker actually holds for each topic in ``specs``.

    Used by ``create-topics`` to print a verification line after creating, and available to the
    readiness endpoint when a deployment wants more than "the broker answered".
    """
    resolved = config if config is not None else StreamingConfig.from_settings()
    by_name = {spec.name: spec for spec in specs}
    client = _admin_client(resolved)
    try:
        await client.start()
    except (KafkaError, OSError) as exc:
        msg = f"Kafka broker at {resolved.bootstrap_servers} is unreachable"
        raise ProviderUnavailable(msg, details={"error": type(exc).__name__}) from exc
    try:
        existing = await client.list_topics()
        wanted = [name for name in by_name if name in set(existing)]
        if not wanted:
            return []
        metadata = await client.describe_topics(wanted)
        retentions = await _retention_by_topic(client, wanted)
    finally:
        await client.close()

    descriptions: list[TopicDescription] = []
    for topic in metadata:
        name = str(topic["topic"])
        partitions = list(topic.get("partitions", ()))
        replication = len(partitions[0]["replicas"]) if partitions else 0
        retention = retentions.get(name)
        spec = by_name[name]
        descriptions.append(
            TopicDescription(
                name=name,
                partitions=len(partitions),
                replication_factor=replication,
                retention_ms=retention,
                matches_spec=(
                    len(partitions) == spec.partitions
                    and (retention is None or retention == spec.retention_ms)
                ),
            )
        )
    return sorted(descriptions, key=lambda description: description.name)


async def _retention_by_topic(
    client: AIOKafkaAdminClient,
    topics: list[str],
) -> dict[str, int]:
    """Read the effective ``retention.ms`` of each topic from the broker's config API.

    Best effort on purpose: ``DescribeConfigs`` is the one call here that a locked-down cluster
    may deny, and losing the retention column is not a reason to fail a describe that otherwise
    succeeded.
    """
    resources = [ConfigResource(ConfigResourceType.TOPIC, topic) for topic in topics]
    try:
        responses = await client.describe_configs(resources)
    except (KafkaError, OSError) as exc:
        _LOGGER.warning("kafka.topic.describe_configs_failed", error=type(exc).__name__)
        return {}

    retentions: dict[str, int] = {}
    for response in responses:
        for resource in getattr(response, "resources", ()):
            entry = _parse_config_resource(resource)
            if entry is not None:
                retentions[entry[0]] = entry[1]
    return retentions


def _parse_config_resource(resource: Any) -> tuple[str, int] | None:
    """Pull ``(topic, retention_ms)`` out of one ``DescribeConfigs`` resource tuple.

    The tuple is ``(error_code, error_message, resource_type, resource_name, config_entries)``
    and each config entry starts ``(name, value, ...)``. Indexing it positionally is unpleasant
    but unavoidable: aiokafka exposes the decoded protocol struct, not a typed object.
    """
    fields = tuple(resource) if isinstance(resource, tuple | list) else ()
    min_resource_fields = 5
    if len(fields) < min_resource_fields or int(fields[0]) != _NO_ERROR:
        return None
    name = str(fields[3])
    for config_entry in fields[4]:
        entry_fields = tuple(config_entry)
        min_entry_fields = 2
        if len(entry_fields) < min_entry_fields:
            continue
        if str(entry_fields[0]) == _RETENTION_CONFIG_KEY and entry_fields[1] is not None:
            try:
                return name, int(entry_fields[1])
            except (TypeError, ValueError):
                return None
    return None


async def check_broker(
    timeout_s: float | None = None,
    *,
    config: StreamingConfig | None = None,
) -> bool:
    """Readiness probe for ``GET /ready``: can we talk to the Kafka API right now?

    Deliberately mirrors :func:`autotwin_core.db.session.check_database`, including its refusal
    to raise. A readiness endpoint that propagates a connection error tells an orchestrator far
    less than one that returns ``kafka: false``, and here it would be actively wrong: Kafka is
    optional (BUILD_SPEC §8), so an unreachable broker degrades the system rather than breaking
    it.

    Bootstrapping a metadata request — rather than opening a bare TCP socket — is what makes
    this meaningful: a port that accepts connections several seconds before the Kafka API is
    ready is the classic source of a flaky first produce.
    """
    resolved = config if config is not None else StreamingConfig.from_settings()
    budget = timeout_s if timeout_s is not None else resolved.broker_probe_timeout_s
    client = _admin_client(resolved)
    try:
        async with asyncio.timeout(budget):
            await client.start()
            await client.describe_cluster()
    except (TimeoutError, KafkaError, OSError) as exc:
        _LOGGER.warning(
            "kafka.check.failed",
            bootstrap_servers=resolved.bootstrap_servers,
            error=type(exc).__name__,
            detail=str(exc),
        )
        return False
    else:
        return True
    finally:
        # close() is safe on a client that never finished starting, and skipping it on the
        # failure path would leak the connection attempt's socket for the life of the process.
        await client.close()
