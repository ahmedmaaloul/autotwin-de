"""The Deutscher Wetterdienst 10-minute observation adapter.

Air temperature is the strongest weather driver of EV consumption (BUILD_SPEC §10.1), so every
trap in these files ends up in the energy model if it is missed. The ones under test here are
the ones the adapter's own docstring lists:

* space-padded fields (``"         44"``) on both the header and the data rows;
* ``-999`` meaning *missing* — averaged in as a number it turns a mild September afternoon
  into an ice age, so it must become ``None`` and never ``0.0``;
* the literal ``eor`` end-of-row sentinel column;
* ``MESS_DATUM`` in UTC, ``YYYYMMDDHHMM`` for the 10-minute products and ``YYYYMMDDHH`` for
  the hourly ones, so the width is measured rather than assumed;
* ISO-8859-1 encoding — ``Großenkneten`` is not valid UTF-8 at all;
* a genuinely fixed-width station catalogue whose names contain spaces, commas and
  parentheses, which ``str.split()`` silently truncates.

Nearest-station selection is checked against a haversine written out in this file, so the
expectation does not come from the same code path as the answer.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest

from autotwin_contracts import (
    Bundesland,
    Coordinate,
    DataOrigin,
    ProviderMode,
    SourceSystem,
    WeatherCondition,
    WeatherRecord,
    WeatherStationRecord,
)
from autotwin_core.config import DataMode, get_settings
from autotwin_ingestion.providers.weather import (
    DWD_ATTRIBUTION,
    MISSING_VALUE,
    PRODUCTS,
    DWDWeatherProvider,
    derive_condition,
    parse_product_file,
    parse_station_catalogue,
)

CATALOGUE_FIXTURE: Final[str] = "dwd_zehn_now_tu_Beschreibung_Stationen.txt"
TU_FRANKFURT_FIXTURE: Final[str] = "dwd_produkt_zehn_now_tu_01424.txt"
TU_STUTTGART_FIXTURE: Final[str] = "dwd_produkt_zehn_now_tu_04928.txt"
FF_STUTTGART_FIXTURE: Final[str] = "dwd_produkt_zehn_now_ff_04928.txt"

DWD_ENCODING: Final[str] = "iso-8859-1"
CATALOGUE_HEADER_ROWS: Final[int] = 2

# Published coordinates of the two demo-corridor endpoints.
FRANKFURT: Final[Coordinate] = Coordinate(latitude=50.1109, longitude=8.6821)
STUTTGART: Final[Coordinate] = Coordinate(latitude=48.7758, longitude=9.1829)
HAMBURG: Final[Coordinate] = Coordinate(latitude=53.5511, longitude=9.9937)
MUNICH: Final[Coordinate] = Coordinate(latitude=48.1351, longitude=11.5820)

# The last row of every bundled temperature file (both stations share the grid).
LATEST_TEMPERATURE_AT: Final[datetime] = datetime(2026, 9, 14, 13, 20, tzinfo=UTC)

EARTH_RADIUS_KM: Final[float] = 6371.0088
"""IUGG mean Earth radius, the same figure the WGS 84 haversine convention uses."""


def haversine_km(a: Coordinate, b: Coordinate) -> float:
    """Great-circle distance, written out here so the expectation is independent of the code."""
    phi_1, phi_2 = math.radians(a.latitude), math.radians(b.latitude)
    d_phi = phi_2 - phi_1
    d_lambda = math.radians(b.longitude - a.longitude)
    h = math.sin(d_phi / 2) ** 2 + math.cos(phi_1) * math.cos(phi_2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def catalogue_text(fixtures_dir: Path) -> str:
    """The station catalogue, decoded the way the adapter decodes it."""
    return (fixtures_dir / CATALOGUE_FIXTURE).read_text(encoding=DWD_ENCODING)


@pytest.fixture
def stations(catalogue_text: str) -> list[WeatherStationRecord]:
    return parse_station_catalogue(
        catalogue_text, source_url=None, fetched_at=datetime(2026, 9, 14, tzinfo=UTC)
    )


@pytest.fixture
def by_id(stations: list[WeatherStationRecord]) -> dict[str, WeatherStationRecord]:
    return {station.dwd_station_id: station for station in stations}


@pytest.fixture
def temperature_rows(fixtures_dir: Path) -> list[dict[str, str]]:
    """Station 04928's temperature product, parsed."""
    return parse_product_file(
        (fixtures_dir / TU_STUTTGART_FIXTURE).read_text(encoding=DWD_ENCODING)
    )


@pytest.fixture
def provider() -> DWDWeatherProvider:
    """A fixture-mode adapter: no HTTP client is ever constructed on this path."""
    return DWDWeatherProvider(data_mode=DataMode.fixture, max_stations=4)


@pytest.fixture
def override_product() -> Iterator[Callable[[str, str], None]]:
    """Drop a replacement product file into ``AUTOTWIN_DATA_DIR/fixtures``.

    That directory is the *documented* operator override consulted ahead of the bundled copy,
    so this exercises a real seam rather than monkeypatching: a deployment can pin a newer
    snapshot by dropping a file there. Everything written is removed again afterwards.
    """
    written: list[Path] = []

    def write(name: str, text: str) -> None:
        path = Path(get_settings().fixtures_dir) / name
        path.write_text(text, encoding=DWD_ENCODING)
        written.append(path)

    yield write
    for path in written:
        path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- product files


class TestProductFileParsing:
    """``produkt_*.txt`` is semicolon-separated *and* space-padded. Both must be handled."""

    def test_header_names_are_stripped(self, temperature_rows: list[dict[str, str]]) -> None:
        # The raw header reads "STATIONS_ID;MESS_DATUM;  QN;PP_10;..." — with the padding kept,
        # every lookup of "QN" would miss.
        assert list(temperature_rows[0]) == [
            "STATIONS_ID",
            "MESS_DATUM",
            "QN",
            "PP_10",
            "TT_10",
            "TM5_10",
            "RF_10",
            "TD_10",
        ]

    def test_values_are_stripped(self, temperature_rows: list[dict[str, str]]) -> None:
        # "       4928" and "  16.2" in the file; float("  16.2") would work but
        # int("       4928") in a dict key comparison would not.
        assert temperature_rows[0]["STATIONS_ID"] == "4928"
        assert temperature_rows[0]["TT_10"] == "16.2"
        assert all(value == value.strip() for row in temperature_rows for value in row.values())

    def test_eor_sentinel_is_dropped(self, temperature_rows: list[dict[str, str]]) -> None:
        # "eor" is a literal end-of-row marker, not a measurement.
        assert "eor" not in temperature_rows[0]
        assert all("eor" not in row for row in temperature_rows)

    def test_row_count_matches_the_ten_minute_grid(
        self, temperature_rows: list[dict[str, str]]
    ) -> None:
        # 00:00 to 13:20 inclusive in 10-minute steps is 13 * 6 + 2 + 1 = 81 rows.
        assert len(temperature_rows) == 81
        assert temperature_rows[0]["MESS_DATUM"] == "202609140000"
        assert temperature_rows[-1]["MESS_DATUM"] == "202609141320"

    def test_values_stay_text(self, temperature_rows: list[dict[str, str]]) -> None:
        # Deliberate: only the caller knows which columns are numeric, so converting -999
        # here would be guessing on the parser's behalf.
        assert all(isinstance(value, str) for value in temperature_rows[0].values())

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("", 0),
            ("   \n\n", 0),
            ("STATIONS_ID;MESS_DATUM;TT_10;eor\n", 0),
            ("STATIONS_ID;MESS_DATUM;TT_10;eor\n 4928;202609140000; 16.2;eor\n", 1),
            # A truncated line is dropped rather than zipped short, which would shift every
            # value one column to the left.
            ("STATIONS_ID;MESS_DATUM;TT_10;eor\n 4928;202609140000\n", 0),
            # Blank lines inside the file are skipped, not counted as rows.
            ("STATIONS_ID;MESS_DATUM;TT_10;eor\n\n 4928;202609140000; 16.2;eor\n", 1),
        ],
    )
    def test_boundary_inputs(self, text: str, expected: int) -> None:
        assert len(parse_product_file(text)) == expected

    def test_extra_columns_are_kept(self) -> None:
        rows = parse_product_file("A;B;eor\n1;2;eor;3\n")
        # More values than columns is tolerated: zip stops at the header, so the row still
        # yields its named fields instead of being lost.
        assert rows == [{"A": "1", "B": "2"}]


class TestMissingValueSentinel:
    """``-999`` is *missing*, and the difference between ``None`` and ``0.0`` is the whole test."""

    def test_the_fixture_really_contains_the_sentinel(self, fixtures_dir: Path) -> None:
        # Station 01424 reports no air pressure upstream and writes -999 for every row. If this
        # ever stops being true, the assertions below stop testing anything.
        text = (fixtures_dir / TU_FRANKFURT_FIXTURE).read_text(encoding=DWD_ENCODING)
        rows = parse_product_file(text)
        assert {row["PP_10"] for row in rows} == {"-999"}
        assert MISSING_VALUE == -999.0

    async def test_sentinel_becomes_none_never_zero(self, provider: DWDWeatherProvider) -> None:
        result = await provider.fetch_observations([FRANKFURT])
        frankfurt = _record_for(result.data, "01424")
        # Read naively, this station's pressure would be -999 hPa; clamped to a "sensible"
        # range it would be 0.0. Both are lies, and both would reach the ML feature vector.
        assert frankfurt.pressure_hpa is None
        # The temperature in the same row is a real measurement and must survive.
        assert frankfurt.temperature_c == pytest.approx(24.2)

    async def test_a_record_with_only_some_measurements_is_still_returned(
        self, provider: DWDWeatherProvider
    ) -> None:
        result = await provider.fetch_observations([FRANKFURT])
        frankfurt = _record_for(result.data, "01424")
        # Station 01424 has no wind product upstream (404) and no pressure sensor. The
        # interface asks for a record with a temperature and nulls, not for nothing at all.
        assert frankfurt.wind_speed_ms is None
        assert frankfurt.temperature_c is not None
        assert frankfurt.humidity_percent == pytest.approx(56.5)

    async def test_humidity_above_one_hundred_is_clamped(
        self,
        override_product: Callable[[str, str], None],
        provider: DWDWeatherProvider,
    ) -> None:
        # A saturated sensor occasionally reports 100.1 %. That is a calibration artefact, not
        # a reason to drop an otherwise good observation — and the record model rejects >100.
        override_product(
            TU_FRANKFURT_FIXTURE,
            "STATIONS_ID;MESS_DATUM;  QN;PP_10;TT_10;TM5_10;RF_10;TD_10;eor\n"
            "       1424;202609141320;    2;   -999;  12.0;  11.0; 100.5;  11.9;eor\n",
        )
        result = await provider.fetch_observations([FRANKFURT])
        assert _record_for(result.data, "01424").humidity_percent == pytest.approx(100.0)


class TestMessDatumWidth:
    """``MESS_DATUM`` width differs per product, so it is measured rather than assumed."""

    @pytest.mark.parametrize(
        ("stamp", "expected"),
        [
            # 10-minute products: YYYYMMDDHHMM.
            ("202609141320", datetime(2026, 9, 14, 13, 20, tzinfo=UTC)),
            # Hourly products: YYYYMMDDHH. A hand-rolled slice would read "14" as part of the
            # day and place the observation three weeks late.
            ("2026091413", datetime(2026, 9, 14, 13, 0, tzinfo=UTC)),
        ],
    )
    async def test_both_widths_parse_as_utc(
        self,
        override_product: Callable[[str, str], None],
        provider: DWDWeatherProvider,
        stamp: str,
        expected: datetime,
    ) -> None:
        override_product(
            TU_FRANKFURT_FIXTURE,
            "STATIONS_ID;MESS_DATUM;  QN;PP_10;TT_10;TM5_10;RF_10;TD_10;eor\n"
            f"       1424;{stamp};    2;   -999;  12.0;  11.0;  80.0;  11.9;eor\n",
        )
        result = await provider.fetch_observations([FRANKFURT])
        observed = _record_for(result.data, "01424").observed_at
        assert observed == expected
        # DWD publishes in UTC; a naive datetime here would be silently reinterpreted as local
        # time by anything downstream that localises.
        assert observed.tzinfo is not None
        assert observed.utcoffset() == timedelta(0)

    @pytest.mark.parametrize(
        "stamp",
        [
            # Eight digits is a *date*, not a timestamp; accepting it would date every
            # observation to midnight and collapse 81 rows onto one key.
            "20260914",
            "not-a-date",
            "",
            "20260914132000",
        ],
    )
    async def test_unreadable_timestamps_drop_the_row_without_raising(
        self,
        override_product: Callable[[str, str], None],
        stamp: str,
    ) -> None:
        override_product(
            TU_FRANKFURT_FIXTURE,
            "STATIONS_ID;MESS_DATUM;  QN;PP_10;TT_10;TM5_10;RF_10;TD_10;eor\n"
            f"       1424;{stamp};    2;   -999;  12.0;  11.0;  80.0;  11.9;eor\n",
        )
        temperature_only = DWDWeatherProvider(
            data_mode=DataMode.fixture, max_stations=1, products=("air_temperature",)
        )
        result = await temperature_only.fetch_observations([FRANKFURT])
        # Every row unreadable means no observation for that station — an omission, which the
        # interface allows, rather than an exception or an invented timestamp.
        assert result.data == []

    async def test_another_product_supplies_the_grid_when_temperature_is_unreadable(
        self,
        override_product: Callable[[str, str], None],
        provider: DWDWeatherProvider,
    ) -> None:
        # Documented behaviour, asserted so it stays deliberate: when the temperature file
        # yields no usable rows, the timestamp grid comes from the first product that did, and
        # the record is returned with a null temperature rather than dropped. A record with
        # precipitation and no temperature still tells the UI whether the road is wet.
        override_product(
            TU_FRANKFURT_FIXTURE,
            "STATIONS_ID;MESS_DATUM;  QN;PP_10;TT_10;TM5_10;RF_10;TD_10;eor\n"
            "       1424;not-a-date;    2;   -999;  12.0;  11.0;  80.0;  11.9;eor\n",
        )
        result = await provider.fetch_observations([FRANKFURT])
        frankfurt = _record_for(result.data, "01424")
        assert frankfurt.temperature_c is None
        assert frankfurt.precipitation_mm == pytest.approx(0.0)


class TestEncoding:
    """DWD Climate Data Center files are ISO-8859-1, and it is not a cosmetic difference."""

    def test_the_catalogue_is_not_valid_utf8(self, fixtures_dir: Path) -> None:
        raw = (fixtures_dir / CATALOGUE_FIXTURE).read_bytes()
        # 0xDF is "ß" in latin-1 and an invalid UTF-8 lead byte here, so reading this file as
        # UTF-8 does not mojibake quietly — it raises. Which is the good case; the quiet one is
        # a "replace" decode that turns Großenkneten into Gro?enkneten in the database.
        assert b"Gro\xdfenkneten" in raw
        with pytest.raises(UnicodeDecodeError):
            raw.decode("utf-8")

    def test_umlauts_round_trip(self, by_id: dict[str, WeatherStationRecord]) -> None:
        assert by_id["00044"].name == "Großenkneten"
        assert by_id["00259"].name == "Müllheim"
        assert by_id["04928"].bundesland is Bundesland.BW

    def test_no_replacement_characters_survive(self, stations: list[WeatherStationRecord]) -> None:
        # U+FFFD anywhere means a decode fell back to errors="replace".
        assert not any("�" in station.name for station in stations)


class TestStationCatalogue:
    """Fixed-width, with a dash ruler on line 2 and names that defeat ``str.split()``."""

    def test_every_data_row_parses(
        self, stations: list[WeatherStationRecord], catalogue_text: str
    ) -> None:
        # Independent row count: the file minus its two header lines and any blank tail.
        expected = len(
            [line for line in catalogue_text.splitlines()[CATALOGUE_HEADER_ROWS:] if line.strip()]
        )
        assert expected == 466
        assert len(stations) == expected

    def test_the_header_and_the_dash_ruler_are_skipped(
        self, stations: list[WeatherStationRecord]
    ) -> None:
        names = {station.name for station in stations}
        assert "Stationsname" not in names
        assert not any(name.startswith("---") for name in names)

    @pytest.mark.parametrize(
        ("station_id", "name"),
        [
            # Parentheses, spaces, a comma, and an abbreviation with a full stop. Splitting the
            # line on whitespace truncates every one of these to its first token.
            ("04928", "Stuttgart (Schnarrenberg)"),
            ("20098", "Seebach (Nationalpark Schwarzwald)"),
            ("00379", "Berka, Bad (Flugplatz)"),
            ("00314", "Kubschütz, Kr. Bautzen"),
            ("05538", "Wielenbach (Demollstr.)"),
            ("14138", "Falkenberg (Grenzschichtmessfeld)"),
            ("01424", "Frankfurt/Main-Westend"),
        ],
    )
    def test_multi_word_names_survive_intact(
        self, by_id: dict[str, WeatherStationRecord], station_id: str, name: str
    ) -> None:
        assert by_id[station_id].name == name

    def test_known_station_fields(self, by_id: dict[str, WeatherStationRecord]) -> None:
        # Catalogue line: "04928 19980604 20260914   314   48.8281   9.2000 Stuttgart
        # (Schnarrenberg)  Baden-Württemberg  Frei"
        schnarrenberg = by_id["04928"]
        assert schnarrenberg.coordinate.latitude == pytest.approx(48.8281)
        assert schnarrenberg.coordinate.longitude == pytest.approx(9.2000)
        assert schnarrenberg.elevation_m == pytest.approx(314.0)
        assert schnarrenberg.bundesland is Bundesland.BW
        assert schnarrenberg.valid_from == date(1998, 6, 4)
        assert schnarrenberg.valid_to == date(2026, 9, 14)
        assert schnarrenberg.provenance.source is SourceSystem.dwd
        assert schnarrenberg.provenance.data_origin is DataOrigin.official

    def test_station_ids_are_zero_padded_to_five_digits(
        self, stations: list[WeatherStationRecord]
    ) -> None:
        # The catalogue writes "00044" and the data rows write "         44". Both must
        # normalise, or the join between catalogue and observation silently finds nothing.
        assert all(len(station.dwd_station_id) == 5 for station in stations)
        assert all(station.dwd_station_id.isdigit() for station in stations)
        assert all(
            station.dwd_station_id == station.provenance.source_identifier for station in stations
        )

    def test_every_bundesland_resolves(self, stations: list[WeatherStationRecord]) -> None:
        # A station whose state did not resolve means the fixed-width tail slice drifted and
        # the name is probably wrong too.
        assert all(station.bundesland is not None for station in stations)

    def test_all_stations_are_inside_germany(self, stations: list[WeatherStationRecord]) -> None:
        for station in stations:
            assert 47.2 <= station.coordinate.latitude <= 55.1
            assert 5.8 <= station.coordinate.longitude <= 15.1

    def test_elevations_are_plausible(self, stations: list[WeatherStationRecord]) -> None:
        # Sea level to the Zugspitze (2962 m); a mis-sliced column lands far outside this.
        elevations = [station.elevation_m for station in stations if station.elevation_m]
        assert min(elevations) >= -5.0
        assert max(elevations) <= 3000.0

    @pytest.mark.parametrize(
        "line",
        [
            "this row is not a station at all",
            "00044 20070208 notadate 44 52.9336 8.2370 Großenkneten Niedersachsen Frei",
            "",
        ],
    )
    def test_unparsable_rows_are_skipped_not_raised(self, line: str) -> None:
        text = "header\n-----\n" + line + "\n"
        assert (
            parse_station_catalogue(
                text, source_url=None, fetched_at=datetime(2026, 9, 14, tzinfo=UTC)
            )
            == []
        )

    def test_empty_catalogue_yields_no_stations(self) -> None:
        assert (
            parse_station_catalogue(
                "", source_url=None, fetched_at=datetime(2026, 9, 14, tzinfo=UTC)
            )
            == []
        )


# --------------------------------------------------------------------------- selection


class TestNearestStationSelection:
    """The nearest station per point, de-duplicated and capped, so a route is not a flood."""

    def test_frankfurt_resolves_to_the_genuinely_nearest_station(
        self, stations: list[WeatherStationRecord]
    ) -> None:
        # argmin computed here with the local haversine, not with the provider's.
        nearest = min(stations, key=lambda station: haversine_km(FRANKFURT, station.coordinate))
        assert nearest.dwd_station_id == "01424"
        assert haversine_km(FRANKFURT, nearest.coordinate) == pytest.approx(2.0, abs=0.3)

    def test_stuttgart_resolves_to_schnarrenberg(
        self, stations: list[WeatherStationRecord]
    ) -> None:
        nearest = min(stations, key=lambda station: haversine_km(STUTTGART, station.coordinate))
        assert nearest.dwd_station_id == "04928"
        # Stuttgart-Echterdingen (04931) is the runner-up at ~10 km; picking it would put the
        # temperature 300 m lower on the airport plateau.
        assert haversine_km(STUTTGART, nearest.coordinate) == pytest.approx(5.9, abs=0.3)

    async def test_the_provider_agrees_with_the_independent_argmin(
        self, provider: DWDWeatherProvider, stations: list[WeatherStationRecord]
    ) -> None:
        result = await provider.fetch_observations([FRANKFURT, STUTTGART])
        expected = {
            min(stations, key=lambda s: haversine_km(point, s.coordinate)).dwd_station_id
            for point in (FRANKFURT, STUTTGART)
        }
        assert {record.station_id for record in result.data} == expected

    async def test_records_are_located_at_the_station_not_at_the_request(
        self, provider: DWDWeatherProvider, by_id: dict[str, WeatherStationRecord]
    ) -> None:
        result = await provider.fetch_observations([FRANKFURT])
        frankfurt = _record_for(result.data, "01424")
        # The interface allows answering with the nearest station, but then requires the record
        # to sit at the station — otherwise the measurement claims a precision it does not have.
        assert frankfurt.coordinate == by_id["01424"].coordinate
        assert frankfurt.coordinate != FRANKFURT

    async def test_several_points_near_one_station_yield_one_record(
        self, provider: DWDWeatherProvider
    ) -> None:
        # Three segment midpoints across Frankfurt must not become three downloads.
        nearby = [
            FRANKFURT,
            Coordinate(latitude=50.1150, longitude=8.6900),
            Coordinate(latitude=50.1200, longitude=8.6750),
        ]
        result = await provider.fetch_observations(nearby)
        assert len(result.data) == 1

    async def test_max_stations_caps_the_fan_out(
        self, stations: list[WeatherStationRecord]
    ) -> None:
        provider = DWDWeatherProvider(data_mode=DataMode.fixture, max_stations=1)
        result = await provider.fetch_observations([FRANKFURT, STUTTGART, HAMBURG, MUNICH])
        # The cap drops the stations furthest from the route, so what survives must be the
        # nearest of the four requests — Frankfurt, 2.0 km, beats Stuttgart's 5.9 km.
        assert len(result.data) <= 1
        distances = {
            point: min(haversine_km(point, s.coordinate) for s in stations)
            for point in (FRANKFURT, STUTTGART, HAMBURG, MUNICH)
        }
        assert min(distances, key=lambda point: distances[point]) is FRANKFURT
        assert [record.station_id for record in result.data] == ["01424"]

    async def test_no_points_is_not_an_error(self, provider: DWDWeatherProvider) -> None:
        result = await provider.fetch_observations([])
        # An empty request is a legitimate question with an empty answer; it must not raise and
        # must still carry the attribution and a reason.
        assert result.data == []
        assert DWD_ATTRIBUTION in result.warnings
        assert any("no DWD station" in warning for warning in result.warnings)

    @pytest.mark.parametrize("max_stations", [0, -1])
    def test_non_positive_max_stations_is_rejected_at_construction(self, max_stations: int) -> None:
        with pytest.raises(ValueError, match="max_stations"):
            DWDWeatherProvider(max_stations=max_stations)

    def test_unknown_product_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="unknown DWD product"):
            DWDWeatherProvider(products=("air_temperature", "sunshine"))

    def test_product_tokens_are_not_derivable_from_the_directory_names(self) -> None:
        # Listed, not computed: air_temperature files are named TU and precipitation "nieder".
        assert PRODUCTS == {
            "air_temperature": "TU",
            "wind": "wind",
            "precipitation": "nieder",
        }


class TestObservationRecords:
    """What the fixture-mode adapter returns end to end, with no network and no cache."""

    async def test_both_corridor_stations_answer(self, provider: DWDWeatherProvider) -> None:
        result = await provider.fetch_observations([FRANKFURT, STUTTGART])
        assert {record.station_id for record in result.data} == {"01424", "04928"}
        assert result.mode is ProviderMode.fixture
        assert result.is_degraded is True

    async def test_attribution_is_always_the_first_warning(
        self, provider: DWDWeatherProvider
    ) -> None:
        # The DWD licence requires it on anything derived from the data, and `warnings` is the
        # one channel that reaches the UI verbatim.
        result = await provider.fetch_observations([FRANKFURT])
        assert result.warnings[0] == DWD_ATTRIBUTION

    async def test_latest_row_wins_when_no_time_is_asked_for(
        self, provider: DWDWeatherProvider
    ) -> None:
        result = await provider.fetch_observations([STUTTGART])
        stuttgart = _record_for(result.data, "04928")
        assert stuttgart.observed_at == LATEST_TEMPERATURE_AT
        # Last line of dwd_produkt_zehn_now_tu_04928.txt: 23.9 °C, 988.0 hPa, 51.4 % RH; last
        # line of the wind file at that timestamp: 1.7 m/s.
        assert stuttgart.temperature_c == pytest.approx(23.9)
        assert stuttgart.pressure_hpa == pytest.approx(988.0)
        assert stuttgart.humidity_percent == pytest.approx(51.4)
        assert stuttgart.wind_speed_ms == pytest.approx(1.7)
        assert stuttgart.precipitation_mm == pytest.approx(0.0)

    async def test_condition_matches_the_measurements(self, provider: DWDWeatherProvider) -> None:
        result = await provider.fetch_observations([STUTTGART])
        stuttgart = _record_for(result.data, "04928")
        # Dry, 51 % RH, 1.7 m/s: no rule fires except the humidity proxy's bottom rung.
        assert stuttgart.condition is WeatherCondition.clear
        assert stuttgart.condition is derive_condition(
            temperature_c=stuttgart.temperature_c,
            precipitation_mm=stuttgart.precipitation_mm,
            wind_speed_ms=stuttgart.wind_speed_ms,
            humidity_percent=stuttgart.humidity_percent,
        )

    async def test_at_selects_the_last_row_at_or_before_the_request(
        self, provider: DWDWeatherProvider
    ) -> None:
        asked = datetime(2026, 9, 14, 6, 5, tzinfo=UTC)
        result = await provider.fetch_observations([STUTTGART], at=asked)
        # 06:05 is between grid points, so the 06:00 observation is the newest one that had
        # actually happened. Rounding up would report a measurement from the future.
        assert _record_for(result.data, "04928").observed_at == datetime(
            2026, 9, 14, 6, 0, tzinfo=UTC
        )

    async def test_at_on_a_grid_point_is_exact(self, provider: DWDWeatherProvider) -> None:
        asked = datetime(2026, 9, 14, 6, 0, tzinfo=UTC)
        result = await provider.fetch_observations([STUTTGART], at=asked)
        assert _record_for(result.data, "04928").observed_at == asked

    async def test_at_before_the_whole_file_falls_back_to_the_earliest_row(
        self, provider: DWDWeatherProvider
    ) -> None:
        result = await provider.fetch_observations([STUTTGART], at=datetime(2020, 1, 1, tzinfo=UTC))
        # Nothing in the file is old enough, so the closest real measurement is the first one.
        # Returning nothing would leave a route segment with no temperature at all.
        assert _record_for(result.data, "04928").observed_at == datetime(
            2026, 9, 14, 0, 0, tzinfo=UTC
        )

    async def test_naive_at_is_read_as_utc(self, provider: DWDWeatherProvider) -> None:
        # Deliberately naive: DWD publishes in UTC, so a caller that forgets the tzinfo
        # must be read as UTC rather than as the server's local time.
        naive = datetime(2026, 9, 14, 6, 5)
        aware = await provider.fetch_observations([STUTTGART], at=naive.replace(tzinfo=UTC))
        result = await provider.fetch_observations([STUTTGART], at=naive)
        assert (
            _record_for(result.data, "04928").observed_at
            == _record_for(aware.data, "04928").observed_at
        )

    async def test_provenance_points_at_the_station_file(
        self, provider: DWDWeatherProvider
    ) -> None:
        result = await provider.fetch_observations([STUTTGART])
        provenance = _record_for(result.data, "04928").provenance
        assert provenance.source is SourceSystem.dwd
        assert provenance.source_identifier == "04928"
        assert provenance.source_url is not None
        # The station id is zero-padded to five digits in the filename even though the data
        # rows write it bare.
        assert "10minutenwerte_TU_04928_now.zip" in provenance.source_url
        assert provenance.source_timestamp == LATEST_TEMPERATURE_AT

    async def test_a_station_with_no_bundled_product_is_omitted(self) -> None:
        # Only 01424 and 04928 have bundled product files. Hamburg's nearest station has none,
        # so it is left out rather than returned with every field null.
        provider = DWDWeatherProvider(data_mode=DataMode.fixture, max_stations=4)
        result = await provider.fetch_observations([HAMBURG])
        assert result.data == []
        assert result.mode is ProviderMode.fixture


class TestDeriveCondition:
    """The rule table of the adapter's docstring, including both sides of every threshold."""

    @pytest.mark.parametrize(
        ("temperature_c", "precipitation_mm", "wind_speed_ms", "humidity_percent", "expected"),
        [
            # Rule 1 — Beaufort 8 starts at exactly 17.2 m/s, and it outranks precipitation
            # because a Sturm is what changes a driver's plan.
            (None, None, 17.2, None, WeatherCondition.storm),
            (None, None, 17.19, None, WeatherCondition.unknown),
            (0.0, 5.0, 17.2, None, WeatherCondition.storm),
            # Rule 2/3 — wet at or below 0.5 °C is snow, above it rain.
            (0.5, 0.1, None, None, WeatherCondition.snow),
            (0.51, 0.1, None, None, WeatherCondition.rain),
            (-8.0, 2.0, None, None, WeatherCondition.snow),
            (18.0, 0.1, None, None, WeatherCondition.rain),
            # 0.09 mm in ten minutes is not "raining"; the humidity proxy decides instead.
            (18.0, 0.09, None, 50.0, WeatherCondition.clear),
            (18.0, 0.09, None, 99.0, WeatherCondition.fog),
            # Rules 4-6 — the humidity proxy, which is a proxy and documented as one.
            (None, None, None, 98.0, WeatherCondition.fog),
            (None, None, None, 97.99, WeatherCondition.clouds),
            (None, None, None, 85.0, WeatherCondition.clouds),
            (None, None, None, 84.99, WeatherCondition.clear),
            # Rule 7 — nothing measured at all cannot be summarised as "clear".
            (None, None, None, None, WeatherCondition.unknown),
            # Something measured but no humidity: dry is the most that can be said.
            (15.0, None, None, None, WeatherCondition.clear),
            (None, 0.0, None, None, WeatherCondition.clear),
        ],
    )
    def test_rule_table(
        self,
        temperature_c: float | None,
        precipitation_mm: float | None,
        wind_speed_ms: float | None,
        humidity_percent: float | None,
        expected: WeatherCondition,
    ) -> None:
        assert (
            derive_condition(
                temperature_c=temperature_c,
                precipitation_mm=precipitation_mm,
                wind_speed_ms=wind_speed_ms,
                humidity_percent=humidity_percent,
            )
            is expected
        )

    def test_a_freezing_temperature_alone_is_not_snow(self) -> None:
        # Snow needs precipitation. A dry frost is `clear`, and reporting snow would have the
        # UI warning drivers about a road that is merely cold.
        assert (
            derive_condition(
                temperature_c=-10.0,
                precipitation_mm=0.0,
                wind_speed_ms=None,
                humidity_percent=None,
            )
            is WeatherCondition.clear
        )


def _record_for(records: list[WeatherRecord], station_id: str) -> WeatherRecord:
    """The record for one station, failing the test with a readable message if it is absent."""
    for record in records:
        if record.station_id == station_id:
            return record
    pytest.fail(f"no observation for station {station_id}; got {[r.station_id for r in records]}")
