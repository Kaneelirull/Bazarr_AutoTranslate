import os
import io
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "docker"))
os.environ.setdefault("BAZARR_URL", "http://bazarr:6767")
os.environ.setdefault("BAZARR_API_KEY", "test")
os.environ.setdefault("LINGARR_URL", "http://lingarr:8080")

import Bazarr_AutoTranslate as app  # noqa: E402
from clean_et_subs import ValidationStateStore  # noqa: E402


def make_srt(text: str) -> str:
    return f"1\n00:00:01,000 --> 00:00:01,900\n{text}\n"


def make_multi_srt(*texts: str) -> str:
    return "\n\n".join(
        f"{index}\n00:00:{index:02d},000 --> 00:00:{index:02d},900\n{text}"
        for index, text in enumerate(texts, start=1)
    ) + "\n"


class ExistingCleanupPipelineTests(unittest.TestCase):
    def tearDown(self):
        app._shutdown_repair_executor()
        with app._pending_repairs_lock:
            app._pending_repairs.clear()
            app._repair_keys.clear()

    def test_cooldown_can_be_cleared_by_removed_target_path(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "show.et.srt"
            cache_file = Path(directory) / "submitted_cache.json"
            with patch.multiple(
                app,
                STATE_DIR=directory,
                SUBMIT_CACHE_FILE=str(cache_file),
                _submitted_cache={},
                _submitted_paths={},
            ):
                app._record_submission(42, "et", str(target))
                self.assertIsNotNone(app._check_cooldown(42, "et"))
                cleared = app._clear_submission_for_path(target, "et")
                self.assertEqual(cleared, 1)
                self.assertIsNone(app._check_cooldown(42, "et"))

    def test_existing_valid_file_is_scanned_then_skipped_by_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "media"
            root.mkdir()
            (root / "show.eng.srt").write_text(make_srt("Good evening"), encoding="utf-8")
            (root / "show.et.srt").write_text(make_srt("Tere õhtust"), encoding="utf-8")
            state = ValidationStateStore(Path(directory) / "state.json")

            with patch.multiple(
                app,
                CLEANUP_ROOTS=[root],
                CLEANUP_LANGUAGES={"et"},
                CLEANUP_SCAN_EXISTING=True,
                CLEANUP_SCAN_DRY_RUN=False,
                CLEANUP_ACTION="quarantine",
                CLEANUP_QUARANTINE_DIR=Path(directory) / "quarantine",
                _validation_state=state,
            ):
                first = app.run_existing_cleanup_scan()
                second = app.run_existing_cleanup_scan()

            self.assertEqual(first["files_checked"], 1)
            self.assertEqual(second["files_checked"], 0)
            self.assertEqual(second["skipped_unchanged"], 1)

    def test_format_only_recovery_does_not_call_lingarr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "show.eng.srt"
            target = root / "show.et.srt"
            state = ValidationStateStore(root / "state.json")
            source.write_text(make_multi_srt("First line", "Second cue"), encoding="utf-8")
            target.write_text(
                "1\n00:00:01,000 --> 00:00:01,900\nEsimene\n\nteine rida\n\n"
                "2\n00:00:02,000 --> 00:00:02,900\nTeine subtiiter\n",
                encoding="utf-8",
            )

            with (
                patch.multiple(
                    app,
                    CLEANUP_LANGUAGES={"et"},
                    CLEANUP_FORMAT_REPAIR_ENABLED=True,
                    CLEANUP_REPAIR_ENABLED=True,
                    _validation_state=state,
                ),
                patch.object(app, "lingarr_translate_line") as translate,
            ):
                action, report = app._validate_translated_file(
                    str(source), str(target), "en", "et", None, title="show"
                )

            self.assertEqual(action, "formatted")
            self.assertTrue(report.valid)
            translate.assert_not_called()
            self.assertIn("Esimene\nteine rida", target.read_text(encoding="utf-8"))

    def test_repair_logs_attempts_without_dialogue_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "show.eng.srt"
            target = root / "show.et.srt"
            state = ValidationStateStore(root / "state.json")
            source.write_text(
                make_multi_srt("Before secret", "Target secret dialogue", "After secret"),
                encoding="utf-8",
            )
            target.write_text(
                make_multi_srt("Enne", "[SOURCE] leaked [/SOURCE]", "Pärast"),
                encoding="utf-8",
            )
            responses = ["[SOURCE] still leaked [/SOURCE]", "Parandatud"]

            def translate(*args, **kwargs):
                kwargs["outcome_meta"].update({"httpStatus": 200, "httpDurationSeconds": 0.01})
                return responses.pop(0)

            output = io.StringIO()
            with (
                patch.multiple(
                    app,
                    CLEANUP_LANGUAGES={"et"},
                    CLEANUP_FORMAT_REPAIR_ENABLED=True,
                    CLEANUP_REPAIR_ENABLED=True,
                    CLEANUP_REPAIR_CONTEXT_LINES=1,
                    CLEANUP_MAX_REPAIR_ATTEMPTS=2,
                    _validation_state=state,
                ),
                patch.object(app, "lingarr_translate_line", side_effect=translate),
                redirect_stdout(output),
            ):
                action, _ = app._validate_translated_file(
                    str(source), str(target), "en", "et", None, title="show"
                )

            logs = output.getvalue()
            self.assertEqual(action, "repaired")
            self.assertIn("attempt 1/2 with context before=1 after=1", logs)
            self.assertIn("attempt 2/2 without context", logs)
            self.assertIn("rejected HTTP 200", logs)
            self.assertIn("accepted HTTP 200", logs)
            self.assertNotIn("Target secret dialogue", logs)
            self.assertNotIn("Parandatud", logs)

    def test_quarantine_triggers_both_bazarr_rescans(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "media"
            root.mkdir()
            (root / "show.eng.srt").write_text(make_srt("One line"), encoding="utf-8")
            target = root / "show.et.srt"
            target.write_text(make_srt("Üks\nKaks\nKolm\nNeli\nViis"), encoding="utf-8")
            quarantine = Path(directory) / "quarantine"
            state = ValidationStateStore(Path(directory) / "state.json")

            with (
                patch.multiple(
                    app,
                    CLEANUP_ROOTS=[root],
                    CLEANUP_LANGUAGES={"et"},
                    CLEANUP_SCAN_EXISTING=True,
                    CLEANUP_SCAN_DRY_RUN=False,
                    CLEANUP_ACTION="quarantine",
                    CLEANUP_QUARANTINE_DIR=quarantine,
                    CLEANUP_REPAIR_ENABLED=False,
                    _validation_state=state,
                ),
                patch.object(app, "trigger_bazarr_sync") as trigger,
                patch.object(app, "wait_for_bazarr_sync", return_value=True) as wait,
            ):
                stats = app.run_existing_cleanup_scan()

            self.assertFalse(target.exists())
            self.assertTrue((quarantine / "show.et.srt").exists())
            self.assertTrue((quarantine / "show.et.srt.validation.json").exists())
            self.assertEqual(stats["quarantined_files"], 1)
            self.assertEqual(stats["excessive_line_cues"], 1)
            trigger.assert_called_once_with(True, True)
            wait.assert_called_once_with(True, True, app.SYNC_TIMEOUT)

    def test_dry_run_does_not_repair_move_or_rescan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "media"
            root.mkdir()
            (root / "show.eng.srt").write_text(make_srt("One line"), encoding="utf-8")
            target = root / "show.et.srt"
            original = make_srt("Üks\nKaks\nKolm\nNeli\nViis")
            target.write_text(original, encoding="utf-8")
            state = ValidationStateStore(Path(directory) / "state.json")

            with (
                patch.multiple(
                    app,
                    CLEANUP_ROOTS=[root],
                    CLEANUP_LANGUAGES={"et"},
                    CLEANUP_SCAN_EXISTING=True,
                    CLEANUP_SCAN_DRY_RUN=True,
                    CLEANUP_ACTION="quarantine",
                    CLEANUP_QUARANTINE_DIR=Path(directory) / "quarantine",
                    CLEANUP_REPAIR_ENABLED=True,
                    _validation_state=state,
                ),
                patch.object(app, "lingarr_translate_line") as translate,
                patch.object(app, "trigger_bazarr_sync") as trigger,
            ):
                stats = app.run_existing_cleanup_scan()

            self.assertTrue(target.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), original)
            self.assertEqual(stats["dry_run_files"], 1)
            translate.assert_not_called()
            trigger.assert_not_called()

    def test_repair_queue_uses_dedicated_worker_and_suppresses_duplicate(self):
        started = threading.Event()
        release = threading.Event()
        report = SimpleNamespace(repairable_cue_indexes=[0], issues=[])

        def worker(**kwargs):
            started.set()
            release.wait(2)
            return app.RepairJobResult(
                "repaired", report, "show", "et", "episodes", 42,
                attempts=1, target_path="show.et.srt",
            )

        stats = {
            "submitted": 0,
            "completed": 0,
            "failed": 0,
            "translations": [],
            "episode_activity": False,
            "movie_activity": False,
        }
        with (
            patch.object(app, "_perform_repair", side_effect=worker),
            patch.object(app, "_repair_capacity", threading.BoundedSemaphore(2)),
        ):
            first = app._queue_repair(("show", "hash"), {}, report, "show", "et")
            self.assertTrue(started.wait(1), "dedicated repair worker did not start")
            duplicate = app._queue_repair(("show", "hash"), {}, report, "show", "et")
            self.assertEqual(first, "repair-queued")
            self.assertEqual(duplicate, "repair-duplicate")
            release.set()
            results = app._drain_pending_repairs(stats)

        self.assertEqual(len(results), 1)
        self.assertEqual(stats["completed"], 1)
        self.assertTrue(stats["episode_activity"])

    def test_repair_queue_overflow_is_deferred(self):
        started = threading.Event()
        release = threading.Event()
        report = SimpleNamespace(repairable_cue_indexes=[0], issues=[])

        def worker(**kwargs):
            started.set()
            release.wait(2)
            return app.RepairJobResult("repair-deferred", report, "one", "et", None, None)

        stats = {
            "submitted": 0,
            "completed": 0,
            "failed": 0,
            "translations": [],
            "episode_activity": False,
            "movie_activity": False,
        }
        with (
            patch.object(app, "_perform_repair", side_effect=worker),
            patch.object(app, "_repair_capacity", threading.BoundedSemaphore(1)),
        ):
            first = app._queue_repair(("one",), {}, report, "one", "et")
            self.assertTrue(started.wait(1))
            second = app._queue_repair(("two",), {}, report, "two", "et")
            self.assertEqual(first, "repair-queued")
            self.assertEqual(second, "repair-deferred")
            release.set()
            app._drain_pending_repairs(stats)

    def test_bazarr_wait_observes_job_start_before_completion(self):
        class Response:
            def __init__(self, jobs):
                self._jobs = jobs

            def raise_for_status(self):
                return None

            def json(self):
                return {"data": self._jobs}

        responses = [
            Response([]),
            Response([{"job_id": 1, "job_name": "Series subtitle scan", "status": "running"}]),
            Response([]),
        ]
        clock = [0.0]

        def advance(seconds):
            clock[0] += seconds

        with (
            patch.object(app.requests, "get", side_effect=responses),
            patch.object(app.time, "time", side_effect=lambda: clock[0]),
            patch.object(app.time, "sleep", side_effect=advance),
            patch.object(app, "SYNC_POLL_INTERVAL", 1),
            patch.object(app, "SYNC_START_TIMEOUT", 5),
        ):
            self.assertTrue(app.wait_for_bazarr_sync(True, False, 30))


if __name__ == "__main__":
    unittest.main()
