/*
    Roadworks, closures, incidents and warnings from the Autobahn GmbH feed.

    Cleaning here is confined to whitespace defects that are properties of the source and would
    otherwise poison every `group by road_name` in the layer above (docs/data/sources.md §3):
    roads are published with trailing whitespace (`"A60 "`) and directions with a leading space
    (`" Nürnberg -> München"`).

    No "is it happening right now" logic lives here — that is a function of query time, not of
    the row, and it belongs in `fct_traffic_events` where it is computed once per build and can
    be documented as such.
*/

with source as (

    select * from {{ source('autotwin_raw', 'traffic_events') }}

),

renamed as (

    select
        id                                          as traffic_event_id,
        external_id,

        event_type::text                            as event_type,
        severity::text                              as severity,

        upper(nullif(trim(road_name), ''))          as road_name,
        nullif(trim(direction), '')                 as direction,
        nullif(trim(title), '')                     as title,
        nullif(trim(description), '')               as description,

        st_y(location::geometry)::double precision  as latitude,
        st_x(location::geometry)::double precision  as longitude,
        location                                    as location_geography,
        geometry                                    as extent_geometry,

        starts_at,
        ends_at,
        is_blocked,
        delay_minutes,

        -- Provenance block (BUILD_SPEC §3.1).
        source::text                                as source_system,
        source_identifier,
        source_url,
        source_timestamp,
        data_origin::text                           as data_origin,
        ingestion_run_id,
        ingested_at

    from source

)

select * from renamed
