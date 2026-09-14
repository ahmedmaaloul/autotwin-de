"""Response models of the AutoTwin API, one module per router.

Endpoint-specific payloads live here rather than in ``autotwin_contracts`` because they change
with their endpoint and nothing outside the API needs them; only the genuinely cross-cutting
envelopes (``Page``, ``ErrorResponse``, ``HealthResponse``) are shared.

Only the pieces from :mod:`autotwin_api.schemas.common` are re-exported here. Importing every
sibling module would make this package's import graph depend on all of them at once, which is
exactly the coupling that makes one broken schema module break every endpoint.
"""

from __future__ import annotations

from autotwin_api.schemas.common import ApiModel, CoordinateOut, GeoPointOut, ProvenanceOut

__all__ = [
    "ApiModel",
    "CoordinateOut",
    "GeoPointOut",
    "ProvenanceOut",
]
