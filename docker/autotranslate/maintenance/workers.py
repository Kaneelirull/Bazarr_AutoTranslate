from __future__ import annotations

import hashlib
import multiprocessing
import os
import subprocess
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator


STABLE_CACHE_ACTIONS = frozenset({
    "valid", "valid-warning", "reported", "dry-run", "formatted", "repaired",
})
_DETECTOR = None


def _worker_detector():
    global _DETECTOR
    if _DETECTOR is None:
        from ..subtitles.foundation import build_detector
        _DETECTOR = build_detector()
    return _DETECTOR


@dataclass(frozen=True)
class MaintenanceFileStat:
    path: str
    size: int
    modified_ns: int

    @classmethod
    def capture(cls, path: str | Path | None) -> "MaintenanceFileStat | None":
        if not path:
            return None
        candidate = Path(path)
        try:
            stat = candidate.stat()
        except OSError:
            return None
        return cls(str(candidate), int(stat.st_size), int(stat.st_mtime_ns))

    def to_dict(self) -> dict:
        return {
            "path": os.path.normcase(os.path.abspath(self.path)),
            "size": self.size,
            "modifiedNs": self.modified_ns,
        }


@dataclass(frozen=True)
class ValidationTask:
    sequence: int
    target_path: str
    target_language: str
    operation: str = "validate"
    source_path: str | None = None
    source_aligned: bool = False
    video_path: str | None = None
    receipt_path: str | None = None
    validation_kwargs: dict = field(default_factory=dict)
    completeness_kwargs: dict = field(default_factory=dict)
    undersized_enabled: bool = True
    ffprobe_timeout: int = 15


@dataclass
class ValidationResult:
    sequence: int
    target_path: str
    target_language: str
    target_stat: MaintenanceFileStat | None
    source_stat: MaintenanceFileStat | None
    video_stat: MaintenanceFileStat | None
    receipt_stat: MaintenanceFileStat | None
    target_hash: str | None = None
    source_hash: str | None = None
    report: object | None = None
    completeness: object | None = None
    validation_mode: str = "target-only"
    worker_pid: int = 0
    cue_count: int | None = None
    error: str | None = None

    def dependency_fingerprint(self) -> dict:
        return {
            "source": self.source_stat.to_dict() if self.source_stat else None,
            "video": self.video_stat.to_dict() if self.video_stat else None,
            "receipt": self.receipt_stat.to_dict() if self.receipt_stat else None,
        }


def _file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe_duration(path: str | None, timeout: int) -> float | None:
    if not path:
        return None
    try:
        completed = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", path,
            ],
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout)),
            check=False,
        )
        duration = float(completed.stdout.strip()) if completed.returncode == 0 else 0.0
        return duration if duration > 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def analyze_validation_task(task: ValidationTask) -> ValidationResult:
    """Analyze one subtitle without importing production runtime or mutating state."""
    target_stat = MaintenanceFileStat.capture(task.target_path)
    source_stat = MaintenanceFileStat.capture(task.source_path)
    video_stat = MaintenanceFileStat.capture(task.video_path)
    receipt_stat = MaintenanceFileStat.capture(task.receipt_path)
    result = ValidationResult(
        sequence=task.sequence,
        target_path=task.target_path,
        target_language=task.target_language,
        target_stat=target_stat,
        source_stat=source_stat,
        video_stat=video_stat,
        receipt_stat=receipt_stat,
        validation_mode="source-aware" if task.source_aligned else "target-only",
        worker_pid=os.getpid(),
    )
    if target_stat is None:
        result.error = "target_unavailable"
        return result
    try:
        if task.operation == "count_cues":
            from ..subtitles.foundation import parse_srt_cues, read_text_best_effort
            raw = read_text_best_effort(Path(task.target_path))
            if raw is None:
                result.error = "target_unreadable"
                return result
            cues, errors = parse_srt_cues(raw)
            result.cue_count = len(cues) if not errors else None
            if errors:
                result.error = "target_structure"
            return result
        if task.operation == "structure":
            from ..subtitles.foundation import (
                completeness_issue,
                evaluate_subtitle_completeness,
                validate_srt_structure,
            )
            result.target_hash = _file_sha256(task.target_path)
            result.report = validate_srt_structure(Path(task.target_path))
            duration = _probe_duration(task.video_path, task.ffprobe_timeout)
            if task.undersized_enabled and duration is not None:
                result.completeness = evaluate_subtitle_completeness(
                    task.target_path, duration, **task.completeness_kwargs,
                )
                issue = completeness_issue(result.completeness)
                if issue is not None:
                    result.report.issues.append(issue)
            return result
        from ..subtitles.foundation import (
            completeness_issue,
            evaluate_subtitle_completeness,
            target_language_for_code,
            validate_subtitle_pair,
        )
        from ..subtitles.library import validate_subtitle_without_source

        result.target_hash = _file_sha256(task.target_path)
        if task.source_path and source_stat is not None:
            result.source_hash = _file_sha256(task.source_path)
        detector = _worker_detector()
        language = target_language_for_code(task.target_language)
        if language is None:
            result.error = "unsupported_language"
            return result
        if task.source_aligned and task.source_path and source_stat is not None:
            result.report = validate_subtitle_pair(
                Path(task.source_path), Path(task.target_path), detector, language,
                target_lang=task.target_language, **task.validation_kwargs,
            )
        else:
            result.report = validate_subtitle_without_source(
                Path(task.target_path), detector, language,
                target_lang=task.target_language, **task.validation_kwargs,
            )
        duration = _probe_duration(task.video_path, task.ffprobe_timeout)
        if task.undersized_enabled and duration is not None:
            result.completeness = evaluate_subtitle_completeness(
                task.target_path, duration, **task.completeness_kwargs,
            )
            issue = completeness_issue(result.completeness)
            if issue is not None:
                result.report.issues.append(issue)
        return result
    except Exception as exc:
        result.error = type(exc).__name__
        return result


class MaintenanceWorkerPool:
    """Bounded, ordered process execution for non-AI maintenance analysis."""

    def __init__(
        self,
        workers: int,
        *,
        executor_factory: Callable[..., ProcessPoolExecutor] = ProcessPoolExecutor,
    ) -> None:
        self.workers = max(1, min(32, int(workers)))
        self.max_in_flight = self.workers * 2
        self._executor_factory = executor_factory
        self._executor: ProcessPoolExecutor | None = None

    def _executor_for_run(self) -> ProcessPoolExecutor:
        if self._executor is None:
            self._executor = self._executor_factory(
                max_workers=self.workers,
                mp_context=multiprocessing.get_context("spawn"),
            )
        return self._executor

    def map_ordered(
        self,
        tasks: Iterable[ValidationTask],
        *,
        stop_requested: Callable[[], bool] = lambda: False,
    ) -> Iterator[ValidationResult]:
        executor = self._executor_for_run()
        iterator = iter(tasks)
        pending: dict[Future, ValidationTask] = {}
        buffered: dict[int, ValidationResult] = {}
        submission_order: deque[int] = deque()
        exhausted = False

        def fill() -> None:
            nonlocal exhausted
            while (
                not exhausted
                and len(pending) + len(buffered) < self.max_in_flight
                and not stop_requested()
            ):
                try:
                    task = next(iterator)
                except StopIteration:
                    exhausted = True
                    return
                submission_order.append(task.sequence)
                pending[executor.submit(analyze_validation_task, task)] = task

        fill()
        while pending:
            done, _ = wait(tuple(pending), timeout=0.25, return_when=FIRST_COMPLETED)
            if not done:
                if stop_requested():
                    break
                continue
            for future in done:
                task = pending.pop(future)
                try:
                    buffered[task.sequence] = future.result()
                except Exception as exc:
                    buffered[task.sequence] = ValidationResult(
                        sequence=task.sequence,
                        target_path=task.target_path,
                        target_language=task.target_language,
                        target_stat=None,
                        source_stat=None,
                        video_stat=None,
                        receipt_stat=None,
                        error=type(exc).__name__,
                    )
            while submission_order and submission_order[0] in buffered:
                yield buffered.pop(submission_order.popleft())
            fill()
        if stop_requested():
            for future in pending:
                future.cancel()

    def shutdown(
        self, *, wait_for_workers: bool = True, grace_seconds: float = 30.0,
    ) -> int:
        """Stop accepting work and bound child-process drainage."""
        executor = self._executor
        self._executor = None
        if executor is None:
            return 0
        processes = tuple((getattr(executor, "_processes", None) or {}).values())
        executor.shutdown(wait=False, cancel_futures=True)
        if not wait_for_workers:
            return 0
        deadline = time.monotonic() + max(0.0, float(grace_seconds))
        for process in processes:
            process.join(timeout=max(0.0, deadline - time.monotonic()))
        alive = [process for process in processes if process.is_alive()]
        for process in alive:
            process.terminate()
        manager = getattr(executor, "_executor_manager_thread", None)
        if manager is not None and manager.is_alive():
            manager.join(timeout=1.0)
        return len(alive)


def cache_entry_matches(
    entry: dict | None,
    *,
    target_stat: MaintenanceFileStat | None,
    dependency_fingerprint: dict,
    validator_version: str,
    config_fingerprint: str,
) -> bool:
    if entry is None or target_stat is None:
        return False
    return bool(
        entry.get("actionResult") in STABLE_CACHE_ACTIONS
        and entry.get("targetSize") == target_stat.size
        and entry.get("targetModifiedNs") == target_stat.modified_ns
        and entry.get("dependencyFingerprint") == dependency_fingerprint
        and entry.get("validatorVersion") == validator_version
        and entry.get("configFingerprint") == config_fingerprint
    )


__all__ = [
    "MaintenanceFileStat", "MaintenanceWorkerPool", "ValidationResult",
    "ValidationTask", "analyze_validation_task", "cache_entry_matches",
]
