"""OAuth 2.0 client-credentials token handling for Fuel Finder.

Docs rule: reuse access and refresh tokens until they are close to expiry; never request a
token before every call. Tokens are kept in memory only and never logged.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from milemap.config import FuelFinderSettings
from milemap.redact import redact
from milemap.sources.fuel_finder.errors import AuthError, GeoBlockedError

log = logging.getLogger(__name__)

GENERATE_PATH = "/api/v1/oauth/generate_access_token"
REFRESH_PATH = "/api/v1/oauth/regenerate_access_token"


class TokenManager:
    EARLY_REFRESH_S = 120  # renew this long before expiry
    DEFAULT_LIFETIME_S = 3600

    def __init__(
        self,
        settings: FuelFinderSettings,
        http: httpx.Client,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._s = settings
        self._http = http
        self._clock = clock
        self._access: str | None = None
        self._refresh: str | None = None
        self._expires_at = 0.0
        self.generate_count = 0
        self.refresh_count = 0
        self.response_shape: dict[str, Any] | None = None  # field names only, never values

    def _secrets(self) -> tuple[str | None, ...]:
        return (*self._s.secrets(), self._access, self._refresh)

    def token(self) -> str:
        if self._access and self._clock() < self._expires_at - self.EARLY_REFRESH_S:
            return self._access
        if self._refresh:
            try:
                tok = self._request(
                    REFRESH_PATH, {"client_id": self._s.client_id, "refresh_token": self._refresh}
                )
                self.refresh_count += 1
                log.info("Access token refreshed")
                return tok
            except AuthError as e:
                log.info("Token refresh failed (%s); generating a new token", e)
        tok = self._request(
            GENERATE_PATH, {"client_id": self._s.client_id, "client_secret": self._s.client_secret}
        )
        self.generate_count += 1
        log.info("Access token generated")
        return tok

    def invalidate(self) -> None:
        """Forget the access token (e.g. after a 401); the refresh token is kept."""
        self._access = None
        self._expires_at = 0.0

    def _request(self, path: str, body: dict[str, str]) -> str:
        try:
            r = self._http.post(self._s.base_url + path, json=body, timeout=30)
        except httpx.HTTPError as e:
            raise AuthError(f"Token request to {path} failed: {type(e).__name__}") from None
        if r.status_code == 403:
            raise GeoBlockedError(
                "HTTP 403 from the Fuel Finder token endpoint. The API only answers UK IP "
                "addresses: check that no VPN is active."
            )
        if r.status_code >= 400:
            snippet = redact(r.text[:300], self._secrets())
            raise AuthError(f"Token request to {path} failed: HTTP {r.status_code}: {snippet}")
        try:
            data = r.json()
        except ValueError:
            raise AuthError(f"Token response from {path} was not JSON") from None
        payload = data.get("data", data) if isinstance(data, dict) else {}
        if not isinstance(payload, dict) or "access_token" not in payload:
            keys = sorted(data) if isinstance(data, dict) else type(data).__name__
            raise AuthError(f"Token response from {path} had no access_token (keys: {keys})")

        self.response_shape = {
            "endpoint": path,
            "top_level_keys": sorted(data.keys()),
            "token_object_keys": sorted(payload.keys()),
            "expires_in": payload.get("expires_in"),
            "refresh_token_expires_in": payload.get("refresh_token_expires_in"),
            "token_type": payload.get("token_type"),
        }
        self._access = str(payload["access_token"])
        self._refresh = payload.get("refresh_token") or self._refresh
        try:
            lifetime = float(payload.get("expires_in") or self.DEFAULT_LIFETIME_S)
        except (TypeError, ValueError):
            lifetime = self.DEFAULT_LIFETIME_S
        self._expires_at = self._clock() + lifetime
        return self._access
