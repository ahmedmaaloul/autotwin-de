"""AutoTwin DE — German open-data adapters, ingestion pipelines and the data zone.

Three layers, in the order data moves through them:

``providers``
    Adapters for the five official sources (BUILD_SPEC §4). They parse and they walk the
    ``live → cache → fixture`` chain; they never touch the database.

``pipelines``
    Validate what an adapter returned, upsert it into PostGIS on a natural key, and keep
    ``data_ingestion_runs`` truthful — including when the run fails (BUILD_SPEC §6, §15).

``lake``
    The ``raw → bronze → silver → gold`` zone: archived source documents with SHA-256
    sidecars, and Parquet tables that keep the *rejected* rows the database deliberately never
    receives.

``python -m autotwin_ingestion.cli`` drives all three; :mod:`autotwin_ingestion.demo` composes
them into the one command ``make demo`` runs.

Nothing heavy is imported here. Pulling in :mod:`autotwin_ingestion.lake` costs Polars, PyArrow
and DuckDB, which an API process that only wants the routing adapter has no use for, so every
submodule is imported explicitly by the code that needs it.
"""

from __future__ import annotations

__version__ = "0.1.0"
"""Package version, mirrored in ``services/ingestion/pyproject.toml``."""
