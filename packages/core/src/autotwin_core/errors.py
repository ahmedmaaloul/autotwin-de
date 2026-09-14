"""Typed application errors and the provider error hierarchy (BUILD_SPEC §4, §7).

Two rules from the spec meet in this module:

* **Degrade, never crash** — an external source that is down raises a *typed* error which the
  caller can catch precisely enough to decide between "fall back to cache" and "give up".
* **One error envelope** — every 4xx/5xx response body is ``{"error": {code, message, details,
  request_id}}``, so each exception carries the stable ``code`` and the ``http_status`` it maps
  to as *class* attributes. The API's exception handler is then a single generic function
  rather than a growing chain of ``isinstance`` checks.

Codes come from :class:`~autotwin_contracts.api.ErrorCode` so the string the frontend switches
on is defined once, in the package both sides are generated from.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar, Self

from autotwin_contracts.api import ErrorCode, ErrorDetail, ErrorResponse
from autotwin_contracts.enums import SourceSystem

__all__ = [
    "AutoTwinError",
    "ConfigurationMissing",
    "ConflictError",
    "InvalidSourceData",
    "NotFoundError",
    "ProviderError",
    "ProviderTimeout",
    "ProviderUnavailable",
    "RateLimited",
    "ValidationError",
]


class AutoTwinError(Exception):
    """Base class of every error AutoTwin raises deliberately.

    Anything that is *not* an ``AutoTwinError`` reaching the API boundary is a bug, and is
    reported as ``internal_error``/500 without leaking its message to the client. That
    distinction is the reason this base exists at all.
    """

    code: ClassVar[str] = ErrorCode.internal_error.value
    """Stable snake_case identifier; part of the public API contract."""

    http_status: ClassVar[int] = 500
    """HTTP status the API answers with for this class of failure (BUILD_SPEC §7)."""

    def __init__(self, message: str, *, details: Mapping[str, object] | None = None) -> None:
        """Store the English message and any structured context worth showing the caller."""
        super().__init__(message)
        self.message = message
        self.details: dict[str, object] = dict(details) if details else {}

    def __str__(self) -> str:
        """The message alone — details belong in the envelope, not in a log prefix."""
        return self.message

    def __repr__(self) -> str:
        """Developer-facing form, including the wire code so grepping a log is easy."""
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"

    def as_error_detail(self, *, request_id: str | None = None) -> ErrorDetail:
        """Render as the contract's error body."""
        return ErrorDetail(
            code=self.code,
            message=self.message,
            details=dict(self.details) or None,
            request_id=request_id,
        )

    def as_error_response(self, *, request_id: str | None = None) -> ErrorResponse:
        """Render as the full ``{"error": {...}}`` envelope returned to HTTP clients."""
        return ErrorResponse(error=self.as_error_detail(request_id=request_id))


class NotFoundError(AutoTwinError):
    """A requested resource does not exist — an id, a slug or a model version."""

    code: ClassVar[str] = ErrorCode.not_found.value
    http_status: ClassVar[int] = 404

    @classmethod
    def of(cls, resource: str, identifier: object) -> Self:
        """Build the usual "no such X" error with the identifier kept in ``details``.

        Repeating the same message by hand in every repository is how ``details`` ends up
        inconsistent and the frontend ends up parsing message text.
        """
        return cls(
            f"{resource} {identifier!r} was not found",
            details={"resource": resource, "identifier": str(identifier)},
        )


class ValidationError(AutoTwinError):
    """Input failed a domain rule that Pydantic cannot express on its own.

    Request-shape violations are raised by FastAPI/Pydantic and mapped to the same code by the
    exception handler; this class covers the rules that need context, such as "arrival SOC
    would be negative" or "bbox is outside Germany".
    """

    code: ClassVar[str] = ErrorCode.validation_error.value
    http_status: ClassVar[int] = 422


class ConflictError(AutoTwinError):
    """The request contradicts the current state of a resource.

    Raised for lifecycle violations — starting a simulation run that is already running,
    activating a second ML model of the same name — where retrying unchanged cannot succeed.
    """

    code: ClassVar[str] = "conflict"
    http_status: ClassVar[int] = 409


class ProviderError(AutoTwinError):
    """An external data source could not be used (BUILD_SPEC §4).

    Providers raise one of the subclasses; catching ``ProviderError`` is how a pipeline says
    "any upstream problem — fall back to cache or fixture and mark the answer degraded".

    ``source`` and ``source_url`` are folded into :attr:`details` so the error envelope and
    the ingestion run's ``error_message`` both state *which* German source failed, which is
    the first thing anyone asks when the dashboard shows a stale tile.
    """

    code: ClassVar[str] = "provider_error"
    http_status: ClassVar[int] = 502

    def __init__(
        self,
        message: str,
        *,
        source: SourceSystem | None = None,
        source_url: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        """Record the failing source alongside the usual message and details."""
        merged: dict[str, object] = dict(details) if details else {}
        if source is not None:
            merged["source"] = str(source)
        if source_url is not None:
            merged["source_url"] = source_url
        super().__init__(message, details=merged)
        self.source = source
        self.source_url = source_url


class ProviderUnavailable(ProviderError):
    """The source is unreachable: DNS failure, refused connection, or a 5xx response."""

    code: ClassVar[str] = ErrorCode.provider_unavailable.value
    http_status: ClassVar[int] = 503


class ProviderTimeout(ProviderError):
    """The source did not answer within the configured timeout.

    Separate from :class:`ProviderUnavailable` because the remedies differ: a timeout on the
    public OSRM demo server is usually load and worth retrying, a refused connection is not.
    """

    code: ClassVar[str] = ErrorCode.provider_timeout.value
    http_status: ClassVar[int] = 504


class InvalidSourceData(ProviderError):
    """The source answered, but the payload does not match what the adapter can parse.

    Almost always means the upstream schema changed — a renamed column in the
    Ladesäulenregister CSV, a restructured Autobahn JSON object. Surfaced as 502 so it is
    visibly *their* shape and *our* parser, not a client mistake.
    """

    code: ClassVar[str] = ErrorCode.invalid_source_data.value
    http_status: ClassVar[int] = 502


class RateLimited(ProviderError):
    """The source refused the call because the client is over its quota.

    Nominatim's usage policy is the realistic case. ``retry_after_s`` mirrors the upstream
    ``Retry-After`` header so the API can pass the advice on instead of inventing a backoff.
    """

    code: ClassVar[str] = ErrorCode.rate_limited.value
    http_status: ClassVar[int] = 429

    def __init__(
        self,
        message: str,
        *,
        retry_after_s: float | None = None,
        source: SourceSystem | None = None,
        source_url: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        """Record the upstream's retry advice, when it gave any."""
        merged: dict[str, object] = dict(details) if details else {}
        if retry_after_s is not None:
            merged["retry_after_s"] = retry_after_s
        super().__init__(message, source=source, source_url=source_url, details=merged)
        self.retry_after_s = retry_after_s


class ConfigurationMissing(ProviderError):
    """A setting, credential or local path the provider needs is absent.

    A 500 rather than a 503: nothing upstream is broken, the deployment is incomplete. Raised
    eagerly at construction time so a misconfigured provider fails on startup instead of
    halfway through an ingestion run.
    """

    code: ClassVar[str] = ErrorCode.configuration_missing.value
    http_status: ClassVar[int] = 500
