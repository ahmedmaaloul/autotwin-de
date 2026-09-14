"""Generic EV profiles (BUILD_SPEC §9) — one source of truth for three consumers.

The Alembic seed migration ``0002_seed_vehicle_models``, the simulator's physics and the ML
feature builder all need the same battery capacities, masses and drag figures. Duplicating
them would let a training set drift away from the vehicles that produced it, so the numbers
live here and the three consumers import them.

The figures are **class-level, public-domain order-of-magnitude values**, not reverse-engineered
OEM data: a "compact EV" is any 58 kWh hatchback, not a specific car. That is a deliberate
scoping decision, documented so nobody mistakes the output for a manufacturer range estimate.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, computed_field

from autotwin_contracts.enums import VehicleClass

__all__ = [
    "GENERIC_VEHICLE_PROFILES",
    "VEHICLE_PROFILES_BY_CODE",
    "VehicleProfile",
    "get_vehicle_profile",
]

_VEHICLE_ID_NAMESPACE: Final[str] = "https://autotwin.de/vehicle-models/"


class VehicleProfile(BaseModel):
    """A row of ``vehicle_models``: the physical parameters of one vehicle class.

    Frozen, because the physical model reads these values on every simulation tick and a
    mutated profile mid-run would make a training set unreproducible.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID | None = Field(
        default=None,
        description="Primary key once persisted; None for the in-code profiles. "
        "Use stable_id when seeding so re-running the migration is idempotent.",
    )
    code: str = Field(
        ...,
        min_length=1,
        description="Stable machine name of the profile, e.g. 'compact_ev'.",
    )
    display_name: str = Field(..., min_length=1, description="Label shown in the UI.")
    vehicle_class: VehicleClass = Field(..., description="Segment this profile represents.")
    battery_capacity_kwh: float = Field(
        ...,
        gt=0.0,
        description="Gross battery capacity in kWh.",
    )
    usable_capacity_kwh: float = Field(
        ...,
        gt=0.0,
        description="Usable capacity in kWh — what SOC is expressed against.",
    )
    nominal_consumption_kwh_100km: float = Field(
        ...,
        gt=0.0,
        description="Reference consumption in kWh/100 km; the denominator of EnergyIntensity.",
    )
    max_dc_power_kw: float = Field(..., gt=0.0, description="Peak DC charging power in kW.")
    max_ac_power_kw: float = Field(..., gt=0.0, description="Peak AC charging power in kW.")
    mass_kg: float = Field(..., gt=0.0, description="Kerb mass in kg, incl. battery.")
    drag_coefficient: float = Field(
        ...,
        gt=0.0,
        description="Aerodynamic drag coefficient c_d (dimensionless).",
    )
    frontal_area_m2: float = Field(..., gt=0.0, description="Frontal area A in m².")
    rolling_resistance: float = Field(
        ...,
        gt=0.0,
        description="Rolling resistance coefficient c_rr (dimensionless).",
    )
    is_generic: bool = Field(
        default=True,
        description="True for the built-in class profiles; false for user-defined vehicles.",
    )

    @computed_field  # type: ignore[prop-decorator]  # mypy: decorated property (pydantic docs)
    @property
    def drag_area(self) -> float:
        """``c_d · A`` in m² — the aerodynamic term of the road-load model and an ML feature."""
        return self.drag_coefficient * self.frontal_area_m2

    @property
    def stable_id(self) -> UUID:
        """Deterministic UUID5 derived from :attr:`code`.

        The seed migration uses it so that inserting the profiles twice is a no-op and so that
        a model artefact trained on one machine refers to the same vehicle rows on another.
        """
        return uuid5(NAMESPACE_URL, f"{_VEHICLE_ID_NAMESPACE}{self.code}")

    def resolved_id(self) -> UUID:
        """The persisted id when known, otherwise the deterministic one."""
        return self.id if self.id is not None else self.stable_id


GENERIC_VEHICLE_PROFILES: Final[tuple[VehicleProfile, ...]] = (
    VehicleProfile(
        code="compact_ev",
        display_name="Kompaktklasse EV",
        vehicle_class=VehicleClass.compact,
        battery_capacity_kwh=58.0,
        usable_capacity_kwh=54.0,
        nominal_consumption_kwh_100km=15.5,
        max_dc_power_kw=120.0,
        max_ac_power_kw=11.0,
        mass_kg=1700.0,
        drag_coefficient=0.27,
        frontal_area_m2=2.20,
        rolling_resistance=0.010,
    ),
    VehicleProfile(
        code="sedan_ev",
        display_name="Limousine EV",
        vehicle_class=VehicleClass.sedan,
        battery_capacity_kwh=77.0,
        usable_capacity_kwh=74.0,
        nominal_consumption_kwh_100km=16.5,
        max_dc_power_kw=205.0,
        max_ac_power_kw=11.0,
        mass_kg=2100.0,
        drag_coefficient=0.23,
        frontal_area_m2=2.30,
        rolling_resistance=0.010,
    ),
    VehicleProfile(
        code="performance_ev",
        display_name="Performance EV",
        vehicle_class=VehicleClass.performance,
        battery_capacity_kwh=93.0,
        usable_capacity_kwh=84.0,
        nominal_consumption_kwh_100km=20.0,
        max_dc_power_kw=270.0,
        max_ac_power_kw=11.0,
        mass_kg=2200.0,
        drag_coefficient=0.25,
        frontal_area_m2=2.35,
        rolling_resistance=0.011,
    ),
    VehicleProfile(
        code="suv_ev",
        display_name="SUV EV",
        vehicle_class=VehicleClass.suv,
        battery_capacity_kwh=84.0,
        usable_capacity_kwh=78.0,
        nominal_consumption_kwh_100km=19.5,
        max_dc_power_kw=175.0,
        max_ac_power_kw=11.0,
        mass_kg=2400.0,
        drag_coefficient=0.29,
        frontal_area_m2=2.65,
        rolling_resistance=0.011,
    ),
    VehicleProfile(
        code="van_ev",
        display_name="Transporter EV",
        vehicle_class=VehicleClass.van,
        battery_capacity_kwh=64.0,
        usable_capacity_kwh=60.0,
        nominal_consumption_kwh_100km=23.0,
        max_dc_power_kw=110.0,
        max_ac_power_kw=11.0,
        mass_kg=2500.0,
        drag_coefficient=0.33,
        frontal_area_m2=3.30,
        rolling_resistance=0.012,
    ),
)
"""The five profiles seeded by migration 0002, in the order of BUILD_SPEC §9."""

VEHICLE_PROFILES_BY_CODE: Final[MappingProxyType[str, VehicleProfile]] = MappingProxyType(
    {profile.code: profile for profile in GENERIC_VEHICLE_PROFILES}
)
"""Lookup by ``code`` — the key the simulator's vehicle mix and the API both use."""


def get_vehicle_profile(code: str) -> VehicleProfile:
    """Return the generic profile for ``code``.

    Raises ``KeyError`` listing the known codes: an unknown vehicle code is a configuration
    mistake, and failing loudly beats silently simulating a compact car.
    """
    try:
        return VEHICLE_PROFILES_BY_CODE[code]
    except KeyError:
        known = ", ".join(sorted(VEHICLE_PROFILES_BY_CODE))
        msg = f"unknown vehicle profile {code!r}; known profiles: {known}"
        raise KeyError(msg) from None
