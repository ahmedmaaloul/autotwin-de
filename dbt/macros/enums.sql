{#
    SQL mirrors of the classmethods and properties on `autotwin_contracts.enums`.

    Every macro in this file re-implements a Python rule that already exists in the codebase.
    That duplication is deliberate and bounded: the analytical layer must be able to bucket a
    value without a round-trip through Python, and the only acceptable way to do that is a
    single, documented SQL copy of the rule that a test can compare against the Python one.
    If a threshold changes in `autotwin_contracts.enums`, it changes here in the same commit.
#}

{% macro energy_intensity(actual, nominal) -%}
    {#-
        Mirror of `EnergyIntensity.from_ratio` (BUILD_SPEC §2).

        Buckets `actual / nominal` consumption: < 0.95 low, < 1.10 medium, < 1.30 high,
        else critical.

        Python raises on a non-positive `nominal` because that means the caller swapped its
        arguments. SQL cannot raise usefully inside a `select` over millions of rows, so a
        non-positive or NULL nominal yields NULL and the `not_null` test on the column is what
        surfaces the mistake.

        Args:
            actual: SQL expression, consumption in kWh/100 km.
            nominal: SQL expression, the vehicle's nominal consumption in kWh/100 km.
    -#}
    case
        when {{ actual }} is null or {{ nominal }} is null or {{ nominal }} <= 0 then null
        when {{ actual }} / {{ nominal }} < 0.95 then 'low'
        when {{ actual }} / {{ nominal }} < 1.10 then 'medium'
        when {{ actual }} / {{ nominal }} < 1.30 then 'high'
        else 'critical'
    end
{%- endmacro %}


{% macro charging_category_from_power(power_kw) -%}
    {#-
        Mirror of `ChargingCategory.from_power` (BUILD_SPEC §2).

        < 22 kW normal · 22-149 kW fast · >= 150 kW ultra_fast. An unknown rating is `normal`,
        exactly as in Python: promoting an unrated site into the fast-charger statistics would
        overstate the network.

        Args:
            power_kw: SQL expression, the strongest charging point of the site in kW.
    -#}
    case
        when {{ power_kw }} is null or {{ power_kw }} < 22.0 then 'normal'
        when {{ power_kw }} < 150.0 then 'fast'
        else 'ultra_fast'
    end
{%- endmacro %}


{% macro road_class_ordinal(road_class) -%}
    {#-
        Mirror of `RoadClass.ordinal` (BUILD_SPEC §2, feature `road_class_ordinal` in §10.2).

        motorway 6 · trunk 5 · primary 4 · secondary 3 · tertiary 2 · residential/service 1 ·
        unknown 0. `residential` and `service` share rank 1 because their energy behaviour is
        indistinguishable at this model's resolution.

        Args:
            road_class: SQL expression yielding a RoadClass value as text.
    -#}
    case {{ road_class }}
        when 'motorway' then 6
        when 'trunk' then 5
        when 'primary' then 4
        when 'secondary' then 3
        when 'tertiary' then 2
        when 'residential' then 1
        when 'service' then 1
        when 'unknown' then 0
        else 0
    end
{%- endmacro %}


{% macro traffic_severity_ordinal(severity) -%}
    {#-
        Mirror of `TrafficSeverity.ordinal`: low 1 · moderate 2 · high 3 · severe 4.

        Args:
            severity: SQL expression yielding a TrafficSeverity value as text.
    -#}
    case {{ severity }}
        when 'low' then 1
        when 'moderate' then 2
        when 'high' then 3
        when 'severe' then 4
        else null
    end
{%- endmacro %}


{% macro traffic_severity_delay_factor(severity) -%}
    {#-
        Mirror of `TrafficSeverity.delay_factor`: the multiplier on free-flow travel time.

        low 1.00 · moderate 1.15 · high 1.40 · severe 2.00. Deliberately coarse: the sources
        publish a category, not a measured delay, so a finer scale would be false precision.

        Args:
            severity: SQL expression yielding a TrafficSeverity value as text.
    -#}
    case {{ severity }}
        when 'low' then 1.00
        when 'moderate' then 1.15
        when 'high' then 1.40
        when 'severe' then 2.00
        else 1.00
    end
{%- endmacro %}
