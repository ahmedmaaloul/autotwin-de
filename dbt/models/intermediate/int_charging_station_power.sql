/*
    Per-station power profile, rolled up from the individual connectors.

    Why this model exists: the Ladesäulenregister describes a site's power twice and the two
    descriptions disagree often enough to matter. The site row carries `max_power_kw` (strongest
    connector) and `total_power_kw` (grid connection), while the connector rows carry a rating
    each — published as free text that can hold several values in one cell (`"11; 3,7"`, see
    docs/data/sources.md §1). Sites exist with connectors and no site-level rating, and sites
    exist with a site-level rating and no connector rows at all.

    Rather than picking one silently, this model computes both, reconciles them into a single
    `effective_max_power_kw`, and publishes `power_basis` so every downstream number can say
    where it came from. Everything above this model uses the reconciled value; nothing above it
    has to know that the source was ambiguous.
*/

with points as (

    select * from {{ ref('stg_charging_points') }}

),

stations as (

    select * from {{ ref('stg_charging_stations') }}

),

connector_rollup as (

    select
        station_id,

        count(*)                                                        as connector_count,
        count(power_kw)                                                 as rated_connector_count,
        count(*) filter (where current_type = 'dc')                     as dc_connector_count,
        count(*) filter (where current_type = 'ac')                     as ac_connector_count,

        -- Power classes at connector level. `>= 50 kW` is the fast-charger threshold of
        -- BUILD_SPEC §2; `>= 150 kW` is the ultra-fast (HPC) threshold.
        count(*) filter (where power_kw >= {{ var('fast_charger_min_kw') }})  as fast_connector_count,
        count(*) filter (where power_kw >= {{ var('ultra_fast_min_kw') }})    as ultra_fast_connector_count,
        count(*) filter (where power_kw < 22.0)                               as normal_connector_count,

        max(power_kw)                                                   as connector_max_power_kw,
        -- Sum over rated connectors only: a NULL rating must not count as 0 kW, or a site with
        -- one unrated 300 kW connector would look weaker than a site with two wallboxes.
        sum(power_kw)                                                   as connector_sum_power_kw,
        avg(power_kw)                                                   as connector_avg_power_kw,

        bool_or(connector_type = 'ccs')                                 as has_ccs,
        bool_or(connector_type = 'type2')                               as has_type2,
        bool_or(connector_type = 'chademo')                             as has_chademo,

        -- Stable, readable connector summary for the UI: sorted, de-duplicated, comma joined.
        string_agg(distinct connector_type, ', ' order by connector_type) as connector_types

    from points
    group by station_id

),

reconciled as (

    select
        s.station_id,
        s.external_id,

        -- Provenance taken from the parent site. Connector rows carry their own copy of the
        -- block (BUILD_SPEC §3.1), written from the site's attribution by the pipeline, and
        -- `tests/assert_connector_provenance_matches_station.sql` proves the two still agree —
        -- so reading it from the site here is a choice of grain, not a loss of information.
        s.source_system,
        s.data_origin,
        s.ingested_at,

        s.bundesland,
        s.charging_category                                             as declared_charging_category,
        s.max_power_kw                                                  as declared_max_power_kw,
        s.total_power_kw                                                as declared_total_power_kw,
        s.charging_points_count                                         as declared_connector_count,

        coalesce(c.connector_count, 0)                                  as connector_count,
        coalesce(c.rated_connector_count, 0)                            as rated_connector_count,
        coalesce(c.dc_connector_count, 0)                               as dc_connector_count,
        coalesce(c.ac_connector_count, 0)                               as ac_connector_count,
        coalesce(c.fast_connector_count, 0)                             as fast_connector_count,
        coalesce(c.ultra_fast_connector_count, 0)                       as ultra_fast_connector_count,
        coalesce(c.normal_connector_count, 0)                           as normal_connector_count,

        c.connector_max_power_kw,
        c.connector_sum_power_kw,
        c.connector_avg_power_kw,
        coalesce(c.has_ccs, false)                                      as has_ccs,
        coalesce(c.has_type2, false)                                    as has_type2,
        coalesce(c.has_chademo, false)                                  as has_chademo,
        c.connector_types,

        -- The reconciled figures. `greatest` ignores NULLs in Postgres, so a site with only one
        -- of the two descriptions still resolves.
        greatest(s.max_power_kw, c.connector_max_power_kw)              as effective_max_power_kw,
        coalesce(s.total_power_kw, c.connector_sum_power_kw)            as effective_total_power_kw,

        case
            when s.max_power_kw is not null and c.connector_max_power_kw is not null then 'both'
            when c.connector_max_power_kw is not null then 'connectors'
            when s.max_power_kw is not null then 'site'
            else 'none'
        end                                                             as power_basis,

        -- How far apart the two descriptions are, in kW. Not a test — a legitimate site can
        -- declare a 300 kW grid connection and list a single 150 kW connector — but a useful
        -- column to sort by when the register looks wrong.
        case
            when s.max_power_kw is not null and c.connector_max_power_kw is not null
                then abs(s.max_power_kw - c.connector_max_power_kw)
        end                                                             as power_disagreement_kw,

        -- Whether the connector rows agree with the site's declared connector count. The
        -- register's count is per *Ladepunkt*, while a single cell can expand to several rows,
        -- so a mismatch is expected and only its size is interesting.
        coalesce(c.connector_count, 0) - s.charging_points_count        as connector_count_delta

    from stations as s
    left join connector_rollup as c
        on s.station_id = c.station_id

),

classified as (

    select
        *,
        -- Re-derive the category from the reconciled power so that downstream models can
        -- compare it against what the pipeline stored. Same thresholds as
        -- `ChargingCategory.from_power` — the macro is the single SQL copy of that rule.
        {{ charging_category_from_power('effective_max_power_kw') }}     as effective_charging_category,
        coalesce(effective_max_power_kw >= {{ var('fast_charger_min_kw') }}, false)
                                                                        as is_fast_charger,
        coalesce(effective_max_power_kw >= {{ var('ultra_fast_min_kw') }}, false)
                                                                        as is_ultra_fast_charger
    from reconciled

)

select * from classified
