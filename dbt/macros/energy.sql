{#
    Energy-model helpers used by `mart_route_energy`.

    Unlike `macros/enums.sql`, the curve below is an *approximation* of the authoritative
    implementation in `autotwin_ml.baseline.PhysicalEnergyModel`, not a line-by-line mirror:
    only the three anchor points are fixed by BUILD_SPEC §10.1, the Python model interpolates
    them inside a full road-load computation. It is reproduced here so that the analytical
    layer can attribute a share of predicted energy to climate control without calling Python,
    and it is used for attribution only — never to produce a consumption figure.
#}

{% macro hvac_load_kw(temperature_c) -%}
    {#-
        Auxiliary HVAC power at a given outside temperature, in kW.

        Piecewise-linear through the anchors BUILD_SPEC §10.1 fixes:
        3.5 kW at -10 °C · 0 kW at 20 °C · 2.0 kW at +35 °C, clamped outside that range.
        Excludes the 0.35 kW constant base load, which is not weather-driven and therefore
        must not appear in a "weather penalty".

        Args:
            temperature_c: SQL expression, outside air temperature in °C.
    -#}
    case
        when {{ temperature_c }} is null then null
        when {{ temperature_c }} <= -10.0 then 3.5
        when {{ temperature_c }} < 20.0 then 3.5 * (20.0 - {{ temperature_c }}) / 30.0
        when {{ temperature_c }} < 35.0 then 2.0 * ({{ temperature_c }} - 20.0) / 15.0
        else 2.0
    end
{%- endmacro %}


{% macro null_if_missing_sentinel(col, sentinel=-999) -%}
    {#-
        Map the DWD "value missing" sentinel to NULL.

        DWD open data writes `-999` for every unmeasured parameter (docs/data/sources.md §2).
        `autotwin_ingestion` already converts it, so this is a backstop rather than the primary
        defence — but it belongs in staging because the failure mode is silent: a single -999
        that slips through drags a station's mean temperature down by tens of degrees and
        nothing in the pipeline errors.

        Args:
            col: SQL expression for a DWD-sourced measurement.
            sentinel: the sentinel value to null out.
    -#}
    nullif({{ col }}, {{ sentinel }})
{%- endmacro %}
