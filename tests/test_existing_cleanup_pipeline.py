import json
import os
import io
import sys
import tempfile
import threading
import time
import unittest
from collections import defaultdict
from types import SimpleNamespace
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch


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
from clean_et_subs import ValidationStateStore  # noqa: E402
from state_store import StateStore  # noqa: E402


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
        self._state_directory = tempfile.TemporaryDirectory()
        app._validation_state = StateStore(
            Path(self._state_directory.name) / "state.sqlite3",
            validator_version=cleanup.VALIDATOR_VERSION,
        )
        self._permissions_patcher = patch.object(
            cleanup, "normalize_managed_file", lambda _path: None
        )
        self._permissions_patcher.start()

    def tearDown(self):
        app._shutdown_repair_executor()
        app._translation_capacity.reset()
        with app._pending_repairs_lock:
            app._pending_repairs.clear()
            app._repair_keys.clear()
        self._permissions_patcher.stop()
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

    def test_submit_cache_loads_legacy_entries_and_ignores_malformed_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_file = Path(directory) / "submitted_cache.json"
            now = app.time.time()
            cache_file.write_text(
                json.dumps(
                    {
                        "42:et": now,
                        "not-an-id:et": {"submittedAt": now},
                        "broken": {"submittedAt": "not-a-time"},
                    }
                ),
                encoding="utf-8",
            )
            validation_file = Path(directory) / "validation_state.json"
            state = StateStore(Path(directory) / "migrated.sqlite3")
            try:
                result = state.migrate_legacy(
                    cache_file, validation_file, cooldown_seconds=3600
                )
                self.assertEqual(result["submissions"], 1)
                self.assertIsNotNone(state.check_cooldown("legacy", 42, "et"))
            finally:
                state.close()

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
        import clean_et_subs

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

    def test_process_item_accepts_actual_hi_output_and_persists_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "movie.mkv"
            source = root / "movie.en.hi.srt"
            target = root / "movie.et.hi.srt"
            video.write_bytes(b"video")
            source.write_text(make_srt("English dialogue"), encoding="utf-8")
            item = {
                "radarrId": 7,
                "title": "Movie",
                "missing_subtitles": [{"code2": "et"}],
            }
            subtitles = [{"code2": "en", "path": str(source), "forced": False}]
            state = app._validation_state
            stats = defaultdict(int)
            stats["translations"] = []

            def completed(*_args):
                target.write_text(make_srt("Tere"), encoding="utf-8")
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
            state.record_quarantine_tombstone(
                identity,
                target_path=target,
                target_hash=target_hash,
                target_language="et",
                rules=["excessive_lines"],
                origin="lingarr",
                hold_days=30,
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

    def test_two_cycle_repeat_quarantine_creates_translation_hold(self):
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
                CLEANUP_QUARANTINE_HOLD_DAYS=30,
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
            hold = state.active_quarantine_tombstone(
                app._quarantine_identity("et", video_path=video)
            )
            self.assertEqual(hold["occurrences"], 2)

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

            self.assertEqual(stats["quarantine_holds"], 1)
            self.assertEqual(stats["deferred"], 1)
            submit.assert_not_called()

    def test_changed_valid_replacement_clears_quarantine_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "movie.et.srt"
            target.write_text(make_srt("Katki\nÜks\nKaks\nKolm\nNeli"), encoding="utf-8")
            state = app._validation_state
            identity = app._quarantine_identity("et", target_path=target)
            state.record_quarantine_tombstone(
                identity,
                target_path=target,
                target_hash=app._file_hash_or_none(target),
                target_language="et",
                rules=["excessive_lines"],
                origin="unknown",
                hold_days=30,
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
            self.assertIsNone(state.active_quarantine_tombstone(identity))

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

    def test_existing_valid_file_is_scanned_then_skipped_by_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "media"
            root.mkdir()
            (root / "show.eng.srt").write_text(make_srt("Good evening"), encoding="utf-8")
            (root / "show.et.srt").write_text(make_srt("Tere õhtust"), encoding="utf-8")
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
                second = app.run_existing_cleanup_scan()

            self.assertEqual(first["files_checked"], 1)
            self.assertEqual(second["files_checked"], 0)
            self.assertEqual(second["skipped_unchanged"], 1)

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
                "[SOURCE] still leaked [/SOURCE]",
                "[SOURCE] still leaked [/SOURCE]",
                "[SOURCE] still leaked [/SOURCE]",
                "[SOURCE] still leaked [/SOURCE]",
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
                    ("repair-show", "hash"), {}, report, "Repair Show", "et"
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
