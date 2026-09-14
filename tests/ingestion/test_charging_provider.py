"""The Bundesnetzagentur Ladesäulenregister parser.

The committed fixture is real: UTF-8 **with BOM**, ``;``-delimited with CRLF line endings, ten
lines of preamble before the header, 47 columns, German decimal commas, ``DD.MM.YYYY`` dates,
and quoted fields that contain the delimiter. Every one of those is a way to read the file
wrongly while still producing rows, which is why the assertions below are against values
derived independently — a second pass with a raw :mod:`csv` reader, the literal bytes of the
first data line, a hash recomputed in the test — rather than against whatever the adapter
happens to return today.

The first data row of the fixture, quoted verbatim so the expectations can be checked by eye::

    1010338;Albwerk Elektro- und Kommunikationstechnik GmbH;…;In Betrieb;
    Normalladeeinrichtung;2;22;11.01.2020;Am Berg;1;;72535;Heroldstatt;
    Landkreis Alb-Donau-Kreis;Baden-Württemberg;48,442398;9,659075;…
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final

import pytest

from autotwin_contracts import (
    Bundesland,
    ChargingCategory,
    ChargingStationRecord,
    ConnectorType,
    CurrentType,
    DataOrigin,
    SourceSystem,
)
from autotwin_core.errors import InvalidSourceData
from autotwin_ingestion.providers.charging import (
    CONNECTOR_GROUPS,
    EXPECTED_COLUMN_COUNT,
    SITE_COLUMNS,
    iter_records,
    read_edition_date,
)

FIXTURE_NAME: Final[str] = "bnetza_ladesaeulenregister_sample.csv"
UTF8_BOM: Final[bytes] = b"\xef\xbb\xbf"
PREAMBLE_ROWS: Final[int] = 10
"""Notice lines before the header; the header is line 11 (BUILD_SPEC §4)."""

FETCHED_AT: Final[datetime] = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)

# Germany's extent, rounded outwards from the official Bundesamt für Kartographie figures
# (List/Sylt 55.06 N, Oberstdorf 47.27 N, Selfkant 5.87 E, Görlitz 15.04 E).
GERMANY_SOUTH: Final[float] = 47.2
GERMANY_NORTH: Final[float] = 55.1
GERMANY_WEST: Final[float] = 5.8
GERMANY_EAST: Final[float] = 15.1

_MINIMAL_ROW: Final[dict[str, str]] = {
    "Ladeeinrichtungs-ID": "9999999",
    "Betreiber": "Testbetreiber GmbH",
    "Status": "In Betrieb",
    "Art der Ladeeinrichtung": "Normalladeeinrichtung",
    "Anzahl Ladepunkte": "1",
    "Nennleistung Ladeeinrichtung [kW]": "22",
    "Straße": "Teststraße",
    "Hausnummer": "1",
    "Postleitzahl": "72535",
    "Ort": "Teststadt",
    "Bundesland": "Baden-Württemberg",
    "Breitengrad": "48,442398",
    "Längengrad": "9,659075",
    "Steckertypen1": "AC Typ 2 Steckdose",
    "Nennleistung Stecker1": "22",
}
"""The smallest row the parser accepts, used as the base for every synthetic case below."""


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def register_csv(fixtures_dir: Path) -> Path:
    """The committed Ladesäulenregister excerpt, byte-for-byte as the register serves it."""
    return fixtures_dir / FIXTURE_NAME


@pytest.fixture
def header_columns(register_csv: Path) -> list[str]:
    """The real 47-column header, read independently of the adapter."""
    with register_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle, delimiter=";"))
    return [column.strip() for column in rows[PREAMBLE_ROWS]]


@pytest.fixture
def raw_rows(register_csv: Path) -> list[list[str]]:
    """Every data row of the fixture, parsed by a plain :mod:`csv` reader.

    This is the independent yardstick for the record count and the connector expansion: it
    shares nothing with the adapter except the delimiter and the encoding.
    """
    with register_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle, delimiter=";"))
    return rows[PREAMBLE_ROWS + 1 :]


@pytest.fixture
def records(register_csv: Path) -> list[ChargingStationRecord]:
    """The whole fixture, parsed."""
    return list(iter_records(register_csv, source_url=None, fetched_at=FETCHED_AT))


@pytest.fixture
def skips(register_csv: Path) -> list[str]:
    """One message per row the parser refused; the fixture must produce none."""
    collected: list[str] = []
    list(
        iter_records(register_csv, source_url=None, fetched_at=FETCHED_AT, on_skip=collected.append)
    )
    return collected


@pytest.fixture
def write_register(tmp_path: Path, header_columns: list[str]) -> Callable[..., Path]:
    """Build a synthetic register file that keeps the *real* header and preamble shape.

    Synthetic rows are written with :class:`csv.writer`, so a cell containing ``;`` is quoted
    exactly the way the Bundesnetzagentur quotes it. That is the only honest way to prove the
    parser survives an embedded delimiter.
    """

    def build(
        *rows: str,
        edition: str = "01.09.2026",
        bom: bool = True,
        preamble_rows: int = PREAMBLE_ROWS,
        columns: list[str] | None = None,
    ) -> Path:
        used = header_columns if columns is None else columns
        filler = ";" * (len(used) - 1)
        preamble = [f"Ladesäulenregister Bundesnetzagentur{filler}"] * preamble_rows
        if preamble_rows > 7:
            preamble[7] = f"Letzte Aktualisierung vom: {edition}{filler}"
        text = "\r\n".join([*preamble, ";".join(used), *rows]) + "\r\n"
        path = tmp_path / "synthetic_register.csv"
        path.write_bytes((UTF8_BOM if bom else b"") + text.encode("utf-8"))
        return path

    return build


def make_row(columns: list[str], **overrides: str) -> str:
    """Render one CSV data line from the 47 real column names."""
    values = dict.fromkeys(columns, "")
    values.update(_MINIMAL_ROW)
    values.update(overrides)
    buffer = io.StringIO()
    csv.writer(buffer, delimiter=";", lineterminator="").writerow(
        [values[column] for column in columns]
    )
    return buffer.getvalue()


def parse_one(path: Path) -> ChargingStationRecord | None:
    """Parse a one-row synthetic file, returning the record or ``None`` if it was skipped."""
    parsed = list(iter_records(path, source_url=None, fetched_at=FETCHED_AT))
    return parsed[0] if parsed else None


# --------------------------------------------------------------------------- the file itself


class TestFixtureIsTheRealFile:
    """The fixture must keep every trap of the source, or the parser tests prove nothing."""

    def test_file_begins_with_a_utf8_bom(self, register_csv: Path) -> None:
        # If this ever fails, the fixture was re-saved by an editor that stripped the BOM and
        # `TestByteOrderMark` below silently stops testing anything.
        assert register_csv.read_bytes()[:3] == UTF8_BOM

    def test_header_is_the_eleventh_line(self, header_columns: list[str]) -> None:
        assert header_columns[0] == "Ladeeinrichtungs-ID"

    def test_header_has_forty_seven_columns(self, header_columns: list[str]) -> None:
        # 23 site columns + 6 connector groups x 4 columns = 47.
        assert len(SITE_COLUMNS) + CONNECTOR_GROUPS * 4 == EXPECTED_COLUMN_COUNT
        assert len(header_columns) == EXPECTED_COLUMN_COUNT

    def test_site_columns_come_first_and_in_order(self, header_columns: list[str]) -> None:
        assert tuple(header_columns[: len(SITE_COLUMNS)]) == SITE_COLUMNS

    def test_lines_end_with_crlf(self, register_csv: Path) -> None:
        # The register is a Windows export. A parser opened without ``newline=""`` would work
        # here but corrupt a quoted field containing a newline in the full file.
        assert b"\r\n" in register_csv.read_bytes()

    def test_edition_date_is_read_from_the_preamble(self, register_csv: Path) -> None:
        # Line 8 of the fixture reads "Letzte Aktualisierung vom: 01.09.2026".
        assert read_edition_date(register_csv) == date(2026, 9, 1)

    def test_edition_date_is_none_when_the_preamble_does_not_state_one(
        self, write_register: Callable[..., Path], header_columns: list[str]
    ) -> None:
        path = write_register(make_row(header_columns), edition="", preamble_rows=2)
        assert read_edition_date(path) is None


class TestByteOrderMark:
    """``utf-8-sig`` versus ``utf-8`` — the one-character difference that hides in plain sight."""

    def test_plain_utf8_leaves_the_marker_on_the_first_cell(self, register_csv: Path) -> None:
        # The trap, demonstrated rather than described: decoded as plain UTF-8 the very first
        # cell of the file carries an invisible U+FEFF. Here the ten-line preamble happens to
        # absorb it, which is *why* this needs a test — the damage is one layout change away
        # from the header, and it would be invisible in a diff.
        naive = register_csv.read_text(encoding="utf-8").splitlines()[0]
        assert naive.startswith("﻿")
        assert naive.split(";")[0] != "Ladesäulenregister Bundesnetzagentur"

        correct = register_csv.read_text(encoding="utf-8-sig").splitlines()[0]
        assert not correct.startswith("﻿")
        assert correct.split(";")[0] == "Ladesäulenregister Bundesnetzagentur"

    def test_no_column_name_carries_the_marker(self, header_columns: list[str]) -> None:
        # `header_columns` is read with utf-8-sig, as the adapter does. A U+FEFF anywhere in a
        # column name makes every lookup of that column miss, in silence.
        assert not any("﻿" in column for column in header_columns)

    def test_first_record_keeps_the_register_id(self, records: list[ChargingStationRecord]) -> None:
        # Whenever the "Ladeeinrichtungs-ID" lookup misses — a leaked BOM, a renamed column, a
        # stray space — the column reads empty and `_synthetic_id` fabricates a 32-character
        # hash instead. A non-None, non-synthetic external_id is the observable proof that the
        # key column was actually found.
        assert records[0].external_id == "bnetza:1010338"
        assert records[0].provenance.source_identifier == "1010338"

    def test_every_record_carries_a_register_id(
        self, records: list[ChargingStationRecord], raw_rows: list[list[str]]
    ) -> None:
        # No row in the fixture has a blank Ladeeinrichtungs-ID, so no record may carry a
        # synthesised one; a fabricated id here would mean the id column was not found.
        assert all(row[0].strip() for row in raw_rows)
        ids = {record.external_id for record in records}
        assert len(ids) == len(records)
        assert all(identifier.startswith("bnetza:") for identifier in ids)
        assert all(record.provenance.source_identifier is not None for record in records)


class TestKnownFirstRecord:
    """The first data row, asserted field by field against the bytes quoted in the docstring."""

    @pytest.fixture
    def first(self, records: list[ChargingStationRecord]) -> ChargingStationRecord:
        return records[0]

    def test_external_id(self, first: ChargingStationRecord) -> None:
        assert first.external_id == "bnetza:1010338"

    def test_operator(self, first: ChargingStationRecord) -> None:
        assert first.operator is not None
        assert first.operator.startswith("Albwerk")

    def test_address(self, first: ChargingStationRecord) -> None:
        assert (first.street, first.house_number) == ("Am Berg", "1")
        assert (first.postal_code, first.city) == ("72535", "Heroldstatt")

    def test_bundesland_is_the_iso_code(self, first: ChargingStationRecord) -> None:
        # "Baden-Württemberg" in the file, "BW" in the database (BUILD_SPEC §2).
        assert first.bundesland is Bundesland.BW

    def test_coordinate_survives_the_decimal_comma(self, first: ChargingStationRecord) -> None:
        # "48,442398" must become 48.442398 — not 48.0 and not 48442398.
        assert first.coordinate.latitude == pytest.approx(48.442398, abs=1e-9)
        assert first.coordinate.longitude == pytest.approx(9.659075, abs=1e-9)

    def test_commissioning_date_is_german_order(self, first: ChargingStationRecord) -> None:
        # "11.01.2020" is 11 January, not 1 November. Reading it as %m.%d.%Y is the classic
        # failure and would still parse without error here.
        assert first.commissioned_on == date(2020, 1, 11)

    def test_power_and_category(self, first: ChargingStationRecord) -> None:
        assert first.max_power_kw == pytest.approx(22.0)
        assert first.total_power_kw == pytest.approx(22.0)
        # BUILD_SPEC §2 puts the normal/fast boundary at 22 kW inclusive, so this site is
        # `fast` even though the register calls it a "Normalladeeinrichtung". The two
        # vocabularies genuinely differ; the spec's wins.
        assert first.charging_category is ChargingCategory.fast

    def test_two_type2_ac_points(self, first: ChargingStationRecord) -> None:
        assert first.charging_points_count == 2
        assert [point.connector_type for point in first.points] == [ConnectorType.type2] * 2
        assert [point.current_type for point in first.points] == [CurrentType.ac] * 2
        assert [point.ordinal for point in first.points] == [1, 2]
        assert all(point.power_kw == pytest.approx(22.0) for point in first.points)

    def test_provenance_is_official_and_dated_from_the_preamble(
        self, first: ChargingStationRecord
    ) -> None:
        assert first.provenance.source is SourceSystem.bundesnetzagentur
        assert first.provenance.data_origin is DataOrigin.official
        # The edition date, not the download time: a station must not claim to be as fresh as
        # the moment AutoTwin happened to fetch the file.
        assert first.provenance.source_timestamp == datetime(2026, 9, 1, tzinfo=UTC)
        assert first.provenance.ingested_at == FETCHED_AT

    def test_raw_keeps_the_operator_declared_point_count(
        self, first: ChargingStationRecord
    ) -> None:
        # The register's own "Anzahl Ladepunkte" is preserved verbatim even where the parsed
        # connector count wins, so a disagreement stays diagnosable.
        assert first.raw is not None
        assert first.raw["Anzahl Ladepunkte"] == "2"
        assert first.raw["Bezahlsysteme"] == "RFID-Karte;Onlinezahlungsverfahren"

    def test_raw_drops_the_public_key_blobs(self, first: ChargingStationRecord) -> None:
        # 96-character hex keys are ~40 % of the file and answer no question the API asks.
        assert first.raw is not None
        assert not any(key.startswith("Public Key") for key in first.raw)


class TestWholeFixture:
    """Invariants over all 200 rows, each checked against a second, independent computation."""

    def test_record_count_equals_an_independent_row_count(
        self, records: list[ChargingStationRecord], raw_rows: list[list[str]]
    ) -> None:
        # The fixture's docstring promises 200 data rows; the raw reader confirms it, and the
        # adapter must not lose or invent one.
        assert len(raw_rows) == 200
        assert len(records) == len(raw_rows)

    def test_no_row_is_skipped(self, skips: list[str]) -> None:
        assert skips == []

    def test_connector_count_matches_an_independent_expansion(
        self, records: list[ChargingStationRecord], raw_rows: list[list[str]]
    ) -> None:
        # Recount the connectors straight from the six Steckertypen columns: every ";"-separated
        # non-empty token is one physical connector. A parser that forgot that a group is itself
        # multi-valued would come out low here and nowhere else.
        expected = sum(
            len([token for token in row[23 + (group - 1) * 4].split(";") if token.strip()])
            for row in raw_rows
            for group in range(1, CONNECTOR_GROUPS + 1)
        )
        assert expected == 436
        assert sum(len(record.points) for record in records) == expected

    def test_point_count_never_contradicts_the_point_list(
        self, records: list[ChargingStationRecord]
    ) -> None:
        # A UI that shows "4 connectors" above a list of 2 is worse than either number alone.
        assert all(record.charging_points_count == len(record.points) for record in records)

    def test_declared_and_parsed_counts_legitimately_differ(
        self, records: list[ChargingStationRecord], raw_rows: list[list[str]]
    ) -> None:
        # 403 declared Ladepunkte versus 436 parsed connectors: a Ladepunkt carrying a Type 2
        # socket and a Schuko outlet is one Ladepunkt and two connectors. The divergence is
        # documented behaviour, so it is asserted rather than tolerated.
        declared = sum(int(row[5]) for row in raw_rows)
        assert declared == 403
        assert sum(len(record.points) for record in records) > declared

    def test_every_station_is_inside_germany(self, records: list[ChargingStationRecord]) -> None:
        # Not a data-quality rule here but a parsing check: a mangled decimal comma, a
        # thousands separator eaten, or swapped lat/lon all land outside this box.
        for record in records:
            assert GERMANY_SOUTH <= record.coordinate.latitude <= GERMANY_NORTH
            assert GERMANY_WEST <= record.coordinate.longitude <= GERMANY_EAST

    def test_every_bundesland_resolves(self, records: list[ChargingStationRecord]) -> None:
        # All sixteen states appear in the fixture, spelled with umlauts and hyphens.
        assert {record.bundesland for record in records} == set(Bundesland)

    def test_dc_connectors_are_ccs_or_chademo(self, records: list[ChargingStationRecord]) -> None:
        # The register prefixes the connector text with AC/DC; where it does not, the standard
        # itself decides. Either way a Type 2 socket must never come out as DC.
        for record in records:
            for point in record.points:
                if point.connector_type in {ConnectorType.ccs, ConnectorType.chademo}:
                    assert point.current_type is CurrentType.dc
                if point.connector_type in {ConnectorType.type2, ConnectorType.schuko}:
                    assert point.current_type is CurrentType.ac

    def test_category_follows_the_strongest_connector(
        self, records: list[ChargingStationRecord]
    ) -> None:
        # BUILD_SPEC §2: <22 kW normal, 22-149 fast, >=150 ultra_fast.
        for record in records:
            assert record.charging_category is ChargingCategory.from_power(record.max_power_kw)

    @pytest.mark.parametrize("limit", [1, 5, 199, 200, 500])
    def test_limit_caps_the_stream(self, register_csv: Path, limit: int) -> None:
        parsed = list(
            iter_records(register_csv, source_url=None, fetched_at=FETCHED_AT, limit=limit)
        )
        assert len(parsed) == min(limit, 200)

    def test_limit_zero_yields_nothing(self, register_csv: Path) -> None:
        parsed = list(iter_records(register_csv, source_url=None, fetched_at=FETCHED_AT, limit=0))
        assert parsed == []


# --------------------------------------------------------------------------- quoting


class TestEmbeddedDelimiter:
    """``line.split(";")`` yields a different column count per row. A CSV parser is mandatory."""

    def test_quoted_payment_systems_stay_one_field(
        self, write_register: Callable[..., Path], header_columns: list[str]
    ) -> None:
        row = make_row(
            header_columns,
            **{"Bezahlsysteme": "RFID-Karte;Onlinezahlungsverfahren;Debitkarte"},
        )
        # The writer quotes it exactly as the register does; two semicolons inside one cell.
        assert '"RFID-Karte;Onlinezahlungsverfahren;Debitkarte"' in row
        record = parse_one(write_register(row))
        assert record is not None
        assert record.raw is not None
        assert record.raw["Bezahlsysteme"] == "RFID-Karte;Onlinezahlungsverfahren;Debitkarte"
        # Naive splitting would push every later column two places left, so the coordinates
        # would land in the wrong cells and the row would be skipped or mislocated.
        assert record.coordinate.latitude == pytest.approx(48.442398, abs=1e-9)
        assert record.city == "Teststadt"

    def test_quoted_field_does_not_change_the_column_count(
        self, write_register: Callable[..., Path], header_columns: list[str]
    ) -> None:
        path = write_register(
            make_row(header_columns, **{"Bezahlsysteme": "RFID-Karte;Onlinezahlung"})
        )
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle, delimiter=";"))
        assert len(rows[PREAMBLE_ROWS + 1]) == EXPECTED_COLUMN_COUNT
        # And the line as raw text has *more* semicolons than columns minus one, which is what
        # makes the naive split wrong.
        raw_line = path.read_text(encoding="utf-8-sig").splitlines()[PREAMBLE_ROWS + 1]
        assert raw_line.count(";") > EXPECTED_COLUMN_COUNT - 1

    def test_opening_hours_with_seven_weekday_entries(
        self, write_register: Callable[..., Path], header_columns: list[str]
    ) -> None:
        # The real first row carries seven ";"-separated weekdays and seven time ranges in two
        # quoted cells — 12 extra delimiters on one line.
        weekdays = "Montag; Dienstag; Mittwoch; Donnerstag; Freitag; Samstag; Sonntag"
        times = "; ".join(["00:00-23:59"] * 7)
        record = parse_one(
            write_register(
                make_row(
                    header_columns,
                    **{
                        "Öffnungszeiten: Wochentage": weekdays,
                        "Öffnungszeiten: Tageszeiten": times,
                    },
                )
            )
        )
        assert record is not None
        assert record.external_id == "bnetza:9999999"


class TestMultiValuedConnectorGroups:
    """One ``Steckertypen{n}`` group can describe several physical connectors."""

    def test_two_types_two_powers(
        self, write_register: Callable[..., Path], header_columns: list[str]
    ) -> None:
        record = parse_one(
            write_register(
                make_row(
                    header_columns,
                    **{
                        "Steckertypen1": "AC Typ 2 Steckdose; AC Schuko",
                        "Nennleistung Stecker1": "11; 3,7",
                    },
                )
            )
        )
        assert record is not None
        # "11; 3,7" is two powers, positionally paired with the two connector names — and the
        # second one is a German decimal, so 3.7 kW, never 37.
        assert [point.power_kw for point in record.points] == [
            pytest.approx(11.0),
            pytest.approx(3.7),
        ]
        assert [point.connector_type for point in record.points] == [
            ConnectorType.type2,
            ConnectorType.schuko,
        ]
        # The site's strongest connector is 11 kW, so the site-level 22 kW column loses.
        assert record.max_power_kw == pytest.approx(11.0)
        assert record.charging_category is ChargingCategory.normal

    def test_single_power_broadcasts_to_every_connector(
        self, write_register: Callable[..., Path], header_columns: list[str]
    ) -> None:
        # A twin socket on one rating states the power once. Both connectors get it; neither
        # gets None, which would understate the site.
        record = parse_one(
            write_register(
                make_row(
                    header_columns,
                    **{
                        "Steckertypen1": "AC Typ 2 Steckdose; AC Typ 2 Steckdose",
                        "Nennleistung Stecker1": "22",
                    },
                )
            )
        )
        assert record is not None
        assert [point.power_kw for point in record.points] == [
            pytest.approx(22.0),
            pytest.approx(22.0),
        ]

    def test_extra_connectors_get_no_guessed_power(
        self, write_register: Callable[..., Path], header_columns: list[str]
    ) -> None:
        # Three names, two powers: the third connector is left unrated rather than given the
        # last stated value, because that would be an invention (BUILD_SPEC §0.2).
        record = parse_one(
            write_register(
                make_row(
                    header_columns,
                    **{
                        "Steckertypen1": "AC Typ 2 Steckdose; AC Schuko; AC CEE",
                        "Nennleistung Stecker1": "22; 3,7",
                    },
                )
            )
        )
        assert record is not None
        assert [point.power_kw for point in record.points] == [
            pytest.approx(22.0),
            pytest.approx(3.7),
            None,
        ]

    def test_groups_are_numbered_across_the_whole_site(
        self, write_register: Callable[..., Path], header_columns: list[str]
    ) -> None:
        record = parse_one(
            write_register(
                make_row(
                    header_columns,
                    **{
                        "Steckertypen1": "DC Fahrzeugkupplung Typ Combo 2 (CCS); DC CHAdeMO",
                        "Nennleistung Stecker1": "150; 50",
                        "Steckertypen2": "AC Typ 2 Steckdose",
                        "Nennleistung Stecker2": "43",
                    },
                )
            )
        )
        assert record is not None
        assert [point.ordinal for point in record.points] == [1, 2, 3]
        assert [point.connector_type for point in record.points] == [
            ConnectorType.ccs,
            ConnectorType.chademo,
            ConnectorType.type2,
        ]
        assert [point.current_type for point in record.points] == [
            CurrentType.dc,
            CurrentType.dc,
            CurrentType.ac,
        ]
        assert record.max_power_kw == pytest.approx(150.0)
        assert record.charging_category is ChargingCategory.ultra_fast

    def test_empty_groups_are_skipped_entirely(self, records: list[ChargingStationRecord]) -> None:
        # Most rows fill one or two of the six groups; an empty group must add no connector.
        assert min(len(record.points) for record in records) >= 1
        assert max(len(record.points) for record in records) <= CONNECTOR_GROUPS * 4


# --------------------------------------------------------------------------- normalisation


class TestBundeslandNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Baden-Württemberg", Bundesland.BW),
            ("Baden-Wuerttemberg", Bundesland.BW),
            ("BW", Bundesland.BW),
            ("DE-BW", Bundesland.BW),
            ("Bayern", Bundesland.BY),
            ("Nordrhein-Westfalen", Bundesland.NW),
            ("Mecklenburg-Vorpommern", Bundesland.MV),
            ("Thüringen", Bundesland.TH),
            ("Sachsen-Anhalt", Bundesland.ST),
        ],
    )
    def test_known_spellings_resolve(
        self,
        write_register: Callable[..., Path],
        header_columns: list[str],
        raw: str,
        expected: Bundesland,
    ) -> None:
        record = parse_one(write_register(make_row(header_columns, Bundesland=raw)))
        assert record is not None
        assert record.bundesland is expected

    @pytest.mark.parametrize("raw", ["", "Atlantis", "Île-de-France", "Sachsen-Anhaltz", "-"])
    def test_unknown_spellings_yield_none_without_raising(
        self,
        write_register: Callable[..., Path],
        header_columns: list[str],
        raw: str,
    ) -> None:
        # An unmapped state is a data-quality finding, not a reason to drop the station or to
        # stop the ingestion run.
        record = parse_one(write_register(make_row(header_columns, Bundesland=raw)))
        assert record is not None
        assert record.bundesland is None
        assert record.external_id == "bnetza:9999999"


class TestDecimalAndDateParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("22", 22.0),
            ("3,7", 3.7),
            ("11,0", 11.0),
            # An operator entering English notation; destroying the dot would make it 37 kW.
            ("3.7", 3.7),
            # A thousands separator only counts as one when a decimal comma is present too.
            ("1.000,5", 1000.5),
            ("", None),
            ("keine Angabe", None),
            ("-5", None),
        ],
    )
    def test_power_column(
        self,
        write_register: Callable[..., Path],
        header_columns: list[str],
        raw: str,
        expected: float | None,
    ) -> None:
        record = parse_one(
            write_register(make_row(header_columns, **{"Nennleistung Stecker1": raw}))
        )
        assert record is not None
        actual = record.points[0].power_kw
        if expected is None:
            assert actual is None
        else:
            assert actual == pytest.approx(expected)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("11.01.2020", date(2020, 1, 11)),
            ("31.12.2019", date(2019, 12, 31)),
            ("", None),
            ("2020-01-11", None),
            ("31.02.2020", None),
        ],
    )
    def test_commissioning_date(
        self,
        write_register: Callable[..., Path],
        header_columns: list[str],
        raw: str,
        expected: date | None,
    ) -> None:
        record = parse_one(write_register(make_row(header_columns, Inbetriebnahmedatum=raw)))
        assert record is not None
        assert record.commissioned_on == expected


# --------------------------------------------------------------------------- failure modes


class TestUnusableRows:
    """Rows are skipped, never guessed at — and a skip never propagates as an exception."""

    @pytest.mark.parametrize(
        ("latitude", "longitude"),
        [
            ("", ""),
            ("48,442398", ""),
            ("", "9,659075"),
            ("keine", "Angabe"),
            # Out of range for WGS 84 — Pydantic rejects it and the row is dropped.
            ("95,0", "9,0"),
            ("48,0", "200,0"),
        ],
    )
    def test_rows_without_usable_coordinates_are_skipped_not_raised(
        self,
        write_register: Callable[..., Path],
        header_columns: list[str],
        latitude: str,
        longitude: str,
    ) -> None:
        path = write_register(
            make_row(header_columns, Breitengrad=latitude, **{"Längengrad": longitude})
        )
        skipped: list[str] = []
        parsed = list(
            iter_records(path, source_url=None, fetched_at=FETCHED_AT, on_skip=skipped.append)
        )
        assert parsed == []
        assert len(skipped) == 1

    def test_a_valid_coordinate_outside_germany_is_kept(
        self, write_register: Callable[..., Path], header_columns: list[str]
    ) -> None:
        # Paris. The parser's job is to read the file; deciding a station cannot be in France
        # belongs to the quality rules, which report it rather than hiding it here.
        record = parse_one(
            write_register(
                make_row(
                    header_columns,
                    Breitengrad="48,8566",
                    Bundesland="Île-de-France",
                    **{"Längengrad": "2,3522"},
                )
            )
        )
        assert record is not None
        assert record.coordinate.longitude == pytest.approx(2.3522)
        assert record.bundesland is None

    def test_blank_power_leaves_the_rating_unknown(
        self, write_register: Callable[..., Path], header_columns: list[str]
    ) -> None:
        record = parse_one(
            write_register(
                make_row(
                    header_columns,
                    **{
                        "Nennleistung Ladeeinrichtung [kW]": "",
                        "Nennleistung Stecker1": "",
                    },
                )
            )
        )
        assert record is not None
        # None, never 0.0: a 0 kW charger would be counted as a working slow charger.
        assert record.max_power_kw is None
        assert record.total_power_kw is None
        # An unrated site is `normal`, so it cannot inflate the fast-charger statistics.
        assert record.charging_category is ChargingCategory.normal

    def test_missing_id_gets_a_deterministic_synthetic_key(
        self, write_register: Callable[..., Path], header_columns: list[str]
    ) -> None:
        path = write_register(make_row(header_columns, **{"Ladeeinrichtungs-ID": ""}))
        record = parse_one(path)
        assert record is not None
        # Recomputed here from the identifying tuple the adapter documents — operator, street,
        # house number, postcode, latitude, longitude — so this asserts the *rule*, not the
        # string the implementation happened to produce.
        expected = hashlib.sha256(
            "\x00".join(
                (
                    _MINIMAL_ROW["Betreiber"],
                    _MINIMAL_ROW["Straße"],
                    _MINIMAL_ROW["Hausnummer"],
                    _MINIMAL_ROW["Postleitzahl"],
                    _MINIMAL_ROW["Breitengrad"],
                    _MINIMAL_ROW["Längengrad"],
                )
            ).encode("utf-8")
        ).hexdigest()[:32]
        assert record.external_id == f"bnetza:{expected}"
        assert record.provenance.source_identifier is None
        # Re-ingesting the same file must be an idempotent upsert, so the key is stable.
        assert parse_one(path) is not None
        assert parse_one(path).external_id == record.external_id  # type: ignore[union-attr]

    def test_truncated_row_is_reported_and_skipped(
        self, write_register: Callable[..., Path]
    ) -> None:
        skipped: list[str] = []
        path = write_register("1010338;Albwerk")
        parsed = list(
            iter_records(path, source_url=None, fetched_at=FETCHED_AT, on_skip=skipped.append)
        )
        assert parsed == []
        assert "2 of 47 columns" in skipped[0]

    def test_blank_lines_are_ignored_silently(
        self, write_register: Callable[..., Path], header_columns: list[str]
    ) -> None:
        skipped: list[str] = []
        path = write_register(make_row(header_columns), ";" * (EXPECTED_COLUMN_COUNT - 1), "")
        parsed = list(
            iter_records(path, source_url=None, fetched_at=FETCHED_AT, on_skip=skipped.append)
        )
        # A trailing all-empty line is how the export ends; it is not a data-quality finding.
        assert len(parsed) == 1
        assert skipped == []

    def test_header_only_file_yields_nothing(self, write_register: Callable[..., Path]) -> None:
        assert list(iter_records(write_register(), source_url=None, fetched_at=FETCHED_AT)) == []

    def test_empty_file_raises_invalid_source_data(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.csv"
        empty.write_bytes(b"")
        with pytest.raises(InvalidSourceData, match="no header row"):
            list(iter_records(empty, source_url=None, fetched_at=FETCHED_AT))

    def test_missing_required_column_raises_with_the_name(
        self, write_register: Callable[..., Path], header_columns: list[str]
    ) -> None:
        mangled = [
            "Ladesäulen-ID" if name == "Ladeeinrichtungs-ID" else name for name in header_columns
        ]
        path = write_register(columns=mangled)
        # A renamed key column is a schema change upstream, which must stop the run loudly
        # rather than produce 117 000 rows with synthesised ids.
        with pytest.raises(InvalidSourceData, match="Ladeeinrichtungs-ID"):
            list(iter_records(path, source_url=None, fetched_at=FETCHED_AT))

    def test_reordered_columns_still_parse(
        self, write_register: Callable[..., Path], header_columns: list[str]
    ) -> None:
        # Validation is by name, not position: the register has reshuffled columns between
        # editions before, and that must not be fatal.
        swapped = list(header_columns)
        first, second = swapped.index("Betreiber"), swapped.index("Ort")
        swapped[first], swapped[second] = swapped[second], swapped[first]
        values = dict.fromkeys(header_columns, "")
        values.update(_MINIMAL_ROW)
        buffer = io.StringIO()
        csv.writer(buffer, delimiter=";", lineterminator="").writerow(
            [values[column] for column in swapped]
        )
        record = parse_one(write_register(buffer.getvalue(), columns=swapped))
        assert record is not None
        assert record.operator == "Testbetreiber GmbH"
        assert record.city == "Teststadt"
