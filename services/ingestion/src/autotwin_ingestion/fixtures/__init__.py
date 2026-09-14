"""Bundled fixtures — the last rung of the ``live → cache → fixture`` chain (BUILD_SPEC §4).

Every file next to this module is a *real* response captured from the German source it is
named after, trimmed only in row count, never in shape. That distinction is the whole point:
the fixture keeps the ten-line Bundesnetzagentur preamble, the ISO-8859-1 encoding of the DWD
product files and the lat-first ``point`` strings of the Autobahn API, so the parser exercised
offline is byte-for-byte the parser that runs against production.

Provenance for each file — exact URL, capture date, what was trimmed, licence — is in
``README.md`` in this directory.

Resolution order used by the adapters (:func:`resolve_fixture`):

1. ``AUTOTWIN_DATA_DIR/fixtures/<name>`` — an operator-supplied override, so a deployment can
   pin a newer snapshot without a code change or a new wheel.
2. the copy bundled here, which ships inside the wheel and is therefore always present.

Both are read-only from the adapters' point of view; nothing in this package ever writes to a
fixture path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from autotwin_core.config import get_settings

__all__ = [
    "AUTOBAHN_CLOSURE_FIXTURE",
    "AUTOBAHN_ROADS_FIXTURE",
    "AUTOBAHN_ROADWORKS_FIXTURE",
    "AUTOBAHN_WARNING_FIXTURE",
    "BNETZA_CSV_FIXTURE",
    "BUNDLED_FIXTURES_DIR",
    "DWD_PRODUCT_FIXTURES",
    "DWD_STATIONS_FIXTURE",
    "NOMINATIM_FIXTURE",
    "OSRM_ROUTE_FIXTURE",
    "fixture_bytes",
    "fixture_text",
    "resolve_fixture",
]

BUNDLED_FIXTURES_DIR: Final[Path] = Path(__file__).parent
"""Directory holding the committed sample payloads."""

BNETZA_CSV_FIXTURE: Final[str] = "bnetza_ladesaeulenregister_sample.csv"
"""Ladesäulenregister excerpt: original preamble, original 47-column header, 200 data rows."""

DWD_STATIONS_FIXTURE: Final[str] = "dwd_zehn_now_tu_Beschreibung_Stationen.txt"
"""Excerpt of the DWD 10-minute station catalogue, in its original fixed-width ISO-8859-1 form."""

DWD_PRODUCT_FIXTURES: Final[dict[int, dict[str, str]]] = {
    1424: {
        "air_temperature": "dwd_produkt_zehn_now_tu_01424.txt",
        "precipitation": "dwd_produkt_zehn_now_rr_01424.txt",
    },
    4928: {
        "air_temperature": "dwd_produkt_zehn_now_tu_04928.txt",
        "wind": "dwd_produkt_zehn_now_ff_04928.txt",
        "precipitation": "dwd_produkt_zehn_now_rr_04928.txt",
    },
}
"""Station number → product → extracted ``produkt_*.txt``.

The two stations are the ones nearest the demo corridor endpoints — 01424 Frankfurt/Main-
Westend and 04928 Stuttgart (Schnarrenberg) — so a Frankfurt → Stuttgart analysis still has
real temperatures behind it with the network unplugged. Station 01424 has no wind file
upstream either: that 404 is a property of the station, and the fixture reproduces it rather
than papering over it.
"""

AUTOBAHN_ROADS_FIXTURE: Final[str] = "autobahn_roads.json"
"""``GET /o/autobahn/`` — the road index, trailing whitespace of ``"A60 "`` preserved."""

AUTOBAHN_ROADWORKS_FIXTURE: Final[str] = "autobahn_A5_roadworks.json"
"""``GET /o/autobahn/A5/services/roadworks`` — both ``display_type`` variants represented."""

AUTOBAHN_CLOSURE_FIXTURE: Final[str] = "autobahn_A5_closure.json"
"""``GET /o/autobahn/A5/services/closure``."""

AUTOBAHN_WARNING_FIXTURE: Final[str] = "autobahn_A5_warning.json"
"""``GET /o/autobahn/A5/services/warning`` — includes a live INRIX congestion report."""

OSRM_ROUTE_FIXTURE: Final[str] = "osrm_frankfurt_stuttgart.json"
"""A real OSRM answer for the demo corridor Frankfurt am Main → Stuttgart, untrimmed."""

NOMINATIM_FIXTURE: Final[str] = "nominatim_places.json"
"""Normalised query → raw ``jsonv2`` result array, covering the six demo cities."""


def resolve_fixture(name: str) -> Path:
    """Return the path of fixture ``name``, preferring an operator override.

    The override directory is consulted first so that a deployment can refresh a snapshot by
    dropping a file into ``AUTOTWIN_DATA_DIR/fixtures`` — useful when the live source has been
    down long enough that last quarter's bundled sample is misleading.

    Raises:
        FileNotFoundError: neither location holds the file. Adapters catch this and re-raise
            it as :class:`~autotwin_core.errors.ConfigurationMissing`, because a missing
            fixture means the deployment is incomplete, not that the source is down.
    """
    override = Path(get_settings().fixtures_dir) / name
    if override.is_file():
        return override
    bundled = BUNDLED_FIXTURES_DIR / name
    if bundled.is_file():
        return bundled
    msg = f"fixture {name!r} was found neither in {override.parent} nor in {BUNDLED_FIXTURES_DIR}"
    raise FileNotFoundError(msg)


def fixture_bytes(name: str) -> bytes:
    """Read a fixture as raw bytes — for ZIP and byte-exact encoding tests."""
    return resolve_fixture(name).read_bytes()


def fixture_text(name: str, *, encoding: str = "utf-8") -> str:
    """Read a fixture as text.

    ``encoding`` is explicit at every call site on purpose: the DWD files are ISO-8859-1 and
    the Bundesnetzagentur file is UTF-8 *with* a BOM, and defaulting either of them silently
    is exactly the bug the fixtures exist to catch.
    """
    return resolve_fixture(name).read_text(encoding=encoding)
