"""Data-quality payloads: per-source health, ingestion history and licence terms.

This module carries the honesty rule of BUILD_SPEC §0.2 in two concrete forms.

**The status rule lives here, once.** ``GET /api/v1/data/quality`` and the dashboard's
``data_freshness`` block both have to answer "is this source healthy?", and two
implementations of that question would eventually disagree on the same screen. The rule is
:func:`derive_status`, and both callers import it.

**The licence table lives here, once.** Every ingested dataset keeps its publisher's terms
(``DATA_LICENSES.md``); the API restates them next to the data so a user of the platform is
never shown a figure whose attribution they would have to go looking for. The entries are
deliberately literal strings rather than links: an attribution line has to be reproducible
verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final, Self
from uuid import UUID

from pydantic import Field

from autotwin_api.schemas.common import ApiModel
from autotwin_contracts import (
    DataOrigin,
    IngestionOutcome,
    IngestionStatus,
    ProviderMode,
    SourceSystem,
)

__all__ = [
    "SOURCE_TERMS",
    "DataSourceInfo",
    "IngestionRunOut",
    "QualityReportOut",
    "SourceQuality",
    "SourceTerms",
    "acceptance_rate_percent",
    "age_minutes_of",
    "derive_status",
]


@dataclass(frozen=True, slots=True)
class SourceTerms:
    """Everything AutoTwin states about one upstream source besides its rows.

    ``max_age_minutes`` is the editorial part: it encodes how often the publisher actually
    changes the data, which is what separates *delayed* from merely *not fetched in the last
    hour*. The Ladesäulenregister is republished monthly, so a run from last week is perfectly
    healthy; a DWD observation from last week is not.
    """

    display_name: str
    licence: str | None
    """SPDX-style identifier or the publisher's own wording. ``None`` when the publisher
    declares no terms at all — which is a fact about the source, not a missing value."""

    attribution: str | None
    """The credit line that must accompany anything derived from this source, verbatim."""

    url: str | None
    data_origin: DataOrigin
    max_age_minutes: float | None
    """Age beyond which the last successful run counts as *delayed*; ``None`` disables the
    check for sources with no meaningful refresh cadence."""


_DAY: Final[float] = 24 * 60.0

SOURCE_TERMS: Final[MappingProxyType[SourceSystem, SourceTerms]] = MappingProxyType(
    {
        SourceSystem.bundesnetzagentur: SourceTerms(
            display_name="Bundesnetzagentur — Ladesäulenregister",
            licence="CC BY 4.0",
            attribution="Ladesäulenregister der Bundesnetzagentur",
            url=(
                "https://www.bundesnetzagentur.de/DE/Fachthemen/ElektrizitaetundGas/"
                "E-Mobilitaet/Ladesaeulenkarte/start.html"
            ),
            data_origin=DataOrigin.official,
            # The register is republished roughly monthly; 45 days leaves room for a late one
            # without ever calling a genuinely stale copy fresh.
            max_age_minutes=45 * _DAY,
        ),
        SourceSystem.dwd: SourceTerms(
            display_name="Deutscher Wetterdienst — Open Data",
            licence="CC BY 4.0",
            attribution="Quelle: Deutscher Wetterdienst",
            url="https://opendata.dwd.de/",
            data_origin=DataOrigin.official,
            # 10-minute station values; three hours old already means several missed cycles.
            max_age_minutes=180.0,
        ),
        SourceSystem.autobahn: SourceTerms(
            display_name="Autobahn GmbH des Bundes",
            licence=None,
            attribution="Daten: Autobahn GmbH des Bundes",
            url="https://verkehr.autobahn.de/o/autobahn/",
            data_origin=DataOrigin.official,
            max_age_minutes=360.0,
        ),
        SourceSystem.mobilithek: SourceTerms(
            display_name="Mobilithek (DATEX II)",
            licence="Siehe Datenbereitsteller",
            attribution="Daten: Mobilithek",
            url="https://mobilithek.info/",
            data_origin=DataOrigin.official,
            max_age_minutes=360.0,
        ),
        SourceSystem.osm: SourceTerms(
            display_name="OpenStreetMap",
            licence="ODbL 1.0",
            attribution="© OpenStreetMap-Mitwirkende",
            url="https://www.openstreetmap.org/copyright",
            data_origin=DataOrigin.official,
            max_age_minutes=90 * _DAY,
        ),
        SourceSystem.osrm: SourceTerms(
            display_name="OSRM (OpenStreetMap-Routing)",
            licence="ODbL 1.0",
            attribution="© OpenStreetMap-Mitwirkende",
            url="https://router.project-osrm.org/",
            data_origin=DataOrigin.official,
            max_age_minutes=90 * _DAY,
        ),
        SourceSystem.simulator: SourceTerms(
            display_name="AutoTwin Simulator",
            licence="Apache-2.0",
            attribution="Simuliert von AutoTwin DE — keine Messdaten",
            url=None,
            data_origin=DataOrigin.simulated,
            max_age_minutes=None,
        ),
        SourceSystem.derived: SourceTerms(
            display_name="AutoTwin (abgeleitet)",
            licence="Apache-2.0",
            attribution="Abgeleitet von AutoTwin DE",
            url=None,
            data_origin=DataOrigin.derived,
            max_age_minutes=None,
        ),
    }
)
"""Licence, attribution and refresh cadence per source (``DATA_LICENSES.md``).

The Autobahn entry carries ``licence=None`` on purpose. The publisher states no terms, and
inventing a plausible one — "public domain", say — would be the most damaging kind of
convenience in a file whose job is to be legally accurate.
"""


def age_minutes_of(moment: datetime | None, *, now: datetime) -> float | None:
    """Minutes between ``moment`` and ``now``, or ``None`` when there is no moment.

    Clamped at zero: a source clock running slightly ahead of ours would otherwise render as a
    negative age, which reads as a bug in this platform rather than a skew in theirs.
    """
    if moment is None:
        return None
    return max(0.0, (now - moment).total_seconds() / 60.0)


def acceptance_rate_percent(*, rows_received: int, rows_accepted: int) -> float:
    """Share of received rows that survived validation, in percent (BUILD_SPEC §6).

    A run that received nothing reports 0 %, not 100 %: "every one of the zero rows I received
    was accepted" is technically true and operationally useless, and the failed
    Ladesäulenregister run in this database is exactly that case.
    """
    if rows_received <= 0:
        return 0.0
    return min(100.0, rows_accepted / rows_received * 100.0)


def derive_status(
    *,
    source: SourceSystem,
    outcome: IngestionOutcome | None,
    provider_mode: ProviderMode | None,
    age_minutes: float | None,
) -> IngestionStatus:
    """Classify a source from its most recent run (BUILD_SPEC §2, ``IngestionStatus``).

    The order of the tests is the substance of this function:

    1. **Simulated sources are never "healthy".** ``simulator`` and ``derived`` produce no
       external data, so they report ``simulation`` — a distinct state, so that a green tick
       on the dashboard can never be read as "a German authority confirmed this".
    2. **A failed run outranks everything.** Age and provider mode describe data that a failed
       run did not produce.
    3. **Anything but ``live`` is degraded.** Answering from cache or from a bundled fixture is
       the documented fallback (BUILD_SPEC §4), and the whole point of the chain is that the
       fallback is *visible*.
    4. **A partial run is degraded too** — rows were rejected by the quality rules, so the
       table is knowingly incomplete.
    5. **Only then does age matter**, against the publisher's own cadence
       (:attr:`SourceTerms.max_age_minutes`).
    """
    terms = SOURCE_TERMS.get(source)
    if terms is not None and terms.data_origin is not DataOrigin.official:
        return IngestionStatus.simulation
    if outcome is IngestionOutcome.failed:
        return IngestionStatus.failed
    if provider_mode is not None and provider_mode is not ProviderMode.live:
        return IngestionStatus.degraded
    if outcome is IngestionOutcome.partial:
        return IngestionStatus.degraded
    limit = terms.max_age_minutes if terms is not None else None
    if limit is not None and age_minutes is not None and age_minutes > limit:
        return IngestionStatus.delayed
    return IngestionStatus.healthy


class QualityReportOut(ApiModel):
    """The validation report a pipeline stored on its run row (BUILD_SPEC §6).

    Violations are truncated before they leave the service: a run that rejected forty thousand
    malformed coordinates has forty thousand violations, and shipping them would turn a
    diagnostic into a denial of service on the browser. ``violations_truncated`` says how many
    were withheld, so the number is never silently wrong.
    """

    violations: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Individual rule violations: rule, field, message and row index.",
    )
    rule_stats: dict[str, int] = Field(
        default_factory=dict,
        description="How often each rule fired across the whole run.",
    )
    violations_truncated: int = Field(
        default=0,
        ge=0,
        description="Violations withheld from `violations` because the list was capped.",
    )


class SourceQuality(ApiModel):
    """Current health of one upstream source, with the terms its data comes under."""

    source: SourceSystem = Field(..., description="The source system this row describes.")
    status: IngestionStatus = Field(
        ...,
        description=(
            "healthy | delayed | degraded | failed | simulation — see the rule in "
            "`derive_status`. `simulation` means the rows were generated by this platform."
        ),
    )
    data_origin: DataOrigin = Field(
        ...,
        description="official for an ingested source, simulated for the simulator.",
    )
    last_run_at: datetime | None = Field(
        default=None,
        description="Start of the most recent pipeline execution (UTC), null if it never ran.",
    )
    age_minutes: float | None = Field(
        default=None,
        description="Minutes since that run started; null when the source has never run.",
    )
    rows_received: int = Field(..., ge=0, description="Rows the source delivered.")
    rows_accepted: int = Field(..., ge=0, description="Rows that passed every quality rule.")
    rows_rejected: int = Field(..., ge=0, description="Rows rejected and therefore not stored.")
    rows_duplicate: int = Field(..., ge=0, description="Rows already known from an earlier run.")
    acceptance_rate: float = Field(
        ...,
        ge=0.0,
        le=100.0,
        description="`rows_accepted / rows_received` as a percentage (0-100).",
    )
    provider_mode: ProviderMode | None = Field(
        default=None,
        description="How the last run obtained the data: live, cache or fixture.",
    )
    licence: str | None = Field(
        default=None,
        description="Licence of the dataset. Null means the publisher declares none.",
    )
    attribution: str | None = Field(
        default=None,
        description="Credit line that must accompany anything derived from this source.",
    )
    source_url: str | None = Field(
        default=None,
        description="Document or endpoint the last run read, else the publisher's landing page.",
    )
    pipeline: str | None = Field(
        default=None,
        description="Name of the pipeline that produced the last run, e.g. `dwd_weather`.",
    )
    error_message: str | None = Field(
        default=None,
        description="Why the last run failed; null when it did not.",
    )


class IngestionRunOut(ApiModel):
    """One execution of one pipeline — a row of ``data_ingestion_runs``."""

    id: UUID = Field(..., description="Run identifier, referenced by every row it wrote.")
    source: SourceSystem = Field(..., description="Source system the run fetched from.")
    pipeline: str = Field(..., description="Pipeline name, e.g. `bnetza_charging_stations`.")
    started_at: datetime = Field(..., description="When the run started (UTC).")
    finished_at: datetime | None = Field(
        default=None,
        description="When it finished (UTC); null while it is still running.",
    )
    status: IngestionOutcome = Field(..., description="success, partial or failed.")
    provider_mode: ProviderMode = Field(
        ...,
        description="Whether the rows came from the live source, the cache or a fixture.",
    )
    rows_received: int = Field(..., ge=0, description="Rows read from the source.")
    rows_accepted: int = Field(..., ge=0, description="Rows written to the database.")
    rows_rejected: int = Field(..., ge=0, description="Rows a quality rule refused.")
    rows_duplicate: int = Field(..., ge=0, description="Rows already present from an earlier run.")
    bytes_downloaded: int | None = Field(
        default=None,
        description="Payload size in bytes, when the pipeline measured it.",
    )
    source_url: str | None = Field(default=None, description="Exact URL the run read.")
    error_message: str | None = Field(
        default=None,
        description="Failure detail, null on a successful run.",
    )
    quality_report: QualityReportOut | None = Field(
        default=None,
        description="Validation summary of this run (BUILD_SPEC §6).",
    )


class DataSourceInfo(ApiModel):
    """A source AutoTwin draws on, with its terms and when it last answered.

    Separate from :class:`SourceQuality` because the audience is different: this is the
    *catalogue* a reader consults before using a number, and it stays meaningful for a source
    that has never been ingested in this deployment.
    """

    source: SourceSystem = Field(..., description="Stable identifier of the source.")
    display_name: str = Field(..., description="Publisher's name as it should be shown.")
    licence: str | None = Field(
        default=None,
        description="Licence identifier; null when the publisher declares none.",
    )
    attribution: str | None = Field(
        default=None,
        description="Required credit line, to be reproduced verbatim.",
    )
    url: str | None = Field(default=None, description="Landing page or API root.")
    data_origin: DataOrigin = Field(
        ...,
        description="official, simulated or derived — what kind of data this source yields.",
    )
    last_run_at: datetime | None = Field(
        default=None,
        description="Start of the most recent ingestion run, null if never ingested here.",
    )
    last_status: IngestionOutcome | None = Field(
        default=None,
        description="Outcome of that run, null if the source has never run.",
    )
    rows_in_database: int | None = Field(
        default=None,
        ge=0,
        description="Rows this deployment currently holds from the source, when countable.",
    )

    @classmethod
    def of(
        cls,
        source: SourceSystem,
        *,
        last_run_at: datetime | None = None,
        last_status: IngestionOutcome | None = None,
        rows_in_database: int | None = None,
    ) -> Self:
        """Build the catalogue entry for ``source`` from :data:`SOURCE_TERMS`."""
        terms = SOURCE_TERMS[source]
        return cls(
            source=source,
            display_name=terms.display_name,
            licence=terms.licence,
            attribution=terms.attribution,
            url=terms.url,
            data_origin=terms.data_origin,
            last_run_at=last_run_at,
            last_status=last_status,
            rows_in_database=rows_in_database,
        )
