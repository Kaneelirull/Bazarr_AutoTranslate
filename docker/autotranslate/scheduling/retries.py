from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class RetryQueueProcessor:
    """Work-conserving retry refill owned by the scheduling layer."""

    batch_size: int
    run_batch: Callable[[dict, int, set[int], dict[str, int]], tuple[int, int]]
    shutdown_requested: Callable[[], bool]
    emit: Callable[[str], None] = print

    def process(
        self,
        stats: dict,
        *,
        submission_budget: int | None = None,
        examined_plan_ids: set[int] | None = None,
        series_admissions: dict[str, int] | None = None,
    ) -> None:
        remaining = max(
            0,
            int(self.batch_size if submission_budget is None else submission_budget),
        )
        examined = examined_plan_ids if examined_plan_ids is not None else set()
        admissions = series_admissions if series_admissions is not None else {}
        while remaining > 0 and not self.shutdown_requested():
            examined_before = len(examined)
            submissions, plans = self.run_batch(
                stats, remaining, examined, admissions
            )
            remaining = max(0, remaining - submissions)
            if remaining <= 0 or plans == 0 or len(examined) <= examined_before:
                break
            self.emit(
                f"[RETRY] Refilling {remaining} translation slot(s) after "
                "reconciliation/no-progress outcomes"
            )
