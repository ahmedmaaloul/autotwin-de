/*
    Invariant: `mart_charging_infrastructure` has exactly one row for each of the sixteen German
    federal states — no more, no fewer, and no state that is not one of the sixteen.

    This is the invariant the whole design of that mart rests on. It is built from the
    `bundesland_reference` seed left-joined to the stations, rather than from
    `group by bundesland`, for one reason: a state with no charging sites must appear with a zero
    instead of vanishing. A regional comparison that silently drops its worst-covered region is
    worse than no comparison, because the reader cannot tell the difference between "no data" and
    "no gap".

    A `unique` test on `bundesland` proves there are no duplicates and a `relationships` test
    proves every row maps to the seed — but neither can prove that nothing is *missing*, which is
    the failure this design exists to prevent. Hence a singular test.

    Why it could break:

    * Someone rewrites the mart as a `group by` over the stations during a refactor, and it keeps
      passing every other test while quietly losing whichever states have no sites.
    * The join is changed to an inner join, or to a join on `bundesland_label_de` — text that
      differs on `ü` normalisation — instead of on the ISO code.
    * A row is added to or removed from the seed without the reference being updated; Germany has
      had sixteen states since 1990 and `accepted_values` on the seed pins the codes.

    Returns one row per missing, unexpected or duplicated state.
*/

with expected as (

    select bundesland from {{ ref('bundesland_reference') }}

),

actual as (

    select bundesland from {{ ref('mart_charging_infrastructure') }}

),

missing_from_mart as (

    select
        e.bundesland,
        'missing_from_mart' as violation,
        0                   as row_count
    from expected as e
    left join actual as a
        on e.bundesland = a.bundesland
    where a.bundesland is null

),

not_in_reference as (

    select
        a.bundesland,
        'not_in_reference'  as violation,
        1                   as row_count
    from actual as a
    left join expected as e
        on a.bundesland = e.bundesland
    where e.bundesland is null

),

duplicated_in_mart as (

    select
        bundesland,
        'duplicated_in_mart' as violation,
        count(*)::int        as row_count
    from actual
    group by bundesland
    having count(*) > 1

)

select * from missing_from_mart
union all
select * from not_in_reference
union all
select * from duplicated_in_mart
