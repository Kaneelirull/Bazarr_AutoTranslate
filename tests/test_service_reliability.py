import os
import io
import sys
import tempfile
import threading
import time
import unittest
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

import Bazarr_AutoTranslate as app  # noqa: E402
import clean_et_subs as cleanup  # noqa: E402
from state_store import StateStore  # noqa: E402
from state_store import StateStoreError  # noqa: E402


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
                _status_finish_cycle=lambda: None,
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

            with patch.object(
                cleanup,
                "normalize_managed_file",
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


if __name__ == "__main__":
    unittest.main()
