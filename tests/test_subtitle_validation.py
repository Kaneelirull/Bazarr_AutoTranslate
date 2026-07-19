import sys
import tempfile
import unittest
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "docker"))

from clean_et_subs import (  # noqa: E402
    SubtitleCue,
    ValidationStateStore,
    build_detector,
    discover_target_subtitles,
    file_sha256,
    find_preferred_source,
    parse_srt_cues,
    purge_old_files,
    quarantine_subtitle,
    repair_subtitle_file,
    target_language_for_code,
    validate_subtitle_pair,
    validate_subtitle_without_source,
    write_validation_report,
)


def make_srt(*texts: str) -> str:
    blocks = []
    for index, text in enumerate(texts, start=1):
        blocks.append(
            f"{index}\n00:00:{index:02d},000 --> 00:00:{index:02d},900\n{text}"
        )
    return "\n\n".join(blocks) + "\n"


class SubtitleValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.detector = build_detector()
        cls.estonian = target_language_for_code("et")

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

    def test_validation_state_skips_only_unchanged_valid_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "show.eng.srt"
            target = root / "show.et.srt"
            state = ValidationStateStore(root / "validation_state.json")
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
            state = ValidationStateStore(root / "validation_state.json")
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
