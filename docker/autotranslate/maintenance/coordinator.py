from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from ..models import MaintenanceResult


@dataclass(frozen=True)
class MaintenanceOperation:
    name: str
    due: Callable[[], bool]
    run: Callable[[], object]
    mark_completed: Callable[[], None]


class MaintenanceCoordinator:
    """Runs due maintenance serially and leaves failed operations due."""

    def __init__(
        self,
        operations: Iterable[MaintenanceOperation],
        *,
        stop_requested: Callable[[], bool] = lambda: False,
    ):
        self.operations = tuple(operations)
        self.stop_requested = stop_requested

    def run_due(self) -> MaintenanceResult:
        attempted: list[str] = []
        failed: list[str] = []
        metrics: dict[str, object] = {}
        for operation in self.operations:
            if self.stop_requested():
                break
            if not operation.due():
                continue
            attempted.append(operation.name)
            try:
                metrics[operation.name] = operation.run()
            except Exception as exc:
                failed.append(operation.name)
                metrics[f"{operation.name}Failure"] = type(exc).__name__
                continue
            operation.mark_completed()
            if self.stop_requested():
                break
        return MaintenanceResult(
            healthy=not failed,
            attempted=tuple(attempted),
            failed=tuple(failed),
            metrics=metrics,
        )
