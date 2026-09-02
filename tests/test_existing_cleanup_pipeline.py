import json
import os
import io
import sys
import tempfile
import threading
import time
import unittest
import logging
from collections import defaultdict
from concurrent.futures import Future
from types import SimpleNamespace
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import Mock, patch


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
import autotranslate.subtitles.foundation as subtitle_foundation  # noqa: E402
import autotranslate.subtitles.repair as subtitle_repair  # noqa: E402
import autotranslate.subtitles.workflow as subtitle_workflow  # noqa: E402
import autotranslate.composition as composition  # noqa: E402
import autotranslate.items_workflow as items_workflow  # noqa: E402
import autotranslate.maintenance.runtime as maintenance_runtime  # noqa: E402
import autotranslate.status.runtime as status_runtime  # noqa: E402
from autotranslate.subtitles.library import ValidationStateStore  # noqa: E402
from autotranslate.persistence.state_store import StateStore  # noqa: E402
from autotranslate.subtitles.foundation import ValidationIssue, ValidationReport  # noqa: E402

app = load_runtime(Config.from_env(), None)


def make_srt(text: str) -> str:
    return f"1\n00:00:01,000 --> 00:00:01,900\n{text}\n"


def make_multi_srt(*texts: str) -> str:
    return "\n\n".join(
        f"{index}\n00:00:{index:02d},000 --> 00:00:{index:02d},900\n{text}"
        for index, text in enumerate(texts, start=1)
    ) + "\n"


def make_timed_srt(cue_count: int, final_second: int, text: str = "Dialogue line") -> str:
    blocks = []
    for index in range(1, cue_count + 1):
        second = max(1, int(final_second * index / cue_count))
        hours, remainder = divmod(second, 3600)
        minutes, seconds = divmod(remainder, 60)
        stamp = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        blocks.append(f"{index}\n{stamp},000 --> {stamp},900\n{text} {index}")
    return "\n\n".join(blocks) + "\n"


class ExistingCleanupPipelineTests(unittest.TestCase):
    def setUp(self):
        app._cycle_suppressions.begin_cycle(self.id())

    def test_retention_uses_owned_logging_path_without_legacy_sink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_dir = root / "logs"
            quarantine_dir = root / "quarantine"
            current_log = log_dir / "bazarr-autotranslate-current.log"
            validation_state = SimpleNamespace(
                protected_artifact_paths=lambda: {root / "protected.srt"},
                prune_older_than=lambda _days: 3,
            )
            calls = []

            def purge(path, days, **kwargs):
                calls.append((Path(path), days, kwargs))
                return []

            with (
                patch.object(
                    composition, "_logging_resource",
                    SimpleNamespace(current_path=current_log),
                ),
                patch.multiple(
                    app,
                    LOG_DIR=log_dir,
                    CLEANUP_QUARANTINE_DIR=quarantine_dir,
                    RETENTION_DAYS=30,
                    QUARANTINE_ARTIFACT_RETENTION_DAYS=10,
                    _manual_review_service=None,
                ),
                patch.object(app, "_get_validation_state", return_value=validation_state),
                patch.object(app, "_status_compact_history", return_value=2),
                patch.object(cleanup, "purge_old_files", side_effect=purge),
            ):
                result = app.run_retention_housekeeping()

            self.assertEqual(calls[1][0], log_dir)
            self.assertEqual(calls[1][2]["exclude"], [current_log])
            self.assertEqual(result["state_entries"], 3)
            self.assertEqual(result["status_events"], 2)

    def test_tracked_retention_completes_or_fails_status_job(self):
        with (
            patch.object(app, "_status_create_maintenance", return_value="retention-job") as create,
            patch.object(app, "_status_complete_maintenance") as complete,
            patch.object(
                maintenance_runtime, "run_retention_housekeeping",
                return_value={"log_files": 0},
            ),
        ):
            self.assertEqual(
                app._run_retention_housekeeping_tracked(), {"log_files": 0}
            )
        create.assert_called_once_with(
            "retention", {"title": "Retention housekeeping"}, state="retaining"
        )
        complete.assert_called_once_with("retention-job", "accepted")

        with (
            patch.object(app, "_status_create_maintenance", return_value="retention-job"),
            patch.object(app, "_status_complete_maintenance") as complete,
            patch.object(
                maintenance_runtime, "run_retention_housekeeping",
                side_effect=RuntimeError("boom"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                app._run_retention_housekeeping_tracked()
        complete.assert_called_once_with(
            "retention-job", "failed", reason="retention housekeeping failed"
        )

    def test_repair_drain_is_interruptible_after_shutdown(self):
        pending = Future()
        started = time.monotonic()
        with patch.object(app, "shutdown_requested", True):
            completed = list(app._completed_repair_futures([pending]))
        self.assertEqual(completed, [])
        self.assertLess(time.monotonic() - started, 1.0)

    def test_cycle_suppression_registry_is_thread_safe_and_resets(self):
        registry = app.CycleSuppressionRegistry()
        registry.begin_cycle("cycle-1")
        identities = [f"show|{language}" for language in ("et", "sv")]
        threads = [
            threading.Thread(
                target=registry.suppress,
                args=(identities[index % 2],),
                kwargs={"action": "quarantined"},
            )
            for index in range(20)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(registry.get("show|et")["cycleId"], "cycle-1")
        self.assertEqual(registry.get("show|sv")["cycleId"], "cycle-1")
        self.assertIsNone(registry.get("other-show|et"))
        registry.begin_cycle("cycle-2")
        self.assertIsNone(registry.get("show|et"))

    def test_daily_log_formatter_adds_utc_timestamp_only_to_file_handler(self):
        with tempfile.TemporaryDirectory() as directory:
            sink = app._DailyLogSink(Path(directory))
            handler = app._DailyLogHandler(sink)
            handler.setFormatter(
                app._UtcLogFormatter(
                    "%(asctime)s.%(msecs)03dZ %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%S",
                )
            )
            record = logging.LogRecord(
                "test", logging.INFO, __file__, 1, "[INFO] Top Gear", (), None
            )
            record.created = 1_800_000_000.125
            record.msecs = 125
            handler.emit(record)
            sink.flush()
            persisted = sink.current_path.read_text(encoding="utf-8")
            self.assertRegex(
                persisted,
                r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.125Z \[INFO\] Top Gear\n$",
            )
            self.assertEqual(
                logging.Formatter("%(message)s").format(record), "[INFO] Top Gear"
            )
            sink.close()

    def test_async_repair_updates_series_circuit_once(self):
        metadata = {
            "series_key": "sonarr:77",
            "series_title": "Top Gear",
            "item_type": "episodes",
            "item_id": 42,
            "target_lang": "et",
        }
        for action, expected_success in (
            ("repaired", True),
            ("quarantined", False),
        ):
            future = Future()
            future.set_result(
                app.RepairJobResult(
                    action,
                    SimpleNamespace(issues=[]),
                    "Top Gear",
                    "et",
                    "episodes",
                    42,
                )
            )
            state = SimpleNamespace(record_circuit_outcome=lambda **_kwargs: None)
            with (
                patch.object(app, "_get_validation_state", return_value=state),
                patch.object(state, "record_circuit_outcome") as record,
                patch.object(app, "_refresh_status_diagnostics"),
                patch.object(app, "_status_transition"),
            ):
                app._publish_repair_status(future, metadata)
            record.assert_called_once()
            self.assertEqual(
                record.call_args.kwargs["success"], expected_success
            )
            if expected_success:
                self.assertIsNone(record.call_args.kwargs["reason"])

    def test_repair_terminalizes_when_circuit_bookkeeping_raises(self):
        report = SimpleNamespace(issues=[])
        future = Future()
        future.set_result(app.RepairJobResult(
            "repaired", report, "Schitt's Creek S02E01", "et",
            "episodes", 42, attempts=1, target_path="target.et.srt",
        ))
        metadata = {
            "series_key": "sonarr:77",
            "series_title": "Schitt's Creek",
            "item_type": "episodes",
            "item_id": 42,
            "target_lang": "et",
            "status_ref": {"kind": "maintenance", "id": "repair-status"},
            "maintenance_scan_job_id": "scan-status",
            "status_lock": threading.Lock(),
            "status_published": False,
            "scan_child_finished": False,
        }
        state = SimpleNamespace(
            record_circuit_outcome=Mock(side_effect=TypeError("missing reason")),
        )
        with (
            patch.object(app, "_get_validation_state", return_value=state),
            patch.object(app, "_resolve_retry_success", return_value=False),
            patch.object(app, "_complete_repair_status", return_value=True) as complete,
            patch.object(app, "_scan_child_finished", return_value=True) as child_finished,
        ):
            app._publish_repair_status(future, metadata)
            app._publish_repair_status(future, metadata)

        complete.assert_called_once_with(
            metadata, "repaired", reason=None, repaired=True,
            details={"attempts": 1},
        )
        child_finished.assert_called_once_with("scan-status", "repaired")
        self.assertTrue(metadata["status_published"])
        self.assertTrue(metadata["scan_child_finished"])

    def test_repair_terminal_status_retries_transient_persistence_failure(self):
        future = Future()
        future.set_result(app.RepairJobResult(
            "repaired", SimpleNamespace(issues=[]), "Show S01E01", "et",
            "episodes", 42, attempts=2, target_path="target.et.srt",
        ))
        metadata = {
            "item_type": "episodes",
            "item_id": 42,
            "target_lang": "et",
            "status_ref": {"kind": "maintenance", "id": "repair-status"},
            "maintenance_scan_job_id": "scan-status",
            "status_lock": threading.Lock(),
            "status_published": False,
        }
        complete = Mock(side_effect=[False, False, False, True])
        resolve = Mock(return_value=False)
        with (
            patch.object(app, "_complete_repair_status", complete),
            patch.object(app, "_resolve_retry_success", resolve),
            patch.object(app, "_get_validation_state", return_value=SimpleNamespace()),
            patch.object(app, "_scan_child_finished", return_value=True) as child_finished,
        ):
            app._publish_repair_status(future, metadata)
            self.assertFalse(metadata["status_published"])
            app._publish_repair_status(future, metadata)

        self.assertEqual(complete.call_count, 4)
        resolve.assert_called_once()
        child_finished.assert_called_once_with("scan-status", "repaired")
        self.assertTrue(metadata["status_published"])

    def test_waiting_repairs_precede_translations_without_exceeding_shared_limit(self):
        for limit in (1, 2, 4):
            gate = app.SharedCapacityCoordinator(limit)
            occupied = []
            active = 0
            maximum = 0
            counter_lock = threading.Lock()
            for _ in range(limit):
                occupied.append(gate.acquire_translation())
                active += 1
                maximum = max(maximum, active)

            repair_reserved = threading.Event()
            repair_started = threading.Event()
            translation_started = threading.Event()
            release_repair = threading.Event()

            def repair():
                nonlocal active, maximum
                repair_token = gate.reserve_repair()
                repair_reserved.set()
                self.assertTrue(gate.start_repair(repair_token))
                with counter_lock:
                    active += 1
                    maximum = max(maximum, active)
                repair_started.set()
                release_repair.wait(1)
                with counter_lock:
                    active -= 1
                gate.release(repair_token)

            def translation():
                nonlocal active, maximum
                token = gate.acquire_translation()
                with counter_lock:
                    active += 1
                    maximum = max(maximum, active)
                translation_started.set()
                with counter_lock:
                    active -= 1
                gate.release(token)

            repair_thread = threading.Thread(target=repair)
            repair_thread.start()
            self.assertTrue(repair_reserved.wait(1))
            translation_thread = threading.Thread(target=translation)
            translation_thread.start()
            with counter_lock:
                active -= 1
            gate.release(occupied.pop())
            self.assertTrue(repair_started.wait(1))
            self.assertFalse(translation_started.wait(0.05))
            release_repair.set()
            repair_thread.join(1)
            self.assertTrue(translation_started.wait(1))
            translation_thread.join(1)
            for token in occupied:
                with counter_lock:
                    active -= 1
                gate.release(token)
            self.assertLessEqual(maximum, limit)

    def setUp(self):
        self._state_directory = tempfile.TemporaryDirectory()
        app._validation_state = StateStore(
            Path(self._state_directory.name) / "state.sqlite3",
            validator_version=cleanup.VALIDATOR_VERSION,
        )
        self._permissions_patchers = [
            patch.object(module, "normalize_managed_file", lambda _path: None)
            for module in (cleanup, subtitle_foundation, subtitle_repair)
        ]
        for patcher in self._permissions_patchers:
            patcher.start()

    def tearDown(self):
        app._shutdown_repair_executor()
        app._translation_capacity.reset()
        with app._pending_repairs_lock:
            app._pending_repairs.clear()
            app._repair_keys.clear()
        for patcher in reversed(self._permissions_patchers):
            patcher.stop()
        if isinstance(app._validation_state, StateStore):
            app._validation_state.close()
        app._validation_state = None
        self._state_directory.cleanup()

    def _record_lingarr_artifact(
        self, source: Path, target: Path, target_language: str = "et"
    ) -> None:
        suffix = app._target_suffix(target, target_language)
        app._validation_state.record(
            target,
            source_hash=app._file_hash_or_none(source),
            target_hash=app._file_hash_or_none(target),
            result="pending",
            origin="lingarr",
            source_path=source,
            source_language="en",
            target_language=target_language,
            target_identity=app._target_identity_from_sidecar(
                target, target_language
            ),
            target_variant=suffix[1] if suffix is not None else "",
            operation="translation",
        )

    def test_cooldown_can_be_cleared_by_removed_target_path(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "show.et.srt"
            app._record_submission(42, "et", str(target))
            self.assertIsNotNone(app._check_cooldown(42, "et"))
            cleared = app._clear_submission_for_path(target, "et")
            self.assertEqual(cleared, 1)
            self.assertIsNone(app._check_cooldown(42, "et"))

    def test_variant_paths_and_dynamic_target_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "show.mkv"
            video.write_bytes(b"video")
            cases = {
                "show.en.srt": "show.et.srt",
                "show.eng.hi.srt": "show.et.hi.srt",
                "show.en.sdh.srt": "show.et.sdh.srt",
                "show.eng.12.srt": "show.et.12.srt",
            }
            for source_name, target_name in cases.items():
                source = root / source_name
                source.write_text(make_srt("English"), encoding="utf-8")
                self.assertEqual(
                    Path(app._derive_target_path(str(source), "en", "et")).name,
                    target_name,
                )
                (root / target_name).write_text(make_srt("Tere"), encoding="utf-8")

            found = {Path(path).name for path in app._find_target_sidecars(str(video), "et")}
            self.assertEqual(found, set(cases.values()))
            self.assertEqual(app._sub_priority(str(root / "show.est.srt"), "et"), 0)
            self.assertEqual(app._sub_priority(str(root / "show.eng.srt"), "en"), 0)

    def test_changed_hi_output_is_discovered_when_plain_was_expected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "show.mkv"
            expected = root / "show.et.srt"
            hi_target = root / "show.et.hi.srt"
            video.write_bytes(b"video")
            hi_target.write_text(make_srt("Old"), encoding="utf-8")
            before = app._snapshot_target_sidecars(str(video), "et")
            hi_target.write_text(make_srt("New"), encoding="utf-8")

            discovered = app._discover_completed_target(
                str(video), "et", str(expected), before
            )

            self.assertEqual(discovered, str(hi_target))

    def test_failed_job_discovery_rejects_unchanged_preexisting_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "show.mkv"
            target = root / "show.et.srt"
            video.write_bytes(b"video")
            target.write_text(make_srt("Existing"), encoding="utf-8")
            before = app._snapshot_target_sidecars(str(video), "et")

            self.assertIsNone(app._discover_completed_target(
                str(video), "et", str(target), before, require_changed=True,
            ))

            target.write_text(make_srt("Changed"), encoding="utf-8")
            self.assertEqual(app._discover_completed_target(
                str(video), "et", str(target), before, require_changed=True,
            ), str(target))

    def test_variant_quarantine_clears_logical_plain_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "show.mkv"
            target = root / "show.et.hi.srt"
            video.write_bytes(b"video")
            app._record_submission(
                42,
                "et",
                str(root / "show.et.srt"),
                expected_target_path=str(root / "show.et.srt"),
                video_path=str(video),
            )
            self.assertEqual(app._clear_submission_for_path(target, "et"), 1)
            self.assertIsNone(app._check_cooldown(42, "et"))

    def _prune_fixture(self, directory: str, *, managed=("en", "et", "sv")):
        root = Path(directory) / "media"
        root.mkdir()
        video = root / "movie.mkv"
        video.write_bytes(b"video")
        for language in managed:
            (root / f"movie.{language}.srt").write_text(make_srt(f"Valid {language}"), encoding="utf-8")
        return root, video

    def test_prune_quarantines_recognized_extra_languages_and_special_tracks(self):
        with tempfile.TemporaryDirectory() as directory:
            root, video = self._prune_fixture(directory)
            extras = [root / "movie.tur.srt", root / "movie.fre.sdh.srt", root / "movie.commentary.srt"]
            for path in extras:
                path.write_text(make_srt("Extra"), encoding="utf-8")
            quarantine = Path(directory) / "quarantine"
            with (
                patch.multiple(
                    app,
                    LANGUAGES=["en", "et", "sv"],
                    CLEANUP_ROOTS=[root],
                    CLEANUP_QUARANTINE_DIR=quarantine,
                    CLEANUP_PRUNE_EXTRA_LANGUAGES=True,
                    CLEANUP_PRUNE_ACTION="quarantine",
                    CLEANUP_PRUNE_SPECIAL_SIDECARS=True,
                    CLEANUP_PRUNE_UNKNOWN_SIDECARS=False,
                    CLEANUP_SCAN_DRY_RUN=False,
                ),
                patch.object(app, "_probe_media_duration", return_value=5400.0),
                patch.object(app, "_managed_sidecar_is_valid", return_value=(True, {"valid": True})),
                patch.object(
                    app,
                    "_status_create_maintenance",
                    return_value="prune-status",
                ) as create_status,
                patch.object(app, "_status_complete_maintenance") as complete_status,
                patch.object(
                    app,
                    "_status_record_maintenance_outcome",
                ) as record_outcome,
            ):
                stats, episodes_changed, movies_changed = app.run_extra_sidecar_prune()

            self.assertEqual(stats["prune_quarantined"], 3)
            self.assertTrue(episodes_changed)
            self.assertTrue(movies_changed)
            for path in extras:
                self.assertFalse(path.exists())
                report = quarantine / f"{path.name}.validation.json"
                self.assertTrue(report.exists())
            self.assertTrue((root / "movie.en.srt").exists())
            self.assertTrue((root / "movie.et.srt").exists())
            self.assertTrue((root / "movie.sv.srt").exists())
            create_status.assert_called_once_with(
                "sidecar_pruning",
                {"title": "Subtitle sidecar pruning"},
                state="pruning",
            )
            self.assertEqual(complete_status.call_args.args[:2], ("prune-status", "accepted"))
            self.assertEqual(record_outcome.call_count, 3)
            self.assertTrue(all(
                call.args[0:2] == ("sidecar_pruning", "pruned")
                for call in record_outcome.call_args_list
            ))

    def test_missing_managed_language_blocks_all_pruning(self):
        with tempfile.TemporaryDirectory() as directory:
            root, video = self._prune_fixture(directory, managed=("en", "et"))
            extra = root / "movie.tur.srt"
            extra.write_text(make_srt("Extra"), encoding="utf-8")
            with (
                patch.multiple(
                    app,
                    LANGUAGES=["en", "et", "sv"],
                    CLEANUP_ROOTS=[root],
                    CLEANUP_PRUNE_EXTRA_LANGUAGES=True,
                    CLEANUP_SCAN_DRY_RUN=False,
                ),
                patch.object(app, "_probe_media_duration", return_value=5400.0),
                patch.object(app, "_managed_sidecar_is_valid", return_value=(True, {"valid": True})),
            ):
                stats, _, _ = app.run_extra_sidecar_prune([(video, "movies")])
            self.assertEqual(stats["prune_deferred"], 1)
            self.assertEqual(stats["prune_candidates"], 0)
            self.assertTrue(extra.exists())

    def test_invalid_managed_language_blocks_all_pruning(self):
        with tempfile.TemporaryDirectory() as directory:
            root, video = self._prune_fixture(directory)
            extra = root / "movie.tur.srt"
            extra.write_text(make_srt("Extra"), encoding="utf-8")

            def validate(entry, duration, detector):
                valid = entry.language != "sv"
                return valid, {"valid": valid, "language": entry.language}

            with (
                patch.multiple(
                    app,
                    LANGUAGES=["en", "et", "sv"],
                    CLEANUP_ROOTS=[root],
                    CLEANUP_PRUNE_EXTRA_LANGUAGES=True,
                ),
                patch.object(app, "_probe_media_duration", return_value=5400.0),
                patch.object(app, "_managed_sidecar_is_valid", side_effect=validate),
            ):
                stats, _, _ = app.run_extra_sidecar_prune([(video, "movies")])
            self.assertEqual(stats["prune_invalid_languages"], 1)
            self.assertEqual(stats["prune_candidates"], 0)
            self.assertTrue(extra.exists())

    def test_successfully_used_eng_hash_is_prune_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "episode.mkv"
            source = root / "episode.eng.srt"
            target = root / "episode.et.srt"
            video.write_bytes(b"video")
            source.write_text(make_srt("English dialogue"), encoding="utf-8")
            target.write_text(make_srt("Eestikeelne dialoog"), encoding="utf-8")
            source_hash = app._file_hash_or_none(source)
            app._validation_state.record_source_readiness(
                media_identity=app._media_identity_for_video(video),
                video_path=video,
                source_path=source,
                source_language="en",
                source_hash=source_hash,
                media_duration_seconds=600,
                target_language="et",
            )
            classification = app._classify_sidecar(video, source)
            language_disagreement = ValidationReport([
                ValidationIssue(
                    "target_file_invalid",
                    "detected ESTONIAN with confidence 0.99",
                )
            ])
            with (
                patch.object(app, "CLEANUP_UNDERSIZED_ENABLED", True),
                patch(
                    "autotranslate.subtitles.library.validate_subtitle_without_source",
                    return_value=language_disagreement,
                ),
            ):
                ready, evidence = app._managed_sidecar_is_valid(
                    classification, 600, detector=object()
                )
            self.assertTrue(ready)
            self.assertEqual(evidence["reason"], "successful_source_hash")
            self.assertTrue(evidence["languageOverride"])

            non_language_failure = ValidationReport([
                ValidationIssue("target_repetition", "repetitive subtitle content")
            ])
            with patch(
                "autotranslate.subtitles.library.validate_subtitle_without_source",
                return_value=non_language_failure,
            ):
                ready, evidence = app._managed_sidecar_is_valid(
                    classification, 600, detector=object()
                )
            self.assertFalse(ready)
            self.assertNotEqual(evidence.get("reason"), "successful_source_hash")

    def test_cached_scan_publishes_visited_file_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "episode.et.srt"
            target.write_text(make_srt("Tere"), encoding="utf-8")
            tracker = app.StatusTracker(
                root / "status.json", root / "history.jsonl"
            )
            candidate = SimpleNamespace(
                path=target, target_lang="et", variant=""
            )
            with (
                patch.object(app, "_status_tracker", tracker),
                patch.object(app, "CLEANUP_SCAN_EXISTING", True),
                patch.object(app, "CLEANUP_ROOTS", [root]),
                patch.object(app, "CLEANUP_LANGUAGES", {"et"}),
                patch.object(app, "_scan_undersized_sidecars", return_value=False),
                patch.object(app, "_get_cleanup_detector", return_value=object()),
                patch.object(cleanup, "discover_target_subtitles", return_value=[candidate]),
                patch.object(cleanup, "find_preferred_source", return_value=(None, None)),
                patch.object(app._validation_state, "is_unchanged_valid", return_value=True),
                patch.object(app, "run_extra_sidecar_prune", return_value=(app._prune_stats(), False, False)),
            ):
                result = app._run_existing_cleanup_scan_safely()
            self.assertIsNotNone(result)
            outcome = tracker.snapshot()["maintenance"]["recentOutcomes"][0]
            self.assertEqual(outcome["filesDiscovered"], 1)
            self.assertEqual(outcome["filesChecked"], 1)
            self.assertEqual(outcome["progress"], 100)

    def test_managed_variants_are_preserved_and_forced_only_does_not_satisfy_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            root, video = self._prune_fixture(directory, managed=("en", "et"))
            variants = [root / "movie.en.hi.srt", root / "movie.et.sdh.srt", root / "movie.sv.forced.srt"]
            for path in variants:
                path.write_text(make_srt("Managed variant"), encoding="utf-8")
            extra = root / "movie.tur.srt"
            extra.write_text(make_srt("Extra"), encoding="utf-8")
            with (
                patch.multiple(
                    app,
                    LANGUAGES=["en", "et", "sv"],
                    CLEANUP_ROOTS=[root],
                    CLEANUP_PRUNE_EXTRA_LANGUAGES=True,
                ),
                patch.object(app, "_probe_media_duration", return_value=5400.0),
                patch.object(app, "_managed_sidecar_is_valid", return_value=(True, {"valid": True})),
            ):
                stats, _, _ = app.run_extra_sidecar_prune([(video, None)])
            self.assertEqual(stats["prune_deferred"], 1)
            self.assertTrue(extra.exists())
            self.assertTrue(all(path.exists() for path in variants))

    def test_unknown_sidecars_are_retained_by_default_and_removable_by_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            root, video = self._prune_fixture(directory)
            unknown = [root / "movie.srt", root / "movie.2.srt", root / "movie.custom.srt"]
            for path in unknown:
                path.write_text(make_srt("Unknown"), encoding="utf-8")
            common = dict(
                LANGUAGES=["en", "et", "sv"],
                CLEANUP_ROOTS=[root],
                CLEANUP_PRUNE_EXTRA_LANGUAGES=True,
                CLEANUP_PRUNE_ACTION="report",
                CLEANUP_SCAN_DRY_RUN=False,
            )
            with (
                patch.multiple(app, CLEANUP_PRUNE_UNKNOWN_SIDECARS=False, **common),
                patch.object(app, "_probe_media_duration", return_value=5400.0),
                patch.object(app, "_managed_sidecar_is_valid", return_value=(True, {"valid": True})),
            ):
                retained, _, _ = app.run_extra_sidecar_prune([(video, None)])
            with (
                patch.multiple(app, CLEANUP_PRUNE_UNKNOWN_SIDECARS=True, **common),
                patch.object(app, "_probe_media_duration", return_value=5400.0),
                patch.object(app, "_managed_sidecar_is_valid", return_value=(True, {"valid": True})),
            ):
                removable, _, _ = app.run_extra_sidecar_prune([(video, None)])
            self.assertEqual(retained["prune_retained_unknown"], 3)
            self.assertEqual(retained["prune_candidates"], 0)
            self.assertEqual(removable["prune_candidates"], 3)

    def test_overlapping_video_names_do_not_share_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            root, short_video = self._prune_fixture(directory)
            long_video = root / "movie.extended.mkv"
            long_video.write_bytes(b"video")
            long_extra = root / "movie.extended.tur.srt"
            long_extra.write_text(make_srt("Extra"), encoding="utf-8")
            self.assertNotIn(long_extra, app._video_sidecars(short_video))
            self.assertIn(long_extra, app._video_sidecars(long_video))

    def test_extracted_sidecar_is_preserved_as_source_not_managed_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "movie.mkv"
            extracted = root / "movie.extracted.eng.srt"
            video.write_bytes(b"video")
            extracted.write_text(make_srt("English"), encoding="utf-8")
            with patch.object(app, "LANGUAGES", ["en", "et"]):
                classification = app._classify_sidecar(video, extracted)
            self.assertEqual(classification.kind, "source")
            self.assertEqual(classification.language, "en")

    def test_dry_run_prune_reports_without_moving(self):
        with tempfile.TemporaryDirectory() as directory:
            root, video = self._prune_fixture(directory)
            extra = root / "movie.tur.srt"
            extra.write_text(make_srt("Extra"), encoding="utf-8")
            with (
                patch.multiple(
                    app,
                    LANGUAGES=["en", "et", "sv"],
                    CLEANUP_ROOTS=[root],
                    CLEANUP_PRUNE_EXTRA_LANGUAGES=True,
                    CLEANUP_PRUNE_ACTION="quarantine",
                    CLEANUP_SCAN_DRY_RUN=True,
                ),
                patch.object(app, "_probe_media_duration", return_value=5400.0),
                patch.object(app, "_managed_sidecar_is_valid", return_value=(True, {"valid": True})),
            ):
                stats, episodes_changed, movies_changed = app.run_extra_sidecar_prune([(video, "movies")])
            self.assertEqual(stats["prune_candidates"], 1)
            self.assertTrue(extra.exists())
            self.assertFalse(episodes_changed or movies_changed)

    def test_failed_prune_quarantine_leaves_original_untouched(self):
        import autotranslate.subtitles.library as clean_et_subs

        with tempfile.TemporaryDirectory() as directory:
            root, video = self._prune_fixture(directory)
            extra = root / "movie.tur.srt"
            extra.write_text(make_srt("Extra"), encoding="utf-8")
            with (
                patch.multiple(
                    app,
                    LANGUAGES=["en", "et", "sv"],
                    CLEANUP_ROOTS=[root],
                    CLEANUP_PRUNE_EXTRA_LANGUAGES=True,
                    CLEANUP_PRUNE_ACTION="quarantine",
                    CLEANUP_SCAN_DRY_RUN=False,
                ),
                patch.object(app, "_probe_media_duration", return_value=5400.0),
                patch.object(app, "_managed_sidecar_is_valid", return_value=(True, {"valid": True})),
                patch.object(clean_et_subs, "quarantine_subtitle", side_effect=OSError("move failed")),
            ):
                stats, episodes_changed, movies_changed = app.run_extra_sidecar_prune([(video, "movies")])
            self.assertEqual(stats["prune_failures"], 1)
            self.assertTrue(extra.exists())
            self.assertFalse(episodes_changed or movies_changed)

    def test_unavailable_duration_blocks_pruning_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            root, video = self._prune_fixture(directory)
            extra = root / "movie.tur.srt"
            extra.write_text(make_srt("Extra"), encoding="utf-8")
            with (
                patch.multiple(
                    app,
                    LANGUAGES=["en", "et", "sv"],
                    CLEANUP_ROOTS=[root],
                    CLEANUP_PRUNE_EXTRA_LANGUAGES=True,
                ),
                patch.object(app, "_probe_media_duration", return_value=None),
            ):
                stats, _, _ = app.run_extra_sidecar_prune([(video, None)])
            self.assertEqual(stats["prune_duration_unavailable"], 1)
            self.assertEqual(stats["prune_candidates"], 0)
            self.assertTrue(extra.exists())

    def test_regular_undersized_sidecar_is_quarantined(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "media"
            root.mkdir()
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            target = root / "movie.eng.srt"
            target.write_text(make_multi_srt("One", "Two", "Three"), encoding="utf-8")
            quarantine = Path(directory) / "quarantine"
            state = app._validation_state
            stats = defaultdict(int)

            with patch.multiple(
                app,
                CLEANUP_ROOTS=[root],
                CLEANUP_ACTION="quarantine",
                CLEANUP_SCAN_DRY_RUN=False,
                CLEANUP_QUARANTINE_DIR=quarantine,
                _validation_state=state,
                _probe_media_duration=lambda _path: 7200.0,
            ):
                changed = app._scan_undersized_sidecars(stats)

            self.assertTrue(changed)
            self.assertFalse(target.exists())
            self.assertTrue((quarantine / "movie.eng.srt").exists())
            self.assertEqual(stats["undersized_quarantined"], 1)
            audit = json.loads(
                (quarantine / "movie.eng.srt.validation.json").read_text(encoding="utf-8")
            )
            self.assertEqual(audit["origin"], "unknown")
            self.assertEqual(audit["filenameClassification"], "regular")
            self.assertTrue(audit["completeness"]["undersized"])
            self.assertGreaterEqual(len(audit["completeness"]["failedSignals"]), 3)
            self.assertEqual(audit["completeness"]["thresholds"]["requiredSignals"], 3)
            self.assertIn(
                "undersized_subtitle",
                {issue["rule"] for issue in audit["validation"]["issues"]},
            )

    def test_explicit_forced_sidecar_is_exempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "media"
            root.mkdir()
            (root / "movie.mkv").write_bytes(b"video")
            forced = root / "movie.eng.forced.srt"
            forced.write_text(make_srt("Sign"), encoding="utf-8")
            stats = defaultdict(int)

            with patch.multiple(app, CLEANUP_ROOTS=[root], _probe_media_duration=lambda _path: 7200.0):
                changed = app._scan_undersized_sidecars(stats)

            self.assertFalse(changed)
            self.assertTrue(forced.exists())
            self.assertEqual(stats["undersized_forced_exempt"], 1)

    def test_completeness_scan_defers_malformed_srt_to_structural_handling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "media"
            root.mkdir()
            (root / "show.mkv").write_bytes(b"video")
            target = root / "show.et.srt"
            target.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\n\nOrphan line\n",
                encoding="utf-8",
            )
            quarantine = Path(directory) / "quarantine"
            stats = defaultdict(int)

            with patch.multiple(
                app,
                CLEANUP_ROOTS=[root],
                CLEANUP_ACTION="quarantine",
                CLEANUP_QUARANTINE_DIR=quarantine,
                _probe_media_duration=lambda _path: 3600.0,
            ):
                changed = app._scan_undersized_sidecars(stats)

            self.assertFalse(changed)
            self.assertTrue(target.exists())
            self.assertFalse(quarantine.exists())

    def test_source_fallback_uses_next_complete_language(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "media"
            root.mkdir()
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            english = root / "movie.en.srt"
            swedish = root / "movie.sv.srt"
            english.write_text(make_timed_srt(5, 3500, "Sign"), encoding="utf-8")
            swedish.write_text(make_timed_srt(150, 3500, "Detta ar en fullstandig dialograd"), encoding="utf-8")
            quarantine = Path(directory) / "quarantine"
            state = app._validation_state
            stats = defaultdict(int)
            stats["translations"] = []
            item = {"radarrId": 7, "title": "Movie", "missing_subtitles": [{"code2": "et"}]}
            subtitles = [
                {"code2": "en", "path": str(english), "forced": False},
                {"code2": "sv", "path": str(swedish), "forced": False},
            ]

            with patch.multiple(
                app,
                LANGUAGES=["en", "sv", "et"],
                CLEANUP_ROOTS=[root],
                CLEANUP_ACTION="quarantine",
                CLEANUP_QUARANTINE_DIR=quarantine,
                _validation_state=state,
                _probe_media_duration=lambda _path: 3600.0,
                fetch_subtitles=lambda *_args: (str(video), subtitles),
                lingarr_resolve_media_id=lambda *_args: None,
            ):
                app.process_item(item, "movies", "radarrId", stats, threading.Lock())

            self.assertFalse(english.exists())
            self.assertTrue((quarantine / "movie.en.srt").exists())
            self.assertTrue(swedish.exists())
            self.assertEqual(stats["cleanup_alternative_sources"], 1)

    def test_status_accepts_only_after_lingarr_output_validates(self):
        class Recorder:
            def __init__(self):
                self.states = []

            def transition_for(
                self, item_type, item_id, target_language, state, **kwargs
            ):
                self.states.append((state, kwargs))
                return True

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "movie.mkv"
            source = root / "movie.en.srt"
            target = root / "movie.et.srt"
            video.write_bytes(b"video")
            source.write_text(make_srt("English dialogue"), encoding="utf-8")
            item = {
                "radarrId": 7,
                "title": "Movie",
                "missing_subtitles": [{"code2": "et"}],
            }
            subtitles = [{"code2": "en", "path": str(source), "forced": False}]
            recorder = Recorder()
            stats = defaultdict(int)
            stats["translations"] = []
            stats["episode_activity"] = False
            stats["movie_activity"] = False

            def completed(*_args):
                target.write_text(make_srt("Tere"), encoding="utf-8")
                return "Completed"

            report = SimpleNamespace(issues=[])
            with (
                patch.multiple(
                    app,
                    LANGUAGES=["en", "et"],
                    CLEANUP_UNDERSIZED_ENABLED=False,
                    _status_tracker=recorder,
                    fetch_subtitles=lambda *_args: (str(video), subtitles),
                    lingarr_resolve_media_id=lambda *_args: 99,
                    lingarr_get_active_translations=lambda: [],
                    lingarr_submit_file=lambda *_args: 123,
                    lingarr_poll_job=completed,
                    _count_dialogue_lines=lambda _path: 1,
                    _estimate_timeout=lambda _path: 60,
                    _record_submission=lambda *_args, **_kwargs: 1,
                    _mark_submission_submitted=lambda *_args, **_kwargs: None,
                    _mark_submission_failed=lambda *_args, **_kwargs: None,
                    _update_submission_actual_path=lambda *_args, **_kwargs: None,
                    _record_pending_lingarr_output=lambda *_args, **_kwargs: True,
                    _validate_translated_file=lambda *_args, **_kwargs: ("valid", report),
                ),
            ):
                app.process_item(item, "movies", "radarrId", stats, threading.Lock())

            states = [state for state, _ in recorder.states]
            self.assertEqual(states, ["translating", "validating", "accepted"])
            self.assertEqual(stats["completed"], 1)

    def test_failed_lingarr_changed_sidecar_is_provenanced_and_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "movie.mkv"
            source = root / "movie.en.srt"
            target = root / "movie.et.srt"
            video.write_bytes(b"video")
            source.write_text(make_srt("English dialogue"), encoding="utf-8")
            item = {"radarrId": 7, "title": "Movie", "missing_subtitles": [{"code2": "et"}]}
            stats = defaultdict(int)
            stats.update({"translations": [], "episode_activity": False, "movie_activity": False})

            def failed_with_output(*_args, **_kwargs):
                target.write_text(make_srt("Tere, see on tõlgitud dialoog"), encoding="utf-8")
                return "Failed"

            pending = Mock(return_value=True)
            validate = Mock(return_value=("valid", SimpleNamespace(issues=[])))
            with patch.multiple(
                app,
                LANGUAGES=["en", "et"], CLEANUP_UNDERSIZED_ENABLED=False,
                fetch_subtitles=lambda *_args: (str(video), [{"code2": "en", "path": str(source), "forced": False}]),
                lingarr_resolve_media_id=lambda *_args: 99,
                lingarr_get_active_translations=lambda: [],
                lingarr_submit_file=lambda *_args: 123,
                lingarr_poll_job=failed_with_output,
                lingarr_get_job=lambda *_args: {"id": 123, "status": "Failed"},
                _safe_failure_details=lambda *_args, **_kwargs: {"status": "Failed", "category": "provider"},
                _recover_failed_lingarr_job=lambda *_args, **_kwargs: {"recovered": False},
                _count_dialogue_lines=lambda _path: 1,
                _estimate_timeout=lambda _path: 60,
                _record_submission=lambda *_args, **_kwargs: 41,
                _mark_submission_submitted=lambda *_args, **_kwargs: None,
                _mark_submission_failed=lambda *_args, **_kwargs: None,
                _update_submission_actual_path=lambda *_args, **_kwargs: None,
                _record_pending_lingarr_output=pending,
                _validate_translated_file=validate,
            ):
                app.process_item(item, "movies", "radarrId", stats, threading.Lock())

            self.assertEqual(stats["completed"], 1)
            validate.assert_called_once()
            self.assertEqual(pending.call_args.kwargs["attempt_id"], 41)
            self.assertEqual(pending.call_args.kwargs["lingarr_job_id"], 123)
            self.assertEqual(pending.call_args.kwargs["terminal_status"], "Failed")

    def test_completed_job_without_output_uses_atomic_retry_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "movie.mkv"
            source = root / "movie.en.srt"
            video.write_bytes(b"video")
            source.write_text(make_srt("English dialogue"), encoding="utf-8")
            item = {"radarrId": 7, "title": "Movie", "missing_subtitles": [{"code2": "et"}]}
            stats = defaultdict(int)
            stats.update({"translations": [], "episode_activity": False, "movie_activity": False})
            transition = Mock(return_value={"id": 11})
            circuit_outcome = Mock()
            with (
                patch.object(app._validation_state, "terminalize_missing_output_and_schedule_retry", transition),
                patch.object(app._validation_state, "record_circuit_outcome", circuit_outcome),
                patch.multiple(
                    app,
                    LANGUAGES=["en", "et"], CLEANUP_UNDERSIZED_ENABLED=False,
                    fetch_subtitles=lambda *_args: (str(video), [{"code2": "en", "path": str(source), "forced": False}]),
                    lingarr_resolve_media_id=lambda *_args: 99,
                    lingarr_get_active_translations=lambda: [],
                    lingarr_submit_file=lambda *_args: 123,
                    lingarr_poll_job=lambda *_args, **_kwargs: "Completed",
                    _count_dialogue_lines=lambda _path: 1,
                    _estimate_timeout=lambda _path: 60,
                    _record_submission=lambda *_args, **_kwargs: 41,
                    _mark_submission_submitted=lambda *_args, **_kwargs: None,
                ),
            ):
                app.process_item(item, "movies", "radarrId", stats, threading.Lock())

            transition.assert_called_once()
            self.assertEqual(transition.call_args.args[0], 41)
            self.assertEqual(transition.call_args.kwargs["source_hash"], subtitle_foundation.file_sha256(source))
            self.assertEqual(transition.call_args.kwargs["target_path"], str(root / "movie.et.srt"))
            circuit_outcome.assert_called_once()
            self.assertFalse((root / "movie.et.srt").exists())
            self.assertEqual(stats["timed_out"], 1)

    def test_process_item_prefers_deduplicated_extracted_source_and_publishes_canonical_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "movie.mkv"
            bazarr_source = root / "movie.en.srt"
            extracted_source = root / "movie.extracted.eng.2.srt"
            provider_target = root / "movie.extracted.2.et.srt"
            canonical_target = root / "movie.et.srt"
            video.write_bytes(b"video")
            bazarr_source.write_text(make_srt("Bazarr dialogue"), encoding="utf-8")
            extracted_source.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nEmbedded dialogue\n\n"
                "2\n00:00:02,000 --> 00:00:03,000\nEmbedded dialogue\n",
                encoding="utf-8",
            )
            video_stat = video.stat()
            receipt = root / "movie.extracted.json"
            receipt.write_text(
                json.dumps({
                    "schemaVersion": 1,
                    "generator": "SubExtractorr",
                    "video": {
                        "path": str(video),
                        "name": video.name,
                        "size": video_stat.st_size,
                        "mtimeNs": video_stat.st_mtime_ns,
                    },
                    "tracks": [{
                        "path": extracted_source.name,
                        "sha256": subtitle_foundation.file_sha256(extracted_source),
                        "language": "eng",
                        "variant": "2",
                        "trackId": 2,
                        "default": True,
                        "forced": False,
                        "hearingImpaired": False,
                    }],
                }),
                encoding="utf-8",
            )
            item = {
                "radarrId": 7,
                "title": "Movie",
                "missing_subtitles": [{"code2": "et"}],
            }
            subtitles = [{"code2": "en", "path": str(bazarr_source), "forced": False}]
            submitted = []
            stats = defaultdict(int)
            stats["translations"] = []

            def submit(*args):
                submitted.append(args)
                return 123

            def completed(*_args):
                provider_target.write_text(make_srt("Tere"), encoding="utf-8")
                return "Completed"

            report = SimpleNamespace(issues=[])
            pending = Mock(return_value=True)
            validate = Mock(return_value=("valid", report))
            with patch.multiple(
                app,
                LANGUAGES=["en", "et"],
                CLEANUP_UNDERSIZED_ENABLED=False,
                fetch_subtitles=lambda *_args: (str(video), subtitles),
                lingarr_resolve_media_id=lambda *_args: 99,
                lingarr_get_active_translations=lambda: [],
                lingarr_submit_file=submit,
                lingarr_poll_job=completed,
                _count_dialogue_lines=lambda path: 1 if Path(path).exists() else None,
                _estimate_timeout=lambda _path: 60,
                _record_submission=lambda *_args, **_kwargs: 1,
                _mark_submission_submitted=lambda *_args, **_kwargs: None,
                _mark_submission_failed=lambda *_args, **_kwargs: None,
                _update_submission_actual_path=lambda *_args, **_kwargs: None,
                _record_pending_lingarr_output=pending,
                _validate_translated_file=validate,
            ):
                app.process_item(item, "movies", "radarrId", stats, threading.Lock())

            self.assertEqual(Path(submitted[0][1]), extracted_source)
            self.assertTrue(canonical_target.exists())
            self.assertFalse(provider_target.exists())
            self.assertFalse((root / "movie.extracted.et.2.srt").exists())
            self.assertEqual(pending.call_args.kwargs["attempt_id"], 1)
            self.assertEqual(Path(pending.call_args.args[1]), canonical_target)
            validate.assert_called_once()
            self.assertEqual(Path(validate.call_args.args[1]), canonical_target)
            self.assertTrue(stats["movie_activity"])
            self.assertIn("00:00:01,000 --> 00:00:03,000", extracted_source.read_text(encoding="utf-8"))
            self.assertEqual(stats["source_duplicate_groups"], 1)
            self.assertEqual(stats["source_duplicate_cues_removed"], 1)
            self.assertEqual(stats["embedded_sources_selected"], 1)
            self.assertEqual(bazarr_source.read_text(encoding="utf-8"), make_srt("Bazarr dialogue"))

    def test_canonical_publication_does_not_overwrite_concurrent_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider_target = root / "movie.extracted.et.srt"
            canonical_target = root / "movie.et.srt"
            provider_target.write_text(make_srt("Lingarr"), encoding="utf-8")
            canonical_target.write_text(make_srt("Concurrent"), encoding="utf-8")

            published = app._publish_canonical_target(provider_target, canonical_target, "Movie")

            self.assertIsNone(published)
            self.assertEqual(provider_target.read_text(encoding="utf-8"), make_srt("Lingarr"))
            self.assertEqual(canonical_target.read_text(encoding="utf-8"), make_srt("Concurrent"))

    def test_cleanup_action_defers_when_target_changes_before_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "movie.sv.srt"
            target.write_text(make_srt("Concurrent subtitle"), encoding="utf-8")
            report = ValidationReport([
                ValidationIssue("target_file_invalid", "expected Swedish; detected English")
            ])

            with (
                patch.object(app, "_file_hash_or_none", side_effect=["before", "after"]),
                patch.object(subtitle_workflow, "_apply_cleanup_action_locked") as apply_locked,
            ):
                action = app._apply_cleanup_action(target, None, "sv", report)

            self.assertEqual(action, "action-deferred")
            self.assertTrue(target.exists())
            apply_locked.assert_not_called()

    def test_cleanup_action_rejects_replacement_before_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "movie.sv.srt"
            target.write_text(make_srt("Replacement subtitle"), encoding="utf-8")
            report = ValidationReport([
                ValidationIssue("target_file_invalid", "expected Swedish; detected English")
            ])

            with patch.object(
                subtitle_workflow, "_apply_cleanup_action_locked"
            ) as apply_locked:
                action = app._apply_cleanup_action(
                    target,
                    None,
                    "sv",
                    report,
                    expected_target_hash="hash-of-validated-file",
                )

            self.assertEqual(action, "action-deferred")
            self.assertEqual(
                target.read_text(encoding="utf-8"), make_srt("Replacement subtitle")
            )
            apply_locked.assert_not_called()

    def test_outside_scope_valid_result_does_not_cache_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "movie.en.srt"
            target = root / "movie.sv.srt"
            source.write_text(make_srt("English source"), encoding="utf-8")
            target.write_text(make_srt("Swedish target"), encoding="utf-8")

            def replace_during_validation(_path):
                target.write_text(make_srt("Concurrent replacement"), encoding="utf-8")
                return ValidationReport([])

            with (
                patch.object(app, "CLEANUP_LANGUAGES", {"et"}),
                patch.object(app, "_get_cleanup_detector", return_value=None),
                patch.object(
                    subtitle_foundation,
                    "validate_srt_structure",
                    side_effect=replace_during_validation,
                ),
                patch.object(app, "_record_validation_result") as record,
            ):
                action, _report = app._validate_translated_file(
                    str(source), str(target), "en", "sv", 42,
                    item_type="episodes", origin="external",
                )

            self.assertEqual(action, "action-deferred")
            record.assert_not_called()

    def test_outside_scope_report_does_not_cache_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "movie.en.srt"
            target = root / "movie.sv.srt"
            source.write_text(make_srt("English source"), encoding="utf-8")
            target.write_text(make_srt("Ambiguous target"), encoding="utf-8")
            report = ValidationReport([
                ValidationIssue("excessive_lines", "cue 1 has too many lines")
            ])

            def replace_during_validation(*_args, **_kwargs):
                target.write_text(make_srt("Concurrent replacement"), encoding="utf-8")
                return report

            with (
                patch.object(app, "CLEANUP_LANGUAGES", {"et"}),
                patch.object(app, "_get_cleanup_detector", return_value=object()),
                patch.object(
                    cleanup,
                    "validate_subtitle_without_source",
                    side_effect=replace_during_validation,
                ),
                patch.object(
                    subtitle_workflow,
                    "_confident_wrong_language_evidence",
                    return_value=None,
                ),
                patch.object(app, "_record_validation_result") as record,
            ):
                action, _report = app._validate_translated_file(
                    str(source), str(target), "en", "sv", 42,
                    item_type="episodes", origin="external",
                )

            self.assertEqual(action, "action-deferred")
            record.assert_not_called()

    def test_submission_backed_embedded_orphan_self_heals_without_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "movie.mkv"
            source = root / "movie.extracted.eng.2.srt"
            orphan = root / "movie.extracted.2.et.srt"
            canonical = root / "movie.et.srt"
            video.write_bytes(b"video")
            source.write_text(make_srt("Source"), encoding="utf-8")
            video_stat = video.stat()
            (root / "movie.extracted.json").write_text(json.dumps({
                "schemaVersion": 1,
                "generator": "SubExtractorr",
                "video": {
                    "path": str(video), "name": video.name,
                    "size": video_stat.st_size, "mtimeNs": video_stat.st_mtime_ns,
                },
                "tracks": [{
                    "path": source.name,
                    "sha256": subtitle_foundation.file_sha256(source),
                    "language": "eng", "variant": "2", "trackId": 2,
                    "default": True, "forced": False, "hearingImpaired": False,
                }],
            }), encoding="utf-8")
            source_hash = subtitle_foundation.file_sha256(source)
            store = StateStore(root / "state.sqlite3")
            attempt = store.record_submission(
                "movies", 7, "et", cooldown_seconds=3600,
                source_path=str(source), source_hash=source_hash,
                source_language="en", status="submitted",
            )
            orphan.write_text(make_srt("Translated"), encoding="utf-8")
            future_mtime = time.time() + 1
            os.utime(orphan, (future_mtime, future_mtime))
            pending = Mock(return_value=True)
            submit = Mock()
            validate = Mock(return_value=("valid", SimpleNamespace(issues=[])))
            stats = defaultdict(int)
            stats["translations"] = []
            try:
                with patch.multiple(
                    app,
                    LANGUAGES=["en", "et"], CLEANUP_UNDERSIZED_ENABLED=False,
                    _validation_state=store,
                    fetch_subtitles=lambda *_args: (str(video), []),
                    lingarr_resolve_media_id=lambda *_args: 99,
                    lingarr_get_active_translations=lambda: [],
                    lingarr_submit_file=submit,
                    _record_pending_lingarr_output=pending,
                    _validate_translated_file=validate,
                ):
                    app.process_item(
                        {"radarrId": 7, "title": "Movie", "missing_subtitles": [{"code2": "et"}]},
                        "movies", "radarrId", stats, threading.Lock(),
                    )
            finally:
                store.close()

            self.assertTrue(canonical.exists())
            self.assertFalse(orphan.exists())
            pending.assert_called_once()
            self.assertEqual(pending.call_args.kwargs["attempt_id"], attempt)
            validate.assert_called_once()
            submit.assert_not_called()
            self.assertEqual(stats["completed"], 1)

    def test_embedded_orphan_recovery_rejects_wrong_source_and_receipt_owned_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "movie.extracted.eng.2.srt"
            orphan = root / "movie.extracted.2.et.srt"
            canonical = root / "movie.et.srt"
            source.write_text(make_srt("Source"), encoding="utf-8")
            orphan.write_text(make_srt("Translated"), encoding="utf-8")
            store = StateStore(root / "state.sqlite3")
            store.record_submission(
                "movies", 7, "et", cooldown_seconds=3600,
                source_path=str(source), source_hash="different-source",
                source_language="en", status="submitted",
            )
            pending = Mock(return_value=True)
            with patch.multiple(
                app,
                _get_validation_state=lambda: store,
                _record_pending_lingarr_output=pending,
            ):
                wrong_source = items_workflow._recover_embedded_provider_output(
                    item_type="movies", item_id=7, target_language="et",
                    source_path=str(source),
                    source_hash=subtitle_foundation.file_sha256(source),
                    target_path=str(canonical), candidates=[str(orphan)],
                    receipt_owned_paths=set(), title="Movie",
                )
                receipt_owned = items_workflow._recover_embedded_provider_output(
                    item_type="movies", item_id=7, target_language="et",
                    source_path=str(source), source_hash="different-source",
                    target_path=str(canonical), candidates=[str(orphan)],
                    receipt_owned_paths={os.path.normcase(os.path.abspath(orphan))},
                    title="Movie",
                )

            self.assertIsNone(wrong_source)
            self.assertIsNone(receipt_owned)
            self.assertTrue(orphan.exists())
            self.assertFalse(canonical.exists())
            pending.assert_not_called()
            store.close()

    def test_process_item_accepts_actual_hi_output_and_persists_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "movie.mkv"
            source = root / "movie.extracted.eng.hi.srt"
            provider_target = root / "movie.extracted.hi.et.srt"
            target = root / "movie.et.hi.srt"
            video.write_bytes(b"video")
            source.write_text(make_srt("English dialogue"), encoding="utf-8")
            video_stat = video.stat()
            (root / "movie.extracted.json").write_text(json.dumps({
                "schemaVersion": 1,
                "generator": "SubExtractorr",
                "video": {
                    "path": str(video), "name": video.name,
                    "size": video_stat.st_size, "mtimeNs": video_stat.st_mtime_ns,
                },
                "tracks": [{
                    "path": source.name,
                    "sha256": subtitle_foundation.file_sha256(source),
                    "language": "eng", "variant": "hi", "trackId": 2,
                    "default": True, "forced": False, "hearingImpaired": True,
                }],
            }), encoding="utf-8")
            item = {
                "radarrId": 7,
                "title": "Movie",
                "missing_subtitles": [{"code2": "et"}],
            }
            subtitles = []
            state = app._validation_state
            stats = defaultdict(int)
            stats["translations"] = []

            def completed(*_args):
                provider_target.write_text(make_srt("Tere"), encoding="utf-8")
                return "Completed"

            report = SimpleNamespace(issues=[])
            with (
                patch.multiple(
                    app,
                    LANGUAGES=["en", "et"],
                    CLEANUP_UNDERSIZED_ENABLED=False,
                    _validation_state=state,
                    fetch_subtitles=lambda *_args: (str(video), subtitles),
                    lingarr_resolve_media_id=lambda *_args: 99,
                    lingarr_get_active_translations=lambda: [],
                    lingarr_submit_file=lambda *_args: 123,
                    lingarr_poll_job=completed,
                    _count_dialogue_lines=lambda _path: 1,
                    _estimate_timeout=lambda _path: 60,
                    _validate_translated_file=lambda *_args, **_kwargs: ("valid", report),
                ),
                patch.object(
                    app, "_normalize_managed_output", return_value=True
                ) as normalize_output,
            ):
                app.process_item(item, "movies", "radarrId", stats, threading.Lock())
                identity = app._target_identity_from_sidecar(target, "et")
                metadata = state.find_submission(identity, "et")

            self.assertEqual(stats["completed"], 1)
            self.assertEqual(stats["variant_outputs_discovered"], 0)
            self.assertFalse(provider_target.exists())
            self.assertEqual(
                os.path.normcase(metadata["expectedTargetPath"]),
                os.path.normcase(str(target)),
            )
            self.assertEqual(
                os.path.normcase(metadata["actualTargetPath"]),
                os.path.normcase(str(target)),
            )
            self.assertEqual(metadata["targetVariant"], ".hi")
            normalize_output.assert_called_once_with(str(target), "Movie")
            self.assertEqual(state.matching_origin(target, app._file_hash_or_none(target)), "lingarr")

    def test_status_marks_repaired_validation_as_accepted_subtype(self):
        class Recorder:
            def __init__(self):
                self.calls = []

            def transition_for(self, *_args, **kwargs):
                self.calls.append((_args[-1], kwargs))
                return True

        recorder = Recorder()
        with patch.object(app, "_status_tracker", recorder):
            app._status_finish_validation("movies", 7, "et", "repaired")

        self.assertEqual(recorder.calls, [("accepted", {"repaired": True, "reason": None})])

    def test_status_persistence_failure_does_not_stop_translation_flow(self):
        class BrokenTracker:
            def transition_for(self, *_args, **_kwargs):
                raise OSError("disk full")

        with patch.object(app, "_status_tracker", BrokenTracker()):
            updated = app._status_transition("movies", 7, "et", "translating")

        self.assertFalse(updated)

    def test_ffprobe_failure_returns_safe_none(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "movie.mkv"
            video.write_bytes(b"video")
            failed = SimpleNamespace(returncode=1, stdout="", stderr="probe failed")

            with (
                patch.multiple(app, _duration_cache={}),
                patch.object(app.subprocess, "run", return_value=failed),
            ):
                duration = app._probe_media_duration(video)

            self.assertIsNone(duration)
            self.assertTrue(video.exists())

    def test_unknown_independently_segmented_target_skips_exact_alignment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "movie.eng.srt"
            target = root / "movie.et.srt"
            source.write_text(make_multi_srt(*(["English dialogue"] * 8)), encoding="utf-8")
            target.write_text(
                make_multi_srt(*[
                    f"See on korralik eestikeelne subtiitrite dialoog number {index}."
                    for index in range(1, 8)
                ]),
                encoding="utf-8",
            )
            state = app._validation_state

            with patch.multiple(app, _validation_state=state, CLEANUP_LANGUAGES={"et"}):
                action, report = app._validate_translated_file(
                    str(source), str(target), "en", "et", None, dry_run=True
                )

            self.assertEqual(action, "valid")
            self.assertTrue(report.valid, report.summary())
            self.assertTrue(target.exists())

    def test_source_less_excessive_lines_only_is_retained_and_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "movie.et.srt"
            target.write_text(
                make_srt("Üks\nKaks\nKolm\nNeli\nViis"),
                encoding="utf-8",
            )
            state = app._validation_state

            with patch.multiple(
                app,
                CLEANUP_LANGUAGES={"et"},
                CLEANUP_SOURCELESS_LINE_ONLY_ACTION="warn",
                _validation_state=state,
            ):
                action, report = app._validate_translated_file(
                    str(root / "missing.eng.srt"),
                    str(target),
                    "en",
                    "et",
                    None,
                )

            target_hash = app._file_hash_or_none(target)
            self.assertEqual(action, "valid-warning")
            self.assertEqual({issue.rule for issue in report.issues}, {"excessive_lines"})
            self.assertTrue(target.exists())
            self.assertTrue(state.is_unchanged_valid(target, None, target_hash))

    def test_source_less_line_count_plus_prompt_marker_is_quarantined(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "movie.et.srt"
            target.write_text(
                make_srt("[CONTEXT]\nÜks\nKaks\nKolm\nNeli"),
                encoding="utf-8",
            )
            quarantine = root / "quarantine"
            state = app._validation_state

            with patch.multiple(
                app,
                CLEANUP_LANGUAGES={"et"},
                CLEANUP_SOURCELESS_LINE_ONLY_ACTION="warn",
                CLEANUP_ACTION="quarantine",
                CLEANUP_ROOTS=[root],
                CLEANUP_QUARANTINE_DIR=quarantine,
                _validation_state=state,
            ):
                action, report = app._validate_translated_file(
                    str(root / "missing.eng.srt"),
                    str(target),
                    "en",
                    "et",
                    None,
                )

            self.assertEqual(action, "quarantined")
            self.assertIn("prompt_marker", {issue.rule for issue in report.issues})
            self.assertFalse(target.exists())

    def test_repeat_invalid_hash_suppresses_ai_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "movie.eng.srt"
            target = root / "movie.et.srt"
            source.write_text(make_srt("English"), encoding="utf-8")
            target.write_text(make_srt("Üks\nKaks\nKolm\nNeli\nViis"), encoding="utf-8")
            state = app._validation_state
            target_hash = app._file_hash_or_none(target)
            identity = app._quarantine_identity("et", target_path=target)
            state.record_quarantine_event(
                identity,
                target_path=target,
                target_hash=target_hash,
                target_language="et",
                rules=["excessive_lines"],
                origin="lingarr",
            )
            self._record_lingarr_artifact(source, target)

            with (
                patch.multiple(
                    app,
                    CLEANUP_LANGUAGES={"et"},
                    CLEANUP_ACTION="report",
                    CLEANUP_REPAIR_ENABLED=True,
                    _validation_state=state,
                ),
                patch.object(app, "_queue_repair") as queue_repair,
            ):
                action, report = app._validate_translated_file(
                    str(source),
                    str(target),
                    "en",
                    "et",
                    None,
                    origin="lingarr",
                    provenance_source_hash=app._file_hash_or_none(source),
                )

            self.assertEqual(action, "reported")
            self.assertTrue(report.ai_repair_suppressed)
            queue_repair.assert_not_called()

    def test_quarantine_suppresses_only_the_active_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "movie.mkv"
            source = root / "movie.eng.srt"
            target = root / "movie.et.srt"
            video.write_bytes(b"video")
            source.write_text(make_srt("English"), encoding="utf-8")
            invalid = make_srt("[CONTEXT]\nÜks\nKaks\nKolm\nNeli")
            target.write_text(invalid, encoding="utf-8")
            state = app._validation_state
            quarantine = root / "quarantine"

            common = dict(
                CLEANUP_LANGUAGES={"et"},
                CLEANUP_ACTION="quarantine",
                CLEANUP_ROOTS=[root],
                CLEANUP_QUARANTINE_DIR=quarantine,
                CLEANUP_REPAIR_ENABLED=False,
                _validation_state=state,
            )
            with patch.multiple(app, **common):
                first_action, first_report = app._validate_translated_file(
                    str(source),
                    str(target),
                    "en",
                    "et",
                    7,
                    origin="lingarr",
                    provenance_source_hash=app._file_hash_or_none(source),
                )
                target.write_text(invalid, encoding="utf-8")
                second_action, second_report = app._validate_translated_file(
                    str(source),
                    str(target),
                    "en",
                    "et",
                    7,
                    origin="lingarr",
                    provenance_source_hash=app._file_hash_or_none(source),
                )

            self.assertEqual(first_action, "quarantined")
            self.assertEqual(second_action, "quarantined")
            self.assertFalse(first_report.repeat_offender)
            self.assertTrue(second_report.repeat_offender)
            event = state.quarantine_event(
                app._quarantine_identity("et", video_path=video)
            )
            self.assertEqual(event["occurrences"], 2)

            stats = defaultdict(int)
            stats["translations"] = []
            item = {
                "radarrId": 7,
                "title": "Movie",
                "missing_subtitles": [{"code2": "et"}],
            }
            subtitles = [{"code2": "en", "path": str(source), "forced": False}]
            with (
                patch.multiple(
                    app,
                    LANGUAGES=["en", "et"],
                    CLEANUP_UNDERSIZED_ENABLED=False,
                    _validation_state=state,
                    fetch_subtitles=lambda *_args: (str(video), subtitles),
                    lingarr_resolve_media_id=lambda *_args: 99,
                ),
                patch.object(app, "lingarr_submit_file") as submit,
            ):
                app.process_item(item, "movies", "radarrId", stats, threading.Lock())

            self.assertEqual(stats["cycle_suppressions"], 1)
            self.assertEqual(stats["deferred"], 1)
            submit.assert_not_called()
            app._cycle_suppressions.begin_cycle("next-cycle")
            self.assertIsNone(
                app._cycle_quarantine_suppression(video, "et")
            )

    def test_changed_valid_replacement_resolves_quarantine_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "movie.et.srt"
            target.write_text(make_srt("Katki\nÜks\nKaks\nKolm\nNeli"), encoding="utf-8")
            state = app._validation_state
            identity = app._quarantine_identity("et", target_path=target)
            state.record_quarantine_event(
                identity,
                target_path=target,
                target_hash=app._file_hash_or_none(target),
                target_language="et",
                rules=["excessive_lines"],
                origin="unknown",
            )
            target.write_text(make_srt("See on korras."), encoding="utf-8")

            with patch.multiple(
                app,
                LANGUAGES=["en", "et"],
                CLEANUP_LANGUAGES={"et"},
                _validation_state=state,
            ):
                action, report = app._validate_translated_file(
                    str(root / "missing.eng.srt"),
                    str(target),
                    "en",
                    "et",
                    None,
                )

            self.assertEqual(action, "valid")
            self.assertTrue(report.valid)
            self.assertIsNotNone(
                state.quarantine_event(identity)["resolvedAt"]
            )

    def test_recorded_lingarr_target_keeps_exact_alignment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "movie.eng.srt"
            target = root / "movie.et.srt"
            source.write_text(make_multi_srt(*(["English dialogue"] * 8)), encoding="utf-8")
            target.write_text(
                make_multi_srt(*(["See on korralik eestikeelne subtiitrite dialoog."] * 7)),
                encoding="utf-8",
            )
            state = app._validation_state
            self._record_lingarr_artifact(source, target)

            with patch.multiple(app, _validation_state=state, CLEANUP_LANGUAGES={"et"}):
                action, report = app._validate_translated_file(
                    str(source), str(target), "en", "et", None, dry_run=True
                )

            self.assertEqual(action, "dry-run")
            self.assertIn("cue_count_mismatch", {issue.rule for issue in report.issues})

    def test_changed_source_drops_stale_exact_alignment_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "movie.eng.srt"
            target = root / "movie.et.srt"
            source.write_text(
                make_multi_srt("New source one", "New source two"),
                encoding="utf-8",
            )
            target.write_text(make_srt("See on korras."), encoding="utf-8")
            state = app._validation_state
            state.record(
                target,
                source_hash="old-source-hash",
                target_hash=app._file_hash_or_none(target),
                result="pending_validation",
                origin="lingarr",
            )

            with patch.multiple(app, _validation_state=state, CLEANUP_LANGUAGES={"et"}):
                action, report = app._validate_translated_file(
                    str(source), str(target), "en", "et", None, dry_run=True
                )

            self.assertEqual(action, "valid")
            self.assertTrue(report.valid)

    def test_completed_lingarr_output_outside_cleanup_languages_checks_completeness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "movie.eng.srt"
            target = root / "movie.sv.srt"
            source.write_text(make_timed_srt(150, 3500, "Complete source dialogue"), encoding="utf-8")
            target.write_text(make_timed_srt(3, 3500, "Fragment"), encoding="utf-8")
            quarantine = root / "quarantine"
            state = app._validation_state

            with patch.multiple(
                app,
                CLEANUP_LANGUAGES={"et"},
                CLEANUP_ROOTS=[root],
                CLEANUP_ACTION="quarantine",
                CLEANUP_QUARANTINE_DIR=quarantine,
                _validation_state=state,
            ):
                action, report = app._validate_translated_file(
                    str(source),
                    str(target),
                    "en",
                    "sv",
                    None,
                    media_duration=3600.0,
                    origin="lingarr",
                    provenance_source_hash=app._file_hash_or_none(source),
                )

            self.assertEqual(action, "quarantined")
            self.assertIn("undersized_subtitle", {issue.rule for issue in report.issues})
            self.assertFalse(target.exists())
            audit = json.loads(
                (quarantine / "movie.sv.srt.validation.json").read_text(encoding="utf-8")
            )
            self.assertEqual(audit["origin"], "lingarr")

    def test_existing_hash_validation_seeds_metadata_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "media"
            root.mkdir()
            (root / "show.eng.srt").write_text(make_srt("Good evening"), encoding="utf-8")
            target = root / "show.et.srt"
            target.write_text(make_srt("Tere õhtust"), encoding="utf-8")
            state = app._validation_state

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
                state.delete_maintenance_cache_entries([target])
                second = app.run_existing_cleanup_scan()
                third = app.run_existing_cleanup_scan()

            self.assertEqual(first["files_checked"], 2)
            self.assertEqual(second["files_checked"], 0)
            self.assertEqual(second["skipped_unchanged"], 2)
            self.assertEqual(second["cache_hits"], 1)
            self.assertEqual(third["skipped_unchanged"], 2)
            self.assertEqual(third["cache_hits"], 2)

    def test_wrong_language_managed_sidecar_is_quarantined_outside_ai_scope(self):
        fixture_root = REPO_ROOT / "examples" / "LanguageValidationFailure" / "Shameless-S09E12"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "media"
            quarantine = Path(directory) / "quarantine"
            root.mkdir()
            video = root / "Shameless (US) (2011) - S09E12.mkv"
            video.write_bytes(b"video")
            copied = {}
            for language in ("en", "et", "sv"):
                source = next(fixture_root.glob(f"*.{language}.srt"))
                target = root / f"Shameless (US) (2011) - S09E12.{language}.srt"
                target.write_bytes(source.read_bytes())
                copied[language] = target

            outcomes = []
            with (
                patch.multiple(
                    app,
                    LANGUAGES=["en", "et", "sv"],
                    CLEANUP_LANGUAGES={"et"},
                    CLEANUP_ROOTS=[root],
                    CLEANUP_QUARANTINE_DIR=quarantine,
                    CLEANUP_SCAN_EXISTING=True,
                    CLEANUP_SCAN_DRY_RUN=False,
                    CLEANUP_ACTION="quarantine",
                ),
                patch.object(app, "_probe_media_duration", return_value=3600.0),
                patch.object(app, "_tracked_bazarr_sync", return_value=True) as sync,
                patch.object(app, "lingarr_translate_line") as cue_repair,
                patch.object(
                    app, "_status_record_maintenance_outcome",
                    side_effect=lambda *args, **kwargs: outcomes.append((args, kwargs)),
                ),
            ):
                stats = app.run_existing_cleanup_scan()

            self.assertTrue(copied["en"].exists())
            self.assertTrue(copied["et"].exists())
            self.assertFalse(copied["sv"].exists())
            quarantined = quarantine / copied["sv"].name
            self.assertTrue(quarantined.exists())
            self.assertEqual(stats["quarantined_files"], 1)
            cue_repair.assert_not_called()
            sync.assert_called_once_with(True, True, app.SYNC_TIMEOUT)
            language_outcome = next(
                (entry for entry in outcomes if entry[0][0] == "language_validation"),
                None,
            )
            self.assertIsNotNone(language_outcome)
            self.assertEqual(language_outcome[0][1], "quarantined")
            self.assertEqual(
                language_outcome[1]["reason"],
                "expected sv; detected ENGLISH 1.00",
            )

            app._cycle_suppressions.begin_cycle(f"{self.id()}:regeneration")
            regeneration_stats = defaultdict(int)
            regeneration_stats["translations"] = []
            submit = Mock(return_value=321)
            validate = Mock(return_value=("valid", SimpleNamespace(issues=[])))

            def completed_with_swedish(*_args, **_kwargs):
                copied["sv"].write_text(
                    make_multi_srt(*(["Det här är en svensk undertextrad."] * 7)),
                    encoding="utf-8",
                )
                return "Completed"

            with patch.multiple(
                app,
                LANGUAGES=["en", "et", "sv"],
                CLEANUP_UNDERSIZED_ENABLED=False,
                fetch_subtitles=lambda *_args: (
                    str(video),
                    [
                        {"code2": "en", "path": str(copied["en"]), "forced": False},
                        {"code2": "et", "path": str(copied["et"]), "forced": False},
                    ],
                ),
                lingarr_resolve_media_id=lambda *_args: 99,
                lingarr_get_active_translations=lambda: [],
                lingarr_submit_file=submit,
                lingarr_poll_job=completed_with_swedish,
                _count_dialogue_lines=lambda _path: 100,
                _estimate_timeout=lambda _path: 60,
                _record_submission=lambda *_args, **_kwargs: 44,
                _mark_submission_submitted=lambda *_args, **_kwargs: None,
                _mark_submission_failed=lambda *_args, **_kwargs: None,
                _update_submission_actual_path=lambda *_args, **_kwargs: None,
                _record_pending_lingarr_output=lambda *_args, **_kwargs: True,
                _validate_translated_file=validate,
            ):
                app.process_item(
                    {
                        "tvdbId": 900,
                        "title": "Shameless (US) S09E12",
                        "missing_subtitles": [{"code2": "sv"}],
                    },
                    "episodes", "tvdbId", regeneration_stats, threading.Lock(),
                )

            self.assertTrue(copied["sv"].exists())
            submit.assert_called_once()
            validate.assert_called_once()
            self.assertEqual(regeneration_stats["completed"], 1)

    def test_short_wrong_language_file_outside_ai_scope_is_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "media"
            quarantine = Path(directory) / "quarantine"
            root.mkdir()
            video = root / "show.mkv"
            target = root / "show.sv.srt"
            video.write_bytes(b"video")
            target.write_text(make_srt("This is English"), encoding="utf-8")
            with (
                patch.multiple(
                    app,
                    LANGUAGES=["en", "et", "sv"],
                    CLEANUP_LANGUAGES={"et"},
                    CLEANUP_ROOTS=[root],
                    CLEANUP_QUARANTINE_DIR=quarantine,
                    CLEANUP_SCAN_EXISTING=True,
                    CLEANUP_SCAN_DRY_RUN=False,
                    CLEANUP_ACTION="quarantine",
                    CLEANUP_UNDERSIZED_ENABLED=False,
                ),
                patch.object(app, "_probe_media_duration", return_value=3600.0),
                patch.object(app, "_tracked_bazarr_sync") as sync,
            ):
                stats = app.run_existing_cleanup_scan()

            self.assertTrue(target.exists())
            self.assertFalse(quarantine.exists())
            self.assertEqual(stats["files_checked"], 1)
            self.assertEqual(stats["reported_files"], 0)
            sync.assert_not_called()

    def test_wrong_language_without_usable_completeness_is_reported_not_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "media"
            quarantine = Path(directory) / "quarantine"
            root.mkdir()
            video = root / "show.mkv"
            target = root / "show.sv.srt"
            video.write_bytes(b"video")
            target.write_text(
                make_timed_srt(
                    150, 3500, "This is clearly an English dialogue sentence."
                ),
                encoding="utf-8",
            )
            with patch.multiple(
                app,
                LANGUAGES=["sv"],
                CLEANUP_LANGUAGES=set(),
                CLEANUP_ROOTS=[root],
                CLEANUP_QUARANTINE_DIR=quarantine,
                CLEANUP_SCAN_EXISTING=True,
                CLEANUP_SCAN_DRY_RUN=False,
                CLEANUP_ACTION="quarantine",
                CLEANUP_UNDERSIZED_ENABLED=True,
            ):
                with (
                    patch.object(app, "_probe_media_duration", return_value=None),
                    patch.object(app, "_tracked_bazarr_sync") as sync,
                ):
                    first = app.run_existing_cleanup_scan()

                self.assertTrue(target.exists())
                self.assertFalse(quarantine.exists())
                self.assertEqual(first["reported_files"], 1)
                sync.assert_not_called()

                with (
                    patch.object(app, "_probe_media_duration", return_value=3600.0),
                    patch.object(app, "_tracked_bazarr_sync", return_value=True) as sync,
                ):
                    second = app.run_existing_cleanup_scan()

            self.assertEqual(second["cache_hits"], 0)
            self.assertEqual(second["quarantined_files"], 1)
            self.assertFalse(target.exists())
            self.assertTrue((quarantine / target.name).exists())
            sync.assert_called_once_with(True, True, app.SYNC_TIMEOUT)

    def test_source_less_managed_language_is_validated_when_ai_scope_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "media"
            quarantine = Path(directory) / "quarantine"
            root.mkdir()
            video = root / "show.mkv"
            target = root / "show.sv.srt"
            video.write_bytes(b"video")
            target.write_text(
                make_timed_srt(150, 3500, "This is complete English dialogue"),
                encoding="utf-8",
            )

            with (
                patch.multiple(
                    app,
                    LANGUAGES=["sv"],
                    CLEANUP_LANGUAGES=set(),
                    CLEANUP_ROOTS=[root],
                    CLEANUP_QUARANTINE_DIR=quarantine,
                    CLEANUP_SCAN_EXISTING=True,
                    CLEANUP_SCAN_DRY_RUN=False,
                    CLEANUP_ACTION="quarantine",
                ),
                patch.object(app, "_probe_media_duration", return_value=3600.0),
                patch.object(app, "_tracked_bazarr_sync", return_value=True) as sync,
            ):
                stats = app.run_existing_cleanup_scan()

            self.assertFalse(target.exists())
            self.assertTrue((quarantine / target.name).exists())
            self.assertEqual(stats["files_checked"], 1)
            self.assertEqual(stats["quarantined_files"], 1)
            sync.assert_called_once_with(True, True, app.SYNC_TIMEOUT)

    def test_legacy_target_only_cache_is_revalidated_for_managed_language(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "media"
            quarantine = Path(directory) / "quarantine"
            root.mkdir()
            video = root / "show.mkv"
            target = root / "show.sv.srt"
            video.write_bytes(b"video")
            target.write_text(
                make_timed_srt(150, 3500, "This is complete English dialogue"),
                encoding="utf-8",
            )
            target_hash = subtitle_foundation.file_sha256(target)
            app._validation_state.record(
                target,
                source_hash=None,
                target_hash=target_hash,
                result="valid",
                origin="external",
                validator_version="source-aware-v4-completeness-provenance",
            )

            with (
                patch.multiple(
                    app,
                    LANGUAGES=["sv"],
                    CLEANUP_LANGUAGES=set(),
                    CLEANUP_ROOTS=[root],
                    CLEANUP_QUARANTINE_DIR=quarantine,
                    CLEANUP_SCAN_EXISTING=True,
                    CLEANUP_SCAN_DRY_RUN=False,
                    CLEANUP_ACTION="quarantine",
                ),
                patch.object(app, "_probe_media_duration", return_value=3600.0),
                patch.object(app, "_tracked_bazarr_sync", return_value=True),
            ):
                stats = app.run_existing_cleanup_scan()

            self.assertEqual(stats["files_checked"], 1)
            self.assertEqual(stats["skipped_unchanged"], 0)
            self.assertFalse(target.exists())
            self.assertTrue((quarantine / target.name).exists())
    def test_format_only_recovery_does_not_call_lingarr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "show.eng.srt"
            target = root / "show.et.srt"
            state = app._validation_state
            source.write_text(make_multi_srt("First line", "Second cue"), encoding="utf-8")
            target.write_text(
                "1\n00:00:01,000 --> 00:00:01,900\nEsimene\n\nteine rida\n\n"
                "2\n00:00:02,000 --> 00:00:02,900\nTeine subtiiter\n",
                encoding="utf-8",
            )
            self._record_lingarr_artifact(source, target)

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
                    str(source),
                    str(target),
                    "en",
                    "et",
                    None,
                    title="show",
                    origin="lingarr",
                    provenance_source_hash=app._file_hash_or_none(source),
                )

            self.assertEqual(action, "formatted")
            self.assertTrue(report.valid)
            translate.assert_not_called()
            self.assertIn("Esimene\nteine rida", target.read_text(encoding="utf-8"))

    def test_invariant_observation_skips_repair_and_persists_as_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "Top Gear S14E01.eng.srt"
            target = root / "Top Gear S14E01.et.srt"
            source.write_text(
                "442\n00:23:44,000 --> 00:23:45,000\nBefore the car.\n\n"
                "443\n00:23:46,000 --> 00:23:48,000\nAston Martin DB9\n\n"
                "444\n00:23:49,000 --> 00:23:51,000\nAfter the car.\n",
                encoding="utf-8",
            )
            target.write_text(
                "442\n00:23:44,000 --> 00:23:45,000\nEnne autot.\n\n"
                "443\n00:23:46,000 --> 00:23:48,000\nAston Martin DB9\n\n"
                "444\n00:23:49,000 --> 00:23:51,000\nPÃ¤rast autot.\n",
                encoding="utf-8",
            )
            self._record_lingarr_artifact(source, target)
            tracker = app.StatusTracker(
                root / "status.json", root / "status_history.jsonl"
            )

            with (
                patch.multiple(
                    app,
                    CLEANUP_LANGUAGES={"et"},
                    CLEANUP_FORMAT_REPAIR_ENABLED=False,
                    CLEANUP_REPAIR_ENABLED=True,
                    _status_tracker=tracker,
                ),
                patch.object(app, "lingarr_translate_line") as translate,
            ):
                action, report = app._validate_translated_file(
                    str(source), str(target), "en", "et", 42,
                    title="Top Gear", item_type="episodes", origin="lingarr",
                    provenance_source_hash=app._file_hash_or_none(source),
                )

            translate.assert_not_called()
            self.assertEqual(action, "valid")
            self.assertTrue(report.valid)
            self.assertEqual(report.repairable_cue_indexes, [])
            self.assertEqual(report.observations[0].cue_number, 443)
            target_hash = app._file_hash_or_none(target)
            record = app._validation_state.matching_record(target, target_hash)
            self.assertEqual(record["result"], "valid_with_warnings")
            self.assertEqual(
                record["details"]["validation"]["observations"][0]["cueNumber"],
                443,
            )
            public = tracker.snapshot()["validationObservations"]
            self.assertEqual((public[0]["title"], public[0]["cueNumber"]), ("Top Gear", 443))
            serialized = json.dumps(public)
            self.assertNotIn(str(target), serialized)
            self.assertNotIn("Aston Martin DB9", serialized)

            database = Path(self._state_directory.name) / "state.sqlite3"
            app._validation_state.close()
            app._validation_state = StateStore(
                database, validator_version=cleanup.VALIDATOR_VERSION
            )
            reloaded = app._validation_state.matching_record(target, target_hash)
            self.assertEqual(reloaded["result"], "valid_with_warnings")
            self.assertEqual(
                reloaded["details"]["validation"]["observations"][0]["classification"],
                "likely_invariant",
            )

    def test_repair_logs_attempts_without_dialogue_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "show.eng.srt"
            target = root / "show.et.srt"
            state = app._validation_state
            source.write_text(
                make_multi_srt("Before secret", "Target secret dialogue", "After secret"),
                encoding="utf-8",
            )
            target.write_text(
                make_multi_srt("Enne", "[SOURCE] leaked [/SOURCE]", "Pärast"),
                encoding="utf-8",
            )
            self._record_lingarr_artifact(source, target)
            responses = [
                "[SOURCE] leaked one [/SOURCE]",
                "[SOURCE] leaked two [/SOURCE]",
                "[SOURCE] leaked three [/SOURCE]",
                "[SOURCE] leaked four [/SOURCE]",
                "Parandatud",
            ]

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
                    CLEANUP_MAX_REPAIR_ATTEMPTS=5,
                    _validation_state=state,
                ),
                patch.object(app, "lingarr_translate_line", side_effect=translate),
                redirect_stdout(output),
            ):
                action, _ = app._validate_translated_file(
                    str(source),
                    str(target),
                    "en",
                    "et",
                    None,
                    title="show",
                    origin="lingarr",
                    provenance_source_hash=app._file_hash_or_none(source),
                )

            logs = output.getvalue()
            self.assertEqual(action, "repaired")
            self.assertIn("attempt 1/5 with context before=1 after=1", logs)
            self.assertIn("attempt 5/5 without context", logs)
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
            state = app._validation_state

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
                    CLEANUP_SOURCELESS_LINE_ONLY_ACTION="quarantine",
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
            state = app._validation_state

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
                    CLEANUP_SOURCELESS_LINE_ONLY_ACTION="quarantine",
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

    def test_repair_completion_updates_status_before_cycle_drain(self):
        started = threading.Event()
        release = threading.Event()
        report = SimpleNamespace(repairable_cue_indexes=[0], issues=[])

        def worker(**kwargs):
            started.set()
            release.wait(2)
            return app.RepairJobResult(
                "repaired", report, "Repair Show", "et", "episodes", 42,
                attempts=1, target_path="repair-show.et.srt",
            )

        work = [
            ({
                "sonarrEpisodeId": 42,
                "seriesTitle": "Repair Show",
                "missing_subtitles": [{"code2": "et"}],
            }, "episodes", "sonarrEpisodeId"),
            ({
                "sonarrEpisodeId": 43,
                "seriesTitle": "Still Translating",
                "missing_subtitles": [{"code2": "et"}],
            }, "episodes", "sonarrEpisodeId"),
        ]
        jobs = app.build_cycle_jobs(
            work, ["et"], "cycle-repair", lambda item, _kind: item["seriesTitle"]
        )
        stats = {
            "submitted": 0,
            "completed": 0,
            "failed": 0,
            "translations": [],
            "episode_activity": False,
            "movie_activity": False,
        }

        with tempfile.TemporaryDirectory() as directory:
            tracker = app.StatusTracker(
                Path(directory) / "status.json",
                Path(directory) / "status_history.jsonl",
            )
            tracker.start_cycle("cycle-repair", 1, jobs)
            tracker.transition_for("episodes", 42, "et", "repairing")
            tracker.transition_for("episodes", 43, "et", "translating")

            with (
                patch.object(app, "_status_tracker", tracker),
                patch.object(app, "_perform_repair", side_effect=worker),
                patch.object(app, "_repair_capacity", threading.BoundedSemaphore(2)),
            ):
                queued = app._queue_repair(
                    ("repair-show", "hash"),
                    {
                        "item_type": "episodes",
                        "item_id": 42,
                        "target_lang": "et",
                    },
                    report,
                    "Repair Show",
                    "et",
                )
                self.assertEqual(queued, "repair-queued")
                self.assertTrue(started.wait(1))
                release.set()

                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    snapshot = tracker.snapshot()
                    if snapshot["currentCycle"]["accepted"] == 1:
                        break
                    time.sleep(0.01)
                else:
                    self.fail("repair status did not become terminal after worker completion")

                self.assertEqual(snapshot["currentCycle"]["repairing"], 0)
                self.assertEqual(snapshot["currentCycle"]["translating"], 1)
                self.assertEqual(snapshot["currentCycle"]["accepted"], 1)
                self.assertEqual(
                    [(job["itemId"], job["state"]) for job in snapshot["activeJobs"]],
                    [(43, "translating")],
                )
                self.assertTrue(snapshot["recentOutcomes"][0]["repaired"])
                self.assertEqual(snapshot["history"]["1h"]["accepted"], 1)
                self.assertEqual(snapshot["history"]["1h"]["repaired"], 1)
                self.assertEqual(stats["completed"], 0)

                results = app._drain_pending_repairs(stats)
                drained_snapshot = tracker.snapshot()

            self.assertEqual(len(results), 1)
            self.assertEqual(stats["completed"], 1)
            self.assertEqual(drained_snapshot["history"]["1h"]["accepted"], 1)
            self.assertEqual(len(drained_snapshot["recentOutcomes"]), 1)

    def test_existing_library_repair_uses_maintenance_status_job(self):
        started = threading.Event()
        release = threading.Event()
        report = SimpleNamespace(repairable_cue_indexes=[1288, 1289], issues=[])

        def worker(**_kwargs):
            started.set()
            release.wait(2)
            return app.RepairJobResult(
                "repaired", report, "Shameless (US) S06E03", "et", None, None,
                attempts=1, target_path="ignored.srt",
            )

        with tempfile.TemporaryDirectory() as directory:
            tracker = app.StatusTracker(
                Path(directory) / "status.json",
                Path(directory) / "history.jsonl",
            )
            with (
                patch.object(app, "_status_tracker", tracker),
                patch.object(app, "_perform_repair", side_effect=worker),
                patch.object(app, "_repair_capacity", threading.BoundedSemaphore(2)),
            ):
                queued = app._queue_repair(
                    ("existing", "hash"),
                    {
                        "source_lang": "en",
                        "target_lang": "et",
                        "title": "Shameless (US) S06E03",
                    },
                    report,
                    "Shameless (US) S06E03",
                    "et",
                )
                self.assertEqual(queued, "repair-queued")
                self.assertTrue(started.wait(1))
                active = tracker.snapshot()["maintenance"]["activeJobs"]
                self.assertEqual(len(active), 1)
                self.assertEqual(active[0]["operation"], "cue_repair")
                self.assertIn(
                    active[0]["state"],
                    {"repair_waiting_capacity", "repairing"},
                )
                release.set()
                app._drain_pending_repairs({
                    "submitted": 0,
                    "completed": 0,
                    "failed": 0,
                    "translations": [],
                    "episode_activity": False,
                    "movie_activity": False,
                })
            snapshot = tracker.snapshot()
            self.assertEqual(snapshot["maintenance"]["activeJobs"], [])
            self.assertEqual(
                snapshot["maintenance"]["recentOutcomes"][0]["outcome"],
                "repaired",
            )

    def test_cleanup_scan_waits_for_child_repair_and_records_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = app.StatusTracker(
                Path(directory) / "status.json",
                Path(directory) / "history.jsonl",
            )
            with patch.object(app, "_status_tracker", tracker):
                scan_id = app._status_create_maintenance(
                    "existing_library_scan",
                    {"title": "Existing subtitle library"},
                    state="scanning",
                )
                with app._maintenance_scan_contexts_lock:
                    app._maintenance_scan_contexts[scan_id] = {
                        "started": time.monotonic(),
                        "stats": {},
                        "files_discovered": 2,
                        "files_checked": 2,
                        "pending": 0,
                        "repairs_queued": 0,
                        "repairs_completed": 0,
                        "enumeration_done": False,
                        "last_publish": 0,
                    }
                app._scan_child_queued(scan_id)
                app._scan_enumeration_finished(
                    scan_id,
                    {
                        "files_checked": 1,
                        "skipped_unchanged": 1,
                        "formatted_files": 0,
                        "repair_failures": 0,
                        "action_failures": 0,
                        "prune_failures": 0,
                    },
                )
                waiting = tracker.snapshot()["maintenance"]["activeJobs"][0]
                self.assertEqual(waiting["state"], "waiting_repair_completion")
                self.assertEqual(waiting["filesChecked"], 2)
                self.assertEqual(waiting["cueRepairsQueued"], 1)
                app._scan_child_finished(scan_id, "repaired")
            snapshot = tracker.snapshot()
            self.assertEqual(snapshot["maintenance"]["activeJobs"], [])
            self.assertEqual(
                snapshot["maintenance"]["lastScan"]["metrics"]["repaired"], 1
            )

    def test_cleanup_scan_retries_terminal_persistence_without_double_counting(self):
        scan_id = "scan-retry"
        context = {
            "started": time.monotonic(),
            "stats": {},
            "files_discovered": 1,
            "files_checked": 1,
            "pending": 1,
            "repairs_queued": 1,
            "repairs_completed": 0,
            "enumeration_done": True,
            "last_publish": 0,
        }
        with app._maintenance_scan_contexts_lock:
            app._maintenance_scan_contexts[scan_id] = context
        complete = Mock(side_effect=[False, False, False, True])
        try:
            with (
                patch.object(app, "_status_complete_maintenance", complete),
                patch.object(app, "_status_record_maintenance") as record,
                patch.object(status_runtime, "_schedule_scan_finalization_retry") as schedule,
            ):
                self.assertFalse(app._scan_child_finished(scan_id, "repaired"))
                self.assertIs(app._maintenance_scan_contexts[scan_id], context)
                self.assertEqual(context["pending"], 0)
                self.assertEqual(context["repairs_completed"], 1)
                self.assertEqual(context["stats"]["async_repairs_completed"], 1)

                self.assertTrue(app._scan_child_finished(scan_id, "repaired"))

            self.assertNotIn(scan_id, app._maintenance_scan_contexts)
            self.assertEqual(complete.call_count, 4)
            record.assert_called_once()
            schedule.assert_called_once_with(scan_id, context)
            self.assertEqual(context["repairs_completed"], 1)
            self.assertEqual(context["stats"]["async_repairs_completed"], 1)
        finally:
            with app._maintenance_scan_contexts_lock:
                app._maintenance_scan_contexts.pop(scan_id, None)

    def test_zero_child_scan_retries_terminal_persistence(self):
        scan_id = "scan-zero-child-retry"
        context = {
            "started": time.monotonic(),
            "stats": {},
            "files_discovered": 0,
            "files_checked": 0,
            "pending": 0,
            "repairs_queued": 0,
            "repairs_completed": 0,
            "enumeration_done": False,
            "last_publish": 0,
        }
        with app._maintenance_scan_contexts_lock:
            app._maintenance_scan_contexts[scan_id] = context
        complete = Mock(side_effect=[False, False, False, True])
        try:
            with (
                patch.object(app, "_status_complete_maintenance", complete),
                patch.object(app, "_status_record_maintenance") as record,
                patch.object(status_runtime, "_schedule_scan_finalization_retry") as schedule,
            ):
                app._scan_enumeration_finished(scan_id, {"files_checked": 0})
                self.assertIs(app._maintenance_scan_contexts[scan_id], context)
                self.assertTrue(context["finalization_pending"])
                schedule.assert_called_once_with(scan_id, context)

                self.assertTrue(app._retry_scan_finalization(scan_id, context))

            self.assertNotIn(scan_id, app._maintenance_scan_contexts)
            self.assertEqual(complete.call_count, 4)
            record.assert_called_once()
        finally:
            with app._maintenance_scan_contexts_lock:
                app._maintenance_scan_contexts.pop(scan_id, None)

    def test_repair_status_maps_terminal_outcomes_and_worker_errors(self):
        class Recorder:
            def __init__(self):
                self.calls = []

            def transition_for(self, *args, **kwargs):
                self.calls.append((args[-1], kwargs))
                return True

        report = SimpleNamespace(repairable_cue_indexes=[], issues=[])
        outcomes = [
            ("repaired", "accepted", {"repaired": True, "reason": None}),
            ("quarantined", "quarantined", {
                "repaired": False, "reason": "quarantined",
            }),
            ("deleted", "quarantined", {
                "repaired": False, "reason": "deleted",
            }),
            ("repair-deferred", "deferred", {
                "repaired": False, "reason": "repair deferred",
            }),
            ("kept", "failed", {
                "repaired": False, "reason": "repair kept",
            }),
        ]
        metadata = {
            "item_type": "episodes",
            "item_id": 42,
            "target_lang": "et",
        }
        recorder = Recorder()
        with patch.object(app, "_status_tracker", recorder):
            for action, _state, _kwargs in outcomes:
                future = app.Future()
                future.set_result(app.RepairJobResult(
                    action, report, "show", "et", "episodes", 42
                ))
                app._publish_repair_status(future, metadata)

            failed_future = app.Future()
            failed_future.set_exception(RuntimeError("boom"))
            app._publish_repair_status(failed_future, metadata)

        expected = [(state, kwargs) for _action, state, kwargs in outcomes]
        expected.append((
            "failed",
            {"repaired": False, "reason": "repair worker failed"},
        ))
        self.assertEqual(recorder.calls, expected)

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
