/*
    Conformed charging-site dimension: one row per physical site, everything a consumer needs
    to describe it without joining anything else.

    It is the join target for `mart_charging_coverage` and the row source behind
    `GET /api/v1/charging/stations`, so it carries three things the staging model does not:

    * the *reconciled* power figures from `int_charging_station_power`, with `power_basis`
      saying which of the register's two contradictory descriptions they rest on;
    * the German state name from the `bundesland_reference` seed, so that a UI never has to
      keep its own code-to-label map;
    * `location_geography`, because the spatial marts and the GeoJSON endpoint both need it and
      re-deriving a point from two floats loses the SRID.

    Note the two category columns. `charging_category` is what the ingestion pipeline stored;
    `effective_charging_category` is the same rule applied to the reconciled power. They differ
    exactly where the register's site-level rating disagrees with its own connector list, and
    keeping both is what makes that disagreement countable instead of arguable.
*/

with stations as (

    select * from {{ ref('stg_charging_stations') }}

),

power as (

    select * from {{ ref('int_charging_station_power') }}

),

bundeslaender as (

    select * from {{ ref('bundesland_reference') }}

),

joined as (

    select
        -- Keys -----------------------------------------------------------------------------
        s.station_id,
        s.external_id,

        -- Descriptive ----------------------------------------------------------------------
        s.operator,
        s.street,
        s.house_number,
        s.postal_code,
        s.city,
        s.bundesland,
        b.label_de                                          as bundesland_label_de,
        s.country,

        -- Geography ------------------------------------------------------------------------
        s.latitude,
        s.longitude,
        s.location_geography,

        -- Lifecycle ------------------------------------------------------------------------
        s.commissioned_on,
        s.commissioned_year,

        -- Power, as the pipeline stored it -------------------------------------------------
        s.charging_points_count                             as declared_connector_count,
        s.max_power_kw                                      as declared_max_power_kw,
        s.total_power_kw                                    as declared_total_power_kw,
        s.charging_category,
        s.is_fast_charger                                   as declared_is_fast_charger,

        -- Power, reconciled against the connector rows -------------------------------------
        p.connector_count,
        p.rated_connector_count,
        p.ac_connector_count,
        p.dc_connector_count,
        p.normal_connector_count,
        p.fast_connector_count,
        p.ultra_fast_connector_count,
        p.connector_types,
        p.has_ccs,
        p.has_type2,
        p.has_chademo,

        p.effective_max_power_kw,
        p.effective_total_power_kw,
        p.effective_charging_category,
        p.is_fast_charger,
        p.is_ultra_fast_charger,
        p.power_basis,
        p.power_disagreement_kw,
        p.connector_count_delta,

        -- True where the stored category and the reconciled one disagree. A count of these is
        -- a data-quality headline, not a defect in this model.
        (s.charging_category is distinct from p.effective_charging_category)
                                                            as has_category_disagreement,

        -- Provenance (BUILD_SPEC §3.1) -----------------------------------------------------
        s.source_system,
        s.source_identifier,
        s.source_url,
        s.source_timestamp,
        s.data_origin,
        s.ingestion_run_id,
        s.ingested_at

    from stations as s
    -- Inner-join semantics would be wrong here: `int_charging_station_power` is built from the
    -- same station spine and always has the row, but a left join states that the dimension's
    -- grain is the station list and nothing else may shrink it.
    left join power as p
        on s.station_id = p.station_id
    left join bundeslaender as b
        on s.bundesland = b.bundesland

)

select * from joined
