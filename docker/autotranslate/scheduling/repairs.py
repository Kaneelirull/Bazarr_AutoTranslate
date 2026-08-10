from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable

from .capacity import CapacityCoordinator


@dataclass
class _ActiveRepair:
    job_id: int
    scope_id: str
    future: Future


@dataclass(frozen=True)
class RepairDrainResult:
    completed: int
    failed: int
    persisted: int
    results: tuple[Any, ...] = ()


class RepairCoordinator:
    """Persist-before-submit repair scheduling with bounded shutdown."""

    def __init__(
        self,
        state: Any,
        capacity: CapacityCoordinator,
        *,
        workers: int,
        shutdown_grace_seconds: int = 30,
    ):
        self.state = state
        self.capacity = capacity
        self.shutdown_grace_seconds = max(1, int(shutdown_grace_seconds))
        self._executor = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="repair-worker")
        self._lock = threading.RLock()
        self._active: dict[int, _ActiveRepair] = {}
        self._accepting = True

    def submit(
        self,
        scope_id: str,
        dedupe_key: str,
        metadata: dict,
        work: Callable[[], Any],
    ) -> int | None:
        with self._lock:
            if not self._accepting:
                return None
            job_id = self.state.enqueue_repair_job(
                dedupe_key=dedupe_key,
                target_language=metadata.get("target_language") or "",
                item_type=metadata.get("item_type"),
                item_id=metadata.get("item_id"),
                source_path=metadata.get("source_path"),
                target_path=metadata.get("target_path"),
                source_hash=metadata.get("source_hash"),
                target_hash=metadata.get("target_hash"),
                cue_indexes=metadata.get("cue_indexes") or (),
                payload={key: value for key, value in metadata.items() if "path" not in key.lower()},
            )
            if job_id in self._active:
                return None
            future = self._executor.submit(self._run, job_id, work)
            self._active[job_id] = _ActiveRepair(job_id, str(scope_id), future)
            return job_id

    def _run(self, job_id: int, work: Callable[[], Any]) -> Any:
        token = self.capacity.acquire("repair")
        if token is None:
            self.state.transition_repair_job(
                job_id,
                "persisted_for_restart",
                shutdown_classification="cancelled_before_start",
            )
            return None
        owner = f"repair:{job_id}:{uuid.uuid4().hex}"
        self.state.transition_repair_job(
            job_id,
            "active",
            lease_owner=owner,
            lease_expires_at=time.time() + self.shutdown_grace_seconds,
        )
        try:
            result = work()
            self.state.transition_repair_job(
                job_id, "completed", expected_states=("active",)
            )
            return result
        except Exception as exc:
            self.state.transition_repair_job(
                job_id, "failed", error_code=type(exc).__name__,
                expected_states=("active",),
            )
            raise
        finally:
            self.capacity.release(token)

    def drain(self, scope_id: str) -> RepairDrainResult:
        scope = str(scope_id)
        with self._lock:
            entries = [
                entry for entry in self._active.values()
                if entry.scope_id == scope
            ]
        if not entries:
            return RepairDrainResult(0, 0, 0)
        wait([entry.future for entry in entries])
        completed = failed = persisted = 0
        results: list[Any] = []
        for entry in entries:
            try:
                result = entry.future.result()
                results.append(result)
                row = next((
                    job for job in self.state.repair_jobs_for_restart()
                    if job["id"] == entry.job_id
                ), None)
                if row is not None:
                    persisted += 1
                else:
                    completed += 1
            except Exception:
                failed += 1
            finally:
                with self._lock:
                    self._active.pop(entry.job_id, None)
        return RepairDrainResult(
            completed=completed,
            failed=failed,
            persisted=persisted,
            results=tuple(results),
        )

    def shutdown(self) -> int:
        with self._lock:
            self._accepting = False
            entries = list(self._active.values())
            futures = [entry.future for entry in entries]
        self.capacity.stop_accepting()
        _done, pending = wait(futures, timeout=self.shutdown_grace_seconds)
        for entry in entries:
            if entry.future in pending:
                self.state.transition_repair_job(
                    entry.job_id,
                    "persisted_for_restart",
                    shutdown_classification="interrupted",
                    expected_states=("queued", "active"),
                )
        self._executor.shutdown(wait=False, cancel_futures=True)
        return len(pending)
