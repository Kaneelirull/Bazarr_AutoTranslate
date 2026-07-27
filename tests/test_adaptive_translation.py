import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "docker"))
os.environ.setdefault("BAZARR_URL", "http://bazarr")
os.environ.setdefault("BAZARR_API_KEY", "test")
os.environ.setdefault("LINGARR_URL", "http://lingarr")

import Bazarr_AutoTranslate as app  # noqa: E402
from clean_et_subs import parse_srt_cues  # noqa: E402


class AdaptiveTranslationTests(unittest.TestCase):
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
