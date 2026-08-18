import os
import io
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "docker"))
os.environ.setdefault("BAZARR_URL", "http://bazarr:6767")
os.environ.setdefault("BAZARR_API_KEY", "test")
os.environ.setdefault("LINGARR_URL", "http://lingarr:8080")
os.environ.setdefault(
    "LOG_DIR", str(Path(tempfile.gettempdir()) / "bazarr-autotranslate-tests")
)

from autotranslate.config import Config  # noqa: E402
from autotranslate.production import load_runtime  # noqa: E402
import autotranslate.subtitles.library as cleanup  # noqa: E402
from autotranslate.persistence.state_store import StateStore  # noqa: E402
from autotranslate.persistence.state_store import StateStoreError  # noqa: E402

app = load_runtime(Config.from_env(), None)


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"HTTP {self.status_code}", response=self
            )

    def json(self):
        return self.payload


class ServiceReliabilityTests(unittest.TestCase):
    def setUp(self):
        self._state_directory = tempfile.TemporaryDirectory()
        app._validation_state = StateStore(
            Path(self._state_directory.name) / "state.sqlite3",
            validator_version=cleanup.VALIDATOR_VERSION,
        )

    def tearDown(self):
        app._translation_capacity.reset()
        app._validation_state.close()
        app._validation_state = None
        self._state_directory.cleanup()

    def test_complementary_quarantine_attempts_recover_before_regeneration(self):
        root = Path(self._state_directory.name)
        source = root / "show.eng.srt"
        target = root / "show.et.srt"
        video = root / "show.mkv"
        first = root / "quarantine-1.et.srt"
        second = root / "quarantine-2.et.srt"
        source.write_text(cleanup.render_srt_cues([
            cleanup.SubtitleCue(1, "00:00:01,000 --> 00:00:01,900", ["This is the first ordinary dialogue sentence"]),
            cleanup.SubtitleCue(2, "00:00:02,000 --> 00:00:02,900", ["This is the second ordinary dialogue sentence"]),
        ]), encoding="utf-8")
        first.write_text(cleanup.render_srt_cues([
            cleanup.SubtitleCue(1, "00:00:01,000 --> 00:00:01,900", ["See on esimene tavaline dialoogilause"]),
            cleanup.SubtitleCue(2, "00:00:02,000 --> 00:00:02,900", ["This is the second ordinary dialogue sentence"]),
        ]), encoding="utf-8")
        second.write_text(cleanup.render_srt_cues([
            cleanup.SubtitleCue(1, "00:00:01,000 --> 00:00:01,900", ["This is the first ordinary dialogue sentence"]),
            cleanup.SubtitleCue(2, "00:00:02,000 --> 00:00:02,900", ["See on teine tavaline dialoogilause"]),
        ]), encoding="utf-8")
        video.write_bytes(b"video")
        source_hash = cleanup.file_sha256(source)
        plan, _ = app._validation_state.schedule_retry_plan(
            item_type="episodes", item_id=22, target_language="et",
            source_hash=source_hash, source_path=source, source_language="en",
            target_path=target, series_key="sonarr:1", series_title="Show",
            media_title="Show S01E01", failure_class="whole_file",
            rules=["target_structure"], state="regeneration_waiting",
            eligible_completed_cycle=0,
        )
        signatures = cleanup.source_cue_signatures(source)
        for number, artifact in enumerate((first, second), start=1):
            app._validation_state.record_quarantine_attempt(
                item_type="episodes", item_id=22, target_language="et",
                source_hash=source_hash, target_hash=cleanup.file_sha256(artifact),
                attempt_number=number, artifact_path=artifact, report_path=None,
                failure_rules=["copied_source"], cue_signatures=signatures,
            )
        stats = {}
        detector = Mock()
        detector.detect_language_of.return_value = cleanup.Language.ESTONIAN
        detector.compute_language_confidence.return_value = 1.0
        with (
            patch.object(app, "_get_cleanup_detector", return_value=detector),
            patch.object(app, "lingarr_translate_line", side_effect=AssertionError("provider called")) as provider,
        ):
            app._run_quarantine_recoveries(stats)

        self.assertTrue(target.exists())
        recovered = target.read_text(encoding="utf-8")
        self.assertIn("See on esimene tavaline dialoogilause", recovered)
        self.assertIn("See on teine tavaline dialoogilause", recovered)
        provider.assert_not_called()
        self.assertEqual(app._validation_state.retry_plan(plan["id"])["state"], "accepted_after_donor_recovery")
        self.assertEqual(stats["ensemble_recovered"], 1)

    def test_tracked_bazarr_sync_closes_status_on_success_and_exception(self):
        with (
            patch.object(app, "_status_create_maintenance", return_value="sync-job") as create,
            patch.object(app, "_status_complete_maintenance") as complete,
            patch.object(app, "trigger_bazarr_sync") as trigger,
            patch.object(app, "wait_for_bazarr_sync", return_value=True) as wait,
        ):
            self.assertTrue(app._tracked_bazarr_sync(True, True, 45))
        create.assert_called_once_with(
            "bazarr_sync", {"title": "Series and movies"}, state="synchronizing"
        )
        trigger.assert_called_once_with(True, True)
        wait.assert_called_once_with(True, True, 45)
        complete.assert_called_once_with(
            "sync-job", "accepted", reason=None
        )

        with (
            patch.object(app, "_status_create_maintenance", return_value="sync-job"),
            patch.object(app, "_status_complete_maintenance") as complete,
            patch.object(app, "trigger_bazarr_sync", side_effect=RuntimeError("offline")),
        ):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                app._tracked_bazarr_sync(True, False, 45)
        complete.assert_called_once_with(
            "sync-job", "failed", reason="Bazarr synchronization failed"
        )

    def test_request_json_retries_transient_failures_with_bounded_backoff(self):
        response = FakeResponse({"data": []})
        with (
            patch.object(
                app.requests,
                "get",
                side_effect=[
                    requests.ConnectionError("offline"),
                    requests.Timeout("slow"),
                    response,
                ],
            ) as request,
            patch.object(app.time, "sleep") as sleep,
        ):
            payload = app._request_json(
                "get",
                "http://bazarr/api/movies/wanted",
                service="Bazarr",
                operation="fetch movies wanted queue",
                timeout=10,
            )

        self.assertEqual(payload, {"data": []})
        self.assertEqual(request.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(1), call(2)])

    def test_runtime_rejects_typed_config_it_cannot_honor(self):
        config = Config.from_env()
        changed = replace(config, check_interval=config.check_interval + 10)
        with self.assertRaisesRegex(RuntimeError, "check_interval"):
            app.main(changed)

    def test_request_json_does_not_retry_permanent_client_failure(self):
        with (
            patch.object(
                app.requests, "get", return_value=FakeResponse({}, status_code=401)
            ) as request,
            patch.object(app.time, "sleep") as sleep,
        ):
            with self.assertRaises(app.ServiceRequestError):
                app._request_json(
                    "get",
                    "http://bazarr/api/movies/wanted",
                    service="Bazarr",
                    operation="fetch movies wanted queue",
                    timeout=10,
                )

        request.assert_called_once()
        sleep.assert_not_called()

    def test_bazarr_empty_queue_is_distinct_from_failure(self):
        with patch.object(app, "_request_json", return_value={"data": []}):
            self.assertEqual(app.fetch_wanted("movies"), [])
        with patch.object(
            app,
            "_request_json",
            side_effect=app.ServiceRequestError("Bazarr", "wanted", "offline"),
        ):
            with self.assertRaises(app.ServiceRequestError):
                app.fetch_wanted("movies")

    def test_cycle_reports_partial_bazarr_outage_as_degraded(self):
        def wanted(item_type):
            if item_type == "episodes":
                raise app.ServiceRequestError("Bazarr", "episodes wanted", "offline")
            return []

        output = io.StringIO()
        with (
            patch.multiple(
                app,
                _status_tracker=None,
                _pending_repairs={},
                lingarr_build_media_cache=lambda: None,
                lingarr_get_active_translations=lambda: [],
                fetch_wanted=wanted,
                _take_pending_prune_videos=lambda: {},
                _drain_lingarr_queue=lambda: True,
                _status_finish_cycle=lambda _metrics=None: None,
            ),
            redirect_stdout(output),
        ):
            app.run_cycle(1)

        logs = output.getvalue()
        self.assertIn("unavailable queue(s): episodes", logs)
        self.assertIn("Cycle state: degraded", logs)
        self.assertNotIn("No wanted items found", logs)

    def test_subtitle_lookup_failure_defers_item(self):
        item = {
            "radarrId": 7,
            "title": "Movie",
            "missing_subtitles": [{"code2": "et"}],
        }
        stats = {
            "deferred": 0,
            "api_errors": 0,
            "translations": [],
            "episode_activity": False,
            "movie_activity": False,
        }
        with patch.object(
            app,
            "fetch_subtitles",
            side_effect=app.ServiceRequestError("Bazarr", "subtitles", "offline"),
        ):
            app.process_item(item, "movies", "radarrId", stats, threading.Lock())

        self.assertEqual(stats["deferred"], 1)
        self.assertEqual(stats["api_errors"], 1)

    def test_persistence_failure_prevents_lingarr_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "movie.mkv"
            source = root / "movie.en.srt"
            video.write_bytes(b"video")
            source.write_text(
                "1\n00:00:01,000 --> 00:00:01,900\nEnglish\n",
                encoding="utf-8",
            )
            item = {
                "radarrId": 7,
                "title": "Movie",
                "missing_subtitles": [{"code2": "et"}],
            }
            stats = {
                "deferred": 0,
                "api_errors": 0,
                "translations": [],
                "episode_activity": False,
                "movie_activity": False,
            }
            submit = Mock()
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
                _count_dialogue_lines=lambda _path: 1,
                _estimate_timeout=lambda _path: 60,
                _record_submission=Mock(
                    side_effect=StateStoreError("disk unavailable")
                ),
            ):
                app.process_item(
                    item, "movies", "radarrId", stats, threading.Lock()
                )

            submit.assert_not_called()
            self.assertEqual(stats["deferred"], 1)

    def test_repair_defers_if_source_changed_while_queued(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "movie.en.srt"
            target = root / "movie.et.srt"
            source.write_text(
                "1\n00:00:01,000 --> 00:00:01,900\nOriginal\n",
                encoding="utf-8",
            )
            target.write_text(
                "1\n00:00:01,000 --> 00:00:01,900\nKatki\n",
                encoding="utf-8",
            )
            expected_source = app._file_hash_or_none(source)
            expected_target = app._file_hash_or_none(target)
            source.write_text(
                "1\n00:00:01,000 --> 00:00:01,900\nChanged\n",
                encoding="utf-8",
            )
            report = SimpleNamespace(repairable_cue_indexes=[0])
            translate_line = Mock()
            with (
                patch.object(app, "_get_cleanup_detector", return_value=object()),
                patch.object(
                    cleanup,
                    "target_language_for_code",
                    return_value=SimpleNamespace(),
                ),
                patch.object(app, "lingarr_translate_line", translate_line),
            ):
                result = app._perform_repair(
                    str(source),
                    str(target),
                    "en",
                    "et",
                    None,
                    "Movie",
                    "movies",
                    report,
                    expected_target,
                    expected_source_hash=expected_source,
                )

            self.assertEqual(result.action, "repair-deferred")
            translate_line.assert_not_called()

    def test_moved_source_requires_matching_hash_and_language(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            moved = root / "movie.eng.hi.srt"
            moved.write_text(
                "1\n00:00:01,000 --> 00:00:01,900\nEnglish\n",
                encoding="utf-8",
            )
            metadata = {
                "sourcePath": str(root / "movie.en.srt"),
                "sourceHash": app._file_hash_or_none(moved),
                "sourceLanguage": "en",
            }
            target = root / "movie.et.hi.srt"

            self.assertTrue(
                app._submission_matches_source(
                    metadata, str(moved), "en", target, "et"
                )
            )
            self.assertFalse(
                app._submission_matches_source(
                    metadata, str(moved), "sv", target, "et"
                )
            )
            moved.write_text(
                "1\n00:00:01,000 --> 00:00:01,900\nChanged\n",
                encoding="utf-8",
            )
            self.assertFalse(
                app._submission_matches_source(
                    metadata, str(moved), "en", target, "et"
                )
            )

    def test_lingarr_language_schema_is_normalized(self):
        payload = [
            {"name": "English", "code": "en", "targets": ["et", "sv"]},
            {"name": "Broken", "code": None, "targets": []},
            "invalid",
        ]
        with patch.object(app, "_request_json", return_value=payload):
            languages = app.lingarr_get_languages()

        self.assertEqual(
            languages,
            [app.LingarrSourceLanguage("English", "en", ("et", "sv"))],
        )

    def test_lingarr_active_schema_rejects_malformed_payload(self):
        with patch.object(app, "_request_json", return_value={"count": 1}):
            with self.assertRaises(app.ServiceRequestError):
                app.lingarr_get_active_translations()
        with patch.object(
            app,
            "_request_json",
            return_value=[{"mediaId": 1, "mediaType": "Movie"}],
        ):
            with self.assertRaises(app.ServiceRequestError):
                app.lingarr_get_active_translations()

    def test_capacity_gate_counts_external_work_and_local_reservations(self):
        gate = app.TranslationCapacityGate(2)
        external = [
            app.LingarrActiveTranslation(1, "Movie", "InProgress")
        ]
        second_acquired = threading.Event()
        second_token = []

        with patch.object(app, "lingarr_get_active_translations", return_value=external):
            first = gate.acquire(2, "Movie")
            self.assertIsNotNone(first)

            def acquire_second():
                second_token.append(gate.acquire(3, "Movie"))
                second_acquired.set()

            worker = threading.Thread(target=acquire_second)
            worker.start()
            time.sleep(0.05)
            self.assertFalse(second_acquired.is_set())

            gate.release(first)
            self.assertTrue(second_acquired.wait(1))
            gate.release(second_token[0])
            worker.join(1)

    def test_capacity_gate_fails_closed_when_active_state_is_unavailable(self):
        gate = app.TranslationCapacityGate(2)
        with patch.object(
            app,
            "lingarr_get_active_translations",
            side_effect=app.ServiceRequestError("Lingarr", "active", "offline"),
        ):
            self.assertIsNone(gate.acquire(2, "Movie"))

    def test_unverified_explicit_origin_uses_target_only_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "movie.eng.srt"
            target = root / "movie.et.srt"
            source.write_text(
                "1\n00:00:01,000 --> 00:00:01,900\nOne\n\n"
                "2\n00:00:02,000 --> 00:00:02,900\nTwo\n",
                encoding="utf-8",
            )
            target.write_text(
                "1\n00:00:01,000 --> 00:00:01,900\nTere maailm\n",
                encoding="utf-8",
            )
            state = app._validation_state

            with patch.multiple(
                app, CLEANUP_LANGUAGES={"et"}, _validation_state=state
            ):
                action, report = app._validate_translated_file(
                    str(source),
                    str(target),
                    "en",
                    "et",
                    None,
                    dry_run=True,
                    origin="lingarr",
                    provenance_source_hash="different-source",
                )

        self.assertEqual(action, "valid")
        self.assertTrue(report.valid)

    def test_cleanup_statistics_do_not_double_count_undersized(self):
        report = SimpleNamespace(
            issues=[
                SimpleNamespace(rule="excessive_lines"),
                SimpleNamespace(rule="undersized_subtitle"),
                SimpleNamespace(rule="prompt_marker"),
            ]
        )
        stats = {}
        app._record_cleanup_stats(stats, "quarantined", report)

        self.assertEqual(stats["cleanup_excessive_lines"], 1)
        self.assertEqual(stats["cleanup_undersized_targets"], 1)
        self.assertEqual(stats["cleanup_other_issues"], 1)

    def test_managed_file_contract_calls_chown_then_chmod(self):
        target = Path("subtitle.srt")
        with (
            patch.object(cleanup.os, "name", "posix"),
            patch.object(cleanup.os, "chown", create=True) as chown,
            patch.object(cleanup.os, "chmod") as chmod,
        ):
            cleanup.normalize_managed_file(target)

        chown_path, uid, gid = chown.call_args.args
        chmod_path, mode = chmod.call_args.args
        self.assertEqual(str(chown_path), str(target))
        self.assertEqual((uid, gid), (568, 568))
        self.assertEqual(str(chmod_path), str(target))
        self.assertEqual(mode, 0o664)

    def test_managed_replace_preserves_original_on_permission_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "movie.et.srt"
            candidate = root / ".movie.et.srt.tmp"
            target.write_text("original", encoding="utf-8")
            candidate.write_text("replacement", encoding="utf-8")

            with patch(
                "autotranslate.subtitles.foundation.normalize_managed_file",
                side_effect=PermissionError("chown denied"),
            ):
                with self.assertRaises(PermissionError):
                    app._replace_managed_file(candidate, target)

            self.assertEqual(target.read_text(encoding="utf-8"), "original")
            self.assertFalse(candidate.exists())

    def test_quarantine_and_report_are_normalized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "movie.et.srt"
            source.write_text("subtitle", encoding="utf-8")
            quarantine = root / "quarantine"

            with patch.object(cleanup, "normalize_managed_file") as normalize:
                destination = cleanup.quarantine_subtitle(
                    source, [root], quarantine
                )
                report = cleanup.write_validation_report(
                    destination, {"valid": False}
                )

            self.assertFalse(source.exists())
            self.assertTrue(destination.exists())
            self.assertTrue(report.exists())
            self.assertEqual(normalize.call_args_list[0], call(source))
            self.assertEqual(len(normalize.call_args_list), 2)

    def _bound_retry_circuit(self, source: Path, target: Path):
        state = app._validation_state
        plan, _ = state.schedule_retry_plan(
            item_type="episodes", item_id=42, target_language="et",
            source_hash="source", source_path=source, source_language="en",
            target_path=target, failure_class="cue_repairable",
            rules=["excessive_lines"], eligible_completed_cycle=0,
            state="repair_retry_queued", series_key="sonarr:1",
            series_title="Top Gear", media_title="Top Gear S01E01",
        )
        state.record_circuit_outcome(
            series_key="sonarr:1", series_title="Top Gear", success=False,
            reason="invalid", threshold=1, open_cycles=1,
            config_fingerprint=app._CIRCUIT_CONFIG_FINGERPRINT,
        )
        completed = state.advance_completed_cycle()
        trial = state.circuit_permission(
            series_key="sonarr:1", series_title="Top Gear",
            config_fingerprint=app._CIRCUIT_CONFIG_FINGERPRINT,
            trial_owner="attempt:1",
        )
        state.bind_circuit_trial_job(
            "sonarr:1", "attempt:1", 99, trial_plan_id=plan["id"],
            lease_generation=trial["leaseGeneration"],
        )
        return state, plan, completed

    def test_reconciliation_closes_accepted_validation_pending_trial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "show.en.srt"
            target = root / "show.et.srt"
            state, plan, _ = self._bound_retry_circuit(source, target)
            state.resolve_retry_plan(plan["id"], "source", outcome="accepted_after_retry")

            with patch.object(app, "lingarr_get_job") as get_job:
                self.assertEqual(app._reconcile_circuit_trial_leases(state), 1)

            get_job.assert_not_called()
            self.assertEqual(state.circuit_breakers(), [])

    def test_reconciliation_releases_completed_trial_waiting_for_fresh_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, plan, _ = self._bound_retry_circuit(root / "show.en.srt", root / "show.et.srt")
            state.update_retry_plan(
                plan["id"], state="regeneration_waiting",
                eligible_completed_cycle=app._completed_cycle,
                reason="completed retry output awaiting validation",
            )

            with patch.object(app, "lingarr_get_job", return_value={"status": "Completed"}):
                self.assertEqual(app._reconcile_circuit_trial_leases(state), 1)

            retry = state.retry_plan(plan["id"])
            circuit = state.circuit_breakers()[0]
            self.assertEqual(retry["state"], "regeneration_waiting")
            self.assertEqual(circuit["state"], "open")
            self.assertIsNone(circuit["trialJobId"])

    def test_reconciliation_retains_completed_trial_with_active_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, _plan, _ = self._bound_retry_circuit(root / "show.en.srt", root / "show.et.srt")

            with patch.object(app, "lingarr_get_job", return_value={"status": "Completed"}):
                self.assertEqual(app._reconcile_circuit_trial_leases(state), 1)

            circuit = state.circuit_breakers()[0]
            self.assertEqual(circuit["state"], "half_open")
            self.assertEqual(circuit["trialState"], "validation_pending")

    def test_restart_reconciliation_retains_trial_owned_by_durable_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "show.en.srt"
            target = root / "show.et.srt"
            source.write_text("source", encoding="utf-8")
            target.write_text("target", encoding="utf-8")
            state, plan, _ = self._bound_retry_circuit(source, target)
            trial = state.circuit_trial_for_retry_plan(plan["id"])
            repair_id = state.enqueue_repair_job(
                dedupe_key="restart-owned", item_type="episodes", item_id=42,
                target_language="et", source_path=source, target_path=target,
                source_hash=app._file_hash_or_none(source),
                target_hash=app._file_hash_or_none(target),
                payload={
                    "retryPlanId": plan["id"],
                    "trialGeneration": trial["leaseGeneration"],
                },
            )
            state.transition_repair_job(repair_id, "persisted_for_restart", expected_states=("queued",))
            state.update_retry_plan(plan["id"], state="regeneration_waiting")

            with patch.object(app, "lingarr_get_job", return_value={"status": "Completed"}):
                self.assertEqual(app._reconcile_circuit_trial_leases(state), 1)

            circuit = state.circuit_breakers()[0]
            self.assertEqual(circuit["state"], "half_open")
            self.assertEqual(circuit["trialState"], "validation_pending")

    def test_bound_retry_terminal_output_deferrals_release_trial(self):
        reasons = (
            "completed output missing",
            "managed file ownership failed",
            "completed output provenance persistence failed",
        )
        for reason in reasons:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                app._validation_state.close()
                app._validation_state = StateStore(
                    root / "state.sqlite3",
                    validator_version=cleanup.VALIDATOR_VERSION,
                )
                state, plan, _ = self._bound_retry_circuit(root / "show.en.srt", root / "show.et.srt")
                trial = state.circuit_trial_for_retry_plan(plan["id"])

                app._defer_bound_retry_trial(plan, True, trial["leaseGeneration"], reason)

                retry = state.retry_plan(plan["id"])
                self.assertEqual(retry["state"], "regeneration_waiting")
                self.assertEqual(retry["lastDeferralClass"], "output_deferred")
                self.assertEqual(state.circuit_breakers()[0]["state"], "open")

    def test_existing_retry_success_resolves_plan_without_bound_trial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = app._validation_state
            plan, _ = state.schedule_retry_plan(
                item_type="episodes", item_id=42, target_language="et",
                source_hash="source", source_path=root / "show.en.srt",
                source_language="en", target_path=root / "show.et.srt",
                failure_class="whole_file", rules=["target_structure"],
                state="regeneration_waiting", eligible_completed_cycle=0,
                series_key="sonarr:1", series_title="Top Gear",
            )
            state.record_circuit_outcome(
                series_key="sonarr:1", series_title="Top Gear", success=False,
                reason="invalid", threshold=1, open_cycles=1,
                config_fingerprint=app._CIRCUIT_CONFIG_FINGERPRINT,
            )

            self.assertTrue(app._resolve_existing_retry_success(
                plan, "sonarr:1", "Top Gear",
            ))

            self.assertEqual(
                state.retry_plan(plan["id"])["state"], "accepted_after_retry"
            )
            self.assertEqual(state.circuit_breakers(), [])

    def test_existing_retry_success_closes_matching_bound_trial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, plan, _ = self._bound_retry_circuit(
                root / "show.en.srt", root / "show.et.srt"
            )

            self.assertTrue(app._resolve_existing_retry_success(
                plan, "sonarr:1", "Top Gear",
            ))

            self.assertEqual(
                state.retry_plan(plan["id"])["state"], "accepted_after_retry"
            )
            self.assertEqual(state.circuit_breakers(), [])

    def test_existing_retry_stale_settlement_reopens_matching_trial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, plan, _ = self._bound_retry_circuit(
                root / "show.en.srt", root / "show.et.srt"
            )
            replacement, _ = state.schedule_retry_plan(
                item_type="episodes", item_id=42, target_language="et",
                source_hash="changed", source_path=root / "show.en.srt",
                source_language="en", target_path=root / "show.et.srt",
                failure_class="whole_file", rules=["target_structure"],
                state="regeneration_waiting", eligible_completed_cycle=0,
                series_key="sonarr:1", series_title="Top Gear",
            )

            self.assertFalse(app._resolve_existing_retry_success(
                plan, "sonarr:1", "Top Gear",
            ))

            self.assertEqual(state.retry_plan(plan["id"])["state"], "superseded")
            self.assertEqual(
                state.retry_plan(replacement["id"])["state"],
                "regeneration_waiting",
            )
            self.assertEqual(state.circuit_breakers()[0]["state"], "open")

    def test_existing_retry_stale_unbound_settlement_is_circuit_no_op(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = app._validation_state
            stale, _ = state.schedule_retry_plan(
                item_type="episodes", item_id=42, target_language="et",
                source_hash="old", source_path=root / "show.en.srt",
                source_language="en", target_path=root / "show.et.srt",
                failure_class="whole_file", rules=["target_structure"],
                state="regeneration_waiting", eligible_completed_cycle=0,
                series_key="sonarr:1", series_title="Top Gear",
            )
            state.record_circuit_outcome(
                series_key="sonarr:1", series_title="Top Gear", success=False,
                reason="invalid", threshold=1, open_cycles=3,
                config_fingerprint=app._CIRCUIT_CONFIG_FINGERPRINT,
            )
            state.schedule_retry_plan(
                item_type="episodes", item_id=42, target_language="et",
                source_hash="new", source_path=root / "show.en.srt",
                source_language="en", target_path=root / "show.et.srt",
                failure_class="whole_file", rules=["target_structure"],
                state="regeneration_waiting", eligible_completed_cycle=0,
                series_key="sonarr:1", series_title="Top Gear",
            )

            self.assertFalse(app._resolve_existing_retry_success(
                stale, "sonarr:1", "Top Gear",
            ))

            circuit = state.circuit_breakers()[0]
            self.assertEqual(circuit["state"], "open")
            self.assertEqual(circuit["failures"], 1)

    def test_end_cycle_repair_success_closes_linked_trial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "show.en.srt"
            target = root / "show.et.srt"
            source.write_text("source", encoding="utf-8")
            target.write_text("target", encoding="utf-8")
            state, plan, completed = self._bound_retry_circuit(source, target)
            with (
                patch.object(app, "_completed_cycle", completed),
                patch.object(app, "_validate_translated_file", return_value=("repaired", SimpleNamespace(manual_review=False))),
                patch.object(app, "_resolve_retry_success", side_effect=lambda plan_id, source_hash, **kwargs: state.resolve_retry_plan(plan_id, source_hash, **kwargs)),
                patch.object(app, "_refresh_status_diagnostics"),
            ):
                app._run_end_cycle_repair_retries({})

            self.assertEqual(state.circuit_breakers(), [])
            self.assertEqual(state.retry_plan(plan["id"])["state"], "accepted_after_retry")

    def test_end_cycle_transient_defer_remains_retryable_and_releases_trial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "show.en.srt"
            target = root / "show.et.srt"
            source.write_text("source", encoding="utf-8")
            target.write_text("target", encoding="utf-8")
            state, plan, completed = self._bound_retry_circuit(source, target)
            with (
                patch.object(app, "_completed_cycle", completed),
                patch.object(app, "_validate_translated_file", return_value=("repair-deferred", SimpleNamespace(manual_review=False))),
                patch.object(app, "_refresh_status_diagnostics"),
            ):
                app._run_end_cycle_repair_retries({})

            retry = state.retry_plan(plan["id"])
            self.assertEqual(retry["state"], "regeneration_waiting")
            self.assertEqual(retry["lastDeferralClass"], "repair_deferred")
            self.assertEqual(state.circuit_breakers()[0]["state"], "open")

    def test_end_cycle_manual_review_remains_excluded_and_releases_trial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "show.en.srt"
            target = root / "show.et.srt"
            source.write_text("source", encoding="utf-8")
            target.write_text("target", encoding="utf-8")
            state, plan, completed = self._bound_retry_circuit(source, target)
            with (
                patch.object(app, "_completed_cycle", completed),
                patch.object(app, "_validate_translated_file", return_value=("repair-deferred", SimpleNamespace(manual_review=True))),
                patch.object(app, "_refresh_status_diagnostics"),
            ):
                app._run_end_cycle_repair_retries({})

            retry = state.retry_plan(plan["id"])
            self.assertEqual(retry["lastDeferralClass"], "manual_review")
            self.assertEqual(state.circuit_breakers()[0]["state"], "open")

    def test_repair_worker_exception_reschedules_retry_and_releases_trial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, plan, completed = self._bound_retry_circuit(root / "show.en.srt", root / "show.et.srt")
            trial = state.circuit_trial_for_retry_plan(plan["id"])
            future = app.Future()
            future.set_exception(RuntimeError("worker crashed"))
            metadata = {
                "retry_plan_id": plan["id"],
                "trial_generation": trial["leaseGeneration"],
                "status_lock": threading.Lock(),
            }
            with (
                patch.object(app, "_completed_cycle", completed),
                patch.object(app, "_complete_repair_status"),
                patch.object(app, "_scan_child_finished"),
                patch.object(app, "_refresh_status_diagnostics"),
            ):
                app._publish_repair_status(future, metadata)

            retry = state.retry_plan(plan["id"])
            self.assertEqual(retry["state"], "regeneration_waiting")
            self.assertEqual(retry["lastDeferralClass"], "worker_exception")
            self.assertEqual(state.circuit_breakers()[0]["state"], "open")

    def test_stale_quarantined_repair_cannot_mutate_current_trial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, plan, _ = self._bound_retry_circuit(root / "show.en.srt", root / "show.et.srt")
            trial = state.circuit_trial_for_retry_plan(plan["id"])
            before = state.circuit_breakers()[0]
            future = app.Future()
            future.set_result(app.RepairJobResult(
                "quarantined", SimpleNamespace(issues=[]), "Top Gear", "et",
                "episodes", 42, target_path=str(root / "show.et.srt"),
            ))
            metadata = {
                "retry_plan_id": plan["id"],
                "trial_generation": trial["leaseGeneration"] + 1,
                "series_key": "sonarr:1", "series_title": "Top Gear",
                "status_lock": threading.Lock(),
            }
            with (
                patch.object(app, "_schedule_validation_retry") as schedule,
                patch.object(app, "_complete_repair_status"),
                patch.object(app, "_scan_child_finished"),
                patch.object(app, "_refresh_status_diagnostics"),
            ):
                app._publish_repair_status(future, metadata)

            schedule.assert_not_called()
            after = state.circuit_breakers()[0]
            self.assertEqual(after["state"], "half_open")
            self.assertEqual(after["failures"], before["failures"])
            self.assertEqual(
                state.circuit_trial_for_retry_plan(plan["id"])["leaseGeneration"],
                trial["leaseGeneration"],
            )

    def test_preworker_repair_queue_deferral_reschedules_and_releases_trial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, plan, _ = self._bound_retry_circuit(root / "show.en.srt", root / "show.et.srt")
            trial = state.circuit_trial_for_retry_plan(plan["id"])
            report = SimpleNamespace(repairable_cue_indexes=[0], issues=[])
            job_kwargs = {
                "retry_plan_id": plan["id"],
                "trial_generation": trial["leaseGeneration"],
                "expected_source_hash": "source",
                "target_lang": "et",
            }
            with (
                patch.object(app, "_repair_capacity", threading.BoundedSemaphore(0)),
                patch.object(app, "_status_create_repair_ref", return_value={}),
                patch.object(app, "_status_ref_complete"),
                patch.object(app, "_refresh_status_diagnostics"),
            ):
                self.assertEqual(
                    app._queue_repair(("queue-full",), job_kwargs, report, "Top Gear", "et"),
                    "repair-deferred",
                )

            retry = state.retry_plan(plan["id"])
            self.assertEqual(retry["state"], "regeneration_waiting")
            self.assertEqual(retry["lastDeferralClass"], "repair_deferred")
            self.assertEqual(state.circuit_breakers()[0]["state"], "open")

    def test_restart_changed_repair_inputs_reschedule_and_release_trial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "show.en.srt"
            target = root / "show.et.srt"
            source.write_text("changed", encoding="utf-8")
            target.write_text("target", encoding="utf-8")
            state, plan, completed = self._bound_retry_circuit(source, target)
            trial = state.circuit_trial_for_retry_plan(plan["id"])
            repair_id = state.enqueue_repair_job(
                dedupe_key="restart:42", item_type="episodes", item_id=42,
                target_language="et", source_path=source, target_path=target,
                source_hash="old-source", target_hash=app._file_hash_or_none(target),
                payload={
                    "retryPlanId": plan["id"],
                    "trialGeneration": trial["leaseGeneration"],
                },
            )
            state.transition_repair_job(repair_id, "persisted_for_restart", expected_states=("queued",))

            with patch.object(app, "_completed_cycle", completed):
                self.assertEqual(app._requeue_persisted_repairs(state), 0)

            retry = state.retry_plan(plan["id"])
            self.assertEqual(retry["state"], "regeneration_waiting")
            self.assertEqual(retry["lastDeferralClass"], "repair_inputs_changed")
            self.assertEqual(state.circuit_breakers()[0]["state"], "open")

    def test_regeneration_delay_is_indefinite_and_capped(self):
        with (
            patch.object(app, "REGENERATION_INITIAL_DELAY_CYCLES", 2),
            patch.object(app, "REGENERATION_BACKOFF_MULTIPLIER", 2.0),
            patch.object(app, "REGENERATION_MAX_DELAY_CYCLES", 16),
        ):
            self.assertEqual(
                [app._regeneration_delay_cycles(index) for index in range(7)],
                [2, 4, 8, 16, 16, 16, 16],
            )

    def test_regeneration_retries_dispatch_in_parallel_with_capacity_two(self):
        plans = [
            {
                "id": index,
                "itemType": "episodes",
                "itemId": 100 + index,
                "targetLanguage": "et",
                "attemptCount": 0,
                "seriesKey": f"sonarr:{index}",
                "seriesTitle": f"Show {index}",
                "mediaTitle": f"Show {index} S01E0{index}",
                "sourcePath": f"/media/Show {index} - S01E0{index}.en.srt",
            }
            for index in range(1, 4)
        ]
        state = Mock()
        state.due_retry_count.return_value = 3
        state.claim_due_retry_plans.return_value = plans
        state.retry_plans.return_value = [
            {**plan, "state": "accepted_after_regeneration"}
            for plan in plans
        ]
        gate = threading.Semaphore(2)
        lock = threading.Lock()
        active = 0
        maximum = 0
        started = threading.Event()
        release = threading.Event()

        def process(*_args, **_kwargs):
            nonlocal active, maximum
            with gate:
                with lock:
                    active += 1
                    maximum = max(maximum, active)
                    if maximum == 2:
                        started.set()
                release.wait(2)
                with lock:
                    active -= 1

        def unblock():
            self.assertTrue(started.wait(2))
            release.set()

        waiter = threading.Thread(target=unblock)
        waiter.start()
        stats = {}
        with (
            patch.object(app, "_get_validation_state", return_value=state),
            patch.object(app, "process_item", side_effect=process),
            patch.object(app, "_status_admit_retry") as admit,
            patch.object(app, "PARALLEL_TRANSLATES", 2),
            patch.object(
                app._shared_capacity, "release_current_translation"
            ) as release_capacity,
        ):
            app._run_regeneration_retries(stats)
        waiter.join(2)

        self.assertEqual(maximum, 2)
        self.assertEqual(admit.call_count, 3)
        self.assertEqual(release_capacity.call_count, 3)
        self.assertEqual(stats["regeneration_queued"], 3)

    def test_no_progress_retry_does_not_consume_same_series_submission_slot(self):
        plans = [
            {
                "id": index,
                "itemType": "episodes",
                "itemId": 100 + index,
                "targetLanguage": "et",
                "attemptCount": 0,
                "seriesKey": "sonarr:1",
                "canonicalSeriesKey": "sonarr:1",
                "seriesTitle": "Show",
                "mediaTitle": f"Show episode {index}",
                "sourcePath": f"/media/show-{index}.en.srt",
            }
            for index in (1, 2)
        ]
        state = Mock()
        state.due_retry_count.return_value = 2
        state.retry_plans.return_value = [
            {**plan, "state": "accepted_after_regeneration"}
            for plan in plans
        ]
        admission_snapshots = []

        def claim(*_args, series_admissions=None, **_kwargs):
            admission_snapshots.append(dict(series_admissions or {}))
            index = len(admission_snapshots) - 1
            return [plans[index]] if index < len(plans) else []

        state.claim_due_retry_plans.side_effect = claim

        def process(*_args, retry_plan=None, retry_submission_callback=None, **_kwargs):
            if retry_plan["id"] == 2:
                retry_submission_callback(retry_plan)

        with (
            patch.object(app, "_get_validation_state", return_value=state),
            patch.object(app, "process_item", side_effect=process),
            patch.object(app, "_status_admit_retry"),
            patch.object(app._shared_capacity, "release_current_translation"),
        ):
            app._run_regeneration_retries({}, submission_budget=2)

        self.assertEqual(admission_snapshots[0], {})
        self.assertEqual(admission_snapshots[1], {})
        self.assertEqual(admission_snapshots[2], {"sonarr:1": 1})

    def test_repair_publication_guard_prevents_target_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "movie.en.srt"
            target = root / "movie.et.srt"
            source.write_text("source", encoding="utf-8")
            target.write_text("original", encoding="utf-8")
            report = SimpleNamespace(repairable_cue_indexes=[0], issues=[])
            repair = SimpleNamespace(
                success=True,
                interrupted=False,
                attempts=1,
                attempt_history=[],
                donor_history=[],
                repaired_cues=[1],
                report=report,
            )
            guard = Mock(return_value=False)
            with (
                patch.object(app, "_get_cleanup_detector", return_value=object()),
                patch.object(cleanup, "target_language_for_code", return_value=object()),
                patch.object(cleanup, "repair_subtitle_file", return_value=repair),
                patch.object(app, "_replace_managed_file") as replace,
            ):
                result = app._perform_repair(
                    str(source), str(target), "en", "et", None, "Movie",
                    "movies", report, app._file_hash_or_none(target),
                    expected_source_hash=app._file_hash_or_none(source),
                    publication_guard=guard,
                )

            self.assertEqual(result.action, "repair-deferred")
            self.assertEqual(target.read_text(encoding="utf-8"), "original")
            guard.assert_called_once_with()
            replace.assert_not_called()


if __name__ == "__main__":
    unittest.main()
