{% test expression_is_true(model, expression, column_name=none) %}
{#-
    Assert that a row-level boolean expression holds for every row of a model.

    This is the escape hatch for invariants that span two columns of the same row — a fast
    charger whose category says `normal`, a trip that ended before it started, a share that
    does not add up. Writing each of them as a singular test would scatter one-line checks
    across `tests/`; writing them in the model's YAML keeps the invariant next to the column
    it constrains.

    Two forms, matching the convention readers know from `dbt_utils`:

      * under `tests:` at model level — `expression` is the complete predicate,
        e.g. `ended_at >= started_at`;
      * under a `columns:` entry — `expression` is appended to the column name,
        e.g. `>= 0` on `distance_km` becomes `distance_km >= 0`.

    Rows where the predicate evaluates to NULL are *failures*: in SQL a comparison against a
    NULL operand is unknown, and treating "unknown" as "fine" is how a NULL-propagation bug
    stays invisible for a year. Where NULLs are legitimate, say so in the predicate
    (`col is null or col >= 0`).

    Args:
        expression: SQL boolean expression, or a predicate fragment at column level.
-#}

{%- set predicate = expression if column_name is none else column_name ~ " " ~ expression -%}

select *
from {{ model }}
where not coalesce(({{ predicate }}), false)

{% endtest %}
