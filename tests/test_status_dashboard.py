import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "docker"))

from autotranslate.status.tracker import (  # noqa: E402
    StatusTracker,
    build_cycle_jobs,
    episode_identity,
    episode_identity_from_path,
    retry_media_identity,
)
from autotranslate.status.server import (  # noqa: E402
    render_dashboard,
    render_logs_page,
    read_logs,
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

    def test_up_next_snapshot_returns_complete_ordered_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = self.make_tracker(directory)
            work = [({
                "radarrId": item_id,
                "title": f"Movie {item_id:02d}",
                "missing_subtitles": [{"code2": "et"}],
            }, "movies", "radarrId") for item_id in range(15)]
            jobs = build_cycle_jobs(work, ["et"], "cycle-many", title_for)
            tracker.start_cycle("cycle-many", 1, jobs)

            up_next = tracker.snapshot()["upNext"]

            self.assertEqual(len(up_next), 15)
            self.assertEqual(
                [job["title"] for job in up_next],
                [f"Movie {item_id:02d}" for item_id in range(15)],
            )

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

    def test_retry_identity_rejects_season_folder_title(self):
        identity = retry_media_identity({
            "itemType": "episodes",
            "itemId": 11569,
            "seriesTitle": "Season 05",
            "mediaTitle": "Season 05 S05E24",
            "sourcePath": (
                "/media/_Shows/The Big Bang Theory (2007) [tvdbid-80379]/"
                "Season 05/The Big Bang Theory (2007) - S05E24 - "
                "The Countdown Reflection [Bluray-1080p].en.srt"
            ),
        })
        self.assertEqual(
            identity,
            {
                "displayTitle": "The Big Bang Theory (2007)",
                "episodeCode": "S05E24",
                "episodeTitle": "The Countdown Reflection",
            },
        )
        pathless = retry_media_identity({
            "itemType": "episodes",
            "itemId": 99,
            "seriesTitle": "Season 10",
            "mediaTitle": "Season 10 S10E16",
        })
        self.assertEqual(pathless["displayTitle"], "Episode 99")
        self.assertEqual(pathless["episodeCode"], "S10E16")

    def test_retry_identity_handles_series_movies_and_dated_episodes(self):
        top_gear = retry_media_identity({
            "itemType": "episodes",
            "itemId": 1,
            "seriesTitle": "Top Gear",
            "sourcePath": "/media/Top Gear - S04E02 - Episode 2.en.srt",
        })
        bluey = retry_media_identity({
            "itemType": "episodes",
            "itemId": 2,
            "sourcePath": "/media/Bluey (2018) - S01E38 - Copycat [WEB].eng.srt",
        })
        dated = retry_media_identity({
            "itemType": "episodes",
            "itemId": 3,
            "sourcePath": (
                "/media/The Daily Show - 2026-07-27 - "
                "Episode Name [WEB].en.srt"
            ),
        })
        movie = retry_media_identity({
            "itemType": "movies",
            "itemId": 4,
            "mediaTitle": "Example Movie (2025).en.srt",
        })
        windows_release = retry_media_identity({
            "itemType": "episodes",
            "itemId": 5,
            "sourcePath": (
                r"C:\media\How I Met Your Mother (2005) - S02E22 - "
                r"Something Blue -NOGRP.en.srt"
            ),
        })
        self.assertEqual(top_gear["displayTitle"], "Top Gear")
        self.assertEqual(top_gear["episodeCode"], "S04E02")
        self.assertEqual(bluey["displayTitle"], "Bluey (2018)")
        self.assertEqual(bluey["episodeTitle"], "Copycat")
        self.assertEqual(dated["episodeCode"], "2026-07-27")
        self.assertEqual(dated["episodeTitle"], "Episode Name")
        self.assertEqual(movie["displayTitle"], "Example Movie (2025)")
        self.assertEqual(
            windows_release["displayTitle"], "How I Met Your Mother (2005)"
        )
        self.assertEqual(windows_release["episodeTitle"], "Something Blue")

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
            tracker.finish_cycle({
                "cycle_suppressions": 2,
                "cooldown_deferrals": 3,
                "circuit_deferrals": 1,
            })

            cycle = tracker.snapshot()["currentCycle"]
            self.assertEqual(cycle["done"], 3)
            self.assertEqual(cycle["accepted"], 1)
            self.assertEqual(cycle["deferred"], 2)
            self.assertEqual(cycle["remaining"], 0)
            self.assertEqual(cycle["metrics"]["cycle_suppressions"], 2)
            self.assertEqual(cycle["metrics"]["cooldown_deferrals"], 3)
            self.assertEqual(cycle["metrics"]["circuit_deferrals"], 1)

    def test_wait_states_are_terminal_and_do_not_inflate_deferred(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = self.make_tracker(directory)
            jobs = queue_jobs()
            tracker.start_cycle("cycle-1", 1, jobs)
            tracker.transition(jobs[0]["key"], "waiting_retry")
            tracker.transition(jobs[1]["key"], "series_protected")
            tracker.transition(jobs[2]["key"], "missing_source")
            snapshot = tracker.snapshot()
            cycle = snapshot["currentCycle"]
            self.assertEqual(cycle["done"], 3)
            self.assertEqual(cycle["deferred"], 0)
            self.assertEqual(cycle["waitingRetry"], 1)
            self.assertEqual(cycle["seriesProtected"], 1)
            self.assertEqual(cycle["missingSource"], 1)
            self.assertEqual(snapshot["history"]["1h"]["waiting_retry"], 1)
            self.assertEqual(snapshot["history"]["1h"]["series_protected"], 1)
            self.assertEqual(snapshot["history"]["1h"]["missing_source"], 1)

    def test_admitted_retry_reopens_waiting_job_for_up_next_and_active(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = self.make_tracker(directory)
            jobs = queue_jobs()
            tracker.start_cycle("cycle-1", 1, jobs)
            tracker.transition(jobs[0]["key"], "waiting_retry")

            self.assertTrue(tracker.admit_retry(
                plan_id=9,
                item_type="episodes",
                item_id=42,
                target_language="et",
                display_title="Example Show",
                episode_code="S01E02",
                episode_title="The Beginning",
                attempt=2,
            ))
            snapshot = tracker.snapshot()
            self.assertEqual(len(snapshot["upNext"]), 3)
            retry = next(
                row for row in snapshot["upNext"]
                if row.get("retryPlanId") == 9
            )
            self.assertEqual(retry["retryPlanId"], 9)
            self.assertEqual(retry["retryAttempt"], 2)
            self.assertEqual(snapshot["currentCycle"]["waitingRetry"], 0)

            tracker.transition_for("episodes", 42, "et", "translating")
            snapshot = tracker.snapshot()
            self.assertEqual(snapshot["activeJobs"][0]["retryPlanId"], 9)
            self.assertEqual(len(snapshot["upNext"]), 2)

    def test_admitted_retry_absent_from_wanted_queue_is_added(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = self.make_tracker(directory)
            tracker.start_cycle("cycle-1", 1, queue_jobs())
            self.assertTrue(tracker.admit_retry(
                plan_id=12,
                item_type="episodes",
                item_id=999,
                target_language="et",
                display_title="Legacy Show",
                episode_code="S02E03",
            ))
            snapshot = tracker.snapshot()
            self.assertEqual(snapshot["currentCycle"]["initial"], 4)
            self.assertEqual(snapshot["upNext"][-1]["title"], "Legacy Show")

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
                    "upNext", "recentOutcomes", "validationObservations",
                    "timing", "circuits",
                    "retryPlans", "completedCycle",
                    "retryMaxAttempts",
                    "history", "maintenance",
                ],
            )
            self.assertFalse((Path(directory) / "status.json.tmp").exists())

    def test_retry_max_attempts_preserves_zero_as_unlimited(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = self.make_tracker(directory)
            self.assertEqual(tracker.snapshot()["retryMaxAttempts"], 0)
            tracker.set_diagnostics(timing={}, circuits=[], retry_max_attempts=0)
            self.assertEqual(tracker.snapshot()["retryMaxAttempts"], 0)

    def test_validation_observations_are_private_limited_and_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = FakeClock()
            tracker = self.make_tracker(directory, clock=clock, recent_limit=3)
            for index in range(22):
                tracker.record_validation_observation(
                    title=f"Example Show {index}",
                    episode_code=f"S01E{index:02d}",
                    item_type="episodes",
                    item_id=index,
                    target_language="et" if index % 2 else "sv",
                    cue_number=400 + index,
                    classification="ambiguous" if index % 2 else "likely_invariant",
                    reason=(
                        "Exact Title Case copy retained by the balanced policy."
                        if index % 2 else
                        "Copy retained because its structure indicates an invariant name or model."
                    ),
                    evidence={
                        "similarity": 1.0,
                        "exactNormalizedCopy": True,
                        "tokenCount": 3,
                        "tokenShape": "title_case",
                        "modelMarkerCount": 0,
                        "cueLanguage": "en",
                        "cueLanguageConfidence": 0.42,
                        "wholeTargetConfidence": 0.98,
                        "contextConfidence": 0.97,
                        "sourcePath": "/media/private/show.srt",
                        "sourceText": "private subtitle dialogue",
                        "targetHash": "private-hash",
                    },
                )
                clock.advance(1)

            observations = tracker.snapshot()["validationObservations"]
            self.assertEqual(len(observations), 20)
            self.assertEqual(observations[0]["cueNumber"], 421)
            self.assertEqual(observations[-1]["cueNumber"], 402)
            encoded = json.dumps(observations)
            for private in (
                "/media/private", "private subtitle", "private-hash", "sourcePath",
            ):
                self.assertNotIn(private, encoded)

            recovered = self.make_tracker(directory, clock=clock, recent_limit=3)
            self.assertEqual(
                recovered.snapshot()["validationObservations"], observations
            )
            clock.advance(31 * 86400)
            self.assertEqual(recovered.compact_history(), 22)
            self.assertEqual(recovered.snapshot()["validationObservations"], [])

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
            self.assertIn('type="module" src="/assets/app/dashboard.js"', page)
            self.assertNotIn("<style>", page)
            self.assertNotIn("<script>", page)
            self.assertNotIn("&quot;jobs&quot;", page)

    def test_dashboard_exposes_configured_display_timezone(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = self.make_tracker(directory)
            page = render_dashboard(
                tracker.snapshot(), display_timezone="Europe/Tallinn"
            )
            self.assertIn('data-time-zone="Europe/Tallinn"', page)

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
                    f"http://127.0.0.1:{port}/assets/app/dashboard.js"
                ) as response:
                    script = response.read()
                    self.assertEqual(response.headers["Content-Type"], "text/javascript; charset=utf-8")
                    self.assertIn(b"/api/status", script)
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/status"
                ) as response:
                    payload = json.loads(response.read())
                    self.assertIn("currentCycle", payload)
                    self.assertEqual(payload["validationObservations"], [])
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

    def test_http_response_contains_expected_client_disconnects(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = self.make_tracker(directory)
            server, thread = start_status_server(tracker, "127.0.0.1", 0)
            try:
                for error_type in (
                    BrokenPipeError,
                    ConnectionAbortedError,
                    ConnectionResetError,
                ):
                    for failure_point in ("send_response", "end_headers", "write"):
                        with self.subTest(
                            error_type=error_type.__name__,
                            failure_point=failure_point,
                        ):
                            handler = object.__new__(server.RequestHandlerClass)
                            handler.send_response = Mock()
                            handler.send_header = Mock()
                            handler.end_headers = Mock()
                            handler.wfile = Mock()
                            if failure_point == "write":
                                handler.wfile.write.side_effect = error_type()
                            else:
                                getattr(handler, failure_point).side_effect = error_type()
                            handler.close_connection = False

                            handler._send(
                                200,
                                "application/json; charset=utf-8",
                                b'{}',
                            )

                            self.assertTrue(handler.close_connection)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_http_response_propagates_unexpected_write_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = self.make_tracker(directory)
            server, thread = start_status_server(tracker, "127.0.0.1", 0)
            try:
                handler = object.__new__(server.RequestHandlerClass)
                handler.send_response = Mock()
                handler.send_header = Mock()
                handler.end_headers = Mock()
                handler.wfile = Mock()
                handler.wfile.write.side_effect = OSError("unexpected failure")
                handler.close_connection = False

                with self.assertRaisesRegex(OSError, "unexpected failure"):
                    handler._send(
                        200,
                        "application/json; charset=utf-8",
                        b'{}',
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_dashboard_assets_use_accessible_reason_tooltips(self):
        script = (REPO_ROOT / "docker" / "frontend" / "src" / "dashboard" / "format.tsx").read_text(encoding="utf-8")
        stylesheet = (REPO_ROOT / "docker" / "static" / "dashboard.css").read_text(encoding="utf-8")
        self.assertIn('role="tooltip"', script)
        self.assertIn('aria-expanded={open}', script)
        self.assertIn('event.key === "Escape"', script)
        self.assertIn("onMouseEnter", script)
        self.assertIn("onFocus", script)
        self.assertIn(".badge-tooltip-trigger:focus-visible", stylesheet)
        self.assertIn(".status-tooltip[hidden]", stylesheet)

    def test_dashboard_assets_render_timing_and_protection_states(self):
        source_root = REPO_ROOT / "docker" / "frontend" / "src" / "dashboard"
        script = "\n".join(path.read_text(encoding="utf-8") for path in source_root.glob("*.tsx"))
        stylesheet = (REPO_ROOT / "docker" / "static" / "dashboard.css").read_text(encoding="utf-8")
        for text in ("Cold-start estimate", "Learned average", "Protection active", "Trial awaiting repair/validation", "Retry queue", "No retry work scheduled.", "Rescheduled after no progress", "Repair at cycle end", "Waiting for circuit", "aria-pressed", "retry-sort-select"):
            self.assertIn(text, script)
        for selector in (".timing-grid", ".protection-row.is-healthy", ".retry-details-toggle:focus-visible", ".retry-detail-row[hidden]", ".work-view-switch"):
            self.assertIn(selector, stylesheet)
        self.assertFalse((REPO_ROOT / "docker" / "static" / "dashboard.js").exists())

    def test_port_conflict_raises_without_corrupting_tracker(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = self.make_tracker(directory)
            with patch(
                "autotranslate.status.server._DashboardServer",
                side_effect=OSError("address in use"),
            ):
                with self.assertRaises(OSError):
                    start_status_server(tracker, "127.0.0.1", 8765)
                self.assertEqual(tracker.snapshot()["service"]["phase"], "startup")

    def test_log_reader_filters_paginates_and_redacts_paths_and_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bazarr-autotranslate-2026-01-01.log").write_text(
                "2026-01-01T12:34:56.789Z [INFO] Top Gear started\n"
                "[ERROR] api_key=secret /media/shows/topgear/file.srt\n"
                "[INFO] Other show\n",
                encoding="utf-8",
            )
            page = read_logs(root, {"job": ["top gear"], "limit": ["1"]})
            self.assertEqual(len(page["lines"]), 1)
            self.assertTrue(page["lines"][0].startswith("2026-01-01T12:34:56.789Z"))
            error = read_logs(root, {"level": ["ERROR"]})
            self.assertEqual(len(error["lines"]), 1)
            self.assertNotIn("secret", error["lines"][0])
            self.assertNotIn("/media/", error["lines"][0])

    def test_log_page_uses_visible_tokenized_controls(self):
        page = render_logs_page()
        stylesheet = (
            REPO_ROOT / "docker" / "static" / "dashboard.css"
        ).read_text(encoding="utf-8")
        self.assertIn('id="logs-root"', page)
        self.assertIn('type="module" src="/assets/app/logs.js"', page)
        self.assertIn("border: 1px solid var(--border-default)", stylesheet)
        self.assertIn("background: var(--bg-overlay)", stylesheet)
        self.assertIn(".log-filters input:focus-visible", stylesheet)

        script = (REPO_ROOT / "docker" / "frontend" / "src" / "logs" / "App.tsx").read_text(encoding="utf-8")
        self.assertIn("Search text", script)
        self.assertIn("New records use UTC timestamps", script)
        self.assertIn("Top Gear or job ID", script)
        self.assertIn("load(false)", script)
        self.assertIn("load(true)", script)
        self.assertFalse((REPO_ROOT / "docker" / "static" / "logs.js").exists())
        self.assertIn("box-shadow: 0 0 0 3px var(--accent-glow)", stylesheet)
        for undefined in ("var(--muted)", "var(--border)", "var(--surface-strong)", "var(--text)"):
            self.assertNotIn(undefined, stylesheet)

    def test_maintenance_jobs_are_persistent_separate_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "status.json"
            history_path = Path(directory) / "history.jsonl"
            tracker = StatusTracker(snapshot_path, history_path)
            tracker.start_cycle("cycle", 1, [])
            job_id = tracker.create_maintenance_job(
                "cue_repair",
                {
                    "title": "Shameless (US)",
                    "episodeCode": "S06E03",
                    "targetLanguage": "et",
                    "sourceLanguage": "en",
                },
                state="repair_queued",
                details={"totalRepairableCues": 4, "completedCues": 0},
            )
            active = tracker.snapshot()
            self.assertEqual(active["currentCycle"]["initial"], 0)
            self.assertEqual(active["currentCycle"]["done"], 0)
            self.assertEqual(
                active["maintenance"]["activeJobs"][0]["statusJobId"], job_id
            )
            tracker.transition_maintenance(
                job_id,
                "repairing",
                details={
                    "completedCues": 1,
                    "currentAttempt": 2,
                    "maxAttempts": 5,
                    "progress": 25,
                },
            )
            self.assertTrue(
                tracker.complete_maintenance(job_id, "repaired")
            )
            self.assertFalse(
                tracker.complete_maintenance(job_id, "failed")
            )
            finished = tracker.snapshot()
            self.assertEqual(finished["maintenance"]["activeJobs"], [])
            self.assertEqual(
                finished["maintenance"]["recentOutcomes"][0]["outcome"],
                "repaired",
            )
            self.assertEqual(finished["history"]["1h"]["accepted"], 0)

    def test_startup_job_remains_active_through_backend_lifecycle_states(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = StatusTracker(
                Path(directory) / "status.json",
                Path(directory) / "history.jsonl",
            )
            job_id = tracker.create_maintenance_job(
                "startup", {"title": "Service startup"}, state="startup_wait"
            )
            for state in (
                "startup_sync", "repair_drain", "startup_cleanup",
            ):
                self.assertTrue(tracker.transition_maintenance(job_id, state))
                active = tracker.snapshot()["maintenance"]["activeJobs"]
                self.assertEqual(len(active), 1)
                self.assertEqual(active[0]["operation"], "startup")
                self.assertEqual(active[0]["state"], state)
            retention_id = tracker.create_maintenance_job(
                "retention", {"title": "Retention housekeeping"},
                state="retaining",
            )
            active = tracker.snapshot()["maintenance"]["activeJobs"]
            self.assertEqual(
                [(row["operation"], row["state"]) for row in active],
                [("startup", "startup_cleanup"), ("retention", "retaining")],
            )
            self.assertTrue(tracker.complete_maintenance(retention_id, "accepted"))
            self.assertTrue(tracker.complete_maintenance(job_id, "accepted"))
            snapshot = tracker.snapshot()
            self.assertEqual(snapshot["maintenance"]["activeJobs"], [])
            self.assertEqual(
                snapshot["maintenance"]["recentOutcomes"][0]["outcome"],
                "accepted",
            )

    def test_maintenance_restart_recovers_as_interrupted_once(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "status.json"
            history_path = Path(directory) / "history.jsonl"
            tracker = StatusTracker(snapshot_path, history_path)
            tracker.create_maintenance_job(
                "existing_library_scan",
                {"title": "Existing subtitle library"},
                state="scanning",
            )
            recovered = StatusTracker(snapshot_path, history_path)
            snapshot = recovered.snapshot()
            self.assertEqual(snapshot["maintenance"]["activeJobs"], [])
            self.assertEqual(
                snapshot["maintenance"]["recentOutcomes"][0]["outcome"],
                "interrupted",
            )
            recovered_again = StatusTracker(snapshot_path, history_path)
            self.assertEqual(
                len(recovered_again.snapshot()["maintenance"]["recentOutcomes"]),
                1,
            )

    def test_repair_lifecycle_states_remain_active_cycle_work(self):
        jobs = queue_jobs()
        with tempfile.TemporaryDirectory() as directory:
            tracker = StatusTracker(
                Path(directory) / "status.json",
                Path(directory) / "history.jsonl",
            )
            tracker.start_cycle("cycle-1", 1, jobs)
            key = tracker.active_cycle_job_key("episodes", 42, "et")
            self.assertIsNotNone(key)
            for state in (
                "repair_queued",
                "repair_waiting_capacity",
                "repairing",
                "repair_validating",
            ):
                self.assertTrue(tracker.transition(key, state))
                snapshot = tracker.snapshot()
                self.assertEqual(snapshot["currentCycle"]["repairing"], 1)
                self.assertEqual(snapshot["activeJobs"][0]["state"], state)
                self.assertEqual(snapshot["activeJobs"][0]["workKind"], "cycle")

    def test_maintenance_snapshot_whitelists_public_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = StatusTracker(
                Path(directory) / "status.json",
                Path(directory) / "history.jsonl",
            )
            tracker.create_maintenance_job(
                "cue_repair",
                {
                    "title": "Safe title",
                    "path": "/media/private/show.srt",
                    "prompt": "secret dialogue",
                    "credential": "token-value",
                },
                state="repairing",
                details={
                    "currentAttempt": 1,
                    "repairStage": "secret dialogue /media/private",
                    "responseBody": "unsafe provider response",
                    "sourceContext": "subtitle dialogue",
                },
            )
            job_id = tracker.create_maintenance_job(
                "cue_repair",
                {"title": "Another safe title"},
                state="repairing",
            )
            tracker.complete_maintenance(
                job_id,
                "failed",
                reason="credential token-value",
            )
            payload = json.dumps(tracker.snapshot())
            for unsafe in (
                "/media/private",
                "secret dialogue",
                "token-value",
                "unsafe provider response",
                "subtitle dialogue",
            ):
                self.assertNotIn(unsafe, payload)

    def test_dashboard_assets_render_maintenance_and_adaptive_refresh(self):
        source_root = REPO_ROOT / "docker" / "frontend" / "src" / "dashboard"
        script = "\n".join(path.read_text(encoding="utf-8") for path in source_root.glob("*.tsx"))
        for text in ("maintenance.activeJobs", "Latest maintenance scan", "Health & history", "Waiting for capacity", "Calling Lingarr", "Validating returned cue", "ACTIVE_REFRESH_MS = 3_000", "IDLE_REFRESH_MS = 20_000", "MAX_BACKOFF_MS = 60_000", "/api/status", "mergeRecentOutcomes", "Retry queue", "retryMaxAttempts", "Unlimited", "eligibleCompletedCycle"):
            self.assertIn(text, script)
        app = (source_root / "App.tsx").read_text(encoding="utf-8")
        self.assertLess(app.index("\n      <Overview"), app.index("\n      <Work"))
        self.assertLess(app.index("\n      <Work"), app.index("\n      <HealthHistory"))

    def test_dashboard_assets_render_accessible_validation_observations(self):
        script = (REPO_ROOT / "docker" / "frontend" / "src" / "dashboard" / "Observations.tsx").read_text(encoding="utf-8")
        stylesheet = (REPO_ROOT / "docker" / "static" / "dashboard.css").read_text(encoding="utf-8")
        for text in ("Validation observations", "No copied-source repairs were suppressed.", "Repair skipped", "<details", "expanded", "View evidence"):
            self.assertIn(text, script)
        for selector in (".observation-filters", ".badge-warning", ".observation-details summary:focus-visible", ".observation-evidence"):
            self.assertIn(selector, stylesheet)


if __name__ == "__main__":
    unittest.main()
