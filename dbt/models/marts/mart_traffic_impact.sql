/*
    Traffic disruption aggregated by road, kind and severity, with the demo corridors it lands
    on — the numbers behind the traffic panel of the dashboard and the `traffic_penalty` story
    of a route analysis.

    Grain: one row per (`road_name`, `event_type`, `severity`).

    ### Matching events to corridors

    An event is attributed to a route segment when its reported point lies within
    `traffic_match_buffer_m` (2 km by default) of that segment. Two km is not arbitrary: the
    Autobahn feed reports an incident at the nearest kilometre marker rather than at its true
    extent (docs/data/sources.md §3), and a tighter buffer drops real matches while a looser one
    starts bleeding onto the parallel B-road.

    `ST_DWithin` is evaluated geography-to-geography so the buffer is true metres on the
    spheroid, with the indexed `traffic_events.location` column on the left of the predicate so
    the GIST index of BUILD_SPEC §3.2 can serve it.

    The event's `geometry` extent is deliberately **not** used for matching: it is a
    `LineString` describing the affected stretch, present on nearly every Autobahn item, and
    matching on it would attribute a 30 km roadworks corridor to every segment it passes — which
    inflates `affected_segment_count` into a measure of event length rather than of corridor
    impact. The point is the location the source actually asserts.

    ### Reported delay versus implied delay

    `total_reported_delay_minutes` sums only what the source states, and
    `delay_reporting_coverage_percent` says how much of the group that is — usually very little,
    because the Autobahn feed almost never publishes a delay. `severity_delay_factor` is the
    coarse multiplier from `TrafficSeverity.delay_factor` and is a *category-derived* number, not
    a measurement. They are kept in separate columns for that reason; averaging them together
    would produce a figure with no defined meaning.

    ### "Active now" is a build-time snapshot

    `active_event_count` is computed from `fct_traffic_events.is_active_now`, which is frozen at
    build time. `evaluated_at` is carried through so a consumer can see how stale the verdict is.
    For a live answer the API queries `traffic_events` directly (BUILD_SPEC §7, `?active_only`).
*/

with events as (

    select * from {{ ref('fct_traffic_events') }}

),

segments as (

    select
        route_segment_id,
        route_id,
        segment_geometry,
        segment_distance_km
    from {{ ref('stg_route_segments') }}

),

matches as (

    select
        e.traffic_event_id,
        e.road_name,
        e.event_type,
        e.severity,
        s.route_id,
        s.route_segment_id,
        s.segment_distance_km
    from events as e
    inner join segments as s
        -- Indexed geography column first: see the header.
        on st_dwithin(
            e.location_geography,
            s.segment_geometry::geography,
            {{ var('traffic_match_buffer_m') }}
        )

),

-- Distinct segments per group, so that a segment hit by three events is counted once.
group_segment_pairs as (

    select distinct
        road_name,
        event_type,
        severity,
        route_id,
        route_segment_id,
        segment_distance_km
    from matches

),

corridor_rollup as (

    select
        road_name,
        event_type,
        severity,
        count(distinct route_id)        as affected_route_count,
        count(*)                        as affected_segment_count,
        sum(segment_distance_km)        as affected_segment_distance_km
    from group_segment_pairs
    group by road_name, event_type, severity

),

matched_event_rollup as (

    select
        road_name,
        event_type,
        severity,
        count(distinct traffic_event_id) as matched_event_count
    from matches
    group by road_name, event_type, severity

),

event_rollup as (

    select
        road_name,
        event_type,
        severity,

        -- Counts ----------------------------------------------------------------------------
        count(*)                                                as event_count,
        count(*) filter (where is_active_now)                   as active_event_count,
        count(*) filter (where is_upcoming)                     as upcoming_event_count,
        count(*) filter (where is_expired)                      as expired_event_count,
        count(*) filter (where is_blocked)                      as blocked_event_count,
        count(*) filter (where has_extent_geometry)             as events_with_extent_count,
        count(distinct direction)                               as direction_count,

        -- Delay, as reported by the source ------------------------------------------------
        count(*) filter (where has_reported_delay)              as events_with_reported_delay_count,
        sum(delay_minutes)                                      as total_reported_delay_minutes,
        avg(delay_minutes)                                      as mean_reported_delay_minutes,
        max(delay_minutes)                                      as max_reported_delay_minutes,

        -- Timing ----------------------------------------------------------------------------
        min(starts_at)                                          as earliest_starts_at,
        max(ends_at)                                            as latest_ends_at,
        avg(planned_duration_hours)                             as mean_planned_duration_hours,
        count(*) filter (where is_open_ended)                   as open_ended_event_count,
        max(evaluated_at)                                       as evaluated_at,

        -- Provenance -------------------------------------------------------------------------
        string_agg(distinct source_system, ', ' order by source_system) as source_systems,
        string_agg(distinct data_origin, ', ' order by data_origin)     as data_origins,
        max(ingested_at)                                        as latest_ingested_at

    from events
    group by road_name, event_type, severity

),

assembled as (

    select
        -- Group keys. `road_name` is NULL for the events the feed publishes without a road,
        -- which is a real group and not a defect — `is_road_known` lets a UI split them out
        -- without inventing a label.
        e.road_name,
        (e.road_name is not null)                               as is_road_known,
        e.event_type,
        e.severity,
        -- Re-derived from the group key rather than aggregated out of the members: the value is
        -- a function of `severity` alone, and a `min()` over it would only obscure that.
        {{ traffic_severity_ordinal('e.severity') }}            as severity_ordinal,
        {{ traffic_severity_delay_factor('e.severity') }}::double precision
                                                                as severity_delay_factor,

        e.event_count,
        e.active_event_count,
        e.upcoming_event_count,
        e.expired_event_count,
        e.blocked_event_count,
        e.open_ended_event_count,
        e.events_with_extent_count,
        e.direction_count,
        (e.active_event_count > 0)                              as has_active_event,

        e.events_with_reported_delay_count,
        round(e.total_reported_delay_minutes::numeric, 1)::double precision  as total_reported_delay_minutes,
        round(e.mean_reported_delay_minutes::numeric, 1)::double precision   as mean_reported_delay_minutes,
        e.max_reported_delay_minutes,
        round((100.0 * e.events_with_reported_delay_count / e.event_count)::numeric, 1)::double precision
                                                                as delay_reporting_coverage_percent,

        e.earliest_starts_at,
        e.latest_ends_at,
        round(e.mean_planned_duration_hours::numeric, 2)::double precision   as mean_planned_duration_hours,
        e.evaluated_at,

        -- Corridor impact. Zero is the right value for a group that touches no demo route —
        -- most of the 108 German motorways are not on one of the seeded corridors.
        {{ meters_to_km(var('traffic_match_buffer_m') ~ '::double precision') }}
                                                                as match_buffer_km,
        coalesce(m.matched_event_count, 0)                      as corridor_matched_event_count,
        coalesce(c.affected_route_count, 0)                     as affected_route_count,
        coalesce(c.affected_segment_count, 0)                   as affected_segment_count,
        round(coalesce(c.affected_segment_distance_km, 0)::numeric, 3)::double precision
                                                                as affected_segment_distance_km,
        round((100.0 * coalesce(m.matched_event_count, 0) / e.event_count)::numeric, 1)::double precision
                                                                as corridor_match_share_percent,

        e.source_systems,
        e.data_origins,
        e.latest_ingested_at

    from event_rollup as e
    left join corridor_rollup as c
        on e.road_name is not distinct from c.road_name
        and e.event_type = c.event_type
        and e.severity = c.severity
    left join matched_event_rollup as m
        on e.road_name is not distinct from m.road_name
        and e.event_type = m.event_type
        and e.severity = m.severity

)

select * from assembled
