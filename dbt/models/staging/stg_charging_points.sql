/*
    The individual connectors of a charging site.

    `charging_points` is one of the seven tables BUILD_SPEC §3.1 puts the provenance block on,
    and `autotwin_core.db.models.ChargingPoint` carries `ProvenanceMixin` accordingly — so the
    block is surfaced here like everywhere else. It is worth saying why the contract looks
    different: `autotwin_contracts.records.ChargingPointRecord` has no provenance fields,
    because a connector record is a *nested value object* inside a `ChargingStationRecord` and
    inherits the parent's attribution at write time. The row that lands in the database does
    carry its own copy, and a staging model that dropped it would leave `int_charging_station_power`
    rolling up unattributed power figures.

    `power_kw` is passed through as-is, zeros and all — see `stg_charging_stations` for why.
*/

with source as (

    select * from {{ source('autotwin_raw', 'charging_points') }}

),

renamed as (

    select
        id                              as charging_point_id,
        station_id,
        ordinal                         as connector_ordinal,

        connector_type::text            as connector_type,
        current_type::text              as current_type,
        power_kw,

        -- Kept as text: the register publishes it verbatim and it is only ever displayed or
        -- used to trace a row back to the source file.
        nullif(trim(public_key), '')    as public_key,

        -- Provenance block (BUILD_SPEC §3.1), written from the parent site's attribution by
        -- the ingestion pipeline.
        source::text                    as source_system,
        source_identifier,
        source_url,
        source_timestamp,
        data_origin::text               as data_origin,
        ingestion_run_id,
        ingested_at

    from source

)

select * from renamed
