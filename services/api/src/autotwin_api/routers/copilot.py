"""``POST /api/v1/copilot/ask`` — optional natural-language questions about the twin.

**Superseded by the MCP server** (``services/mcp``, ``uv run python -m autotwin_mcp``): the
supported way to ask a model about the twin is to connect an MCP client, which calls the
platform's tools and answers from real output. This endpoint remains as an honest 503 so
that the documented API surface (BUILD_SPEC §7) does not silently lose a route.


This endpoint answers **503 `configuration_missing`** in this build, and that is the correct
implementation rather than a placeholder.

Two independent reasons, both reported:

1. ``AUTOTWIN_LLM_ENABLED`` is false by default. The platform's zero-cost rule (BUILD_SPEC §0.1)
   rules out a hosted model, so the copilot is designed around a local Ollama instance that a
   developer opts into.
2. No ``LLMProvider`` adapter is registered in this deployment. ``autotwin_core.providers``
   declares the interface; nothing implements it yet.

What this endpoint must **not** do is more important than what it does. Returning a plausible
paragraph assembled from templates would be exactly the "stub theatre" BUILD_SPEC §0.4 forbids,
and on a platform whose entire argument is that it never dresses up generated content as fact,
a fake answer here would undermine every honest number elsewhere.

The insight the copilot would verbalise already exists without it:
``autotwin_ml.insights.explain_route_energy`` decomposes a route's consumption into ranked,
bilingual drivers deterministically, and ``RouteAnalysis.explanation`` carries it (BUILD_SPEC
§11). The copilot is a phrasing layer over that, never its source.
"""

from __future__ import annotations

from typing import ClassVar

from fastapi import APIRouter
from pydantic import Field

from autotwin_api.deps import AppSettings
from autotwin_api.schemas.common import ApiModel
from autotwin_core.errors import AutoTwinError
from autotwin_core.logging import get_logger

__all__ = ["router"]

_LOGGER = get_logger(__name__)

router = APIRouter()


class LlmUnavailableError(AutoTwinError):
    """The copilot is not configured in this deployment.

    503 rather than 404 or 501: the route exists and a deployment with a local model reachable
    would answer it, so a client that retries against a configured instance succeeds. The code
    matches the one ``/api/v1/ml/*`` uses when nothing is trained, so a frontend has a single
    branch for "this deployment is not set up for that".
    """

    code: ClassVar[str] = "configuration_missing"
    http_status: ClassVar[int] = 503


class CopilotAsk(ApiModel):
    """A question about the platform's data."""

    question: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="The question, in German or English.",
        examples=["Warum verbraucht die A8 im Winter mehr Energie?"],
    )
    locale: str = Field(
        default="de",
        pattern="^(de|en)$",
        description="Language the answer should be written in.",
    )
    context: dict[str, str] = Field(
        default_factory=dict,
        description="Optional page context, e.g. the route slug the user is looking at.",
    )


class CopilotAnswer(ApiModel):
    """A copilot answer with the evidence it was grounded in.

    Declared even though this build never returns one, because it is the contract: an answer is
    only ever a rendering of citations the platform can produce itself, never free generation.
    """

    answer: str = Field(..., description="The answer text, in the requested language.")
    citations: list[str] = Field(
        default_factory=list,
        description="Endpoints and rows the answer was grounded in.",
    )
    model: str = Field(..., description="Local model that produced the text, e.g. `llama3.2`.")
    generated: bool = Field(
        default=True,
        description="Always true — the text is model output, and must be labelled as such.",
    )


@router.post(
    "/ask",
    response_model=CopilotAnswer,
    summary="Ask the copilot (optional feature)",
    description=(
        "Superseded by the MCP server in `services/mcp`: connect an MCP client to ask a model "
        "about the twin.\n\n"
        "Disabled in this deployment: returns **503** with code `configuration_missing` and a "
        "message naming what is missing. It never returns a fabricated answer — a plausible "
        "paragraph with nothing behind it would undermine every honest figure this API "
        "serves.\n\n"
        "The route analysis already explains itself without a model: "
        "`POST /api/v1/routes/analyze` returns `explanation.drivers`, a deterministic ranked "
        "decomposition of the energy with bilingual labels (BUILD_SPEC §11)."
    ),
)
async def ask(payload: CopilotAsk, settings: AppSettings) -> CopilotAnswer:
    """Refuse, with the reason and the action that would change it.

    Raises:
        LlmUnavailableError: Always in this build — ``503 configuration_missing``.
    """
    _LOGGER.info(
        "copilot.unavailable",
        llm_enabled=settings.llm_enabled,
        question_length=len(payload.question),
    )
    if not settings.llm_enabled:
        msg = (
            "the copilot is switched off in this deployment (AUTOTWIN_LLM_ENABLED=false). "
            "Set it to true and run a local Ollama instance at "
            f"{settings.ollama_base_url} serving {settings.ollama_model!r} to enable it. "
            "Route explanations do not need it: POST /api/v1/routes/analyze returns a "
            "deterministic, bilingual breakdown of the energy under `explanation`."
        )
        raise LlmUnavailableError(
            msg,
            details={
                "setting": "AUTOTWIN_LLM_ENABLED",
                "ollama_base_url": settings.ollama_base_url,
                "ollama_model": settings.ollama_model,
            },
        )

    msg = (
        "AUTOTWIN_LLM_ENABLED is true but no LLMProvider adapter is registered in this build. "
        "autotwin_core.providers declares the interface; no implementation is wired to the "
        "provider registry, and this endpoint will not fabricate an answer instead."
    )
    raise LlmUnavailableError(msg, details={"provider": "llm", "registered": False})
