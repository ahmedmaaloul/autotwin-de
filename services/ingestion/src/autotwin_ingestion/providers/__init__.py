"""Concrete adapters for the five German open-data sources AutoTwin DE runs on (§4).

===========================  ==================================================  ===========
capability                   adapter                                             licence
===========================  ==================================================  ===========
charging infrastructure      :class:`BundesnetzagenturChargingProvider`           CC BY 4.0
weather observations         :class:`DWDWeatherProvider`                          CC BY 4.0
traffic disruptions          :class:`AutobahnTrafficProvider`                     unconfirmed
routing                      :class:`OSRMRoutingProvider`                         ODbL 1.0
geocoding                    :class:`NominatimGeocodingProvider`                  ODbL 1.0
===========================  ==================================================  ===========

Every one of them implements an ABC from ``autotwin_core.providers.base``, walks the mandatory
``live → cache → fixture`` chain, and reports its :class:`~autotwin_contracts.ProviderMode`
truthfully. None of them touches the database — parsing and persistence are separate jobs, so
an adapter can be tested against a committed fixture with no services running.

Prefer :mod:`autotwin_ingestion.providers.registry` over constructing an adapter directly;
that is what keeps the choice of source in one place and lets a test swap in a double.
"""

from __future__ import annotations

from autotwin_ingestion.providers.charging import (
    BNETZA_LANDING_PAGE_URL,
    BundesnetzagenturChargingProvider,
    iter_records,
)
from autotwin_ingestion.providers.geocoding import (
    NominatimGeocodingProvider,
    normalise_query,
    parse_places,
)
from autotwin_ingestion.providers.registry import (
    PROVIDER_KINDS,
    ProviderKind,
    close_providers,
    get_charging_provider,
    get_geocoding_provider,
    get_routing_provider,
    get_traffic_provider,
    get_weather_provider,
    reset_providers,
    set_provider_override,
)
from autotwin_ingestion.providers.routing import (
    DEFAULT_PROFILE,
    OSRMRoutingProvider,
    parse_route,
    road_class_from_ref,
)
from autotwin_ingestion.providers.traffic import (
    DEFAULT_ROADS,
    SERVICES,
    AutobahnTrafficProvider,
    derive_severity,
    extract_validity,
    parse_german_datetime,
    parse_item,
)
from autotwin_ingestion.providers.weather import (
    DWD_ATTRIBUTION,
    PRODUCTS,
    DWDWeatherProvider,
    derive_condition,
    parse_product_file,
    parse_station_catalogue,
)

__all__ = [
    "BNETZA_LANDING_PAGE_URL",
    "DEFAULT_PROFILE",
    "DEFAULT_ROADS",
    "DWD_ATTRIBUTION",
    "PRODUCTS",
    "PROVIDER_KINDS",
    "SERVICES",
    "AutobahnTrafficProvider",
    "BundesnetzagenturChargingProvider",
    "DWDWeatherProvider",
    "NominatimGeocodingProvider",
    "OSRMRoutingProvider",
    "ProviderKind",
    "close_providers",
    "derive_condition",
    "derive_severity",
    "extract_validity",
    "get_charging_provider",
    "get_geocoding_provider",
    "get_routing_provider",
    "get_traffic_provider",
    "get_weather_provider",
    "iter_records",
    "normalise_query",
    "parse_german_datetime",
    "parse_item",
    "parse_places",
    "parse_product_file",
    "parse_route",
    "parse_station_catalogue",
    "reset_providers",
    "road_class_from_ref",
    "set_provider_override",
]
