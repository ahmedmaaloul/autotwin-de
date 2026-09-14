"""Autobahn GmbH des Bundes traffic adapter — roadworks, closures and warnings.

``https://verkehr.autobahn.de/o/autobahn/`` is keyless, anonymous and undocumented beyond a
community OpenAPI description. The endpoints used here are::

    GET /                                -> {"roads": ["A1", "A2", ..., "A60 ", ...]}
    GET /{roadId}/services/roadworks     -> {"roadworks": [...]}
    GET /{roadId}/services/closure       -> {"closure":   [...]}
    GET /{roadId}/services/warning       -> {"warning":   [...]}

**Field facts, measured against live responses rather than read from documentation**

``coordinate``
    ``{"lat": float, "long": float}`` — the key is ``long``, not ``lon`` or ``lng``.
``point`` / ``extent``
    comma-joined strings that are **latitude-first**, the opposite of ``geometry``.
``geometry``
    a GeoJSON ``LineString`` (longitude-first), undocumented but present on every item.
``isBlocked``
    the *string* ``"false"`` in 3327 of 3327 sampled items. **Dead field, never read.** The
    real blocking signal is ``"CLOSED" in impact.symbols``.
``impact``
    absent entirely on ``WARNING`` items, and ``symbols`` may contain nulls. Both are handled.
``startTimestamp``
    absent whenever ``display_type == "SHORT_TERM_ROADWORKS"`` — a perfect correlation across
    the sample. The German ``description`` lines then carry the only start there is.
``subtitle``
    the driving direction, with a **leading space**: ``" Nürnberg -> München"``.
``roads``
    listed with trailing whitespace (``"A60 "``), so every road id is stripped before use.

**Times in ``description`` are German civil time.** A ``WARNING`` item sampled on 2026-09-14
carried ``startTimestamp = 2026-09-14T10:14:00Z`` and the description line ``Beginn: 14.09.26
um 12:14 Uhr`` — the same instant, two hours apart, which pins the description clock to
``Europe/Berlin`` and not to UTC. :func:`parse_german_datetime` converts accordingly.

**Scope.** The network has 108 road ids. Fetching all of them is 324 requests for a page that
renders a handful of events, so the default is the corridor subset
:data:`DEFAULT_ROADS` (A1-A9, which covers every demo corridor) with at most
:data:`MAX_CONCURRENT_REQUESTS` requests in flight. A ``bbox`` is applied *after* the fetch,
because the API offers no spatial filter of its own.

**Licence caveat.** No licence statement is published with this API. AutoTwin treats it as
available for development with the licence unconfirmed: cached locally, never redistributed,
low request rate (see ``DATA_LICENSES.md``). The :class:`TrafficProvider` interface exists so
that a Mobilithek (DATEX II) adapter can replace this one without touching a caller.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from autotwin_contracts import (
    BoundingBox,
    Coordinate,
    DataOrigin,
    ProvenanceInfo,
    ProviderMode,
    SourceSystem,
    TrafficEventRecord,
    TrafficEventType,
    TrafficSeverity,
    utc_now,
)
from autotwin_core.config import DataMode, Settings, get_settings
from autotwin_core.errors import ConfigurationMissing, InvalidSourceData, ProviderError
from autotwin_core.logging import get_logger
from autotwin_core.providers import (
    AutoTwinHTTPClient,
    FileCache,
    ProviderResult,
    TrafficProvider,
    TrafficResult,
)
from autotwin_ingestion.fixtures import (
    AUTOBAHN_CLOSURE_FIXTURE,
    AUTOBAHN_ROADS_FIXTURE,
    AUTOBAHN_ROADWORKS_FIXTURE,
    AUTOBAHN_WARNING_FIXTURE,
    resolve_fixture,
)

__all__ = [
    "CLOSED_SYMBOL",
    "DEFAULT_ROADS",
    "MAX_CONCURRENT_REQUESTS",
    "SERVICES",
    "AutobahnTrafficProvider",
    "derive_severity",
    "extract_validity",
    "parse_german_datetime",
    "parse_item",
]

_logger = get_logger(__name__)

SERVICES: Final[tuple[str, ...]] = ("roadworks", "closure", "warning")
"""The three services read per road. The response key equals the service name."""

DEFAULT_ROADS: Final[tuple[str, ...]] = (
    "A1",
    "A2",
    "A3",
    "A4",
    "A5",
    "A6",
    "A7",
    "A8",
    "A9",
)
"""Default road subset: the nine trunk Autobahnen, which contain every demo corridor.

A3 (Frankfurt-Nürnberg), A5 (Frankfurt-Karlsruhe), A6 and A8 (Karlsruhe-Stuttgart-München)
are the corridors the simulator drives, and A1/A2/A4/A7/A9 carry the rest of the national
long-distance traffic. The remaining 99 road ids are reachable by passing ``roads=`` or by
calling :meth:`AutobahnTrafficProvider.fetch_roads` first.
"""

MAX_CONCURRENT_REQUESTS: Final[int] = 4
"""Requests in flight against a public-sector API that publishes no rate limit."""

CLOSED_SYMBOL: Final[str] = "CLOSED"
"""The only trustworthy blocking signal in the payload (``isBlocked`` is dead)."""

_CACHE_NAMESPACE: Final[str] = "autobahn"
"""Cache namespace for the road index and the per-road service responses."""

_GERMAN_TIMEZONE: Final[str] = "Europe/Berlin"
"""Clock the German ``description`` lines are written in — verified against a UTC sibling."""

_CENTURY_PIVOT: Final[int] = 80
"""Two-digit years below this are 20xx, at or above are 19xx.

Fixed rather than derived from today's date so that parsing is deterministic and a test does
not change meaning in 2081. Every value observed in the feed is a near-future year (``26``,
``27``), so the pivot is far from any real data.
"""

_BEGIN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"Beginn:\s*(\d{1,2})\.(\d{1,2})\.(\d{2,4})(?:\s*um\s*(\d{1,2}):(\d{2})\s*Uhr)?"
)
"""``Beginn: 26.04.26 um 05:00 Uhr`` — the labelled form used by long-term roadworks."""

_END_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"Ende:\s*(\d{1,2})\.(\d{1,2})\.(\d{2,4})(?:\s*um\s*(\d{1,2}):(\d{2})\s*Uhr)?"
)
"""``Ende: 30.09.26 um 20:00 Uhr``; the time part is optional and defaults to local midnight.

Deliberately *not* matched against ``(Ende der Gesamtmaßnahme: 30.09.26)``: that line states
when the whole construction project finishes, while the event describes one phase of it. The
colon has to follow ``Ende`` immediately, and in the Gesamtmaßnahme line it does not.
"""

_INTERVAL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})\s+(\d{1,2}):(\d{2})\s+bis\s+zum\s+"
    r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})\s+(\d{1,2}):(\d{2})\s*Uhr"
)
"""``15.09.26 19:30 bis zum 16.09.26 05:00 Uhr.`` — how a Tagesbaustelle states its phases.

This is the *only* temporal information a ``SHORT_TERM_ROADWORKS`` item carries: it has no
``startTimestamp`` and no ``Beginn:``/``Ende:`` line. Measured on a live A5 response, 48 of 48
short-term items were written in this form.
"""

_RANGE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"zwischen\s+dem\s+(\d{1,2})\.(\d{1,2})\.(\d{2,4})\s+und\s+dem\s+"
    r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})"
)
"""``Jeden Montag … zwischen dem 14.09.26 und dem 19.09.26 von 20:00 bis 00:00 Uhr.``

A recurring night-work window. Only the outer date range is taken, at local midnight on each
end: the event is *valid* across that range even though the road is only closed for part of
each night, and modelling the nightly windows would need a recurrence type the database does
not have. The approximation is documented rather than hidden because it errs towards showing
an event as active for longer than it blocks traffic.
"""

_STATIONARY_TRAFFIC: Final[str] = "STATIONARY_TRAFFIC"
_QUEUING_TRAFFIC: Final[str] = "QUEUING_TRAFFIC"
_SLOW_TRAFFIC: Final[str] = "SLOW_TRAFFIC"

_CONGESTION_SEVERITY: Final[dict[str, TrafficSeverity]] = {
    _STATIONARY_TRAFFIC: TrafficSeverity.severe,
    _QUEUING_TRAFFIC: TrafficSeverity.high,
    _SLOW_TRAFFIC: TrafficSeverity.moderate,
}
"""``abnormalTrafficType`` → severity. Stillstand is the only one that doubles travel time."""

_SHORT_TERM_ROADWORKS: Final[str] = "SHORT_TERM_ROADWORKS"
"""Tagesbaustelle — hours rather than months, and the reason ``startTimestamp`` is missing."""

_RAW_DROPPED_KEYS: Final[frozenset[str]] = frozenset(
    {"geometry", "footer", "lorryParkingFeatureIcons", "routeRecommendation", "icon", "isBlocked"}
)
"""Kept out of ``TrafficEventRecord.raw``: stored elsewhere, always empty, or dead."""


class AutobahnTrafficProvider(TrafficProvider):
    """Reads roadworks, closures and warnings from the Autobahn GmbH API (BUILD_SPEC §4).

    One provider call fans out to ``len(roads) * 3`` requests, each of which walks its own
    ``live → cache → fixture`` chain. The mode reported for the call is the least-live mode
    that contributed, so a single cached road downgrades the whole answer rather than letting
    a partly stale map claim to be live.
    """

    name = "autobahn_traffic_events"
    source = SourceSystem.autobahn

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        http_client: AutoTwinHTTPClient | None = None,
        cache: FileCache | None = None,
        data_mode: DataMode | None = None,
        roads: Sequence[str] = DEFAULT_ROADS,
        services: Sequence[str] = SERVICES,
    ) -> None:
        """Configure the adapter.

        Args:
            settings: Configuration source; defaults to the process-wide settings.
            http_client: Shared HTTP client. One is created — and owned — if omitted.
            cache: On-disk cache; defaults to ``AUTOTWIN_DATA_DIR/cache``.
            data_mode: Override the configured fallback policy.
            roads: Road ids to poll. Whitespace is stripped, so ``"A60 "`` from the API's own
                index is accepted verbatim.
            services: Services to poll per road.

        Raises:
            ValueError: an unknown service was requested, or no road was given.
        """
        unknown = sorted(set(services).difference(SERVICES))
        if unknown:
            msg = f"unknown Autobahn service(s) {unknown}; known: {list(SERVICES)}"
            raise ValueError(msg)
        cleaned = tuple(dict.fromkeys(road.strip() for road in roads if road.strip()))
        if not cleaned:
            msg = "at least one road id is required"
            raise ValueError(msg)

        self._settings = settings or get_settings()
        self._data_mode = data_mode or self._settings.data_mode
        self._cache = cache or FileCache()
        self._roads = cleaned
        self._services = tuple(services)
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        self._http = http_client
        self._owns_http = http_client is None

    async def aclose(self) -> None:
        """Release the HTTP client if this adapter created it."""
        if self._http is not None and self._owns_http:
            await self._http.aclose()
            self._http = None

    @property
    def roads(self) -> tuple[str, ...]:
        """Road ids this instance polls."""
        return self._roads

    async def fetch_events(self, bbox: BoundingBox | None = None) -> TrafficResult:
        """Fetch the currently published events for the configured roads.

        Args:
            bbox: Optional ``west, south, east, north`` filter, applied after the fetch
                because the API has no spatial parameter. An event matches when its
                representative point *or* any vertex of its line geometry falls inside.

        Returns:
            Events with a representative point each and a line geometry where published.

        Raises:
            ProviderUnavailable: live mode was demanded and the API was unreachable.
            RateLimited: the API refused the request for rate reasons.
            InvalidSourceData: a response was not a readable service payload.
            ConfigurationMissing: nothing answered and no fixture is installed.
        """
        modes: set[ProviderMode] = set()
        failures: list[str] = []
        substituted: set[str] = set()
        records: list[TrafficEventRecord] = []
        seen: set[str] = set()

        requests = [(road, service) for road in self._roads for service in self._services]
        tasks = [self._load_service(road, service, failures) for road, service in requests]
        # The oldest contributing payload sets the reported age: a result is only as fresh as
        # its stalest part, and one live road must not make five cached ones look current.
        oldest_fetch: datetime | None = None
        for road, service, payload, mode, fetched_at in await asyncio.gather(*tasks):
            modes.add(mode)
            if fetched_at is not None and (oldest_fetch is None or fetched_at < oldest_fetch):
                oldest_fetch = fetched_at
            if payload is None:
                continue
            effective_road = road
            if mode is ProviderMode.fixture and road != _FIXTURE_ROAD:
                # The bundled sample was captured from one road. Labelling its events with the
                # road that was *asked* for would put A5 roadworks on the A1 in the database,
                # so the fixture's own road wins and the substitution is reported.
                substituted.add(road)
                effective_road = _FIXTURE_ROAD
            for item in _items_of(payload, service):
                record = parse_item(item, road=effective_road, service=service, source_url=None)
                if record is None:
                    continue
                if record.external_id is not None:
                    if record.external_id in seen:
                        continue
                    seen.add(record.external_id)
                if bbox is not None and not _within(record, bbox):
                    continue
                records.append(record)

        if not modes:  # pragma: no cover - guarded by the non-empty road list
            msg = "no Autobahn service was polled"
            raise ConfigurationMissing(msg)

        warnings: list[str] = []
        if failures:
            warnings.append(
                f"{len(failures)} of {len(requests)} Autobahn requests failed; first: {failures[0]}"
            )
        if substituted:
            warnings.append(
                f"no live or cached data for {', '.join(sorted(substituted))}; "
                f"those roads were answered from the bundled {_FIXTURE_ROAD} sample"
            )

        mode = _weakest_mode(modes)
        records.sort(key=_sort_key)
        return ProviderResult(
            data=records,
            mode=mode,
            source_url=self._settings.autobahn_base_url.rstrip("/") + "/",
            # Never utc_now() unconditionally: a cached answer reports when it was really
            # fetched, so /data-quality shows the true age instead of a comforting fiction.
            fetched_at=oldest_fetch or utc_now(),
            warnings=warnings,
        )

    async def fetch_roads(self) -> ProviderResult[list[str]]:
        """Fetch the index of road ids the API knows about, stripped of trailing whitespace."""
        url = self._settings.autobahn_base_url.rstrip("/") + "/"
        failures: list[str] = []
        payload, mode, fetched_at = await self._load(
            url, _CACHE_NAMESPACE, "roads", AUTOBAHN_ROADS_FIXTURE, failures
        )
        if payload is None:
            msg = f"{url} returned no road index and no fixture is installed"
            raise ConfigurationMissing(msg)
        raw = payload.get("roads")
        if not isinstance(raw, list):
            msg = f"{url} did not return a 'roads' list"
            raise InvalidSourceData(msg)
        roads = sorted({str(road).strip() for road in raw if str(road).strip()})
        return ProviderResult(
            data=roads,
            mode=mode,
            source_url=url,
            fetched_at=fetched_at or utc_now(),
            warnings=failures,
        )

    # ------------------------------------------------------------------ internals

    async def _load_service(
        self,
        road: str,
        service: str,
        failures: list[str],
    ) -> tuple[str, str, Mapping[str, Any] | None, ProviderMode, datetime | None]:
        """Load one ``(road, service)`` response through the fallback chain."""
        url = self._service_url(road, service)
        fixture = _FIXTURE_BY_SERVICE.get(service)
        async with self._semaphore:
            payload, mode, fetched_at = await self._load(
                url, _CACHE_NAMESPACE, url, fixture, failures
            )
        return road, service, payload, mode, fetched_at

    async def _load(
        self,
        url: str,
        namespace: str,
        key: str,
        fixture_name: str | None,
        failures: list[str],
    ) -> tuple[Mapping[str, Any] | None, ProviderMode, datetime | None]:
        """Walk ``live → cache → fixture`` for one JSON endpoint.

        Returns the payload, the mode that produced it, and **when that payload was actually
        retrieved**. The third element is the point: a cached answer is not fresh, and reporting
        ``utc_now()`` for it would make a six-hour-old roadworks list look like it arrived this
        second — which is precisely the kind of quiet dishonesty this project exists not to do.
        ``None`` means "unknown", which the caller renders as the cache age being unavailable.
        """
        if self._data_mode is not DataMode.fixture:
            try:
                payload = await self._client().get_json(url)
            except ProviderError as error:
                if self._data_mode is DataMode.live:
                    raise
                failures.append(f"{url} unavailable: {error}")
            else:
                if isinstance(payload, dict):
                    self._cache.set_json(namespace, key, payload)
                    return payload, ProviderMode.live, utc_now()
                msg = f"{url} returned {type(payload).__name__}, expected a JSON object"
                raise InvalidSourceData(msg)

            cached = self._cache.get_json(namespace, key, allow_stale=True)
            if isinstance(cached, dict):
                age_s = self._cache.age_seconds(namespace, key, suffix=".json")
                cached_at = None if age_s is None else utc_now() - timedelta(seconds=age_s)
                return cached, ProviderMode.cache, cached_at

        # A bundled fixture has no meaningful retrieval time: it is a committed sample, not
        # something that was fetched. Saying so is more useful than inventing a timestamp.
        if fixture_name is None:
            return None, ProviderMode.fixture, None
        try:
            path = resolve_fixture(fixture_name)
        except FileNotFoundError:
            return None, ProviderMode.fixture, None
        return _read_json(path), ProviderMode.fixture, None

    def _service_url(self, road: str, service: str) -> str:
        """Absolute URL of one road's service endpoint."""
        base = self._settings.autobahn_base_url.rstrip("/")
        return f"{base}/{road}/services/{service}"

    def _client(self) -> AutoTwinHTTPClient:
        """Return the HTTP client, creating the owned one on first use."""
        if self._http is None:
            self._http = AutoTwinHTTPClient()
            self._owns_http = True
        return self._http


_FIXTURE_ROAD: Final[str] = "A5"
"""Road the bundled service fixtures were captured from."""

_FIXTURE_BY_SERVICE: Final[dict[str, str]] = {
    "roadworks": AUTOBAHN_ROADWORKS_FIXTURE,
    "closure": AUTOBAHN_CLOSURE_FIXTURE,
    "warning": AUTOBAHN_WARNING_FIXTURE,
}
"""Service → bundled sample response."""


# --------------------------------------------------------------------------- parsing


def parse_item(
    item: Mapping[str, Any],
    *,
    road: str,
    service: str,
    source_url: str | None,
) -> TrafficEventRecord | None:
    """Convert one API item into a :class:`TrafficEventRecord`.

    Returns ``None`` — rather than raising — for an item that cannot be located, because a
    single malformed entry among three hundred is a data-quality finding, not an outage.
    """
    coordinate = _coordinate_of(item)
    if coordinate is None:
        _logger.warning(f"Autobahn item without a usable location: {item.get('identifier')!r}")
        return None

    symbols = _symbols_of(item)
    is_blocked = CLOSED_SYMBOL in symbols
    abnormal = _text(item.get("abnormalTrafficType"))
    description = _description_of(item)
    derived_start, derived_end = extract_validity(description)
    starts_at = _parse_iso8601(_text(item.get("startTimestamp"))) or derived_start
    title = _text(item.get("title")) or f"{road} {service}"

    return TrafficEventRecord(
        external_id=_text(item.get("identifier")),
        event_type=_event_type(service, abnormal),
        severity=derive_severity(
            service=service,
            display_type=_text(item.get("display_type")),
            symbols=symbols,
            abnormal_traffic_type=abnormal,
        ),
        road_name=road.strip() or None,
        direction=_text(item.get("subtitle")),
        title=title,
        description=description or None,
        coordinate=coordinate,
        geometry=tuple(_geometry_of(item)),
        starts_at=starts_at,
        ends_at=derived_end,
        is_blocked=is_blocked,
        delay_minutes=_positive_float(item.get("delayTimeValue")),
        raw={key: value for key, value in item.items() if key not in _RAW_DROPPED_KEYS},
        provenance=ProvenanceInfo(
            source=SourceSystem.autobahn,
            source_identifier=_text(item.get("identifier")),
            source_url=source_url,
            source_timestamp=starts_at,
            data_origin=DataOrigin.official,
        ),
    )


def derive_severity(
    *,
    service: str,
    display_type: str | None,
    symbols: Sequence[str],
    abnormal_traffic_type: str | None,
) -> TrafficSeverity:
    """Classify an event's impact on travel time.

    The API states no severity, so it is derived from the three things it does state: which
    service published the item, whether the impact symbols contain ``CLOSED``, and — for
    warnings — what kind of abnormal traffic INRIX reported.

    =============  ==========================  =========  ==============
    service        display_type                CLOSED?    severity
    =============  ==========================  =========  ==============
    ``closure``    any                         yes        ``severe``
    ``closure``    any                         no         ``high``
    ``roadworks``  ``SHORT_TERM_ROADWORKS``    yes        ``high``
    ``roadworks``  ``SHORT_TERM_ROADWORKS``    no         ``low``
    ``roadworks``  ``ROADWORKS``               yes        ``high``
    ``roadworks``  ``ROADWORKS``               no         ``moderate``
    ``warning``    stationary traffic          —          ``severe``
    ``warning``    queuing traffic             —          ``high``
    ``warning``    slow traffic                —          ``moderate``
    ``warning``    no abnormal traffic type    yes        ``high``
    ``warning``    no abnormal traffic type    no         ``low``
    =============  ==========================  =========  ==============

    A ``SHORT_TERM_ROADWORKS`` item without a closure is only ``low`` because a Tagesbaustelle
    on the hard shoulder is measured in minutes of delay; a multi-month ``ROADWORKS`` narrowing
    a carriageway is the ``moderate`` baseline. Lane symbols such as ``BREAKDOWN_LANE`` never
    raise the level on their own — the sample shows them on items with no measurable impact.
    """
    if abnormal_traffic_type:
        mapped = _CONGESTION_SEVERITY.get(abnormal_traffic_type.strip().upper())
        if mapped is not None:
            return mapped
    blocked = CLOSED_SYMBOL in symbols
    if service == "closure":
        return TrafficSeverity.severe if blocked else TrafficSeverity.high
    if service == "roadworks":
        if blocked:
            return TrafficSeverity.high
        if display_type == _SHORT_TERM_ROADWORKS:
            return TrafficSeverity.low
        return TrafficSeverity.moderate
    return TrafficSeverity.high if blocked else TrafficSeverity.low


def extract_validity(description: str) -> tuple[datetime | None, datetime | None]:
    """Derive ``(starts_at, ends_at)`` from the German report body, in UTC.

    The Autobahn API states a machine-readable ``startTimestamp`` on long-term items only, and
    never states an end at all. Everything else is in prose, in three shapes that between them
    cover every item in the live sample:

    ==============================  ================================================
    shape                           used by
    ==============================  ================================================
    ``Beginn:`` / ``Ende:`` lines    long-term ``ROADWORKS``
    ``… bis zum … Uhr``              ``SHORT_TERM_ROADWORKS`` and phased closures
    ``zwischen dem … und dem …``     recurring night-work closures
    ==============================  ================================================

    A body may carry several phases; the earliest start and the latest end are taken, so the
    record covers the whole disruption rather than one arbitrary night of it.

    Returns ``(None, None)`` when the body states no dates — which is a real outcome, not a
    parse failure, and is left as nulls rather than filled with the ingestion time.
    """
    starts: list[datetime] = []
    ends: list[datetime] = []

    begin = parse_german_datetime(description, _BEGIN_PATTERN)
    if begin is not None:
        starts.append(begin)
    end = parse_german_datetime(description, _END_PATTERN)
    if end is not None:
        ends.append(end)

    for match in _INTERVAL_PATTERN.finditer(description):
        day, month, year, hour, minute = match.group(1, 2, 3, 4, 5)
        moment = _to_utc(day, month, year, hour, minute)
        if moment is not None:
            starts.append(moment)
        day, month, year, hour, minute = match.group(6, 7, 8, 9, 10)
        moment = _to_utc(day, month, year, hour, minute)
        if moment is not None:
            ends.append(moment)

    for match in _RANGE_PATTERN.finditer(description):
        day, month, year = match.group(1, 2, 3)
        moment = _to_utc(day, month, year, None, None)
        if moment is not None:
            starts.append(moment)
        day, month, year = match.group(4, 5, 6)
        moment = _to_utc(day, month, year, None, None)
        if moment is not None:
            ends.append(moment)

    return (min(starts) if starts else None, max(ends) if ends else None)


def parse_german_datetime(text: str, pattern: re.Pattern[str]) -> datetime | None:
    """Extract ``DD.MM.YY[YY][ um HH:MM Uhr]`` with a labelled pattern, as aware UTC.

    ``pattern`` must expose five groups — day, month, year, hour, minute — with the last two
    optional. Only the first match is used, which is why the multi-phase shapes go through
    :func:`extract_validity` instead.
    """
    match = pattern.search(text)
    if match is None:
        return None
    day, month, year, hour, minute = match.groups()
    return _to_utc(day, month, year, hour, minute)


def _to_utc(
    day: str,
    month: str,
    year: str,
    hour: str | None,
    minute: str | None,
) -> datetime | None:
    """Build an aware UTC datetime from German civil-time components.

    The description lines are written in ``Europe/Berlin``, which was established by comparing
    a warning's ``Beginn: 14.09.26 um 12:14 Uhr`` with its own ``startTimestamp`` of
    ``2026-09-14T10:14:00Z`` — the same instant, two hours apart. Parsing them as UTC would
    move every roadworks end by one or two hours depending on the season.

    A missing time part means local midnight, which is how the feed itself writes an all-day
    closure (``Ende: 17.10.26 um 00:00 Uhr``).

    Raises:
        ConfigurationMissing: the runtime has no IANA time-zone database, so German civil time
            cannot be resolved. ``tzdata`` is a declared dependency so that this cannot happen
            on a platform without a system one.
    """
    try:
        local = datetime(
            _full_year(int(year)),
            int(month),
            int(day),
            int(hour) if hour else 0,
            int(minute) if minute else 0,
            tzinfo=_german_timezone(),
        )
    except ValueError:
        # An impossible date such as 31.02.26; the feed is written by humans.
        return None
    return local.astimezone(UTC)


def _full_year(raw: int) -> int:
    """Expand a two-digit year using the fixed pivot; four-digit years pass through."""
    if raw >= 100:
        return raw
    return 2000 + raw if raw < _CENTURY_PIVOT else 1900 + raw


@lru_cache(maxsize=1)
def _german_timezone() -> ZoneInfo:
    """The ``Europe/Berlin`` zone, resolved once per process."""
    try:
        return ZoneInfo(_GERMAN_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError, OSError) as error:
        msg = (
            f"time-zone database entry {_GERMAN_TIMEZONE!r} is unavailable, so German report "
            f"times cannot be converted to UTC: {error!r}"
        )
        raise ConfigurationMissing(msg) from error


def _event_type(service: str, abnormal_traffic_type: str | None) -> TrafficEventType:
    """Map service and abnormal-traffic type onto the canonical vocabulary.

    A ``warning`` carrying an ``abnormalTrafficType`` is a Stau report, which the project
    models as ``congestion``; a warning without one is a generic Gefahrenmeldung.
    """
    if service == "roadworks":
        return TrafficEventType.roadworks
    if service == "closure":
        return TrafficEventType.closure
    if abnormal_traffic_type:
        return TrafficEventType.congestion
    return TrafficEventType.warning


def _coordinate_of(item: Mapping[str, Any]) -> Coordinate | None:
    """Representative point: ``coordinate`` first, then ``point``, then the geometry.

    ``coordinate`` uses the key ``long``; ``point`` is a latitude-first comma string. Both
    orders are hard-coded from measurement, never guessed.
    """
    raw = item.get("coordinate")
    if isinstance(raw, Mapping):
        latitude = _float(raw.get("lat"))
        longitude = _float(raw.get("long"))
        if latitude is not None and longitude is not None:
            return _coordinate(latitude, longitude)

    point = _text(item.get("point"))
    if point:
        parts = point.split(",")
        if len(parts) >= 2:
            latitude = _float(parts[0])
            longitude = _float(parts[1])
            if latitude is not None and longitude is not None:
                return _coordinate(latitude, longitude)

    geometry = _geometry_of(item)
    return geometry[0] if geometry else None


def _geometry_of(item: Mapping[str, Any]) -> list[Coordinate]:
    """Read the undocumented GeoJSON ``LineString``; positions are longitude-first."""
    raw = item.get("geometry")
    if not isinstance(raw, Mapping) or raw.get("type") != "LineString":
        return []
    coordinates = raw.get("coordinates")
    if not isinstance(coordinates, list):
        return []
    points: list[Coordinate] = []
    for position in coordinates:
        if not isinstance(position, list | tuple) or len(position) < 2:
            continue
        longitude = _float(position[0])
        latitude = _float(position[1])
        if latitude is None or longitude is None:
            continue
        point = _coordinate(latitude, longitude)
        if point is not None:
            points.append(point)
    return points


def _symbols_of(item: Mapping[str, Any]) -> list[str]:
    """Impact symbols as a clean list.

    ``impact`` is absent on every warning and its ``symbols`` list contains nulls in the wild,
    so both are normalised here instead of at each of the three call sites.
    """
    impact = item.get("impact")
    if not isinstance(impact, Mapping):
        return []
    symbols = impact.get("symbols")
    if not isinstance(symbols, list):
        return []
    return [str(symbol) for symbol in symbols if isinstance(symbol, str)]


def _description_of(item: Mapping[str, Any]) -> str:
    """Join the German ``description`` lines into one text block."""
    lines = item.get("description")
    if not isinstance(lines, list):
        return ""
    return "\n".join(str(line) for line in lines if isinstance(line, str)).strip()


def _items_of(payload: Mapping[str, Any], service: str) -> list[Mapping[str, Any]]:
    """Read the service's item list; an absent key means "nothing to report"."""
    raw = payload.get(service)
    if raw is None:
        return []
    if not isinstance(raw, list):
        msg = f"Autobahn payload key {service!r} holds {type(raw).__name__}, expected a list"
        raise InvalidSourceData(msg)
    return [item for item in raw if isinstance(item, Mapping)]


def _within(record: TrafficEventRecord, bbox: BoundingBox) -> bool:
    """Whether the event touches the box — representative point or any geometry vertex."""
    if bbox.contains(record.coordinate):
        return True
    return any(bbox.contains(point) for point in record.geometry)


def _sort_key(record: TrafficEventRecord) -> tuple[int, str]:
    """Order by descending severity, then by id, so paging is stable across calls."""
    return -record.severity.ordinal, record.external_id or record.title


def _coordinate(latitude: float, longitude: float) -> Coordinate | None:
    """Build a coordinate, returning ``None`` for out-of-range values instead of raising."""
    if not (-90.0 <= latitude <= 90.0) or not (-180.0 <= longitude <= 180.0):
        return None
    return Coordinate(latitude=latitude, longitude=longitude)


def _parse_iso8601(raw: str | None) -> datetime | None:
    """Parse ``startTimestamp`` — ISO-8601 with an offset or a ``Z`` suffix — as aware UTC."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _text(value: object) -> str | None:
    """Stripped text, or ``None`` for an empty or non-string value.

    Stripping is what removes the leading space the API puts in front of every ``subtitle``.
    """
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _float(value: object) -> float | None:
    """Parse a float from a number or a numeric string; ``None`` when it is neither."""
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


def _positive_float(value: object) -> float | None:
    """Parse a non-negative float — ``delayTimeValue`` arrives as the string ``"6"``."""
    parsed = _float(value)
    return parsed if parsed is not None and parsed >= 0.0 else None


def _read_json(path: Path) -> Mapping[str, Any]:
    """Read a bundled fixture, raising the typed error if it is not a JSON object."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        msg = f"fixture {path.name} is {type(payload).__name__}, expected a JSON object"
        raise InvalidSourceData(msg)
    return payload


def _weakest_mode(modes: Iterable[ProviderMode]) -> ProviderMode:
    """Least-live mode of the responses that contributed."""
    ranked = {ProviderMode.live: 0, ProviderMode.cache: 1, ProviderMode.fixture: 2}
    collected = list(modes)
    if not collected:
        return ProviderMode.fixture
    return max(collected, key=lambda mode: ranked[mode])
