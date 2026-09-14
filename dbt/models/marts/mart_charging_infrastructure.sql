/*
    Charging infrastructure per federal state — the per-Bundesland comparison behind
    `GET /api/v1/analytics/regions` and `GET /api/v1/charging/statistics`.

    ### The spine is the seed, not the data

    The sixteen rows come from `bundesland_reference`, left-joined to the stations. A state with
    no charging sites therefore appears with zeros instead of vanishing from the comparison —
    which is the whole point of a regional mart, and something `group by bundesland` can never
    do.

    ### What is deliberately not counted

    Stations whose `bundesland` the register's spelling could not be resolved to are **excluded**:
    there is no honest state to attribute them to, and a seventeenth "unknown" row would be read
    as a region. That means `sum(station_count)` over this mart is the *resolved* national total,
    not the national total — and `share_of_resolved_stations_percent` says so in its name.
    The unresolved rows are a data-quality finding and surface through
    `GET /api/v1/data/quality`, where they belong.

    ### Density, median and p90

    Absolute counts only ever say "Bayern is large". `stations_per_1000_km2` is what makes
    Bremen and Bayern comparable. The median and p90 of site power describe the *shape* of a
    state's network rather than its size: two states with identical station counts and very
    different p90 figures have built very different things, and only the percentiles show it.

    All power figures are the reconciled ones from `int_charging_station_power`, so they survive
    the register's habit of describing a site's power twice and disagreeing with itself.

    Grain: one row per Bundesland — always exactly sixteen.
*/

with bundeslaender as (

    select * from {{ ref('bundesland_reference') }}

),

stations as (

    select * from {{ ref('dim_charging_station') }}
    -- See the header: a site without a resolvable state is a quality finding, not a region.
    where bundesland is not null

),

station_rollup as (

    select
        bundesland,

        -- Sites ----------------------------------------------------------------------------
        count(*)                                                            as station_count,
        count(*) filter (where is_fast_charger)                             as fast_station_count,
        count(*) filter (where is_ultra_fast_charger)                       as ultra_fast_station_count,
        count(*) filter (where effective_charging_category = 'normal')      as normal_station_count,
        count(distinct operator)                                            as operator_count,

        -- Connectors -----------------------------------------------------------------------
        sum(connector_count)                                                as charging_point_count,
        sum(fast_connector_count)                                           as fast_charging_point_count,
        sum(ultra_fast_connector_count)                                     as ultra_fast_charging_point_count,
        sum(ac_connector_count)                                             as ac_charging_point_count,
        sum(dc_connector_count)                                             as dc_charging_point_count,

        -- Power. `effective_total_power_kw` is the site's grid connection where the register
        -- states one and the sum of its connectors otherwise; falling back to the strongest
        -- connector keeps a single-connector site from contributing zero.
        sum(coalesce(effective_total_power_kw, effective_max_power_kw))     as installed_power_kw,
        percentile_cont(0.5) within group (order by effective_max_power_kw) as median_station_power_kw,
        percentile_cont(0.9) within group (order by effective_max_power_kw) as p90_station_power_kw,
        max(effective_max_power_kw)                                         as max_station_power_kw,

        -- Coverage of the power figures themselves: how much of the above rests on something
        -- the register actually published.
        count(*) filter (where power_basis = 'none')                        as stations_without_power_count,
        count(*) filter (where has_category_disagreement)                   as stations_with_category_disagreement_count,

        -- Commissioning --------------------------------------------------------------------
        count(*) filter (where commissioned_on is not null)                 as stations_with_commissioning_date_count,
        min(commissioned_on)                                                as first_commissioned_on,
        max(commissioned_on)                                                as latest_commissioned_on,
        count(*) filter (where commissioned_on >= current_date - interval '12 months')
                                                                            as stations_commissioned_last_12m,
        count(*) filter (where commissioned_on >= current_date - interval '36 months')
                                                                            as stations_commissioned_last_36m,

        -- Provenance -----------------------------------------------------------------------
        count(*) filter (where data_origin = 'official')                    as official_station_count,
        max(ingested_at)                                                    as latest_ingested_at

    from stations
    group by bundesland

),

commissioning_by_year as (

    select
        bundesland,
        commissioned_year,
        count(*) as station_count
    from stations
    where commissioned_year is not null
    group by bundesland, commissioned_year

),

commissioning_series as (

    select
        bundesland,
        -- The growth curve as `{"2019": 118, "2020": 240, ...}`. A JSONB object rather than one
        -- row per year because the grain of this mart is the state: a (state, year) grain would
        -- make every density and percentile column meaningless.
        jsonb_object_agg(commissioned_year::text, station_count)    as stations_by_commissioning_year
    from commissioning_by_year
    group by bundesland

),

peak_year as (

    select distinct on (bundesland)
        bundesland,
        commissioned_year   as peak_commissioning_year,
        station_count       as peak_commissioning_year_station_count
    from commissioning_by_year
    order by bundesland, station_count desc, commissioned_year desc

),

assembled as (

    select
        b.bundesland,
        b.label_de                                              as bundesland_label_de,
        b.area_km2,

        coalesce(r.station_count, 0)                            as station_count,
        coalesce(r.fast_station_count, 0)                       as fast_station_count,
        coalesce(r.ultra_fast_station_count, 0)                 as ultra_fast_station_count,
        coalesce(r.normal_station_count, 0)                     as normal_station_count,
        coalesce(r.operator_count, 0)                           as operator_count,

        coalesce(r.charging_point_count, 0)                     as charging_point_count,
        coalesce(r.fast_charging_point_count, 0)                as fast_charging_point_count,
        coalesce(r.ultra_fast_charging_point_count, 0)          as ultra_fast_charging_point_count,
        coalesce(r.ac_charging_point_count, 0)                  as ac_charging_point_count,
        coalesce(r.dc_charging_point_count, 0)                  as dc_charging_point_count,

        round(coalesce(r.installed_power_kw, 0)::numeric, 1)::double precision
                                                                as installed_power_kw,
        round(r.median_station_power_kw::numeric, 1)::double precision  as median_station_power_kw,
        round(r.p90_station_power_kw::numeric, 1)::double precision     as p90_station_power_kw,
        r.max_station_power_kw,

        coalesce(r.stations_without_power_count, 0)             as stations_without_power_count,
        coalesce(r.stations_with_category_disagreement_count, 0)
                                                                as stations_with_category_disagreement_count,

        -- Density. The numbers that make a small state comparable with a large one.
        round((1000.0 * coalesce(r.station_count, 0) / b.area_km2)::numeric, 2)::double precision
                                                                as stations_per_1000_km2,
        round((1000.0 * coalesce(r.fast_station_count, 0) / b.area_km2)::numeric, 2)::double precision
                                                                as fast_stations_per_1000_km2,
        round((1000.0 * coalesce(r.charging_point_count, 0) / b.area_km2)::numeric, 2)::double precision
                                                                as charging_points_per_1000_km2,
        round((1000.0 * coalesce(r.installed_power_kw, 0) / b.area_km2)::numeric, 1)::double precision
                                                                as installed_kw_per_1000_km2,

        case
            when coalesce(r.station_count, 0) > 0
                then round((100.0 * r.fast_station_count / r.station_count)::numeric, 1)::double precision
        end                                                     as fast_station_share_percent,
        case
            when coalesce(r.charging_point_count, 0) > 0
                then round((100.0 * r.fast_charging_point_count / r.charging_point_count)::numeric, 1)::double precision
        end                                                     as fast_charging_point_share_percent,
        case
            when coalesce(r.station_count, 0) > 0
                then round((r.charging_point_count::numeric / r.station_count), 2)::double precision
        end                                                     as mean_connectors_per_station,

        -- Growth ---------------------------------------------------------------------------
        coalesce(r.stations_with_commissioning_date_count, 0)   as stations_with_commissioning_date_count,
        coalesce(r.stations_commissioned_last_12m, 0)           as stations_commissioned_last_12m,
        coalesce(r.stations_commissioned_last_36m, 0)           as stations_commissioned_last_36m,
        r.first_commissioned_on,
        r.latest_commissioned_on,
        p.peak_commissioning_year,
        p.peak_commissioning_year_station_count,
        coalesce(c.stations_by_commissioning_year, '{}'::jsonb) as stations_by_commissioning_year,

        -- Sites added in the last twelve months against the stock that existed before them.
        -- NULL — not 0 — where there is no prior stock to grow from, because "infinite growth
        -- from nothing" is not a rate.
        case
            when coalesce(r.stations_with_commissioning_date_count, 0)
                 - coalesce(r.stations_commissioned_last_12m, 0) > 0
                then round((
                    100.0 * r.stations_commissioned_last_12m
                    / (r.stations_with_commissioning_date_count - r.stations_commissioned_last_12m)
                )::numeric, 1)::double precision
        end                                                     as expansion_rate_12m_percent,

        -- Provenance -----------------------------------------------------------------------
        coalesce(r.official_station_count, 0)                   as official_station_count,
        r.latest_ingested_at

    from bundeslaender as b
    left join station_rollup as r
        on b.bundesland = r.bundesland
    left join commissioning_series as c
        on b.bundesland = c.bundesland
    left join peak_year as p
        on b.bundesland = p.bundesland

),

ranked as (

    select
        *,

        -- Shares are of the *resolved* national population — see the header.
        case
            when sum(station_count) over () > 0
                then round((100.0 * station_count / sum(station_count) over ())::numeric, 2)::double precision
        end                                                     as share_of_resolved_stations_percent,
        case
            when sum(installed_power_kw) over () > 0
                then round((100.0 * installed_power_kw / sum(installed_power_kw) over ())::numeric, 2)::double precision
        end                                                     as share_of_resolved_power_percent,

        rank() over (order by station_count desc)               as rank_by_station_count,
        rank() over (order by stations_per_1000_km2 desc)       as rank_by_station_density,
        rank() over (order by installed_power_kw desc)          as rank_by_installed_power,
        rank() over (order by coalesce(fast_station_share_percent, -1) desc)
                                                                as rank_by_fast_station_share

    from assembled

)

select * from ranked
