"""Liveness, readiness and metrics — the three endpoints an operator reaches for first.

The split between ``/health`` and ``/ready`` is routinely got wrong, and getting it wrong turns
a recoverable dependency failure into a full outage: a liveness probe that queries the database
makes every replica fail at once and the orchestrator responds by restarting all of them. So the
central test here is a negative one — ``/health`` must answer **while the database is broken**.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from autotwin_api import __version__
from autotwin_api.metrics import REGISTRY
from autotwin_core.db import session as db_session_module

from . import api_client

DOCUMENTED_METRICS = (
    "api_requests_total",
    "api_request_duration_seconds",
    "telemetry_events_received_total",
    "telemetry_events_failed_total",
    "active_simulated_vehicles",
    "ingestion_rows_processed",
    "ingestion_rows_rejected",
)
"""The series BUILD_SPEC §7 promises: request counts and latencies, telemetry throughput, active
simulated vehicles and ingestion row counts. A Grafana dashboard queries these names, so a rename
is a breaking change and this tuple is the contract."""


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """The API, driven in-process."""
    async with api_client() as instance:
        yield instance


class TestHealth:
    """``GET /health`` — is this process alive?"""

    async def test_reports_status_version_and_uptime(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["version"] == __version__
        # The uptime is measured from a monotonic clock captured at import, so it is a small
        # positive number in a test process and can never be negative or jump on an NTP step.
        assert 0.0 <= body["uptime_s"] < 600.0

    async def test_answers_while_the_database_is_unreachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole reason the endpoint exists, asserted by breaking the database first.

        Every route into PostgreSQL is made to raise. ``/health`` still answers 200, because it
        performs no I/O at all; a database-backed endpoint on the same app answers 500 in the
        project's error envelope, which is what proves the sabotage was real rather than the
        test asserting nothing.
        """

        def database_is_down(*args: object, **kwargs: object) -> object:
            msg = "connection refused"
            raise OSError(msg)

        monkeypatch.setattr(db_session_module, "get_sessionmaker", database_is_down)
        monkeypatch.setattr(db_session_module, "get_engine", database_is_down)

        async with api_client(raise_app_exceptions=False) as client:
            health = await client.get("/health")
            data = await client.get("/api/v1/vehicles/live")

        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        assert data.status_code == 500
        assert data.json()["error"]["code"] == "internal_error"

    async def test_is_fast_enough_to_be_a_liveness_probe(self, client: httpx.AsyncClient) -> None:
        """Ten calls in a row, all 200 — an orchestrator polls this every few seconds."""
        for _ in range(10):
            assert (await client.get("/health")).status_code == 200

    async def test_echoes_a_correlation_id(self, client: httpx.AsyncClient) -> None:
        """Even the probe carries ``X-Request-ID``; the middleware is outermost for that reason."""
        response = await client.get("/health")
        assert response.headers["X-Request-ID"]

    async def test_carries_no_data_mode_header(self, client: httpx.AsyncClient) -> None:
        """``X-AutoTwin-Data-Mode`` means "an external source answered this"; nothing did."""
        response = await client.get("/health")
        assert "X-AutoTwin-Data-Mode" not in response.headers


class TestReady:
    """``GET /ready`` — can this process serve traffic? Unlike ``/health``, it does probe."""

    async def test_reports_every_dependency_and_degrades_with_503(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing readiness probe pulls the instance out of the load balancer, not down.

        The body is the same shape either way, so a probe failure is diagnosable from the
        response alone rather than from a log nobody has in front of them.
        """
        from autotwin_api.routers import health as health_router

        async def database_down(*args: object, **kwargs: object) -> bool:
            return False

        async def model_missing() -> bool:
            return False

        monkeypatch.setattr(health_router, "check_database", database_down)
        monkeypatch.setattr(health_router, "_check_model", model_missing)

        async with api_client() as client:
            response = await client.get("/ready")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        assert body["checks"]["database"] is False
        assert body["checks"]["model"] is False
        # Kafka is disabled in this configuration, so the check is *skipped* and reports true:
        # holding the service out of the load balancer for a transport it is not using would be
        # wrong (BUILD_SPEC §8 makes the broker optional).
        assert body["checks"]["kafka"] is True

    async def test_is_ready_when_every_check_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from autotwin_api.routers import health as health_router

        async def database_up(*args: object, **kwargs: object) -> bool:
            return True

        async def model_ready() -> bool:
            return True

        monkeypatch.setattr(health_router, "check_database", database_up)
        monkeypatch.setattr(health_router, "_check_model", model_ready)

        async with api_client() as client:
            response = await client.get("/ready")

        assert response.status_code == 200
        assert response.json() == {
            "status": "ready",
            "checks": {"database": True, "kafka": True, "model": True},
        }


class TestMetrics:
    """``GET /metrics`` — the Prometheus scrape."""

    async def test_serves_the_text_exposition_format(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/metrics")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert "version=" in response.headers["content-type"]

    @pytest.mark.parametrize("metric", DOCUMENTED_METRICS)
    async def test_exposes_every_documented_metric_name(
        self, client: httpx.AsyncClient, metric: str
    ) -> None:
        """Asserted on the ``# TYPE`` line, which carries the name exactly as it is scraped.

        ``prometheus_client`` appends ``_total`` to a ``Counter``'s sample name and strips it
        from the constructor argument, so the two ingestion gauges and the counters only come out
        with the spec's names because of how each one was declared. A rename to
        ``ingestion_rows_processed_total`` would pass a naive substring check and break every
        dashboard.
        """
        body = (await client.get("/metrics")).text
        type_lines = {
            line.removeprefix("# TYPE ").split(" ")[0]
            for line in body.splitlines()
            if line.startswith("# TYPE ")
        }
        assert metric in type_lines

    async def test_counts_requests_by_route_template_not_by_raw_path(
        self, client: httpx.AsyncClient
    ) -> None:
        """One series per route, never one per id: 116 440 station ids would take a scrape down.

        Two different values of the same path parameter must produce one series carrying the
        placeholder, and neither of the values may appear anywhere in the exposition. The ids
        here are deliberately malformed, so the request is rejected during validation and no
        database is needed to exercise the label.
        """
        await client.get("/api/v1/charging/stations/first-bad-id")
        await client.get("/api/v1/charging/stations/second-bad-id")

        body = (await client.get("/metrics")).text
        assert "{station_id}" in body
        assert "first-bad-id" not in body
        assert "second-bad-id" not in body

    async def test_request_metrics_name_the_endpoint_that_was_matched(self) -> None:
        """A matched route must be counted under a label that identifies it.

        Asserted by looking for the endpoint's own name in the exposition rather than by
        counting `unmatched`, because the registry is shared across the whole test process and
        earlier 404s have already put a sample there.
        """
        async with api_client(raise_app_exceptions=False) as client:
            await client.get("/api/v1/vehicles")

            body = (await client.get("/metrics")).text

        labels = [line for line in body.splitlines() if line.startswith("api_requests_total")]
        assert any("vehicles" in line for line in labels), (
            "GET /api/v1/vehicles produced no metric series naming the endpoint"
        )

    async def test_an_unmatched_path_folds_into_one_series(self, client: httpx.AsyncClient) -> None:
        """A crawler probing random URLs must not create unbounded cardinality."""
        await client.get("/does-not-exist-1")
        await client.get("/does-not-exist-2")

        body = (await client.get("/metrics")).text
        assert 'path="unmatched"' in body
        assert "does-not-exist" not in body

    async def test_uses_a_private_registry(self) -> None:
        """Not ``prometheus_client.REGISTRY``: building several apps in one interpreter — which
        this suite does — would otherwise raise on a duplicate metric name."""
        import prometheus_client

        assert REGISTRY is not prometheus_client.REGISTRY
