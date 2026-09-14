/*
    Per-trip energy and driving aggregates, recomputed from raw telemetry.

    `trips` already carries running totals maintained by the streaming consumer. This model
    does not trust them: the consumer is at-least-once, updates the trip row in a separate
    transaction from the telemetry insert, and can therefore drift. Recomputing from the points
    gives `fct_trips` a second, independent figure and
    `tests/assert_trip_energy_reconciles.sql` the ability to prove they agree.

    The arithmetic is a difference over a window, and there are two traps in it:

    1. The window is partitioned by `vehicle_id` (per BUILD_SPEC's telemetry grain), so the row
       before the first point of a trip is the *last point of the vehicle's previous trip*.
       Differencing across that boundary would charge one trip's energy to the next one. Every
       step where `prev_trip_id is distinct from trip_id` is therefore discarded.
    2. `cumulative_energy_kwh` and `odometer_m` are monotone counters that reset at a trip
       boundary. A negative step is either a reset that slipped past trap 1 or a producer bug —
       it is nulled out of the sums *and counted* in `anomalous_step_count`, because a metric
       that quietly clamps its own bad inputs cannot be audited.

    Grain: one row per `trip_id`. Everything here is SIMULATED data.
*/

with telemetry as (

    select *
    from {{ ref('stg_telemetry') }}
    -- Idle vehicles emit telemetry without a trip; those points belong to no journey.
    where trip_id is not null

),

sequenced as (

    select
        telemetry_id,
        vehicle_id,
        trip_id,
        recorded_at,
        speed_kmh,
        acceleration_ms2,
        battery_soc_percent,
        battery_temperature_c,
        outside_temperature_c,
        cumulative_energy_kwh,
        odometer_m,
        road_class,

        lag(trip_id)                over vehicle_window as prev_trip_id,
        lag(recorded_at)            over vehicle_window as prev_recorded_at,
        lag(cumulative_energy_kwh)  over vehicle_window as prev_cumulative_energy_kwh,
        lag(odometer_m)             over vehicle_window as prev_odometer_m,

        -- `telemetry_id` breaks ties: two ticks can share a timestamp at second resolution and
        -- an unstable order would make first/last SOC non-deterministic between runs.
        row_number() over (partition by trip_id order by recorded_at asc, telemetry_id asc)   as point_rank_asc,
        row_number() over (partition by trip_id order by recorded_at desc, telemetry_id desc) as point_rank_desc

    from telemetry
    window vehicle_window as (partition by vehicle_id order by recorded_at asc, telemetry_id asc)

),

steps as (

    select
        *,

        case
            when prev_trip_id is distinct from trip_id then null
            else extract(epoch from recorded_at - prev_recorded_at)
        end                                                  as step_seconds,

        case
            when prev_trip_id is distinct from trip_id then null
            when cumulative_energy_kwh - prev_cumulative_energy_kwh < 0 then null
            else cumulative_energy_kwh - prev_cumulative_energy_kwh
        end                                                  as step_energy_kwh,

        case
            when prev_trip_id is distinct from trip_id then null
            when odometer_m - prev_odometer_m < 0 then null
            else odometer_m - prev_odometer_m
        end                                                  as step_distance_m,

        (
            prev_trip_id is not distinct from trip_id
            and (
                cumulative_energy_kwh - prev_cumulative_energy_kwh < 0
                or odometer_m - prev_odometer_m < 0
            )
        )                                                    as step_is_anomalous

    from sequenced

),

aggregated as (

    select
        trip_id,
        min(vehicle_id)                                                 as vehicle_id,
        count(distinct vehicle_id)                                      as distinct_vehicle_count,

        count(*)                                                        as telemetry_point_count,
        count(*) filter (where step_is_anomalous)                       as anomalous_step_count,

        min(recorded_at)                                                as first_recorded_at,
        max(recorded_at)                                                as last_recorded_at,

        sum(step_distance_m)                                            as distance_m,
        sum(step_energy_kwh)                                            as energy_kwh,
        sum(step_seconds)                                               as tracked_seconds,

        -- Distance driven on a motorway, for the road-mix feature of BUILD_SPEC §10.2.
        sum(step_distance_m) filter (where road_class = 'motorway')     as motorway_distance_m,

        max(battery_soc_percent) filter (where point_rank_asc = 1)      as start_soc_percent,
        max(battery_soc_percent) filter (where point_rank_desc = 1)     as end_soc_percent,
        min(battery_soc_percent)                                        as min_soc_percent,
        max(battery_soc_percent)                                        as max_soc_percent,

        avg(speed_kmh)                                                  as mean_tick_speed_kmh,
        max(speed_kmh)                                                  as max_speed_kmh,
        stddev_samp(speed_kmh)                                          as speed_stddev_kmh,

        avg(abs(acceleration_ms2))                                      as acceleration_abs_mean_ms2,
        -- "Acceleration event" = |a| above 1 m/s², the threshold the ML feature
        -- `accel_events_per_km` (BUILD_SPEC §10.2) counts.
        count(*) filter (where abs(acceleration_ms2) > 1.0)             as acceleration_event_count,

        avg(outside_temperature_c)                                      as mean_outside_temperature_c,
        min(outside_temperature_c)                                      as min_outside_temperature_c,
        avg(battery_temperature_c)                                      as mean_battery_temperature_c

    from steps
    group by trip_id

),

derived as (

    select
        trip_id,
        vehicle_id,
        distinct_vehicle_count,
        telemetry_point_count,
        anomalous_step_count,

        first_recorded_at,
        last_recorded_at,
        extract(epoch from last_recorded_at - first_recorded_at)::double precision as elapsed_seconds,
        -- Summed durations of the steps that stayed inside the trip. Equal to `elapsed_seconds`
        -- minus any gap the vehicle spent outside this trip, so it is the time the trip was
        -- actually being tracked — standing still included.
        tracked_seconds,

        distance_m,
        {{ meters_to_km('distance_m') }}                                as distance_km,
        energy_kwh,
        motorway_distance_m,

        case
            when distance_m > 0
                then round((100.0 * motorway_distance_m / distance_m)::numeric, 1)::double precision
        end                                                             as motorway_distance_share_percent,

        -- kWh per 100 km. Guarded: a trip whose first telemetry batch has not landed has zero
        -- distance, and dividing by it would poison every fleet average downstream.
        case
            when distance_m > 0
                then round((energy_kwh / (distance_m / 1000.0) * 100.0)::numeric, 3)::double precision
        end                                                             as avg_consumption_kwh_100km,

        case
            when distance_m > 0
                then round((acceleration_event_count / (distance_m / 1000.0))::numeric, 3)::double precision
        end                                                             as accel_events_per_km,

        -- Distance-over-time average speed, which is the honest one; `mean_tick_speed_kmh` is
        -- the average of the samples and is biased upwards whenever ticks are emitted more
        -- often while moving than while standing.
        case
            when tracked_seconds > 0
                then round(((distance_m / 1000.0) / (tracked_seconds / 3600.0))::numeric, 2)::double precision
        end                                                             as avg_speed_kmh,
        mean_tick_speed_kmh,
        max_speed_kmh,
        speed_stddev_kmh,

        acceleration_abs_mean_ms2,
        acceleration_event_count,

        start_soc_percent,
        end_soc_percent,
        min_soc_percent,
        max_soc_percent,
        start_soc_percent - end_soc_percent                             as soc_used_percent,

        mean_outside_temperature_c,
        min_outside_temperature_c,
        mean_battery_temperature_c

    from aggregated

)

select * from derived
