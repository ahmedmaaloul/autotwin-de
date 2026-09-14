"""AutoTwin DE core — configuration, structured logging, typed errors and PostGIS persistence.

``autotwin_core`` sits one layer above :mod:`autotwin_contracts` and below every service
(BUILD_SPEC §1): it may import the contracts and nothing else from the project. Everything that
needs a database, a setting or a logger goes through here, which is what keeps the dependency
graph acyclic and lets the ingestion, simulator, streaming and ML packages be developed against
one another without importing the API.

Only the small, universally used surface is re-exported here. The persistence layer is imported
explicitly — ``from autotwin_core.db import ChargingStation`` — so that a script needing nothing
but settings does not pay for SQLAlchemy's import.
"""

from __future__ import annotations

from autotwin_core.config import Settings, get_settings, reset_settings_cache
from autotwin_core.errors import (
    AutoTwinError,
    ConfigurationMissing,
    ConflictError,
    InvalidSourceData,
    NotFoundError,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
    RateLimited,
    ValidationError,
)
from autotwin_core.logging import configure_logging, get_logger

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
    "Settings",
    "ValidationError",
    "__version__",
    "configure_logging",
    "get_logger",
    "get_settings",
    "reset_settings_cache",
]

__version__ = "0.1.0"
"""Version of the core package; the API reports it in ``GET /health``."""
