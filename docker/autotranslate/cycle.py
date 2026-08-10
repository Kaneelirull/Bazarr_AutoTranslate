from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .models import CycleResult


@dataclass
class CycleRunner:
    run_legacy_cycle: Callable[[int], bool]

    def run(self, cycle_number: int) -> CycleResult:
        healthy = bool(self.run_legacy_cycle(cycle_number))
        return CycleResult(cycle_number=cycle_number, healthy=healthy, degraded=not healthy)
