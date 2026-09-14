/*
    Conformed corridor dimension: one row per route, with the shape of its segmentation
    summarised onto it.

    Two distances live side by side here and they are not the same number:

    * `distance_km` is what the routing engine drove — the figure the API and the UI quote.
    * `geometry_length_km` is the length of the returned line measured in the German metric
      grid. It comes out a few per mille shorter, because a polyline is a chord approximation
      of the road it represents.

    Every offset in `mart_charging_coverage` is measured along the *geometry*, so the two are
    kept apart and named apart. Quoting one and measuring in the other is how a gap analysis
    ends up not adding up to its own route.

    `segments_distance_km` is a third distance: the sum of the segment lengths. It should track
    `geometry_length_km` closely, and `segmentation_coverage_percent` is what says whether it
    does — a route whose segments cover 80 % of its own geometry has a broken segmentation, and
    the energy total built on it is 20 % short without anything erroring.

    Grain: one row per route (demo and ad-hoc alike).
*/

with routes as (

    select * from {{ ref('stg_routes') }}

),

segments as (

    select * from {{ ref('stg_route_segments') }}

),

segment_rollup as (

    select
        route_id,

        count(*)                                                        as segment_count,
        sum(segment_distance_m)                                         as segments_distance_m,
        min(segment_ordinal)                                            as first_segment_ordinal,
        max(segment_ordinal)                                            as last_segment_ordinal,

        sum(segment_distance_m) filter (where road_class = 'motorway')   as motorway_distance_m,
        sum(segment_distance_m) filter (where road_class in ('trunk', 'primary'))
                                                                        as trunk_primary_distance_m,
        sum(segment_distance_m) filter (where road_class in ('secondary', 'tertiary', 'residential', 'service'))
                                                                        as minor_road_distance_m,

        avg(speed_limit_kmh)                                            as mean_speed_limit_kmh,
        avg(assumed_speed_kmh)                                          as mean_assumed_speed_kmh,
        sum(elevation_gain_m)                                           as elevation_gain_m,

        count(*) filter (where predicted_kwh is not null)                as analysed_segment_count,
        count(*) filter (where traffic_severity is not null)             as segments_with_traffic_count

    from segments
    group by route_id

),

joined as (

    select
        -- Keys -----------------------------------------------------------------------------
        r.route_id,
        r.slug                                          as route_slug,
        r.name                                          as route_name,

        -- Endpoints ------------------------------------------------------------------------
        r.origin_name,
        r.destination_name,
        r.origin_latitude,
        r.origin_longitude,
        r.destination_latitude,
        r.destination_longitude,

        -- Geometry -------------------------------------------------------------------------
        r.route_geometry,
        {{ meters_to_km('st_length(' ~ to_metric_crs('r.route_geometry') ~ ')') }}
                                                        as geometry_length_km,

        -- Routing engine figures -----------------------------------------------------------
        r.distance_m,
        r.distance_km,
        r.duration_s,
        r.duration_min,
        r.routing_profile,
        case
            when r.duration_s > 0
                then round((r.distance_m / 1000.0 / (r.duration_s / 3600.0))::numeric, 1)::double precision
        end                                             as free_flow_avg_speed_kmh,

        r.is_demo,

        -- Segmentation ---------------------------------------------------------------------
        coalesce(s.segment_count, 0)                    as segment_count,
        coalesce(s.analysed_segment_count, 0)           as analysed_segment_count,
        coalesce(s.segments_with_traffic_count, 0)      as segments_with_traffic_count,
        s.first_segment_ordinal,
        s.last_segment_ordinal,
        {{ meters_to_km('s.segments_distance_m') }}     as segments_distance_km,

        case
            when s.segments_distance_m > 0
                then round((100.0 * coalesce(s.motorway_distance_m, 0) / s.segments_distance_m)::numeric, 1)::double precision
        end                                             as motorway_distance_share_percent,
        case
            when s.segments_distance_m > 0
                then round((100.0 * coalesce(s.trunk_primary_distance_m, 0) / s.segments_distance_m)::numeric, 1)::double precision
        end                                             as trunk_primary_distance_share_percent,
        case
            when s.segments_distance_m > 0
                then round((100.0 * coalesce(s.minor_road_distance_m, 0) / s.segments_distance_m)::numeric, 1)::double precision
        end                                             as minor_road_distance_share_percent,

        round(s.mean_speed_limit_kmh::numeric, 1)::double precision      as mean_speed_limit_kmh,
        round(s.mean_assumed_speed_kmh::numeric, 1)::double precision    as mean_assumed_speed_kmh,
        s.elevation_gain_m,

        -- How much of the route the segmentation actually accounts for. 100 % is the healthy
        -- value; anything materially below it means segments were lost.
        case
            when r.distance_m > 0 and s.segments_distance_m is not null
                then round((100.0 * s.segments_distance_m / r.distance_m)::numeric, 1)::double precision
        end                                             as segmentation_coverage_percent,

        -- Provenance (BUILD_SPEC §3.1) -----------------------------------------------------
        r.source_system,
        r.source_identifier,
        r.source_url,
        r.source_timestamp,
        r.data_origin,
        r.ingestion_run_id,
        r.ingested_at

    from routes as r
    -- Left join: a route that has not been analysed yet has no segments and still belongs in
    -- the dimension. `segment_count = 0` is the honest statement of that.
    left join segment_rollup as s
        on r.route_id = s.route_id

)

select * from joined
