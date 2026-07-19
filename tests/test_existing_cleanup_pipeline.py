import os
import sys
import tempfile
import unittest
from pathlib import Path
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


class ExistingCleanupPipelineTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
