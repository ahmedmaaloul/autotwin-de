/*
    Simulated vehicle telemetry — the highest-volume table in the schema.

    Every row here is SIMULATED (BUILD_SPEC §0.2, §3.2). `data_origin` is carried explicitly on
    every row rather than assumed, so that a mart built on this model cannot be mistaken for
    measured reality by anyone reading its output.

    Deliberately a view. Materialising it would double the storage of the largest table in the
    database to add nothing but column names; `fct_vehicle_telemetry` is where the layer stops
    being free, and that one is incremental for exactly this reason.
*/

with source as (

    select * from {{ source('autotwin_raw', 'telemetry') }}

),

renamed as (

    select
        id                                          as telemetry_id,

        -- Human ids, as text: telemetry is keyed by the identifiers that travel on the Kafka
        -- message, not by the UUID primary keys of `vehicles` / `trips`.
        vehicle_id,
        trip_id,
        route_id,

        recorded_at,

        st_y(location::geometry)::double precision  as latitude,
        st_x(location::geometry)::double precision  as longitude,
        location                                    as location_geography,

        speed_kmh,
        acceleration_ms2,
        heading_deg,

        battery_soc_percent,
        battery_temperature_c,
        outside_temperature_c,

        instantaneous_power_kw,
        energy_consumption_kwh_100km,
        cumulative_energy_kwh,
        estimated_range_km,

        road_class::text                            as road_class,
        speed_limit_kmh,
        odometer_m,
        state::text                                 as vehicle_state,

        data_origin::text                           as data_origin,
        ingested_at

    from source

)

select * from renamed
