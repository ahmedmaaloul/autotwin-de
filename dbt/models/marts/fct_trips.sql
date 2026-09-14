/*
    The trip fact: one row per completed or running journey, with both accounts of its energy.

    ### Every row in this table is SIMULATED

    Trips exist because `autotwin_simulator` drove them. The `reported_*` and `measured_*`
    columns are both simulation output; `measured_` means "recomputed from the telemetry
    stream", never "measured on a vehicle".

    ### Two accounts, on purpose

    * `reported_*` — the running totals the streaming consumer maintains on the `trips` row as
      batches land (BUILD_SPEC §8).
    * `measured_*` — the same quantities recomputed from the raw telemetry points by
      `int_trip_energy`, with counter resets and cross-trip window steps excluded.

    They are produced by different code paths in different transactions, so they can drift: a
    consumer restart between the telemetry insert and the trip update leaves the trip row
    behind. Publishing both and the delta between them turns that drift into a number anyone
    can look at, and `tests/assert_trip_energy_reconciles.sql` is what watches it.

    The `measured_*` figures are the ones downstream analytics should use — they are
    reproducible from the points that are still in the database.

    Grain: one row per `trip_id`.
*/

with trips as (

    select * from {{ ref('stg_trips') }}

),

trip_energy as (

    select * from {{ ref('int_trip_energy') }}

),

vehicles as (

    select
        id                  as vehicle_uuid,
        vehicle_id          as vehicle_human_id,
        vehicle_model_id,
        simulation_run_id
    from {{ source('autotwin_raw', 'vehicles') }}

),

vehicle_models as (

    select
        vehicle_model_id,
        vehicle_model_code,
        vehicle_class,
        display_name        as vehicle_display_name,
        nominal_consumption_kwh_100km,
        usable_capacity_kwh,
        nominal_range_km
    from {{ ref('dim_vehicle_model') }}

),

routes as (

    select
        route_id,
        route_slug,
        route_name,
        distance_km         as route_distance_km,
        is_demo             as route_is_demo
    from {{ ref('dim_route') }}

),

joined as (

    select
        -- Keys -----------------------------------------------------------------------------
        t.trip_uuid,
        t.trip_id,
        t.vehicle_uuid,
        v.vehicle_human_id                              as vehicle_id,
        v.simulation_run_id,
        m.vehicle_model_id,
        m.vehicle_model_code,
        m.vehicle_class,
        m.vehicle_display_name,
        t.route_id,
        r.route_slug,
        r.route_name,
        r.route_distance_km,
        r.route_is_demo,

        -- Timing ---------------------------------------------------------------------------
        t.started_at,
        t.ended_at,
        t.started_at::date                              as started_date,
        case
            when t.ended_at is not null
                then round((extract(epoch from t.ended_at - t.started_at) / 60.0)::numeric, 2)::double precision
        end                                             as duration_min,
        (t.ended_at is null)                            as is_running,
        t.trip_state,

        -- Telemetry coverage ---------------------------------------------------------------
        coalesce(e.telemetry_point_count, 0)            as telemetry_point_count,
        coalesce(e.anomalous_step_count, 0)             as anomalous_step_count,
        e.first_recorded_at,
        e.last_recorded_at,
        e.tracked_seconds,

        -- Distance and energy, as the consumer reported them --------------------------------
        t.reported_distance_m,
        t.reported_distance_km,
        t.reported_energy_kwh,
        t.reported_avg_consumption_kwh_100km,

        -- Distance and energy, recomputed from the telemetry points ------------------------
        e.distance_m                                    as measured_distance_m,
        e.distance_km                                   as measured_distance_km,
        e.energy_kwh                                    as measured_energy_kwh,
        e.avg_consumption_kwh_100km                     as measured_avg_consumption_kwh_100km,

        -- Divergence between the two accounts ----------------------------------------------
        round((e.energy_kwh - t.reported_energy_kwh)::numeric, 3)::double precision
                                                        as energy_divergence_kwh,
        case
            when t.reported_energy_kwh > 0
                then round((100.0 * (e.energy_kwh - t.reported_energy_kwh) / t.reported_energy_kwh)::numeric, 2)::double precision
        end                                             as energy_divergence_percent,
        round((e.distance_m - t.reported_distance_m)::numeric, 1)::double precision
                                                        as distance_divergence_m,

        -- Energy quality -------------------------------------------------------------------
        m.nominal_consumption_kwh_100km,
        {{ energy_intensity('e.avg_consumption_kwh_100km', 'm.nominal_consumption_kwh_100km') }}
                                                        as energy_intensity,
        case
            when m.nominal_consumption_kwh_100km > 0
                then round((e.avg_consumption_kwh_100km / m.nominal_consumption_kwh_100km)::numeric, 4)::double precision
        end                                             as consumption_ratio_to_nominal,

        -- Share of the vehicle's usable battery this trip consumed. The honest range metric:
        -- it uses the recomputed energy, not the SOC delta, because a trip that charged
        -- mid-way has a misleading SOC delta.
        case
            when m.usable_capacity_kwh > 0
                then round((100.0 * e.energy_kwh / m.usable_capacity_kwh)::numeric, 1)::double precision
        end                                             as usable_battery_consumed_percent,

        -- State of charge --------------------------------------------------------------------
        t.start_soc_percent                             as reported_start_soc_percent,
        t.end_soc_percent                               as reported_end_soc_percent,
        e.start_soc_percent                             as measured_start_soc_percent,
        e.end_soc_percent                               as measured_end_soc_percent,
        e.min_soc_percent,
        e.soc_used_percent,
        -- Negative `soc_used_percent` means the vehicle gained charge over the journey, which
        -- on a long corridor means it stopped to charge. Not an error — a fact worth flagging.
        coalesce(e.soc_used_percent < 0, false)         as charged_during_trip,

        -- Driving behaviour (the aggregate features of BUILD_SPEC §10.2) -------------------
        e.avg_speed_kmh,
        e.max_speed_kmh,
        e.speed_stddev_kmh,
        e.acceleration_abs_mean_ms2,
        e.accel_events_per_km,
        e.motorway_distance_share_percent,

        -- Conditions -----------------------------------------------------------------------
        e.mean_outside_temperature_c,
        e.min_outside_temperature_c,
        e.mean_battery_temperature_c,

        -- Provenance (BUILD_SPEC §3.1) -----------------------------------------------------
        t.source_system,
        t.source_identifier,
        t.data_origin,
        t.ingestion_run_id,
        t.ingested_at

    from trips as t
    -- Left join: a trip whose telemetry has not arrived yet is still a trip. Its `measured_*`
    -- columns are NULL, which is a readable statement of "no points yet" — an inner join would
    -- make the trip disappear instead.
    left join trip_energy as e
        on t.trip_id = e.trip_id
    left join vehicles as v
        on t.vehicle_uuid = v.vehicle_uuid
    left join vehicle_models as m
        on v.vehicle_model_id = m.vehicle_model_id
    left join routes as r
        on t.route_id = r.route_id

)

select * from joined
