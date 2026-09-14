{% test unique_combination_of_columns(model, combination_of_columns) %}
{#-
    Assert that a set of columns is unique together — a composite key test.

    Needed wherever the grain is not a single surrogate column: `(route_id, segment_ordinal)`,
    `(station_id, ordinal)`, `(weather_station_id, observed_at)`. dbt's built-in `unique` tests
    one column, and concatenating the key by hand into a synthetic column would hide NULLs
    (`a || null` is NULL, so two broken rows would look like one).

    NULLs are compared with `is not distinct from` semantics by grouping on the raw columns,
    which is what Postgres `group by` already does — two rows that are NULL in the same
    position *do* collide here, deliberately: for a key column that is a defect either way.

    Args:
        combination_of_columns: list of column names forming the composite key.
-#}

{%- set columns_csv = combination_of_columns | join(", ") -%}

select
    {{ columns_csv }},
    count(*) as duplicate_row_count
from {{ model }}
group by {{ columns_csv }}
having count(*) > 1

{% endtest %}
