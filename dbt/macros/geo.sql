{#
    Geographic helpers.

    The bounding box is the one fixed by BUILD_SPEC §6 and implemented in
    `autotwin_core.quality.rules.within_germany_bbox` / `autotwin_contracts.geo.GERMANY_BBOX`.
    It is repeated here — and nowhere else in SQL — so that the warehouse-side invariant tests
    and the ingestion-side quality rules reject exactly the same rows.
#}

{% macro germany_bbox_filter(lat_col, lon_col) -%}
    {#-
        A boolean expression that is TRUE only for a point inside Germany's extent.

        NULL coordinates evaluate to FALSE rather than NULL, so that a caller writing
        `where not {{ germany_bbox_filter(...) }}` catches missing coordinates as violations
        instead of dropping them out of the result set unnoticed.

        Args:
            lat_col: SQL expression yielding latitude in WGS 84 decimal degrees.
            lon_col: SQL expression yielding longitude in WGS 84 decimal degrees.
    -#}
    (
        {{ lat_col }} is not null
        and {{ lon_col }} is not null
        and {{ lat_col }} between 47.2 and 55.1
        and {{ lon_col }} between 5.8 and 15.1
    )
{%- endmacro %}


{% macro meters_to_km(col, precision=3) -%}
    {#-
        Convert a metre value to kilometres.

        BUILD_SPEC §14: distances are stored in metres and exposed in kilometres wherever a
        human reads them, with the unit in the column name. Rounding happens in `numeric` and
        the result is cast back to `double precision` so that downstream arithmetic keeps one
        numeric type across the whole layer.

        Args:
            col: SQL expression yielding a distance in metres.
            precision: decimal places to keep; 3 = metre resolution.
    -#}
    round(({{ col }})::numeric / 1000.0, {{ precision }})::double precision
{%- endmacro %}


{% macro to_metric_crs(geometry_col) -%}
    {#-
        Project a WGS 84 geometry into the German national metric grid.

        Every "how far along this line" question in the project is answered in ETRS89 /
        UTM zone 32N (EPSG:25832). It matters: `ST_LineLocatePoint` and `ST_Length` are
        geometry functions and measure in the units of the coordinate system they are handed.
        On raw WGS 84 that unit is *degrees*, and at 51° N one degree of longitude covers only
        ~63 % of the ground distance of one degree of latitude — so an east-west corridor such
        as Köln-Dresden would have its offsets systematically compressed against a north-south
        one, and the resulting gap analysis would be wrong in a direction-dependent way that no
        aggregate would reveal.

        `geography` columns must be cast to `geometry` before they are passed in; the cast is
        left to the caller so that it is visible at the call site.

        Args:
            geometry_col: SQL expression yielding a `geometry` in EPSG:4326.
    -#}
    st_transform({{ geometry_col }}, {{ var('germany_metric_srid') }})
{%- endmacro %}
