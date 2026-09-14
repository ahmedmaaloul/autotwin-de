"""seed generic EV profiles

Revision ID: 0002
Revises: 0001
Created: 2026-09-14

The five rows inserted here are *generic vehicle-class profiles*, not manufacturer data.
They describe a compact EV, a sedan EV, and so on, using publicly documented
order-of-magnitude figures for the class. Nothing here is reverse-engineered from an OEM
and nothing here should be read as a specification of any particular car.

The numbers live in exactly one place — ``autotwin_contracts.vehicles.GENERIC_VEHICLE_PROFILES``
(BUILD_SPEC §9) — and are imported from there so the simulator, the ML feature builder and this
seed cannot drift apart. The contracts package is a pure-Pydantic leaf with no database
dependency, so importing it into a migration is safe in a way that importing the ORM models
would not be: models change with the code, migrations must keep describing the past.

Each row's ``id`` is a UUIDv5 derived from its ``code``, which makes the seed idempotent
across machines and lets ML artefacts reference a profile by a stable identifier.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from autotwin_contracts.vehicles import GENERIC_VEHICLE_PROFILES

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# A lightweight table definition, deliberately NOT the ORM model: a migration must keep
# working after the model has moved on.
vehicle_models = sa.table(
    "vehicle_models",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("code", sa.Text),
    sa.column("display_name", sa.Text),
    sa.column(
        "vehicle_class",
        postgresql.ENUM(
            "compact", "sedan", "performance", "suv", "van", name="vehicle_class", create_type=False
        ),
    ),
    sa.column("battery_capacity_kwh", sa.Float),
    sa.column("usable_capacity_kwh", sa.Float),
    sa.column("nominal_consumption_kwh_100km", sa.Float),
    sa.column("max_dc_power_kw", sa.Float),
    sa.column("max_ac_power_kw", sa.Float),
    sa.column("mass_kg", sa.Float),
    sa.column("drag_coefficient", sa.Float),
    sa.column("frontal_area_m2", sa.Float),
    sa.column("rolling_resistance", sa.Float),
    sa.column("is_generic", sa.Boolean),
)


def upgrade() -> None:
    op.bulk_insert(
        vehicle_models,
        [
            {
                "id": profile.resolved_id(),
                "code": profile.code,
                "display_name": profile.display_name,
                "vehicle_class": profile.vehicle_class.value,
                "battery_capacity_kwh": profile.battery_capacity_kwh,
                "usable_capacity_kwh": profile.usable_capacity_kwh,
                "nominal_consumption_kwh_100km": profile.nominal_consumption_kwh_100km,
                "max_dc_power_kw": profile.max_dc_power_kw,
                "max_ac_power_kw": profile.max_ac_power_kw,
                "mass_kg": profile.mass_kg,
                "drag_coefficient": profile.drag_coefficient,
                "frontal_area_m2": profile.frontal_area_m2,
                "rolling_resistance": profile.rolling_resistance,
                "is_generic": True,
            }
            for profile in GENERIC_VEHICLE_PROFILES
        ],
    )


def downgrade() -> None:
    codes = [profile.code for profile in GENERIC_VEHICLE_PROFILES]
    op.execute(
        sa.delete(vehicle_models).where(sa.column("code", sa.Text).in_(codes))  # type: ignore[arg-type]
    )
