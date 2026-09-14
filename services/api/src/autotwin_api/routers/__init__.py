"""HTTP routers, one module per resource group of BUILD_SPEC §7.

**The convention is binding.** Every module in this package exposes exactly one name::

    router: APIRouter        # created WITHOUT a prefix and WITHOUT tags

Prefixes and tags belong to :func:`autotwin_api.app.create_app`, which mounts each router at
its documented base path. Keeping them out of the modules means the URL layout of the whole
API is readable in one place, and a router cannot quietly move itself.

Nothing is imported here: the app factory imports each module by name, so a module that does
not exist yet is skipped with a warning instead of breaking every other endpoint.
"""

from __future__ import annotations

__all__: list[str] = []
