from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class CircuitBreakerManager:
    state: Any
    config_fingerprint: str

    def permission(self, *, series_id: int, series_title: str, claim: bool, owner: str | None = None) -> dict:
        return self.state.circuit_permission(
            series_key=f"sonarr:{int(series_id)}",
            series_title=series_title,
            config_fingerprint=self.config_fingerprint,
            claim=claim,
            trial_owner=owner,
        )

    def release_unbound(self, series_id: int, owner: str, reason: str) -> bool:
        return self.state.release_circuit_trial(f"sonarr:{int(series_id)}", owner, reason)
