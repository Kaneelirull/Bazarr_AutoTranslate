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
    retry_media_identity,
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
                    "upNext", "recentOutcomes", "timing", "circuits",
                    "retryPlans", "completedCycle",
                    "retryMaxAttempts",
                    "history", "maintenance",
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

    def test_dashboard_exposes_configured_display_timezone(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = self.make_tracker(directory)
            with patch.dict("os.environ", {"TZ": "Europe/Tallinn"}):
                page = render_dashboard(tracker.snapshot())
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

    def test_dashboard_assets_use_accessible_reason_tooltips(self):
        script = (
            REPO_ROOT / "docker" / "static" / "dashboard.js"
        ).read_text(encoding="utf-8")
        stylesheet = (
            REPO_ROOT / "docker" / "static" / "dashboard.css"
        ).read_text(encoding="utf-8")

        self.assertIn('data-tooltip-trigger aria-describedby="${tooltipId}"', script)
        self.assertIn('role="tooltip" hidden', script)
        self.assertIn("<strong>Reason</strong>", script)
        self.assertIn("escapeHtml(reason)", script)
        self.assertIn('event.key !== "Escape"', script)
        self.assertIn('document.addEventListener("mouseover"', script)
        self.assertIn("pinnedTooltipTrigger", script)
        self.assertNotIn('class="reason"', script)
        self.assertIn(".badge-tooltip-trigger:focus-visible", stylesheet)
        self.assertIn(".status-tooltip[hidden]", stylesheet)
        self.assertIn("max-width: min(320px, calc(100vw - 24px))", stylesheet)

    def test_dashboard_assets_render_timing_and_protection_states(self):
        script = (
            REPO_ROOT / "docker" / "static" / "dashboard.js"
        ).read_text(encoding="utf-8")
        stylesheet = (
            REPO_ROOT / "docker" / "static" / "dashboard.css"
        ).read_text(encoding="utf-8")

        self.assertIn('toFixed(1)} sec/cue', script)
        self.assertIn('"Cold-start estimate"', script)
        self.assertIn('"Learned average"', script)
        self.assertIn('"File translation"', script)
        self.assertIn('"Cue repair"', script)
        self.assertIn("All series available", script)
        self.assertIn("Protection active", script)
        self.assertIn(
            'failures === 1 ? "failure" : "failures"',
            script,
        )
        self.assertIn('entry.state === "open" || entry.state === "half_open"', script)
        self.assertIn("entry.eligibleAfterCycle", script)
        self.assertIn("Trial ready", script)
        self.assertIn("Trial in progress", script)
        self.assertIn("Trial in ${remaining.toLocaleString()}", script)
        self.assertNotIn("Number(entry.retryAt) * 1000", script)
        self.assertIn('role="status"', script)
        self.assertIn(".diagnostics-content", stylesheet)
        self.assertIn(".timing-grid", stylesheet)
        self.assertIn(".protection-row.is-healthy", stylesheet)
        self.assertIn(".protection-row.is-warning", stylesheet)
        self.assertIn("@media (max-width: 440px)", stylesheet)
        self.assertIn('["Est. total", "estimate"]', script)
        self.assertIn('["Remaining", "eta"]', script)
        self.assertIn("exactTimeMarkup(row.startedAt)", script)
        self.assertIn('new Intl.DateTimeFormat("en-GB"', script)
        self.assertIn('hourCycle: "h23"', script)
        self.assertIn("configuredTimeZone", script)
        self.assertIn("formatRemaining", script)
        self.assertIn("Over by", script)
        self.assertIn(".live-remaining", script)
        self.assertIn('"Retry queue"', script)
        self.assertIn("retryPlans", script)
        self.assertIn('class="data-table retry-table"', script)
        self.assertIn('class="retry-detail-row"', script)
        self.assertIn('class="retry-detail-grid"', script)
        self.assertIn('class="retry-details-toggle"', script)
        self.assertIn('aria-expanded="${expanded ? "true" : "false"}"', script)
        self.assertIn('aria-controls="${escapeHtml(detailId)}"', script)
        self.assertNotIn("<summary>View details</summary>", script)
        self.assertIn('"Due now"', script)
        self.assertIn('"Admitted"', script)
        self.assertIn('"Translating"', script)
        self.assertIn('"Repair queued"', script)
        self.assertIn('"Retry exhausted"', script)
        self.assertIn('"Source blocked"', script)
        self.assertIn("No retry work scheduled.", script)
        self.assertIn('"Waiting for retry"', script)
        self.assertIn('"Circuit protected"', script)
        self.assertIn('"Missing source"', script)
        self.assertIn("detail.category", script)
        self.assertIn("retryMedia", script)
        self.assertNotIn(r"\.srt\s*season\s*\d+", script)
        self.assertNotIn("After cycle", script)
        self.assertIn('return "Due now"', script)
        self.assertIn('"Rescheduled after no progress"', script)
        self.assertIn('return "Next cycle"', script)
        self.assertIn("`In ${cyclesRemaining} Cycles`", script)
        self.assertIn("retryNextAction(plan, completedCycle)", script)
        self.assertIn("Repair at cycle end", script)
        self.assertIn("Waiting for circuit", script)
        self.assertIn("Manual review", script)
        self.assertIn('let retrySortKey = "nextAction"', script)
        self.assertIn("compareRetryPlans", script)
        self.assertIn("expandedRetryIds", script)
        self.assertIn("RETRY_BATCH_SIZE = 20", script)
        self.assertIn("Showing ${visible.length.toLocaleString()} of", script)
        self.assertIn("Show ${Math.min(RETRY_BATCH_SIZE, remaining)", script)
        self.assertIn('data-retry-sort="${escapeHtml(key)}"', script)
        self.assertIn('id="retry-sort-select"', script)
        self.assertIn('aria-live="polite"', script)
        self.assertIn(".retry-details-toggle:focus-visible", stylesheet)
        self.assertIn(".retry-detail-row[hidden]", stylesheet)
        self.assertIn("grid-template-columns: repeat(4, minmax(0, 1fr))", stylesheet)
        self.assertIn(".retry-mobile-sort", stylesheet)
        self.assertIn("@media (min-width: 1400px)", stylesheet)
        self.assertIn("width: min(90%, 2200px)", stylesheet)
        self.assertIn(".retry-table td.cell-details", stylesheet)
        self.assertIn(".time-exact-only", stylesheet)

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
        self.assertIn("Search text", page)
        self.assertIn("New records use UTC timestamps", page)
        self.assertIn('placeholder="Top Gear or job ID"', page)
        self.assertIn('href="/">Status</a>', page)
        self.assertIn('id="theme-toggle"', page)
        self.assertIn('id="refresh-button"', page)
        self.assertIn("border: 1px solid var(--border-default)", stylesheet)
        self.assertIn("background: var(--bg-overlay)", stylesheet)
        self.assertIn(".log-filters input:focus-visible", stylesheet)

        script = (
            REPO_ROOT / "docker" / "static" / "logs.js"
        ).read_text(encoding="utf-8")
        self.assertIn('localStorage.getItem("dashboard-theme")', script)
        self.assertIn('localStorage.setItem("dashboard-theme", value)', script)
        self.assertIn('refresh.addEventListener("click"', script)
        self.assertIn('refresh.textContent = "Refreshing..."', script)
        self.assertIn('output.setAttribute("aria-busy", "true")', script)
        self.assertIn("load(false)", script)
        self.assertIn("load(true)", script)
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
        script = (
            REPO_ROOT / "docker" / "static" / "dashboard.js"
        ).read_text(encoding="utf-8")
        stylesheet = (
            REPO_ROOT / "docker" / "static" / "dashboard.css"
        ).read_text(encoding="utf-8")
        for text in (
            "maintenance.activeJobs",
            "Recent maintenance",
            "Rolling maintenance",
            "Waiting for capacity",
            "Calling Lingarr",
            "Validating returned cue",
            "ACTIVE_REFRESH_MS = 3_000",
            "IDLE_REFRESH_MS = 20_000",
            "MAX_BACKOFF_MS = 60_000",
            "if (requestInFlight || document.hidden) return",
            'aria-label="${escapeHtml(`${operationLabel(row.operation)} progress`)}"',
        ):
            self.assertIn(text, script)
        self.assertIn(".badge.maintenance-work", stylesheet)
        self.assertIn(".job-progress progress", stylesheet)


if __name__ == "__main__":
    unittest.main()
