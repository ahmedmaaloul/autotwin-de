/*
    DWD 10-minute observations, typed and renamed.

    The one piece of cleaning that happens here is the `-999` sentinel: DWD writes it for every
    unmeasured parameter (docs/data/sources.md §2). `autotwin_ingestion` already converts it,
    and this is the warehouse-side backstop — the failure mode is silent rather than loud, so
    it is worth paying for twice. A single surviving -999 in `temperature_c` moves a station's
    daily mean by tens of degrees and nothing anywhere errors.
*/

with source as (

    select * from {{ source('autotwin_raw', 'weather_observations') }}

),

renamed as (

    select
        id                                          as weather_observation_id,
        weather_station_id,
        observed_at,

        st_y(location::geometry)::double precision  as latitude,
        st_x(location::geometry)::double precision  as longitude,
        location                                    as location_geography,

        {{ null_if_missing_sentinel('temperature_c') }}     as temperature_c,
        {{ null_if_missing_sentinel('precipitation_mm') }}  as precipitation_mm,
        {{ null_if_missing_sentinel('wind_speed_ms') }}     as wind_speed_ms,
        {{ null_if_missing_sentinel('wind_gust_ms') }}      as wind_gust_ms,
        {{ null_if_missing_sentinel('humidity_percent') }}  as humidity_percent,
        {{ null_if_missing_sentinel('pressure_hpa') }}      as pressure_hpa,

        condition::text                             as weather_condition,

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
