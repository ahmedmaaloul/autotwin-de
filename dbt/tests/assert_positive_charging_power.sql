/*
    Invariant: where a charging power is stated at all, it is strictly positive. A connector
    rated at 0 kW is not a connector, and a negative rating is a parsing artefact.

    NULL is explicitly **not** a violation. The Ladesäulenregister genuinely omits the rating for
    some sites, and `int_charging_station_power.power_basis` is what reports how much of the
    project's power arithmetic rests on stated figures. Conflating "unknown" with "zero" is the
    error this test is written to avoid, not to commit: a site whose single 300 kW connector is
    unrated must not be counted as weaker than two wallboxes, which is exactly why
    `int_charging_station_power` sums only rated connectors.

    Why it could break:

    * **The multi-value cell.** `Nennleistung Stecker1` can hold several ratings in one field —
      `"11; 3,7"` (docs/data/sources.md §1). The adapter splits on `;` and converts the decimal
      comma. A split that yields an empty fragment (trailing separator, `"11;"`) converts to 0.0
      in any parser that uses a permissive float coercion.
    * **The decimal comma.** `3,7` parsed with a `.`-expecting float either raises or, worse,
      truncates to `3`. The same code path that truncates can yield `0` for `0,8`.
    * **Column drift.** The register fixed its historic `Art der Ladeeinrichung` typo and renamed
      `Anschlussleistung` to `Nennleistung Ladeeinrichtung [kW]`. Code that positionally indexes
      the 47 columns reads a neighbouring field — a count, a postcode — as a power.

    Zeros are corrosive rather than loud: they drag `median_station_power_kw` down, push sites
    into the `normal` category, and silently shrink `fast_station_count` in every regional and
    corridor mart. Staging deliberately passes them through untouched so that this test can see
    them — a staging model that quietly repaired them would make the test unfalsifiable.

    Returns one row per offending rating.
*/

with connector_violations as (

    select
        'stg_charging_points'       as source_model,
        'power_kw'                  as offending_field,
        charging_point_id::text     as entity_id,
        station_id::text            as station_id,
        connector_ordinal,
        power_kw                    as offending_value
    from {{ ref('stg_charging_points') }}
    where power_kw is not null
      and power_kw <= 0

),

station_max_violations as (

    select
        'stg_charging_stations'     as source_model,
        'max_power_kw'              as offending_field,
        station_id::text            as entity_id,
        station_id::text            as station_id,
        null::int                   as connector_ordinal,
        max_power_kw                as offending_value
    from {{ ref('stg_charging_stations') }}
    where max_power_kw is not null
      and max_power_kw <= 0

),

station_total_violations as (

    select
        'stg_charging_stations'     as source_model,
        'total_power_kw'            as offending_field,
        station_id::text            as entity_id,
        station_id::text            as station_id,
        null::int                   as connector_ordinal,
        total_power_kw              as offending_value
    from {{ ref('stg_charging_stations') }}
    where total_power_kw is not null
      and total_power_kw <= 0

)

select * from connector_violations
union all
select * from station_max_violations
union all
select * from station_total_violations
