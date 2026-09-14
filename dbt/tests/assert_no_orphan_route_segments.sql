/*
    Invariant: every route segment belongs to a route that exists, and it describes a stretch
    that lies inside that route.

    This goes deliberately further than the `relationships` test on `route_id`. A segment can be
    orphaned in two ways, and only one of them is a missing foreign key:

    1. **No parent row.** `route_segments.route_id` points at a route that is not there.
    2. **No parent geometry.** The parent exists, but the segment claims a stretch the route does
       not have — it starts past the end of the route, or its start-plus-length overruns the
       route's own distance. The key is intact and the row is still meaningless.

    The second kind is the one that costs something. `mart_route_energy` sums `predicted_kwh`
    per route, and `dim_route.segmentation_coverage_percent` compares the segment lengths against
    the route's distance; a segment hanging off the end inflates both without breaking either.

    Why it could break:

    * **Re-routing under a fixed id.** A route re-planned against a different OSRM build gets a
      new, slightly different geometry. `route_segments` cascades on delete, not on update, so
      segments written against the previous geometry survive a re-plan that only rewrites the
      route row.
    * **Segmentation against the wrong length.** `autotwin_core.geo.segmentation` cuts the line
      into chunks; feeding it the routing engine's *driving* distance while cutting the
      *geometry* (a few per mille shorter) walks the last segment past the end of the line.
    * **A partially applied analysis.** `/api/v1/routes/analyze` writes segments and updates the
      route in separate statements. An interrupted run can leave segments whose parent was never
      committed.

    The 1 % tolerance on the overrun check is not a fudge: the two distances legitimately differ
    (see `dim_route`), and anything inside that margin is the polyline-versus-road difference
    rather than a defect.

    Returns one row per offending segment, with the reason.
*/

with segments as (

    select * from {{ ref('stg_route_segments') }}

),

routes as (

    select
        route_id,
        distance_m      as route_distance_m,
        distance_km     as route_distance_km
    from {{ ref('stg_routes') }}

),

missing_parent as (

    select
        'missing_parent_route'                      as violation,
        s.route_segment_id,
        s.route_id,
        s.segment_ordinal,
        s.start_offset_km,
        s.segment_distance_km,
        null::double precision                      as route_distance_km
    from segments as s
    left join routes as r
        on s.route_id = r.route_id
    where r.route_id is null

),

starts_past_end as (

    select
        'segment_starts_past_route_end'             as violation,
        s.route_segment_id,
        s.route_id,
        s.segment_ordinal,
        s.start_offset_km,
        s.segment_distance_km,
        r.route_distance_km
    from segments as s
    inner join routes as r
        on s.route_id = r.route_id
    where s.start_offset_m > r.route_distance_m * 1.01

),

overruns_end as (

    select
        'segment_overruns_route_end'                as violation,
        s.route_segment_id,
        s.route_id,
        s.segment_ordinal,
        s.start_offset_km,
        s.segment_distance_km,
        r.route_distance_km
    from segments as s
    inner join routes as r
        on s.route_id = r.route_id
    where s.start_offset_m + s.segment_distance_m > r.route_distance_m * 1.01

)

select * from missing_parent
union all
select * from starts_past_end
union all
select * from overruns_end
