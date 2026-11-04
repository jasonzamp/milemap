"""Remove secret values from any text before it is logged or raised."""

from __future__ import annotations

from collections.abc import Iterable


def redact(text: str, secrets: Iterable[str | None]) -> str:
    for s in secrets:
        if s and len(s) >= 4:
            text = text.replace(s, "[REDACTED]")
    return text
