"""Deutscher Wetterdienst open-data adapter — 10-minute "now" observations.

Air temperature is the strongest weather driver of EV consumption (BUILD_SPEC §10.1), so the
requirement on this adapter is narrow and concrete: for any point on a route, return the most
recent real measurement from the nearest station that reports one.

**Why the 10-minute "now" products.** DWD publishes hourly and daily aggregates too, but the
``now`` directories are the real-time feed — updated every ten minutes, one small ZIP per
station, no key, no registration. Three of them are read::

    climate_environment/CDC/observations_germany/climate/10_minutes/air_temperature/now/
    climate_environment/CDC/observations_germany/climate/10_minutes/wind/now/
    climate_environment/CDC/observations_germany/climate/10_minutes/precipitation/now/

**Parsing facts, all verified against the real files on 2026-09-14**

* Observation files are ``;``-delimited **with space padding** (``"         44"``) — every
  field must be stripped, on both the header and the data rows.
* Encoding is **ISO-8859-1**. ``Großenkneten`` and ``Baden-Württemberg`` mojibake as UTF-8.
* ``-999`` means *missing*. It must become ``None`` before any arithmetic; averaged in, it
  turns a mild autumn day into an ice age.
* ``eor`` is a literal end-of-row sentinel in the last column and is discarded.
* ``MESS_DATUM`` is UTC, ``YYYYMMDDHHMM`` for the 10-minute products (``YYYYMMDDHH`` for
  hourly ones — the width differs per product, so it is measured, not assumed).
* ``STATIONS_ID`` is space-padded in the data and zero-padded to five digits in filenames.
  Both are normalised to ``int``, or the join between catalogue and observation fails
  silently and every station looks like it has no data.
* The station catalogue is genuinely **fixed-width** with a dash ruler on line 2.
  ``str.split()`` breaks on ``Seebach (Nationalpark Schwarzwald)``; this module slices the
  fixed-width tail instead, and falls back to a token walk only if that ever stops matching.

**Politeness.** Fetching all ~470 stations for one route would be abusive for a public-sector
file server that asks for nothing in return. Each call therefore resolves the *nearest*
station per requested point, de-duplicates, caps the result at
:attr:`DWDWeatherProvider.max_stations`, and runs at most
:data:`MAX_CONCURRENT_DOWNLOADS` downloads at a time. The station catalogue is cached for a
day, since stations do not move.

**Attribution.** „Quelle: Deutscher Wetterdienst" is required on anything derived from this
data. It travels on every result as the first entry of ``warnings`` — the one channel a
:class:`~autotwin_core.providers.result.ProviderResult` has that reaches the UI verbatim — so
the frontend can render it next to the temperature instead of hard-coding it.
"""

from __future__ import annotations

import asyncio
import io
import re
import zipfile
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final

from autotwin_contracts import (
    Bundesland,
    Coordinate,
    DataOrigin,
    ProvenanceInfo,
    ProviderMode,
    SourceSystem,
    WeatherCondition,
    WeatherRecord,
    WeatherStationRecord,
    haversine_km,
    utc_now,
)
from autotwin_core.config import DataMode, Settings, get_settings
from autotwin_core.errors import ConfigurationMissing, InvalidSourceData, ProviderError
from autotwin_core.logging import get_logger
from autotwin_core.providers import (
    AutoTwinHTTPClient,
    FileCache,
    ProviderResult,
    WeatherProvider,
    WeatherResult,
)
from autotwin_ingestion.fixtures import (
    DWD_PRODUCT_FIXTURES,
    DWD_STATIONS_FIXTURE,
    resolve_fixture,
)

__all__ = [
    "DWD_ATTRIBUTION",
    "MAX_CONCURRENT_DOWNLOADS",
    "MISSING_VALUE",
    "PRODUCTS",
    "DWDWeatherProvider",
    "derive_condition",
    "parse_product_file",
    "parse_station_catalogue",
]

_logger = get_logger(__name__)

DWD_ATTRIBUTION: Final[str] = "Quelle: Deutscher Wetterdienst"
"""Attribution the DWD open-data licence requires on anything derived from these files."""

_OBSERVATIONS_ROOT: Final[str] = "climate_environment/CDC/observations_germany/climate/10_minutes"
"""Path of the 10-minute observation tree on ``opendata.dwd.de``."""

PRODUCTS: Final[dict[str, str]] = {
    "air_temperature": "TU",
    "wind": "wind",
    "precipitation": "nieder",
}
"""Product directory → the token used in ``10minutenwerte_<token>_<station>_now.zip``.

The tokens are not derivable from the directory names — ``air_temperature`` files are named
``TU`` (Temperatur/Feuchte), ``precipitation`` files ``nieder`` — so they are listed, not
computed.
"""

_STATION_CATALOGUE_FILE: Final[str] = "zehn_now_tu_Beschreibung_Stationen.txt"
"""Catalogue of stations reporting the 10-minute temperature product."""

_CATALOGUE_ENCODING: Final[str] = "iso-8859-1"
"""Encoding of every DWD Climate Data Center text file."""

MISSING_VALUE: Final[float] = -999.0
"""DWD's sentinel for a missing measurement."""

_END_OF_ROW: Final[str] = "eor"
"""Trailing sentinel column in every ``produkt_*.txt``."""

MAX_CONCURRENT_DOWNLOADS: Final[int] = 4
"""Simultaneous requests to opendata.dwd.de. Deliberately low — it is a free public service."""

_DEFAULT_MAX_STATIONS: Final[int] = 8
"""Stations fetched per call unless the caller says otherwise."""

_CATALOGUE_TTL_S: Final[int] = 86_400
"""Stations do not move; re-reading the 470 KB catalogue more than once a day is waste."""

_OBSERVATION_TTL_S: Final[int] = 600
"""The ``now`` products advance every ten minutes, so that is their natural cache lifetime."""

_CACHE_NAMESPACE: Final[str] = "dwd"
"""Cache namespace for the catalogue and the per-station product ZIPs."""

_CATALOGUE_HEADER_ROWS: Final[int] = 2
"""Column header plus the dash ruler."""

_CATALOGUE_ROW_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\s*(\d+)\s+(\d{8})\s+(\d{8})\s+(-?\d+)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s(.*)$"
)
"""Numeric head of a catalogue row. The textual tail is sliced at fixed widths afterwards."""

_CATALOGUE_NAME_WIDTH: Final[int] = 41
"""Width of the ``Stationsname`` field, measured from the file (not from the dash ruler,

which describes the *header* row and is narrower than the data columns it labels)."""

_CATALOGUE_STATE_WIDTH: Final[int] = 41
"""Width of the ``Bundesland`` field in the data rows."""

_SNOW_TEMPERATURE_C: Final[float] = 0.5
"""At or below this air temperature, precipitation is recorded as snow rather than rain."""

_WET_THRESHOLD_MM: Final[float] = 0.1
"""Precipitation sum over ten minutes that counts as "it is raining"."""

_STORM_WIND_MS: Final[float] = 17.2
"""Beaufort 8 (stürmischer Wind). Above it the condition is reported as ``storm``."""

_FOG_HUMIDITY_PERCENT: Final[float] = 98.0
"""Near-saturation at 2 m. The 10-minute products carry no visibility parameter."""

_CLOUD_HUMIDITY_PERCENT: Final[float] = 85.0
"""Humid but unsaturated air; reported as ``clouds`` rather than ``clear``."""


class DWDWeatherProvider(WeatherProvider):
    """Reads 10-minute observations from the DWD Climate Data Center (BUILD_SPEC §4).

    Each of the three layers walks its own ``live → cache → fixture`` chain — the catalogue
    and every station product independently — and the mode reported for the call as a whole is
    the *least live* of the layers that contributed. A result that mixes a cached catalogue
    with a fresh observation is therefore reported as ``cache``, never as ``live``.
    """

    name = "dwd_weather_observations"
    source = SourceSystem.dwd

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        http_client: AutoTwinHTTPClient | None = None,
        cache: FileCache | None = None,
        data_mode: DataMode | None = None,
        max_stations: int = _DEFAULT_MAX_STATIONS,
        products: Sequence[str] = tuple(PRODUCTS),
    ) -> None:
        """Configure the adapter.

        Args:
            settings: Configuration source; defaults to the process-wide settings.
            http_client: Shared HTTP client. One is created — and owned — if omitted.
            cache: On-disk cache; defaults to ``AUTOTWIN_DATA_DIR/cache``.
            data_mode: Override the configured fallback policy.
            max_stations: Upper bound on stations contacted per call, so that a 300-segment
                route cannot turn into 300 downloads.
            products: Which 10-minute products to read. Temperature alone is enough for the
                energy model; wind and precipitation refine the condition.

        Raises:
            ValueError: ``max_stations`` is not positive, or an unknown product was named.
        """
        if max_stations < 1:
            msg = f"max_stations must be at least 1, got {max_stations!r}"
            raise ValueError(msg)
        unknown = sorted(set(products).difference(PRODUCTS))
        if unknown:
            msg = f"unknown DWD product(s) {unknown}; known: {sorted(PRODUCTS)}"
            raise ValueError(msg)

        self._settings = settings or get_settings()
        self._data_mode = data_mode or self._settings.data_mode
        self._cache = cache or FileCache()
        self._max_stations = max_stations
        self._products = tuple(products)
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
        self._catalogue_memo: ProviderResult[list[WeatherStationRecord]] | None = None
        self._http = http_client
        self._owns_http = http_client is None

    async def aclose(self) -> None:
        """Release the HTTP client if this adapter created it."""
        if self._http is not None and self._owns_http:
            await self._http.aclose()
            self._http = None

    async def fetch_observations(
        self,
        points: Sequence[Coordinate],
        at: datetime | None = None,
    ) -> WeatherResult:
        """Fetch the observation nearest in space to each point, and in time to ``at``.

        Args:
            points: Locations to cover — typically route segment midpoints.
            at: Observation time (aware UTC). ``None`` means the most recent row.

        Returns:
            One record per distinct station covering the points, located *at the station* as
            the interface requires. Points with no reporting station in range are omitted.

        Raises:
            ProviderUnavailable: live mode was demanded and DWD was unreachable.
            InvalidSourceData: the catalogue or a product file changed shape.
            ConfigurationMissing: no catalogue at all — not live, not cached, not bundled.
        """
        catalogue = await self._catalogue()
        stations = self._nearest_stations(catalogue.data, points)
        if not stations:
            return ProviderResult(
                data=[],
                mode=catalogue.mode,
                source_url=catalogue.source_url,
                fetched_at=catalogue.fetched_at,
                warnings=[DWD_ATTRIBUTION, "no DWD station could be resolved for the request"],
            )

        modes = {catalogue.mode}
        warnings: list[str] = []
        results = await asyncio.gather(
            *(self._observation_for(station, at, warnings) for station in stations)
        )
        records: list[WeatherRecord] = []
        for record, mode in results:
            modes.add(mode)
            if record is not None:
                records.append(record)

        mode = _weakest_mode(modes)
        return ProviderResult(
            data=records,
            mode=mode,
            source_url=self._product_url("air_temperature", stations[0].dwd_station_id),
            fetched_at=utc_now() if mode is ProviderMode.live else catalogue.fetched_at,
            warnings=[DWD_ATTRIBUTION, *warnings],
        )

    async def _catalogue(self) -> ProviderResult[list[WeatherStationRecord]]:
        """The station catalogue, re-read from DWD at most once per :data:`_CATALOGUE_TTL_S`.

        Stations do not move, and the catalogue is 470 KB. Downloading it again for every route
        analysis would be both wasteful and impolite towards a free public-sector file server,
        so the answer — *including its mode and its original ``fetched_at``* — is memoised for
        the process. Re-using a memoised live catalogue does not make the next answer less
        live: the payload of :meth:`fetch_observations` is the observations, which are fetched
        fresh every call, and ``fetched_at`` on the catalogue result keeps stating its real age.
        """
        memo = self._catalogue_memo
        if memo is not None and (utc_now() - memo.fetched_at).total_seconds() < _CATALOGUE_TTL_S:
            return memo
        catalogue = await self.fetch_stations()
        self._catalogue_memo = catalogue
        return catalogue

    async def fetch_stations(self) -> ProviderResult[list[WeatherStationRecord]]:
        """Fetch the station catalogue — also what seeds the ``weather_stations`` table.

        Not part of :class:`~autotwin_core.providers.base.WeatherProvider`, because only the
        DWD models stations as first-class objects; the ingestion pipeline calls it directly.
        Always walks the full chain; :meth:`_catalogue` is the memoised internal reader.
        """
        url = self._catalogue_url()
        if self._data_mode is not DataMode.fixture:
            try:
                payload = await self._client().get_bytes(url)
            except ProviderError as error:
                if self._data_mode is DataMode.live:
                    raise
                _logger.warning(f"DWD station catalogue unreachable: {error}", provider=self.name)
            else:
                self._cache.set_bytes(_CACHE_NAMESPACE, url, payload)
                return ProviderResult.live(
                    self._parse_catalogue(payload, url, utc_now()),
                    source_url=url,
                    warnings=[DWD_ATTRIBUTION],
                )

            cached = self._cache.get_bytes(_CACHE_NAMESPACE, url, allow_stale=True)
            if cached is not None:
                age = self._cache.age_seconds(_CACHE_NAMESPACE, url) or 0.0
                fetched_at = _ago(age)
                return ProviderResult.cached(
                    self._parse_catalogue(cached, url, fetched_at),
                    source_url=url,
                    fetched_at=fetched_at,
                    warnings=[DWD_ATTRIBUTION],
                )

        try:
            path = resolve_fixture(DWD_STATIONS_FIXTURE)
        except FileNotFoundError as error:
            raise ConfigurationMissing(str(error)) from error
        fetched_at = _file_mtime(path)
        return ProviderResult.fixture(
            self._parse_catalogue(path.read_bytes(), url, fetched_at),
            source_url=url,
            fetched_at=fetched_at,
            warnings=[DWD_ATTRIBUTION, "Serving the bundled DWD station excerpt."],
        )

    # ------------------------------------------------------------------ internals

    def _parse_catalogue(
        self, payload: bytes, url: str, fetched_at: datetime
    ) -> list[WeatherStationRecord]:
        """Decode and parse the catalogue, raising if it yields nothing usable."""
        stations = parse_station_catalogue(
            payload.decode(_CATALOGUE_ENCODING, errors="replace"),
            source_url=url,
            fetched_at=fetched_at,
        )
        if not stations:
            msg = f"{url} yielded no parsable station rows"
            raise InvalidSourceData(msg)
        return stations

    def _nearest_stations(
        self,
        stations: Sequence[WeatherStationRecord],
        points: Sequence[Coordinate],
    ) -> list[WeatherStationRecord]:
        """Resolve the nearest station per point, de-duplicated and capped.

        Ordering is by the distance of the *closest* point served, so that when the cap bites
        it is the stations furthest from the route that are dropped.
        """
        if not stations:
            return []
        best: dict[str, tuple[float, WeatherStationRecord]] = {}
        for point in points:
            nearest = min(stations, key=lambda station: haversine_km(point, station.coordinate))
            distance = haversine_km(point, nearest.coordinate)
            current = best.get(nearest.dwd_station_id)
            if current is None or distance < current[0]:
                best[nearest.dwd_station_id] = (distance, nearest)
        ranked = sorted(best.values(), key=lambda entry: entry[0])
        return [station for _, station in ranked[: self._max_stations]]

    async def _observation_for(
        self,
        station: WeatherStationRecord,
        at: datetime | None,
        warnings: list[str],
    ) -> tuple[WeatherRecord | None, ProviderMode]:
        """Build one station's record by merging its products on a common timestamp."""
        modes: set[ProviderMode] = set()
        measurements: dict[str, dict[datetime, dict[str, str]]] = {}
        for product in self._products:
            rows, mode = await self._load_product(station.dwd_station_id, product, warnings)
            if rows is None:
                continue
            modes.add(mode)
            measurements[product] = rows

        primary = measurements.get("air_temperature") or _first_non_empty(measurements)
        if not primary:
            return None, _weakest_mode(modes)

        observed_at = _select_timestamp(primary, at)
        if observed_at is None:
            return None, _weakest_mode(modes)

        temperature = _measurement(measurements, "air_temperature", observed_at, "TT_10")
        humidity = _measurement(measurements, "air_temperature", observed_at, "RF_10")
        pressure = _measurement(measurements, "air_temperature", observed_at, "PP_10")
        wind = _measurement(measurements, "wind", observed_at, "FF_10")
        precipitation = _measurement(measurements, "precipitation", observed_at, "RWS_10")

        record = WeatherRecord(
            station_id=station.dwd_station_id,
            observed_at=observed_at,
            coordinate=station.coordinate,
            temperature_c=temperature,
            precipitation_mm=precipitation,
            wind_speed_ms=wind,
            humidity_percent=_clamp_percent(humidity),
            pressure_hpa=pressure if pressure is not None and pressure > 0.0 else None,
            condition=derive_condition(
                temperature_c=temperature,
                precipitation_mm=precipitation,
                wind_speed_ms=wind,
                humidity_percent=humidity,
            ),
            provenance=ProvenanceInfo(
                source=SourceSystem.dwd,
                source_identifier=station.dwd_station_id,
                source_url=self._product_url("air_temperature", station.dwd_station_id),
                source_timestamp=observed_at,
                data_origin=DataOrigin.official,
            ),
        )
        return record, _weakest_mode(modes)

    async def _load_product(
        self,
        station_id: str,
        product: str,
        warnings: list[str],
    ) -> tuple[dict[datetime, dict[str, str]] | None, ProviderMode]:
        """Load one station's product file, walking ``live → cache → fixture``.

        A station that simply does not report a product — station 01424 has no wind file and
        answers 404 — is not a failure. It yields ``None`` and the caller leaves those fields
        empty, which is the behaviour the interface asks for: a record with a temperature and
        nulls beats no record at all.
        """
        url = self._product_url(product, station_id)
        if self._data_mode is not DataMode.fixture:
            payload: bytes | None = None
            async with self._semaphore:
                try:
                    payload = await self._client().get_bytes(url)
                except ProviderError as error:
                    # Deliberately never re-raised, not even in live mode: a station that does
                    # not report a product answers 404, and that is a fact about the station
                    # rather than an outage. The catalogue fetch is where live mode fails loud.
                    warnings.append(f"{product} for station {station_id} unavailable: {error}")
            if payload is not None:
                self._cache.set_bytes(_CACHE_NAMESPACE, url, payload)
                return _rows_from_zip(payload, url), ProviderMode.live

            cached = self._cache.get_bytes(
                _CACHE_NAMESPACE, url, ttl_seconds=_OBSERVATION_TTL_S, allow_stale=True
            )
            if cached is not None:
                return _rows_from_zip(cached, url), ProviderMode.cache

        fixture_name = DWD_PRODUCT_FIXTURES.get(_station_number(station_id), {}).get(product)
        if fixture_name is None:
            return None, ProviderMode.fixture
        try:
            text = resolve_fixture(fixture_name).read_text(encoding=_CATALOGUE_ENCODING)
        except FileNotFoundError:
            return None, ProviderMode.fixture
        return _index_rows(parse_product_file(text)), ProviderMode.fixture

    def _catalogue_url(self) -> str:
        """Absolute URL of the 10-minute temperature station catalogue."""
        base = self._settings.dwd_base_url.rstrip("/")
        return f"{base}/{_OBSERVATIONS_ROOT}/air_temperature/now/{_STATION_CATALOGUE_FILE}"

    def _product_url(self, product: str, station_id: str) -> str:
        """Absolute URL of one station's ZIP for one product."""
        base = self._settings.dwd_base_url.rstrip("/")
        token = PRODUCTS[product]
        padded = f"{_station_number(station_id):05d}"
        return f"{base}/{_OBSERVATIONS_ROOT}/{product}/now/10minutenwerte_{token}_{padded}_now.zip"

    def _client(self) -> AutoTwinHTTPClient:
        """Return the HTTP client, creating the owned one on first use."""
        if self._http is None:
            self._http = AutoTwinHTTPClient()
            self._owns_http = True
        return self._http

    @property
    def max_stations(self) -> int:
        """Upper bound on stations contacted per call."""
        return self._max_stations


# --------------------------------------------------------------------------- parsing


def parse_station_catalogue(
    text: str,
    *,
    source_url: str | None,
    fetched_at: datetime,
) -> list[WeatherStationRecord]:
    """Parse ``zehn_now_*_Beschreibung_Stationen.txt`` into station records.

    Two-stage on purpose. The numeric head (id, validity dates, height, latitude, longitude)
    is matched with a regex, which is immune to the column widths drifting between DWD
    releases. Only the textual tail — name and Bundesland, the two fields that can contain
    spaces — is sliced at fixed widths, with a token-walk fallback for the day those widths
    change. Splitting the whole line on whitespace, the obvious approach, silently truncates
    ``Seebach (Nationalpark Schwarzwald)`` to ``Seebach``.
    """
    stations: list[WeatherStationRecord] = []
    for line in text.splitlines()[_CATALOGUE_HEADER_ROWS:]:
        if not line.strip():
            continue
        match = _CATALOGUE_ROW_PATTERN.match(line)
        if match is None:
            _logger.warning(f"Unparsable DWD catalogue row: {line[:60]!r}")
            continue
        raw_id, valid_from, valid_to, elevation, latitude, longitude, tail = match.groups()
        name, state = _split_catalogue_tail(tail)
        if not name:
            continue
        stations.append(
            WeatherStationRecord(
                dwd_station_id=f"{int(raw_id):05d}",
                name=name,
                coordinate=Coordinate(latitude=float(latitude), longitude=float(longitude)),
                elevation_m=float(elevation),
                bundesland=Bundesland.from_name(state) if state else None,
                valid_from=_parse_compact_date(valid_from),
                valid_to=_parse_compact_date(valid_to),
                provenance=ProvenanceInfo(
                    source=SourceSystem.dwd,
                    source_identifier=f"{int(raw_id):05d}",
                    source_url=source_url,
                    data_origin=DataOrigin.official,
                    ingested_at=fetched_at,
                ),
            )
        )
    return stations


def _split_catalogue_tail(tail: str) -> tuple[str, str]:
    """Split the fixed-width ``Stationsname``/``Bundesland``/``Abgabe`` tail.

    The fallback matters: all sixteen German state names are single tokens, so when the fixed
    widths stop lining up the last-but-one token is still the Bundesland and everything before
    it is the name. It is only a fallback because a station legitimately named ``Sachsen b.
    Ansbach`` would defeat it, and the fixed-width read would not.
    """
    name = tail[:_CATALOGUE_NAME_WIDTH].strip()
    state = tail[_CATALOGUE_NAME_WIDTH : _CATALOGUE_NAME_WIDTH + _CATALOGUE_STATE_WIDTH].strip()
    if name and Bundesland.from_name(state) is not None:
        return name, state

    tokens = tail.split()
    for index in range(len(tokens) - 1, -1, -1):
        if Bundesland.from_name(tokens[index]) is not None:
            return " ".join(tokens[:index]).strip(), tokens[index]
    return name or tail.strip(), state


def parse_product_file(text: str) -> list[dict[str, str]]:
    """Parse a ``produkt_*.txt`` into stripped column → value dictionaries.

    The ``eor`` sentinel column is dropped, every field is stripped of its space padding, and
    values are left as text: converting ``-999`` to ``None`` is the caller's decision because
    only the caller knows which columns are numeric.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    header = [column.strip() for column in lines[0].split(";")]
    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        values = [value.strip() for value in line.split(";")]
        if len(values) < len(header):
            continue
        row = {
            column: value
            for column, value in zip(header, values, strict=False)
            if column and column != _END_OF_ROW
        }
        rows.append(row)
    return rows


def derive_condition(
    *,
    temperature_c: float | None,
    precipitation_mm: float | None,
    wind_speed_ms: float | None,
    humidity_percent: float | None,
) -> WeatherCondition:
    """Derive a coarse :class:`WeatherCondition` from the measured parameters.

    The 10-minute products carry no cloud-cover and no visibility parameter, so anything
    beyond "is it wet, is it freezing, is it blowing" is inference. The rules are therefore
    few, ordered, and stated here rather than buried in a chain of ``if``\\ s:

    ======  ==========================================================  ==========
    order   condition                                                   result
    ======  ==========================================================  ==========
    1       ``FF_10 >= 17.2 m/s`` (Beaufort 8, stürmischer Wind)         ``storm``
    2       ``RWS_10 >= 0.1 mm`` and ``TT_10 <= 0.5 °C``                 ``snow``
    3       ``RWS_10 >= 0.1 mm``                                         ``rain``
    4       ``RF_10 >= 98 %`` and dry (near-saturation at 2 m)           ``fog``
    5       ``RF_10 >= 85 %`` and dry                                    ``clouds``
    6       dry, ``RF_10 < 85 %``                                        ``clear``
    7       nothing measured                                             ``unknown``
    ======  ==========================================================  ==========

    Rules 4-6 are a humidity proxy, not an observation of the sky, and are documented as such
    wherever the condition is rendered. ``storm`` wins over precipitation because a Sturm is
    the thing that changes a driver's plan.
    """
    if wind_speed_ms is not None and wind_speed_ms >= _STORM_WIND_MS:
        return WeatherCondition.storm
    if precipitation_mm is not None and precipitation_mm >= _WET_THRESHOLD_MM:
        if temperature_c is not None and temperature_c <= _SNOW_TEMPERATURE_C:
            return WeatherCondition.snow
        return WeatherCondition.rain
    if humidity_percent is not None:
        if humidity_percent >= _FOG_HUMIDITY_PERCENT:
            return WeatherCondition.fog
        if humidity_percent >= _CLOUD_HUMIDITY_PERCENT:
            return WeatherCondition.clouds
        return WeatherCondition.clear
    if temperature_c is not None or precipitation_mm is not None:
        return WeatherCondition.clear
    return WeatherCondition.unknown


# --------------------------------------------------------------------------- helpers


def _rows_from_zip(payload: bytes, url: str) -> dict[datetime, dict[str, str]]:
    """Extract the single ``produkt_*.txt`` from a station ZIP and index it by timestamp."""
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = [name for name in archive.namelist() if name.startswith("produkt_")]
            if not names:
                msg = f"{url} contains no produkt_*.txt (members: {archive.namelist()})"
                raise InvalidSourceData(msg)
            raw = archive.read(names[0])
    except zipfile.BadZipFile as error:
        msg = f"{url} is not a readable ZIP archive: {error}"
        raise InvalidSourceData(msg) from error
    return _index_rows(parse_product_file(raw.decode(_CATALOGUE_ENCODING, errors="replace")))


def _index_rows(rows: Iterable[dict[str, str]]) -> dict[datetime, dict[str, str]]:
    """Index parsed rows by their ``MESS_DATUM``, dropping rows without a readable one."""
    indexed: dict[datetime, dict[str, str]] = {}
    for row in rows:
        moment = _parse_mess_datum(row.get("MESS_DATUM", ""))
        if moment is not None:
            indexed[moment] = row
    return indexed


def _parse_mess_datum(raw: str) -> datetime | None:
    """Parse ``YYYYMMDDHHMM`` (10-minute) or ``YYYYMMDDHH`` (hourly) as aware UTC.

    The width is measured rather than assumed because DWD uses both across products, and a
    ``strptime`` with the wrong pattern fails loudly on one and *succeeds wrongly* on neither
    — but a hand-rolled slice would silently read the hour as part of the day.
    """
    token = raw.strip()
    fmt = {12: "%Y%m%d%H%M", 10: "%Y%m%d%H"}.get(len(token))
    if fmt is None:
        return None
    try:
        return datetime.strptime(token, fmt).replace(tzinfo=UTC)
    except ValueError:
        return None


def _parse_compact_date(raw: str) -> date | None:
    """Parse a ``YYYYMMDD`` validity bound from the station catalogue."""
    try:
        return datetime.strptime(raw.strip(), "%Y%m%d").date()
    except ValueError:
        return None


def _select_timestamp(
    rows: dict[datetime, dict[str, str]],
    at: datetime | None,
) -> datetime | None:
    """Pick the observation timestamp: the latest row, or the last one at or before ``at``."""
    if not rows:
        return None
    if at is None:
        return max(rows)
    target = at if at.tzinfo else at.replace(tzinfo=UTC)
    earlier = [moment for moment in rows if moment <= target]
    return max(earlier) if earlier else min(rows)


def _measurement(
    measurements: dict[str, dict[datetime, dict[str, str]]],
    product: str,
    observed_at: datetime,
    column: str,
) -> float | None:
    """Read one numeric column at one timestamp, mapping DWD's ``-999`` onto ``None``."""
    row = measurements.get(product, {}).get(observed_at)
    if row is None:
        return None
    return _to_float(row.get(column))


def _to_float(raw: str | None) -> float | None:
    """Parse a measurement; ``-999`` and unparsable text both mean *not measured*."""
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return None if value <= MISSING_VALUE else value


def _clamp_percent(value: float | None) -> float | None:
    """Keep relative humidity inside 0-100, which the record model enforces.

    DWD occasionally reports 100.1 % for a saturated sensor; that is a calibration artefact,
    not a reason to drop an otherwise good observation.
    """
    if value is None:
        return None
    return min(max(value, 0.0), 100.0)


def _station_number(station_id: str) -> int:
    """Normalise a station id to an int, whether it arrived padded, zero-filled or bare."""
    return int(station_id.strip())


def _first_non_empty(
    measurements: dict[str, dict[datetime, dict[str, str]]],
) -> dict[datetime, dict[str, str]]:
    """First product that actually returned rows — the timestamp grid comes from it."""
    for rows in measurements.values():
        if rows:
            return rows
    return {}


def _weakest_mode(modes: Iterable[ProviderMode]) -> ProviderMode:
    """Least-live mode of the layers that contributed, so the answer cannot overstate itself."""
    ranked = {ProviderMode.live: 0, ProviderMode.cache: 1, ProviderMode.fixture: 2}
    collected = list(modes)
    if not collected:
        return ProviderMode.fixture
    return max(collected, key=lambda mode: ranked[mode])


def _ago(seconds: float) -> datetime:
    """The moment ``seconds`` ago, as aware UTC — used to date a cache entry honestly."""
    return utc_now() - timedelta(seconds=seconds)


def _file_mtime(path: Path) -> datetime:
    """Modification time of a file as an aware UTC datetime."""
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
