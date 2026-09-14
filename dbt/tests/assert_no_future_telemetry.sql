/*
    Invariant: nothing in the simulated fleet is recorded in the future. A telemetry tick, a trip
    start and a trip end are all observations of something that has already happened.

    Why it could break. This is the failure mode the simulator's own design invites, and it is
    the reason `autotwin_core.quality.rules.timestamp_not_future` exists:

    * **Speed factor.** The simulator runs at `SIM_SPEED_FACTOR` (default 10×, BUILD_SPEC §5):
      one wall-clock second advances ten simulated seconds. If the clock that stamps
      `recorded_at` is the *simulated* one rather than the wall clock, every run drifts into the
      future at nine seconds per second — and the first symptom is a dashboard whose "latest
      reading" is tomorrow.
    * **Naive datetimes.** BUILD_SPEC §14 requires `tzinfo=UTC` everywhere. A naive local
      timestamp written into a `timestamptz` column is interpreted as UTC, which in German summer
      time puts every row two hours ahead.
    * **Backfills.** `generate-training-data` (BUILD_SPEC §15) synthesises trips over a time
      range; an off-by-one on the range end produces future rows in bulk.

    The two-minute tolerance absorbs clock skew between the container writing the row and the
    container running dbt. It is deliberately small: anything larger would let the speed-factor
    bug run for a while before this fired.

    Returns one row per offending timestamp, with how far into the future it sits.
*/

{% set future_tolerance_minutes = 2 %}

with telemetry_violations as (

    select
        'fct_vehicle_telemetry'                     as source_model,
        'recorded_at'                               as offending_field,
        vehicle_id                                  as entity_id,
        trip_id,
        recorded_at                                 as offending_timestamp,
        round((extract(epoch from recorded_at - current_timestamp) / 60.0)::numeric, 2)
                                                    as minutes_in_future
    from {{ ref('fct_vehicle_telemetry') }}
    where recorded_at > current_timestamp + interval '{{ future_tolerance_minutes }} minutes'

),

trip_start_violations as (

    select
        'fct_trips'                                 as source_model,
        'started_at'                                as offending_field,
        vehicle_id                                  as entity_id,
        trip_id,
        started_at                                  as offending_timestamp,
        round((extract(epoch from started_at - current_timestamp) / 60.0)::numeric, 2)
                                                    as minutes_in_future
    from {{ ref('fct_trips') }}
    where started_at > current_timestamp + interval '{{ future_tolerance_minutes }} minutes'

),

trip_end_violations as (

    select
        'fct_trips'                                 as source_model,
        'ended_at'                                  as offending_field,
        vehicle_id                                  as entity_id,
        trip_id,
        ended_at                                    as offending_timestamp,
        round((extract(epoch from ended_at - current_timestamp) / 60.0)::numeric, 2)
                                                    as minutes_in_future
    from {{ ref('fct_trips') }}
    where ended_at > current_timestamp + interval '{{ future_tolerance_minutes }} minutes'

)

select * from telemetry_violations
union all
select * from trip_start_violations
union all
select * from trip_end_violations
