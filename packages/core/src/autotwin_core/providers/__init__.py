"""Provider abstractions, result envelope, on-disk cache and the shared HTTP client (§4).

This package holds only the *seams*. Concrete adapters live in
``autotwin_ingestion.providers.*`` and import from here, which is what keeps the dependency
direction ``contracts ← core ← ingestion ← api`` acyclic.

A typical adapter walks the mandatory fallback chain with the three pieces below::

    from autotwin_core.providers import AutoTwinHTTPClient, FileCache, ProviderResult

    cached = self._cache.get_json("autobahn", url)
    if cached is not None:
        age = self._cache.age_seconds("autobahn", url, suffix=".json") or 0.0
        return ProviderResult.cached(parse(cached), source_url=url,
                                     fetched_at=utc_now() - timedelta(seconds=age))
"""

from __future__ import annotations

from autotwin_core.providers.base import (
    BaseProvider,
    ChargingInfrastructureProvider,
    ClosableProvider,
    GeocodingProvider,
    GeocodingResult,
    LLMMessage,
    LLMProvider,
    LLMResponse,
    LLMRole,
    LLMToolCall,
    RoutingProvider,
    StationsResult,
    TrafficProvider,
    TrafficResult,
    WeatherProvider,
    WeatherResult,
    close_provider,
)
from autotwin_core.providers.cache import FileCache
from autotwin_core.providers.http import AutoTwinHTTPClient
from autotwin_core.providers.result import ProviderResult

__all__ = [
    "AutoTwinHTTPClient",
    "BaseProvider",
    "ChargingInfrastructureProvider",
    "ClosableProvider",
    "FileCache",
    "GeocodingProvider",
    "GeocodingResult",
    "LLMMessage",
    "LLMProvider",
    "LLMResponse",
    "LLMRole",
    "LLMToolCall",
    "ProviderResult",
    "RoutingProvider",
    "StationsResult",
    "TrafficProvider",
    "TrafficResult",
    "WeatherProvider",
    "WeatherResult",
    "close_provider",
]
