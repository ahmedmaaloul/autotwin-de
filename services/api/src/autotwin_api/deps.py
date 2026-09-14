"""Typed FastAPI dependencies shared by every router.

Each alias here is an ``Annotated[...]`` type, so a handler declares what it needs by writing a
type — ``async def list_stations(session: DbSession, page: Pagination)`` — instead of repeating
``Depends(...)`` in fourteen modules. That is worth more than the keystrokes it saves: when the
session factory or the provider registry changes, the change lands in one file, and a handler
that asks for ``RoutingProviderDep`` is statically known to receive a ``RoutingProvider``.

Nothing is constructed here. The database session, the five provider adapters and the ML model
are owned by ``autotwin_core`` / ``autotwin_ingestion`` / ``autotwin_ml``; this module only
states how the HTTP layer reaches them.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import Depends, Query
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from autotwin_api.pagination import pagination_params
from autotwin_contracts import BoundingBox, PaginationParams
from autotwin_core.config import Settings, get_settings
from autotwin_core.db.session import get_session
from autotwin_core.errors import ValidationError
from autotwin_core.providers import (
    ChargingInfrastructureProvider,
    GeocodingProvider,
    RoutingProvider,
    TrafficProvider,
    WeatherProvider,
)
from autotwin_ingestion.providers.registry import (
    get_charging_provider,
    get_geocoding_provider,
    get_routing_provider,
    get_traffic_provider,
    get_weather_provider,
)
from autotwin_ml.registry import LoadedModel, get_active_model

__all__ = [
    "ActiveModel",
    "AppSettings",
    "BoundingBoxQuery",
    "ChargingProviderDep",
    "DbSession",
    "GeocodingProviderDep",
    "Pagination",
    "RoutingProviderDep",
    "TrafficProviderDep",
    "WeatherProviderDep",
    "active_ml_model",
    "bounding_box_query",
]

DbSession = Annotated[AsyncSession, Depends(get_session)]
"""An ``AsyncSession`` scoped to the request.

It does **not** commit: read endpoints have no business opening a write transaction, and a
write endpoint states its own commit point so that what is persisted is visible in the handler.
"""

Pagination = Annotated[PaginationParams, Depends(pagination_params)]
"""``?page=&page_size=`` — every list endpoint takes this and returns ``Page[T]``."""

AppSettings = Annotated[Settings, Depends(get_settings)]
"""The process-wide settings, for handlers that must report configuration (data mode, Kafka)."""

_BBOX_DETAILS: Final[dict[str, object]] = {"parameter": "bbox"}
"""Structured context attached to every rejected bounding box, so the client is told which
query parameter it got wrong rather than being left to guess from the message."""


def bounding_box_query(
    bbox: Annotated[
        str | None,
        Query(
            description=(
                "Geographic filter as `west,south,east,north` in WGS 84 decimal degrees "
                "(GeoJSON bbox order). Omit to search all of Germany."
            ),
            examples=["8.40,48.60,9.40,49.20"],
        ),
    ] = None,
) -> BoundingBox | None:
    """Parse the ``?bbox=w,s,e,n`` query parameter shared by the map and list endpoints.

    Parsing here rather than in each router means one error message for a malformed box and one
    place where the west/south/east/north order is documented — and that order is the single
    most common source of "the map is empty and nothing is wrong" bug reports.

    Raises:
        ValidationError: The value is not four numbers, or the box is inverted. Surfaced as
            ``422 validation_error`` naming the offending parameter.
    """
    if bbox is None:
        return None
    try:
        return BoundingBox.parse(bbox)
    except PydanticValidationError as exc:
        # An inverted or out-of-range box fails inside the model. Pydantic's str() is a
        # multi-line dump ending in a link to errors.pydantic.dev — useful in a stack trace,
        # hostile in an HTTP response — so only the human sentence is kept.
        message = "; ".join(
            str(error["msg"]).removeprefix("Value error, ") for error in exc.errors()
        )
        raise ValidationError(message, details=_BBOX_DETAILS | {"value": bbox}) from exc
    except ValueError as exc:
        raise ValidationError(str(exc), details=_BBOX_DETAILS | {"value": bbox}) from exc


BoundingBoxQuery = Annotated[BoundingBox | None, Depends(bounding_box_query)]
"""Optional geographic filter, already parsed and validated."""


def active_ml_model() -> LoadedModel:
    """Load the ML model the API serves (BUILD_SPEC §10.2).

    Declared as a *synchronous* dependency on purpose: resolving the artefact touches the
    filesystem and the first call deserialises a LightGBM booster, so FastAPI runs it in its
    worker thread pool instead of blocking the event loop for every other request in flight.
    Subsequent calls hit the registry's mtime-keyed cache.

    Raises:
        ModelNotAvailable: When nothing is trained, or the artefact cannot be loaded. It is an
            ``AutoTwinError`` with ``http_status = 503``, so the exception handler renders the
            standard envelope with code ``model_not_available`` — a retry after training
            succeeds, which is what 503 means and what 404 would not.
    """
    return get_active_model()


ActiveModel = Annotated[LoadedModel, Depends(active_ml_model)]
"""The trained model backing ``/api/v1/ml/predict``; 503 while nothing is trained."""

ChargingProviderDep = Annotated[ChargingInfrastructureProvider, Depends(get_charging_provider)]
"""Bundesnetzagentur Ladesäulenregister adapter (process-wide, pooled HTTP client)."""

WeatherProviderDep = Annotated[WeatherProvider, Depends(get_weather_provider)]
"""DWD open-data adapter."""

TrafficProviderDep = Annotated[TrafficProvider, Depends(get_traffic_provider)]
"""Autobahn GmbH adapter."""

RoutingProviderDep = Annotated[RoutingProvider, Depends(get_routing_provider)]
"""OSRM adapter — the one provider the API calls on the request path, for route planning."""

GeocodingProviderDep = Annotated[GeocodingProvider, Depends(get_geocoding_provider)]
"""Nominatim adapter, behind ``GET /api/v1/routes/geocode``."""
