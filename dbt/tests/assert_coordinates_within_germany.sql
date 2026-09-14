/*
    Invariant: every point this project stores lies inside Germany's bounding box
    (lat 47.2-55.1, lon 5.8-15.1, BUILD_SPEC §6). AutoTwin DE is a German platform; a point
    outside that box is not a foreign location, it is a parsing error.

    Why it could break, per source — these are the specific failure modes of the four adapters,
    not hypotheticals:

    * **Bundesnetzagentur.** The register writes coordinates with a decimal *comma*
      (`48,442398`, docs/data/sources.md §1). A parser that treats the comma as a thousands
      separator yields `48442398`; one that splits the CSV on `;` inside quoted fields shifts
      every column and puts the longitude where the latitude should be. Germany is wider in
      latitude range than in longitude range, so a swapped pair often stays *plausible* — 8.6/50.1
      becomes 50.1/8.6, which is in the Baltic off Kaliningrad — and only a box check catches it.
    * **Autobahn.** The `coordinate` object keys the longitude as **`long`**, not `lon` or `lng`,
      and `point`/`extent` are lat-first strings while `geometry` is lon-first GeoJSON
      (docs/data/sources.md §3). Mixing the two conventions in one adapter swaps the pair.
    * **DWD.** The station-description file is genuinely fixed-width; splitting it on whitespace
      breaks on names like `Seebach (Nationalpark Schwarzwald)` and shifts the coordinate columns.
    * **Simulator.** A route the vehicle follows off the end of its geometry, or a heading
      integrated in degrees where radians were meant, walks a vehicle out of the country.

    The box is defined once, in `macros/geo.sql`, and mirrors
    `autotwin_core.quality.rules.within_germany_bbox` — so ingestion and warehouse reject exactly
    the same rows. NULL coordinates are violations here, not passes: the macro is written to
    evaluate them as FALSE precisely so `where not <macro>` catches them.

    Returns one row per offending point, naming the model and the entity.
*/

with charging_station_violations as (

    select
        'stg_charging_stations'     as source_model,
        station_id::text            as entity_id,
        external_id                 as entity_label,
        latitude,
        longitude
    from {{ ref('stg_charging_stations') }}
    where not {{ germany_bbox_filter('latitude', 'longitude') }}

),

weather_violations as (

    select
        'stg_weather_observations'  as source_model,
        weather_observation_id::text as entity_id,
        observed_at::text           as entity_label,
        latitude,
        longitude
    from {{ ref('stg_weather_observations') }}
    where not {{ germany_bbox_filter('latitude', 'longitude') }}

),

traffic_violations as (

    select
        'stg_traffic_events'        as source_model,
        traffic_event_id::text      as entity_id,
        coalesce(external_id, title) as entity_label,
        latitude,
        longitude
    from {{ ref('stg_traffic_events') }}
    where not {{ germany_bbox_filter('latitude', 'longitude') }}

),

telemetry_violations as (

    select
        'stg_telemetry'             as source_model,
        telemetry_id::text          as entity_id,
        vehicle_id                  as entity_label,
        latitude,
        longitude
    from {{ ref('stg_telemetry') }}
    where not {{ germany_bbox_filter('latitude', 'longitude') }}

)

select * from charging_station_violations
union all
select * from weather_violations
union all
select * from traffic_violations
union all
select * from telemetry_violations
