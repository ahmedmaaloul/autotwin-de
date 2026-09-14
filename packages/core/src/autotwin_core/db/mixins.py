"""The provenance block (BUILD_SPEC §3.1), mixed into every externally-sourced table.

This mixin is the database half of the project's honesty rule (§0.2). Every row that did not
originate inside AutoTwin says who published it, which document it came from, when the *source*
considered it valid, and which pipeline execution wrote it. That is what lets
``GET /api/v1/data/sources`` answer "where did this number come from?" without guesswork, and
what makes it impossible for simulated rows to be mistaken for official ones: ``data_origin``
is not nullable and has no default.

Present on ``charging_stations``, ``charging_points``, ``weather_observations``,
``traffic_events``, ``routes``, ``vehicles`` and ``trips``. Deliberately *not* on ``telemetry``,
which is always simulated and too high-volume to carry seven extra columns per row.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from autotwin_contracts.enums import DataOrigin, SourceSystem
from autotwin_core.db.types import DATA_ORIGIN_ENUM, SOURCE_SYSTEM_ENUM

__all__ = ["ProvenanceMixin"]

_PROVENANCE_SORT_ORDER: int = 50
"""Groups the provenance columns together, after the domain columns of each table."""


class ProvenanceMixin:
    """Columns describing where a row came from and when it was read.

    ``ingestion_run_id`` is declared with :func:`~sqlalchemy.orm.declared_attr` because a
    ``ForeignKey`` object cannot be shared between tables — SQLAlchemy needs a fresh one per
    mapped class, and a plain class attribute would be copied by reference into all seven.
    """

    source: Mapped[SourceSystem] = mapped_column(
        SOURCE_SYSTEM_ENUM,
        nullable=False,
        sort_order=_PROVENANCE_SORT_ORDER,
    )
    source_identifier: Mapped[str | None] = mapped_column(
        sa.Text(),
        nullable=True,
        sort_order=_PROVENANCE_SORT_ORDER + 1,
    )
    source_url: Mapped[str | None] = mapped_column(
        sa.Text(),
        nullable=True,
        sort_order=_PROVENANCE_SORT_ORDER + 2,
    )
    source_timestamp: Mapped[datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=True,
        sort_order=_PROVENANCE_SORT_ORDER + 3,
    )
    data_origin: Mapped[DataOrigin] = mapped_column(
        DATA_ORIGIN_ENUM,
        nullable=False,
        sort_order=_PROVENANCE_SORT_ORDER + 4,
    )

    @declared_attr
    @classmethod
    def ingestion_run_id(cls) -> Mapped[UUID | None]:
        """FK to the pipeline execution that wrote the row.

        ``ON DELETE SET NULL``: pruning old ingestion runs is routine housekeeping and must
        never take the ingested data with it. The row keeps its ``source`` and ``ingested_at``,
        so provenance survives even when the run record does not.
        """
        return mapped_column(
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("data_ingestion_runs.id", ondelete="SET NULL"),
            nullable=True,
            index=False,
            sort_order=_PROVENANCE_SORT_ORDER + 5,
        )

    ingested_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        sort_order=_PROVENANCE_SORT_ORDER + 6,
    )
