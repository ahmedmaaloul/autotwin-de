/*
    Invariant: a connector's provenance is its station's provenance.

    `charging_points` is one of the seven tables BUILD_SPEC §3.1 puts the provenance block on, and
    the pipeline fills it by copying the parent site's attribution — a connector has no
    independent existence in the Ladesäulenregister, it is six columns of the site's own row.
    So `source`, `data_origin` and the connector's own copy of them must agree with the station's,
    always.

    This is the test that makes it safe for `int_charging_station_power` to read provenance from
    the *station* while rolling up power from the *connectors*. Without it, that model would be
    asserting an attribution it never checked — and the honesty rule of BUILD_SPEC §0.2 would be
    resting on an assumption instead of on a fact.

    Why it could break:

    * A future `MobilithekChargingProvider` or an OSM-derived enrichment writes connector rows
      against stations that came from the Bundesnetzagentur, leaving a site whose power figures
      are half official and half derived with nothing in the data saying so.
    * A backfill script sets `data_origin` on `charging_stations` without touching
      `charging_points`, or the other way round.
    * A partial upsert replaces a site's row and re-attributes it while its connectors keep the
      old run's attribution.

    Returns one row per disagreeing connector, naming both sides.
*/

with points as (

    select
        charging_point_id,
        station_id,
        connector_ordinal,
        source_system   as connector_source_system,
        data_origin     as connector_data_origin
    from {{ ref('stg_charging_points') }}

),

stations as (

    select
        station_id,
        external_id,
        source_system   as station_source_system,
        data_origin     as station_data_origin
    from {{ ref('stg_charging_stations') }}

)

select
    p.charging_point_id,
    p.station_id,
    s.external_id,
    p.connector_ordinal,
    p.connector_source_system,
    s.station_source_system,
    p.connector_data_origin,
    s.station_data_origin
from points as p
inner join stations as s
    on p.station_id = s.station_id
where p.connector_source_system is distinct from s.station_source_system
   or p.connector_data_origin is distinct from s.station_data_origin
