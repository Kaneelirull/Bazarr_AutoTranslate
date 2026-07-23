import sys
import tempfile
import unittest
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "docker"))

import clean_et_subs as cleanup  # noqa: E402
from clean_et_subs import (  # noqa: E402
    SubtitleCue,
    ValidationStateStore,
    build_detector,
    discover_target_subtitles,
    evaluate_subtitle_completeness,
    file_sha256,
    find_preferred_source,
    parse_srt_cues,
    purge_old_files,
    quarantine_subtitle,
    recover_srt_structure,
    repair_subtitle_file,
    target_language_for_code,
    validate_subtitle_pair,
    validate_subtitle_without_source,
    write_validation_report,
)
from state_store import StateStore  # noqa: E402


def make_srt(*texts: str) -> str:
    blocks = []
    for index, text in enumerate(texts, start=1):
        blocks.append(
            f"{index}\n00:00:{index:02d},000 --> 00:00:{index:02d},900\n{text}"
        )
    return "\n\n".join(blocks) + "\n"


def make_timed_srt(cue_count: int, final_second: int, text: str = "Dialogue line") -> str:
    blocks = []
    for index in range(1, cue_count + 1):
        second = max(1, int(final_second * index / cue_count))
        hours, remainder = divmod(second, 3600)
        minutes, seconds = divmod(remainder, 60)
        stamp = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        blocks.append(f"{index}\n{stamp},000 --> {stamp},900\n{text}")
    return "\n\n".join(blocks) + "\n"


class SubtitleValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.detector = build_detector()
        cls.estonian = target_language_for_code("et")

    def setUp(self):
        self._state_directory = tempfile.TemporaryDirectory()
        self.state = StateStore(
            Path(self._state_directory.name) / "state.sqlite3",
            validator_version=cleanup.VALIDATOR_VERSION,
        )
        self._permissions_patcher = patch.object(
            cleanup, "normalize_managed_file", lambda _path: None
        )
        self._permissions_patcher.start()

    def tearDown(self):
        self._permissions_patcher.stop()
        self.state.close()
        self._state_directory.cleanup()

    def validate_pair(self, source: Path, target: Path):
        return validate_subtitle_pair(
            source,
            target,
            self.detector,
            self.estonian,
            target_lang="et",
        )

    def test_supplied_example_detects_prompt_leak_at_cue_1049(self):
        source = next((REPO_ROOT / "examples").glob("*.eng.srt"))
        target = next((REPO_ROOT / "examples").glob("*.et.srt"))

        report = self.validate_pair(source, target)

        self.assertFalse(report.valid)
        cue_issues = [issue for issue in report.issues if issue.cue_number == 1049]
        self.assertIn("prompt_marker", {issue.rule for issue in cue_issues})
        self.assertIn("abnormal_expansion", {issue.rule for issue in cue_issues})
        self.assertIn("excessive_lines", {issue.rule for issue in cue_issues})
        self.assertIn(1048, report.repairable_cue_indexes)
        self.assertNotIn(611, report.repairable_cue_indexes)
        cyrillic_issue = [issue for issue in report.issues if issue.cue_number == 137]
        self.assertIn("unexpected_script", {issue.rule for issue in cyrillic_issue})

    def test_valid_aligned_pair_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "episode.eng.srt"
            target = root / "episode.et.srt"
            source.write_text(make_srt("Good evening.", "The car is fast."), encoding="utf-8")
            target.write_text(make_srt("Tere õhtust.", "Auto on kiire."), encoding="utf-8")

            report = self.validate_pair(source, target)

            self.assertTrue(report.valid, report.summary())

    def test_one_kilobyte_movie_subtitle_is_undersized(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "movie.et.srt"
            target.write_text(make_timed_srt(18, 7000, "Short line"), encoding="utf-8")
            result = evaluate_subtitle_completeness(target, 7200)

            self.assertTrue(result.evaluated)
            self.assertTrue(result.undersized)
            self.assertGreaterEqual(len(result.failed_signals), 3)
            self.assertIn("cue_density", result.failed_signals)

    def test_full_timeline_forced_fragment_still_fails_density(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "movie.eng.srt"
            target.write_text(make_timed_srt(21, 9500, "FOREIGN SIGN"), encoding="utf-8")
            result = evaluate_subtitle_completeness(target, 9600)

            self.assertTrue(result.undersized)
            self.assertGreater(result.timeline_coverage, 0.95)
            self.assertNotIn("timeline_coverage", result.failed_signals)
            self.assertEqual(
                {"cue_density", "text_density", "byte_density"},
                set(result.failed_signals),
            )

    def test_short_media_is_exempt_from_completeness_density(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "short.et.srt"
            target.write_text(make_timed_srt(2, 200, "Hi"), encoding="utf-8")
            result = evaluate_subtitle_completeness(target, 600)

            self.assertFalse(result.evaluated)
            self.assertFalse(result.undersized)
            self.assertIn("shorter", result.reason)

    def test_validation_state_only_returns_origin_for_matching_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "movie.et.srt"
            target.write_text(make_srt("Tere"), encoding="utf-8")
            target_hash = file_sha256(target)
            state = self.state
            state.record(
                target,
                source_hash="source",
                target_hash=target_hash,
                result="valid",
                origin="lingarr",
            )

            self.assertEqual(state.matching_origin(target, target_hash), "lingarr")
            self.assertIsNone(state.matching_origin(target, "changed"))

    def test_structural_mismatch_is_not_repairable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "episode.eng.srt"
            target = root / "episode.et.srt"
            source.write_text(make_srt("One", "Two"), encoding="utf-8")
            target.write_text(make_srt("Üks"), encoding="utf-8")

            report = self.validate_pair(source, target)

            self.assertIn("cue_count_mismatch", {issue.rule for issue in report.issues})
            self.assertEqual(report.repairable_cue_indexes, [])

    def test_source_anchored_recovery_folds_internal_blank_line_without_ai(self):
        source = make_srt("First source line", "Second source cue")
        target = (
            "\ufeff1\r\n00:00:01.000-->00:00:01.900\r\nEsimene rida   \r\n\r\n"
            "Teine rida\r\n\r\n\r\n2\r\n00:00:02,000 --> 00:00:02,900\r\nTeine subtiiter\r\n"
        )

        recovery = recover_srt_structure(source, target)

        self.assertTrue(recovery.safe, recovery.reason)
        self.assertTrue(recovery.changed)
        self.assertEqual(recovery.recovered_cues, [1])
        self.assertIn("removed_bom", recovery.fixes)
        cues, errors = parse_srt_cues(recovery.raw)
        self.assertEqual(errors, [])
        self.assertEqual(cues[0].lines, ["Esimene rida", "Teine rida"])

    def test_source_anchored_recovery_exposes_prompt_leak_for_cue_repair(self):
        source = make_srt("Did you get the Hulk reference?", "Forget it.")
        target = (
            "1\n00:00:01,000 --> 00:00:01,900\nPrevious translated context\n[/SOURCE]\n\n"
            "Sa ei saanud Hulki vihjest aru?\n\n"
            "2\n00:00:02,000 --> 00:00:02,900\nUnusta ära.\n"
        )

        recovery = recover_srt_structure(source, target)

        self.assertTrue(recovery.safe, recovery.reason)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "show.eng.srt"
            target_path = root / "show.et.srt"
            source_path.write_text(source, encoding="utf-8")
            target_path.write_text(recovery.raw, encoding="utf-8")
            report = self.validate_pair(source_path, target_path)
        self.assertIn("prompt_marker", {issue.rule for issue in report.issues})
        self.assertEqual(report.repairable_cue_indexes, [0])

    def test_source_anchored_recovery_refuses_missing_anchor(self):
        source = make_srt("One", "Two")
        target = make_srt("Üks")

        recovery = recover_srt_structure(source, target)

        self.assertFalse(recovery.safe)
        self.assertIn("anchor count differs", recovery.reason)

    def test_repair_retries_without_context_and_replaces_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "episode.eng.srt"
            target = root / "episode.et.srt"
            source.write_text(
                make_srt("Before context.", "Oh, my God!", "After context."),
                encoding="utf-8",
            )
            target.write_text(
                make_srt("Eelnev kontekst.", "[TARGET]>>> Oh, mu jumal! <<<<[/TARGET]", "Järgnev kontekst."),
                encoding="utf-8",
            )
            calls = []

            def translator(line, before, after):
                calls.append((line, before, after))
                if len(calls) == 1:
                    return "[TARGET]>>> Oh, mu jumal! <<<<[/TARGET]"
                return "Oh, mu jumal!"

            result = repair_subtitle_file(
                source,
                target,
                self.detector,
                self.estonian,
                translator,
                target_lang="et",
                max_attempts=2,
                context_lines=1,
            )

            self.assertTrue(result.success, result.reason)
            self.assertEqual(result.repaired_cues, [2])
            self.assertEqual(calls[0][1:], (["Before context."], ["After context."]))
            self.assertEqual(calls[1][1:], ([], []))
            cues, errors = parse_srt_cues(target.read_text(encoding="utf-8"))
            self.assertEqual(errors, [])
            self.assertEqual(cues[1], SubtitleCue(2, "00:00:02,000 --> 00:00:02,900", ["Oh, mu jumal!"]))

    def test_failed_repair_leaves_original_file_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "episode.eng.srt"
            target = root / "episode.et.srt"
            source.write_text(make_srt("Oh, my God!"), encoding="utf-8")
            original = make_srt("[TARGET]>>> leaked context <<<<[/TARGET]")
            target.write_text(original, encoding="utf-8")

            result = repair_subtitle_file(
                source,
                target,
                self.detector,
                self.estonian,
                lambda line, before, after: "[TARGET]>>> still broken <<<<[/TARGET]",
                target_lang="et",
                max_attempts=2,
            )

            self.assertFalse(result.success)
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_five_line_context_leak_is_repairable_without_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "episode.eng.srt"
            target = root / "episode.et.srt"
            source.write_text(make_srt("Oh, my God!"), encoding="utf-8")
            target.write_text(
                make_srt("Esimene.\nTeine.\nKolmas.\nNeljas.\nViies."),
                encoding="utf-8",
            )

            report = self.validate_pair(source, target)

            self.assertIn("excessive_lines", {issue.rule for issue in report.issues})
            self.assertEqual(report.repairable_cue_indexes, [0])

    def test_four_line_source_allows_five_line_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "episode.eng.srt"
            target = root / "episode.et.srt"
            source.write_text(make_srt("One\nTwo\nThree\nFour"), encoding="utf-8")
            target.write_text(make_srt("Üks\nKaks\nKolm\nNeli\nViis"), encoding="utf-8")

            report = self.validate_pair(source, target)

            self.assertNotIn("excessive_lines", {issue.rule for issue in report.issues})

    def test_excessive_line_repair_retries_without_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "episode.eng.srt"
            target = root / "episode.et.srt"
            source.write_text(make_srt("Before", "Target line", "After"), encoding="utf-8")
            target.write_text(
                make_srt("Enne", "Üks\nKaks\nKolm\nNeli\nViis", "Pärast"),
                encoding="utf-8",
            )
            calls = []

            def translator(line, before, after):
                calls.append((before, after))
                if len(calls) == 1:
                    return "Üks\nKaks\nKolm\nNeli\nViis"
                return "Sihtlause"

            result = repair_subtitle_file(
                source,
                target,
                self.detector,
                self.estonian,
                translator,
                target_lang="et",
                context_lines=1,
                max_attempts=2,
            )

            self.assertTrue(result.success, result.reason)
            self.assertEqual(calls[0], (["Before"], ["After"]))
            self.assertEqual(calls[1], ([], []))
            self.assertEqual(result.attempts, 2)
            self.assertEqual([entry["outcome"] for entry in result.attempt_history], ["rejected", "accepted"])
            self.assertTrue(result.attempt_history[1]["withoutContext"])

    def test_attempt_logger_contains_metadata_not_subtitle_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "episode.eng.srt"
            target = root / "episode.et.srt"
            source.write_text(make_srt("Secret source dialogue"), encoding="utf-8")
            target.write_text(make_srt("[SOURCE] leaked [/SOURCE]"), encoding="utf-8")
            events = []

            result = repair_subtitle_file(
                source,
                target,
                self.detector,
                self.estonian,
                lambda line, before, after: "Parandatud rida",
                target_lang="et",
                attempt_logger=events.append,
            )

            self.assertTrue(result.success, result.reason)
            serialized = json.dumps(events)
            self.assertNotIn("Secret source dialogue", serialized)
            self.assertNotIn("Parandatud rida", serialized)
            self.assertEqual([event["event"] for event in events], ["sending", "accepted"])

    def test_target_only_validation_uses_hard_line_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "episode.et.srt"
            target.write_text(make_srt("Üks\nKaks\nKolm\nNeli\nViis"), encoding="utf-8")

            report = validate_subtitle_without_source(
                target,
                self.detector,
                self.estonian,
                target_lang="et",
                max_cue_lines=4,
            )

            self.assertIn("excessive_lines", {issue.rule for issue in report.issues})

    def test_discovers_target_variants_and_prefers_eng_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (
                "show.et.srt",
                "show.et.hi.srt",
                "show.et.sdh.srt",
                "show.et.2.srt",
                "show.eng.srt",
                "show.en.srt",
            ):
                (root / name).write_text(make_srt("Text"), encoding="utf-8")

            candidates = discover_target_subtitles([root], ["et"])

            self.assertEqual(len(candidates), 4)
            for candidate in candidates:
                source, source_lang = find_preferred_source(candidate)
                self.assertEqual(source, root / "show.eng.srt")
                self.assertEqual(source_lang, "en")

    def test_variant_target_prefers_matching_source_before_plain_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (
                "show.et.hi.srt",
                "show.et.sdh.srt",
                "show.et.12.srt",
                "show.eng.srt",
                "show.en.hi.srt",
                "show.eng.sdh.srt",
                "show.en.12.srt",
            ):
                (root / name).write_text(make_srt("Text"), encoding="utf-8")

            pairings = {}
            for candidate in discover_target_subtitles([root], ["et"]):
                source, source_lang = find_preferred_source(candidate)
                pairings[candidate.variant] = source.name
                self.assertEqual(source_lang, "en")

            self.assertEqual(
                pairings,
                {
                    ".hi": "show.en.hi.srt",
                    ".sdh": "show.eng.sdh.srt",
                    ".12": "show.en.12.srt",
                },
            )

    def test_discovers_three_letter_target_alias_as_configured_language(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "show.est.hi.srt").write_text(make_srt("Tere"), encoding="utf-8")
            (root / "show.eng.hi.srt").write_text(make_srt("Hello"), encoding="utf-8")

            candidates = discover_target_subtitles([root], ["et"])
            source, source_lang = find_preferred_source(candidates[0])

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].target_lang, "et")
            self.assertEqual(candidates[0].language_token, "est")
            self.assertEqual(source, root / "show.eng.hi.srt")
            self.assertEqual(source_lang, "en")

    def test_validation_state_skips_only_unchanged_valid_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "show.eng.srt"
            target = root / "show.et.srt"
            state = self.state
            source.write_text(make_srt("Hello"), encoding="utf-8")
            target.write_text(make_srt("Tere"), encoding="utf-8")
            source_hash = file_sha256(source)
            target_hash = file_sha256(target)
            state.record(
                target,
                source_hash=source_hash,
                target_hash=target_hash,
                result="valid",
            )

            self.assertTrue(state.is_unchanged_valid(target, source_hash, target_hash))
            target.write_text(make_srt("Tere jälle"), encoding="utf-8")
            self.assertFalse(state.is_unchanged_valid(target, source_hash, file_sha256(target)))

    def test_validation_state_returns_current_target_only_details(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "show.et.srt"
            target.write_text(make_srt("Tere"), encoding="utf-8")
            target_hash = file_sha256(target)
            state = self.state
            state.record(
                target,
                source_hash="source-hash-is-irrelevant-for-target-only-reuse",
                target_hash=target_hash,
                result="valid",
                details={"completeness": {"mediaDurationSeconds": 3600.0}},
            )

            details = state.current_valid_details(target, target_hash)
            self.assertEqual(details["completeness"]["mediaDurationSeconds"], 3600.0)
            target.write_text(make_srt("Muudetud"), encoding="utf-8")
            self.assertIsNone(state.current_valid_details(target, file_sha256(target)))

    def test_validation_state_reuses_warning_and_tracks_quarantine_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "show.et.hi.srt"
            target.write_text(make_srt("Tere"), encoding="utf-8")
            target_hash = file_sha256(target)
            state = self.state
            state.record(
                target,
                source_hash=None,
                target_hash=target_hash,
                result="valid_with_warnings",
                details={"warningRules": ["excessive_lines"]},
            )
            self.assertTrue(state.is_unchanged_valid(target, None, target_hash))

            now = datetime.now(timezone.utc)
            first, repeated = state.record_quarantine_tombstone(
                "show|et",
                target_path=target,
                target_hash=target_hash,
                target_language="et",
                rules=["excessive_lines"],
                origin="unknown",
                hold_days=30,
                now=now,
            )
            second, repeated_again = state.record_quarantine_tombstone(
                "show|et",
                target_path=target,
                target_hash=target_hash,
                target_language="et",
                rules=["excessive_lines"],
                origin="unknown",
                hold_days=30,
                now=now + timedelta(hours=1),
            )

            self.assertFalse(repeated)
            self.assertTrue(repeated_again)
            self.assertEqual(first["occurrences"], 1)
            self.assertEqual(second["occurrences"], 2)
            self.assertIsNotNone(
                state.active_quarantine_tombstone(
                    "show|et", now=now + timedelta(days=29)
                )
            )
            self.assertIsNone(
                state.active_quarantine_tombstone(
                    "show|et", now=now + timedelta(days=31)
                )
            )

            changed, changed_repeat = state.record_quarantine_tombstone(
                "show|et",
                target_path=target,
                target_hash="different-hash",
                target_language="et",
                rules=["prompt_marker"],
                origin="unknown",
                hold_days=30,
                now=now + timedelta(hours=2),
            )
            third, original_repeats_again = state.record_quarantine_tombstone(
                "show|et",
                target_path=target,
                target_hash=target_hash,
                target_language="et",
                rules=["excessive_lines"],
                origin="unknown",
                hold_days=30,
                now=now + timedelta(hours=3),
            )
            self.assertFalse(changed_repeat)
            self.assertEqual(changed["occurrences"], 1)
            self.assertTrue(original_repeats_again)
            self.assertEqual(third["occurrences"], 3)

    def test_quarantine_preserves_relative_path_and_writes_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "media"
            target = root / "shows" / "show.et.srt"
            quarantine = Path(directory) / "quarantine"
            target.parent.mkdir(parents=True)
            target.write_text(make_srt("Katki"), encoding="utf-8")

            destination = quarantine_subtitle(target, [root], quarantine)
            report_path = write_validation_report(
                destination,
                {"validation": {"issues": [{"rule": "excessive_lines", "cueNumber": 1}]}},
            )

            self.assertFalse(target.exists())
            self.assertEqual(destination, quarantine / "shows" / "show.et.srt")
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["validation"]["issues"][0]["rule"], "excessive_lines")

    def test_retention_removes_only_files_older_than_thirty_days(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_file = root / "old" / "show.et.srt"
            old_report = root / "old" / "show.et.srt.validation.json"
            recent_file = root / "recent.log"
            excluded_file = root / "current.log"
            old_file.parent.mkdir()
            for path in (old_file, old_report, recent_file, excluded_file):
                path.write_text("data", encoding="utf-8")
            now = time.time()
            old_time = now - 31 * 86400
            recent_time = now - 29 * 86400
            os.utime(old_file, (old_time, old_time))
            os.utime(old_report, (old_time, old_time))
            os.utime(recent_file, (recent_time, recent_time))
            os.utime(excluded_file, (old_time, old_time))

            removed = purge_old_files(
                root,
                30,
                now_timestamp=now,
                exclude=[excluded_file],
            )

            self.assertEqual(set(removed), {old_file, old_report})
            self.assertTrue(recent_file.exists())
            self.assertTrue(excluded_file.exists())
            self.assertFalse((root / "old").exists())

    def test_validation_state_history_uses_same_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "show.et.srt"
            state = self.state
            target.write_text(make_srt("Tere"), encoding="utf-8")
            state.record(
                target,
                source_hash=None,
                target_hash=file_sha256(target),
                result="valid",
            )

            removed = state.prune_older_than(
                30,
                now=datetime.now(timezone.utc) + timedelta(days=31),
            )

            self.assertEqual(removed, 1)


if __name__ == "__main__":
    unittest.main()
