"""Bundesnetzagentur Ladesäulenregister adapter — Germany's charging-infrastructure register.

The register is the only complete, official list of publicly accessible charging points in
Germany. It is published as one large CSV, not as an API, and almost every property of that
file is a trap for a naive reader. What follows is the list of facts this module is built on;
each was verified against the real bytes on 2026-09-14 and each has a matching constant below.

================================  ============================================================
property                          reality
================================  ============================================================
URL                               ``Ladesaeulenregister_BNetzA_<YYYY-MM-01>.csv`` on
                                  ``data.bundesnetzagentur.de``. **The filename rotates every
                                  month and the previous month is deleted**, so there is no
                                  stable "latest" link to configure once and forget.
encoding                          UTF-8 **with BOM** → ``utf-8-sig``. Reading it as plain
                                  ``utf-8`` welds ``\\ufeff`` onto the first column name and
                                  every lookup then misses in silence.
delimiter / line ending           ``;`` / CRLF
preamble                          **10 lines** of notice text and a merged-cell group banner;
                                  the real header is line 11. One of those lines carries
                                  ``Letzte Aktualisierung vom: DD.MM.YYYY`` — the edition
                                  date, which this adapter lifts into ``source_timestamp``.
columns                           **47**: 23 site columns + 6 groups of 4 connector columns.
decimal separator                 comma (``48,442398``)
date format                       ``DD.MM.YYYY``
quoting                           ``"`` around fields that *contain the delimiter*
                                  (``"RFID-Karte;Onlinezahlungsverfahren"``, ``"11; 3,7"``).
                                  ``line.split(";")`` yields a different column count per row,
                                  so a real CSV parser is mandatory.
================================  ============================================================

Two further facts are not in the published documentation and were measured from the file:

* ``Steckertypen{n}`` is itself multi-valued (``"AC Typ 2 Steckdose; AC Schuko"``) and pairs
  positionally with ``Nennleistung Stecker{n}`` (``"22; 22"``). One "Ladepunkt" column group
  can therefore describe several physical connectors, which is why the number of
  :class:`~autotwin_contracts.ChargingPointRecord` rows may exceed ``Anzahl Ladepunkte``.
* ``Status`` is ``In Betrieb`` or ``In Wartung``. Both are kept; a site under maintenance is
  still infrastructure, and dropping it would understate the network.

**Memory.** The file is ~55 MB and ~117 000 rows. It is written to disk and parsed row by row
from there with :func:`iter_records`, so no 55 MB *string* is ever materialised — a decoded
copy would be well over 100 MB of Python ``str``. The download itself passes through memory
once, because the shared :class:`~autotwin_core.providers.http.AutoTwinHTTPClient` exposes a
buffered ``GET`` rather than a streaming one; that is a deliberate, bounded trade for having a
single retry/timeout/error-translation policy across every adapter.
"""

from __future__ import annotations

import csv
import hashlib
import os
import re
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final
from uuid import uuid4

from autotwin_contracts import (
    Bundesland,
    ChargingCategory,
    ChargingPointRecord,
    ChargingStationRecord,
    ConnectorType,
    Coordinate,
    CurrentType,
    DataOrigin,
    ProvenanceInfo,
    SourceSystem,
    utc_now,
)
from autotwin_core.config import DataMode, Settings, get_settings
from autotwin_core.errors import ConfigurationMissing, InvalidSourceData, ProviderError
from autotwin_core.logging import get_logger
from autotwin_core.providers import (
    AutoTwinHTTPClient,
    ChargingInfrastructureProvider,
    FileCache,
    ProviderResult,
    StationsResult,
)
from autotwin_ingestion.fixtures import BNETZA_CSV_FIXTURE, resolve_fixture

__all__ = [
    "BNETZA_LANDING_PAGE_URL",
    "CONNECTOR_GROUPS",
    "EXPECTED_COLUMN_COUNT",
    "SITE_COLUMNS",
    "BundesnetzagenturChargingProvider",
    "iter_records",
    "read_edition_date",
]

_logger = get_logger(__name__)

BNETZA_LANDING_PAGE_URL: Final[str] = (
    "https://www.bundesnetzagentur.de/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/"
    "Ladesaeulenkarte/start.html"
)
"""Page that carries the current absolute CSV link, since the filename rotates monthly."""

_DOWNLOAD_URL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"https?://[^\s\"'<>]*?Ladesaeulenregister_BNetzA_\d{4}-\d{2}-\d{2}\.csv"
)
"""Absolute CSV link as it appears verbatim in the landing-page HTML."""

_URL_EDITION_PATTERN: Final[re.Pattern[str]] = re.compile(r"_(\d{4})-(\d{2})-(\d{2})\.csv\b")
"""Edition date embedded in the filename, used when the preamble cannot be read."""

_EDITION_TEMPLATE: Final[str] = (
    "https://data.bundesnetzagentur.de/Bundesnetzagentur/DE/Fachthemen/ElektrizitaetundGas/"
    "E-Mobilitaet/Ladesaeulenregister_BNetzA_{year:04d}-{month:02d}-01.csv"
)
"""Date-templated guess, used when the landing page cannot be reached or has been restyled."""

_PREAMBLE_ROWS: Final[int] = 10
"""Notice lines before the header. The real header is row 11 of the file."""

EXPECTED_COLUMN_COUNT: Final[int] = 47
"""23 site columns, plus 6 connector groups of 4 columns each."""

SITE_COLUMNS: Final[tuple[str, ...]] = (
    "Ladeeinrichtungs-ID",
    "Betreiber",
    "Anzeigename (Karte)",
    "Status",
    "Art der Ladeeinrichtung",
    "Anzahl Ladepunkte",
    "Nennleistung Ladeeinrichtung [kW]",
    "Inbetriebnahmedatum",
    "Straße",
    "Hausnummer",
    "Adresszusatz",
    "Postleitzahl",
    "Ort",
    "Kreis/kreisfreie Stadt",
    "Bundesland",
    "Breitengrad",
    "Längengrad",
    "Standortbezeichnung",
    "Informationen zum Parkraum",
    "Bezahlsysteme",
    "Öffnungszeiten",
    "Öffnungszeiten: Wochentage",
    "Öffnungszeiten: Tageszeiten",
)
"""The 23 site-level column names, verbatim from the file — not from the documentation."""

CONNECTOR_GROUPS: Final[int] = 6
"""``Steckertypen1..6`` — the register allows six connector groups per Ladeeinrichtung."""

_REQUIRED_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "Ladeeinrichtungs-ID",
        "Betreiber",
        "Art der Ladeeinrichtung",
        "Anzahl Ladepunkte",
        "Nennleistung Ladeeinrichtung [kW]",
        "Straße",
        "Postleitzahl",
        "Ort",
        "Bundesland",
        "Breitengrad",
        "Längengrad",
        "Steckertypen1",
        "Nennleistung Stecker1",
    }
)
"""Columns without which the file cannot be turned into records at all."""

_EDITION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"Letzte\s+Aktualisierung\s+vom:\s*(\d{2})\.(\d{2})\.(\d{4})"
)
"""``Letzte Aktualisierung vom: 01.09.2026`` in the preamble — the edition date of the file."""

_CACHE_NAMESPACE: Final[str] = "bnetza"
"""Cache namespace; also the sub-directory of ``data/raw`` the download is archived in."""

_CACHE_POINTER_KEY: Final[str] = "ladesaeulenregister"
"""Stable cache key.

Deliberately *not* the URL: the URL changes every month, and keying on it would throw away a
perfectly good copy of last month's register at the exact moment the new one cannot be
reached. The cache entry is a small JSON pointer at the archived file in ``data/raw``, so the
55 MB payload exists once on disk rather than twice.
"""

_RAW_FILENAME_TEMPLATE: Final[str] = "Ladesaeulenregister_BNetzA_{edition}_{digest}.csv"
"""Archive filename: edition date plus a content digest, so re-downloads are self-evident."""

_EXTERNAL_ID_PREFIX: Final[str] = "bnetza"
"""Namespace of the natural key written to ``charging_stations.external_id``."""

_SYNTHETIC_ID_LENGTH: Final[int] = 32
"""Hex characters of the synthesised id — 128 bits, collision-free for 10^5 rows."""

_ARTS_OF_CHARGING_DEVICE: Final[frozenset[str]] = frozenset(
    {"Normalladeeinrichtung", "Schnellladeeinrichtung"}
)
"""The two values ``Art der Ladeeinrichtung`` takes. Anything else is reported as a warning."""

_RAW_KEPT_COLUMNS: Final[tuple[str, ...]] = (
    "Ladeeinrichtungs-ID",
    "Betreiber",
    "Anzeigename (Karte)",
    "Status",
    "Art der Ladeeinrichtung",
    "Anzahl Ladepunkte",
    "Nennleistung Ladeeinrichtung [kW]",
    "Kreis/kreisfreie Stadt",
    "Standortbezeichnung",
    "Informationen zum Parkraum",
    "Bezahlsysteme",
    "Öffnungszeiten",
)
"""What is kept in ``ChargingStationRecord.raw``.

The 96-character ``Public Key{n}`` hex blobs and the three opening-hours columns are dropped:
they are ~40 % of the file's bytes, JSONB-compress badly and answer no question the API asks.
"""


class BundesnetzagenturChargingProvider(ChargingInfrastructureProvider):
    """Downloads and parses the Ladesäulenregister (BUILD_SPEC §4).

    The fallback chain has one extra rung at the front compared with the other adapters,
    because *finding* the file is itself unreliable::

        configured URL  →  landing-page scrape  →  date-templated guess
                        →  on-disk cache (data/raw)  →  bundled fixture

    The first three rungs are all "live" — whichever one answers, the result's mode is
    ``live``. Only the last two downgrade it.

    Note on the shipped default: ``AUTOTWIN_BNETZA_DOWNLOAD_URL`` defaults to the widely-cited
    legacy ``.../Ladesaeulenregister.csv`` path, which **404s as of 2026-09**. It is still
    tried first so that an operator who has configured a working URL is always obeyed; a 404
    costs one fast request and the chain then moves on to the landing page. Configure the
    setting to an empty string to skip that probe.
    """

    name = "bnetza_charging_stations"
    source = SourceSystem.bundesnetzagentur

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        http_client: AutoTwinHTTPClient | None = None,
        cache: FileCache | None = None,
        data_mode: DataMode | None = None,
        landing_page_url: str = BNETZA_LANDING_PAGE_URL,
        limit: int | None = None,
    ) -> None:
        """Configure the adapter.

        Args:
            settings: Configuration to read URLs and directories from; defaults to the
                process-wide settings.
            http_client: Shared HTTP client. One is created — and owned — if omitted.
            cache: On-disk cache. Defaults to a cache rooted at ``AUTOTWIN_DATA_DIR/cache``.
            data_mode: Override the configured fallback policy, for a one-off CLI run.
            landing_page_url: Page to scrape the current CSV link from.
            limit: Default cap on the number of records parsed; ``fetch_stations`` can
                override it per call. Tests use it to keep fixtures fast.
        """
        self._settings = settings or get_settings()
        self._data_mode = data_mode or self._settings.data_mode
        self._cache = cache or FileCache()
        self._landing_page_url = landing_page_url
        self._limit = limit
        self._http = http_client
        self._owns_http = http_client is None

    async def aclose(self) -> None:
        """Release the HTTP client if this adapter created it."""
        if self._http is not None and self._owns_http:
            await self._http.aclose()
            self._http = None

    async def fetch_stations(self, *, limit: int | None = None) -> StationsResult:
        """Fetch every charging site the register lists.

        Args:
            limit: Stop after this many accepted records. ``None`` uses the constructor
                default, which is itself ``None`` — the whole file.

        Returns:
            Every parsed site, with provenance and a stable ``external_id``. ``mode`` is
            ``live`` only when the CSV was downloaded during this call.

        Raises:
            ProviderUnavailable: live mode was requested and the download failed.
            InvalidSourceData: the file was retrieved but its columns no longer match.
            ConfigurationMissing: no source at all — no URL reachable, no cache, no fixture.
        """
        effective_limit = self._limit if limit is None else limit
        if self._data_mode is DataMode.fixture:
            return self._read_fixture(effective_limit)

        try:
            return await self._fetch_live(effective_limit)
        except ProviderError as error:
            if self._data_mode is DataMode.live:
                raise
            _logger.warning(
                f"Ladesäulenregister download failed ({error}); falling back to cache",
                provider=self.name,
            )
            reason = str(error)

        cached = self._read_cache(effective_limit)
        if cached is not None:
            return cached.with_warning(f"Live source unavailable: {reason}")
        return self._read_fixture(effective_limit).with_warning(
            f"Live source unavailable and no cached download: {reason}"
        )

    # ------------------------------------------------------------------ live download

    async def _fetch_live(self, limit: int | None) -> StationsResult:
        """Download the register, archive it, and parse it from disk.

        Every candidate URL is tried in order; a dead one produces a warning on the result
        rather than an exception, because the monthly rotation means a 404 is the *expected*
        outcome for at least one candidate most of the time.
        """
        warnings: list[str] = []
        payload: bytes | None = None
        source_url: str | None = None

        for candidate in await self._candidate_urls(warnings):
            try:
                response = await self._client().get(candidate)
            except ProviderError as error:
                warnings.append(f"{candidate} did not answer: {error}")
                continue
            payload = response.content
            source_url = candidate
            break

        if payload is None or source_url is None:
            msg = "no Ladesäulenregister URL answered; tried landing page and date templates"
            raise ConfigurationMissing(msg, details={"attempts": warnings})

        digest = hashlib.sha256(payload).hexdigest()
        edition = _edition_from_url(source_url)
        archived = self._archive(payload, edition=edition, digest=digest)
        fetched_at = utc_now()
        self._cache.set_json(
            _CACHE_NAMESPACE,
            _CACHE_POINTER_KEY,
            {
                "path": str(archived),
                "source_url": source_url,
                "sha256": digest,
                "bytes": len(payload),
                "downloaded_at": fetched_at.isoformat(),
            },
        )
        _logger.info(
            "Ladesäulenregister downloaded",
            provider=self.name,
            url=source_url,
            bytes=len(payload),
            sha256=digest[:16],
            path=str(archived),
        )
        records, parse_warnings = self._parse(archived, source_url, fetched_at, limit)
        return ProviderResult.live(
            records,
            source_url=source_url,
            fetched_at=fetched_at,
            warnings=[*warnings, *parse_warnings],
        )

    async def _candidate_urls(self, warnings: list[str]) -> list[str]:
        """Build the ordered list of URLs to try: configured, scraped, then templated."""
        candidates: list[str] = []
        configured = self._settings.bnetza_download_url.strip()
        if configured:
            candidates.append(configured)

        discovered = await self._discover_download_url()
        if discovered is None:
            warnings.append(f"no CSV link found on {self._landing_page_url}")
        elif discovered not in candidates:
            candidates.append(discovered)

        for templated in _templated_urls(utc_now().date()):
            if templated not in candidates:
                candidates.append(templated)
        return candidates

    async def _discover_download_url(self) -> str | None:
        """Scrape the landing page for the current absolute CSV link.

        The link appears as plain text in the HTML, so one regex is enough and no HTML parser
        dependency is warranted. ``.xlsx`` siblings are excluded by the pattern itself.
        """
        try:
            html = await self._client().get_text(self._landing_page_url)
        except ProviderError as error:
            _logger.warning(f"Landing page unreachable: {error}", provider=self.name)
            return None
        match = _DOWNLOAD_URL_PATTERN.search(html)
        return match.group(0) if match else None

    def _archive(self, payload: bytes, *, edition: date | None, digest: str) -> Path:
        """Write the download into ``data/raw`` so an ingestion run can cite a real file.

        Written through a temporary file and an atomic replace, for the same reason the
        provider cache does it: a half-written 55 MB CSV that looks complete is worse than no
        file at all. The digest goes in the filename, so two editions with identical content
        collapse onto one archive entry instead of filling the disk.
        """
        # Resolved, because the path is written into the cache pointer and read back by a
        # later process whose working directory is not this one — an Airflow worker, say.
        directory = Path(self._settings.raw_dir).resolve() / _CACHE_NAMESPACE
        directory.mkdir(parents=True, exist_ok=True)
        label = edition.isoformat() if edition else "unknown"
        target = directory / _RAW_FILENAME_TEMPLATE.format(edition=label, digest=digest[:12])
        temporary = directory / f".{target.name}.{uuid4().hex[:8]}.tmp"
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
        return target

    # ------------------------------------------------------------------ cache & fixture

    def _read_cache(self, limit: int | None) -> StationsResult | None:
        """Serve the archived download, or ``None`` when there is not one to serve."""
        pointer = self._cache.get_json(_CACHE_NAMESPACE, _CACHE_POINTER_KEY, allow_stale=True)
        if not isinstance(pointer, dict):
            return None
        path = Path(str(pointer.get("path", "")))
        if not path.is_file():
            _logger.warning(f"Cached Ladesäulenregister {path} has disappeared", provider=self.name)
            return None
        downloaded_at = _parse_iso(pointer.get("downloaded_at")) or utc_now()
        source_url = pointer.get("source_url")
        records, warnings = self._parse(
            path, str(source_url) if source_url else None, downloaded_at, limit
        )
        return ProviderResult.cached(
            records,
            source_url=str(source_url) if source_url else None,
            fetched_at=downloaded_at,
            warnings=warnings,
        )

    def _read_fixture(self, limit: int | None) -> StationsResult:
        """Serve the bundled 200-row excerpt — offline development, CI, or a total outage."""
        try:
            path = resolve_fixture(BNETZA_CSV_FIXTURE)
        except FileNotFoundError as error:
            raise ConfigurationMissing(str(error)) from error
        fetched_at = _file_mtime(path)
        records, warnings = self._parse(path, None, fetched_at, limit)
        return ProviderResult.fixture(
            records,
            source_url=BNETZA_LANDING_PAGE_URL,
            fetched_at=fetched_at,
            warnings=[
                "Serving the bundled Ladesäulenregister excerpt — not the full register.",
                *warnings,
            ],
        )

    def _parse(
        self,
        path: Path,
        source_url: str | None,
        fetched_at: datetime,
        limit: int | None,
    ) -> tuple[list[ChargingStationRecord], list[str]]:
        """Parse ``path`` into records, collecting a warning for every unusable row."""
        skipped: list[str] = []
        records = list(
            iter_records(
                path,
                source_url=source_url,
                fetched_at=fetched_at,
                limit=limit,
                on_skip=skipped.append,
            )
        )
        warnings: list[str] = []
        if skipped:
            warnings.append(
                f"{len(skipped)} row(s) could not be parsed and were dropped; first: {skipped[0]}"
            )
        return records, warnings

    def _client(self) -> AutoTwinHTTPClient:
        """Return the HTTP client, creating the owned one on first use."""
        if self._http is None:
            self._http = AutoTwinHTTPClient()
            self._owns_http = True
        return self._http


# --------------------------------------------------------------------------- parsing


def read_edition_date(path: Path) -> date | None:
    """Read ``Letzte Aktualisierung vom: DD.MM.YYYY`` out of the ten-line preamble.

    This is the only statement the file makes about *when* it is valid, and it becomes
    ``provenance.source_timestamp`` — without it every station would claim to be as fresh as
    the moment AutoTwin happened to download it.
    """
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for index, line in enumerate(handle):
            if index >= _PREAMBLE_ROWS:
                return None
            match = _EDITION_PATTERN.search(line)
            if match:
                day, month, year = (int(part) for part in match.groups())
                try:
                    return date(year, month, day)
                except ValueError:  # pragma: no cover - an impossible date in the preamble
                    return None
    return None


def iter_records(
    path: Path,
    *,
    source_url: str | None,
    fetched_at: datetime,
    limit: int | None = None,
    on_skip: Callable[[str], None] | None = None,
) -> Iterator[ChargingStationRecord]:
    """Stream :class:`ChargingStationRecord` objects out of a Ladesäulenregister CSV.

    A generator rather than a list-returning function so that a pipeline can upsert in
    batches without ever holding 117 000 Pydantic models at once; ``fetch_stations`` opts into
    that cost only because its interface (BUILD_SPEC §4) promises a list.

    Args:
        path: The CSV on disk — a fresh download, an archived one, or the fixture.
        source_url: URL the file came from, written into every record's provenance.
        fetched_at: When the bytes were obtained; ``provenance.ingested_at``.
        limit: Stop after this many records.
        on_skip: Optional callable receiving one message per unusable row. Rows are skipped,
            never guessed at: a station without coordinates cannot be placed on a map, and
            inventing one would break the honesty rule of BUILD_SPEC §0.2.

    Raises:
        InvalidSourceData: the preamble, the header or the column set no longer match.
    """
    edition = read_edition_date(path)
    source_timestamp = (
        datetime(edition.year, edition.month, edition.day, tzinfo=UTC) if edition else None
    )
    if source_timestamp is None and source_url:
        url_edition = _edition_from_url(source_url)
        if url_edition:
            source_timestamp = datetime(
                url_edition.year, url_edition.month, url_edition.day, tzinfo=UTC
            )

    emitted = 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, delimiter=";")
        header = _read_header(reader, path)
        index_of = {column: position for position, column in enumerate(header)}
        for row_number, values in enumerate(reader, start=_PREAMBLE_ROWS + 2):
            if not any(value.strip() for value in values):
                continue
            if len(values) < len(header):
                _report(on_skip, f"row {row_number}: {len(values)} of {len(header)} columns")
                continue
            try:
                record = _build_record(
                    values,
                    index_of,
                    source_url=source_url,
                    source_timestamp=source_timestamp,
                    fetched_at=fetched_at,
                )
            except (ValueError, TypeError) as error:
                _report(on_skip, f"row {row_number}: {error}")
                continue
            # Guard BEFORE the yield: checking afterwards makes ``limit=0`` emit one record,
            # and callers use limit=0 to mean "parse nothing, just validate the header".
            if limit is not None and emitted >= limit:
                return
            yield record
            emitted += 1


def _read_header(reader: Iterator[list[str]], path: Path) -> list[str]:
    """Skip the ten preamble rows and validate the header row.

    Validation is by *name*, not by position: the register has reshuffled columns between
    editions before, and a name-based check that tolerates reordering while rejecting a
    missing column is the one that fails for the right reason.
    """
    header: list[str] | None = None
    for index, values in enumerate(reader):
        if index < _PREAMBLE_ROWS:
            continue
        header = [value.strip() for value in values]
        break
    if header is None:
        msg = f"{path.name} has no header row after {_PREAMBLE_ROWS} preamble lines"
        raise InvalidSourceData(msg)
    missing = sorted(_REQUIRED_COLUMNS.difference(header))
    if missing:
        msg = (
            f"{path.name} is missing required column(s) {missing}; "
            f"the Ladesäulenregister layout has changed"
        )
        raise InvalidSourceData(msg, details={"missing": missing, "found": header[:5]})
    if len(header) != EXPECTED_COLUMN_COUNT:
        _logger.warning(
            f"Ladesäulenregister has {len(header)} columns, expected {EXPECTED_COLUMN_COUNT}",
            file=path.name,
        )
    # Drift short of a missing required column is worth a log line but not a failure: the
    # register has renamed optional columns between editions before (it fixed the long-running
    # "Art der Ladeeinrichung" typo), and an ingestion run that still produces good rows should
    # say so rather than stop.
    added = sorted(set(header[: len(SITE_COLUMNS)]).difference(SITE_COLUMNS))
    dropped = sorted(set(SITE_COLUMNS).difference(header))
    if added or dropped:
        _logger.warning(
            f"Ladesäulenregister site columns drifted (added={added}, dropped={dropped})",
            file=path.name,
        )
    return header


def _build_record(
    values: Sequence[str],
    index_of: dict[str, int],
    *,
    source_url: str | None,
    source_timestamp: datetime | None,
    fetched_at: datetime,
) -> ChargingStationRecord:
    """Turn one CSV row into a record, raising ``ValueError`` for rows that cannot be used."""

    def field(column: str) -> str:
        position = index_of.get(column)
        return values[position].strip() if position is not None else ""

    latitude = _parse_decimal(field("Breitengrad"))
    longitude = _parse_decimal(field("Längengrad"))
    if latitude is None or longitude is None:
        msg = f"unusable coordinates ({field('Breitengrad')!r}, {field('Längengrad')!r})"
        raise ValueError(msg)

    points = _parse_points(field)
    site_power = _parse_decimal(field("Nennleistung Ladeeinrichtung [kW]"))
    connector_powers = [point.power_kw for point in points if point.power_kw is not None]
    max_power_kw = max(connector_powers) if connector_powers else site_power

    source_id = field("Ladeeinrichtungs-ID") or None
    external_id = (
        f"{_EXTERNAL_ID_PREFIX}:{source_id}"
        if source_id
        else f"{_EXTERNAL_ID_PREFIX}:{_synthetic_id(field)}"
    )

    art = field("Art der Ladeeinrichtung")
    if art and art not in _ARTS_OF_CHARGING_DEVICE:
        _logger.warning(f"Unknown 'Art der Ladeeinrichtung' value {art!r}")

    return ChargingStationRecord(
        external_id=external_id,
        operator=field("Betreiber") or None,
        street=field("Straße") or None,
        house_number=field("Hausnummer") or None,
        postal_code=field("Postleitzahl") or None,
        city=field("Ort") or None,
        bundesland=_parse_bundesland(field("Bundesland")),
        coordinate=Coordinate(latitude=latitude, longitude=longitude),
        commissioned_on=_parse_german_date(field("Inbetriebnahmedatum")),
        charging_points_count=_parse_point_count(field("Anzahl Ladepunkte"), len(points)),
        max_power_kw=max_power_kw,
        total_power_kw=site_power,
        charging_category=ChargingCategory.from_power(max_power_kw),
        points=tuple(points),
        raw={column: field(column) for column in _RAW_KEPT_COLUMNS if field(column)},
        provenance=ProvenanceInfo(
            source=SourceSystem.bundesnetzagentur,
            source_identifier=source_id,
            source_url=source_url,
            source_timestamp=source_timestamp,
            data_origin=DataOrigin.official,
            ingested_at=fetched_at,
        ),
    )


def _parse_points(field: Callable[[str], str]) -> list[ChargingPointRecord]:
    """Expand the six ``Steckertypen{n}`` groups into individual connector records.

    Each group is itself a ``;``-separated list that pairs positionally with
    ``Nennleistung Stecker{n}``. Where a group names several connectors but states one power
    — the common shape for a twin socket on one rating — that single value applies to all of
    them; where the lists disagree in any other way the extra connectors get no power rather
    than a guessed one.
    """
    points: list[ChargingPointRecord] = []
    ordinal = 1
    for group in range(1, CONNECTOR_GROUPS + 1):
        types = _split_multi(field(f"Steckertypen{group}"))
        powers = _split_multi(field(f"Nennleistung Stecker{group}"))
        keys = _split_multi(field(f"Public Key{group}"))
        if not types and not powers:
            continue
        for position in range(max(len(types), len(powers), 1)):
            raw_type = _pick(types, position)
            power = _parse_decimal(_pick(powers, position) or "")
            connector = ConnectorType.parse(raw_type)
            points.append(
                ChargingPointRecord(
                    ordinal=ordinal,
                    connector_type=connector,
                    current_type=_current_type(raw_type, connector),
                    power_kw=power,
                    public_key=_pick(keys, position),
                )
            )
            ordinal += 1
    return points


def _current_type(raw_type: str | None, connector: ConnectorType) -> CurrentType:
    """Decide AC or DC.

    The register prefixes the connector text with ``AC`` or ``DC`` (``"DC Fahrzeugkupplung
    Typ Combo 2 (CCS)"``), which is authoritative when present. When it is not, the connector
    standard itself settles it: CCS and CHAdeMO are DC-only, Type 2 / Schuko / CEE are AC.
    """
    if raw_type:
        upper = raw_type.upper()
        if upper.startswith("DC") or " DC " in upper:
            return CurrentType.dc
        if upper.startswith("AC") or " AC " in upper:
            return CurrentType.ac
    if connector in {ConnectorType.ccs, ConnectorType.chademo}:
        return CurrentType.dc
    if connector in {ConnectorType.type2, ConnectorType.schuko, ConnectorType.cee}:
        return CurrentType.ac
    return CurrentType.unknown


def _synthetic_id(field: Callable[[str], str]) -> str:
    """Deterministic id for the rare row with a blank ``Ladeeinrichtungs-ID``.

    Hashes the tuple that actually identifies a site in the physical world — operator, street,
    house number, postcode and coordinates — so that re-ingesting the same file is an
    idempotent upsert rather than a duplication. Coordinates are included because German
    business parks routinely put several operators at one postal address.
    """
    parts = (
        field("Betreiber"),
        field("Straße"),
        field("Hausnummer"),
        field("Postleitzahl"),
        field("Breitengrad"),
        field("Längengrad"),
    )
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
    return digest[:_SYNTHETIC_ID_LENGTH]


def _parse_bundesland(raw: str) -> Bundesland | None:
    """Resolve the ``Bundesland`` column, tolerating spellings the enum already knows."""
    return Bundesland.from_name(raw) if raw else None


def _parse_point_count(raw: str, parsed_connectors: int) -> int:
    """Number of connectors at the site, never below 1.

    The parsed connector count wins over the register's ``Anzahl Ladepunkte`` so that this
    column and the ``charging_points`` child rows can never disagree — a UI that shows "4
    connectors" above a list of 2 is worse than either number alone. The two legitimately
    differ when one Ladepunkt carries a Type 2 socket and a Schuko outlet on the same cable;
    the operator's declared figure is preserved verbatim in ``raw['Anzahl Ladepunkte']``.
    """
    if parsed_connectors > 0:
        return parsed_connectors
    try:
        return max(int(raw), 1)
    except ValueError:
        return 1


def _parse_decimal(raw: str | None) -> float | None:
    """Parse a German decimal (``48,442398`` / ``3,7``); ``None`` when absent or negative."""
    if not raw:
        return None
    # A dot is only a thousands separator when a decimal comma is also present; "3.7" without
    # a comma is a value some operators enter in English notation, and destroying it would
    # turn 3.7 kW into 37 kW.
    normalised = raw.replace(".", "").replace(",", ".") if "," in raw else raw
    try:
        value = float(normalised)
    except ValueError:
        return None
    return value if value >= 0.0 else None


def _parse_german_date(raw: str) -> date | None:
    """Parse ``DD.MM.YYYY``; ``None`` for an empty or malformed cell."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%d.%m.%Y").date()
    except ValueError:
        return None


def _split_multi(raw: str) -> list[str]:
    """Split a multi-valued cell on ``;`` and drop empty fragments."""
    if not raw:
        return []
    return [part.strip() for part in raw.split(";") if part.strip()]


def _pick(values: Sequence[str], position: int) -> str | None:
    """Value at ``position``; a single-element list broadcasts to every connector."""
    if position < len(values):
        return values[position]
    if len(values) == 1:
        return values[0]
    return None


def _templated_urls(today: date) -> list[str]:
    """Date-templated guesses for the current and the previous month's edition."""
    previous_year, previous_month = (
        (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    )
    return [
        _EDITION_TEMPLATE.format(year=today.year, month=today.month),
        _EDITION_TEMPLATE.format(year=previous_year, month=previous_month),
    ]


def _edition_from_url(url: str) -> date | None:
    """Extract the edition date embedded in the filename, if the URL carries one."""
    match = _URL_EDITION_PATTERN.search(url)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:  # pragma: no cover - the pattern already constrains the ranges
        return None


def _parse_iso(raw: object) -> datetime | None:
    """Parse an ISO-8601 timestamp written by this adapter into the cache pointer."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _file_mtime(path: Path) -> datetime:
    """Modification time of a file as an aware UTC datetime."""
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)


def _report(on_skip: Callable[[str], None] | None, message: str) -> None:
    """Forward a skip message to the caller's collector, if it supplied one."""
    if on_skip is not None:
        on_skip(message)
