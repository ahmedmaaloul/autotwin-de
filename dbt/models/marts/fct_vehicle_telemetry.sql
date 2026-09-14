{{
    config(
        materialized='incremental',
        unique_key='telemetry_id',
        incremental_strategy='delete+insert',
        on_schema_change='fail',
        indexes=[
            {'columns': ['vehicle_id', 'recorded_at'], 'type': 'btree'},
            {'columns': ['recorded_at'], 'type': 'btree'},
            {'columns': ['trip_id'], 'type': 'btree'},
        ],
    )
}}

/*
    The telemetry fact: one row per tick per vehicle, with the vehicle dimension resolved and
    the row-level derivations the analytics endpoints need.

    ### Every row in this table is SIMULATED

    `telemetry` is written by `autotwin_simulator` (BUILD_SPEC §3.2, §0.2). Nothing here was
    measured on a real vehicle. `data_origin` states it on every row, and the column is tested
    to contain nothing but `simulated` — so a chart built on this fact cannot be mistaken for
    fleet reality by anyone who checks.

    ### Why this one model is incremental

    It is the only mart whose input grows without bound: a 40-vehicle simulation at 1 Hz
    produces ~3.5 M rows a day. A full refresh of the layer must stay cheap, and rebuilding
    this table from scratch on every `dbt build` is what would stop that being true.

    The incremental window reaches `telemetry_lookback_minutes` back behind the newest row
    already loaded, rather than starting exactly at it. That covers the Kafka consumer's
    at-least-once behaviour: a batch whose offsets were committed late can land with a
    `recorded_at` behind rows that are already here. `delete+insert` on `telemetry_id` then
    makes re-reading that window idempotent — which the append strategy would not, because the
    same source row would arrive twice.

    `on_schema_change='fail'` is deliberate. A column quietly appearing or vanishing upstream
    must stop the run, not produce a fact table whose history has holes in exactly the columns
    an analysis later reaches for.

    ### Vehicle resolution

    `telemetry.vehicle_id` is the *human* identifier (`ATW-0042`), not a `vehicles.id` UUID.
    The join to `vehicles` is what turns it into a vehicle model, and it is a left join on
    purpose: telemetry that arrives before its vehicle row is committed is a real, transient
    state of an at-least-once pipeline, and dropping those rows would silently shorten a trip.

    Grain: one row per `telemetry_id`.
*/

with telemetry as (

    select * from {{ ref('stg_telemetry') }}

    {% if is_incremental() %}
    -- Only the rows that could still be new. `-infinity` makes the first incremental run after
    -- a truncate behave like a full load instead of selecting nothing.
    where recorded_at >= (
        select coalesce(max(recorded_at), '-infinity'::timestamptz)
               - interval '{{ var('telemetry_lookback_minutes') }} minutes'
        from {{ this }}
    )
    {% endif %}

),

vehicles as (

    select
        vehicle_id                      as vehicle_human_id,
        id                              as vehicle_uuid,
        vehicle_model_id,
        simulation_run_id
    from {{ source('autotwin_raw', 'vehicles') }}

),

vehicle_models as (

    select
        vehicle_model_id,
        vehicle_model_code,
        vehicle_class,
        nominal_consumption_kwh_100km,
        usable_capacity_kwh,
        mass_kg,
        drag_area_m2
    from {{ ref('dim_vehicle_model') }}

),

joined as (

    select
        t.telemetry_id,

        -- Dimension keys -------------------------------------------------------------------
        t.vehicle_id,
        v.vehicle_uuid,
        v.simulation_run_id,
        m.vehicle_model_id,
        m.vehicle_model_code,
        m.vehicle_class,
        t.trip_id,
        t.route_id,

        -- Time -----------------------------------------------------------------------------
        t.recorded_at,
        t.recorded_at::date                             as recorded_date,
        date_trunc('hour', t.recorded_at)               as recorded_hour,

        -- Position -------------------------------------------------------------------------
        t.latitude,
        t.longitude,
        t.location_geography,

        -- Motion ---------------------------------------------------------------------------
        t.speed_kmh,
        t.acceleration_ms2,
        t.heading_deg,
        t.road_class,
        {{ road_class_ordinal('t.road_class') }}        as road_class_ordinal,
        t.speed_limit_kmh,

        -- Over the limit by more than the 5 km/h a speedometer and a map disagree by anyway.
        -- NULL where the segment has no posted limit — which on a German motorway is the
        -- normal case, not a gap in the data.
        case
            when t.speed_limit_kmh is null then null
            else t.speed_kmh > t.speed_limit_kmh + 5.0
        end                                             as is_over_speed_limit,

        -- Battery and climate --------------------------------------------------------------
        t.battery_soc_percent,
        t.battery_temperature_c,
        t.outside_temperature_c,

        -- Auxiliary climate load implied by the outside temperature (BUILD_SPEC §10.1). An
        -- attribution aid for `/api/v1/analytics/energy`, not a measured channel: the simulator
        -- computes its own HVAC term and this reconstructs it from the same anchors.
        round({{ hvac_load_kw('t.outside_temperature_c') }}::numeric, 3)::double precision
                                                        as hvac_load_kw_estimate,

        -- Energy ---------------------------------------------------------------------------
        t.instantaneous_power_kw,
        t.energy_consumption_kwh_100km,
        t.cumulative_energy_kwh,
        t.estimated_range_km,
        t.odometer_m,

        m.nominal_consumption_kwh_100km,

        -- How this tick's consumption compares with what the vehicle class is rated for.
        -- Identical thresholds to `EnergyIntensity.from_ratio` — the macro is the single SQL
        -- copy of that rule (BUILD_SPEC §2).
        {{ energy_intensity('t.energy_consumption_kwh_100km', 'm.nominal_consumption_kwh_100km') }}
                                                        as energy_intensity,
        case
            when m.nominal_consumption_kwh_100km > 0
                then round((t.energy_consumption_kwh_100km / m.nominal_consumption_kwh_100km)::numeric, 4)::double precision
        end                                             as consumption_ratio_to_nominal,

        -- Remaining usable energy implied by the state of charge.
        case
            when m.usable_capacity_kwh is not null
                then round((m.usable_capacity_kwh * t.battery_soc_percent / 100.0)::numeric, 3)::double precision
        end                                             as remaining_energy_kwh,

        -- State ----------------------------------------------------------------------------
        t.vehicle_state,

        -- Provenance -----------------------------------------------------------------------
        t.data_origin,
        t.ingested_at

    from telemetry as t
    left join vehicles as v
        on t.vehicle_id = v.vehicle_human_id
    left join vehicle_models as m
        on v.vehicle_model_id = m.vehicle_model_id

)

select * from joined
