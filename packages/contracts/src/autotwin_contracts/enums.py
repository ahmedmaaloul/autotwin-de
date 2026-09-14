"""Canonical enumerations of AutoTwin DE (BUILD_SPEC §2).

These member *values* are written into PostgreSQL, returned by the API and consumed by the
TypeScript frontend, so they are a wire format: renaming a member is a breaking change to
three layers at once.

All enums derive from :class:`enum.StrEnum`. A ``StrEnum`` member *is* a ``str``, which means
Pydantic, ``json.dumps`` and SQLAlchemy all round-trip it as its plain value without any
custom serialiser. Member names are deliberately identical to their values so that
``Enum.name``-based persistence (SQLAlchemy's default for ``Enum`` columns) and
``Enum.value``-based serialisation cannot drift apart.

Several enums carry small classmethods that normalise messy real-world input (German
authority spellings, OSM tags, raw power ratings). They live next to the enum rather than in
the adapters because every adapter and every test must agree on the same mapping.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Final

__all__ = [
    "Bundesland",
    "ChargingCategory",
    "ConnectorType",
    "CurrentType",
    "DataOrigin",
    "EnergyIntensity",
    "IngestionOutcome",
    "IngestionStatus",
    "ProviderMode",
    "RoadClass",
    "SimulationState",
    "SourceSystem",
    "TrafficEventType",
    "TrafficSeverity",
    "VehicleClass",
    "VehicleState",
    "WeatherCondition",
]


class DataOrigin(StrEnum):
    """Where a row's *content* ultimately comes from.

    The honesty rule of the project (BUILD_SPEC §0.2) hangs on this enum: simulated rows are
    never allowed to look official, and the UI renders a ``SIMULIERT`` badge for them.
    """

    official = "official"
    """Published by a German authority or an official open-data portal."""

    simulated = "simulated"
    """Produced by the AutoTwin vehicle simulator."""

    derived = "derived"
    """Computed by AutoTwin from other rows (segmentation, predictions, aggregates)."""


class SourceSystem(StrEnum):
    """The concrete system that produced a row, for provenance and per-source quality views."""

    bundesnetzagentur = "bundesnetzagentur"
    """Ladesäulenregister of the Bundesnetzagentur (charging infrastructure)."""

    dwd = "dwd"
    """Deutscher Wetterdienst open data (weather observations and stations)."""

    autobahn = "autobahn"
    """Autobahn GmbH public API (roadworks, closures, warnings on the A-network)."""

    mobilithek = "mobilithek"
    """Mobilithek / MDM — federal mobility data platform."""

    osm = "osm"
    """OpenStreetMap, including Nominatim geocoding."""

    osrm = "osrm"
    """OSRM routing engine (public demo server or a local container)."""

    simulator = "simulator"
    """The AutoTwin vehicle simulator."""

    derived = "derived"
    """AutoTwin's own analytical output."""


class IngestionStatus(StrEnum):
    """Health of a *source* as shown on the data-quality and dashboard surfaces."""

    healthy = "healthy"
    """Last run succeeded and the data is fresh."""

    delayed = "delayed"
    """Last run succeeded but the data is older than its expected refresh interval."""

    degraded = "degraded"
    """Answered from cache or fixture because the live source was unreachable."""

    failed = "failed"
    """Last run failed; no usable data was produced."""

    simulation = "simulation"
    """Not an external source at all — the simulator feeds this stream."""


class IngestionOutcome(StrEnum):
    """Result of a single pipeline execution (``data_ingestion_runs.status``)."""

    success = "success"
    """Every received row was accepted or was a known duplicate."""

    partial = "partial"
    """Some rows were rejected by quality rules; the rest were written."""

    failed = "failed"
    """The run aborted; nothing was written."""


class ChargingCategory(StrEnum):
    """Power class of a charging site, using the thresholds of BUILD_SPEC §2."""

    normal = "normal"
    """< 22 kW — AC wallboxes and household-grade outlets."""

    fast = "fast"
    """22-149 kW — AC 22 kW and the common 50 kW / 100 kW DC chargers."""

    ultra_fast = "ultra_fast"
    """>= 150 kW — HPC sites relevant for motorway corridor travel."""

    @classmethod
    def from_power(cls, kw: float | None) -> ChargingCategory:
        """Classify a site by its strongest charging point.

        Sites with an unknown rating are treated as ``normal`` rather than dropped: the
        Ladesäulenregister does occasionally omit the power column, and silently promoting
        such a site into the fast-charger statistics would overstate the network.
        """
        if kw is None or kw < 22.0:
            return cls.normal
        if kw < 150.0:
            return cls.fast
        return cls.ultra_fast


class CurrentType(StrEnum):
    """Current type of a charging point."""

    ac = "ac"
    dc = "dc"
    unknown = "unknown"


class ConnectorType(StrEnum):
    """Physical connector standard of a charging point."""

    type2 = "type2"
    """IEC 62196 Type 2 (Mennekes) — socket or tethered cable."""

    ccs = "ccs"
    """Combined Charging System / CCS Combo 2, the German DC standard."""

    chademo = "chademo"
    """CHAdeMO, still present at older DC sites."""

    schuko = "schuko"
    """CEE 7/4 household outlet ("Schuko") — emergency charging only."""

    tesla = "tesla"
    """Tesla-proprietary connector (rare in Germany, where Superchargers use CCS)."""

    cee = "cee"
    """Industrial CEE ("Kraftstrom") outlet, blue or red."""

    other = "other"
    """A recognised connector outside this vocabulary (e.g. Type 1)."""

    unknown = "unknown"
    """The source did not say."""

    @classmethod
    def parse(cls, raw: str | None) -> ConnectorType:
        """Map a raw German source spelling onto the canonical vocabulary.

        The Bundesnetzagentur register spells connectors as free text in German, e.g.
        ``"AC Steckdose Typ 2"``, ``"DC Kupplung Combo"`` or ``"AC CEE 5 polig"``, and the
        spelling varies between releases of the file. Matching is done on an alphanumeric
        squash of the input so that ``"Typ 2"``, ``"Typ2"`` and ``"TYP-2"`` are one case.

        Order matters: ``CHAdeMO`` and ``Combo`` are tested before ``Typ 2`` because a single
        cell may name both the DC standard and its Type-2 signalling connector.
        """
        if raw is None:
            return cls.unknown
        squashed = "".join(ch for ch in raw.casefold() if ch.isalnum())
        if not squashed or squashed in _CONNECTOR_UNKNOWN_TOKENS:
            return cls.unknown
        if "chademo" in squashed:
            return cls.chademo
        if "combo" in squashed or "ccs" in squashed:
            return cls.ccs
        if "tesla" in squashed or "supercharger" in squashed:
            return cls.tesla
        if "schuko" in squashed or "haushalt" in squashed or "typf" in squashed:
            return cls.schuko
        if "cee" in squashed or "kraftstrom" in squashed or "starkstrom" in squashed:
            return cls.cee
        if "typ2" in squashed or "type2" in squashed or "mennekes" in squashed:
            return cls.type2
        if "typ1" in squashed or "type1" in squashed or "j1772" in squashed:
            return cls.other
        return cls.other


_CONNECTOR_UNKNOWN_TOKENS: Final[frozenset[str]] = frozenset(
    {"unknown", "unbekannt", "keineangabe", "na", "none", "null", "sonstige", "sonstiges"}
)


class Bundesland(StrEnum):
    """The sixteen German federal states, as ISO 3166-2:DE codes without the ``DE-`` prefix.

    Codes rather than names are stored because they are stable, short, and safe to use as map
    keys and URL query values; the German label is rendered from :attr:`label_de`.
    """

    BW = "BW"
    BY = "BY"
    BE = "BE"
    BB = "BB"
    HB = "HB"
    HH = "HH"
    HE = "HE"
    MV = "MV"
    NI = "NI"
    NW = "NW"
    RP = "RP"
    SL = "SL"
    SN = "SN"
    ST = "ST"
    SH = "SH"
    TH = "TH"

    @property
    def label_de(self) -> str:
        """The official German name of the state, for UI labels and report text."""
        return _BUNDESLAND_LABELS_DE[self]

    @classmethod
    def from_name(cls, name: str | None) -> Bundesland | None:
        """Resolve a state from a code or a name, tolerating real-world spelling variance.

        German open data arrives with umlauts (``Baden-Württemberg``), transliterations
        (``Baden-Wuerttemberg``), ISO prefixes (``DE-BW``), colloquial abbreviations (``NRW``)
        and English names (``Bavaria``). All of them normalise to the same key here, so
        adapters never have to carry their own lookup table.

        Returns ``None`` for anything unrecognised — an unmapped state is a data-quality
        finding, not a reason to crash the pipeline.
        """
        key = _normalise_place_key(name)
        if not key:
            return None
        if key in _BUNDESLAND_ALIASES:
            return _BUNDESLAND_ALIASES[key]
        # "DE-BW" / "DE BW" squash to "debw"; strip the country prefix and retry once.
        if key.startswith("de") and key[2:] in _BUNDESLAND_ALIASES:
            return _BUNDESLAND_ALIASES[key[2:]]
        return None


_UMLAUT_TRANSLITERATION: Final[tuple[tuple[str, str], ...]] = (
    ("ä", "ae"),
    ("ö", "oe"),
    ("ü", "ue"),
    ("ß", "ss"),
)


def _normalise_place_key(raw: str | None) -> str:
    """Fold a place name to a comparison key: lower-case, transliterated, alphanumeric only.

    Accepts ``None`` because the columns this is fed from — the Bundesnetzagentur
    ``Bundesland`` field, the DWD station list, OSM's ``address.state`` — are all nullable,
    and forcing every adapter to guard the call would just spread the same check around.
    """
    if raw is None:
        return ""
    folded = raw.strip().casefold()
    for umlaut, replacement in _UMLAUT_TRANSLITERATION:
        folded = folded.replace(umlaut, replacement)
    return "".join(ch for ch in folded if ch.isalnum())


_BUNDESLAND_LABELS_DE: Final[MappingProxyType[Bundesland, str]] = MappingProxyType(
    {
        Bundesland.BW: "Baden-Württemberg",
        Bundesland.BY: "Bayern",
        Bundesland.BE: "Berlin",
        Bundesland.BB: "Brandenburg",
        Bundesland.HB: "Bremen",
        Bundesland.HH: "Hamburg",
        Bundesland.HE: "Hessen",
        Bundesland.MV: "Mecklenburg-Vorpommern",
        Bundesland.NI: "Niedersachsen",
        Bundesland.NW: "Nordrhein-Westfalen",
        Bundesland.RP: "Rheinland-Pfalz",
        Bundesland.SL: "Saarland",
        Bundesland.SN: "Sachsen",
        Bundesland.ST: "Sachsen-Anhalt",
        Bundesland.SH: "Schleswig-Holstein",
        Bundesland.TH: "Thüringen",
    }
)

_BUNDESLAND_SPELLINGS: Final[tuple[tuple[Bundesland, tuple[str, ...]], ...]] = (
    (Bundesland.BW, ("Baden-Württemberg", "Baden-Wuerttemberg", "Baden-Wurttemberg", "BaWü")),
    (Bundesland.BY, ("Bayern", "Freistaat Bayern", "Bavaria", "Bay")),
    (Bundesland.BE, ("Berlin",)),
    (Bundesland.BB, ("Brandenburg",)),
    (Bundesland.HB, ("Bremen", "Freie Hansestadt Bremen")),
    (Bundesland.HH, ("Hamburg", "Freie und Hansestadt Hamburg")),
    (Bundesland.HE, ("Hessen", "Hesse")),
    (Bundesland.MV, ("Mecklenburg-Vorpommern", "Mecklenburg Vorpommern", "MeckPomm")),
    (Bundesland.NI, ("Niedersachsen", "Lower Saxony", "NDS")),
    (Bundesland.NW, ("Nordrhein-Westfalen", "NRW", "North Rhine-Westphalia")),
    (Bundesland.RP, ("Rheinland-Pfalz", "Rhineland-Palatinate")),
    (Bundesland.SL, ("Saarland",)),
    (Bundesland.SN, ("Sachsen", "Freistaat Sachsen", "Saxony")),
    (Bundesland.ST, ("Sachsen-Anhalt", "Saxony-Anhalt")),
    (Bundesland.SH, ("Schleswig-Holstein",)),
    (Bundesland.TH, ("Thüringen", "Thueringen", "Freistaat Thüringen", "Thuringia")),
)


def _build_bundesland_aliases() -> MappingProxyType[str, Bundesland]:
    """Build the normalised alias table once, from the readable spelling table above."""
    aliases: dict[str, Bundesland] = {}
    for land in Bundesland:
        aliases[_normalise_place_key(land.value)] = land
        aliases[_normalise_place_key(land.label_de)] = land
    for land, spellings in _BUNDESLAND_SPELLINGS:
        for spelling in spellings:
            aliases[_normalise_place_key(spelling)] = land
    return MappingProxyType(aliases)


_BUNDESLAND_ALIASES: Final[MappingProxyType[str, Bundesland]] = _build_bundesland_aliases()


class RoadClass(StrEnum):
    """Functional road class, aligned with the OSM ``highway`` tag hierarchy."""

    motorway = "motorway"
    """Autobahn."""

    trunk = "trunk"
    """Kraftfahrstraße / autobahn-like federal road."""

    primary = "primary"
    """Bundesstraße."""

    secondary = "secondary"
    """Landesstraße."""

    tertiary = "tertiary"
    """Kreisstraße."""

    residential = "residential"
    """Built-up area street."""

    service = "service"
    """Access road, parking aisle, service way."""

    unknown = "unknown"
    """Unclassified or missing in the source."""

    @property
    def ordinal(self) -> int:
        """Monotone rank (0-6) used directly as the ``road_class_ordinal`` ML feature.

        Tree models split on numbers, so the class has to be ordered rather than one-hot
        encoded to keep the feature vector of BUILD_SPEC §10.2 compact. ``residential`` and
        ``service`` share rank 1 because both are low-speed access roads whose energy
        behaviour is indistinguishable at the resolution of this model.
        """
        return _ROAD_CLASS_ORDINALS[self]

    @classmethod
    def from_osm(cls, highway: str | None) -> RoadClass:
        """Map an OSM ``highway`` tag onto the canonical class.

        ``*_link`` ramps inherit the class of the road they serve, which is what matters for
        energy: a motorway ramp is still motorway driving.
        """
        if highway is None:
            return cls.unknown
        tag = highway.strip().casefold().removesuffix("_link")
        return _OSM_HIGHWAY_MAP.get(tag, cls.unknown)


_ROAD_CLASS_ORDINALS: Final[MappingProxyType[RoadClass, int]] = MappingProxyType(
    {
        RoadClass.motorway: 6,
        RoadClass.trunk: 5,
        RoadClass.primary: 4,
        RoadClass.secondary: 3,
        RoadClass.tertiary: 2,
        RoadClass.residential: 1,
        RoadClass.service: 1,
        RoadClass.unknown: 0,
    }
)

_OSM_HIGHWAY_MAP: Final[MappingProxyType[str, RoadClass]] = MappingProxyType(
    {
        "motorway": RoadClass.motorway,
        "trunk": RoadClass.trunk,
        "primary": RoadClass.primary,
        "secondary": RoadClass.secondary,
        "tertiary": RoadClass.tertiary,
        "residential": RoadClass.residential,
        "living_street": RoadClass.residential,
        "unclassified": RoadClass.residential,
        "service": RoadClass.service,
        "track": RoadClass.service,
    }
)


class TrafficEventType(StrEnum):
    """Kind of traffic disruption reported by Autobahn GmbH or the Mobilithek."""

    roadworks = "roadworks"
    """Baustelle."""

    closure = "closure"
    """Sperrung — full or directional closure."""

    incident = "incident"
    """Unfall or other unplanned blocking event."""

    warning = "warning"
    """Verkehrsmeldung / Gefahrenmeldung without a closure."""

    congestion = "congestion"
    """Stau or slow-moving traffic."""

    other = "other"
    """Reported, but outside this vocabulary."""


class TrafficSeverity(StrEnum):
    """How strongly an event is expected to affect travel time."""

    low = "low"
    moderate = "moderate"
    high = "high"
    severe = "severe"

    @property
    def ordinal(self) -> int:
        """Rank 1-4, used as the ``traffic_severity_ordinal`` ML feature."""
        return _TRAFFIC_SEVERITY_ORDINALS[self]

    @property
    def delay_factor(self) -> float:
        """Multiplier on free-flow travel time for the affected segment.

        Deliberately coarse and documented: the sources give a category, not a measured
        delay, so inventing a finer scale would be false precision. Stop-and-go traffic
        roughly doubles the time on a motorway segment, which anchors ``severe`` at 2.0.
        """
        return _TRAFFIC_SEVERITY_DELAY_FACTORS[self]


_TRAFFIC_SEVERITY_ORDINALS: Final[MappingProxyType[TrafficSeverity, int]] = MappingProxyType(
    {
        TrafficSeverity.low: 1,
        TrafficSeverity.moderate: 2,
        TrafficSeverity.high: 3,
        TrafficSeverity.severe: 4,
    }
)

_TRAFFIC_SEVERITY_DELAY_FACTORS: Final[MappingProxyType[TrafficSeverity, float]] = MappingProxyType(
    {
        TrafficSeverity.low: 1.0,
        TrafficSeverity.moderate: 1.15,
        TrafficSeverity.high: 1.4,
        TrafficSeverity.severe: 2.0,
    }
)


class WeatherCondition(StrEnum):
    """Coarse weather condition, derived from DWD observation parameters."""

    clear = "clear"
    clouds = "clouds"
    rain = "rain"
    snow = "snow"
    fog = "fog"
    storm = "storm"
    unknown = "unknown"


class VehicleClass(StrEnum):
    """Vehicle segment of a generic EV profile (BUILD_SPEC §9)."""

    compact = "compact"
    sedan = "sedan"
    performance = "performance"
    suv = "suv"
    van = "van"


class SimulationState(StrEnum):
    """Lifecycle of a simulation run, as driven by the ``/simulations`` control endpoints."""

    pending = "pending"
    """Created and configured, not yet started."""

    running = "running"
    paused = "paused"

    stopping = "stopping"
    """Stop requested; the runner is draining its current tick."""

    stopped = "stopped"
    """Stopped by an operator before completion."""

    completed = "completed"
    """Reached its natural end."""

    failed = "failed"
    """Aborted on an unrecoverable error."""


class VehicleState(StrEnum):
    """Lifecycle of a single simulated vehicle and of its current trip."""

    idle = "idle"
    """Ready, not on a trip."""

    driving = "driving"
    charging = "charging"

    stopped = "stopped"
    """Halted mid-trip (traffic standstill or operator stop)."""

    completed = "completed"
    """Finished its route."""


class EnergyIntensity(StrEnum):
    """How a segment's predicted consumption compares with the vehicle's nominal figure."""

    low = "low"
    """Below 95 % of nominal — regeneration, downhill, light load."""

    medium = "medium"
    """95-110 % of nominal — unremarkable driving."""

    high = "high"
    """110-130 % of nominal — cold, fast or hilly."""

    critical = "critical"
    """Above 130 % of nominal — range planning must account for it explicitly."""

    @classmethod
    def from_ratio(cls, actual: float, nominal: float) -> EnergyIntensity:
        """Bucket ``actual / nominal`` consumption onto the four-level scale.

        ``nominal`` is a vehicle constant (kWh/100 km from the profile), so a non-positive
        value means the caller mixed up its arguments — that is a programming error and is
        raised rather than silently bucketed.
        """
        if nominal <= 0.0:
            msg = f"nominal consumption must be positive, got {nominal!r}"
            raise ValueError(msg)
        ratio = actual / nominal
        if ratio < 0.95:
            return cls.low
        if ratio < 1.1:
            return cls.medium
        if ratio < 1.3:
            return cls.high
        return cls.critical


class ProviderMode(StrEnum):
    """How a provider answered — surfaced to the UI via ``X-AutoTwin-Data-Mode``."""

    live = "live"
    """Fetched from the upstream source during this request."""

    cache = "cache"
    """Served from the on-disk cache because the live source was unavailable or still fresh."""

    fixture = "fixture"
    """Served from a bundled fixture — offline development, CI, or a total outage."""
