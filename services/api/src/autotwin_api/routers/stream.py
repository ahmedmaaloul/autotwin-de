"""``GET /api/v1/stream/telemetry`` — the live fleet as Server-Sent Events (BUILD_SPEC §8).

Three decisions define this endpoint.

**SSE, not WebSocket.** The traffic is one-directional: the server pushes vehicle positions and
the browser never pushes back. SSE reconnects by itself, survives any HTTP proxy and needs no
protocol upgrade; a WebSocket would be strictly more machinery for the same result.

**It polls PostgreSQL, not Kafka.** A page refresh then costs one query instead of a consumer
group rebalance, twenty browsers watching the same fleet cost twenty cheap queries rather than
twenty broker connections, and the stream works identically when Kafka is switched off — which
is the configuration ``make demo`` runs in.

**It sends deltas.** Each tick returns only the vehicles whose newest sample is strictly newer
than the cursor, and the cursor then advances to the newest ``recorded_at`` in that batch. A
client that has been connected for an hour has received each vehicle once per movement, not
3 600 full fleet snapshots. The first tick carries no cursor and is therefore the initial
snapshot, which is what lets the map draw immediately without a separate request.

Wire format, fixed because ``apps/web/hooks/use-telemetry-stream.ts`` already parses it::

    event: telemetry
    data: [{"vehicle_id": "ATW-0001", ...}, ...]

plus a ``: keep-alive`` comment every 15 seconds so that no proxy in between decides the
connection is idle.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Final

from fastapi import APIRouter, Query, Request
from sse_starlette.event import ServerSentEvent
from sse_starlette.sse import EventSourceResponse

from autotwin_api.deps import BoundingBoxQuery
from autotwin_api.routers.vehicles import MAX_LIVE_VEHICLES, fetch_live_vehicles
from autotwin_contracts import BoundingBox
from autotwin_core.db.session import get_sessionmaker
from autotwin_core.logging import get_logger

__all__ = ["router"]

_LOGGER = get_logger(__name__)

router = APIRouter()

_POLL_INTERVAL_S: Final[float] = 1.0
"""How often the database is polled.

Matched to the simulator's tick and to the consumer's batch interval (BUILD_SPEC §8): polling
faster would return empty sets, polling slower would make the map visibly lag the fleet.
"""

_KEEPALIVE_S: Final[float] = 15.0
"""Interval between ``: keep-alive`` comments. Comfortably under the 30-60 s idle timeout that
load balancers and reverse proxies typically apply."""

_RETRY_MS: Final[int] = 3000
"""Reconnection delay suggested to the browser. Three seconds rides out an API restart without
a stampede of reconnects from every open tab."""

_EVENT_NAME: Final[str] = "telemetry"


async def _telemetry_events(
    request: Request,
    *,
    since: datetime | None,
    bbox: BoundingBox | None,
    vehicle_ids: list[str] | None,
) -> AsyncIterator[ServerSentEvent]:
    """Yield one SSE message per tick that produced a change.

    A **short session per poll** rather than one held open for the life of the connection: an
    SSE subscriber can stay connected for hours, and a pooled database connection pinned for
    that long would exhaust the pool at a handful of viewers. Opening a session per second is
    a checkout from an already-warm pool and costs nothing by comparison.

    The loop exits on client disconnect and on cancellation (an API shutdown, or the response
    being torn down), and the ``finally`` clause is what guarantees the session context is
    unwound in both cases.
    """
    sessions = get_sessionmaker()
    cursor = since
    emitted = 0

    # The first frame flushes the response headers, so the browser's `onopen` fires immediately
    # instead of waiting for the first vehicle to move.
    yield ServerSentEvent(comment="stream open", retry=_RETRY_MS)

    try:
        while True:
            if await request.is_disconnected():
                break

            async with sessions() as session:
                vehicles = await fetch_live_vehicles(
                    session,
                    since=cursor,
                    bbox=bbox,
                    vehicle_ids=vehicle_ids,
                    limit=MAX_LIVE_VEHICLES,
                )

            if vehicles:
                cursor = max(vehicle.recorded_at for vehicle in vehicles)
                emitted += len(vehicles)
                payload = json.dumps(
                    [vehicle.model_dump(mode="json") for vehicle in vehicles],
                    separators=(",", ":"),
                )
                yield ServerSentEvent(data=payload, event=_EVENT_NAME)

            await asyncio.sleep(_POLL_INTERVAL_S)
    except asyncio.CancelledError:
        # Normal teardown: the client went away or the process is shutting down. Re-raised so
        # the task group that cancelled it sees the cancellation it asked for.
        _LOGGER.debug("stream.telemetry.cancelled", emitted=emitted)
        raise
    finally:
        _LOGGER.info("stream.telemetry.closed", emitted=emitted)


@router.get(
    "/telemetry",
    summary="Live telemetry stream (SSE)",
    response_class=EventSourceResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": (
                "An open Server-Sent Events stream. Named event `telemetry`, whose `data` is a "
                "JSON **array** of `VehicleLive` objects — only the vehicles that changed since "
                "the last message. `: keep-alive` comments arrive every 15 seconds."
            ),
        }
    },
    description=(
        "Pushes vehicle telemetry as it is written, about once a second.\n\n"
        "* The **first** message carries every vehicle that matches the filters — the initial "
        "state of the map. Later messages carry only what changed.\n"
        "* Pass `?since=<ISO-8601>` to resume from a known cursor after a reconnect; without "
        "it the stream starts with a full snapshot.\n"
        "* `?bbox=west,south,east,north` limits the stream to the visible map area, and "
        "`?vehicle_ids=ATW-0001,ATW-0002` to specific vehicles.\n\n"
        "The stream reads the database, not Kafka, so it costs the same whether telemetry "
        "arrives through the broker or is written directly (BUILD_SPEC §8). Everything it "
        "carries is **simulated**."
    ),
)
async def telemetry_stream(
    request: Request,
    bbox: BoundingBoxQuery,
    since: Annotated[
        datetime | None,
        Query(
            description=(
                "Resume cursor: only vehicles whose newest sample is strictly newer than this "
                "are sent. Omit for a full snapshot followed by deltas."
            )
        ),
    ] = None,
    vehicle_ids: Annotated[
        str | None,
        Query(
            description="Comma-separated fleet identifiers to follow.",
            examples=["ATW-0001,ATW-0002"],
        ),
    ] = None,
) -> EventSourceResponse:
    """Open the stream.

    The keep-alive is produced by the response itself rather than by the generator: a comment
    emitted on a timer cannot be delayed by a slow query, which is exactly when a proxy is most
    likely to give up on the connection.
    """
    identifiers = (
        [item.strip() for item in vehicle_ids.split(",") if item.strip()] if (vehicle_ids) else None
    )
    return EventSourceResponse(
        _telemetry_events(request, since=since, bbox=bbox, vehicle_ids=identifiers),
        ping=_KEEPALIVE_S,
        ping_message_factory=lambda: ServerSentEvent(comment="keep-alive"),
        headers={
            # nginx and most reverse proxies buffer a response body by default, which would
            # hold each frame until the buffer filled and make a one-second stream useless.
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache, no-transform",
        },
    )
