/*
    Charging sites from the Bundesnetzagentur Ladesäulenregister, typed and renamed.

    Staging rules (dbt/README.md): rename, cast, light clean. No joins, no business logic.
    In particular the power columns are passed through *untouched* even where they are zero or
    negative — `tests/assert_positive_charging_power.sql` exists precisely to catch those, and
    a staging model that quietly repaired them would make the test unfalsifiable.

    Coordinates are unpacked into plain `double precision` columns so that no downstream model
    has to know PostGIS. The `location` geography itself is passed through as well, for the
    three models that legitimately do spatial work (`int_corridor_stations`,
    `mart_charging_coverage`, `mart_traffic_impact`).
*/

with source as (

    select * from {{ source('autotwin_raw', 'charging_stations') }}

),

renamed as (

    select
        id                                          as station_id,
        external_id,

        -- The register's text columns arrive padded and occasionally empty rather than NULL.
        nullif(trim(operator), '')                  as operator,
        nullif(trim(street), '')                    as street,
        nullif(trim(house_number), '')              as house_number,

        -- German postal codes are five digits and the leading zero is meaningful (01067
        -- Dresden). Any upstream step that round-trips the column through an integer loses it;
        -- re-padding is safe because a PLZ is never longer than five characters.
        case
            when nullif(trim(postal_code), '') is null then null
            when length(trim(postal_code)) < 5 then lpad(trim(postal_code), 5, '0')
            else trim(postal_code)
        end                                         as postal_code,

        nullif(trim(city), '')                      as city,
        bundesland::text                            as bundesland,
        upper(nullif(trim(country), ''))            as country,

        -- ST_X is longitude and ST_Y is latitude. The cast to `geometry` is required because
        -- the accessors are not defined on `geography`.
        st_y(location::geometry)::double precision   as latitude,
        st_x(location::geometry)::double precision   as longitude,
        location                                    as location_geography,

        commissioned_on,
        extract(year from commissioned_on)::int     as commissioned_year,

        charging_points_count,
        max_power_kw,
        total_power_kw,
        charging_category::text                     as charging_category,
        is_fast_charger,

        -- Provenance block (BUILD_SPEC §3.1). `source` is a reserved-ish word in enough
        -- dialects that it is worth renaming once, here.
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
