/*
    One simulated journey of one vehicle.

    Naming: `trips` has both a UUID primary key and a text business key called `trip_id`.
    Renaming the business key would be worse than renaming the surrogate one, so the UUID is
    exposed as `trip_uuid` and `trip_id` keeps its meaning across the whole project — it is the
    value telemetry rows and Kafka messages carry. The same rule gives `vehicle_uuid` its name:
    `trips.vehicle_id` is a FK to `vehicles.id`, *not* the human `ATW-0042` identifier that
    `stg_telemetry.vehicle_id` holds. Conflating the two is the single easiest way to produce a
    silently empty join in this schema.

    The aggregate columns (`distance_m`, `energy_kwh`, …) are the streaming consumer's running
    totals. They are passed through unchanged; `int_trip_energy` recomputes the same quantities
    from the raw telemetry and `fct_trips` publishes both plus their divergence.
*/

with source as (

    select * from {{ source('autotwin_raw', 'trips') }}

),

renamed as (

    select
        id                          as trip_uuid,
        trip_id,
        vehicle_id                  as vehicle_uuid,
        route_id,

        started_at,
        ended_at,

        start_soc_percent,
        end_soc_percent,

        distance_m                  as reported_distance_m,
        {{ meters_to_km('distance_m') }} as reported_distance_km,
        energy_kwh                  as reported_energy_kwh,
        avg_consumption_kwh_100km   as reported_avg_consumption_kwh_100km,

        state::text                 as trip_state,

        -- Provenance block (BUILD_SPEC §3.1). A trip is always simulated, but the column is
        -- carried rather than assumed so the honesty rule survives a schema change.
        source::text                as source_system,
        source_identifier,
        source_url,
        source_timestamp,
        data_origin::text           as data_origin,
        ingestion_run_id,
        ingested_at

    from source

)

select * from renamed
