/*
    Charging stations that serve a demo corridor, projected onto it.

    This is the one model in the layer that legitimately needs PostGIS: "does this station
    serve this route, and where along the route is it?" is a spatial question and there is no
    honest way to answer it with latitude and longitude alone.

    Two different spatial operations happen here, on purpose:

    * **Selection** uses `ST_DWithin` on `geography`, which measures true metres on the
      spheroid. A station is in the corridor if it is within `corridor_buffer_m` of the line.
    * **Projection** uses `ST_LineLocatePoint`, which only exists for `geometry` and measures
      in the units of the coordinate system it is given, which on raw WGS 84 is *degrees*.
      Both line and point are therefore pushed through `to_metric_crs()` first — see that macro
      for why a degree-based offset is wrong in a direction-dependent way. The resulting
      fraction is multiplied by the *projected* length of the line, so an offset in this model
      is a real distance along the real line.

    `route_distance_m` (the routing engine's driving distance) is carried alongside and is
    deliberately NOT used as the base for offsets: it is a few per mille longer than the
    geometry, and mixing the two produces a gap analysis that does not add up to its own route.

    Grain: one row per (route, station) pair within the buffer.
*/

with routes as (

    select
        route_id,
        slug,
        name                                        as route_name,
        distance_km                                 as route_driving_distance_km,
        distance_m                                  as route_driving_distance_m,
        route_geometry,
        {{ to_metric_crs('route_geometry') }}          as route_geometry_metric
    from {{ ref('stg_routes') }}
    -- Corridor analysis is a demo-corridor product: ad-hoc routes planned through
    -- /api/v1/routes/plan are not persisted as analysis subjects.
    where is_demo

),

routes_measured as (

    select
        *,
        st_length(route_geometry_metric)            as route_geometry_length_m
    from routes

),

stations as (

    select * from {{ ref('stg_charging_stations') }}

),

station_power as (

    select * from {{ ref('int_charging_station_power') }}

),

matched as (

    select
        r.route_id,
        r.slug,
        r.route_name,
        r.route_driving_distance_km,
        r.route_geometry_length_m,

        s.station_id,
        s.external_id,
        s.operator,
        s.city,
        s.bundesland,
        s.latitude,
        s.longitude,
        s.commissioned_on,
        s.data_origin,
        s.source_system,

        p.effective_max_power_kw,
        p.effective_total_power_kw,
        p.effective_charging_category,
        p.is_fast_charger,
        p.is_ultra_fast_charger,
        p.connector_count,
        p.fast_connector_count,
        p.ultra_fast_connector_count,
        p.has_ccs,
        p.connector_types,

        -- True metres on the spheroid: how far the driver leaves the corridor to reach it.
        st_distance(s.location_geography, r.route_geometry::geography) as distance_to_route_m,

        -- Position along the route as a fraction in [0, 1], measured in the metric grid.
        st_linelocatepoint(
            r.route_geometry_metric,
            {{ to_metric_crs('s.location_geography::geometry') }}
        )                                                              as route_fraction

    from routes_measured as r
    inner join stations as s
        on st_dwithin(s.location_geography, r.route_geometry::geography, {{ var('corridor_buffer_m') }})
    left join station_power as p
        on s.station_id = p.station_id

),

offsets as (

    select
        *,
        route_fraction * route_geometry_length_m                       as route_offset_m,
        {{ meters_to_km('route_fraction * route_geometry_length_m') }}  as route_offset_km,
        {{ meters_to_km('distance_to_route_m') }}                      as distance_to_route_km,
        {{ meters_to_km('route_geometry_length_m') }}                  as route_geometry_length_km,
        {{ var('corridor_buffer_m') }}::double precision               as corridor_buffer_m
    from matched

)

select * from offsets
