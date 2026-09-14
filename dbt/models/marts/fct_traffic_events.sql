/*
    The traffic-event fact: one row per disruption published by the Autobahn GmbH feed, with
    its lifecycle resolved and its severity turned into the two numbers the rest of the project
    uses.

    ### "Active now" is a build-time snapshot

    `is_active_now` is evaluated when the model is built, against `current_timestamp`, and then
    frozen into the table. It is correct for as long as the mart is fresh and progressively
    wrong after that — so `evaluated_at` is published next to it and every consumer can see how
    old the verdict is. The API must not read this column for a live answer; it filters
    `traffic_events` directly (`?active_only`, BUILD_SPEC §7). What the column is for is the
    aggregate in `mart_traffic_impact`, which is a snapshot by nature.

    ### Two severity numbers, and what they are not

    `severity_ordinal` (1-4) is the ML feature of BUILD_SPEC §10.2. `severity_delay_factor`
    (1.00-2.00) is the multiplier on free-flow travel time from `TrafficSeverity.delay_factor`.
    Both are *category-derived*: the source publishes a class, never a measurement. The one
    genuinely reported number is `delay_minutes`, and it is NULL on almost every item — which
    is why `has_reported_delay` exists, so an average over it can state its own coverage
    instead of quietly averaging the handful of rows that happen to have one.

    Grain: one row per traffic event.
*/

with events as (

    select * from {{ ref('stg_traffic_events') }}

),

evaluated as (

    select
        -- Keys -----------------------------------------------------------------------------
        traffic_event_id,
        external_id,

        -- Classification -------------------------------------------------------------------
        event_type,
        severity,
        {{ traffic_severity_ordinal('severity') }}          as severity_ordinal,
        {{ traffic_severity_delay_factor('severity') }}::double precision
                                                            as severity_delay_factor,

        -- Description ----------------------------------------------------------------------
        road_name,
        direction,
        title,
        description,

        -- Geography ------------------------------------------------------------------------
        latitude,
        longitude,
        location_geography,
        extent_geometry,
        (extent_geometry is not null)                       as has_extent_geometry,

        -- Lifecycle ------------------------------------------------------------------------
        starts_at,
        ends_at,
        current_timestamp                                   as evaluated_at,

        case
            when starts_at is not null and ends_at is not null
                then round((extract(epoch from ends_at - starts_at) / 3600.0)::numeric, 2)::double precision
        end                                                 as planned_duration_hours,

        /*
            A NULL `starts_at` means "already in force", not "unknown": the feed omits the
            field for every SHORT_TERM_ROADWORKS item, in 3 327 of 3 327 observed cases
            (docs/data/sources.md §3). Treating it as unknown would hide every short-term
            roadworks item from the active set — which is the set a driver cares about most.
            A NULL `ends_at` is open-ended and stays active.
        */
        (
            (starts_at is null or starts_at <= current_timestamp)
            and (ends_at is null or ends_at >= current_timestamp)
        )                                                   as is_active_now,
        (starts_at is not null and starts_at > current_timestamp)   as is_upcoming,
        (ends_at is not null and ends_at < current_timestamp)       as is_expired,
        (starts_at is null)                                 as is_open_started,
        (ends_at is null)                                   as is_open_ended,

        -- Impact ---------------------------------------------------------------------------
        is_blocked,
        delay_minutes,
        (delay_minutes is not null)                         as has_reported_delay,

        -- Provenance (BUILD_SPEC §3.1) -----------------------------------------------------
        source_system,
        source_identifier,
        source_url,
        source_timestamp,
        data_origin,
        ingestion_run_id,
        ingested_at

    from events

)

select * from evaluated
