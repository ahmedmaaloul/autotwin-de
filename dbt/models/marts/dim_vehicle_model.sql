/*
    The five generic EV profiles of BUILD_SPEC §9, plus the derived constants that every other
    model would otherwise re-derive.

    These figures are public, class-level order-of-magnitude values — a "compact EV", not any
    particular manufacturer's car. That is a deliberate design constraint of the project
    (BUILD_SPEC §9) and the `is_generic` flag carries it into the warehouse.

    Reads `autotwin_raw.vehicle_models` directly rather than through a staging model, because
    BUILD_SPEC §16 fixes the staging layer to eight named models and this table is not one of
    them. It is the least-bad option: the alternative is a ninth staging model the contract does
    not allow, or a mart that cannot resolve a trip's vehicle.

    Grain: one row per vehicle profile (five rows after migration `0002`).
*/

with source as (

    select * from {{ source('autotwin_raw', 'vehicle_models') }}

),

renamed as (

    select
        id                                  as vehicle_model_id,
        code                                as vehicle_model_code,
        display_name,
        vehicle_class::text                 as vehicle_class,

        battery_capacity_kwh,
        usable_capacity_kwh,
        nominal_consumption_kwh_100km,

        max_dc_power_kw,
        max_ac_power_kw,

        mass_kg,
        drag_coefficient,
        frontal_area_m2,
        rolling_resistance,

        is_generic

    from source

),

derived as (

    select
        *,

        -- `drag_area` (cd · A) is feature 17 of BUILD_SPEC §10.2 and appears in the aero term
        -- of the physical model. Computing it once here keeps the definition in one place.
        round((drag_coefficient * frontal_area_m2)::numeric, 4)::double precision as drag_area_m2,

        -- Share of the nameplate battery the vehicle is actually allowed to use. The buffer is
        -- what makes `usable_capacity_kwh` the only capacity a range calculation may use.
        case
            when battery_capacity_kwh > 0
                then round((100.0 * usable_capacity_kwh / battery_capacity_kwh)::numeric, 1)::double precision
        end                                 as usable_capacity_share_percent,

        -- Nominal range: usable energy divided by nominal consumption. A catalogue figure under
        -- catalogue conditions — 20 °C, no headwind, no traffic — and therefore an upper bound,
        -- not a promise. Every mart that reports real consumption reports it against this.
        case
            when nominal_consumption_kwh_100km > 0
                then round((100.0 * usable_capacity_kwh / nominal_consumption_kwh_100km)::numeric, 1)::double precision
        end                                 as nominal_range_km,

        -- 10 → 80 % on DC at the vehicle's peak rate, ignoring the taper of BUILD_SPEC §10.3.
        -- A floor on charging time, useful for sanity-checking a `ChargingPlan`, never a
        -- substitute for running the curve.
        case
            when max_dc_power_kw > 0
                then round((60.0 * 0.7 * usable_capacity_kwh / max_dc_power_kw)::numeric, 1)::double precision
        end                                 as min_dc_charge_10_to_80_min,

        -- Everything in this table is documented public data curated by the project, not a
        -- measurement taken from a source system (BUILD_SPEC §3.1 gives the table no
        -- provenance block). Stating it beats leaving the column absent.
        cast('derived' as text)             as data_origin

    from renamed

)

select * from derived
