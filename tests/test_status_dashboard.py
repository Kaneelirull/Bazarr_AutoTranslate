import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "docker"))

from status_dashboard import (  # noqa: E402
    StatusTracker,
    build_cycle_jobs,
    episode_identity,
    episode_identity_from_path,
    render_dashboard,
    start_status_server,
)


class FakeClock:
    def __init__(self, value=1_800_000_000.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def title_for(item, item_type):
    return item.get("seriesTitle") if item_type == "episodes" else item.get("title")


def queue_jobs(cycle_id="cycle-1"):
    work = [
        ({
            "sonarrEpisodeId": 42,
            "seriesTitle": "Example Show",
            "season": 1,
            "episode": 2,
            "title": "The Beginning",
            "missing_subtitles": [
                {"code2": "sv"},
                {"code2": "et"},
                {"code2": "et"},
                {"code2": None},
            ],
        }, "episodes", "sonarrEpisodeId"),
        ({
            "radarrId": 7,
            "title": "Example Movie",
            "missing_subtitles": [{"code2": "et"}],
        }, "movies", "radarrId"),
    ]
    return build_cycle_jobs(work, ["en", "et", "sv"], cycle_id, title_for)


class StatusDashboardTests(unittest.TestCase):
    def make_tracker(self, directory, clock=None, recent_limit=20):
        return StatusTracker(
            Path(directory) / "status.json",
            Path(directory) / "status_history.jsonl",
            retention_days=30,
            recent_limit=recent_limit,
            clock=clock or FakeClock(),
        )

    def test_queue_is_one_job_per_language_in_configured_order(self):
        jobs = queue_jobs()
        self.assertEqual(
            [(job["title"], job["targetLanguage"]) for job in jobs],
            [
                ("Example Show", "et"),
                ("Example Show", "sv"),
                ("Example Movie", "et"),
            ],
        )
        self.assertEqual(len({job["key"] for job in jobs}), 3)
        self.assertEqual(jobs[0]["episodeCode"], "S01E02")
        self.assertEqual(jobs[0]["episodeTitle"], "The Beginning")
        self.assertIsNone(jobs[-1]["episodeCode"])
        self.assertIsNone(jobs[-1]["episodeTitle"])

    def test_episode_identity_accepts_schema_variants_and_paths(self):
        self.assertEqual(
            episode_identity(
                {
                    "series_title": "Example Show",
                    "season_number": "4",
                    "episode_number": 9,
                    "episode_title": "A Better Ending",
                },
                "episodes",
            ),
            ("S04E09", "A Better Ending"),
        )
        self.assertEqual(
            episode_identity_from_path(
                "/media/Example Show - s4e9 - A Better Ending.en.srt"
            ),
            "S04E09",
        )
        self.assertIsNone(episode_identity_from_path("/media/Example Movie.mkv"))

    def test_queued_duration_is_blank_and_active_duration_uses_started_time(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            tracker = self.make_tracker(directory, clock)
            jobs = queue_jobs()
            tracker.start_cycle("cycle-1", 1, jobs)
            clock.advance(30)
            queued = tracker.snapshot()["upNext"][0]
            self.assertIsNone(queued["durationSeconds"])

            tracker.transition(jobs[0]["key"], "translating")
            clock.advance(12.4)
            active = tracker.snapshot()["activeJobs"][0]
            self.assertEqual(active["durationSeconds"], 12.4)

    def test_path_enrichment_updates_all_languages_and_history(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = self.make_tracker(directory)
            jobs = queue_jobs()
            for job in jobs:
                if job["itemType"] == "episodes":
                    job["episodeCode"] = None
                    job["episodeTitle"] = None
            tracker.start_cycle("cycle-1", 1, jobs)
            self.assertTrue(
                tracker.set_episode_identity(
                    "episodes", 42, "S03E07", "Recovered title"
                )
            )
            episode_rows = [
                row for row in tracker.snapshot()["upNext"]
                if row["itemType"] == "episodes"
            ]
            self.assertEqual(
                {(row["episodeCode"], row["episodeTitle"]) for row in episode_rows},
                {("S03E07", "Recovered title")},
            )
            tracker.transition(jobs[0]["key"], "accepted")
            recent = tracker.snapshot()["recentOutcomes"][0]
            self.assertEqual(recent["episodeCode"], "S03E07")
            self.assertEqual(recent["episodeTitle"], "Recovered title")

    def test_submission_is_active_and_validation_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            tracker = self.make_tracker(directory, clock)
            jobs = queue_jobs()
            tracker.start_cycle("cycle-1", 1, jobs)
            job = jobs[0]

            tracker.transition(job["key"], "translating")
            active = tracker.snapshot()
            self.assertEqual(active["currentCycle"]["accepted"], 0)
            self.assertEqual(active["currentCycle"]["translating"], 1)
            self.assertEqual(active["currentCycle"]["initial"], 3)

            clock.advance(12)
            tracker.transition(job["key"], "validating")
            tracker.transition(job["key"], "accepted")
            accepted = tracker.snapshot()
            self.assertEqual(accepted["currentCycle"]["accepted"], 1)
            self.assertEqual(accepted["currentCycle"]["done"], 1)
            self.assertEqual(accepted["currentCycle"]["remaining"], 2)

    def test_finish_cycle_marks_unfinished_jobs_deferred(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = self.make_tracker(directory)
            jobs = queue_jobs()
            tracker.start_cycle("cycle-1", 1, jobs)
            tracker.transition(jobs[0]["key"], "accepted")
            tracker.finish_cycle()

            cycle = tracker.snapshot()["currentCycle"]
            self.assertEqual(cycle["done"], 3)
            self.assertEqual(cycle["accepted"], 1)
            self.assertEqual(cycle["deferred"], 2)
            self.assertEqual(cycle["remaining"], 0)

    def test_rolling_windows_and_repaired_subtype(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            tracker = self.make_tracker(directory, clock)
            jobs = queue_jobs()
            tracker.start_cycle("cycle-1", 1, jobs)
            tracker.transition(jobs[0]["key"], "accepted", repaired=True)
            clock.advance(2 * 3600)
            tracker.transition(jobs[1]["key"], "failed")
            clock.advance(5 * 3600)
            tracker.transition(jobs[2]["key"], "timed_out")

            history = tracker.snapshot()["history"]
            self.assertEqual(history["1h"]["timed_out"], 1)
            self.assertEqual(history["6h"]["failed"], 1)
            self.assertEqual(history["6h"]["accepted"], 0)
            self.assertEqual(history["12h"]["accepted"], 1)
            self.assertEqual(history["12h"]["repaired"], 1)

    def test_restart_recovers_active_job_as_interrupted_deferred(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            tracker = self.make_tracker(directory, clock)
            jobs = queue_jobs()
            tracker.start_cycle("cycle-1", 1, jobs[:1])
            tracker.transition(jobs[0]["key"], "translating")
            clock.advance(30)

            recovered = self.make_tracker(directory, clock)
            snapshot = recovered.snapshot()
            self.assertEqual(snapshot["currentCycle"]["deferred"], 1)
            self.assertEqual(
                snapshot["recentOutcomes"][0]["reason"],
                "interrupted by service restart",
            )

    def test_malformed_history_line_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / "status_history.jsonl"
            history.write_text(
                '{"kind":"job","timestamp":"2027-01-15T08:00:00Z","outcome":"accepted"}\n'
                '{"incomplete":',
                encoding="utf-8",
            )
            tracker = StatusTracker(
                root / "status.json",
                history,
                retention_days=30,
                clock=lambda: 1_800_000_000.0,
            )
            self.assertEqual(tracker.snapshot()["history"]["7d"]["accepted"], 1)

    def test_history_compaction_removes_events_past_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            tracker = self.make_tracker(directory, clock)
            jobs = queue_jobs()
            tracker.start_cycle("cycle-1", 1, jobs[:1])
            tracker.transition(jobs[0]["key"], "accepted")
            clock.advance(31 * 86400)

            self.assertEqual(tracker.compact_history(), 1)
            self.assertEqual(tracker.snapshot()["history"]["7d"]["accepted"], 0)
            self.assertEqual(
                (Path(directory) / "status_history.jsonl").read_text(encoding="utf-8"),
                "",
            )

    def test_maintenance_is_separate_from_translation_history(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = self.make_tracker(directory)
            tracker.record_maintenance({
                "repaired": 2,
                "quarantined": 3,
                "pruned": 1,
            })
            snapshot = tracker.snapshot()
            self.assertEqual(snapshot["history"]["7d"]["repaired"], 0)
            self.assertEqual(snapshot["maintenance"]["history"]["7d"]["repaired"], 2)
            self.assertEqual(snapshot["maintenance"]["lastScan"]["metrics"]["quarantined"], 3)

    def test_concurrent_terminal_updates_are_not_lost(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = self.make_tracker(directory)
            jobs = queue_jobs()
            tracker.start_cycle("cycle-1", 1, jobs)
            threads = [
                threading.Thread(
                    target=tracker.transition,
                    args=(job["key"], "accepted"),
                )
                for job in jobs
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            snapshot = tracker.snapshot()
            self.assertEqual(snapshot["currentCycle"]["accepted"], 3)
            self.assertEqual(snapshot["history"]["1h"]["accepted"], 3)

    def test_snapshot_is_atomic_json_and_contains_stable_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = self.make_tracker(directory)
            tracker.start_cycle("cycle-1", 1, queue_jobs())
            payload = json.loads(
                (Path(directory) / "status.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                list(payload),
                [
                    "generatedAt", "service", "currentCycle", "activeJobs",
                    "upNext", "recentOutcomes", "history", "maintenance",
                ],
            )
            self.assertFalse((Path(directory) / "status.json.tmp").exists())

    def test_snapshot_writers_use_unique_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "status.json"
            history = root / "status.jsonl"
            first = StatusTracker(snapshot, history)
            second = StatusTracker(snapshot, history)
            errors = []

            def write_many(tracker, prefix):
                try:
                    for index in range(20):
                        tracker.set_phase(f"{prefix}-{index}")
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            threads = [
                threading.Thread(target=write_many, args=(first, "first")),
                threading.Thread(target=write_many, args=(second, "second")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            payload = json.loads(snapshot.read_text(encoding="utf-8"))
            self.assertIn("service", payload)
            self.assertEqual(list(root.glob(".status.json.*.tmp")), [])

    def test_html_escapes_titles_and_contains_no_paths_or_auto_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = self.make_tracker(directory)
            jobs = queue_jobs()
            jobs[0]["title"] = '<script>alert("x")</script>'
            tracker.start_cycle("cycle-1", 1, jobs)
            page = render_dashboard(tracker.snapshot())
            self.assertNotIn("<script>", page)
            self.assertIn("&lt;script&gt;", page)
            self.assertNotIn("/media/", page)
            self.assertNotIn("api_key", page.lower())
            self.assertNotIn("http-equiv=\"refresh\"", page.lower())
            self.assertIn("/assets/dashboard.css", page)
            self.assertIn("/assets/dashboard.js", page)
            self.assertNotIn("<style>", page)
            self.assertNotIn("<script>", page)
            self.assertNotIn("&quot;jobs&quot;", page)

    def test_http_routes_cache_headers_and_not_found(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = self.make_tracker(directory)
            tracker.start_cycle("cycle-1", 1, queue_jobs())
            server, thread = start_status_server(tracker, "127.0.0.1", 0)
            port = server.server_address[1]
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    csp = response.headers["Content-Security-Policy"]
                    self.assertIn("script-src 'self'", csp)
                    self.assertIn("connect-src 'self'", csp)
                    self.assertNotIn("'unsafe-inline'", csp)
                    self.assertIn(b"Translation status", response.read())
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/assets/dashboard.css"
                ) as response:
                    self.assertEqual(response.headers["Content-Type"], "text/css; charset=utf-8")
                    self.assertIn(b"--bg-base", response.read())
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/assets/dashboard.js"
                ) as response:
                    script = response.read()
                    self.assertIn(b"formatDuration", script)
                    self.assertIn(b"Refresh in", script)
                    self.assertIn(b"types.size > 1", script)
                    self.assertIn(b"No maintenance actions", script)
                    self.assertIn(b"repaired)", script)
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/status"
                ) as response:
                    payload = json.loads(response.read())
                    self.assertIn("currentCycle", payload)
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/healthz"
                ) as response:
                    self.assertEqual(json.loads(response.read())["status"], "ok")
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/missing")
                self.assertEqual(error.exception.code, 404)
                error.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_port_conflict_raises_without_corrupting_tracker(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = self.make_tracker(directory)
            with patch(
                "status_dashboard._DashboardServer",
                side_effect=OSError("address in use"),
            ):
                with self.assertRaises(OSError):
                    start_status_server(tracker, "127.0.0.1", 8765)
                self.assertEqual(tracker.snapshot()["service"]["phase"], "startup")


if __name__ == "__main__":
    unittest.main()
