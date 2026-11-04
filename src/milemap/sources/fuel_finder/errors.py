from __future__ import annotations


class FuelFinderError(RuntimeError):
    """Base class for Fuel Finder errors. Messages never contain secrets."""


class GeoBlockedError(FuelFinderError):
    """HTTP 403: Fuel Finder only answers UK IP addresses (check VPN / host location)."""


class AuthError(FuelFinderError):
    """Token generation or refresh failed."""


class ApiError(FuelFinderError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status
