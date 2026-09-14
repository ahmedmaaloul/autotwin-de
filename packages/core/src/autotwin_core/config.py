"""Application settings — the single place in AutoTwin that reads the environment.

BUILD_SPEC §5 is explicit: ``get_settings()`` is the only supported way to learn how this
deployment is configured, and no other module may touch ``os.environ``. Centralising that has
two concrete payoffs here: a test can swap the whole configuration by clearing one cache, and
the data-honesty rule (§0.2) hinges on a single authoritative ``DATA_MODE`` that the providers,
the API headers and the UI badge all read from the same object.

Field names are lower-case Python identifiers; pydantic-settings maps them case-insensitively
onto the ``AUTOTWIN_`` prefixed environment variables of the spec, so ``database_url`` is fed by
``AUTOTWIN_DATABASE_URL``.
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Final, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

__all__ = [
    "AppEnv",
    "DataMode",
    "LogFormat",
    "Settings",
    "get_settings",
    "reset_settings_cache",
]

_PSYCOPG_DRIVER: Final[str] = "postgresql+psycopg"
"""The only PostgreSQL driver AutoTwin speaks — psycopg 3, which serves sync *and* async."""


class AppEnv(StrEnum):
    """Where the process is running, which is what picks sane defaults for everything else."""

    local = "local"
    """Developer machine: human-readable logs, live sources allowed, docs exposed."""

    ci = "ci"
    """GitHub Actions: no network egress to German open-data portals, fixtures only."""

    docker = "docker"
    """Inside Compose: JSON logs to stdout, service hostnames instead of localhost."""


class LogFormat(StrEnum):
    """Rendering of the structured log stream."""

    json = "json"
    """One JSON object per line — what a log shipper wants."""

    console = "console"
    """Colourised, aligned key/value output — what a human wants."""


class DataMode(StrEnum):
    """How hard the providers try to reach the live German open-data sources (BUILD_SPEC §4).

    Distinct from :class:`~autotwin_contracts.enums.ProviderMode`: this is the *policy* the
    operator configures, while ``ProviderMode`` is the *outcome* a provider reports for one
    call. ``cached`` policy can still produce a ``live`` outcome — and saying so truthfully is
    the entire point of keeping the two apart.
    """

    live = "live"
    """Require the live source; a failure is an error, not a silent downgrade."""

    cached = "cached"
    """Try live, fall back to the on-disk cache and then to fixtures. The default."""

    fixture = "fixture"
    """Never leave the machine. Always used in tests and in CI."""


class Settings(BaseSettings):
    """Runtime configuration of every AutoTwin process (BUILD_SPEC §5).

    Instantiated once per process through :func:`get_settings`. Directly constructing a
    ``Settings`` is legitimate in tests — ``Settings(data_mode=DataMode.fixture, ...)`` — but
    application code should always go through the cached accessor so that one process cannot
    disagree with itself about, say, which data mode it is in.
    """

    model_config = SettingsConfigDict(
        env_prefix="AUTOTWIN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ runtime
    env: AppEnv = Field(default=AppEnv.local, description="Deployment environment.")
    debug: bool = Field(
        default=False,
        description="Verbose behaviour: SQL echo, uncached loggers, tracebacks in responses.",
    )
    log_level: str = Field(default="INFO", description="Root log level name, e.g. INFO or DEBUG.")
    log_format: LogFormat = Field(
        default=LogFormat.json,
        description="Log rendering; defaults to console when ENV=local and unset.",
    )

    # ----------------------------------------------------------------- database
    database_url: str = Field(
        default="postgresql+psycopg://autotwin:autotwin@localhost:5433/autotwin",
        description="PostgreSQL DSN. Port 5433 avoids clashing with a host-installed server.",
    )
    db_pool_size: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Connections kept open per engine, per process.",
    )

    # -------------------------------------------------------------------- kafka
    kafka_bootstrap_servers: str = Field(
        default="localhost:19092",
        description="Redpanda/Kafka bootstrap servers, comma-separated.",
    )
    kafka_enabled: bool = Field(
        default=True,
        description="When false the simulator writes straight to Postgres (BUILD_SPEC §8).",
    )
    kafka_client_id: str = Field(
        default="autotwin",
        description="Client id prefix reported to the broker.",
    )

    # --------------------------------------------------------------------- data
    data_mode: DataMode = Field(
        default=DataMode.cached,
        description="Provider fallback policy: live, cached or fixture.",
    )
    data_dir: Path = Field(
        default=Path("./data"),
        description="Root of the on-disk data lake: raw downloads, cache and fixtures.",
    )
    cache_ttl_seconds: int = Field(
        default=21_600,
        ge=0,
        description="How long a cached download counts as fresh; 6 h matches the DWD cadence.",
    )

    # ---------------------------------------------------------- external sources
    osrm_base_url: str = Field(
        default="https://router.project-osrm.org",
        description="OSRM routing engine; point at http://localhost:5001 for a local container.",
    )
    osrm_timeout_s: float = Field(
        default=20.0,
        gt=0.0,
        description="Per-request timeout for routing calls, in seconds.",
    )
    bnetza_download_url: str = Field(
        default=(
            "https://data.bundesnetzagentur.de/Bundesnetzagentur/SharedDocs/Downloads/DE/"
            "Sachgebiete/Energie/Unternehmen_Institutionen/E_Mobilitaet/Ladesaeulenregister.csv"
        ),
        description="Ladesäulenregister CSV published by the Bundesnetzagentur.",
    )
    dwd_base_url: str = Field(
        default="https://opendata.dwd.de",
        description="Root of the DWD open-data server (Climate Data Center and MOSMIX).",
    )
    autobahn_base_url: str = Field(
        default="https://verkehr.autobahn.de/o/autobahn",
        description="Public Autobahn GmbH API: roadworks, closures and warnings.",
    )
    nominatim_base_url: str = Field(
        default="https://nominatim.openstreetmap.org",
        description="Nominatim geocoder; its usage policy requires a descriptive user agent.",
    )
    http_timeout_s: float = Field(
        default=20.0,
        gt=0.0,
        description="Default timeout for outbound HTTP calls, in seconds.",
    )
    http_user_agent: str = Field(
        default="AutoTwinDE/0.1 (portfolio project; +github)",
        description="User-Agent sent upstream; OSM and DWD require an identifiable one.",
    )

    # ---------------------------------------------------------------------- api
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"],
        description="Browser origins allowed to call the API. JSON list or comma-separated.",
    )

    # ----------------------------------------------------------------------- ml
    ml_model_dir: Path = Field(
        default=Path("./models"),
        description="Directory holding the trained model artefacts and their metrics files.",
    )
    ml_active_model: str = Field(
        default="energy_consumption",
        description="Name of the model the API serves from /api/v1/ml/predict.",
    )

    # ---------------------------------------------------------------------- llm
    ollama_base_url: str = Field(
        default="http://localhost:11434",
        description="Ollama endpoint for the optional copilot.",
    )
    ollama_model: str = Field(default="llama3.2", description="Local model tag to prompt.")
    llm_enabled: bool = Field(
        default=False,
        description="When false /api/v1/copilot/ask answers 503 instead of pretending.",
    )

    # ---------------------------------------------------------------- simulation
    sim_default_vehicles: int = Field(
        default=40,
        ge=1,
        description="Vehicle count of a simulation run created without an explicit one.",
    )
    sim_tick_seconds: float = Field(
        default=1.0,
        gt=0.0,
        description="Wall-clock seconds between simulator ticks.",
    )
    sim_speed_factor: float = Field(
        default=10.0,
        gt=0.0,
        description="Simulated seconds per wall-clock second; 10 makes a corridor run watchable.",
    )
    sim_seed: int = Field(
        default=20_260_214,
        description="Master RNG seed. Fixed so a demo run is reproducible frame for frame.",
    )

    # ------------------------------------------------------------------ validation
    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: Any) -> str:
        """Accept ``debug``/``Debug``/``DEBUG`` and reject anything that is not a real level.

        A typo here would otherwise silently disable logging exactly when it is needed.
        """
        name = str(value).strip().upper()
        if name not in logging.getLevelNamesMapping():
            known = ", ".join(sorted(logging.getLevelNamesMapping()))
            msg = f"unknown log level {value!r}; expected one of: {known}"
            raise ValueError(msg)
        return name

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, value: Any) -> Any:
        """Accept a JSON array *or* a comma-separated list from the environment.

        ``NoDecode`` switches off pydantic-settings' automatic JSON decoding for this field so
        that ``AUTOTWIN_CORS_ORIGINS=http://a,http://b`` — the spelling every Compose file and
        CI matrix reaches for — does not blow up before this validator ever runs.
        """
        if not isinstance(value, str):
            return value
        raw = value.strip()
        if not raw:
            return []
        if raw.startswith("["):
            decoded: Any = json.loads(raw)
            return decoded
        return [part.strip() for part in raw.split(",") if part.strip()]

    @model_validator(mode="after")
    def _default_log_format_to_env(self) -> Self:
        """Render console logs on a developer machine unless the operator said otherwise.

        An explicit ``AUTOTWIN_LOG_FORMAT`` always wins; this only fills the gap, because a
        local ``make dev`` session that prints JSON is hostile to read, while a container that
        prints colour codes is hostile to parse.
        """
        if "log_format" not in self.model_fields_set and self.env is AppEnv.local:
            self.log_format = LogFormat.console
        return self

    # ------------------------------------------------------------------ database URLs
    @property
    def database_url_sync(self) -> str:
        """DSN for synchronous engines — Alembic, ingestion scripts, analytical tooling."""
        return _with_psycopg_driver(self.database_url)

    @property
    def database_url_async(self) -> str:
        """DSN for the asyncio engine used by the API and the streaming consumer.

        Identical in spelling to :attr:`database_url_sync`, because psycopg 3 serves both
        worlds through one SQLAlchemy dialect. Both properties exist anyway so that call sites
        state which engine they mean, and so that swapping a driver later is one edit here
        rather than a grep across seven packages.
        """
        return _with_psycopg_driver(self.database_url)

    # ----------------------------------------------------------------- filesystem
    @property
    def data_dir_path(self) -> Path:
        """Root data directory, created if missing."""
        return _ensure_dir(self.data_dir)

    @property
    def raw_dir(self) -> Path:
        """Untouched downloads exactly as the source served them (bronze layer)."""
        return _ensure_dir(self.data_dir / "raw")

    @property
    def cache_dir(self) -> Path:
        """Provider response cache; entries older than :attr:`cache_ttl_seconds` are stale."""
        return _ensure_dir(self.data_dir / "cache")

    @property
    def fixtures_dir(self) -> Path:
        """Committed sample payloads — the last rung of the fallback chain (BUILD_SPEC §4)."""
        return _ensure_dir(self.data_dir / "fixtures")

    @property
    def model_dir_path(self) -> Path:
        """Directory of trained ML artefacts, created if missing."""
        return _ensure_dir(self.ml_model_dir)

    # --------------------------------------------------------------------- flags
    @property
    def is_fixture_mode(self) -> bool:
        """True when providers must not touch the network at all."""
        return self.data_mode is DataMode.fixture

    @property
    def is_live_mode(self) -> bool:
        """True when a provider must reach the live source or fail loudly."""
        return self.data_mode is DataMode.live

    @property
    def is_local(self) -> bool:
        """True on a developer machine — gates docs exposure and human-readable output."""
        return self.env is AppEnv.local

    @property
    def log_level_number(self) -> int:
        """:attr:`log_level` as the numeric level ``logging`` and structlog filter on."""
        return logging.getLevelNamesMapping()[self.log_level]


def _with_psycopg_driver(url: str) -> str:
    """Force ``postgresql+psycopg`` onto a DSN, whatever driver it was written with.

    Operators paste DSNs from psql (``postgresql://``), from asyncpg tutorials
    (``postgresql+asyncpg://``) or from an ORM guide (``postgres://``). All of them mean the
    same database, and rewriting the scheme here is cheaper than debugging a driver mismatch
    that only surfaces on the first query.
    """
    scheme, separator, remainder = url.partition("://")
    if not separator:
        return url
    base = scheme.split("+", 1)[0]
    if base not in {"postgresql", "postgres"}:
        return url
    return f"{_PSYCOPG_DRIVER}://{remainder}"


def _ensure_dir(path: Path) -> Path:
    """Return ``path`` as an existing directory, creating it and its parents if needed.

    Directory creation is deliberately a side effect of *asking* for the path: every caller
    wants to read or write there immediately, and a missing ``data/cache`` must never be the
    reason an ingestion run fails.
    """
    resolved = path.expanduser()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, reading the environment exactly once.

    The cache is what makes "never read ``os.environ`` outside this module" enforceable: a
    later change to the environment cannot make two modules disagree mid-request.
    """
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached settings so the next :func:`get_settings` re-reads the environment.

    For tests and for long-lived tooling that rewrites ``.env`` between runs. Application code
    has no reason to call it — configuration is immutable for the life of a process.
    """
    get_settings.cache_clear()
