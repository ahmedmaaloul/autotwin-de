"""The Autobahn GmbH traffic adapter.

``https://verkehr.autobahn.de/o/autobahn/`` is keyless and undocumented, and its payload has
four properties that a reasonable reader gets wrong. This file exists to pin all four:

1. the coordinate object's longitude key is ``"long"`` — reading ``lon`` yields ``None`` and
   puts every German event on the Greenwich meridian off the coast of Ghana;
2. ``isBlocked`` is the *string* ``"false"`` on every item ever sampled, so it is a dead field;
   the real blocking signal is ``"CLOSED" in impact.symbols``;
3. ``startTimestamp`` is absent for ``SHORT_TERM_ROADWORKS``, and the German prose is then the
   only statement of when the work happens;
4. the free-text fields arrive dirty — ``subtitle`` with a leading space, road ids as ``"A60 "``.

Times inside ``description`` are German civil time. The fixture proves it: the warning
``NLW_2026_002476`` carries ``startTimestamp = 2026-07-29T12:18:00+02:00`` next to the line
``Beginn: 29.07.26 um 12:18 Uhr``. Same instant, so the prose clock is ``Europe/Berlin``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from autotwin_contracts import (
    BoundingBox,
    Coordinate,
    DataOrigin,
    ProviderMode,
    SourceSystem,
    TrafficEventRecord,
    TrafficEventType,
    TrafficSeverity,
)
from autotwin_core.config import DataMode
from autotwin_ingestion.providers.traffic import (
    CLOSED_SYMBOL,
    DEFAULT_ROADS,
    SERVICES,
    AutobahnTrafficProvider,
    derive_severity,
    extract_validity,
    parse_item,
)

ROADWORKS_FIXTURE: Final[str] = "autobahn_A5_roadworks.json"
CLOSURE_FIXTURE: Final[str] = "autobahn_A5_closure.json"
WARNING_FIXTURE: Final[str] = "autobahn_A5_warning.json"
ROADS_FIXTURE: Final[str] = "autobahn_roads.json"

FIXTURE_ROAD: Final[str] = "A5"

# The item quoted in the module docstring, and the one used for most single-item assertions.
FIRST_ROADWORKS_ID: Final[str] = (
    "2025-062919--vi-bs.2026-04-26_05-00-00-000.devi-zus.2026-04-19_20-00-00-000.de1"
)
FIRST_ROADWORKS_LAT: Final[float] = 47.70136455397145
FIRST_ROADWORKS_LON: Final[float] = 7.521931811345328


def load(fixtures_dir: Path, name: str, key: str) -> list[Mapping[str, Any]]:
    """Read one bundled service response and return its item list."""
    payload = json.loads((fixtures_dir / name).read_text(encoding="utf-8"))
    items: list[Mapping[str, Any]] = payload[key]
    return items


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def roadworks(fixtures_dir: Path) -> list[Mapping[str, Any]]:
    return load(fixtures_dir, ROADWORKS_FIXTURE, "roadworks")


@pytest.fixture
def closures(fixtures_dir: Path) -> list[Mapping[str, Any]]:
    return load(fixtures_dir, CLOSURE_FIXTURE, "closure")


@pytest.fixture
def warnings_(fixtures_dir: Path) -> list[Mapping[str, Any]]:
    return load(fixtures_dir, WARNING_FIXTURE, "warning")


@pytest.fixture
def all_items(
    roadworks: list[Mapping[str, Any]],
    closures: list[Mapping[str, Any]],
    warnings_: list[Mapping[str, Any]],
) -> list[tuple[str, Mapping[str, Any]]]:
    """Every bundled item tagged with the service that published it."""
    return [
        *(("roadworks", item) for item in roadworks),
        *(("closure", item) for item in closures),
        *(("warning", item) for item in warnings_),
    ]


@pytest.fixture
def first_roadworks(roadworks: list[Mapping[str, Any]]) -> TrafficEventRecord:
    # Pinned by identifier, so a re-captured fixture that reorders its items fails loudly here
    # rather than quietly making every expectation below describe a different roadworks site.
    assert roadworks[0]["identifier"] == FIRST_ROADWORKS_ID
    record = parse_item(roadworks[0], road=FIXTURE_ROAD, service="roadworks", source_url=None)
    assert record is not None
    assert record.external_id == FIRST_ROADWORKS_ID
    return record


@pytest.fixture
def provider() -> AutobahnTrafficProvider:
    """Fixture-mode adapter restricted to the road the samples were captured from."""
    return AutobahnTrafficProvider(data_mode=DataMode.fixture, roads=(FIXTURE_ROAD,))


# --------------------------------------------------------------------------- coordinates


class TestCoordinateKey:
    """``{"lat": …, "long": …}`` — ``long``, not ``lon``, and not ``lng``."""

    def test_the_payload_really_uses_long(self, roadworks: list[Mapping[str, Any]]) -> None:
        # If the API ever renames the key, this fails before the subtler assertions below do.
        coordinate = roadworks[0]["coordinate"]
        assert set(coordinate) == {"lat", "long"}
        assert "lon" not in coordinate

    def test_longitude_is_read_from_the_long_key(self, first_roadworks: TrafficEventRecord) -> None:
        # Reading "lon" would silently yield None and the coordinate would fall back to the
        # `point` string or the geometry — or, worse, to longitude 0, which is the Gulf of
        # Guinea. A German A5 event belongs at 7.5 E.
        assert first_roadworks.coordinate.longitude == pytest.approx(FIRST_ROADWORKS_LON)
        assert first_roadworks.coordinate.latitude == pytest.approx(FIRST_ROADWORKS_LAT)
        assert first_roadworks.coordinate.longitude != 0.0

    def test_latitude_and_longitude_are_not_swapped(
        self, all_items: list[tuple[str, Mapping[str, Any]]]
    ) -> None:
        # Germany's latitudes (47-55) and longitudes (6-15) are disjoint ranges, so a swap is
        # detectable without knowing any individual event's true position.
        for service, item in all_items:
            record = parse_item(item, road=FIXTURE_ROAD, service=service, source_url=None)
            assert record is not None
            assert 47.2 <= record.coordinate.latitude <= 55.1
            assert 5.8 <= record.coordinate.longitude <= 15.1

    def test_point_string_is_latitude_first(self) -> None:
        # ``point`` is the opposite order from ``geometry``; both are hard-coded from
        # measurement. With only ``point`` available, the latitude must still come out ~48.
        item = {"identifier": "x", "point": "48.5,8.1", "description": []}
        record = parse_item(item, road="A5", service="warning", source_url=None)
        assert record is not None
        assert record.coordinate.latitude == pytest.approx(48.5)
        assert record.coordinate.longitude == pytest.approx(8.1)

    def test_geometry_is_longitude_first(
        self, first_roadworks: TrafficEventRecord, roadworks: list[Mapping[str, Any]]
    ) -> None:
        # RFC 7946 order in the raw payload, latitude-first in the record.
        raw_first = roadworks[0]["geometry"]["coordinates"][0]
        assert raw_first[0] == pytest.approx(7.521931811)
        assert first_roadworks.geometry[0].longitude == pytest.approx(raw_first[0])
        assert first_roadworks.geometry[0].latitude == pytest.approx(raw_first[1])
        assert len(first_roadworks.geometry) == 20

    def test_coordinate_wins_over_point_and_geometry(self) -> None:
        item = {
            "identifier": "x",
            "coordinate": {"lat": 50.0, "long": 9.0},
            "point": "48.5,8.1",
            "geometry": {"type": "LineString", "coordinates": [[7.0, 47.0], [7.1, 47.1]]},
            "description": [],
        }
        record = parse_item(item, road="A5", service="warning", source_url=None)
        assert record is not None
        assert record.coordinate == Coordinate(latitude=50.0, longitude=9.0)

    @pytest.mark.parametrize(
        "item",
        [
            {"identifier": "no-location"},
            {"identifier": "empty-coordinate", "coordinate": {}},
            {"identifier": "null-coordinate", "coordinate": {"lat": None, "long": None}},
            {"identifier": "out-of-range", "coordinate": {"lat": 991.0, "long": 9.0}},
            {"identifier": "half-a-point", "point": "48.5"},
            {"identifier": "garbage-point", "point": "nord,ost"},
            {"identifier": "empty-geometry", "geometry": {"type": "LineString", "coordinates": []}},
        ],
    )
    def test_unlocatable_items_return_none_rather_than_raising(
        self, item: Mapping[str, Any]
    ) -> None:
        # One malformed entry among three hundred is a data-quality finding, not an outage.
        assert parse_item(item, road="A5", service="warning", source_url=None) is None

    def test_geometry_is_used_when_nothing_else_locates_the_event(self) -> None:
        item = {
            "identifier": "geometry-only",
            "geometry": {"type": "LineString", "coordinates": [[8.6, 50.1], [8.7, 50.2]]},
            "description": [],
        }
        record = parse_item(item, road="A5", service="warning", source_url=None)
        assert record is not None
        assert record.coordinate.latitude == pytest.approx(50.1)

    def test_broken_geometry_vertices_are_dropped_not_fatal(self) -> None:
        item = {
            "identifier": "mixed",
            "coordinate": {"lat": 50.0, "long": 9.0},
            "geometry": {
                "type": "LineString",
                "coordinates": [[8.6, 50.1], [8.7], ["x", "y"], [8.8, 50.3], [8.9, 500.0]],
            },
            "description": [],
        }
        record = parse_item(item, road="A5", service="warning", source_url=None)
        assert record is not None
        # Two of the five positions are usable; a short pair, a non-numeric pair and an
        # out-of-range latitude are skipped individually.
        assert len(record.geometry) == 2


# --------------------------------------------------------------------------- blocking


class TestBlockingSignal:
    """``isBlocked`` is dead. ``"CLOSED" in impact.symbols`` is the truth."""

    def test_is_blocked_is_the_string_false_on_every_sampled_item(
        self, all_items: list[tuple[str, Mapping[str, Any]]]
    ) -> None:
        # The field the payload *appears* to offer is a constant. Reading it would report
        # "nothing is closed" for the entire Autobahn network, forever.
        values = {item.get("isBlocked") for _, item in all_items}
        assert values == {"false"}
        assert all(isinstance(value, str) for value in values)

    def test_closed_symbol_wins_over_is_blocked(
        self, first_roadworks: TrafficEventRecord, roadworks: list[Mapping[str, Any]]
    ) -> None:
        # This is the trap the adapter exists to avoid, asserted on a real item: isBlocked says
        # "false", impact.symbols says CLOSED, and a lane really is shut.
        assert roadworks[0]["isBlocked"] == "false"
        assert CLOSED_SYMBOL in roadworks[0]["impact"]["symbols"]
        assert first_roadworks.is_blocked is True

    def test_an_item_without_the_symbol_is_not_blocked(
        self, roadworks: list[Mapping[str, Any]]
    ) -> None:
        unblocked = next(
            item
            for item in roadworks
            if CLOSED_SYMBOL not in (item.get("impact") or {}).get("symbols", [])
        )
        record = parse_item(unblocked, road="A5", service="roadworks", source_url=None)
        assert record is not None
        # Same "false" in the payload, different answer — so the flag is genuinely derived.
        assert unblocked["isBlocked"] == "false"
        assert record.is_blocked is False

    def test_blocking_matches_the_symbols_on_every_item(
        self, all_items: list[tuple[str, Mapping[str, Any]]]
    ) -> None:
        for service, item in all_items:
            record = parse_item(item, road=FIXTURE_ROAD, service=service, source_url=None)
            assert record is not None
            symbols = [
                symbol
                for symbol in (item.get("impact") or {}).get("symbols", []) or []
                if isinstance(symbol, str)
            ]
            assert record.is_blocked is (CLOSED_SYMBOL in symbols)

    def test_missing_impact_block_is_not_blocked(self, warnings_: list[Mapping[str, Any]]) -> None:
        # ``impact`` is absent entirely on warnings; the adapter must not assume the key.
        assert warnings_[0].get("impact") is None
        record = parse_item(warnings_[0], road="A5", service="warning", source_url=None)
        assert record is not None
        assert record.is_blocked is False

    def test_null_entries_in_symbols_are_tolerated(
        self, roadworks: list[Mapping[str, Any]]
    ) -> None:
        # The live feed puts nulls in this list. A `str.upper()` over it would raise.
        with_null = next(
            item for item in roadworks if None in ((item.get("impact") or {}).get("symbols") or [])
        )
        record = parse_item(with_null, road="A5", service="roadworks", source_url=None)
        assert record is not None
        assert record.is_blocked is False

    def test_is_blocked_is_not_copied_into_raw(self, first_roadworks: TrafficEventRecord) -> None:
        raw = first_roadworks.raw
        assert raw is not None
        # Keeping the dead field around invites someone to read it again later.
        assert "isBlocked" not in raw
        # The geometry lives in its own typed column, not duplicated as JSON.
        assert "geometry" not in raw
        assert raw["display_type"] == "ROADWORKS"


# --------------------------------------------------------------------------- timestamps


class TestMissingStartTimestamp:
    """``SHORT_TERM_ROADWORKS`` has no ``startTimestamp``. Parsing must not depend on one."""

    def test_the_correlation_holds_in_the_fixture(self, roadworks: list[Mapping[str, Any]]) -> None:
        short_term = [
            item for item in roadworks if item.get("display_type") == "SHORT_TERM_ROADWORKS"
        ]
        long_term = [item for item in roadworks if item.get("display_type") == "ROADWORKS"]
        assert short_term and long_term
        assert all("startTimestamp" not in item for item in short_term)
        assert all(item.get("startTimestamp") for item in long_term)

    def test_short_term_items_parse_without_raising(
        self, roadworks: list[Mapping[str, Any]]
    ) -> None:
        short_term = [
            item for item in roadworks if item.get("display_type") == "SHORT_TERM_ROADWORKS"
        ]
        records = [
            parse_item(item, road="A5", service="roadworks", source_url=None) for item in short_term
        ]
        assert len(records) == 6
        assert all(record is not None for record in records)

    def test_the_start_comes_from_the_german_prose(
        self, roadworks: list[Mapping[str, Any]]
    ) -> None:
        item = next(
            entry
            for entry in roadworks
            if entry.get("display_type") == "SHORT_TERM_ROADWORKS"
            and any("19:30 bis zum" in line for line in entry["description"])
        )
        record = parse_item(item, road="A5", service="roadworks", source_url=None)
        assert record is not None
        # Description: "15.09.26 19:30 bis zum 16.09.26 05:00 Uhr." plus a second night.
        # 19:30 Berlin time in September is CEST (UTC+2), so 17:30 UTC.
        assert record.starts_at == datetime(2026, 9, 15, 17, 30, tzinfo=UTC)
        # The latest end across both phases, not the first one: the record covers the whole
        # disruption rather than one arbitrary night of it.
        assert record.ends_at == datetime(2026, 9, 17, 3, 0, tzinfo=UTC)

    def test_every_record_has_an_aware_utc_timestamp_or_none(
        self, all_items: list[tuple[str, Mapping[str, Any]]]
    ) -> None:
        for service, item in all_items:
            record = parse_item(item, road=FIXTURE_ROAD, service=service, source_url=None)
            assert record is not None
            for moment in (record.starts_at, record.ends_at):
                if moment is not None:
                    # A naive datetime here would be reinterpreted as local time by the
                    # database driver and shift every event by one or two hours.
                    assert moment.tzinfo is not None
                    assert moment.utcoffset() is not None


class TestGermanCivilTime:
    """The prose clock is ``Europe/Berlin``, established by a UTC sibling in the same item."""

    def test_description_start_matches_the_machine_timestamp(
        self, warnings_: list[Mapping[str, Any]]
    ) -> None:
        item = next(
            entry for entry in warnings_ if entry["identifier"].startswith("NLW_2026_002476")
        )
        # The item states the same instant twice: "Beginn: 29.07.26 um 12:18 Uhr" in prose and
        # 2026-07-29T12:18:00+02:00 as a machine timestamp. Parsing the prose as UTC would put
        # the two two hours apart.
        assert item["startTimestamp"] == "2026-07-29T12:18:00+02:00"
        assert item["description"][0] == "Beginn: 29.07.26 um 12:18 Uhr"
        derived_start, _ = extract_validity("\n".join(item["description"]))
        assert derived_start == datetime(2026, 7, 29, 10, 18, tzinfo=UTC)

        record = parse_item(item, road="A5", service="warning", source_url=None)
        assert record is not None
        assert record.starts_at == derived_start

    def test_summer_and_winter_offsets_differ(self) -> None:
        # CEST in July is UTC+2, CET in January is UTC+1. A fixed offset would be wrong for
        # half the year, and a naive parse wrong all year.
        summer, _ = extract_validity("Beginn: 15.07.26 um 12:00 Uhr")
        winter, _ = extract_validity("Beginn: 15.01.26 um 12:00 Uhr")
        assert summer == datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
        assert winter == datetime(2026, 1, 15, 11, 0, tzinfo=UTC)

    def test_beginn_and_ende_pair(self, roadworks: list[Mapping[str, Any]]) -> None:
        # "Beginn: 26.04.26 um 05:00 Uhr" / "Ende: 30.09.26 um 20:00 Uhr", both CEST.
        start, end = extract_validity("\n".join(roadworks[0]["description"]))
        assert start == datetime(2026, 4, 26, 3, 0, tzinfo=UTC)
        assert end == datetime(2026, 9, 30, 18, 0, tzinfo=UTC)

    def test_gesamtmassnahme_line_is_not_read_as_the_end(self) -> None:
        # "(Ende der Gesamtmaßnahme: …)" states when the whole construction project finishes,
        # while the event describes one phase. Taking it would over-extend every roadworks.
        body = (
            "Zeitraum dieser Bauphase:\n"
            "Beginn: 18.05.26 um 15:30 Uhr\n"
            "Ende: 07.11.26 um 15:30 Uhr\n"
            "(Ende der Gesamtmaßnahme: 31.12.27)"
        )
        _, end = extract_validity(body)
        assert end == datetime(2026, 11, 7, 14, 30, tzinfo=UTC)

    def test_recurring_night_window_takes_the_outer_range(self) -> None:
        body = (
            "Jeden Montag, Dienstag und Mittwoch zwischen dem 14.09.26 und dem 19.09.26 "
            "von 20:00 bis 00:00 Uhr."
        )
        start, end = extract_validity(body)
        # Local midnight on each end; the approximation errs towards showing the event as
        # active for longer than it blocks traffic, which is the safe direction.
        assert start == datetime(2026, 9, 13, 22, 0, tzinfo=UTC)
        assert end == datetime(2026, 9, 18, 22, 0, tzinfo=UTC)

    def test_several_phases_collapse_to_the_widest_span(self) -> None:
        body = (
            "14.09.26 22:00 bis zum 15.09.26 05:00 Uhr.\n"
            "16.09.26 22:00 bis zum 17.09.26 05:00 Uhr.\n"
            "21.09.26 22:00 bis zum 22.09.26 05:00 Uhr."
        )
        start, end = extract_validity(body)
        assert start == datetime(2026, 9, 14, 20, 0, tzinfo=UTC)
        assert end == datetime(2026, 9, 22, 3, 0, tzinfo=UTC)

    @pytest.mark.parametrize(
        "body",
        [
            "",
            "Fahrbahnschäden",
            # An impossible date written by a human; dropped rather than clamped to 28.02.
            "Beginn: 31.02.26 um 05:00 Uhr",
            "Beginn: irgendwann",
        ],
    )
    def test_a_body_without_readable_dates_yields_nulls(self, body: str) -> None:
        # Nulls, not the ingestion time: a made-up start would be indistinguishable from a
        # real one downstream.
        assert extract_validity(body) == (None, None)

    def test_two_digit_years_use_a_fixed_pivot(self) -> None:
        # Fixed rather than derived from today, so this test does not change meaning in 2081.
        start, _ = extract_validity("Beginn: 01.03.26 um 00:00 Uhr")
        assert start is not None
        assert start.year == 2026


# --------------------------------------------------------------------------- text hygiene


class TestTextCleanup:
    def test_subtitle_loses_its_leading_space(
        self, first_roadworks: TrafficEventRecord, roadworks: list[Mapping[str, Any]]
    ) -> None:
        # The API writes " Basel -> Karlsruhe". Stored unstripped it breaks grouping by
        # direction and shows up as a stray indent in the UI.
        assert roadworks[0]["subtitle"].startswith(" ")
        assert first_roadworks.direction == "Basel -> Karlsruhe"

    def test_every_direction_is_stripped(
        self, all_items: list[tuple[str, Mapping[str, Any]]]
    ) -> None:
        for service, item in all_items:
            record = parse_item(item, road=FIXTURE_ROAD, service=service, source_url=None)
            assert record is not None
            assert record.direction is None or record.direction == record.direction.strip()

    def test_road_ids_are_stripped_at_construction(self) -> None:
        # The API's own road index lists "A60 " with a trailing space; passing it straight back
        # would request /A60%20/services/roadworks and 404.
        adapter = AutobahnTrafficProvider(roads=("A60 ", " A5 ", "A5"))
        assert adapter.roads == ("A60", "A5")

    def test_road_name_on_the_record_is_stripped(self, roadworks: list[Mapping[str, Any]]) -> None:
        record = parse_item(roadworks[0], road="A60 ", service="roadworks", source_url=None)
        assert record is not None
        assert record.road_name == "A60"

    async def test_fetch_roads_strips_and_deduplicates(
        self, provider: AutobahnTrafficProvider, fixtures_dir: Path
    ) -> None:
        raw = json.loads((fixtures_dir / ROADS_FIXTURE).read_text(encoding="utf-8"))["roads"]
        # The fixture preserves the trailing whitespace on purpose.
        assert "A60 " in raw
        result = await provider.fetch_roads()
        assert "A60" in result.data
        assert not any(road != road.strip() for road in result.data)
        # "A60 " and "A60" collapse onto one id, so the cleaned list is one shorter.
        assert len(result.data) == len({road.strip() for road in raw})
        assert result.data == sorted(result.data)

    def test_description_lines_are_joined(self, first_roadworks: TrafficEventRecord) -> None:
        assert first_roadworks.description is not None
        assert "Beginn: 26.04.26 um 05:00 Uhr" in first_roadworks.description
        assert "\n" in first_roadworks.description

    def test_title_falls_back_to_road_and_service(self) -> None:
        item = {"identifier": "x", "coordinate": {"lat": 50.0, "long": 9.0}}
        record = parse_item(item, road="A5", service="closure", source_url=None)
        assert record is not None
        # A record with an empty title is unusable in a list view; the fallback is explicit.
        assert record.title == "A5 closure"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("6", 6.0), (6, 6.0), (6.5, 6.5), ("-3", None), ("", None), (None, None), ("x", None)],
    )
    def test_delay_time_value_arrives_as_text(self, raw: object, expected: float | None) -> None:
        item = {
            "identifier": "x",
            "coordinate": {"lat": 50.0, "long": 9.0},
            "delayTimeValue": raw,
        }
        record = parse_item(item, road="A5", service="warning", source_url=None)
        assert record is not None
        if expected is None:
            assert record.delay_minutes is None
        else:
            assert record.delay_minutes == pytest.approx(expected)


# --------------------------------------------------------------------------- classification


class TestSeverityDerivation:
    """The API states no severity, so it is derived. The table is the contract."""

    @pytest.mark.parametrize(
        ("service", "display_type", "symbols", "abnormal", "expected"),
        [
            # A closure is at least `high`; with CLOSED it is `severe`.
            ("closure", "FULL_CLOSURE", [CLOSED_SYMBOL], None, TrafficSeverity.severe),
            ("closure", "FULL_CLOSURE", ["SEPARATE"], None, TrafficSeverity.high),
            # A Tagesbaustelle on the hard shoulder is minutes of delay …
            ("roadworks", "SHORT_TERM_ROADWORKS", [], None, TrafficSeverity.low),
            # … but one that shuts a lane is not.
            ("roadworks", "SHORT_TERM_ROADWORKS", [CLOSED_SYMBOL], None, TrafficSeverity.high),
            # A multi-month narrowing is the `moderate` baseline.
            ("roadworks", "ROADWORKS", [], None, TrafficSeverity.moderate),
            ("roadworks", "ROADWORKS", [CLOSED_SYMBOL], None, TrafficSeverity.high),
            # Lane symbols alone never raise the level: the sample shows them on items with no
            # measurable impact.
            (
                "roadworks",
                "ROADWORKS",
                ["BREAKDOWN_LANE", "ARROW_UP"],
                None,
                TrafficSeverity.moderate,
            ),
            # INRIX congestion reports outrank everything, because they are measured.
            ("warning", "WARNING", [], "STATIONARY_TRAFFIC", TrafficSeverity.severe),
            ("warning", "WARNING", [], "QUEUING_TRAFFIC", TrafficSeverity.high),
            ("warning", "WARNING", [], "SLOW_TRAFFIC", TrafficSeverity.moderate),
            ("warning", "WARNING", [], None, TrafficSeverity.low),
            ("warning", "WARNING", [CLOSED_SYMBOL], None, TrafficSeverity.high),
            # Even on a roadworks item, a congestion report wins.
            ("roadworks", "ROADWORKS", [], "STATIONARY_TRAFFIC", TrafficSeverity.severe),
            # Case and padding of the abnormal type must not matter.
            ("warning", "WARNING", [], " stationary_traffic ", TrafficSeverity.severe),
            # An abnormal type nobody has seen falls back to the symbol rule.
            ("warning", "WARNING", [], "TELEPORTING_TRAFFIC", TrafficSeverity.low),
        ],
    )
    def test_table(
        self,
        service: str,
        display_type: str,
        symbols: list[str],
        abnormal: str | None,
        expected: TrafficSeverity,
    ) -> None:
        assert (
            derive_severity(
                service=service,
                display_type=display_type,
                symbols=symbols,
                abnormal_traffic_type=abnormal,
            )
            is expected
        )

    def test_severity_is_ordered_for_the_ml_feature(self) -> None:
        # `traffic_severity_ordinal` is used directly as a model feature, so the ranking must
        # be monotone rather than an arbitrary enum order.
        ordinals = [
            TrafficSeverity.low.ordinal,
            TrafficSeverity.moderate.ordinal,
            TrafficSeverity.high.ordinal,
            TrafficSeverity.severe.ordinal,
        ]
        assert ordinals == sorted(ordinals)
        assert len(set(ordinals)) == 4

    @pytest.mark.parametrize(
        ("service", "abnormal", "expected"),
        [
            ("roadworks", None, TrafficEventType.roadworks),
            ("roadworks", "SLOW_TRAFFIC", TrafficEventType.roadworks),
            ("closure", None, TrafficEventType.closure),
            # A warning carrying an abnormal traffic type is a Stau report …
            ("warning", "QUEUING_TRAFFIC", TrafficEventType.congestion),
            # … and one without is a generic Gefahrenmeldung.
            ("warning", None, TrafficEventType.warning),
        ],
    )
    def test_event_type_mapping(
        self, service: str, abnormal: str | None, expected: TrafficEventType
    ) -> None:
        item: dict[str, Any] = {
            "identifier": "x",
            "coordinate": {"lat": 50.0, "long": 9.0},
        }
        if abnormal is not None:
            item["abnormalTrafficType"] = abnormal
        record = parse_item(item, road="A5", service=service, source_url=None)
        assert record is not None
        assert record.event_type is expected

    def test_the_fixture_exercises_every_severity(
        self, all_items: list[tuple[str, Mapping[str, Any]]]
    ) -> None:
        # A table this branchy is only meaningfully tested if the real sample reaches all four
        # outcomes; otherwise the parametrised cases above are hypotheticals.
        severities = set()
        for service, item in all_items:
            record = parse_item(item, road=FIXTURE_ROAD, service=service, source_url=None)
            assert record is not None
            severities.add(record.severity)
        assert severities == set(TrafficSeverity)


# --------------------------------------------------------------------------- the adapter


class TestFetchEvents:
    """End to end in fixture mode: no HTTP client is constructed on this path."""

    async def test_all_three_services_are_merged(
        self,
        provider: AutobahnTrafficProvider,
        roadworks: list[Mapping[str, Any]],
        closures: list[Mapping[str, Any]],
        warnings_: list[Mapping[str, Any]],
    ) -> None:
        result = await provider.fetch_events()
        # 12 roadworks + 6 closures + 3 warnings, none of which share an identifier.
        assert len(result.data) == len(roadworks) + len(closures) + len(warnings_) == 21
        assert result.mode is ProviderMode.fixture
        assert result.is_degraded is True

    async def test_duplicate_identifiers_are_collapsed(
        self, provider: AutobahnTrafficProvider
    ) -> None:
        result = await provider.fetch_events()
        identifiers = [record.external_id for record in result.data if record.external_id]
        assert len(identifiers) == len(set(identifiers))

    async def test_results_are_sorted_most_severe_first(
        self, provider: AutobahnTrafficProvider
    ) -> None:
        result = await provider.fetch_events()
        ordinals = [record.severity.ordinal for record in result.data]
        # Descending severity, so a paged UI shows the worst disruption on page one.
        assert ordinals == sorted(ordinals, reverse=True)

    async def test_ordering_is_stable_across_calls(self, provider: AutobahnTrafficProvider) -> None:
        first = await provider.fetch_events()
        second = await provider.fetch_events()
        assert [record.external_id for record in first.data] == [
            record.external_id for record in second.data
        ]

    async def test_provenance_marks_the_source(self, provider: AutobahnTrafficProvider) -> None:
        result = await provider.fetch_events()
        for record in result.data:
            assert record.provenance.source is SourceSystem.autobahn
            assert record.provenance.data_origin is DataOrigin.official
            assert record.provenance.source_timestamp == record.starts_at

    async def test_bbox_filters_on_point_or_geometry(
        self, provider: AutobahnTrafficProvider
    ) -> None:
        # Frankfurt's south-western approach: the A5 closures at 50.13 N / 8.59 E fall inside,
        # the Freiburg and Karlsruhe ones do not.
        frankfurt = BoundingBox(west=8.4, south=49.9, east=8.9, north=50.3)
        filtered = await provider.fetch_events(bbox=frankfurt)
        unfiltered = await provider.fetch_events()
        assert 0 < len(filtered.data) < len(unfiltered.data)
        for record in filtered.data:
            # An event matches on its representative point *or* any vertex of its line, so a
            # closure whose midpoint is outside but which runs through the box still counts.
            assert frankfurt.contains(record.coordinate) or any(
                frankfurt.contains(point) for point in record.geometry
            )

    async def test_a_bbox_over_open_sea_matches_nothing(
        self, provider: AutobahnTrafficProvider
    ) -> None:
        atlantic = BoundingBox(west=-30.0, south=30.0, east=-20.0, north=40.0)
        result = await provider.fetch_events(bbox=atlantic)
        # Empty is a legitimate answer, not a failure, and the mode must still be reported.
        assert result.data == []
        assert result.mode is ProviderMode.fixture

    async def test_asking_for_another_road_reports_the_substitution(self) -> None:
        adapter = AutobahnTrafficProvider(data_mode=DataMode.fixture, roads=("A1",))
        result = await adapter.fetch_events()
        # Labelling the bundled A5 sample as A1 data would put Freiburg roadworks on the
        # Hamburg-Ruhr corridor in the database. The fixture's own road wins, loudly.
        assert {record.road_name for record in result.data} == {"A5"}
        assert any("A5 sample" in warning for warning in result.warnings)

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"services": ("roadworks", "webcams")}, "unknown Autobahn service"),
            ({"roads": ()}, "at least one road"),
            ({"roads": ("  ", "")}, "at least one road"),
        ],
    )
    def test_construction_rejects_nonsense(self, kwargs: dict[str, Any], match: str) -> None:
        with pytest.raises(ValueError, match=match):
            AutobahnTrafficProvider(**kwargs)

    def test_defaults_cover_the_demo_corridors(self) -> None:
        # A3, A5, A6 and A8 carry the routes the simulator drives.
        assert {"A3", "A5", "A6", "A8"}.issubset(DEFAULT_ROADS)
        assert SERVICES == ("roadworks", "closure", "warning")
