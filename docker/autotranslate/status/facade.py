from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


PRIVATE_KEYS = {
    "subtitleline", "sourcepath", "targetpath", "artifactpath", "reportpath",
    "apikey", "requestbody", "responsebody", "targettext", "sourcetext",
    "prompt", "headers", "authorization",
}

_WINDOWS_PATH = re.compile(r"^[a-zA-Z]:[\\/]")


def _private_key(value: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(value).casefold())
    return (
        normalized in PRIVATE_KEYS
        or normalized.endswith("path")
        or normalized.endswith("apikey")
    )


def sanitize_public(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): sanitize_public(item)
            for key, item in value.items()
            if not _private_key(key)
        }
    if isinstance(value, list):
        return [sanitize_public(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_public(item) for item in value]
    if isinstance(value, str):
        if value.startswith(("/", "\\\\")) or _WINDOWS_PATH.match(value):
            return "[redacted path]"
        return value[:500]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"[{type(value).__name__}]"


@dataclass
class StatusFacade:
    tracker: Any

    def set_phase(self, phase: str, *, next_cycle_at: float | None = None) -> None:
        self.tracker.set_phase(phase, next_cycle_at=next_cycle_at)

    def set_diagnostics(self, **values: Any) -> None:
        self.tracker.set_diagnostics(**sanitize_public(values))
