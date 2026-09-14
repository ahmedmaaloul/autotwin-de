"""Provider registry — the one place that decides which adapter answers a question.

Everything outside this module asks for *a* charging provider, not for the Bundesnetzagentur
adapter specifically. That indirection is what makes BUILD_SPEC §4's promise real: swapping
the Autobahn API for a Mobilithek adapter, or the public OSRM demo server for a local
container, is a change here and nowhere else.

**Process-wide instances.** Each getter memoises its adapter, because an adapter owns a
pooled :class:`~autotwin_core.providers.http.AutoTwinHTTPClient` and the API serves many
requests from one process — building a fresh client per request would open a fresh TCP and TLS
connection per request. :func:`close_providers` releases them, and the API's ``lifespan``
calls it on shutdown.

**Data mode.** Nothing is decided here: every adapter reads ``AUTOTWIN_DATA_MODE`` itself, so
``live``/``cached``/``fixture`` behaves identically whether an adapter was built by this
registry or directly by a test. The registry only reflects the configuration in its log line.

**Testability.** Two seams, deliberately different in weight:

* :func:`set_provider_override` installs a double for one kind and
  :func:`reset_providers` removes it again — enough for most tests, and it keeps
  ``monkeypatch`` out of the picture entirely;
* the getters are plain module-level functions, so a test that prefers
  ``monkeypatch.setattr(registry, "get_traffic_provider", ...)`` also works.
"""

from __future__ import annotations

from typing import Final, Literal

from autotwin_core.config import get_settings
from autotwin_core.logging import get_logger
from autotwin_core.providers import (
    ChargingInfrastructureProvider,
    GeocodingProvider,
    RoutingProvider,
    TrafficProvider,
    WeatherProvider,
    close_provider,
)
from autotwin_ingestion.providers.charging import BundesnetzagenturChargingProvider
from autotwin_ingestion.providers.geocoding import NominatimGeocodingProvider
from autotwin_ingestion.providers.routing import OSRMRoutingProvider
from autotwin_ingestion.providers.traffic import AutobahnTrafficProvider
from autotwin_ingestion.providers.weather import DWDWeatherProvider

__all__ = [
    "PROVIDER_KINDS",
    "ProviderKind",
    "close_providers",
    "get_charging_provider",
    "get_geocoding_provider",
    "get_routing_provider",
    "get_traffic_provider",
    "get_weather_provider",
    "reset_providers",
    "set_provider_override",
]

_logger = get_logger(__name__)

ProviderKind = Literal["charging", "weather", "traffic", "routing", "geocoding"]
"""The five external capabilities AutoTwin depends on, as a checkable literal type."""

PROVIDER_KINDS: Final[tuple[ProviderKind, ...]] = (
    "charging",
    "weather",
    "traffic",
    "routing",
    "geocoding",
)
"""Runtime form of :data:`ProviderKind`, for validation and for iterating in tooling."""

_charging: ChargingInfrastructureProvider | None = None
_weather: WeatherProvider | None = None
_traffic: TrafficProvider | None = None
_routing: RoutingProvider | None = None
_geocoding: GeocodingProvider | None = None


def get_charging_provider() -> ChargingInfrastructureProvider:
    """Return the charging-infrastructure adapter — the Bundesnetzagentur Ladesäulenregister."""
    global _charging
    if _charging is None:
        _charging = BundesnetzagenturChargingProvider()
        _log_construction(_charging.name)
    return _charging


def get_weather_provider() -> WeatherProvider:
    """Return the weather adapter — DWD 10-minute open-data observations."""
    global _weather
    if _weather is None:
        _weather = DWDWeatherProvider()
        _log_construction(_weather.name)
    return _weather


def get_traffic_provider() -> TrafficProvider:
    """Return the traffic adapter — the Autobahn GmbH public API."""
    global _traffic
    if _traffic is None:
        _traffic = AutobahnTrafficProvider()
        _log_construction(_traffic.name)
    return _traffic


def get_routing_provider() -> RoutingProvider:
    """Return the routing adapter — OSRM, public demo server or local container."""
    global _routing
    if _routing is None:
        _routing = OSRMRoutingProvider()
        _log_construction(_routing.name)
    return _routing


def get_geocoding_provider() -> GeocodingProvider:
    """Return the geocoding adapter — Nominatim."""
    global _geocoding
    if _geocoding is None:
        _geocoding = NominatimGeocodingProvider()
        _log_construction(_geocoding.name)
    return _geocoding


def set_provider_override(
    kind: ProviderKind,
    provider: ChargingInfrastructureProvider
    | WeatherProvider
    | TrafficProvider
    | RoutingProvider
    | GeocodingProvider
    | None,
) -> None:
    """Install (or with ``None`` remove) the instance a getter returns.

    The intended use is a test double::

        set_provider_override("traffic", StubTrafficProvider())
        ...
        reset_providers()

    An override replaces the memoised instance without closing it, because the caller owns
    whatever it passed in and may still want to assert on it afterwards.

    The type of the double is checked here rather than left to fail later: a stub wired to the
    wrong slot otherwise surfaces as an ``AttributeError`` deep inside a pipeline, several
    frames away from the line that installed it.

    Raises:
        ValueError: ``kind`` is not one of :data:`PROVIDER_KINDS`.
        TypeError: ``provider`` does not implement the interface for ``kind``.
    """
    global _charging, _weather, _traffic, _routing, _geocoding
    if kind not in PROVIDER_KINDS:
        msg = f"unknown provider kind {kind!r}; known: {list(PROVIDER_KINDS)}"
        raise ValueError(msg)

    if provider is None:
        _clear(kind)
        return
    if kind == "charging" and isinstance(provider, ChargingInfrastructureProvider):
        _charging = provider
        return
    if kind == "weather" and isinstance(provider, WeatherProvider):
        _weather = provider
        return
    if kind == "traffic" and isinstance(provider, TrafficProvider):
        _traffic = provider
        return
    if kind == "routing" and isinstance(provider, RoutingProvider):
        _routing = provider
        return
    if kind == "geocoding" and isinstance(provider, GeocodingProvider):
        _geocoding = provider
        return

    msg = f"{type(provider).__name__} does not implement the {kind!r} provider interface"
    raise TypeError(msg)


def _clear(kind: ProviderKind) -> None:
    """Drop the memoised instance for one kind without closing it."""
    global _charging, _weather, _traffic, _routing, _geocoding
    match kind:
        case "charging":
            _charging = None
        case "weather":
            _weather = None
        case "traffic":
            _traffic = None
        case "routing":
            _routing = None
        case "geocoding":
            _geocoding = None


def reset_providers() -> None:
    """Forget every memoised instance without closing it.

    Used between tests, and after ``reset_settings_cache()`` in tooling that rewrites the
    environment: an adapter captured its settings at construction time, so a configuration
    change has no effect until the instances are dropped.
    """
    global _charging, _weather, _traffic, _routing, _geocoding
    _charging = None
    _weather = None
    _traffic = None
    _routing = None
    _geocoding = None


async def close_providers() -> None:
    """Close every memoised adapter that holds resources, then forget them all.

    Idempotent, and safe to call when nothing was ever constructed — which is what lets the
    API's ``lifespan`` call it unconditionally on shutdown.
    """
    for provider in (_charging, _weather, _traffic, _routing, _geocoding):
        if provider is not None:
            await close_provider(provider)
    reset_providers()


def _log_construction(name: str) -> None:
    """Record which adapter was built under which data mode — the first line of any triage."""
    _logger.debug("provider constructed", provider=name, data_mode=get_settings().data_mode.value)
