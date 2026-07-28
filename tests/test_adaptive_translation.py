import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from collections import defaultdict
from unittest.mock import Mock, patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "docker"))
os.environ.setdefault("BAZARR_URL", "http://bazarr")
os.environ.setdefault("BAZARR_API_KEY", "test")
os.environ.setdefault("LINGARR_URL", "http://lingarr")

import Bazarr_AutoTranslate as app  # noqa: E402
from clean_et_subs import parse_srt_cues  # noqa: E402


class AdaptiveTranslationTests(unittest.TestCase):
    def test_shared_capacity_handoff_is_atomic_for_all_supported_limits(self):
        for limit in (1, 2, 4):
            gate = app.SharedCapacityCoordinator(limit)
            occupied = [gate.acquire_translation() for _ in range(limit)]
            token = occupied.pop()
            repair_token = gate.reserve_repair()
            self.assertEqual(repair_token, token)

            repair_started = threading.Event()
            release_repair = threading.Event()
            translation_started = threading.Event()

            def repair():
                self.assertTrue(gate.start_repair(repair_token))
                repair_started.set()
                release_repair.wait(1)
                gate.release(repair_token)

            def translation():
                next_token = gate.acquire_translation()
                translation_started.set()
                gate.release(next_token)

            repair_thread = threading.Thread(target=repair)
            translation_thread = threading.Thread(target=translation)
            repair_thread.start()
            translation_thread.start()
            self.assertTrue(repair_started.wait(1))
            self.assertFalse(translation_started.wait(0.05))
            release_repair.set()
            repair_thread.join(1)
            self.assertTrue(translation_started.wait(1))
            translation_thread.join(1)
            for occupied_token in occupied:
                gate.release(occupied_token)

    def test_file_lanes_keep_one_long_and_remaining_short(self):
        for workers in (2, 4):
            gate = app.FileLaneGate(workers)
            self.assertEqual(gate.acquire(True), "long")
            for _ in range(workers - 1):
                self.assertEqual(gate.acquire(False), "short")
            gate.release("long")
            for _ in range(workers - 1):
                gate.release("short")

    def test_single_worker_prioritizes_waiting_short_job(self):
        gate = app.FileLaneGate(1)
        self.assertEqual(gate.acquire(False), "short")
        order = []

        def acquire(kind):
            lane = gate.acquire(kind == "long")
            order.append(lane)
            gate.release(lane)

        long_thread = threading.Thread(target=acquire, args=("long",))
        short_thread = threading.Thread(target=acquire, args=("short",))
        long_thread.start()
        time.sleep(0.02)
        short_thread.start()
        time.sleep(0.02)
        gate.release("short")
        long_thread.join(1)
        short_thread.join(1)
        self.assertEqual(order, ["short", "long"])

    def test_idle_long_slot_borrows_highest_estimated_short_job(self):
        gate = app.FileLaneGate(2)
        self.assertEqual(gate.acquire(True, 1000), "long")
        self.assertEqual(gate.acquire(False, 100), "short")
        admitted = []

        def acquire(name, estimate):
            lane = gate.acquire(False, estimate)
            admitted.append((name, lane))
            if lane:
                gate.release(lane)

        lower = threading.Thread(target=acquire, args=("lower", 200))
        higher = threading.Thread(target=acquire, args=("higher", 500))
        lower.start()
        higher.start()
        time.sleep(0.05)
        gate.release("long")
        time.sleep(0.05)
        gate.release("short")
        lower.join(1)
        higher.join(1)

        self.assertEqual(admitted[0], ("higher", "short (borrowed)"))
        self.assertEqual({name for name, _lane in admitted}, {"lower", "higher"})

    def test_waiting_long_job_has_priority_over_short_borrower(self):
        gate = app.FileLaneGate(2)
        self.assertEqual(gate.acquire(True, 1000), "long")
        self.assertEqual(gate.acquire(False, 100), "short")
        admitted = []
        release_long = threading.Event()

        def acquire_long():
            lane = gate.acquire(True, 800)
            admitted.append(("long", lane))
            release_long.wait(1)
            gate.release(lane)

        def acquire_short():
            lane = gate.acquire(False, 700)
            admitted.append(("short", lane))
            gate.release(lane)

        long_thread = threading.Thread(target=acquire_long)
        short_thread = threading.Thread(target=acquire_short)
        long_thread.start()
        short_thread.start()
        time.sleep(0.05)
        gate.release("long")
        time.sleep(0.05)
        self.assertEqual(admitted[0], ("long", "long"))
        release_long.set()
        gate.release("short")
        long_thread.join(1)
        short_thread.join(1)

    def test_four_workers_borrow_only_the_preferred_long_slot(self):
        gate = app.FileLaneGate(4)
        for estimate in (100, 200, 300):
            self.assertEqual(gate.acquire(False, estimate), "short")
        self.assertEqual(gate.acquire(False, 400), "short (borrowed)")
        gate.release("short (borrowed)")
        for _ in range(3):
            gate.release("short")

    def test_final_circuit_check_blocks_job_that_was_allowed_before_lane_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "movie.mkv"
            source = root / "movie.en.srt"
            video.write_bytes(b"video")
            source.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nEnglish\n",
                encoding="utf-8",
            )
            item = {
                "sonarrEpisodeId": 7,
                "sonarrSeriesId": 42,
                "seriesTitle": "Top Gear",
                "missing_subtitles": [{"code2": "et"}],
            }
            state = Mock()
            state.circuit_permission.side_effect = [
                {"allowed": True, "state": "closed", "failures": 0},
                {"allowed": True, "state": "closed", "failures": 0},
                {
                    "allowed": False,
                    "state": "open",
                    "failures": 3,
                    "retryAt": 1234,
                },
            ]
            submit = Mock()
            mark_failed = Mock()
            stats = defaultdict(int)
            stats["translations"] = []
            timing = {
                "cueCount": 1,
                "secondsPerCue": 1.8,
                "sampleCount": 0,
                "scope": "cold_start",
                "estimatedSeconds": 2,
                "timeoutSeconds": 60,
                "lane": "short",
            }
            with patch.multiple(
                app,
                LANGUAGES=["en", "et"],
                CLEANUP_UNDERSIZED_ENABLED=False,
                fetch_subtitles=lambda *_args: (
                    str(video),
                    [{"code2": "en", "path": str(source), "forced": False}],
                ),
                lingarr_resolve_media_id=lambda *_args: 99,
                lingarr_get_active_translations=lambda: [],
                lingarr_submit_file=submit,
                _get_validation_state=lambda: state,
                _active_quarantine_hold=lambda *_args: None,
                _check_cooldown=lambda *_args: None,
                _count_dialogue_lines=lambda _path: 1,
                _estimate_timeout=lambda *_args: timing,
                _record_submission=lambda *_args, **_kwargs: 1,
                _mark_submission_failed=mark_failed,
                _file_lane_gate=app.FileLaneGate(2),
                _translation_capacity=app.TranslationCapacityGate(2),
            ):
                app.process_item(
                    item, "episodes", "sonarrEpisodeId", stats, threading.Lock()
                )

            submit.assert_not_called()
            mark_failed.assert_called_once_with(1)
            self.assertEqual(stats["deferred"], 1)
            self.assertEqual(
                [call.kwargs["claim"] for call in state.circuit_permission.call_args_list],
                [False, False, True],
            )

    def test_failed_job_recovery_preserves_source_structure_and_repairs_gap(self):
        source = (
            "1\n00:00:01,000 --> 00:00:02,000\nFirst line\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\nSecond line\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "show.en.srt"
            target_path = root / "show.et.srt"
            source_path.write_text(source, encoding="utf-8")
            detail = {
                "lines": [
                    {"position": 0, "source": "First line", "target": "Esimene rida"},
                    {"position": 1, "source": "Second line", "target": ""},
                ],
                "events": [{"message": "line translation failed"}],
            }
            with (
                patch.object(app, "lingarr_get_job", return_value=detail),
                patch.object(app, "lingarr_translate_line", return_value="Teine rida"),
                patch.object(app, "_get_validation_state") as state,
            ):
                result = app._recover_failed_lingarr_job(
                    42,
                    str(source_path),
                    str(target_path),
                    "en",
                    "et",
                    "Top Gear S01E01",
                )
            self.assertTrue(result["recovered"])
            cues, errors = parse_srt_cues(target_path.read_text(encoding="utf-8"))
            self.assertEqual(errors, [])
            self.assertEqual([cue.number for cue in cues], [1, 2])
            self.assertEqual(
                [cue.timestamp for cue in cues],
                [
                    "00:00:01,000 --> 00:00:02,000",
                    "00:00:03,000 --> 00:00:04,000",
                ],
            )
            self.assertEqual(cues[1].text, "Teine rida")


if __name__ == "__main__":
    unittest.main()
