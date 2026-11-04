"""Settings loaded from environment variables (and the git-ignored .env file).

Secrets are never included in reprs, logs or error messages.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(RuntimeError):
    """A required setting is missing or invalid."""


def _load_env() -> None:
    # Real environment variables win over .env (so cloud secret stores take priority).
    load_dotenv(REPO_ROOT / ".env", override=False)


@dataclass(frozen=True)
class FuelFinderSettings:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    base_url: str

    def secrets(self) -> tuple[str, ...]:
        return (self.client_id, self.client_secret)


def fuel_finder_settings() -> FuelFinderSettings:
    _load_env()
    names = ("FUEL_FINDER_CLIENT_ID", "FUEL_FINDER_CLIENT_SECRET", "FUEL_FINDER_BASE_URL")
    missing = [n for n in names if not os.environ.get(n, "").strip()]
    if missing:
        raise ConfigError(f"Missing environment variables: {', '.join(missing)} (see .env.example)")
    return FuelFinderSettings(
        client_id=os.environ["FUEL_FINDER_CLIENT_ID"].strip(),
        client_secret=os.environ["FUEL_FINDER_CLIENT_SECRET"].strip(),
        base_url=os.environ["FUEL_FINDER_BASE_URL"].strip().rstrip("/"),
    )


def raw_root() -> Path:
    """Root of the raw store. Local path in development."""
    _load_env()
    p = Path(os.environ.get("MILEMAP_RAW_ROOT", "./data/raw"))
    return p if p.is_absolute() else (REPO_ROOT / p).resolve()


def data_dir() -> Path:
    return (REPO_ROOT / "data").resolve()
