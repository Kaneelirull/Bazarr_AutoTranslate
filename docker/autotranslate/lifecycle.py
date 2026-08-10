from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .models import LifecyclePhase, MaintenanceResult


@dataclass
class LifecycleController:
    """Deterministic cycle -> maintenance -> cooldown orchestration."""

    run_cycle: Callable[[int], bool]
    advance_completed_cycle: Callable[[], int]
    run_maintenance: Callable[[], MaintenanceResult]
    set_phase: Callable[..., None]
    refresh_diagnostics: Callable[[], None]
    sleep_interruptibly: Callable[[int], bool]
    check_interval: int

    def run_iteration(self, cycle_number: int) -> tuple[bool, MaintenanceResult]:
        self.set_phase(LifecyclePhase.CYCLE_WORK.value)
        healthy = self.run_cycle(cycle_number)
        if healthy:
            self.advance_completed_cycle()
        self.refresh_diagnostics()
        self.set_phase(LifecyclePhase.POST_CYCLE_MAINTENANCE.value)
        maintenance = self.run_maintenance()
        self.refresh_diagnostics()
        self.set_phase(LifecyclePhase.COOLDOWN.value)
        self.sleep_interruptibly(self.check_interval)
        return healthy, maintenance
