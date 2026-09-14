"""The envelope every provider answers with (BUILD_SPEC §4).

The honesty rule of BUILD_SPEC §0.2 needs a place to live. :class:`ProviderResult` is that
place: an adapter cannot return data without also stating *how* it obtained it, because
:attr:`ProviderResult.mode` has no default. The API copies that mode into the
``X-AutoTwin-Data-Mode`` header (§7) and the frontend renders *"Live-Quelle nicht verfügbar"*
from it, so the whole "degrade, never crash" chain is anchored on this one dataclass.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime

from autotwin_contracts import DataModeInfo, ProviderMode, SourceSystem, utc_now

__all__ = ["ProviderResult"]


@dataclass(frozen=True, slots=True)
class ProviderResult[T]:
    """Data from a provider, together with the provenance of the answer itself.

    Generic over the payload so that ``ProviderResult[list[ChargingStationRecord]]`` and
    ``ProviderResult[RouteResult]`` share one implementation and one set of invariants.
    """

    data: T
    """The parsed payload — records, a route, a list of places."""

    mode: ProviderMode
    """How this answer was obtained: live, cache or fixture. Never guessed, always stated."""

    source_url: str | None
    """The document or endpoint behind the answer, for the provenance block of §3.1."""

    fetched_at: datetime
    """When the underlying data was obtained (UTC) — cache age, not response age."""

    warnings: list[str] = field(default_factory=list)
    """Non-fatal problems, in English, surfaced to the UI next to the data."""

    @property
    def is_degraded(self) -> bool:
        """Whether the answer is anything other than live upstream data.

        Cache and fixture both count. The UI distinguishes them, but every caller that only
        wants to know "should I show a warning badge" asks this.
        """
        return self.mode is not ProviderMode.live

    @classmethod
    def live(
        cls,
        data: T,
        *,
        source_url: str | None = None,
        fetched_at: datetime | None = None,
        warnings: Iterable[str] = (),
    ) -> ProviderResult[T]:
        """Wrap data that was just fetched from the upstream source."""
        return cls(
            data=data,
            mode=ProviderMode.live,
            source_url=source_url,
            fetched_at=fetched_at or utc_now(),
            warnings=list(warnings),
        )

    @classmethod
    def cached(
        cls,
        data: T,
        *,
        source_url: str | None = None,
        fetched_at: datetime,
        warnings: Iterable[str] = (),
    ) -> ProviderResult[T]:
        """Wrap data served from the on-disk cache.

        ``fetched_at`` is required here and describes when the *cached copy* was originally
        downloaded, because that is what makes an age meaningful. Defaulting it to "now" would
        present six-hour-old data as fresh, which is precisely the lie this type prevents.
        """
        return cls(
            data=data,
            mode=ProviderMode.cache,
            source_url=source_url,
            fetched_at=fetched_at,
            warnings=list(warnings),
        )

    @classmethod
    def fixture(
        cls,
        data: T,
        *,
        source_url: str | None = None,
        fetched_at: datetime | None = None,
        warnings: Iterable[str] = (),
    ) -> ProviderResult[T]:
        """Wrap data read from a bundled fixture — offline development, CI, or a full outage."""
        return cls(
            data=data,
            mode=ProviderMode.fixture,
            source_url=source_url,
            fetched_at=fetched_at or utc_now(),
            warnings=list(warnings),
        )

    def with_warning(self, message: str) -> ProviderResult[T]:
        """Return a copy carrying one more warning, leaving this instance untouched."""
        return replace(self, warnings=[*self.warnings, message])

    def map[U](self, transform: Callable[[T], U]) -> ProviderResult[U]:
        """Apply ``transform`` to the payload, preserving mode, source and timestamps.

        Parsing steps are chained this way (bytes to records, records to DTOs) so that the
        provenance of the answer survives every transformation. Rebuilding the envelope by
        hand is where a ``fixture`` result quietly becomes a ``live`` one.
        """
        return ProviderResult(
            data=transform(self.data),
            mode=self.mode,
            source_url=self.source_url,
            fetched_at=self.fetched_at,
            warnings=list(self.warnings),
        )

    def as_data_mode_info(self, source: SourceSystem | None = None) -> DataModeInfo:
        """Render the provenance half of this result as the API's ``DataModeInfo`` DTO."""
        return DataModeInfo(
            mode=self.mode,
            source=source,
            fetched_at=self.fetched_at,
            source_url=self.source_url,
            warnings=tuple(self.warnings),
        )
