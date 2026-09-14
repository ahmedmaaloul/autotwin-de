"""Nominatim geocoding adapter — free-text place lookup for the route planner.

Request shape::

    GET {nominatim_base_url}/search
        ?q=...&format=jsonv2&countrycodes=de&limit=N&addressdetails=1

``countrycodes=de`` keeps the result set inside AutoTwin's area of interest, so that typing
"Frankfurt" cannot return Frankfurt (Kentucky); ``addressdetails=1`` is what supplies
``address.state``, the field the :class:`~autotwin_contracts.Bundesland` mapping needs.

**The usage policy is a hard requirement, not advice.**
`OSM's Nominatim policy <https://operations.osmfoundation.org/policies/nominatim/>`_ demands an
identifying ``User-Agent`` and at most **one request per second** from a single source, and
enforces both by blocking. This adapter therefore:

* sends the ``AUTOTWIN_HTTP_USER_AGENT`` identity through the shared HTTP client;
* serialises its own requests behind :class:`_RateLimiter`, which waits until at least
  :data:`MIN_REQUEST_INTERVAL_S` has elapsed since the previous one. The limiter is
  deterministic — a plain monotonic-clock difference, no jitter, no token bucket — so a test
  can assert on it;
* caches **every** lookup on disk with no expiry. Place names do not move, and a repeated
  search must never become a repeated request.

The bundled fixture covers the six demo cities, which is what lets ``make demo`` geocode with
no network at all.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from autotwin_contracts import (
    BoundingBox,
    Bundesland,
    Coordinate,
    Place,
    SourceSystem,
    utc_now,
)
from autotwin_core.config import DataMode, Settings, get_settings
from autotwin_core.errors import ConfigurationMissing, InvalidSourceData, ProviderError
from autotwin_core.logging import get_logger
from autotwin_core.providers import (
    AutoTwinHTTPClient,
    FileCache,
    GeocodingProvider,
    GeocodingResult,
    ProviderResult,
)
from autotwin_ingestion.fixtures import NOMINATIM_FIXTURE, resolve_fixture

__all__ = [
    "DEFAULT_RESULT_LIMIT",
    "MIN_REQUEST_INTERVAL_S",
    "NominatimGeocodingProvider",
    "normalise_query",
    "parse_places",
]

_logger = get_logger(__name__)

MIN_REQUEST_INTERVAL_S: Final[float] = 1.0
"""Seconds between two outbound requests — the ceiling Nominatim's usage policy sets."""

DEFAULT_RESULT_LIMIT: Final[int] = 5
"""Candidates requested per query; enough for a type-ahead, small enough to stay polite."""

_COUNTRY_CODES: Final[str] = "de"
"""AutoTwin models German mobility, so results are constrained to Germany at the source."""

_CACHE_NAMESPACE: Final[str] = "nominatim"
"""Cache namespace for geocoding responses."""

_GEOCODE_TTL_S: Final[int] = 0
"""Zero means "never expires" in :class:`~autotwin_core.providers.cache.FileCache`.

Deliberate: a city does not move, and the usage policy explicitly asks callers to cache. An
operator who needs a refreshed answer deletes the entry — an automatic expiry would only
generate traffic that the policy asks us not to generate.
"""

_FALLBACK_PLACE_TYPE: Final[str] = "unknown"
"""Used when Nominatim reports neither an ``addresstype`` nor a ``type``."""


class _RateLimiter:
    """Serialises calls so that consecutive requests are at least ``interval_s`` apart.

    A lock plus a monotonic timestamp rather than a token bucket: the policy is "one request
    per second", not "a burst of N then a refill", and a bucket would let a page of six
    type-ahead lookups leave as a burst — exactly what gets a client blocked.

    ``time.monotonic`` is used rather than wall-clock time because it cannot go backwards over
    an NTP correction, which would otherwise release a burst.
    """

    def __init__(self, interval_s: float = MIN_REQUEST_INTERVAL_S) -> None:
        """Configure the minimum spacing between two requests."""
        self._interval_s = max(interval_s, 0.0)
        self._lock = asyncio.Lock()
        self._last_at: float | None = None

    async def acquire(self) -> None:
        """Block until it is polite to issue the next request."""
        async with self._lock:
            now = time.monotonic()
            if self._last_at is not None:
                wait = self._interval_s - (now - self._last_at)
                if wait > 0.0:
                    await asyncio.sleep(wait)
                    now = time.monotonic()
            self._last_at = now

    @property
    def interval_s(self) -> float:
        """Configured minimum spacing, in seconds."""
        return self._interval_s


class NominatimGeocodingProvider(GeocodingProvider):
    """Resolves free-text place queries through Nominatim (BUILD_SPEC §4).

    Fallback chain ``live → cache → fixture``. Unlike the other adapters, an *empty* list is a
    legitimate live answer meaning "no such place in Germany" and is cached as such; only a
    transport or parsing failure degrades the mode.
    """

    name = "nominatim_geocoding"
    source = SourceSystem.osm

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        http_client: AutoTwinHTTPClient | None = None,
        cache: FileCache | None = None,
        data_mode: DataMode | None = None,
        limit: int = DEFAULT_RESULT_LIMIT,
        min_interval_s: float = MIN_REQUEST_INTERVAL_S,
    ) -> None:
        """Configure the adapter.

        Args:
            settings: Configuration source; defaults to the process-wide settings.
            http_client: Shared HTTP client. One is created — and owned — if omitted.
            cache: On-disk cache; defaults to ``AUTOTWIN_DATA_DIR/cache``.
            data_mode: Override the configured fallback policy.
            limit: Candidates to request per query.
            min_interval_s: Spacing between outbound requests. Lowering it below one second
                violates the usage policy; it exists so a test can run without sleeping.

        Raises:
            ValueError: ``limit`` is not positive.
        """
        if limit < 1:
            msg = f"limit must be at least 1, got {limit!r}"
            raise ValueError(msg)
        self._settings = settings or get_settings()
        self._data_mode = data_mode or self._settings.data_mode
        self._cache = cache or FileCache()
        self._limit = limit
        self._limiter = _RateLimiter(min_interval_s)
        self._http = http_client
        self._owns_http = http_client is None

    async def aclose(self) -> None:
        """Release the HTTP client if this adapter created it."""
        if self._http is not None and self._owns_http:
            await self._http.aclose()
            self._http = None

    async def geocode(self, query: str) -> GeocodingResult:
        """Resolve ``query`` to candidate places, best first.

        Args:
            query: Free text as the user typed it, e.g. ``"Stuttgart Hauptbahnhof"``.

        Returns:
            Candidates ordered as Nominatim ranked them. An empty list means "no such place",
            which is an answer, not a failure.

        Raises:
            ProviderUnavailable: the geocoder was unreachable and live mode was demanded.
            RateLimited: the usage-policy limit was hit upstream.
            InvalidSourceData: the response could not be parsed.
            ConfigurationMissing: no source at all — not live, not cached, not bundled.
        """
        normalised = normalise_query(query)
        if not normalised:
            return ProviderResult.fixture(
                [],
                source_url=None,
                warnings=["empty geocoding query"],
            )

        url = f"{self._settings.nominatim_base_url.rstrip('/')}/search"
        key = f"{normalised}|{self._limit}"

        if self._data_mode is not DataMode.fixture:
            try:
                await self._limiter.acquire()
                payload = await self._client().get_json(url, params=self._params(query))
            except ProviderError as error:
                if self._data_mode is DataMode.live:
                    raise
                _logger.warning(f"Nominatim unavailable: {error}", provider=self.name)
                reason = str(error)
            else:
                self._cache.set_json(_CACHE_NAMESPACE, key, payload)
                return ProviderResult.live(
                    parse_places(payload),
                    source_url=url,
                )

            cached = self._cache.get_json(
                _CACHE_NAMESPACE, key, ttl_seconds=_GEOCODE_TTL_S, allow_stale=True
            )
            if cached is not None:
                age = self._cache.age_seconds(_CACHE_NAMESPACE, key, suffix=".json") or 0.0
                return ProviderResult.cached(
                    parse_places(cached),
                    source_url=url,
                    fetched_at=utc_now() - timedelta(seconds=age),
                ).with_warning(f"Live geocoder unavailable: {reason}")

        return self._read_fixture(normalised, url)

    # ------------------------------------------------------------------ internals

    def _read_fixture(self, normalised: str, url: str) -> GeocodingResult:
        """Answer from the bundled demo-city snapshot."""
        try:
            path = resolve_fixture(NOMINATIM_FIXTURE)
        except FileNotFoundError as error:
            raise ConfigurationMissing(str(error)) from error
        fetched_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(snapshot, Mapping):
            msg = f"fixture {path.name} is not a query → results mapping"
            raise InvalidSourceData(msg)
        entry = snapshot.get(normalised)
        if entry is None:
            return ProviderResult.fixture(
                [],
                source_url=url,
                fetched_at=fetched_at,
                warnings=[
                    f"{normalised!r} is not in the bundled geocoding fixture "
                    f"(covered: {', '.join(sorted(snapshot))})"
                ],
            )
        return ProviderResult.fixture(
            parse_places(entry),
            source_url=url,
            fetched_at=fetched_at,
            warnings=["Serving a bundled geocoding result for a demo city."],
        )

    def _params(self, query: str) -> dict[str, str]:
        """Query parameters for ``/search``; ``q`` is sent raw, as the user typed it."""
        return {
            "q": query,
            "format": "jsonv2",
            "countrycodes": _COUNTRY_CODES,
            "limit": str(self._limit),
            "addressdetails": "1",
        }

    def _client(self) -> AutoTwinHTTPClient:
        """Return the HTTP client, creating the owned one on first use."""
        if self._http is None:
            self._http = AutoTwinHTTPClient()
            self._owns_http = True
        return self._http


# --------------------------------------------------------------------------- parsing


def normalise_query(query: str) -> str:
    """Fold a query to its cache and fixture key: trimmed, whitespace-collapsed, case-folded.

    Umlauts are *not* transliterated. ``München`` and ``Muenchen`` are two different strings to
    Nominatim and can rank differently, so collapsing them here would make the cache lie about
    which question was asked.
    """
    return " ".join(query.split()).casefold()


def parse_places(payload: Any) -> list[Place]:
    """Convert a Nominatim ``jsonv2`` array into :class:`Place` records.

    Entries that carry no readable coordinate are skipped rather than dropped silently into a
    default location — a geocoder result without a point is not a place.

    Raises:
        InvalidSourceData: the payload is not a JSON array.
    """
    if not isinstance(payload, list):
        msg = f"Nominatim returned {type(payload).__name__}, expected an array"
        raise InvalidSourceData(msg)
    places: list[Place] = []
    for entry in payload:
        if not isinstance(entry, Mapping):
            continue
        place = _parse_place(entry)
        if place is not None:
            places.append(place)
    return places


def _parse_place(entry: Mapping[str, Any]) -> Place | None:
    """Convert one ``jsonv2`` result object into a :class:`Place`."""
    latitude = _float(entry.get("lat"))
    longitude = _float(entry.get("lon"))
    if latitude is None or longitude is None:
        return None

    display_name = _text(entry.get("display_name"))
    name = _text(entry.get("name")) or (display_name.split(",")[0] if display_name else None)
    if not name or not display_name:
        return None

    address = entry.get("address")
    state = _text(address.get("state")) if isinstance(address, Mapping) else None
    country_code = _text(address.get("country_code")) if isinstance(address, Mapping) else None

    place_type = _text(entry.get("addresstype")) or _text(entry.get("type")) or _FALLBACK_PLACE_TYPE
    return Place(
        name=name,
        display_name=display_name,
        coordinate=Coordinate(latitude=latitude, longitude=longitude),
        place_type=place_type,
        bundesland=Bundesland.from_name(state) if state else None,
        country_code=country_code,
        bbox=_bbox(entry.get("boundingbox")),
        source_identifier=_source_identifier(entry),
    )


def _bbox(raw: object) -> BoundingBox | None:
    """Convert Nominatim's ``boundingbox`` into a :class:`BoundingBox`.

    Nominatim orders it ``[south, north, west, east]`` as *strings* — a different order and a
    different type from the project's ``west, south, east, north`` convention, which is why
    this conversion is a named function rather than four inline indexes.
    """
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes) or len(raw) != 4:
        return None
    south = _float(raw[0])
    north = _float(raw[1])
    west = _float(raw[2])
    east = _float(raw[3])
    if south is None or north is None or west is None or east is None:
        return None
    try:
        return BoundingBox(west=west, south=south, east=east, north=north)
    except ValueError:
        # A degenerate extent (a single node's bbox collapses to a point) is not an error;
        # the caller simply gets no box to fit the map to.
        return None


def _source_identifier(entry: Mapping[str, Any]) -> str | None:
    """Build ``osm:<type>:<id>`` — stable across Nominatim reindexes, unlike ``place_id``."""
    osm_type = _text(entry.get("osm_type"))
    osm_id = entry.get("osm_id")
    if osm_type and isinstance(osm_id, int | str):
        return f"osm:{osm_type}:{osm_id}"
    place_id = entry.get("place_id")
    return f"nominatim:{place_id}" if isinstance(place_id, int | str) else None


def _text(value: object) -> str | None:
    """Stripped text, or ``None`` for an empty or non-string value."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _float(value: object) -> float | None:
    """Parse a float from a number or a numeric string — Nominatim sends both."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None
