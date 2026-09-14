/*
    A route cut into analysis chunks, with the per-segment inputs and the energy prediction the
    API produced for it.

    `route_segments` has no provenance block (BUILD_SPEC §3.1 does not list it) because there is
    no external source to attribute a segment to: AutoTwin cuts the line, looks up the weather
    and the traffic, and runs the energy model. That makes every row `derived` by construction,
    and the constant below states it rather than leaving the column absent — a mart that joins
    segments to stations must not end up with a NULL in the one column the honesty rule depends
    on. It is the only literal `data_origin` in the staging layer.
*/

with source as (

    select * from {{ source('autotwin_raw', 'route_segments') }}

),

renamed as (

    select
        id                                          as route_segment_id,
        route_id,
        ordinal                                     as segment_ordinal,

        geometry                                    as segment_geometry,

        start_offset_m,
        {{ meters_to_km('start_offset_m') }}        as start_offset_km,
        distance_m                                  as segment_distance_m,
        {{ meters_to_km('distance_m') }}            as segment_distance_km,

        road_class::text                            as road_class,
        speed_limit_kmh,
        assumed_speed_kmh,
        elevation_gain_m,
        temperature_c,
        traffic_severity::text                      as traffic_severity,

        predicted_kwh_per_100km,
        predicted_kwh,
        energy_intensity::text                      as energy_intensity,

        -- See the header: derived by construction, not by attribution.
        cast('derived' as text)                     as data_origin

    from source

)

select * from renamed
