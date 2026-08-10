from __future__ import annotations

from dataclasses import dataclass
from typing import Any


PRIVATE_KEYS = {
    "subtitleLine", "sourcePath", "targetPath", "artifactPath", "reportPath",
    "apiKey", "requestBody", "responseBody", "targetText",
}


def sanitize_public(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): sanitize_public(item)
            for key, item in value.items()
            if str(key) not in PRIVATE_KEYS
        }
    if isinstance(value, list):
        return [sanitize_public(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_public(item) for item in value]
    return value


@dataclass
class StatusFacade:
    tracker: Any

    def set_phase(self, phase: str, *, next_cycle_at: float | None = None) -> None:
        self.tracker.set_phase(phase, next_cycle_at=next_cycle_at)

    def set_diagnostics(self, **values: Any) -> None:
        self.tracker.set_diagnostics(**sanitize_public(values))
