import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import Future
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "docker"))

from autotranslate.maintenance.workers import (  # noqa: E402
    MaintenanceWorkerPool,
    ValidationTask,
)
from autotranslate.persistence.state_store import StateStore  # noqa: E402


class _ImmediateExecutor:
    def __init__(self, **_kwargs):
        self.shutdown_calls = []

    def submit(self, function, task):
        future = Future()
        future.set_result(function(task))
        return future

    def shutdown(self, **kwargs):
        self.shutdown_calls.append(kwargs)


class _StuckProcess:
    def __init__(self):
        self.terminated = False

    def join(self, timeout=None):
        return None

    def is_alive(self):
        return not self.terminated

    def terminate(self):
        self.terminated = True


class _StuckExecutor:
    def __init__(self):
        self.process = _StuckProcess()
        self._processes = {1: self.process}
        self._executor_manager_thread = None
        self.shutdown_calls = []

    def shutdown(self, **kwargs):
        self.shutdown_calls.append(kwargs)


class _SlowHeadExecutor:
    def __init__(self, **_kwargs):
        self.first_future = None
        self.first_call = None
        self.submitted = 0
        self.initial_window_filled = threading.Event()

    def submit(self, function, task):
        self.submitted += 1
        future = Future()
        if self.submitted == 1:
            self.first_future = future
            self.first_call = (function, task)
        else:
            future.set_result(function(task))
        if self.submitted == 4:
            self.initial_window_filled.set()
        return future

    def release_first(self):
        function, task = self.first_call
        self.first_future.set_result(function(task))

    def shutdown(self, **_kwargs):
        return None


class MaintenanceWorkerTests(unittest.TestCase):
    def test_non_contiguous_sequences_preserve_submission_order(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for index in range(3):
                path = Path(directory) / f"{index}.srt"
                path.write_text(
                    "1\n00:00:00,000 --> 00:00:01,000\nText\n",
                    encoding="utf-8",
                )
                paths.append(path)
            tasks = [
                ValidationTask(sequence=sequence, operation="count_cues",
                               target_path=str(path), target_language="")
                for sequence, path in zip((2, 7, 12), paths)
            ]
            pool = MaintenanceWorkerPool(2, executor_factory=_ImmediateExecutor)
            try:
                results = list(pool.map_ordered(tasks))
            finally:
                pool.shutdown()
            self.assertEqual([result.sequence for result in results], [2, 7, 12])
            self.assertEqual([result.cue_count for result in results], [1, 1, 1])

    def test_slow_head_bounds_running_and_buffered_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.srt"
            path.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nText\n",
                encoding="utf-8",
            )
            executor = _SlowHeadExecutor()
            pool = MaintenanceWorkerPool(2, executor_factory=lambda **_kwargs: executor)
            tasks = [
                ValidationTask(sequence=index, operation="count_cues",
                               target_path=str(path), target_language="")
                for index in range(10)
            ]
            results = []
            runner = threading.Thread(
                target=lambda: results.extend(pool.map_ordered(tasks)), daemon=True,
            )
            runner.start()
            self.assertTrue(executor.initial_window_filled.wait(timeout=1))
            self.assertEqual(executor.submitted, pool.max_in_flight)
            executor.release_first()
            runner.join(timeout=2)
            pool.shutdown()
            self.assertFalse(runner.is_alive())
            self.assertEqual([result.sequence for result in results], list(range(10)))

    def test_count_cues_runs_outside_coordinator_process(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.srt"
            path.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nText\n",
                encoding="utf-8",
            )
            pool = MaintenanceWorkerPool(1)
            try:
                result = next(pool.map_ordered([
                    ValidationTask(sequence=0, operation="count_cues",
                                   target_path=str(path), target_language="")
                ]))
            finally:
                pool.shutdown()
            self.assertEqual(result.cue_count, 1)
            self.assertNotEqual(result.worker_pid, os.getpid())

    def test_shutdown_terminates_process_after_grace_deadline(self):
        executor = _StuckExecutor()
        pool = MaintenanceWorkerPool(1)
        pool._executor = executor

        terminated = pool.shutdown(grace_seconds=0)

        self.assertEqual(terminated, 1)
        self.assertTrue(executor.process.terminated)
        self.assertEqual(
            executor.shutdown_calls,
            [{"wait": False, "cancel_futures": True}],
        )

    def test_cache_round_trip_and_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target.srt"
            target.write_text("subtitle", encoding="utf-8")
            store = StateStore(
                Path(directory) / "state.sqlite3",
                validator_version="validator-v1",
                config_fingerprint="config-v1",
            )
            try:
                entry = {
                    "targetPath": str(target),
                    "targetSize": target.stat().st_size,
                    "targetModifiedNs": target.stat().st_mtime_ns,
                    "dependencyFingerprint": {"source": None},
                    "validatorVersion": "validator-v1",
                    "configFingerprint": "maintenance-v1",
                    "validationResult": "valid",
                    "actionResult": "valid",
                    "targetHash": "abc",
                    "details": {"sourceAligned": False},
                }
                self.assertEqual(store.upsert_maintenance_cache_entries([entry]), 1)
                cached = store.maintenance_cache_entries([target])
                self.assertEqual(len(cached), 1)
                self.assertEqual(next(iter(cached.values()))["targetHash"], "abc")
                self.assertEqual(store.delete_maintenance_cache_entries([target]), 1)
                self.assertEqual(store.maintenance_cache_entries([target]), {})
            finally:
                store.close()

    def test_retention_prunes_expired_cache_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target.srt"
            target.write_text("subtitle", encoding="utf-8")
            store = StateStore(
                Path(directory) / "state.sqlite3",
                validator_version="validator-v1",
                config_fingerprint="config-v1",
            )
            try:
                store.upsert_maintenance_cache_entries([{
                    "targetPath": str(target),
                    "targetSize": target.stat().st_size,
                    "targetModifiedNs": target.stat().st_mtime_ns,
                    "dependencyFingerprint": {},
                    "validatorVersion": "validator-v1",
                    "configFingerprint": "maintenance-v1",
                    "validationResult": "valid",
                    "actionResult": "valid",
                    "targetHash": "abc",
                    "details": {},
                    "updatedAt": 1,
                }])
                self.assertGreaterEqual(store.prune_older_than(30), 1)
                self.assertEqual(store.maintenance_cache_entries([target]), {})
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
