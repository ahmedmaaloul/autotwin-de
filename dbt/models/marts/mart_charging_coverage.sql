/*
    Charging coverage of each demo corridor, and the gap analysis behind
    `GET /api/v1/charging/coverage` and `GET /api/v1/charging/underserved` (BUILD_SPEC §7.2).

    ### The question

    "Can an EV actually drive this corridor?" is not answered by counting stations. A route with
    forty fast chargers in its first 100 km and none in its last 200 km is worse than a route
    with ten evenly spaced ones, and only a *gap* measurement can tell them apart. The headline
    number here is therefore `max_gap_km`: the longest stretch of the corridor with no fast
    charger on it.

    ### How the gaps are built

    `int_corridor_stations` has already projected every station in the corridor onto the route
    with `ST_LineLocatePoint`, in the metric grid, giving each one an offset in kilometres from
    the origin. This model:

    1. keeps the stations that reach the fast-charger threshold (`fast_charger_min_kw`, 50 kW —
       below that a stop is a hotel, not a refuelling);
    2. orders them by offset and takes `lead()` to get the distance to the next one — the
       *interior* gaps;
    3. adds the two **edge gaps** nobody remembers: origin → first fast charger, and last fast
       charger → destination. Leaving them out is the classic error in this analysis. A corridor
       whose only two fast chargers sit at km 10 and km 12 has one interior gap of 2 km and
       looks immaculate, while the 280 km after km 12 are the entire problem;
    4. represents a corridor with **no** fast charger at all as a single gap the length of the
       route, so that the worst case is a large number rather than a NULL that sorts last.

    ### Two lengths, and which column uses which

    Gaps and offsets are measured along the route *geometry* in ETRS89 / UTM 32N
    (`route_geometry_length_km`), because that is the line the stations were projected onto.
    Densities quoted per 100 km use the routing engine's *driving* distance
    (`route_distance_km`), because that is the number the API and the UI show. The two differ by
    a few per mille — a polyline is a chord approximation of the road — and mixing them inside a
    single figure is how a gap analysis stops adding up to its own route. Both are published.

    ### Score

    `coverage_score` (0-100) is a weighted blend of the gap and density targets set in
    `dbt_project.yml`. It is a planning heuristic, not a standard, and `methodology` on every row
    spells out the exact parameters it was computed with so the UI never has to describe it from
    memory.

    Grain: one row per demo route.
*/

with routes as (

    select
        route_id,
        route_slug,
        route_name,
        origin_name,
        destination_name,
        distance_km             as route_distance_km,
        geometry_length_km      as route_geometry_length_km,
        duration_min            as route_duration_min
    from {{ ref('dim_route') }}
    -- The corridor product is built on the seeded demo routes; ad-hoc routes planned through
    -- `/api/v1/routes/plan` are not persisted as analysis subjects.
    where is_demo

),

corridor as (

    select * from {{ ref('int_corridor_stations') }}

),

-- ------------------------------------------------------------------------------------------
-- Station-level rollup: how much charging sits in the corridor at all.
-- ------------------------------------------------------------------------------------------
corridor_rollup as (

    select
        route_id,

        count(*)                                                            as station_count,
        count(*) filter (where is_fast_charger)                             as fast_station_count,
        count(*) filter (where is_ultra_fast_charger)                       as ultra_fast_station_count,
        count(distinct operator)                                            as operator_count,

        sum(connector_count)                                                as charging_point_count,
        sum(fast_connector_count)                                           as fast_charging_point_count,
        sum(ultra_fast_connector_count)                                     as ultra_fast_charging_point_count,

        sum(coalesce(effective_total_power_kw, effective_max_power_kw))     as installed_power_kw,
        max(effective_max_power_kw)                                         as max_station_power_kw,
        percentile_cont(0.5) within group (order by effective_max_power_kw) as median_station_power_kw,

        count(*) filter (where has_ccs)                                     as ccs_station_count,
        min(distance_to_route_km)                                           as nearest_station_distance_km,
        avg(distance_to_route_km)                                           as mean_station_distance_km

    from corridor
    group by route_id

),

-- ------------------------------------------------------------------------------------------
-- Gap analysis over the fast chargers only.
-- ------------------------------------------------------------------------------------------
fast_stations as (

    select
        route_id,
        station_id,
        route_offset_km,
        effective_max_power_kw
    from corridor
    where is_fast_charger

),

ordered_fast_stations as (

    select
        route_id,
        station_id,
        route_offset_km,
        -- `station_id` breaks ties: two sites at the same motorway junction can project to the
        -- same offset, and an unstable order would make the gap list differ between runs.
        lead(route_offset_km) over (
            partition by route_id
            order by route_offset_km asc, station_id asc
        )                                           as next_offset_km
    from fast_stations

),

interior_gaps as (

    select
        route_id,
        route_offset_km                             as gap_start_km,
        next_offset_km                              as gap_end_km,
        next_offset_km - route_offset_km            as gap_km,
        'between'                                   as gap_kind
    from ordered_fast_stations
    where next_offset_km is not null

),

fast_station_bounds as (

    select
        route_id,
        min(route_offset_km)    as first_fast_offset_km,
        max(route_offset_km)    as last_fast_offset_km
    from fast_stations
    group by route_id

),

edge_gaps as (

    -- Origin → first fast charger.
    select
        r.route_id,
        0.0::double precision                       as gap_start_km,
        b.first_fast_offset_km                      as gap_end_km,
        b.first_fast_offset_km                      as gap_km,
        'origin'                                    as gap_kind
    from routes as r
    inner join fast_station_bounds as b
        on r.route_id = b.route_id

    union all

    -- Last fast charger → destination. `greatest(0, …)` guards the case where a station
    -- projects a metre past the end of the line after both lengths are rounded; a negative
    -- gap would otherwise poison the mean.
    select
        r.route_id,
        b.last_fast_offset_km                       as gap_start_km,
        r.route_geometry_length_km                  as gap_end_km,
        greatest(0.0, r.route_geometry_length_km - b.last_fast_offset_km) as gap_km,
        'destination'                               as gap_kind
    from routes as r
    inner join fast_station_bounds as b
        on r.route_id = b.route_id

),

uncovered_routes as (

    -- A corridor with no fast charger at all is one gap the length of the route. Representing
    -- it as a number rather than as an absent row is what keeps it at the top of a
    -- "worst corridors" sort instead of at the bottom.
    select
        r.route_id,
        0.0::double precision                       as gap_start_km,
        r.route_geometry_length_km                  as gap_end_km,
        r.route_geometry_length_km                  as gap_km,
        'whole_route'                               as gap_kind
    from routes as r
    where not exists (
        select 1 from fast_stations as f where f.route_id = r.route_id
    )

),

all_gaps as (

    select * from interior_gaps
    union all
    select * from edge_gaps
    union all
    select * from uncovered_routes

),

gap_rollup as (

    select
        route_id,
        count(*)                                                    as gap_count,
        -- The completeness invariant of the whole decomposition: interior gaps plus the two
        -- edge gaps must tile the route exactly once, so this has to equal the route's geometry
        -- length. `_models.yml` asserts it to 10 m. It is what catches a missing edge gap, a
        -- double-counted station and an offset measured against the wrong length — three
        -- mistakes that all leave every other column in this model looking perfectly plausible.
        sum(gap_km)                                                 as gap_total_km,
        max(gap_km)                                                 as max_gap_km,
        avg(gap_km)                                                 as mean_gap_km,
        percentile_cont(0.9) within group (order by gap_km)         as p90_gap_km,
        count(*) filter (where gap_km > {{ var('target_max_gap_km') }})
                                                                    as gaps_above_target_count
    from all_gaps
    group by route_id

),

largest_gap as (

    select distinct on (route_id)
        route_id,
        gap_start_km    as largest_gap_start_km,
        gap_end_km      as largest_gap_end_km,
        gap_km          as largest_gap_km,
        gap_kind        as largest_gap_kind
    from all_gaps
    order by route_id, gap_km desc, gap_start_km asc

),

-- ------------------------------------------------------------------------------------------
-- Assembly and scoring.
-- ------------------------------------------------------------------------------------------
assembled as (

    select
        r.route_id,
        r.route_slug,
        r.route_name,
        r.origin_name,
        r.destination_name,
        r.route_distance_km,
        r.route_geometry_length_km,
        r.route_duration_min,

        -- Analysis parameters, carried on the row so a stored result can always be reproduced.
        {{ meters_to_km(var('corridor_buffer_m') ~ '::double precision') }}  as corridor_buffer_km,
        {{ var('fast_charger_min_kw') }}::double precision      as fast_charger_min_kw,
        {{ var('target_max_gap_km') }}::double precision        as target_max_gap_km,
        {{ var('target_fast_stations_per_100km') }}::double precision
                                                                as target_fast_stations_per_100km,

        coalesce(c.station_count, 0)                            as station_count,
        coalesce(c.fast_station_count, 0)                       as fast_station_count,
        coalesce(c.ultra_fast_station_count, 0)                 as ultra_fast_station_count,
        coalesce(c.ccs_station_count, 0)                        as ccs_station_count,
        coalesce(c.operator_count, 0)                           as operator_count,

        coalesce(c.charging_point_count, 0)                     as charging_point_count,
        coalesce(c.fast_charging_point_count, 0)                as fast_charging_point_count,
        coalesce(c.ultra_fast_charging_point_count, 0)          as ultra_fast_charging_point_count,

        round(coalesce(c.installed_power_kw, 0)::numeric, 1)::double precision
                                                                as installed_power_kw,
        c.max_station_power_kw,
        round(c.median_station_power_kw::numeric, 1)::double precision       as median_station_power_kw,
        round(c.nearest_station_distance_km::numeric, 3)::double precision   as nearest_station_distance_km,
        round(c.mean_station_distance_km::numeric, 3)::double precision      as mean_station_distance_km,

        -- Densities against the routing engine's driving distance — see the header.
        round((100.0 * coalesce(c.station_count, 0) / r.route_distance_km)::numeric, 2)::double precision
                                                                as stations_per_100km,
        round((100.0 * coalesce(c.fast_station_count, 0) / r.route_distance_km)::numeric, 2)::double precision
                                                                as fast_stations_per_100km,
        round((100.0 * coalesce(c.fast_charging_point_count, 0) / r.route_distance_km)::numeric, 2)::double precision
                                                                as fast_charging_points_per_100km,

        -- Gaps along the geometry — see the header.
        coalesce(g.gap_count, 0)                                as gap_count,
        round(g.gap_total_km::numeric, 3)::double precision     as gap_total_km,
        round(g.max_gap_km::numeric, 3)::double precision       as max_gap_km,
        round(g.mean_gap_km::numeric, 3)::double precision      as mean_gap_km,
        round(g.p90_gap_km::numeric, 3)::double precision       as p90_gap_km,
        coalesce(g.gaps_above_target_count, 0)                  as gaps_above_target_count,
        round(l.largest_gap_start_km::numeric, 3)::double precision  as largest_gap_start_km,
        round(l.largest_gap_end_km::numeric, 3)::double precision    as largest_gap_end_km,
        round(l.largest_gap_km::numeric, 3)::double precision        as largest_gap_km,
        l.largest_gap_kind,

        current_timestamp                                       as generated_at

    from routes as r
    left join corridor_rollup as c
        on r.route_id = c.route_id
    left join gap_rollup as g
        on r.route_id = g.route_id
    left join largest_gap as l
        on r.route_id = l.route_id

),

scored as (

    select
        *,

        /*
            Two sub-scores, each 0-100 and each saturating at its target:

            * gap_score     = target / max(max_gap, target). 100 when the worst gap is inside
              the target, halving as the worst gap doubles past it.
            * density_score = fast stations per 100 km against the target density, capped.

            Weighted 60/40 towards the gap, because a single long gap makes a corridor
            undriveable while a thin-but-even network merely makes it slow. AFIR (EU 2023/1804)
            requires a >= 150 kW pool every 60 km on the TEN-T core network; the 50 km default
            in `dbt_project.yml` is the stricter internal planning target.
        */
        round((
            0.6 * (100.0 * {{ var('target_max_gap_km') }}
                   / greatest(max_gap_km, {{ var('target_max_gap_km') }}))
            + 0.4 * least(
                100.0,
                100.0 * fast_stations_per_100km / {{ var('target_fast_stations_per_100km') }}
            )
        )::numeric, 1)::double precision                        as coverage_score,

        -- BUILD_SPEC §7.2 requires the API to be able to state how a coverage number was
        -- produced. Building the sentence here keeps it impossible for the wording and the
        -- parameters to drift apart.
        format(
            'Ladestationen im %s-km-Korridor der Route; Lücken entlang der Streckengeometrie '
            || '(EPSG:%s) zwischen Standorten mit mindestens %s kW, einschließlich der Abschnitte '
            || 'vor dem ersten und nach dem letzten Schnelllader. Zielwerte: maximale Lücke '
            || '%s km, %s Schnellladestandorte je 100 km.',
            corridor_buffer_km,
            {{ var('germany_metric_srid') }},
            fast_charger_min_kw,
            target_max_gap_km,
            target_fast_stations_per_100km
        )                                                       as methodology

    from assembled

)

select * from scored
