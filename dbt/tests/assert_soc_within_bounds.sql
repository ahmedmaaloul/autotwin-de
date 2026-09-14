/*
    Invariant: a state of charge is a percentage of usable battery capacity and therefore lies
    in [0, 100] — on every telemetry tick and at both ends of every trip.

    Why it could break. SOC is integrated by the simulator rather than measured, so it is the
    output of an accumulator: `soc -= energy_step / usable_capacity * 100` on every tick. Three
    things push it out of range, and none of them raises anything on its own.

    * A vehicle driven past empty. The physics loop does not stop at zero unless the state
      machine transitions it first, so a missed `VehicleState` transition produces a negative
      SOC a few ticks later.
    * A charging curve that overshoots. `P(soc)` tapers towards 100 % (BUILD_SPEC §10.3); a
      tick long enough to add more than the remaining headroom lands above 100.
    * A vehicle profile whose `usable_capacity_kwh` is wrong or zero, which makes every step
      of the accumulator wrong by a constant factor.

    All three produce numbers that look fine in a chart and are physically impossible, which is
    why this is a hard error rather than a warning. `autotwin_core.quality.rules.soc_in_range`
    enforces the same bound on the ingestion side; this is the warehouse-side half of the pair.

    Returns one row per violation, naming the model, the entity, and the offending value.
*/

with telemetry_violations as (

    select
        'fct_vehicle_telemetry'             as source_model,
        'battery_soc_percent'               as offending_field,
        vehicle_id                          as entity_id,
        trip_id,
        recorded_at                         as observed_at,
        battery_soc_percent                 as offending_value
    from {{ ref('fct_vehicle_telemetry') }}
    where battery_soc_percent is null
       or battery_soc_percent < 0
       or battery_soc_percent > 100

),

trip_start_violations as (

    select
        'fct_trips'                         as source_model,
        'measured_start_soc_percent'        as offending_field,
        vehicle_id                          as entity_id,
        trip_id,
        started_at                          as observed_at,
        measured_start_soc_percent          as offending_value
    from {{ ref('fct_trips') }}
    -- NULL is legitimate here: a trip whose first telemetry batch has not landed has no
    -- measured SOC yet. Only a present-but-impossible value is a violation.
    where measured_start_soc_percent is not null
      and (measured_start_soc_percent < 0 or measured_start_soc_percent > 100)

),

trip_end_violations as (

    select
        'fct_trips'                         as source_model,
        'measured_end_soc_percent'          as offending_field,
        vehicle_id                          as entity_id,
        trip_id,
        coalesce(ended_at, started_at)      as observed_at,
        measured_end_soc_percent            as offending_value
    from {{ ref('fct_trips') }}
    where measured_end_soc_percent is not null
      and (measured_end_soc_percent < 0 or measured_end_soc_percent > 100)

)

select * from telemetry_violations
union all
select * from trip_start_violations
union all
select * from trip_end_violations
