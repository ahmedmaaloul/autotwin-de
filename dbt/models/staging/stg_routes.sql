/*
    Planned corridors: geometry, distance and duration from OSRM.

    `distance_m` is the routing engine's driving distance and is deliberately kept separate
    from the length of `geometry`, which is a geodesic measurement of the same line and comes
    out a few per mille shorter. `int_corridor_stations` needs the geometric length (a station
    is projected onto the line, so its offset has to be measured along that same line); the API
    and the UI quote the routing distance. Mixing them produces a gap analysis that does not
    add up to the route length, which is exactly the kind of defect nobody notices.
*/

with source as (

    select * from {{ source('autotwin_raw', 'routes') }}

),

renamed as (

    select
        id                                              as route_id,
        nullif(trim(slug), '')                          as slug,
        nullif(trim(name), '')                          as name,
        nullif(trim(origin_name), '')                   as origin_name,
        nullif(trim(destination_name), '')              as destination_name,

        st_y(origin::geometry)::double precision        as origin_latitude,
        st_x(origin::geometry)::double precision        as origin_longitude,
        st_y(destination::geometry)::double precision   as destination_latitude,
        st_x(destination::geometry)::double precision   as destination_longitude,

        geometry                                        as route_geometry,

        distance_m,
        {{ meters_to_km('distance_m') }}                as distance_km,
        duration_s,
        round((duration_s / 60.0)::numeric, 1)::double precision as duration_min,

        routing_profile,
        is_demo,

        -- Provenance block (BUILD_SPEC §3.1).
        source::text                                    as source_system,
        source_identifier,
        source_url,
        source_timestamp,
        data_origin::text                               as data_origin,
        ingestion_run_id,
        ingested_at

    from source

)

select * from renamed
