"""The degradation chain — ``live → cache → fixture`` (ADR 005, BUILD_SPEC §0.3 and §4).

This is the promise the whole project is sold on: *the application degrades; it does not fail,
and it does not lie about which it did.* Two halves, and both have to be tested, because either
one alone is worthless — a provider that keeps working but reports ``live`` for a six-hour-old
cache is worse than one that raises, since the UI then shows stale data with a green badge.

So every scenario below asserts three things at once:

* the call **returns usable data** rather than raising;
* ``ProviderResult.mode`` says **truthfully** where the data came from;
* ``is_degraded`` and ``warnings`` carry the reason on to the UI.

The upstream is simulated with ``respx``, which also guarantees the "no network, ever" rule:
an unmatched request raises inside the test instead of leaving the machine. The bytes the
mocks return are the committed fixtures, so a "live" answer here is parsed by exactly the code
that parses a real one.

Failure modes are exercised in ``DataMode.live``, where a provider must raise rather than
degrade, so the typed-error mapping of BUILD_SPEC §4 is visible: timeout → ``ProviderTimeout``,
429 → ``RateLimited``, a 200 carrying garbage → ``InvalidSourceData``.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import time
import zipfile
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
import respx
import structlog

from autotwin_contracts import Coordinate, ProviderMode, utc_now
from autotwin_core.config import DataMode, get_settings
from autotwin_core.errors import (
    ConfigurationMissing,
    InvalidSourceData,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
    RateLimited,
)
from autotwin_core.providers import AutoTwinHTTPClient, FileCache, ProviderResult
from autotwin_ingestion.providers.charging import BundesnetzagenturChargingProvider
from autotwin_ingestion.providers.routing import OSRMRoutingProvider
from autotwin_ingestion.providers.traffic import AutobahnTrafficProvider
from autotwin_ingestion.providers.weather import DWDWeatherProvider

FRANKFURT: Final[Coordinate] = Coordinate(latitude=50.1109, longitude=8.6821)
STUTTGART: Final[Coordinate] = Coordinate(latitude=48.7758, longitude=9.1829)

ROADWORKS_FIXTURE: Final[str] = "autobahn_A5_roadworks.json"
OSRM_FIXTURE: Final[str] = "osrm_frankfurt_stuttgart.json"
CATALOGUE_FIXTURE: Final[str] = "dwd_zehn_now_tu_Beschreibung_Stationen.txt"
TU_FRANKFURT_FIXTURE: Final[str] = "dwd_produkt_zehn_now_tu_01424.txt"
BNETZA_FIXTURE: Final[str] = "bnetza_ladesaeulenregister_sample.csv"

SIX_HOURS_S: Final[int] = 6 * 3600
"""How stale the cache is made in the age tests — one DWD refresh cycle plus a bit."""


def zipped(member: str, payload: bytes) -> bytes:
    """Wrap a product file in a ZIP, which is how DWD serves the ``now`` observations."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member, payload)
    return buffer.getvalue()


def cache_directory_present(root: Path) -> bool:
    """Whether the middle rung of the chain exists on disk at all."""
    return root.is_dir()


def age_entry(cache: FileCache, namespace: str, key: str, *, seconds: float, suffix: str) -> None:
    """Backdate a cache entry's mtime so TTL logic can be tested without sleeping."""
    path = cache.path_for(namespace, key, suffix=suffix)
    when = time.time() - seconds
    os.utime(path, (when, when))


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def cache_root(tmp_path: Path) -> Path:
    """A private cache directory that starts out non-existent."""
    return tmp_path / "cache"


@pytest.fixture
def cache(cache_root: Path) -> FileCache:
    return FileCache(cache_root)


@pytest.fixture
async def http() -> AsyncIterator[AutoTwinHTTPClient]:
    """An injected client, so ``respx`` sees the traffic and settings are not consulted.

    ``max_attempts=1`` because the retry policy is not what these tests are about: with the
    default three attempts every simulated 500 would cost 1.5 s of real backoff.
    """
    async with httpx.AsyncClient() as raw:
        yield AutoTwinHTTPClient(client=raw, max_attempts=1)


@pytest.fixture
def autobahn_url() -> str:
    base = get_settings().autobahn_base_url.rstrip("/")
    return f"{base}/A5/services/roadworks"


@pytest.fixture
def roadworks_payload(fixtures_dir: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(
        (fixtures_dir / ROADWORKS_FIXTURE).read_text(encoding="utf-8")
    )
    return payload


@pytest.fixture
def osrm_payload(fixtures_dir: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((fixtures_dir / OSRM_FIXTURE).read_text(encoding="utf-8"))
    return payload


@pytest.fixture
def traffic(
    http: AutoTwinHTTPClient, cache: FileCache
) -> Callable[[DataMode], AutobahnTrafficProvider]:
    """Build a one-road, one-service traffic adapter in a given data mode."""

    def build(mode: DataMode) -> AutobahnTrafficProvider:
        return AutobahnTrafficProvider(
            http_client=http,
            cache=cache,
            data_mode=mode,
            roads=("A5",),
            services=("roadworks",),
        )

    return build


@pytest.fixture
def routing(
    http: AutoTwinHTTPClient, cache: FileCache
) -> Callable[[DataMode], OSRMRoutingProvider]:
    def build(mode: DataMode) -> OSRMRoutingProvider:
        return OSRMRoutingProvider(http_client=http, cache=cache, data_mode=mode)

    return build


@pytest.fixture
def weather(http: AutoTwinHTTPClient, cache: FileCache) -> Callable[[DataMode], DWDWeatherProvider]:
    """Temperature only: the wind and precipitation layers would drag their own modes in."""

    def build(mode: DataMode) -> DWDWeatherProvider:
        return DWDWeatherProvider(
            http_client=http,
            cache=cache,
            data_mode=mode,
            max_stations=1,
            products=("air_temperature",),
        )

    return build


# --------------------------------------------------------------------------- traffic


class TestTrafficDegradation:
    """Autobahn GmbH: one road, one service, three rungs of the ladder."""

    async def test_live_success_reports_live(
        self,
        traffic: Callable[[DataMode], AutobahnTrafficProvider],
        autobahn_url: str,
        roadworks_payload: dict[str, Any],
    ) -> None:
        with respx.mock(assert_all_called=True) as router:
            router.get(autobahn_url).mock(return_value=httpx.Response(200, json=roadworks_payload))
            result = await traffic(DataMode.cached).fetch_events()
        assert result.mode is ProviderMode.live
        assert result.is_degraded is False
        assert result.warnings == []
        assert len(result.data) == 12

    @pytest.mark.parametrize(
        ("label", "response"),
        [
            ("server error", httpx.Response(500)),
            ("bad gateway", httpx.Response(502)),
            ("maintenance", httpx.Response(503)),
        ],
    )
    async def test_upstream_failure_falls_back_to_cache(
        self,
        traffic: Callable[[DataMode], AutobahnTrafficProvider],
        autobahn_url: str,
        roadworks_payload: dict[str, Any],
        label: str,
        response: httpx.Response,
    ) -> None:
        provider = traffic(DataMode.cached)
        with respx.mock(assert_all_called=False) as router:
            route = router.get(autobahn_url).mock(
                return_value=httpx.Response(200, json=roadworks_payload)
            )
            warm = await provider.fetch_events()
            assert warm.mode is ProviderMode.live

            route.mock(return_value=response)
            degraded = await provider.fetch_events()

        # The same twelve events, still usable, now honestly labelled.
        assert degraded.mode is ProviderMode.cache
        assert degraded.is_degraded is True
        assert len(degraded.data) == len(warm.data)
        assert [record.external_id for record in degraded.data] == [
            record.external_id for record in warm.data
        ]
        assert any(label in warning or "unavailable" in warning for warning in degraded.warnings)

    async def test_connect_error_with_no_cache_falls_back_to_the_fixture(
        self,
        traffic: Callable[[DataMode], AutobahnTrafficProvider],
        autobahn_url: str,
        cache_root: Path,
    ) -> None:
        # The cache directory has never been created — a fresh container, a wiped volume.
        assert not cache_directory_present(cache_root)
        with respx.mock(assert_all_called=True) as router:
            router.get(autobahn_url).mock(side_effect=httpx.ConnectError("no route to host"))
            result = await traffic(DataMode.cached).fetch_events()
        assert result.mode is ProviderMode.fixture
        assert result.is_degraded is True
        # Usable data, not an empty list standing in for a failure.
        assert len(result.data) == 12
        assert any("unavailable" in warning for warning in result.warnings)

    async def test_the_fixture_rung_survives_a_deleted_cache_directory(
        self,
        traffic: Callable[[DataMode], AutobahnTrafficProvider],
        autobahn_url: str,
        cache_root: Path,
        roadworks_payload: dict[str, Any],
    ) -> None:
        provider = traffic(DataMode.cached)
        with respx.mock(assert_all_called=False) as router:
            route = router.get(autobahn_url).mock(
                return_value=httpx.Response(200, json=roadworks_payload)
            )
            await provider.fetch_events()
            assert cache_directory_present(cache_root)

            # Somebody cleans data/ between runs; the middle rung disappears mid-outage.
            shutil.rmtree(cache_root)
            route.mock(return_value=httpx.Response(503))
            result = await provider.fetch_events()

        assert not cache_directory_present(cache_root)
        assert result.mode is ProviderMode.fixture
        assert len(result.data) == 12

    async def test_live_mode_raises_instead_of_degrading(
        self, traffic: Callable[[DataMode], AutobahnTrafficProvider], autobahn_url: str
    ) -> None:
        # ``live`` is the policy that says "a failure is an error, not a silent downgrade".
        with respx.mock(assert_all_called=True) as router:
            router.get(autobahn_url).mock(return_value=httpx.Response(503))
            with pytest.raises(ProviderUnavailable):
                await traffic(DataMode.live).fetch_events()

    async def test_fixture_mode_never_touches_the_network(
        self, traffic: Callable[[DataMode], AutobahnTrafficProvider], autobahn_url: str
    ) -> None:
        with respx.mock(assert_all_called=False) as router:
            never = router.get(autobahn_url).mock(return_value=httpx.Response(200, json={}))
            result = await traffic(DataMode.fixture).fetch_events()
        # Not one request: fixture mode is what CI and offline development run on.
        assert never.call_count == 0
        assert result.mode is ProviderMode.fixture

    async def test_a_retryable_status_is_retried_before_giving_up(
        self,
        cache: FileCache,
        autobahn_url: str,
        roadworks_payload: dict[str, Any],
    ) -> None:
        async with httpx.AsyncClient() as raw:
            client = AutoTwinHTTPClient(client=raw, max_attempts=3, backoff_base_s=0.0)
            provider = AutobahnTrafficProvider(
                http_client=client,
                cache=cache,
                data_mode=DataMode.cached,
                roads=("A5",),
                services=("roadworks",),
            )
            with respx.mock(assert_all_called=True) as router:
                router.get(autobahn_url).mock(
                    side_effect=[
                        httpx.Response(503),
                        httpx.Response(503),
                        httpx.Response(200, json=roadworks_payload),
                    ]
                )
                result = await provider.fetch_events()
        # One transient blip absorbed without the user ever seeing a degraded badge.
        assert result.mode is ProviderMode.live
        assert len(result.data) == 12


# --------------------------------------------------------------------------- routing


class TestRoutingDegradation:
    """OSRM sits on the request path, so its chain is what keeps a user-facing page alive."""

    async def test_live_success_reports_live(
        self,
        routing: Callable[[DataMode], OSRMRoutingProvider],
        osrm_payload: dict[str, Any],
    ) -> None:
        with respx.mock(assert_all_called=True) as router:
            router.get(url__startswith=get_settings().osrm_base_url.rstrip("/")).mock(
                return_value=httpx.Response(200, json=osrm_payload)
            )
            result = await routing(DataMode.cached).route(FRANKFURT, STUTTGART)
        assert result.mode is ProviderMode.live
        assert result.is_degraded is False
        assert result.data.distance_m == pytest.approx(205_000, rel=0.03)

    async def test_failure_falls_back_to_cache_with_an_honest_age(
        self,
        routing: Callable[[DataMode], OSRMRoutingProvider],
        cache: FileCache,
        osrm_payload: dict[str, Any],
    ) -> None:
        base = get_settings().osrm_base_url.rstrip("/")
        provider = routing(DataMode.cached)
        with respx.mock(assert_all_called=False) as router:
            route = router.get(url__startswith=base).mock(
                return_value=httpx.Response(200, json=osrm_payload)
            )
            await provider.route(FRANKFURT, STUTTGART)

            key = "driving|8.68210,50.11090|9.18290,48.77580"
            age_entry(cache, "osrm", key, seconds=SIX_HOURS_S, suffix=".json")
            route.mock(return_value=httpx.Response(500))
            degraded = await provider.route(FRANKFURT, STUTTGART)

        assert degraded.mode is ProviderMode.cache
        assert degraded.is_degraded is True
        assert degraded.data.distance_m == pytest.approx(205_000, rel=0.03)
        # The result dates itself by when the *copy* was downloaded, not by when it was read.
        # Reporting "now" here is what would let the UI present six-hour-old data as fresh.
        age = (utc_now() - degraded.fetched_at).total_seconds()
        assert age == pytest.approx(SIX_HOURS_S, abs=60)
        assert any("Live routing unavailable" in warning for warning in degraded.warnings)

    async def test_a_route_cache_entry_never_expires(
        self,
        routing: Callable[[DataMode], OSRMRoutingProvider],
        cache: FileCache,
        osrm_payload: dict[str, Any],
    ) -> None:
        base = get_settings().osrm_base_url.rstrip("/")
        provider = routing(DataMode.cached)
        with respx.mock(assert_all_called=False) as router:
            route = router.get(url__startswith=base).mock(
                return_value=httpx.Response(200, json=osrm_payload)
            )
            await provider.route(FRANKFURT, STUTTGART)

            # A year old. OSRM models neither traffic nor time of day, so a route between two
            # fixed points is still correct; expiring it would only re-ask a volunteer-run
            # server the same question forever.
            key = "driving|8.68210,50.11090|9.18290,48.77580"
            age_entry(cache, "osrm", key, seconds=365 * 24 * 3600, suffix=".json")
            route.mock(side_effect=httpx.ConnectError("down"))
            result = await provider.route(FRANKFURT, STUTTGART)

        assert result.mode is ProviderMode.cache
        assert result.data.distance_m == pytest.approx(205_000, rel=0.03)

    async def test_no_cache_at_all_falls_back_to_the_bundled_route(
        self, routing: Callable[[DataMode], OSRMRoutingProvider], cache_root: Path
    ) -> None:
        assert not cache_directory_present(cache_root)
        with respx.mock(assert_all_called=True) as router:
            router.get(url__startswith=get_settings().osrm_base_url.rstrip("/")).mock(
                side_effect=httpx.ConnectError("refused")
            )
            result = await routing(DataMode.cached).route(FRANKFURT, STUTTGART)
        assert result.mode is ProviderMode.fixture
        assert result.is_degraded is True
        assert len(result.data.geometry) > 1000

    async def test_a_request_the_fixture_cannot_answer_raises(
        self, routing: Callable[[DataMode], OSRMRoutingProvider], cache_root: Path
    ) -> None:
        berlin = Coordinate(latitude=52.5200, longitude=13.4050)
        with respx.mock(assert_all_called=True) as router:
            router.get(url__startswith=get_settings().osrm_base_url.rstrip("/")).mock(
                side_effect=httpx.ConnectError("refused")
            )
            # The chain has an end. Answering a Berlin request with the Frankfurt geometry
            # would be inventing data, which outranks staying available.
            with pytest.raises(ProviderUnavailable, match="bundled fixture covers only"):
                await routing(DataMode.cached).route(berlin, STUTTGART)


# --------------------------------------------------------------------------- weather


class TestWeatherDegradation:
    """DWD: the catalogue and each station product walk the chain independently."""

    @pytest.fixture
    def dwd_urls(self) -> tuple[str, str]:
        base = get_settings().dwd_base_url.rstrip("/")
        root = "climate_environment/CDC/observations_germany/climate/10_minutes"
        return (
            f"{base}/{root}/air_temperature/now/zehn_now_tu_Beschreibung_Stationen.txt",
            f"{base}/{root}/air_temperature/now/10minutenwerte_TU_01424_now.zip",
        )

    @pytest.fixture
    def dwd_bytes(self, fixtures_dir: Path) -> tuple[bytes, bytes]:
        catalogue = (fixtures_dir / CATALOGUE_FIXTURE).read_bytes()
        product = zipped(
            "produkt_zehn_now_tu_20260914_01424.txt",
            (fixtures_dir / TU_FRANKFURT_FIXTURE).read_bytes(),
        )
        return catalogue, product

    async def test_live_success_reports_live(
        self,
        weather: Callable[[DataMode], DWDWeatherProvider],
        dwd_urls: tuple[str, str],
        dwd_bytes: tuple[bytes, bytes],
    ) -> None:
        catalogue_url, product_url = dwd_urls
        catalogue, product = dwd_bytes
        with respx.mock(assert_all_called=True) as router:
            router.get(catalogue_url).mock(return_value=httpx.Response(200, content=catalogue))
            router.get(product_url).mock(return_value=httpx.Response(200, content=product))
            result = await weather(DataMode.cached).fetch_observations([FRANKFURT])
        assert result.mode is ProviderMode.live
        assert result.is_degraded is False
        assert [record.station_id for record in result.data] == ["01424"]
        assert result.data[0].temperature_c == pytest.approx(24.2)

    async def test_outage_falls_back_to_cache(
        self,
        weather: Callable[[DataMode], DWDWeatherProvider],
        dwd_urls: tuple[str, str],
        dwd_bytes: tuple[bytes, bytes],
    ) -> None:
        catalogue_url, product_url = dwd_urls
        catalogue, product = dwd_bytes
        with respx.mock(assert_all_called=False) as router:
            catalogue_route = router.get(catalogue_url).mock(
                return_value=httpx.Response(200, content=catalogue)
            )
            product_route = router.get(product_url).mock(
                return_value=httpx.Response(200, content=product)
            )
            await weather(DataMode.cached).fetch_observations([FRANKFURT])

            catalogue_route.mock(return_value=httpx.Response(500))
            product_route.mock(return_value=httpx.Response(500))
            # A second adapter instance, because the first memoises the catalogue for a day —
            # otherwise this would test the memo rather than the cache.
            degraded = await weather(DataMode.cached).fetch_observations([FRANKFURT])

        assert degraded.mode is ProviderMode.cache
        assert degraded.is_degraded is True
        # Still a real temperature from a real station, which is the point of the middle rung.
        assert degraded.data[0].temperature_c == pytest.approx(24.2)

    async def test_no_cache_falls_back_to_the_bundled_station_files(
        self,
        weather: Callable[[DataMode], DWDWeatherProvider],
        dwd_urls: tuple[str, str],
        cache_root: Path,
    ) -> None:
        assert not cache_directory_present(cache_root)
        with respx.mock(assert_all_called=False) as router:
            router.get(url__startswith=get_settings().dwd_base_url.rstrip("/")).mock(
                side_effect=httpx.ConnectError("opendata.dwd.de unreachable")
            )
            result = await weather(DataMode.cached).fetch_observations([FRANKFURT])
        assert result.mode is ProviderMode.fixture
        assert result.is_degraded is True
        assert result.data[0].temperature_c == pytest.approx(24.2)

    async def test_a_station_that_does_not_report_a_product_is_not_an_outage(
        self,
        http: AutoTwinHTTPClient,
        cache: FileCache,
        dwd_urls: tuple[str, str],
        dwd_bytes: tuple[bytes, bytes],
    ) -> None:
        catalogue_url, product_url = dwd_urls
        catalogue, product = dwd_bytes
        base = get_settings().dwd_base_url.rstrip("/")
        provider = DWDWeatherProvider(
            http_client=http,
            cache=cache,
            data_mode=DataMode.live,
            max_stations=1,
            products=("air_temperature", "wind"),
        )
        with respx.mock(assert_all_called=False) as router:
            router.get(catalogue_url).mock(return_value=httpx.Response(200, content=catalogue))
            router.get(product_url).mock(return_value=httpx.Response(200, content=product))
            # Station 01424 genuinely has no wind file upstream; DWD answers 404.
            router.get(url__startswith=base).mock(return_value=httpx.Response(404))
            result = await provider.fetch_observations([FRANKFURT])
        # Even in live mode this must not raise: a 404 here is a fact about the station, not an
        # outage, and a record with a temperature beats no record at all.
        assert result.data[0].temperature_c == pytest.approx(24.2)
        assert result.data[0].wind_speed_ms is None
        assert any("wind for station 01424" in warning for warning in result.warnings)


# --------------------------------------------------------------------------- charging


class TestChargingDegradation:
    """The Ladesäulenregister has an extra rung at the front: *finding* the file."""

    @pytest.fixture
    def register_bytes(self, fixtures_dir: Path) -> bytes:
        return (fixtures_dir / BNETZA_FIXTURE).read_bytes()

    @pytest.fixture
    def charging(
        self, http: AutoTwinHTTPClient, cache: FileCache
    ) -> Callable[[DataMode], BundesnetzagenturChargingProvider]:
        def build(mode: DataMode) -> BundesnetzagenturChargingProvider:
            return BundesnetzagenturChargingProvider(
                http_client=http, cache=cache, data_mode=mode, limit=10
            )

        return build

    async def test_the_configured_url_answering_is_a_live_result(
        self,
        charging: Callable[[DataMode], BundesnetzagenturChargingProvider],
        register_bytes: bytes,
    ) -> None:
        with respx.mock(assert_all_called=False) as router:
            router.get(url__regex=r".*Ladesaeulenregister.*\.csv").mock(
                return_value=httpx.Response(200, content=register_bytes)
            )
            # The landing-page scrape is tried too and is allowed to fail: the monthly filename
            # rotation makes a dead URL the expected case for at least one candidate.
            router.get(url__regex=r".*").mock(return_value=httpx.Response(404))
            result = await charging(DataMode.cached).fetch_stations()
        assert result.mode is ProviderMode.live
        assert result.is_degraded is False
        assert len(result.data) == 10
        assert result.data[0].external_id == "bnetza:1010338"

    async def test_the_landing_page_link_is_used_when_the_configured_url_is_dead(
        self,
        charging: Callable[[DataMode], BundesnetzagenturChargingProvider],
        register_bytes: bytes,
    ) -> None:
        discovered = (
            "https://data.bundesnetzagentur.de/Bundesnetzagentur/DE/Fachthemen/"
            "ElektrizitaetundGas/E-Mobilitaet/Ladesaeulenregister_BNetzA_2026-09-01.csv"
        )
        configured = get_settings().bnetza_download_url
        with respx.mock(assert_all_called=False) as router:
            router.get(configured).mock(return_value=httpx.Response(404))
            router.get(discovered).mock(return_value=httpx.Response(200, content=register_bytes))
            router.get(url__regex=r".*start\.html").mock(
                return_value=httpx.Response(
                    200, text=f'<a href="{discovered}">Ladesäulenregister (CSV)</a>'
                )
            )
            result = await charging(DataMode.cached).fetch_stations()
        # All three front rungs are "live"; only the cache and fixture rungs downgrade.
        assert result.mode is ProviderMode.live
        assert result.source_url == discovered
        # The edition date in the filename becomes the provenance timestamp.
        assert result.data[0].provenance.source_timestamp is not None

    async def test_download_failure_falls_back_to_the_archived_copy(
        self,
        charging: Callable[[DataMode], BundesnetzagenturChargingProvider],
        register_bytes: bytes,
    ) -> None:
        with respx.mock(assert_all_called=False) as router:
            csv_route = router.get(url__regex=r".*Ladesaeulenregister.*\.csv").mock(
                return_value=httpx.Response(200, content=register_bytes)
            )
            router.get(url__regex=r".*").mock(return_value=httpx.Response(404))
            await charging(DataMode.cached).fetch_stations()

            csv_route.mock(return_value=httpx.Response(500))
            degraded = await charging(DataMode.cached).fetch_stations()

        assert degraded.mode is ProviderMode.cache
        assert degraded.is_degraded is True
        # 55 MB of real charging stations from six hours ago beat a bundled snapshot.
        assert len(degraded.data) == 10
        assert any("Live source unavailable" in warning for warning in degraded.warnings)

    async def test_nothing_reachable_and_no_cache_falls_back_to_the_bundled_excerpt(
        self,
        charging: Callable[[DataMode], BundesnetzagenturChargingProvider],
        cache_root: Path,
    ) -> None:
        assert not cache_directory_present(cache_root)
        with respx.mock(assert_all_called=False) as router:
            router.get(url__regex=r".*").mock(side_effect=httpx.ConnectError("offline"))
            result = await charging(DataMode.cached).fetch_stations()
        assert result.mode is ProviderMode.fixture
        assert result.is_degraded is True
        assert len(result.data) == 10
        # The UI must be able to say this is an excerpt, not the whole register.
        assert any("excerpt" in warning for warning in result.warnings)

    async def test_live_mode_reports_every_url_it_tried(
        self, charging: Callable[[DataMode], BundesnetzagenturChargingProvider]
    ) -> None:
        with respx.mock(assert_all_called=False) as router:
            router.get(url__regex=r".*").mock(return_value=httpx.Response(429))
            with pytest.raises(ConfigurationMissing) as raised:
                await charging(DataMode.live).fetch_stations()
        # Deliberate: this adapter collapses per-URL failures into one "nothing answered"
        # error rather than surfacing the first RateLimited, because the monthly rotation
        # means some candidates are *expected* to fail. The individual errors are kept so an
        # operator can still see that it was a 429 rather than a 404.
        attempts = raised.value.details["attempts"]
        assert isinstance(attempts, list)
        assert any("rate limited" in str(attempt) for attempt in attempts)


# --------------------------------------------------------------------------- typed errors


class TestErrorTranslation:
    """BUILD_SPEC §4: an upstream problem is a *typed* error the caller can act on."""

    @pytest.mark.parametrize(
        ("side_effect", "expected"),
        [
            # Read and connect timeouts both mean "try again later", which is a different
            # remedy from "the host is refusing connections".
            (httpx.ReadTimeout("read timed out"), ProviderTimeout),
            (httpx.ConnectTimeout("connect timed out"), ProviderTimeout),
            (httpx.ConnectError("connection refused"), ProviderUnavailable),
            (httpx.RemoteProtocolError("server disconnected"), ProviderUnavailable),
        ],
    )
    async def test_transport_failures_on_the_traffic_api(
        self,
        traffic: Callable[[DataMode], AutobahnTrafficProvider],
        autobahn_url: str,
        side_effect: Exception,
        expected: type[Exception],
    ) -> None:
        with respx.mock(assert_all_called=True) as router:
            router.get(autobahn_url).mock(side_effect=side_effect)
            with pytest.raises(expected):
                await traffic(DataMode.live).fetch_events()

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (429, RateLimited),
            (500, ProviderUnavailable),
            (503, ProviderUnavailable),
            (408, ProviderTimeout),
            # 401/403 always mean a missing key or a stale registration for these sources, so
            # pointing the operator at their configuration beats saying "the source is down".
            (401, ConfigurationMissing),
            (403, ConfigurationMissing),
            # AutoTwin only requests hard-coded endpoints, so "that URL is gone" is a schema
            # change, not a client mistake.
            (404, InvalidSourceData),
            (400, InvalidSourceData),
        ],
    )
    async def test_status_codes_on_the_traffic_api(
        self,
        traffic: Callable[[DataMode], AutobahnTrafficProvider],
        autobahn_url: str,
        status: int,
        expected: type[Exception],
    ) -> None:
        with respx.mock(assert_all_called=True) as router:
            router.get(autobahn_url).mock(return_value=httpx.Response(status))
            with pytest.raises(expected):
                await traffic(DataMode.live).fetch_events()

    @pytest.mark.parametrize(
        ("status", "expected"),
        [(429, RateLimited), (500, ProviderUnavailable), (403, ConfigurationMissing)],
    )
    async def test_status_codes_on_the_routing_engine(
        self,
        routing: Callable[[DataMode], OSRMRoutingProvider],
        status: int,
        expected: type[Exception],
    ) -> None:
        # The public OSRM demo server really does rate-limit, and the API maps RateLimited to
        # 429 with the upstream's own retry advice rather than to a generic 502.
        with respx.mock(assert_all_called=True) as router:
            router.get(url__startswith=get_settings().osrm_base_url.rstrip("/")).mock(
                return_value=httpx.Response(status)
            )
            with pytest.raises(expected):
                await routing(DataMode.live).route(FRANKFURT, STUTTGART)

    async def test_routing_timeout(
        self, routing: Callable[[DataMode], OSRMRoutingProvider]
    ) -> None:
        with respx.mock(assert_all_called=True) as router:
            router.get(url__startswith=get_settings().osrm_base_url.rstrip("/")).mock(
                side_effect=httpx.ReadTimeout("slow")
            )
            with pytest.raises(ProviderTimeout):
                await routing(DataMode.live).route(FRANKFURT, STUTTGART)

    @pytest.mark.parametrize(
        "body",
        [
            "<html><body>503 Service Unavailable</body></html>",
            "not json at all",
            "",
            "{unclosed: ",
        ],
    )
    async def test_a_200_carrying_garbage_is_invalid_source_data(
        self,
        traffic: Callable[[DataMode], AutobahnTrafficProvider],
        autobahn_url: str,
        body: str,
    ) -> None:
        # German endpoints do serve an HTML error page with status 200. That must fail as a
        # schema problem, not crash somewhere inside a parser with a KeyError.
        with respx.mock(assert_all_called=True) as router:
            router.get(autobahn_url).mock(return_value=httpx.Response(200, text=body))
            with pytest.raises(InvalidSourceData):
                await traffic(DataMode.live).fetch_events()

    async def test_a_200_carrying_the_wrong_json_shape_is_invalid_source_data(
        self, traffic: Callable[[DataMode], AutobahnTrafficProvider], autobahn_url: str
    ) -> None:
        with respx.mock(assert_all_called=True) as router:
            router.get(autobahn_url).mock(return_value=httpx.Response(200, json=[1, 2, 3]))
            with pytest.raises(InvalidSourceData, match="expected a JSON object"):
                await traffic(DataMode.live).fetch_events()

    async def test_routing_garbage_is_invalid_source_data(
        self, routing: Callable[[DataMode], OSRMRoutingProvider]
    ) -> None:
        with respx.mock(assert_all_called=True) as router:
            router.get(url__startswith=get_settings().osrm_base_url.rstrip("/")).mock(
                return_value=httpx.Response(200, text="<html>rate limit exceeded</html>")
            )
            with pytest.raises(InvalidSourceData):
                await routing(DataMode.live).route(FRANKFURT, STUTTGART)

    async def test_weather_catalogue_garbage_is_invalid_source_data(
        self, weather: Callable[[DataMode], DWDWeatherProvider]
    ) -> None:
        with respx.mock(assert_all_called=False) as router:
            router.get(url__startswith=get_settings().dwd_base_url.rstrip("/")).mock(
                return_value=httpx.Response(200, content=b"this is not a station catalogue")
            )
            # A 200 that parses to zero stations is a schema change, not an empty answer: DWD
            # always has stations.
            with pytest.raises(InvalidSourceData, match="no parsable station rows"):
                await weather(DataMode.live).fetch_observations([FRANKFURT])

    @pytest.mark.parametrize(
        ("error", "status"),
        [
            (ProviderTimeout("x"), 504),
            (ProviderUnavailable("x"), 503),
            (RateLimited("x"), 429),
            (InvalidSourceData("x"), 502),
            (ConfigurationMissing("x"), 500),
        ],
    )
    def test_every_provider_error_carries_its_http_status(
        self, error: ProviderError, status: int
    ) -> None:
        # The API maps these onto status codes generically, so a subclass that forgot its
        # status would answer 500 for everything (BUILD_SPEC §7).
        assert error.http_status == status
        assert error.code


# --------------------------------------------------------------------------- honesty


class TestModeHonesty:
    """``mode`` is what the UI renders. Overstating it is the one unrecoverable failure."""

    async def test_one_cached_road_downgrades_the_whole_answer(
        self,
        http: AutoTwinHTTPClient,
        cache: FileCache,
        roadworks_payload: dict[str, Any],
    ) -> None:
        base = get_settings().autobahn_base_url.rstrip("/")
        provider = AutobahnTrafficProvider(
            http_client=http,
            cache=cache,
            data_mode=DataMode.cached,
            roads=("A5", "A8"),
            services=("roadworks",),
        )
        with respx.mock(assert_all_called=False) as router:
            a5 = router.get(f"{base}/A5/services/roadworks").mock(
                return_value=httpx.Response(200, json=roadworks_payload)
            )
            a8 = router.get(f"{base}/A8/services/roadworks").mock(
                return_value=httpx.Response(200, json=roadworks_payload)
            )
            assert (await provider.fetch_events()).mode is ProviderMode.live

            a5.mock(return_value=httpx.Response(503))
            mixed = await provider.fetch_events()
            assert a8.call_count >= 2

        # A partly stale map must not claim to be live: the weakest contributing mode wins.
        assert mixed.mode is ProviderMode.cache
        assert mixed.is_degraded is True

    @pytest.mark.parametrize(
        ("mode", "degraded"),
        [
            (ProviderMode.live, False),
            (ProviderMode.cache, True),
            (ProviderMode.fixture, True),
        ],
    )
    def test_is_degraded_covers_cache_and_fixture(self, mode: ProviderMode, degraded: bool) -> None:
        # Every caller that only wants to know "should I show a warning badge" asks this one
        # property, so cache must count as degraded even though the data is real.
        result: ProviderResult[list[str]] = ProviderResult(
            data=[], mode=mode, source_url=None, fetched_at=utc_now()
        )
        assert result.is_degraded is degraded

    async def test_cached_traffic_reports_the_age_of_the_copy(
        self,
        traffic: Callable[[DataMode], AutobahnTrafficProvider],
        cache: FileCache,
        autobahn_url: str,
        roadworks_payload: dict[str, Any],
    ) -> None:
        provider = traffic(DataMode.cached)
        with respx.mock(assert_all_called=False) as router:
            route = router.get(autobahn_url).mock(
                return_value=httpx.Response(200, json=roadworks_payload)
            )
            await provider.fetch_events()
            age_entry(cache, "autobahn", autobahn_url, seconds=SIX_HOURS_S, suffix=".json")
            route.mock(return_value=httpx.Response(503))
            degraded = await provider.fetch_events()

        assert degraded.mode is ProviderMode.cache
        assert (utc_now() - degraded.fetched_at).total_seconds() == pytest.approx(
            SIX_HOURS_S, abs=60
        )

    async def test_warnings_reach_the_result_rather_than_only_the_log(
        self, traffic: Callable[[DataMode], AutobahnTrafficProvider], autobahn_url: str
    ) -> None:
        with respx.mock(assert_all_called=True) as router:
            router.get(autobahn_url).mock(side_effect=httpx.ConnectError("down"))
            result = await traffic(DataMode.cached).fetch_events()
        # `warnings` is the only channel that reaches the UI verbatim; a reason that exists
        # only in the server log tells the user nothing.
        assert result.warnings
        assert any("unavailable" in warning for warning in result.warnings)


# --------------------------------------------------------------------------- the cache itself


class TestFileCache:
    """The middle rung. Without it the chain is a slogan."""

    def test_round_trips_bytes_and_json(self, cache: FileCache) -> None:
        assert cache.set_bytes("dwd", "k", b"payload") is True
        assert cache.get_bytes("dwd", "k") == b"payload"
        assert cache.set_json("osrm", "k", {"code": "Ok"}) is True
        assert cache.get_json("osrm", "k") == {"code": "Ok"}

    def test_a_miss_is_none_not_an_error(self, cache: FileCache) -> None:
        assert cache.get_bytes("dwd", "never-written") is None
        assert cache.get_json("dwd", "never-written") is None
        assert cache.age_seconds("dwd", "never-written") is None

    def test_bytes_and_json_entries_do_not_collide(self, cache: FileCache) -> None:
        # Same namespace and key, different representations: the suffix keeps them apart, so a
        # JSON read never returns a half-decoded ZIP.
        cache.set_bytes("dwd", "same", b"\x00\x01")
        cache.set_json("dwd", "same", {"a": 1})
        assert cache.get_bytes("dwd", "same") == b"\x00\x01"
        assert cache.get_json("dwd", "same") == {"a": 1}

    def test_ttl_expiry(self, cache_root: Path) -> None:
        cache = FileCache(cache_root, ttl_seconds=600)
        cache.set_bytes("dwd", "k", b"payload")
        assert cache.get_bytes("dwd", "k") == b"payload"

        # 601 seconds old against a 600-second TTL: one second past the line.
        age_entry(cache, "dwd", "k", seconds=601, suffix=".bin")
        assert cache.get_bytes("dwd", "k") is None
        # The entry is not deleted, only refused — which is what makes allow_stale possible.
        assert cache.path_for("dwd", "k", suffix=".bin").is_file()

    def test_an_entry_exactly_on_the_ttl_is_still_fresh(self, cache_root: Path) -> None:
        cache = FileCache(cache_root, ttl_seconds=600)
        cache.set_bytes("dwd", "k", b"payload")
        age_entry(cache, "dwd", "k", seconds=599, suffix=".bin")
        # The comparison is `age <= ttl`, so just inside the window still serves.
        assert cache.get_bytes("dwd", "k") == b"payload"

    def test_allow_stale_serves_an_expired_entry(self, cache_root: Path) -> None:
        cache = FileCache(cache_root, ttl_seconds=600)
        cache.set_bytes("dwd", "k", b"payload")
        age_entry(cache, "dwd", "k", seconds=SIX_HOURS_S, suffix=".bin")
        assert cache.get_bytes("dwd", "k") is None
        # Providers set this on their last attempt before dropping to a fixture: six-hour-old
        # real data beats a bundled snapshot from last quarter — as long as the mode says so.
        assert cache.get_bytes("dwd", "k", allow_stale=True) == b"payload"
        assert cache.get_json("dwd", "missing", allow_stale=True) is None

    def test_per_read_ttl_override(self, cache_root: Path) -> None:
        cache = FileCache(cache_root, ttl_seconds=SIX_HOURS_S)
        cache.set_json("autobahn", "k", {"roadworks": []})
        age_entry(cache, "autobahn", "k", seconds=900, suffix=".json")
        # The instance TTL would serve this; a stricter per-read TTL must win, which is how the
        # DWD adapter gives its ten-minute products a shorter life than the catalogue.
        assert cache.get_json("autobahn", "k", ttl_seconds=600) is None
        assert cache.get_json("autobahn", "k") == {"roadworks": []}

    @pytest.mark.parametrize("ttl", [0, -1])
    def test_a_non_positive_ttl_disables_expiry(self, cache_root: Path, ttl: int) -> None:
        # Zero means "never expires", which is what the routing adapter wants: a route between
        # two fixed points does not change when the traffic does.
        cache = FileCache(cache_root, ttl_seconds=ttl)
        cache.set_json("osrm", "k", {"code": "Ok"})
        age_entry(cache, "osrm", "k", seconds=365 * 24 * 3600, suffix=".json")
        assert cache.get_json("osrm", "k") == {"code": "Ok"}

    def test_age_seconds_reports_the_entry_age(self, cache: FileCache) -> None:
        cache.set_bytes("dwd", "k", b"payload")
        assert cache.age_seconds("dwd", "k") == pytest.approx(0.0, abs=5)
        age_entry(cache, "dwd", "k", seconds=SIX_HOURS_S, suffix=".bin")
        # This is what a provider turns into `fetched_at`, so the UI can say how old the data
        # is instead of implying it is fresh.
        assert cache.age_seconds("dwd", "k") == pytest.approx(SIX_HOURS_S, abs=5)

    def test_a_corrupt_json_entry_is_treated_as_a_miss_and_removed(self, cache: FileCache) -> None:
        cache.set_json("osrm", "k", {"code": "Ok"})
        path = cache.path_for("osrm", "k", suffix=".json")
        path.write_text("{ truncated", encoding="utf-8")
        assert cache.get_json("osrm", "k") is None
        # Keeping it would make every subsequent run fail the same way; it can only have come
        # from a disk error or a killed process.
        assert not path.exists()

    def test_invalidate_removes_both_representations(self, cache: FileCache) -> None:
        cache.set_bytes("dwd", "k", b"a")
        cache.set_json("dwd", "k", {"b": 1})
        cache.invalidate("dwd", "k")
        assert cache.get_bytes("dwd", "k") is None
        assert cache.get_json("dwd", "k") is None

    def test_clear_is_scoped_to_a_namespace(self, cache: FileCache) -> None:
        cache.set_bytes("dwd", "k", b"a")
        cache.set_bytes("osrm", "k", b"b")
        assert cache.clear("dwd") == 1
        assert cache.get_bytes("dwd", "k") is None
        assert cache.get_bytes("osrm", "k") == b"b"
        assert cache.clear() == 1
        assert cache.get_bytes("osrm", "k") is None

    def test_clearing_a_namespace_that_was_never_written_is_zero(self, cache: FileCache) -> None:
        assert cache.clear("never-used") == 0

    @pytest.mark.parametrize(
        ("namespace", "directory"),
        [
            ("bnetza", "bnetza"),
            ("DWD", "dwd"),
            (" autobahn ", "autobahn"),
            # A namespace can only ever become one directory name, so even a hostile one
            # cannot escape the cache root.
            ("../../etc", "etc"),
            ("a/b", "a_b"),
            ("", "default"),
            ("///", "default"),
        ],
    )
    def test_namespaces_cannot_escape_the_cache_root(
        self, cache: FileCache, cache_root: Path, namespace: str, directory: str
    ) -> None:
        path = cache.path_for(namespace, "k")
        assert path.parent == cache_root / directory
        assert cache_root.resolve() in path.resolve().parents

    def test_keys_are_hashed_so_a_url_is_a_legal_filename(self, cache: FileCache) -> None:
        key = "https://opendata.dwd.de/a/b?c=d&e=f"
        path = cache.path_for("dwd", key)
        assert path.name.endswith(".bin")
        assert "/" not in path.name and "?" not in path.name
        # Same key, same path; different keys, different paths.
        assert cache.path_for("dwd", key) == path
        assert cache.path_for("dwd", key + "x") != path
        assert cache.path_for("osrm", key) != path

    def test_a_write_leaves_no_temporary_files_behind(self, cache: FileCache) -> None:
        cache.set_bytes("dwd", "k", b"payload")
        directory = cache.path_for("dwd", "k").parent
        # The write goes through a temporary in the same directory and an atomic replace, so a
        # reader never sees a half-written 55 MB CSV.
        assert [path.name for path in directory.iterdir() if path.name.endswith(".tmp")] == []

    def test_overwriting_replaces_atomically(self, cache: FileCache) -> None:
        cache.set_bytes("dwd", "k", b"old")
        cache.set_bytes("dwd", "k", b"new")
        assert cache.get_bytes("dwd", "k") == b"new"

    def test_a_non_serialisable_payload_raises(self, cache: FileCache) -> None:
        # A programming error in the adapter, not a runtime condition of the cache — silently
        # dropping it would hide a bug until the next outage.
        with pytest.raises(TypeError):
            cache.set_json("osrm", "k", {"when": object()})

    @pytest.mark.skipif(
        hasattr(os, "getuid") and os.getuid() == 0,
        reason="root ignores directory permissions, so the read-only case cannot be simulated",
    )
    def test_a_read_only_cache_directory_degrades_to_a_miss(self, tmp_path: Path) -> None:
        # A hardened container or a CI sandbox mounts data/ read-only. That is a normal
        # condition, not an error: the chain simply loses its middle link for this process.
        root = tmp_path / "readonly"
        root.mkdir()
        root.chmod(0o500)
        try:
            cache = FileCache(root)
            with structlog.testing.capture_logs() as logs:
                assert cache.set_bytes("dwd", "k", b"payload") is False
                assert cache.set_json("dwd", "k", {"a": 1}) is False
                # Writes fail, reads miss, nothing raises.
                assert cache.get_bytes("dwd", "k") is None
                assert cache.get_json("dwd", "k") is None
                assert cache.clear("dwd") == 0
        finally:
            root.chmod(0o700)

        warnings = [entry for entry in logs if entry.get("log_level") == "warning"]
        assert warnings, "a silently unusable cache is worse than a noisy one"
        assert "not writable" in warnings[0]["event"]
        # Logged once per instance: a read-only directory would otherwise flood the log with
        # one line per request for the life of the process.
        assert len(warnings) == 1

    @pytest.mark.skipif(
        hasattr(os, "getuid") and os.getuid() == 0,
        reason="root ignores directory permissions, so the read-only case cannot be simulated",
    )
    async def test_a_read_only_cache_still_lets_a_provider_answer(
        self,
        http: AutoTwinHTTPClient,
        tmp_path: Path,
        autobahn_url: str,
        roadworks_payload: dict[str, Any],
    ) -> None:
        root = tmp_path / "readonly"
        root.mkdir()
        root.chmod(0o500)
        try:
            provider = AutobahnTrafficProvider(
                http_client=http,
                cache=FileCache(root),
                data_mode=DataMode.cached,
                roads=("A5",),
                services=("roadworks",),
            )
            with respx.mock(assert_all_called=False) as router:
                route = router.get(autobahn_url).mock(
                    return_value=httpx.Response(200, json=roadworks_payload)
                )
                live = await provider.fetch_events()
                route.mock(return_value=httpx.Response(503))
                degraded = await provider.fetch_events()
        finally:
            root.chmod(0o700)

        # The live answer is unaffected by an unwritable cache …
        assert live.mode is ProviderMode.live
        assert len(live.data) == 12
        # … and the outage falls straight through to the fixture, because nothing was stored.
        assert degraded.mode is ProviderMode.fixture
        assert len(degraded.data) == 12
