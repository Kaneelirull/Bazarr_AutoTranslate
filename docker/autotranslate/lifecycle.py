from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from .models import LifecyclePhase, MaintenanceResult


class ShutdownController:
    """One process-wide shutdown deadline shared by every lifecycle owner."""

    def __init__(
        self,
        grace_seconds: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._grace_seconds = max(0.0, float(grace_seconds))
        self._monotonic = monotonic
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._deadline: float | None = None

    def request(self) -> bool:
        """Request shutdown once and preserve the original deadline."""
        with self._lock:
            first_request = self._deadline is None
            if first_request:
                self._deadline = self._monotonic() + self._grace_seconds
            self._event.set()
            return first_request

    def is_requested(self) -> bool:
        return self._event.is_set()

    def remaining(self) -> float:
        with self._lock:
            if self._deadline is None:
                return self._grace_seconds
            return max(0.0, self._deadline - self._monotonic())


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
    shutdown_requested: Callable[[], bool] = lambda: False

    def run_iteration(self, cycle_number: int) -> tuple[bool, MaintenanceResult]:
        self.set_phase(LifecyclePhase.CYCLE_WORK.value)
        healthy = self.run_cycle(cycle_number)
        if healthy:
            self.advance_completed_cycle()
        self.refresh_diagnostics()
        if self.shutdown_requested():
            return healthy, MaintenanceResult(healthy=True)
        self.set_phase(LifecyclePhase.POST_CYCLE_MAINTENANCE.value)
        maintenance = self.run_maintenance()
        self.refresh_diagnostics()
        if self.shutdown_requested():
            return healthy, maintenance
        self.set_phase(LifecyclePhase.COOLDOWN.value)
        self.sleep_interruptibly(self.check_interval)
        return healthy, maintenance

    def run(
        self,
        first_cycle: int,
        *,
        on_iteration: Callable[[int, bool, MaintenanceResult], None] | None = None,
    ) -> int:
        """Run iterations until shutdown; every phase owns its child work."""
        cycle = max(1, int(first_cycle))
        while not self.shutdown_requested():
            healthy, maintenance = self.run_iteration(cycle)
            if on_iteration is not None:
                on_iteration(cycle, healthy, maintenance)
            cycle += 1
        self.set_phase(LifecyclePhase.SHUTDOWN.value)
        return cycle
