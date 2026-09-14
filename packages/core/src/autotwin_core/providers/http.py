"""The shared async HTTP client every provider adapter fetches through.

Adapters talk to five very different German endpoints — a 20 MB CSV from the
Bundesnetzagentur, DWD's open-data file server, the Autobahn GmbH JSON API, OSRM and
Nominatim. They should not each re-implement timeouts, retries, User-Agent policy and error
translation, because each of those has exactly one right answer for this project and four
subtly wrong ones.

**Error translation** is the point of the module. ``httpx`` raises transport exceptions and
returns status codes; the rest of AutoTwin only knows the five
:class:`~autotwin_core.errors.ProviderError` subclasses of BUILD_SPEC §4, which the API maps
onto HTTP status codes in §7. The mapping is:

==========================  =====================================================
condition                   raised
==========================  =====================================================
timeout (connect or read)   :class:`~autotwin_core.errors.ProviderTimeout`
connection / DNS / 5xx      :class:`~autotwin_core.errors.ProviderUnavailable`
429                         :class:`~autotwin_core.errors.RateLimited`
401 / 403                   :class:`~autotwin_core.errors.ConfigurationMissing`
other 4xx                   :class:`~autotwin_core.errors.InvalidSourceData`
body is not the JSON asked  :class:`~autotwin_core.errors.InvalidSourceData`
==========================  =====================================================

``401``/``403`` map to *configuration missing* rather than *unavailable* because for this
project's sources they always mean a missing key or a stale registration, never an outage —
and pointing the operator at their configuration is more useful than telling them the
Mobilithek is down. A ``404`` maps to *invalid source data*: AutoTwin only requests endpoints
it has hard-coded, so "that URL is gone" means the source's shape changed.

**Retries are deterministic.** Backoff is ``base * 2 ** attempt`` with no jitter. Jitter is
the right default for a fleet hammering a shared service; here it would only make a test
suite that asserts on elapsed time flaky, and three requests from one portfolio project are
not a thundering herd. Only GET is offered, so every retry is by construction idempotent.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Final, Self

import httpx

from autotwin_core.config import get_settings
from autotwin_core.errors import (
    ConfigurationMissing,
    InvalidSourceData,
    ProviderTimeout,
    ProviderUnavailable,
    RateLimited,
)
from autotwin_core.logging import get_logger

__all__ = ["AutoTwinHTTPClient"]

_logger = get_logger(__name__)

DEFAULT_MAX_ATTEMPTS: Final[int] = 3
"""Three attempts: one transient blip absorbed, without a user waiting through four timeouts."""

DEFAULT_BACKOFF_BASE_S: Final[float] = 0.5
"""First retry after 0.5 s, second after 1.0 s — deterministic, no jitter."""

MAX_HONOURED_RETRY_AFTER_S: Final[float] = 30.0
"""Upper bound on a server-supplied ``Retry-After``; beyond it, giving up is more honest."""

_RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({408, 425, 429, 500, 502, 503, 504})
"""Statuses worth a second attempt. 4xx other than these mean *we* asked wrongly."""


class AutoTwinHTTPClient:
    """A thin, opinionated wrapper over :class:`httpx.AsyncClient`.

    Usable as an async context manager, which is how one-shot ingestion code should use it::

        async with AutoTwinHTTPClient() as client:
            payload = await client.get_json("https://verkehr.autobahn.de/o/autobahn/A5")

    Long-lived callers (the API, whose routing provider serves requests) construct one client
    at startup and close it in ``lifespan``, so that connections are pooled across requests.

    An existing :class:`httpx.AsyncClient` may be injected. The wrapper then does not own it
    and :meth:`aclose` leaves it open — which is what lets tests share one ``respx``-mocked
    client across several adapters.
    """

    def __init__(
        self,
        *,
        base_url: str = "",
        timeout_s: float | None = None,
        user_agent: str | None = None,
        headers: Mapping[str, str] | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
        follow_redirects: bool = True,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Build a client.

        Args:
            base_url: Optional prefix; relative paths are resolved against it.
            timeout_s: Total timeout per attempt. Defaults to ``AUTOTWIN_HTTP_TIMEOUT_S``.
            user_agent: Identifying User-Agent. Defaults to ``AUTOTWIN_HTTP_USER_AGENT``.
                Nominatim's usage policy *requires* a real one, so it is never omitted.
            headers: Extra default headers.
            max_attempts: Total attempts including the first. ``1`` disables retrying.
            backoff_base_s: Delay before the first retry; doubles per further attempt.
            follow_redirects: DWD and the Bundesnetzagentur both redirect their downloads,
                so this defaults to on.
            client: Inject a pre-built client; it is then not closed by :meth:`aclose`.

        Raises:
            ValueError: for a non-positive ``max_attempts`` or a negative backoff.
        """
        if max_attempts < 1:
            msg = f"max_attempts must be at least 1, got {max_attempts!r}"
            raise ValueError(msg)
        if backoff_base_s < 0.0:
            msg = f"backoff_base_s must not be negative, got {backoff_base_s!r}"
            raise ValueError(msg)

        self._max_attempts = max_attempts
        self._backoff_base_s = backoff_base_s
        self._owns_client = client is None

        if client is not None:
            # An injected client brings its own timeout and headers, and reading settings here
            # would drag configuration into tests that deliberately have none.
            self._client = client
            return

        settings = get_settings()
        default_headers: dict[str, str] = {
            "User-Agent": user_agent or settings.http_user_agent,
            "Accept-Encoding": "gzip, deflate",
        }
        if headers:
            default_headers.update(headers)
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_s if timeout_s is not None else settings.http_timeout_s),
            headers=default_headers,
            follow_redirects=follow_redirects,
        )

    async def __aenter__(self) -> Self:
        """Enter the async context; the client is already usable."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the underlying client if this wrapper owns it."""
        await self.aclose()

    async def aclose(self) -> None:
        """Release pooled connections. Idempotent; a no-op for an injected client."""
        if self._owns_client:
            await self._client.aclose()

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """GET ``url``, retrying transient failures, and raise a typed error on a bad status.

        Returns the raw response so that adapters which need ``Last-Modified`` or
        ``Content-Length`` — the Bundesnetzagentur download checks both before spending
        20 MB of bandwidth — can read them.

        Raises:
            ProviderTimeout: every attempt timed out.
            ProviderUnavailable: connection failure, or a 5xx after the final attempt.
            RateLimited: the server answered 429 after the final attempt.
            ConfigurationMissing: the server answered 401 or 403.
            InvalidSourceData: the server answered with another 4xx.
        """
        response = await self._request_with_retries(url, params=params, headers=headers)
        _raise_for_status(response, url)
        return response

    async def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """GET ``url`` and parse the body as JSON.

        Sends ``Accept: application/json`` but does not trust the response's content type:
        several German endpoints serve JSON as ``text/plain``, and one that serves an HTML
        error page with status 200 — which happens — must fail as invalid source data rather
        than crash somewhere in an adapter's parser.

        Raises:
            InvalidSourceData: the body is not valid JSON.
            ProviderTimeout, ProviderUnavailable, RateLimited, ConfigurationMissing: as
                documented on :meth:`get`.
        """
        merged = {"Accept": "application/json", **dict(headers or {})}
        response = await self.get(url, params=params, headers=merged)
        try:
            return json.loads(response.text)
        except json.JSONDecodeError as error:
            preview = response.text[:200].replace("\n", " ")
            msg = f"GET {url} did not return JSON ({error.msg}); body starts: {preview!r}"
            raise InvalidSourceData(msg) from error

    async def get_bytes(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        """GET ``url`` and return the raw body — CSV and ZIP downloads use this."""
        response = await self.get(url, params=params, headers=headers)
        return response.content

    async def get_text(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        encoding: str | None = None,
    ) -> str:
        """GET ``url`` and decode the body as text.

        ``encoding`` overrides the server's declared charset, which matters here: German
        open-data CSV exports are frequently ``windows-1252`` while claiming UTF-8, and the
        difference shows up as a mangled ``Straße`` in every address.
        """
        response = await self.get(url, params=params, headers=headers)
        if encoding is not None:
            return response.content.decode(encoding, errors="replace")
        return response.text

    async def _request_with_retries(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str] | None,
    ) -> httpx.Response:
        """Issue the GET, retrying transient transport errors and retryable statuses."""
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            is_final = attempt == self._max_attempts - 1
            try:
                response = await self._client.get(url, params=params, headers=headers)
            except httpx.TimeoutException as error:
                last_error = ProviderTimeout(f"GET {url} timed out: {error!r}")
            except httpx.HTTPError as error:
                # Covers ConnectError, DNS failures, protocol errors and proxy problems.
                last_error = ProviderUnavailable(f"GET {url} failed: {error!r}")
            else:
                if is_final or response.status_code not in _RETRYABLE_STATUS_CODES:
                    return response
                await self._sleep_before_retry(attempt, url, response=response)
                continue

            if is_final:
                break
            await self._sleep_before_retry(attempt, url, response=None)

        assert last_error is not None, "retry loop exited without a response or an error"
        raise last_error

    async def _sleep_before_retry(
        self,
        attempt: int,
        url: str,
        *,
        response: httpx.Response | None,
    ) -> None:
        """Wait before the next attempt, honouring a sane ``Retry-After`` on a 429."""
        delay = self._backoff_base_s * (2.0**attempt)
        if response is not None and response.status_code == 429:
            delay = max(delay, _retry_after_seconds(response))
        reason = f"HTTP {response.status_code}" if response is not None else "transport error"
        _logger.warning(
            f"GET {url} failed with {reason}; retrying in {delay:.1f}s "
            f"(attempt {attempt + 2}/{self._max_attempts})"
        )
        if delay > 0.0:
            await asyncio.sleep(delay)


def _retry_after_seconds(response: httpx.Response) -> float:
    """Parse ``Retry-After`` as a delta-seconds value, capped and defaulting to zero.

    Only the numeric form is honoured. The HTTP-date form would make the wait depend on clock
    skew between us and the server, which is exactly the kind of non-determinism this module
    avoids elsewhere.
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return 0.0
    try:
        seconds = float(raw.strip())
    except ValueError:
        return 0.0
    return min(max(seconds, 0.0), MAX_HONOURED_RETRY_AFTER_S)


def _raise_for_status(response: httpx.Response, url: str) -> None:
    """Translate a non-2xx status into the typed provider error of BUILD_SPEC §4."""
    status = response.status_code
    if status < 400:
        return
    detail = f"GET {url} returned HTTP {status}"
    if status == 429:
        raise RateLimited(f"{detail} (rate limited)")
    if status in {401, 403}:
        raise ConfigurationMissing(f"{detail}; the source requires credentials or registration")
    if status >= 500:
        raise ProviderUnavailable(detail)
    if status == 408:
        raise ProviderTimeout(detail)
    raise InvalidSourceData(f"{detail}; the endpoint or its contract has changed")
