{{ config(severity='warn') }}

/*
    Invariant: the streaming consumer's running totals on a trip agree with the same quantities
    recomputed from the telemetry points that are still in the database.

    `trips.energy_kwh` / `trips.distance_m` are maintained incrementally by the Kafka consumer as
    batches land (BUILD_SPEC §8). `int_trip_energy` derives the same numbers from scratch by
    differencing `cumulative_energy_kwh` and `odometer_m` over a window. Two independent paths to
    one number is the whole point: when they agree, both are probably right; when they drift,
    something in the pipeline lost or double-counted a batch.

    **Severity is `warn`, not `error`, and that is a considered choice.** The consumer is
    at-least-once: it writes telemetry and updates the trip row in separate transactions and
    commits offsets only after the database write. A trip that is *running while dbt builds* is
    therefore expected to disagree — telemetry for the newest batch is in, the trip update is
    not. Failing the build on that would make `dbt build` non-deterministic against a live
    simulator, and a test that cries wolf during normal operation is a test people learn to
    ignore. Only **finished** trips are checked here, and even those only warn; with
    `store_failures` on, the offending rows land in
    `analytics_test_failures.assert_trip_energy_reconciles` where a trend is visible.

    Why a finished trip could still diverge:

    * **A lost batch.** The consumer dies between the telemetry insert and the trip update. The
      points are in the table; the trip row is short by that batch.
    * **A double-counted batch.** At-least-once redelivery re-applies an increment to the trip
      row. `telemetry` tolerates the duplicate rows because it is append-only and the recompute
      differences a monotone counter — but an incremental `+=` on the trip row does not.
    * **A counter reset mid-trip.** The producer restarts and `cumulative_energy_kwh` goes back
      to zero inside a trip. `int_trip_energy` excludes those steps and counts them in
      `anomalous_step_count`; the consumer's running total will have absorbed the reset.

    Both tolerances must be breached before a row is reported: an absolute one for short trips,
    where 0.2 kWh is a large share of a small number, and a relative one for long trips, where
    0.5 kWh is noise. Either alone would produce a stream of uninteresting rows.

    Returns one row per diverging finished trip, with both figures and the gap between them.
*/

with finished_trips as (

    select
        trip_id,
        vehicle_id,
        started_at,
        ended_at,
        telemetry_point_count,
        anomalous_step_count,
        reported_energy_kwh,
        measured_energy_kwh,
        energy_divergence_kwh,
        energy_divergence_percent,
        reported_distance_km,
        measured_distance_km
    from {{ ref('fct_trips') }}
    where not is_running
      -- A finished trip whose telemetry never arrived has nothing to reconcile *against*, so
      -- comparing it here would report a missing-telemetry problem as an energy-divergence one.
      -- It stays visible as `telemetry_point_count = 0` on `fct_trips`.
      and telemetry_point_count > 0
      and measured_energy_kwh is not null

)

select *
from finished_trips
where abs(energy_divergence_kwh) > {{ var('trip_energy_tolerance_kwh') }}
  and (
      reported_energy_kwh <= 0
      or abs(energy_divergence_percent) > {{ var('trip_energy_tolerance_percent') }}
  )
