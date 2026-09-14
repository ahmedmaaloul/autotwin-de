/*
    Predicted energy per route segment, with the route-level totals attached — the tabular form
    of the `RouteAnalysis` payload of BUILD_SPEC §7.3.

    Grain: one row per route segment. Route-level figures (`route_predicted_kwh`,
    `route_kwh_per_100km`, the energy-intensity distribution) are computed as windows over the
    route and repeated on each of its segments. That denormalisation is deliberate: the consumer
    of this mart is a route page that shows a header and a segment table together, and making it
    join two relations to render one screen would buy nothing.

    ### Where the numbers come from

    `predicted_kwh` and `predicted_kwh_per_100km` are produced by the energy model of
    BUILD_SPEC §10 and stored on `route_segments` by the API. This mart does **not** recompute
    them — it would be a second, worse implementation of a model that already exists in Python.
    What it adds is the *attribution*: how much of that prediction is climate control, and how
    much extra energy the traffic on the segment implies.

    ### The two penalties, and what they are honestly worth

    * `weather_penalty_percent` — the HVAC load implied by the segment's temperature
      (BUILD_SPEC §10.1 anchors: 3.5 kW at -10 °C, 0 kW at 20 °C, 2.0 kW at +35 °C) over the
      segment's driving time, expressed as a share of what the segment would have used without
      it. This is an **attribution of the prediction**, reconstructed from the same anchors the
      Python model uses — not an independent measurement, and not a second prediction.

    * `traffic_penalty_percent` — traffic does not make a car less efficient per kilometre so
      much as it makes the kilometre take longer, and auxiliary load is charged per *hour*.
      The penalty is therefore the extra auxiliary energy over the extra time that
      `TrafficSeverity.delay_factor` implies. Coarse by construction: the sources publish a
      severity class, never a measured delay, so anything finer would be false precision.

    Both are NULL where the inputs to compute them are NULL — an unanalysed segment, a segment
    with no assumed speed — rather than silently zero. A zero penalty and an unknown penalty are
    different statements.

    ### Reference vehicle

    `energy_intensity` as stored on the segment was bucketed against whatever vehicle the API
    was asked about. `energy_intensity_vs_reference` re-buckets the same prediction against the
    project's reference profile (`reference_vehicle_code`, `compact_ev` by default) so that two
    routes analysed for different cars are still comparable on one axis.
*/

with segments as (

    select * from {{ ref('stg_route_segments') }}

),

routes as (

    select
        route_id,
        route_slug,
        route_name,
        origin_name,
        destination_name,
        distance_km         as route_distance_km,
        duration_min        as route_duration_min,
        is_demo             as route_is_demo,
        segment_count       as route_segment_count
    from {{ ref('dim_route') }}

),

reference_vehicle as (

    select
        vehicle_model_code                  as reference_vehicle_code,
        nominal_consumption_kwh_100km       as reference_nominal_kwh_100km,
        usable_capacity_kwh                 as reference_usable_capacity_kwh
    from {{ ref('dim_vehicle_model') }}
    where vehicle_model_code = '{{ var('reference_vehicle_code') }}'

),

enriched as (

    select
        -- Keys -----------------------------------------------------------------------------
        s.route_segment_id,
        s.route_id,
        r.route_slug,
        r.route_name,
        r.origin_name,
        r.destination_name,
        r.route_is_demo,
        s.segment_ordinal,

        -- Geometry of the segment along the route ------------------------------------------
        s.start_offset_km,
        s.segment_distance_km,
        round((s.start_offset_km + s.segment_distance_km)::numeric, 3)::double precision
                                                        as end_offset_km,
        s.segment_geometry,

        -- Road ------------------------------------------------------------------------------
        s.road_class,
        {{ road_class_ordinal('s.road_class') }}        as road_class_ordinal,
        (s.road_class = 'motorway')                     as is_motorway,
        s.speed_limit_kmh,
        s.assumed_speed_kmh,
        s.elevation_gain_m,

        -- Gradient as a percentage of the segment length. The `gradient_percent` feature of
        -- BUILD_SPEC §10.2. `elevation_gain_m` is a net gain, so this can be negative.
        case
            when s.segment_distance_m > 0
                then round((100.0 * s.elevation_gain_m / s.segment_distance_m)::numeric, 3)::double precision
        end                                             as gradient_percent,

        -- Conditions -------------------------------------------------------------------------
        s.temperature_c,
        s.traffic_severity,
        {{ traffic_severity_ordinal('s.traffic_severity') }}    as traffic_severity_ordinal,
        {{ traffic_severity_delay_factor('s.traffic_severity') }}::double precision
                                                        as traffic_delay_factor,

        -- Energy, as the model produced it --------------------------------------------------
        s.predicted_kwh,
        s.predicted_kwh_per_100km,
        s.energy_intensity,

        -- Reference comparison ---------------------------------------------------------------
        v.reference_vehicle_code,
        v.reference_nominal_kwh_100km,
        v.reference_usable_capacity_kwh,
        {{ energy_intensity('s.predicted_kwh_per_100km', 'v.reference_nominal_kwh_100km') }}
                                                        as energy_intensity_vs_reference,
        case
            when v.reference_nominal_kwh_100km > 0
                then round((s.predicted_kwh_per_100km / v.reference_nominal_kwh_100km)::numeric, 4)::double precision
        end                                             as consumption_ratio_to_reference,

        -- Intermediate quantities for the penalties ------------------------------------------
        case
            when s.assumed_speed_kmh > 0
                then s.segment_distance_km / s.assumed_speed_kmh
        end                                             as free_flow_hours,
        {{ hvac_load_kw('s.temperature_c') }}           as hvac_load_kw,

        s.data_origin

    from segments as s
    inner join routes as r
        on s.route_id = r.route_id
    -- Cross join to the single reference-profile row. Left so that a database whose
    -- `vehicle_models` seed has not run yet still produces the mart with NULL comparisons
    -- instead of zero rows.
    left join reference_vehicle as v
        on true

),

penalties as (

    select
        *,

        -- Auxiliary load: the constant base draw of BUILD_SPEC §10.1 plus the climate term.
        round((hvac_load_kw * free_flow_hours)::numeric, 4)::double precision
                                                        as hvac_energy_kwh,
        round(((0.35 + hvac_load_kw) * free_flow_hours)::numeric, 4)::double precision
                                                        as auxiliary_energy_kwh,
        round(((0.35 + hvac_load_kw) * free_flow_hours * (traffic_delay_factor - 1.0))::numeric, 4)::double precision
                                                        as traffic_extra_auxiliary_kwh,
        round((free_flow_hours * 60.0)::numeric, 2)::double precision       as free_flow_minutes,
        round((free_flow_hours * traffic_delay_factor * 60.0)::numeric, 2)::double precision
                                                        as delayed_minutes

    from enriched

),

attributed as (

    select
        *,

        -- Share of the prediction attributable to climate control, expressed against what the
        -- segment would have used without it. NULL where either input is unknown.
        case
            when predicted_kwh is not null
                 and hvac_energy_kwh is not null
                 and predicted_kwh - hvac_energy_kwh > 0
                then round((100.0 * hvac_energy_kwh / (predicted_kwh - hvac_energy_kwh))::numeric, 2)::double precision
        end                                             as weather_penalty_percent,

        -- Extra auxiliary energy over the extra time the severity class implies, as a share of
        -- the prediction.
        case
            when predicted_kwh > 0 and traffic_extra_auxiliary_kwh is not null
                then round((100.0 * traffic_extra_auxiliary_kwh / predicted_kwh)::numeric, 2)::double precision
        end                                             as traffic_penalty_percent,

        round((delayed_minutes - free_flow_minutes)::numeric, 2)::double precision
                                                        as traffic_delay_minutes

    from penalties

),

with_route_totals as (

    select
        *,

        -- Route-level aggregates, repeated on every segment of the route. See the header.
        sum(predicted_kwh) over route_window                                as route_predicted_kwh,
        sum(segment_distance_km) over route_window                          as route_segments_distance_km,
        count(*) over route_window                                          as route_segment_count,
        count(predicted_kwh) over route_window                              as route_analysed_segment_count,

        count(*) filter (where energy_intensity_vs_reference = 'low') over route_window
                                                                            as route_low_intensity_segment_count,
        count(*) filter (where energy_intensity_vs_reference = 'medium') over route_window
                                                                            as route_medium_intensity_segment_count,
        count(*) filter (where energy_intensity_vs_reference = 'high') over route_window
                                                                            as route_high_intensity_segment_count,
        count(*) filter (where energy_intensity_vs_reference = 'critical') over route_window
                                                                            as route_critical_intensity_segment_count,

        sum(hvac_energy_kwh) over route_window                              as route_hvac_energy_kwh,
        sum(traffic_extra_auxiliary_kwh) over route_window                  as route_traffic_extra_auxiliary_kwh,
        sum(traffic_delay_minutes) over route_window                        as route_traffic_delay_minutes,
        max(predicted_kwh_per_100km) over route_window                      as route_max_segment_kwh_per_100km,

        -- Distance-weighted, not a mean of the segment ratios: a 40 km motorway segment must
        -- not carry the same weight as a 300 m slip road.
        sum(predicted_kwh_per_100km * segment_distance_km) over route_window as route_weighted_kwh_numerator

    from attributed
    window route_window as (partition by route_id)

),

final as (

    select
        route_segment_id,
        route_id,
        route_slug,
        route_name,
        origin_name,
        destination_name,
        route_is_demo,
        segment_ordinal,

        start_offset_km,
        end_offset_km,
        segment_distance_km,
        segment_geometry,

        road_class,
        road_class_ordinal,
        is_motorway,
        speed_limit_kmh,
        assumed_speed_kmh,
        elevation_gain_m,
        gradient_percent,

        temperature_c,
        traffic_severity,
        traffic_severity_ordinal,
        traffic_delay_factor,

        predicted_kwh,
        predicted_kwh_per_100km,
        energy_intensity,
        energy_intensity_vs_reference,
        consumption_ratio_to_reference,
        reference_vehicle_code,
        reference_nominal_kwh_100km,
        reference_usable_capacity_kwh,

        hvac_load_kw,
        hvac_energy_kwh,
        auxiliary_energy_kwh,
        traffic_extra_auxiliary_kwh,
        free_flow_minutes,
        delayed_minutes,
        traffic_delay_minutes,
        weather_penalty_percent,
        traffic_penalty_percent,

        -- Route-level -----------------------------------------------------------------------
        round(route_predicted_kwh::numeric, 3)::double precision        as route_predicted_kwh,
        round(route_segments_distance_km::numeric, 3)::double precision as route_segments_distance_km,
        route_segment_count,
        route_analysed_segment_count,

        case
            when route_segments_distance_km > 0
                then round((route_weighted_kwh_numerator / route_segments_distance_km)::numeric, 3)::double precision
        end                                                             as route_kwh_per_100km,
        route_max_segment_kwh_per_100km,

        route_low_intensity_segment_count,
        route_medium_intensity_segment_count,
        route_high_intensity_segment_count,
        route_critical_intensity_segment_count,
        case
            when route_segment_count > 0
                then round((100.0 * route_critical_intensity_segment_count / route_segment_count)::numeric, 1)::double precision
        end                                                             as route_critical_intensity_share_percent,

        round(route_hvac_energy_kwh::numeric, 3)::double precision      as route_hvac_energy_kwh,
        round(route_traffic_extra_auxiliary_kwh::numeric, 3)::double precision
                                                                        as route_traffic_extra_auxiliary_kwh,
        round(route_traffic_delay_minutes::numeric, 2)::double precision as route_traffic_delay_minutes,

        case
            when route_predicted_kwh > 0 and route_hvac_energy_kwh is not null
                 and route_predicted_kwh - route_hvac_energy_kwh > 0
                then round((100.0 * route_hvac_energy_kwh / (route_predicted_kwh - route_hvac_energy_kwh))::numeric, 2)::double precision
        end                                                             as route_weather_penalty_percent,
        case
            when route_predicted_kwh > 0 and route_traffic_extra_auxiliary_kwh is not null
                then round((100.0 * route_traffic_extra_auxiliary_kwh / route_predicted_kwh)::numeric, 2)::double precision
        end                                                             as route_traffic_penalty_percent,

        -- Range feasibility against the reference profile: how much of a full usable battery
        -- this route would take. Above 100 % means it cannot be driven without charging.
        case
            when reference_usable_capacity_kwh > 0 and route_predicted_kwh is not null
                then round((100.0 * route_predicted_kwh / reference_usable_capacity_kwh)::numeric, 1)::double precision
        end                                                             as route_reference_battery_required_percent,

        data_origin

    from with_route_totals

)

select * from final
