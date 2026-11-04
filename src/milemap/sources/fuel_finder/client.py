"""Polite, paged Fuel Finder API client.

- One request at a time with a minimum gap (documented limit is about 100 requests/minute).
- Retries with exponential backoff on timeouts, 429 and 5xx (the API had severe performance
  problems at launch).
- 403 raises GeoBlockedError (the API only answers UK IP addresses).
- 401 forces one token renewal, then fails.
- Pagination: batch-number 1, 2, 3... until an empty batch or 404.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from milemap import __version__
from milemap.config import FuelFinderSettings
from milemap.redact import redact
from milemap.sources.fuel_finder.auth import TokenManager
from milemap.sources.fuel_finder.errors import ApiError, GeoBlockedError

log = logging.getLogger(__name__)

PRICES_PATH = "/api/v1/pfs/fuel-prices"
STATIONS_PATH = "/api/v1/pfs"
RETRY_STATUSES = {429, 500, 502, 503, 504}
USER_AGENT = f"MileMap/{__version__} (+https://github.com/jasonzamp/milemap)"


@dataclass
class Batch:
    number: int
    status: int
    records: list[Any]
    envelope: dict[str, Any]  # non-record parts of the response (e.g. success/message)
    path: str
    params: dict[str, Any]
    fetched_at: datetime
    elapsed_s: float
    attempts: int


@dataclass
class Response:
    status: int
    body: Any
    fetched_at: datetime
    elapsed_s: float
    attempts: int
    error: str | None = None


@dataclass
class PaginationResult:
    batches_fetched: int = 0
    stopped_because: str = ""  # "empty" | "404" | "cap"
    errors: list[str] = field(default_factory=list)


def split_records(body: Any) -> tuple[list[Any], dict[str, Any]]:
    """Return (records, envelope) for a list response or a dict wrapping a list."""
    if isinstance(body, list):
        return body, {}
    if isinstance(body, dict):
        for key in ("data", "results", "items", "records"):
            if isinstance(body.get(key), list):
                return body[key], {k: v for k, v in body.items() if k != key}
        lists = [k for k, v in body.items() if isinstance(v, list)]
        if len(lists) == 1:
            k = lists[0]
            return body[k], {kk: v for kk, v in body.items() if kk != k}
        return [], body
    return [], {"_unexpected_body_type": type(body).__name__}


class FuelFinderClient:
    def __init__(
        self,
        settings: FuelFinderSettings,
        *,
        min_interval_s: float = 0.75,
        max_retries: int = 5,
        backoff_base_s: float = 2.0,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._s = settings
        self._http = http or httpx.Client(
            timeout=httpx.Timeout(90.0, connect=15.0),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        self._sleep = sleep
        self._clock = clock
        self._min_interval = min_interval_s
        self._max_retries = max_retries
        self._backoff = backoff_base_s
        self._last_request = -1e9
        self.tokens = TokenManager(settings, self._http, clock=clock)
        self.request_count = 0
        self.retry_count = 0

    def _throttle(self) -> None:
        wait = self._min_interval - (self._clock() - self._last_request)
        if wait > 0:
            self._sleep(wait)
        self._last_request = self._clock()

    def _secrets(self) -> tuple[str | None, ...]:
        return (*self._s.secrets(), self.tokens._access, self.tokens._refresh)

    def get(self, path: str, params: dict[str, Any] | None = None) -> Response:
        """GET with retries. Returns 4xx (other than 401/403/429) as a Response, not an error."""
        params = dict(params or {})
        reauthed = False
        last_problem = ""
        for attempt in range(1, self._max_retries + 2):
            self._throttle()
            started = self._clock()
            fetched_at = datetime.now(UTC)
            try:
                token = self.tokens.token()
                r = self._http.get(
                    self._s.base_url + path,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.TransportError as e:
                self.request_count += 1
                last_problem = f"{type(e).__name__}"
                self._retry_wait(attempt, None, last_problem, path, params)
                continue
            self.request_count += 1
            elapsed = self._clock() - started
            if r.status_code == 403:
                raise GeoBlockedError(
                    "HTTP 403 from Fuel Finder. The API only answers UK IP addresses: "
                    "check that no VPN is active."
                )
            if r.status_code == 401 and not reauthed:
                reauthed = True
                self.tokens.invalidate()
                continue
            if r.status_code in RETRY_STATUSES:
                last_problem = f"HTTP {r.status_code}"
                self._retry_wait(attempt, r.headers.get("Retry-After"), last_problem, path, params)
                continue
            if r.status_code >= 400:
                return Response(
                    r.status_code, None, fetched_at, elapsed, attempt,
                    error=redact(r.text[:300], self._secrets()),
                )
            try:
                body = r.json()
            except ValueError:
                raise ApiError(f"Non-JSON response from {path}", r.status_code) from None
            return Response(r.status_code, body, fetched_at, elapsed, attempt)
        raise ApiError(f"Gave up on {path} {params} after retries (last: {last_problem})")

    def _retry_wait(self, attempt, retry_after, problem, path, params) -> None:
        if attempt > self._max_retries:
            return
        self.retry_count += 1
        try:
            wait = float(retry_after) if retry_after else self._backoff * 2 ** (attempt - 1)
        except ValueError:
            wait = self._backoff * 2 ** (attempt - 1)
        wait = min(wait, 120.0)
        log.warning("%s on %s %s; retry %d in %.0fs", problem, path, params, attempt, wait)
        self._sleep(wait)

    def iter_batches(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        max_batches: int | None = None,
        result: PaginationResult | None = None,
    ) -> Iterator[Batch]:
        """Yield batches 1, 2, 3... until an empty batch, a 404, or max_batches."""
        result = result if result is not None else PaginationResult()
        n = 1
        while True:
            if max_batches is not None and n > max_batches:
                result.stopped_because = "cap"
                return
            p = {**(params or {}), "batch-number": n}
            resp = self.get(path, p)
            if resp.status == 404:
                result.stopped_because = "404"
                return
            if resp.status >= 400:
                raise ApiError(f"HTTP {resp.status} on {path} {p}: {resp.error}", resp.status)
            records, envelope = split_records(resp.body)
            result.batches_fetched += 1
            yield Batch(n, resp.status, records, envelope, path, p, resp.fetched_at,
                        resp.elapsed_s, resp.attempts)
            if not records:
                result.stopped_because = "empty"
                return
            n += 1
