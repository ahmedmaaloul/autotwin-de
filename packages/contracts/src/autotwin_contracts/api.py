"""Transport envelopes shared by the API and — through OpenAPI — by the frontend.

Only genuinely cross-cutting types live here: pagination, the error envelope, the liveness
and readiness payloads and the data-freshness marker. Endpoint-specific response models
(``DashboardSummary``, ``RouteAnalysis``, …) belong to ``autotwin_api.schemas``, because they
change with their endpoint and nothing outside the API needs them.

Field descriptions are part of the deliverable: ``openapi-typescript`` copies them into
``apps/web/types/api.generated.ts`` as doc comments, so they are what a frontend developer
reads at the call site.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field

from autotwin_contracts.enums import ProviderMode, SourceSystem
from autotwin_contracts.temporal import UtcDatetime

__all__ = [
    "DATA_MODE_HEADER",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "REQUEST_ID_HEADER",
    "DataModeInfo",
    "ErrorCode",
    "ErrorDetail",
    "ErrorResponse",
    "HealthResponse",
    "Page",
    "PaginationParams",
    "ReadinessChecks",
    "ReadyResponse",
]

DEFAULT_PAGE_SIZE: Final[int] = 50
MAX_PAGE_SIZE: Final[int] = 500

DATA_MODE_HEADER: Final[str] = "X-AutoTwin-Data-Mode"
"""Response header carrying ``live``/``cache``/``fixture`` on every data endpoint."""

REQUEST_ID_HEADER: Final[str] = "X-Request-ID"
"""Correlation id echoed on every response and repeated inside the error envelope."""


class Page[ItemT](BaseModel):
    """The list envelope of BUILD_SPEC §7: ``{items, total, page, page_size, has_next}``.

    Offset pagination, not cursors: the collections behind it are bounded (tens of thousands
    of charging sites, not billions of rows), page numbers are what the UI's pager needs, and
    ``total`` is what makes "1-50 of 12,345" possible.
    """

    model_config = ConfigDict(frozen=True)

    items: list[ItemT] = Field(..., description="The rows on this page, in the query's order.")
    total: int = Field(..., ge=0, description="Total number of rows matching the filters.")
    page: int = Field(..., ge=1, description="1-based index of this page.")
    page_size: int = Field(
        ...,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Rows requested per page (1-{MAX_PAGE_SIZE}).",
    )
    has_next: bool = Field(..., description="Whether a further page exists after this one.")

    @classmethod
    def build(
        cls,
        items: Sequence[ItemT],
        *,
        total: int,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Self:
        """Assemble a page and derive ``has_next`` from the offset arithmetic.

        Deriving it in one place stops the classic off-by-one where an endpoint reports a
        further page that returns nothing.
        """
        return cls(
            items=list(items),
            total=total,
            page=page,
            page_size=page_size,
            has_next=page * page_size < total,
        )

    @classmethod
    def empty(cls, *, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE) -> Self:
        """An empty page — the honest answer for a filter that matched nothing."""
        return cls(items=[], total=0, page=page, page_size=page_size, has_next=False)


class PaginationParams(BaseModel):
    """The ``?page=&page_size=`` query parameters, with the bounds of BUILD_SPEC §7."""

    model_config = ConfigDict(frozen=True)

    page: int = Field(default=1, ge=1, description="1-based page index.")
    page_size: int = Field(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Rows per page (1-{MAX_PAGE_SIZE}).",
    )

    @property
    def offset(self) -> int:
        """SQL ``OFFSET`` for this page."""
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        """SQL ``LIMIT`` for this page."""
        return self.page_size


class ErrorCode(StrEnum):
    """Stable snake_case error codes (BUILD_SPEC §7).

    The frontend switches on these, never on the message text, so that German and English UI
    strings can be chosen client-side. ``ErrorDetail.code`` stays a plain ``str`` so an
    endpoint may add a code without a contracts release; these are the codes everyone knows.
    """

    provider_unavailable = "provider_unavailable"
    """Upstream source unreachable — HTTP 503."""

    provider_timeout = "provider_timeout"
    """Upstream source too slow — HTTP 504."""

    invalid_source_data = "invalid_source_data"
    """Upstream answered, but in a shape we cannot parse — HTTP 502."""

    rate_limited = "rate_limited"
    """Upstream rate limit hit — HTTP 429."""

    configuration_missing = "configuration_missing"
    """A required setting or credential is absent — HTTP 500."""

    validation_error = "validation_error"
    """Request failed validation — HTTP 422."""

    not_found = "not_found"
    """No such resource — HTTP 404."""

    internal_error = "internal_error"
    """Unhandled failure — HTTP 500."""


class ErrorDetail(BaseModel):
    """The body of the error envelope."""

    model_config = ConfigDict(frozen=True)

    code: str = Field(
        ...,
        description="Stable snake_case error code; see ErrorCode for the known values.",
        examples=["provider_unavailable"],
    )
    message: str = Field(
        ...,
        description="Human-readable English explanation. Not for display without translation.",
        examples=["DWD open data unreachable"],
    )
    details: dict[str, Any] | None = Field(
        default=None,
        description="Structured context, e.g. the failing field or the upstream URL.",
    )
    request_id: str | None = Field(
        default=None,
        description="Correlation id, identical to the X-Request-ID response header.",
    )


class ErrorResponse(BaseModel):
    """Every 4xx and 5xx response body: ``{"error": {...}}``.

    One shape for all failures means the frontend has exactly one error path, and a 503 from a
    provider is handled by the same code as a 422 from validation.
    """

    model_config = ConfigDict(frozen=True)

    error: ErrorDetail = Field(..., description="What went wrong.")

    @classmethod
    def of(
        cls,
        code: str | ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> Self:
        """Build the envelope from its parts — what the FastAPI exception handlers call."""
        return cls(
            error=ErrorDetail(
                code=str(code),
                message=message,
                details=details,
                request_id=request_id,
            )
        )


class HealthResponse(BaseModel):
    """``GET /health`` — liveness only. It must not touch the database."""

    model_config = ConfigDict(frozen=True)

    status: str = Field(
        default="ok",
        description="'ok' while the process is serving; the probe checks nothing else.",
    )
    version: str = Field(..., description="Deployed application version.")
    uptime_s: float = Field(..., ge=0.0, description="Seconds since the process started.")


class ReadinessChecks(BaseModel):
    """Per-dependency readiness flags."""

    model_config = ConfigDict(frozen=True)

    database: bool = Field(..., description="PostgreSQL/PostGIS answered SELECT 1.")
    kafka: bool = Field(
        ...,
        description="Broker reachable, or true when KAFKA_ENABLED is false and Kafka is not "
        "required for this deployment.",
    )
    model: bool = Field(..., description="The active ML artefact is loaded and can predict.")


class ReadyResponse(BaseModel):
    """``GET /ready`` — readiness including dependencies (BUILD_SPEC §7)."""

    model_config = ConfigDict(frozen=True)

    status: str = Field(
        ...,
        description="'ready' when every check passed, otherwise 'degraded'.",
        examples=["ready"],
    )
    checks: ReadinessChecks = Field(..., description="Result of each dependency check.")


class DataModeInfo(BaseModel):
    """How a piece of data was obtained — the machinery behind the honesty rule.

    The API attaches it to responses (and mirrors :attr:`mode` into the
    ``X-AutoTwin-Data-Mode`` header) so the UI can say *"Live-Quelle nicht verfügbar — es
    werden zwischengespeicherte Daten angezeigt"* instead of pretending everything is fresh.
    """

    model_config = ConfigDict(frozen=True)

    mode: ProviderMode = Field(..., description="live, cache or fixture.")
    source: SourceSystem | None = Field(
        default=None,
        description="Which upstream system the data belongs to.",
    )
    fetched_at: UtcDatetime | None = Field(
        default=None,
        description="When the underlying data was fetched from the source (UTC).",
    )
    source_url: str | None = Field(
        default=None,
        description="Document or endpoint the data came from.",
    )
    warnings: tuple[str, ...] = Field(
        default=(),
        description="Non-fatal problems encountered while answering, in English.",
    )

    @computed_field  # type: ignore[prop-decorator]  # mypy: decorated property (pydantic docs)
    @property
    def is_live(self) -> bool:
        """True only for genuinely live data — the flag the 'SIMULIERT'/stale badges read."""
        return self.mode is ProviderMode.live
