"""OSRM routing adapter — route geometry, steps and per-node annotations.

Request shape::

    GET {osrm_base_url}/route/v1/{profile}/{lon},{lat};{lon},{lat}
        ?overview=full&geometries=geojson&steps=true&annotations=distance,duration,speed

``overview=full`` keeps every vertex, which the segmentation code in
``autotwin_core.geo.segmentation`` needs; ``geometries=geojson`` avoids carrying a polyline
decoder; ``annotations`` returns the engine's own per-node speed, which the energy model uses
as the assumed free-flow speed of a segment.

**This adapter is on the request path.** ``POST /api/v1/routes/plan`` and ``/analyze`` call it
while a user waits, which drives two design choices: the configured ``AUTOTWIN_OSRM_TIMEOUT_S``
is applied strictly, and every answer is cached on disk **without expiry**. A route between two
fixed coordinates does not change when the traffic does — OSRM has no live traffic — so an
expiring cache would only re-ask a community server the same question forever.

**Public demo server.** ``https://router.project-osrm.org`` is community infrastructure under
a `usage policy <https://github.com/Project-OSRM/osrm-backend/wiki/Api-usage-policy>`_: low
volume, non-commercial, identify yourself. The identifying ``User-Agent`` comes from
``AUTOTWIN_HTTP_USER_AGENT`` via the shared HTTP client, and caching keeps the volume low.

**Local alternative.** For anything beyond demo traffic, run OSRM yourself and point
``AUTOTWIN_OSRM_BASE_URL`` at it::

    make osrm-prepare          # extract + partition a Geofabrik .osm.pbf
    docker compose up osrm     # serves http://localhost:5001

Nothing else changes: same request shape, same parsing, and the ``routes`` the demo seeds are
identical apart from the OSM snapshot they were built from.

**Road classes.** OSRM returns no ``highway`` tag and no ``maxspeed``. What it does return is
the German road reference on each step (``"A 5"``, ``"B 27"``, ``"K 818"``, occasionally
``"B 10; B 27"``), and the letter of a German road number *is* its functional class:
A = Autobahn, B = Bundesstraße, L/S = Landesstraße, K = Kreisstraße. That mapping is used, and
``speed_limit_kmh`` is left ``None`` rather than filled with the engine's assumed speed —
labelling an assumption as a posted limit would poison the energy model's feature vector.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from autotwin_contracts import (
    Coordinate,
    DataOrigin,
    ProvenanceInfo,
    RoadClass,
    RouteResult,
    RouteStepRecord,
    SourceSystem,
    haversine_km,
    utc_now,
)
from autotwin_core.config import DataMode, Settings, get_settings
from autotwin_core.errors import (
    ConfigurationMissing,
    InvalidSourceData,
    ProviderError,
    ProviderUnavailable,
)
from autotwin_core.logging import get_logger
from autotwin_core.providers import (
    AutoTwinHTTPClient,
    FileCache,
    ProviderResult,
    RoutingProvider,
)
from autotwin_ingestion.fixtures import OSRM_ROUTE_FIXTURE, resolve_fixture

__all__ = [
    "DEFAULT_PROFILE",
    "FIXTURE_DESTINATION",
    "FIXTURE_MATCH_KM",
    "FIXTURE_ORIGIN",
    "OSRMRoutingProvider",
    "parse_route",
    "road_class_from_ref",
]

_logger = get_logger(__name__)

DEFAULT_PROFILE: Final[str] = "driving"
"""The only profile AutoTwin models; the demo server also serves it under ``/car``."""

_QUERY: Final[dict[str, str]] = {
    "overview": "full",
    "geometries": "geojson",
    "steps": "true",
    "annotations": "distance,duration,speed",
}
"""Query parameters sent on every request; constant, so they are also part of the cache key."""

_COORDINATE_PRECISION: Final[int] = 5
"""Decimal places used in the URL and the cache key — 5 dp is ~1.1 m, below OSRM's snapping."""

_CACHE_NAMESPACE: Final[str] = "osrm"
"""Cache namespace for route responses."""

_ROUTE_TTL_S: Final[int] = 0
"""Zero means "never expires" in :class:`~autotwin_core.providers.cache.FileCache`.

A route is a function of the road network and two coordinates, and OSRM models neither traffic
nor time of day, so a cached answer stays correct until the underlying OSM extract is rebuilt.
Re-asking a volunteer-run server the same question on a timer would be pure waste.
"""

FIXTURE_ORIGIN: Final[Coordinate] = Coordinate(latitude=50.1109, longitude=8.6821)
"""Frankfurt am Main — origin of the bundled demo route."""

FIXTURE_DESTINATION: Final[Coordinate] = Coordinate(latitude=48.7758, longitude=9.1829)
"""Stuttgart — destination of the bundled demo route."""

FIXTURE_MATCH_KM: Final[float] = 30.0
"""How far a request may sit from the fixture's endpoints and still be answered by it.

30 km covers "Frankfurt Hauptbahnhof" versus "Frankfurt city centre" versus a park-and-ride on
the edge of town. Beyond that the fixture is a *different journey*, and returning it would be
inventing data — so the adapter raises instead. That is the one case where a provider is
required to fail rather than degrade (BUILD_SPEC §4).
"""

_GERMAN_ROAD_CLASSES: Final[dict[str, RoadClass]] = {
    "A": RoadClass.motorway,
    "B": RoadClass.primary,
    "L": RoadClass.secondary,
    "S": RoadClass.secondary,
    "K": RoadClass.tertiary,
}
"""Letter of a German road number → functional class.

``E`` (Europastraße) is deliberately absent: in Germany an E-number is always co-signed on an
A- or B-road, so it adds no information and would misclassify the few B-roads that carry one.
"""


class OSRMRoutingProvider(RoutingProvider):
    """Computes routes with OSRM (BUILD_SPEC §4).

    The fallback chain is ``live → cache → fixture``, with the fixture rung restricted to
    requests that actually match the bundled demo corridor (see :data:`FIXTURE_MATCH_KM`).
    """

    name = "osrm_routing"
    source = SourceSystem.osrm

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        http_client: AutoTwinHTTPClient | None = None,
        cache: FileCache | None = None,
        data_mode: DataMode | None = None,
    ) -> None:
        """Configure the adapter.

        Args:
            settings: Configuration source; defaults to the process-wide settings.
            http_client: Shared HTTP client. When omitted one is created with the routing
                timeout — which is separate from the general HTTP timeout precisely because a
                route request blocks a user-facing page.
            cache: On-disk cache; defaults to ``AUTOTWIN_DATA_DIR/cache``.
            data_mode: Override the configured fallback policy.
        """
        self._settings = settings or get_settings()
        self._data_mode = data_mode or self._settings.data_mode
        self._cache = cache or FileCache()
        self._http = http_client
        self._owns_http = http_client is None

    async def aclose(self) -> None:
        """Release the HTTP client if this adapter created it."""
        if self._http is not None and self._owns_http:
            await self._http.aclose()
            self._http = None

    async def route(
        self,
        origin: Coordinate,
        destination: Coordinate,
        *,
        profile: str = DEFAULT_PROFILE,
    ) -> ProviderResult[RouteResult]:
        """Compute a route from ``origin`` to ``destination``.

        Args:
            origin: Start point in WGS 84.
            destination: End point in WGS 84.
            profile: OSRM profile; ``driving`` is the only one AutoTwin models.

        Returns:
            The first route OSRM returns, with full geometry, step summaries and the raw
            per-node annotation arrays.

        Raises:
            ProviderUnavailable: the engine was unreachable, found no route, or the request
                is too far from the bundled demo corridor for the fixture to answer it.
            ProviderTimeout: the engine did not answer within ``AUTOTWIN_OSRM_TIMEOUT_S``.
            InvalidSourceData: the response was not a route AutoTwin can read.
            RateLimited: the public demo server refused the request.
        """
        url = self._route_url(origin, destination, profile)
        key = self._cache_key(origin, destination, profile)

        if self._data_mode is not DataMode.fixture:
            try:
                payload = await self._client().get_json(url, params=_QUERY)
            except ProviderError as error:
                if self._data_mode is DataMode.live:
                    raise
                _logger.warning(f"OSRM unavailable: {error}", provider=self.name)
                reason = str(error)
            else:
                self._cache.set_json(_CACHE_NAMESPACE, key, payload)
                return ProviderResult.live(
                    parse_route(payload, profile=profile, source_url=url),
                    source_url=url,
                )

            cached = self._cache.get_json(
                _CACHE_NAMESPACE, key, ttl_seconds=_ROUTE_TTL_S, allow_stale=True
            )
            if cached is not None:
                age = self._cache.age_seconds(_CACHE_NAMESPACE, key, suffix=".json") or 0.0
                return ProviderResult.cached(
                    parse_route(cached, profile=profile, source_url=url),
                    source_url=url,
                    fetched_at=_ago(age),
                ).with_warning(f"Live routing unavailable: {reason}")

        return self._read_fixture(origin, destination, profile, url)

    # ------------------------------------------------------------------ internals

    def _read_fixture(
        self,
        origin: Coordinate,
        destination: Coordinate,
        profile: str,
        url: str,
    ) -> ProviderResult[RouteResult]:
        """Answer from the bundled demo route, but only for a request it actually describes."""
        origin_km = haversine_km(origin, FIXTURE_ORIGIN)
        destination_km = haversine_km(destination, FIXTURE_DESTINATION)
        if max(origin_km, destination_km) > FIXTURE_MATCH_KM:
            msg = (
                "no routing engine answered and the bundled fixture covers only "
                f"Frankfurt am Main → Stuttgart (requested origin is {origin_km:.0f} km and "
                f"destination {destination_km:.0f} km away)"
            )
            raise ProviderUnavailable(
                msg,
                details={
                    "origin": origin.as_latlon_tuple(),
                    "destination": destination.as_latlon_tuple(),
                    "fixture_origin": FIXTURE_ORIGIN.as_latlon_tuple(),
                    "fixture_destination": FIXTURE_DESTINATION.as_latlon_tuple(),
                },
            )
        try:
            path = resolve_fixture(OSRM_ROUTE_FIXTURE)
        except FileNotFoundError as error:
            raise ConfigurationMissing(str(error)) from error
        payload = json.loads(path.read_text(encoding="utf-8"))
        return ProviderResult.fixture(
            parse_route(payload, profile=profile, source_url=url),
            source_url=url,
            fetched_at=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
            warnings=[
                "Serving the bundled Frankfurt am Main → Stuttgart route; the requested "
                f"endpoints were matched to it within {FIXTURE_MATCH_KM:.0f} km."
            ],
        )

    def _route_url(self, origin: Coordinate, destination: Coordinate, profile: str) -> str:
        """Build the request URL with longitude-first coordinate pairs, as OSRM expects."""
        base = self._settings.osrm_base_url.rstrip("/")
        return f"{base}/route/v1/{profile}/{_pair(origin)};{_pair(destination)}"

    def _cache_key(self, origin: Coordinate, destination: Coordinate, profile: str) -> str:
        """Cache key on rounded coordinates, so two clicks 20 cm apart share one answer."""
        return f"{profile}|{_pair(origin)}|{_pair(destination)}"

    def _client(self) -> AutoTwinHTTPClient:
        """Return the HTTP client, creating one with the routing timeout on first use."""
        if self._http is None:
            self._http = AutoTwinHTTPClient(timeout_s=self._settings.osrm_timeout_s)
            self._owns_http = True
        return self._http


# --------------------------------------------------------------------------- parsing


def parse_route(
    payload: Any,
    *,
    profile: str = DEFAULT_PROFILE,
    source_url: str | None = None,
) -> RouteResult:
    """Convert an OSRM ``/route`` response into a :class:`RouteResult`.

    Args:
        payload: The decoded JSON body.
        profile: Profile the engine was asked for, recorded on the result.
        source_url: Endpoint the body came from, written into the provenance block.

    Raises:
        ProviderUnavailable: OSRM answered correctly but found no route (``code`` is
            ``NoRoute``/``NoSegment``) — an empty answer to a legitimate question, not a
            malformed one.
        InvalidSourceData: the body is not an OSRM route response.
    """
    if not isinstance(payload, Mapping):
        msg = f"OSRM returned {type(payload).__name__}, expected an object"
        raise InvalidSourceData(msg)

    code = str(payload.get("code", ""))
    if code and code != "Ok":
        message = str(payload.get("message", "")) or code
        if code.startswith("No"):
            raise ProviderUnavailable(f"OSRM found no route: {message}")
        msg = f"OSRM rejected the request ({code}): {message}"
        raise InvalidSourceData(msg)

    routes = payload.get("routes")
    if not isinstance(routes, list) or not routes:
        msg = "OSRM response carries no routes"
        raise InvalidSourceData(msg)
    route = routes[0]
    if not isinstance(route, Mapping):
        msg = "OSRM route entry is not an object"
        raise InvalidSourceData(msg)

    geometry = _geometry_of(route)
    if len(geometry) < 2:
        msg = f"OSRM route geometry has {len(geometry)} point(s), expected at least 2"
        raise InvalidSourceData(msg)

    legs = [leg for leg in _sequence(route.get("legs")) if isinstance(leg, Mapping)]
    return RouteResult(
        geometry=geometry,
        distance_m=_non_negative(route.get("distance")),
        duration_s=_non_negative(route.get("duration")),
        profile=profile,
        leg_count=max(len(legs), 1),
        steps=tuple(_steps_of(legs)),
        weight_name=_text(route.get("weight_name")),
        annotations=_annotations_of(legs),
        provenance=ProvenanceInfo(
            source=SourceSystem.osrm,
            source_url=source_url,
            data_origin=DataOrigin.derived,
            ingested_at=utc_now(),
        ),
    )


def _steps_of(legs: Sequence[Mapping[str, Any]]) -> list[RouteStepRecord]:
    """Flatten every leg's steps into one travel-ordered list of step summaries."""
    steps: list[RouteStepRecord] = []
    ordinal = 0
    for leg in legs:
        for raw in _sequence(leg.get("steps")):
            if not isinstance(raw, Mapping):
                continue
            ref = _text(raw.get("ref"))
            steps.append(
                RouteStepRecord(
                    ordinal=ordinal,
                    name=_text(raw.get("name")) or ref,
                    distance_m=_non_negative(raw.get("distance")),
                    duration_s=_non_negative(raw.get("duration")),
                    road_class=road_class_from_ref(ref),
                    speed_limit_kmh=None,
                    geometry=tuple(_geometry_of(raw)),
                )
            )
            ordinal += 1
    return steps


def road_class_from_ref(ref: str | None) -> RoadClass:
    """Map a German road reference onto a :class:`RoadClass`.

    OSRM reports the reference exactly as it is signposted: ``"A 5"``, ``"B 27"``, ``"K 818"``,
    and for a shared stretch ``"B 10; B 27"``. Where several references are listed the highest
    class wins, because that is the road being driven — a B-road co-signed with another B-road
    is still a Bundesstraße, and an A-road co-signed with a B-road is an Autobahn.

    Returns ``unknown`` for an unsigned street. That is deliberate: OSRM carries no
    ``highway`` tag, and guessing "residential" from the absence of a number would put city
    ring roads and farm tracks in the same bucket.
    """
    if not ref:
        return RoadClass.unknown
    best = RoadClass.unknown
    for token in ref.replace(",", ";").split(";"):
        letter = token.strip()[:1].upper()
        candidate = _GERMAN_ROAD_CLASSES.get(letter)
        if candidate is not None and candidate.ordinal > best.ordinal:
            best = candidate
    return best


def _annotations_of(legs: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Concatenate the per-leg annotation arrays into one dictionary of arrays.

    Kept raw rather than modelled: the energy pipeline reads ``speed`` as the engine's assumed
    free-flow speed per node, and a future feature may want ``duration`` or ``datasources``
    without a contract change.
    """
    merged: dict[str, list[Any]] = {}
    for leg in legs:
        annotation = leg.get("annotation")
        if not isinstance(annotation, Mapping):
            continue
        for key, values in annotation.items():
            if isinstance(values, list):
                merged.setdefault(str(key), []).extend(values)
    return dict(merged) if merged else None


def _geometry_of(container: Mapping[str, Any]) -> list[Coordinate]:
    """Read a GeoJSON ``LineString`` geometry; positions are longitude-first (RFC 7946)."""
    geometry = container.get("geometry")
    if not isinstance(geometry, Mapping):
        return []
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list):
        return []
    points: list[Coordinate] = []
    for position in coordinates:
        if not isinstance(position, list | tuple) or len(position) < 2:
            continue
        longitude, latitude = position[0], position[1]
        if not isinstance(longitude, int | float) or not isinstance(latitude, int | float):
            continue
        points.append(Coordinate.from_lonlat(float(longitude), float(latitude)))
    return points


def _sequence(value: object) -> list[Any]:
    """Return ``value`` as a list, or an empty list when it is not one."""
    return value if isinstance(value, list) else []


def _non_negative(value: object) -> float:
    """Read a distance or duration; anything unreadable becomes ``0.0``.

    The record model rejects negatives, and OSRM never emits them — this exists so that a
    truncated response degrades to a zero-length step instead of a validation traceback.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return max(float(value), 0.0)


def _text(value: object) -> str | None:
    """Stripped text, or ``None`` for an empty or non-string value."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _pair(coordinate: Coordinate) -> str:
    """Render a coordinate as OSRM's ``lon,lat`` with fixed precision."""
    return (
        f"{coordinate.longitude:.{_COORDINATE_PRECISION}f},"
        f"{coordinate.latitude:.{_COORDINATE_PRECISION}f}"
    )


def _ago(seconds: float) -> datetime:
    """The moment ``seconds`` ago, as aware UTC — how a cached answer dates itself."""
    return utc_now() - timedelta(seconds=seconds)
