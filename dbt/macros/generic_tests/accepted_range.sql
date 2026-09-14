{% test accepted_range(model, column_name, min_value=none, max_value=none, inclusive=true) %}
{#-
    Assert that a numeric column stays inside a physically meaningful range.

    dbt ships `accepted_values` for categoricals but nothing for numerics, and the numeric
    invariants of this project (SOC 0-100, power > 0, latitude 47.2-55.1, percentages 0-100)
    are exactly where silent corruption hides: a unit mix-up produces plausible-looking rows
    that no key test would ever catch.

    Implemented here rather than pulled from `dbt_utils` so that the project has no package
    dependency and `dbt parse` / `dbt build` work on a machine with no access to the dbt hub
    (see dbt/README.md, "Why there is no packages.yml").

    NULLs pass — use `not_null` to forbid them, so that a failure names one problem at a time.

    Args:
        min_value: lower bound, omitted for a one-sided range.
        max_value: upper bound, omitted for a one-sided range.
        inclusive: whether the bounds themselves are allowed.
-#}

{%- if min_value is none and max_value is none -%}
    {{ exceptions.raise_compiler_error(
        "accepted_range on " ~ model ~ "." ~ column_name ~
        " needs at least one of min_value / max_value"
    ) }}
{%- endif -%}

{%- set too_small = "<" if inclusive else "<=" -%}
{%- set too_large = ">" if inclusive else ">=" -%}

with validation as (

    select {{ column_name }} as tested_value
    from {{ model }}
    where {{ column_name }} is not null

)

select tested_value
from validation
where
    {%- if min_value is not none %}
    tested_value {{ too_small }} {{ min_value }}
    {%- endif %}
    {%- if min_value is not none and max_value is not none %}
    or
    {%- endif %}
    {%- if max_value is not none %}
    tested_value {{ too_large }} {{ max_value }}
    {%- endif %}

{% endtest %}
