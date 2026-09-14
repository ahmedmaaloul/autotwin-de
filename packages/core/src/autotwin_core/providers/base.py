"""Abstract provider interfaces (BUILD_SPEC §4).

These are the seams between AutoTwin and the outside world. Concrete adapters live in
``autotwin_ingestion.providers.*``; everything in ``core``, ``api`` and ``ml`` depends only on
the abstractions here, which is what lets a test swap the Bundesnetzagentur download for a
committed fixture without a line of production code changing.

**The contract every implementation signs**

1. *Answer or raise a typed error.* Return a :class:`~autotwin_core.providers.result.ProviderResult`
   or raise a :class:`~autotwin_core.errors.ProviderError` subclass — never ``None``, never an
   empty list standing in for a failure, never a bare ``Exception``. The API maps those
   subclasses onto HTTP status codes (§7), so an untyped exception becomes a 500 with no
   useful message.
2. *State the mode truthfully.* ``live`` only when the upstream source answered during this
   call. Data read from the on-disk cache is ``cache``; data read from a bundled fixture is
   ``fixture``. This is the honesty rule of §0.2 and it is not negotiable — the UI tells the
   user which one it is.
3. *Follow the fallback chain.* ``live → cache → fixture``, each step downgrading the mode.
   A provider that cannot reach any of the three raises rather than inventing data.
4. *Be side-effect free.* Providers parse; they never write to the database. Persistence is
   the pipeline's job, which is what keeps provider tests database-free.

Interfaces are plain ABCs rather than ``Protocol``s on purpose: the ``name``/``source``
class variables and the shared documentation are inherited, and ``isinstance`` checks in the
provider registry stay meaningful.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

from autotwin_contracts import (
    BoundingBox,
    ChargingStationRecord,
    Coordinate,
    Place,
    RouteResult,
    SourceSystem,
    TrafficEventRecord,
    WeatherRecord,
)
from autotwin_core.providers.result import ProviderResult

__all__ = [
    "BaseProvider",
    "ChargingInfrastructureProvider",
    "ClosableProvider",
    "GeocodingProvider",
    "GeocodingResult",
    "LLMMessage",
    "LLMProvider",
    "LLMResponse",
    "LLMRole",
    "LLMToolCall",
    "RoutingProvider",
    "StationsResult",
    "TrafficProvider",
    "TrafficResult",
    "WeatherProvider",
    "WeatherResult",
    "close_provider",
]

StationsResult = ProviderResult[list[ChargingStationRecord]]
"""What a :class:`ChargingInfrastructureProvider` returns."""

WeatherResult = ProviderResult[list[WeatherRecord]]
"""What a :class:`WeatherProvider` returns."""

TrafficResult = ProviderResult[list[TrafficEventRecord]]
"""What a :class:`TrafficProvider` returns."""

GeocodingResult = ProviderResult[list[Place]]
"""What a :class:`GeocodingProvider` returns."""


class BaseProvider:
    """Identity shared by every provider.

    Not an ABC itself — it declares no behaviour, only the two class variables that the
    ingestion runner writes into ``data_ingestion_runs.pipeline`` and the provenance block of
    every row the provider produces. Concrete adapters **must** set both; an adapter that
    forgets produces rows whose origin cannot be traced, which §3.1 exists to prevent.
    """

    name: ClassVar[str]
    """Stable adapter identifier, e.g. ``bnetza_charging_stations``. Used in logs and runs."""

    source: ClassVar[SourceSystem]
    """Which upstream system this adapter speaks to; written to ``provenance.source``."""

    @classmethod
    def describe(cls) -> str:
        """Human-readable ``name (source)`` label for log lines and error messages."""
        name = getattr(cls, "name", cls.__name__)
        source = getattr(cls, "source", None)
        return f"{name} ({source})" if source is not None else str(name)


@runtime_checkable
class ClosableProvider(Protocol):
    """A provider holding resources — an HTTP client, a file handle — that must be released.

    Kept separate from :class:`BaseProvider` so that a file- or fixture-backed adapter is not
    forced to implement an empty ``aclose``. The API's ``lifespan`` closes whatever satisfies
    this protocol via :func:`close_provider`.
    """

    async def aclose(self) -> None:
        """Release the provider's resources; must be idempotent."""


async def close_provider(provider: object) -> None:
    """Close ``provider`` if it holds resources, otherwise do nothing.

    Lets application shutdown iterate over a heterogeneous provider registry without knowing
    which adapters are HTTP-backed.
    """
    if isinstance(provider, ClosableProvider):
        await provider.aclose()


class ChargingInfrastructureProvider(BaseProvider, ABC):
    """Source of charging sites — the Ladesäulenregister of the Bundesnetzagentur.

    The register is a single large file rather than a query API, so implementations download
    it whole, parse it, and let the pipeline diff the result against ``charging_stations``.
    Each returned record must already carry its provenance block and a deterministic
    ``external_id``, so that re-ingesting an unchanged file is an idempotent upsert.
    """

    @abstractmethod
    async def fetch_stations(self) -> StationsResult:
        """Fetch every charging site the source knows about.

        Returns:
            Every station in the source, each with provenance and a stable ``external_id``.
            ``mode`` is ``live`` only when the file was downloaded during this call.

        Raises:
            ProviderUnavailable: the download failed at the network or server level.
            ProviderTimeout: the source did not answer within the configured timeout.
            InvalidSourceData: the file was retrieved but its columns no longer match.
            ConfigurationMissing: no download URL or fixture path is configured.
        """


class WeatherProvider(BaseProvider, ABC):
    """Source of weather observations — Deutscher Wetterdienst open data.

    Temperature is the strongest weather driver of EV consumption (§10.1), so implementations
    should prefer returning a record with a temperature and null everything else over
    returning nothing at all.
    """

    @abstractmethod
    async def fetch_observations(
        self,
        points: Sequence[Coordinate],
        at: datetime | None = None,
    ) -> WeatherResult:
        """Fetch observations covering the requested points.

        Args:
            points: Locations to cover — typically the midpoints of a route's segments. An
                implementation is free to answer with the nearest station per point rather
                than an interpolated value, but must then locate the record at the *station*.
            at: Observation time (aware UTC); ``None`` means the most recent observation.

        Returns:
            One record per covered point or station, each with provenance. Points with no
            station in range are omitted rather than filled with invented values.

        Raises:
            ProviderUnavailable: DWD open data was unreachable.
            ProviderTimeout: the request exceeded the configured timeout.
            InvalidSourceData: the station list or observation file changed shape.
        """


class TrafficProvider(BaseProvider, ABC):
    """Source of traffic disruptions — the Autobahn GmbH API or the Mobilithek.

    Implementations normalise the German report vocabulary onto
    :class:`~autotwin_contracts.TrafficEventType` and
    :class:`~autotwin_contracts.TrafficSeverity`; the raw report is kept in ``raw`` so a
    misclassification can be diagnosed after the fact.
    """

    @abstractmethod
    async def fetch_events(self, bbox: BoundingBox | None = None) -> TrafficResult:
        """Fetch currently published traffic events.

        Args:
            bbox: Optional spatial filter in ``west, south, east, north`` order. ``None``
                means the provider's full coverage — for the Autobahn API, the A-network.

        Returns:
            Events with a representative point each, and a line geometry where published.

        Raises:
            ProviderUnavailable: the API was unreachable or returned 5xx.
            ProviderTimeout: the request exceeded the configured timeout.
            RateLimited: the API refused the request for rate reasons.
            InvalidSourceData: the response could not be parsed into events.
        """


class RoutingProvider(BaseProvider, ABC):
    """Source of routes — OSRM, either the public demo server or a local container.

    Unlike the other providers this one is also called at request time by the API
    (``POST /api/v1/routes/plan`` and ``/analyze``), so implementations must be cheap to
    construct and must respect the configured timeout strictly: a slow route request blocks a
    user-facing page, not a background pipeline.
    """

    @abstractmethod
    async def route(
        self,
        origin: Coordinate,
        destination: Coordinate,
        *,
        profile: str = "driving",
    ) -> ProviderResult[RouteResult]:
        """Compute a route between two points.

        Args:
            origin: Start point in WGS 84.
            destination: End point in WGS 84.
            profile: Routing profile; ``driving`` is the only one AutoTwin models.

        Returns:
            A route whose geometry runs origin to destination, with per-step summaries where
            the engine provides them.

        Raises:
            ProviderUnavailable: the routing engine was unreachable or found no route.
            ProviderTimeout: the engine did not answer within the timeout.
            InvalidSourceData: the response was not a route AutoTwin can read.
            RateLimited: the public demo server refused the request.
        """


class GeocodingProvider(BaseProvider, ABC):
    """Source of place lookups — Nominatim.

    Nominatim's usage policy requires an identifying User-Agent and at most one request per
    second. Implementations must honour both; the shared HTTP client sets the User-Agent from
    ``AUTOTWIN_HTTP_USER_AGENT`` and the adapter is responsible for the rate limit.
    """

    @abstractmethod
    async def geocode(self, query: str) -> GeocodingResult:
        """Resolve a free-text place query to candidate places.

        Args:
            query: What the user typed, e.g. ``"Stuttgart Hauptbahnhof"``.

        Returns:
            Candidates ordered best-first; an empty list is a legitimate answer meaning "no
            such place", and must not be confused with a provider failure.

        Raises:
            ProviderUnavailable: the geocoder was unreachable.
            ProviderTimeout: the request exceeded the configured timeout.
            RateLimited: the usage policy limit was hit.
            InvalidSourceData: the response could not be parsed.
        """


LLMRole = Literal["system", "user", "assistant", "tool"]
"""Chat roles AutoTwin uses; ``tool`` carries a tool result back into the conversation."""


@dataclass(frozen=True, slots=True)
class LLMMessage:
    """One turn of a chat conversation."""

    role: LLMRole
    """Who is speaking."""

    content: str
    """The message text; for a ``tool`` message, the serialised tool result."""

    name: str | None = None
    """Tool name for a ``tool`` message, or an optional speaker label otherwise."""

    tool_call_id: str | None = None
    """Identifier of the tool call this message answers, for ``tool`` messages."""


@dataclass(frozen=True, slots=True)
class LLMToolCall:
    """A tool invocation requested by the model."""

    id: str
    """Identifier the answering ``tool`` message must quote back."""

    name: str
    """Name of the requested tool."""

    arguments: dict[str, Any]
    """Parsed arguments. Implementations parse the model's JSON and raise on malformed input
    rather than passing a raw string on, so a caller never has to guess."""


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """A completion, with the accounting needed to show cost and latency in the UI."""

    content: str
    """Assistant text; empty when the model answered only with tool calls."""

    model: str
    """Model identifier that actually answered, e.g. ``llama3.2``."""

    tool_calls: tuple[LLMToolCall, ...] = ()
    """Tool calls requested by the model, in the order it emitted them."""

    finish_reason: str | None = None
    """Why generation stopped, as reported by the backend."""

    prompt_tokens: int | None = None
    """Prompt token count, when the backend reports one."""

    completion_tokens: int | None = None
    """Completion token count, when the backend reports one."""

    latency_ms: float | None = None
    """Wall-clock time of the call, measured by the adapter."""


class LLMProvider(BaseProvider, ABC):
    """Source of language-model completions — a local Ollama instance.

    Strictly optional. ``AUTOTWIN_LLM_ENABLED`` defaults to ``false`` and
    ``POST /api/v1/copilot/ask`` answers ``503`` while it is off, because the project must
    run end-to-end with no model installed (§0.1). Nothing in the analytical path may depend
    on an LLM: route analysis explains itself deterministically through
    ``autotwin_ml.insights`` (§11), and the copilot only rephrases what that already computed.

    No fallback chain applies here — there is no cache or fixture for a completion — so an
    implementation raises rather than degrading, and the caller decides what to show.
    """

    @abstractmethod
    async def complete(
        self,
        messages: Sequence[LLMMessage],
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> LLMResponse:
        """Generate a completion for a conversation.

        Args:
            messages: The conversation so far, oldest first.
            tools: Optional JSON-schema tool definitions the model may call.

        Raises:
            ConfigurationMissing: the LLM is disabled or no model is installed.
            ProviderUnavailable: the model server was unreachable.
            ProviderTimeout: generation exceeded the configured timeout.
            InvalidSourceData: the server's response, or a tool call's arguments, was
                not valid JSON.
        """
