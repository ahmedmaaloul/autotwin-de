"""The error envelope, the correlation id and the parameter validation every list endpoint shares.

BUILD_SPEC §7 fixes exactly one body for every 4xx and 5xx::

    {"error": {"code": ..., "message": ..., "details": ..., "request_id": ...}}

FastAPI produces none of that by default — an ``HTTPException`` renders as ``{"detail": ...}``
and a validation failure as a bare list — so the four handlers in ``autotwin_api.errors`` are
the whole contract, and the frontend's ``ApiError`` parser reads ``error.code``. These tests
assert the shape **precisely**: an envelope that also carried FastAPI's ``detail`` key, or one
that dropped ``request_id``, would pass a looser check and break the client.

Nothing here needs a database. Every case is rejected during validation or by a handler, so the
whole module runs in the default CI suite.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from autotwin_contracts import DATA_MODE_HEADER, REQUEST_ID_HEADER, ErrorCode, ErrorResponse

from . import api_client

ENVELOPE_KEYS = {"code", "message", "details", "request_id"}


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """The API, driven in-process."""
    async with api_client() as instance:
        yield instance


def assert_is_envelope(response: httpx.Response, *, code: str) -> dict[str, Any]:
    """Assert the body is the project envelope and nothing else, and return the error object.

    The ``{"detail": ...}`` check is the point of the whole module: that key is what FastAPI
    produces when a handler is missing, so its *absence* is the evidence that ours ran.
    """
    body: dict[str, Any] = response.json()
    assert set(body) == {"error"}, f"expected only an `error` key, got {sorted(body)}"
    assert "detail" not in body
    error: dict[str, Any] = body["error"]
    assert set(error) == ENVELOPE_KEYS
    assert error["code"] == code
    assert isinstance(error["message"], str)
    assert error["message"]
    assert error["request_id"] == response.headers[REQUEST_ID_HEADER]
    # It has to survive a round trip through the contract model the frontend types come from.
    ErrorResponse.model_validate(body)
    return error


class TestNotFound:
    """404 — from Starlette's own router and from a handler, with one code for both."""

    async def test_an_unmatched_path_returns_the_envelope(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/v1/does-not-exist")

        assert response.status_code == 404
        error = assert_is_envelope(response, code=ErrorCode.not_found.value)
        assert error["details"] is None

    async def test_a_wrong_method_is_405_with_an_allow_header(
        self, client: httpx.AsyncClient
    ) -> None:
        """``exc.headers`` is preserved: a 405 without ``Allow`` is a protocol violation."""
        response = await client.post("/health")

        assert response.status_code == 405
        assert_is_envelope(response, code="method_not_allowed")
        assert "GET" in response.headers["allow"]


class TestValidationErrors:
    """422 — always ``validation_error``, always with the offending field named."""

    async def test_a_page_size_above_the_maximum_is_a_422(self, client: httpx.AsyncClient) -> None:
        """BUILD_SPEC §7 caps ``page_size`` at 500; 501 is the first value that must be refused.

        The cap is not cosmetic — ``/charging/stations`` would otherwise serialise an unbounded
        slice of 116 440 rows on request.
        """
        response = await client.get("/api/v1/charging/stations", params={"page_size": 501})

        assert response.status_code == 422
        error = assert_is_envelope(response, code=ErrorCode.validation_error.value)
        fields = error["details"]["errors"]
        assert [field["loc"] for field in fields] == ["query.page_size"]

    async def test_the_documented_bounds_are_the_ones_the_schema_declares(
        self, client: httpx.AsyncClient
    ) -> None:
        """1-500 inclusive, published in the OpenAPI document the frontend types are built from.

        The rejection above proves the bound is enforced; this proves it is the *documented*
        bound, so a client generated from the schema cannot construct a request the API refuses.
        """
        document = (await client.get("/openapi.json")).json()
        parameters = {
            parameter["name"]: parameter["schema"]
            for parameter in document["paths"]["/api/v1/charging/stations"]["get"]["parameters"]
        }

        assert parameters["page_size"]["minimum"] == 1
        assert parameters["page_size"]["maximum"] == 500
        assert parameters["page_size"]["default"] == 50
        assert parameters["page"]["minimum"] == 1
        assert parameters["page"]["default"] == 1

    async def test_page_zero_is_rejected(self, client: httpx.AsyncClient) -> None:
        """Pages are 1-based; page 0 would silently become a negative OFFSET."""
        response = await client.get("/api/v1/charging/stations", params={"page": 0})

        assert response.status_code == 422
        error = assert_is_envelope(response, code=ErrorCode.validation_error.value)
        assert error["details"]["errors"][0]["loc"] == "query.page"

    async def test_an_unknown_enum_value_names_the_parameter_and_the_alternatives(
        self, client: httpx.AsyncClient
    ) -> None:
        """A client that mistypes a Bundesland gets the list of valid codes, not a 500."""
        response = await client.get("/api/v1/charging/stations", params={"bundesland": "XX"})

        assert response.status_code == 422
        error = assert_is_envelope(response, code=ErrorCode.validation_error.value)
        message = error["details"]["errors"][0]["message"]
        assert "'HE'" in message

    async def test_a_body_field_out_of_range_reports_its_path_in_the_body(
        self, client: httpx.AsyncClient
    ) -> None:
        """``loc`` is dotted and flattened to strings; Pydantic's raw ``ctx`` is not serialisable.

        Leaving a live exception object in ``ctx`` would turn a client's typo into a 500, which
        is exactly the failure the flattening in ``_handle_request_validation_error`` prevents.
        """
        response = await client.post(
            "/api/v1/routes/analyze",
            json={"route_slug": "frankfurt-stuttgart", "start_soc_percent": 150.0},
        )

        assert response.status_code == 422
        error = assert_is_envelope(response, code=ErrorCode.validation_error.value)
        assert error["details"]["errors"][0]["loc"] == "body.start_soc_percent"

    async def test_a_malformed_path_parameter_is_422_not_500(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get("/api/v1/charging/stations/not-a-uuid")

        assert response.status_code == 422
        assert_is_envelope(response, code=ErrorCode.validation_error.value)


class TestBoundingBoxParsing:
    """``?bbox=west,south,east,north`` — parsed once, in one dependency, with one message.

    The west/south/east/north order is the single most common source of "the map is empty and
    nothing is wrong" bug reports, so both failure modes have to be *readable*.
    """

    @pytest.mark.parametrize(
        ("bbox", "expected_fragment"),
        [
            ("nonsense", "four comma-separated values"),
            ("8.4,48.6,9.4", "four comma-separated values"),
            ("8.4,48.6,9.4,49.2,1.0", "four comma-separated values"),
            ("a,b,c,d", "number"),
        ],
    )
    async def test_a_malformed_box_is_a_readable_422(
        self, client: httpx.AsyncClient, bbox: str, expected_fragment: str
    ) -> None:
        response = await client.get("/api/v1/charging/stations", params={"bbox": bbox})

        assert response.status_code == 422
        error = assert_is_envelope(response, code=ErrorCode.validation_error.value)
        assert expected_fragment in error["message"].lower()
        assert error["details"] == {"parameter": "bbox", "value": bbox}

    async def test_west_greater_than_east_is_rejected_by_name(
        self, client: httpx.AsyncClient
    ) -> None:
        """An inverted box is syntactically fine and semantically empty — the worst kind of bug.

        The message names both numbers so the caller can see which way round they put them.
        """
        response = await client.get(
            "/api/v1/charging/stations", params={"bbox": "10.0,48.0,9.0,49.0"}
        )

        assert response.status_code == 422
        error = assert_is_envelope(response, code=ErrorCode.validation_error.value)
        assert "west" in error["message"] and "east" in error["message"]
        assert "10.0" in error["message"] and "9.0" in error["message"]

    async def test_south_greater_than_north_is_rejected(self, client: httpx.AsyncClient) -> None:
        response = await client.get(
            "/api/v1/charging/stations", params={"bbox": "8.0,49.0,9.0,48.0"}
        )

        assert response.status_code == 422
        error = assert_is_envelope(response, code=ErrorCode.validation_error.value)
        assert "south" in error["message"] and "north" in error["message"]

    async def test_a_box_outside_wgs84_is_rejected(self, client: httpx.AsyncClient) -> None:
        """A latitude of 91 degrees does not exist, and PostGIS would not say so politely."""
        response = await client.get(
            "/api/v1/charging/stations", params={"bbox": "8.0,48.0,9.0,91.0"}
        )
        assert response.status_code == 422


class TestRequestId:
    """``X-Request-ID`` — adopted when sane, minted otherwise, echoed always."""

    async def test_a_supplied_id_is_echoed_on_the_response_and_in_the_envelope(
        self, client: httpx.AsyncClient
    ) -> None:
        """Honouring the client's id is what makes one trace span the web app and this API."""
        response = await client.get(
            "/api/v1/does-not-exist", headers={REQUEST_ID_HEADER: "web-app-42"}
        )

        assert response.headers[REQUEST_ID_HEADER] == "web-app-42"
        assert response.json()["error"]["request_id"] == "web-app-42"

    async def test_an_id_is_minted_when_none_is_supplied(self, client: httpx.AsyncClient) -> None:
        """A bare uuid4 hex — short enough to paste into a support ticket."""
        first = await client.get("/health")
        second = await client.get("/health")

        minted = first.headers[REQUEST_ID_HEADER]
        assert len(minted) == 32
        assert int(minted, 16) >= 0  # it is hexadecimal
        assert minted != second.headers[REQUEST_ID_HEADER]

    async def test_an_absurdly_long_inbound_id_is_replaced_with_a_fresh_one(
        self, client: httpx.AsyncClient
    ) -> None:
        """The header-injection and log-forging defence.

        The value is echoed into a response header and into every log line of the request, so an
        unbounded one from an untrusted caller is a vector rather than a nuisance. The limit is
        128 characters; 300 is refused and a fresh id minted in its place — refused *quietly*,
        because rejecting the whole request would break a client over a header it can survive
        without.
        """
        absurd = "x" * 300
        response = await client.get("/health", headers={REQUEST_ID_HEADER: absurd})

        assert response.status_code == 200
        echoed = response.headers[REQUEST_ID_HEADER]
        assert echoed != absurd
        assert len(echoed) == 32

    @pytest.mark.parametrize(
        "candidate",
        [
            "x" * 129,  # one character past the limit
            "   ",  # whitespace only, which strips to empty
            "",
        ],
    )
    async def test_unusable_inbound_ids_are_replaced(
        self, client: httpx.AsyncClient, candidate: str
    ) -> None:
        response = await client.get("/health", headers={REQUEST_ID_HEADER: candidate})
        assert len(response.headers[REQUEST_ID_HEADER]) == 32

    async def test_an_id_at_the_length_limit_is_accepted(self, client: httpx.AsyncClient) -> None:
        """128 characters is inside the bound; the check is ``<=``, and this pins which side."""
        candidate = "a" * 128
        response = await client.get("/health", headers={REQUEST_ID_HEADER: candidate})
        assert response.headers[REQUEST_ID_HEADER] == candidate


class TestUnhandledErrors:
    """500 — the one response that must never leak what went wrong."""

    async def test_an_unexpected_exception_becomes_a_generic_envelope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No stack trace, no table name, no query fragment — only the correlation id.

        The traceback belongs in the logs; the request id is what lets an operator find it. A
        psycopg error string in the body would hand an attacker the schema.
        """
        from autotwin_api.routers import health as health_router

        def explode() -> tuple[bytes, str]:
            msg = 'relation "charging_stations" does not exist'
            raise RuntimeError(msg)

        monkeypatch.setattr(health_router, "render_metrics", explode)

        async with api_client(raise_app_exceptions=False) as client:
            response = await client.get("/metrics", headers={REQUEST_ID_HEADER: "trace-500"})

        assert response.status_code == 500
        error = assert_is_envelope(response, code=ErrorCode.internal_error.value)
        assert "charging_stations" not in error["message"]
        assert "RuntimeError" not in error["message"]
        # The generic 500 handler runs above the request-id middleware, so it sets the header
        # itself; that is the one response where the correlation id matters most.
        assert error["request_id"] == "trace-500"


class TestCopilot:
    """``POST /api/v1/copilot/ask`` — refusing honestly is the implementation, not a stub."""

    async def test_answers_503_configuration_missing_when_the_llm_is_disabled(
        self, client: httpx.AsyncClient
    ) -> None:
        """503 rather than 404 or 501: the route exists and a configured deployment answers it.

        Returning a plausible paragraph assembled from templates would be exactly the stub
        theatre BUILD_SPEC §0.4 forbids — and on a platform whose argument is that it never
        dresses generated content up as fact, a fake answer here would undermine every honest
        number elsewhere.
        """
        response = await client.post(
            "/api/v1/copilot/ask",
            json={"question": "Warum verbraucht die A8 im Winter mehr Energie?"},
        )

        assert response.status_code == 503
        error = assert_is_envelope(response, code=ErrorCode.configuration_missing.value)
        # The message has to name what is missing and what would change it.
        assert "AUTOTWIN_LLM_ENABLED" in error["message"]
        assert error["details"]["setting"] == "AUTOTWIN_LLM_ENABLED"

    async def test_an_empty_question_is_rejected_before_the_llm_check(
        self, client: httpx.AsyncClient
    ) -> None:
        """Validation runs first, so the client learns about its own bug rather than ours."""
        response = await client.post("/api/v1/copilot/ask", json={"question": ""})

        assert response.status_code == 422
        assert_is_envelope(response, code=ErrorCode.validation_error.value)

    async def test_an_unsupported_locale_is_rejected(self, client: httpx.AsyncClient) -> None:
        """The answer language is `de` or `en`; anything else is a request we cannot honour."""
        response = await client.post(
            "/api/v1/copilot/ask", json={"question": "Wieso?", "locale": "fr"}
        )
        assert response.status_code == 422


class TestOpenApiContract:
    """The generated document is what the frontend's types come from."""

    async def test_the_error_envelope_is_documented_on_every_router(
        self, client: httpx.AsyncClient
    ) -> None:
        """Without it ``openapi-typescript`` renders every failure body as ``unknown``.

        The frontend then loses the typed ``error.code`` it is supposed to switch on, and the
        error envelope becomes a convention instead of a contract.
        """
        document = (await client.get("/openapi.json")).json()
        responses = document["paths"]["/api/v1/charging/stations"]["get"]["responses"]

        assert {"422", "500", "503"} <= set(responses)
        schema_ref = responses["503"]["content"]["application/json"]["schema"]["$ref"]
        assert schema_ref.endswith("/ErrorResponse")

    async def test_the_data_mode_header_is_exposed_to_browsers(
        self, client: httpx.AsyncClient
    ) -> None:
        """A custom response header is invisible to ``fetch()`` unless CORS exposes it.

        Both the data-mode banner and the request-id correlation in the web app depend on this,
        and the failure mode is silent: the header arrives and the browser hides it. Asserted on
        an actual response rather than a preflight, because ``Access-Control-Expose-Headers``
        only ever travels on the real one.
        """
        response = await client.get("/health", headers={"Origin": "http://localhost:3000"})

        exposed = response.headers["access-control-expose-headers"]
        assert REQUEST_ID_HEADER in exposed
        assert DATA_MODE_HEADER in exposed
        assert response.headers["access-control-allow-origin"] == "http://localhost:3000"

    async def test_a_preflight_is_answered_for_the_web_app_origin(
        self, client: httpx.AsyncClient
    ) -> None:
        """CORS sits *outside* the timeout guard, so even a 504 reaches the browser readable.

        Without that a browser reports "network error" for what is really a 503, and the
        frontend's error state never renders.
        """
        response = await client.options(
            "/api/v1/charging/stations",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )

        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
        assert "GET" in response.headers["access-control-allow-methods"]
