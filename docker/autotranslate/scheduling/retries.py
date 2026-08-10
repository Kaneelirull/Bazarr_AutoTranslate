from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class RetryQueueProcessor:
    """Work-conserving durable retry admission."""

    state: Any
    completed_cycle: Callable[[], int]
    batch_size: int
    per_series_limit: int

    def process(self, examine: Callable[[dict], str]) -> dict[str, int]:
        metrics = {"examined": 0, "reconciled": 0, "translationRequired": 0, "submitted": 0, "noProgress": 0}
        submission_budget = max(1, int(self.batch_size))
        seen: set[int] = set()
        while metrics["submitted"] < submission_budget:
            claimed = self.state.claim_due_retry_plans(
                self.completed_cycle(), limit=1,
                per_series_limit=max(1, int(self.per_series_limit)),
            )
            if not claimed:
                break
            plan = claimed[0]
            if plan["id"] in seen:
                break
            seen.add(plan["id"])
            metrics["examined"] += 1
            self.state.record_retry_admission(plan["id"], self.completed_cycle(), "examined")
            classification = examine(plan)
            if classification == "submitted":
                metrics["translationRequired"] += 1
                metrics["submitted"] += 1
                self.state.record_retry_admission(plan["id"], self.completed_cycle(), "submitted")
            elif classification == "reconciled":
                metrics["reconciled"] += 1
                self.state.record_retry_admission(plan["id"], self.completed_cycle(), "reconciled")
            else:
                metrics["noProgress"] += 1
                self.state.record_retry_admission(plan["id"], self.completed_cycle(), "no_progress", classification)
                self.state.reschedule_retry_no_progress(
                    plan["id"], completed_cycle=self.completed_cycle(),
                    deferral_class=classification, reason=classification,
                )
        return metrics
