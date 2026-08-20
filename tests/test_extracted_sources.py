import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
DOCKER_ROOT = ROOT / "docker"
if str(DOCKER_ROOT) not in sys.path:
    sys.path.insert(0, str(DOCKER_ROOT))

from autotranslate.scheduling.locks import ArtifactAccessCoordinator  # noqa: E402
from autotranslate.subtitles.library import discover_target_subtitles  # noqa: E402
from autotranslate.subtitles.sources import (  # noqa: E402
    canonical_target_path,
    deduplicate_rolling_cues,
    discover_extracted_sources,
    extracted_receipt_owned_paths,
    lingarr_output_candidates,
    prepare_extracted_source,
)


ALIASES = {"en": {"en", "eng"}, "et": {"et", "est"}, "sv": {"sv", "swe"}}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cue(number: int, start: str, end: str, text: str) -> str:
    return f"{number}\n{start} --> {end}\n{text}\n"


class ExtractedSourceTests(unittest.TestCase):
    def _write_receipt(self, video: Path, tracks: list[dict]) -> Path:
        stat = video.stat()
        receipt = video.with_name(f"{video.stem}.extracted.json")
        receipt.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "generator": "SubExtractorr",
                    "video": {
                        "path": str(video),
                        "name": video.name,
                        "size": stat.st_size,
                        "mtimeNs": stat.st_mtime_ns,
                    },
                    "tracks": tracks,
                }
            ),
            encoding="utf-8",
        )
        return receipt

    def test_receipt_tracks_are_ranked_and_forced_or_traversal_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "Movie.mkv"
            video.write_bytes(b"video")
            plain = root / "Movie.extracted.eng.srt"
            hi = root / "Movie.extracted.eng.hi.srt"
            swedish = root / "Movie.extracted.swe.srt"
            for path, text in ((plain, "Plain"), (hi, "HI"), (swedish, "Swedish")):
                path.write_text(cue(1, "00:00:00,000", "00:00:01,000", text), encoding="utf-8")
            self._write_receipt(
                video,
                [
                    {"path": hi.name, "sha256": sha256(hi), "language": "eng", "variant": "hi", "trackId": 2, "default": True, "hearingImpaired": True, "forced": False},
                    {"path": plain.name, "sha256": sha256(plain), "language": "eng", "trackId": 3, "default": False, "hearingImpaired": False, "forced": False},
                    {"path": swedish.name, "sha256": sha256(swedish), "language": "swe", "trackId": 4, "default": True, "hearingImpaired": False, "forced": False},
                    {"path": "../escape.srt", "sha256": "bad", "language": "eng", "forced": False},
                    {"path": "Movie.extracted.eng.2.srt", "sha256": "bad", "language": "eng", "forced": True},
                    {"path": plain.name, "sha256": sha256(plain), "language": "swe", "variant": "", "forced": False},
                    {"path": hi.name, "sha256": sha256(hi), "language": "eng", "variant": "2", "forced": False},
                ],
            )

            candidates, error = discover_extracted_sources(video, ALIASES, ("en", "et", "sv"))

            self.assertIsNone(error)
            self.assertEqual([candidate.path for candidate in candidates], [plain, hi, swedish])

    def test_stale_receipt_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "Movie.mkv"
            video.write_bytes(b"video")
            source = root / "Movie.extracted.eng.srt"
            source.write_text(cue(1, "00:00:00,000", "00:00:01,000", "Text"), encoding="utf-8")
            receipt = self._write_receipt(video, [{"path": source.name, "sha256": sha256(source), "language": "eng"}])
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            payload["video"]["size"] += 1
            receipt.write_text(json.dumps(payload), encoding="utf-8")

            candidates, error = discover_extracted_sources(video, ALIASES, ("en", "et"))

            self.assertEqual(candidates, [])
            self.assertIn("size", error)

    def test_deduplication_merges_full_span_and_is_idempotent(self):
        raw = "\n\n".join(
            (
                cue(4, "00:00:12,969", "00:00:14,129", "Same").strip(),
                cue(8, "00:00:14,129", "00:00:15,249", "Same").strip(),
                cue(9, "00:00:15,330", "00:00:16,000", "Different").strip(),
            )
        ) + "\n"

        result = deduplicate_rolling_cues(raw)
        repeated = deduplicate_rolling_cues(result.raw)

        self.assertTrue(result.changed)
        self.assertEqual(result.duplicate_groups, 1)
        self.assertEqual(result.removed_cues, 1)
        self.assertIn("1\n00:00:12,969 --> 00:00:15,249\nSame", result.raw)
        self.assertIn("2\n00:00:15,330 --> 00:00:16,000\nDifferent", result.raw)
        self.assertFalse(repeated.changed)

    def test_deduplication_respects_100_ms_boundary_and_payload(self):
        mergeable = "\n\n".join(
            (
                cue(1, "00:00:00,000", "00:00:01,000", "Same").strip(),
                cue(2, "00:00:01,100", "00:00:02,000", "Same").strip(),
                cue(3, "00:00:02,201", "00:00:03,000", "Same").strip(),
                cue(4, "00:00:03,000", "00:00:04,000", "Changed").strip(),
            )
        ) + "\n"
        result = deduplicate_rolling_cues(mergeable)
        self.assertEqual(result.output_cues, 3)
        self.assertEqual(result.removed_cues, 1)

    def test_preparation_atomically_updates_source_and_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "Movie.mkv"
            video.write_bytes(b"video")
            source = root / "Movie.extracted.eng.srt"
            source.write_text(
                cue(1, "00:00:00,000", "00:00:01,000", "Same")
                + "\n"
                + cue(2, "00:00:01,000", "00:00:02,000", "Same"),
                encoding="utf-8",
            )
            receipt = self._write_receipt(video, [{"path": source.name, "sha256": sha256(source), "language": "eng", "forced": False}])
            candidates, _ = discover_extracted_sources(video, ALIASES, ("en", "et"))

            prepared = prepare_extracted_source(candidates[0], artifact_access=ArtifactAccessCoordinator(), normalize=lambda _path: None)
            payload = json.loads(receipt.read_text(encoding="utf-8"))

            self.assertIsNone(prepared.error)
            self.assertTrue(prepared.changed)
            self.assertEqual(prepared.removed_cues, 1)
            self.assertEqual(payload["tracks"][0]["sha256"], sha256(source))
            self.assertEqual(payload["tracks"][0]["preparation"]["state"], "complete")
            self.assertEqual(payload["tracks"][0]["preparation"]["removedCues"], 1)

    def test_pending_receipt_is_finalized_after_source_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "Movie.mkv"
            video.write_bytes(b"video")
            source = root / "Movie.extracted.eng.srt"
            original = cue(1, "00:00:00,000", "00:00:01,000", "Same") + "\n" + cue(2, "00:00:01,000", "00:00:02,000", "Same")
            result = deduplicate_rolling_cues(original)
            source.write_text(result.raw, encoding="utf-8")
            input_hash = hashlib.sha256(original.encode("utf-8")).hexdigest()
            output_hash = sha256(source)
            receipt = self._write_receipt(
                video,
                [{
                    "path": source.name,
                    "sha256": input_hash,
                    "language": "eng",
                    "forced": False,
                    "preparation": {
                        "algorithm": "adjacent-exact-v1",
                        "state": "pending",
                        "inputSha256": input_hash,
                        "outputSha256": output_hash,
                        "inputCueCount": 2,
                        "outputCueCount": 1,
                        "duplicateGroups": 1,
                        "removedCues": 1,
                    },
                }],
            )
            candidates, _ = discover_extracted_sources(video, ALIASES, ("en", "et"))

            prepared = prepare_extracted_source(candidates[0], artifact_access=ArtifactAccessCoordinator(), normalize=lambda _path: None)
            payload = json.loads(receipt.read_text(encoding="utf-8"))

            self.assertIsNone(prepared.error)
            self.assertEqual(payload["tracks"][0]["sha256"], output_hash)
            self.assertEqual(payload["tracks"][0]["preparation"]["state"], "complete")

    def test_atomic_failure_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "Movie.mkv"
            video.write_bytes(b"video")
            source = root / "Movie.extracted.eng.srt"
            original = cue(1, "00:00:00,000", "00:00:01,000", "Same") + "\n" + cue(2, "00:00:01,000", "00:00:02,000", "Same")
            source.write_text(original, encoding="utf-8")
            self._write_receipt(video, [{"path": source.name, "sha256": sha256(source), "language": "eng", "forced": False}])
            candidates, _ = discover_extracted_sources(video, ALIASES, ("en", "et"))

            real_replace = os.replace

            def fail_source_publish(source_path, destination_path):
                if Path(destination_path) == source:
                    raise OSError("blocked")
                return real_replace(source_path, destination_path)

            with patch("autotranslate.subtitles.sources.os.replace", side_effect=fail_source_publish):
                prepared = prepare_extracted_source(candidates[0], artifact_access=ArtifactAccessCoordinator(), normalize=lambda _path: None)

            self.assertIsNotNone(prepared.error)
            self.assertEqual(source.read_text(encoding="utf-8"), original)

    def test_extracted_files_are_not_discovered_as_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Movie.extracted.et.srt").write_text("source", encoding="utf-8")
            canonical = root / "Movie.et.srt"
            canonical.write_text("target", encoding="utf-8")
            discovered = discover_target_subtitles((root,), ("et",))
            self.assertEqual([item.path for item in discovered], [canonical])

    def test_receipt_ownership_includes_tracks_rejected_for_translation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "Movie.mkv"
            video.write_bytes(b"video")
            receipt = root / "Movie.extracted.json"
            receipt.write_text(json.dumps({
                "tracks": [
                    {"path": "Movie.extracted.eng.2.srt"},
                    {"path": "Movie.extracted.2.et.srt", "sha256": "stale"},
                    {"path": "../outside.srt"},
                ],
            }), encoding="utf-8")
            self.assertEqual(
                {path.name for path in extracted_receipt_owned_paths(video)},
                {"Movie.extracted.eng.2.srt", "Movie.extracted.2.et.srt"},
            )

    def test_canonical_target_path_preserves_only_semantic_variants(self):
        expected = {
            "": Path("/media/Movie.et.srt"),
            ".2": Path("/media/Movie.et.srt"),
            "2": Path("/media/Movie.et.srt"),
            ".hi": Path("/media/Movie.et.hi.srt"),
            "sdh": Path("/media/Movie.et.sdh.srt"),
        }
        for variant, target in expected.items():
            with self.subTest(variant=variant):
                self.assertEqual(
                    canonical_target_path(Path("/media/Movie.mkv"), "et", variant),
                    target,
                )

    def test_lingarr_output_candidates_cover_replacement_and_append_layouts(self):
        cases = {
            "": ("Movie.extracted.et.srt",),
            ".2": ("Movie.extracted.et.2.srt", "Movie.extracted.2.et.srt"),
            ".hi": ("Movie.extracted.et.hi.srt", "Movie.extracted.hi.et.srt"),
            ".sdh": ("Movie.extracted.et.sdh.srt", "Movie.extracted.sdh.et.srt"),
        }
        for variant, names in cases.items():
            with self.subTest(variant=variant):
                replacement = Path("/media") / names[0]
                self.assertEqual(
                    tuple(path.name for path in lingarr_output_candidates(replacement, "et", variant)),
                    names,
                )

    def test_large_duplicate_examples_have_expected_reductions(self):
        expected = {
            "Top Gear (2002) - S27E03 - Episode 3 [WEBDL-1080p][AAC 2.0][h264]-BLOOM.eng.srt": (2491, 1167),
            "Top Gear (2002) - S27E04 - Episode 4 [WEBDL-1080p][AAC 2.0][h264]-BLOOM.en.hi.srt": (2626, 1242),
        }
        for name, (input_cues, output_cues) in expected.items():
            with self.subTest(name=name):
                raw = (ROOT / "examples" / "DeDuplicate" / name).read_text(encoding="utf-8-sig")
                result = deduplicate_rolling_cues(raw)
                self.assertEqual(result.input_cues, input_cues)
                self.assertEqual(result.output_cues, output_cues)


if __name__ == "__main__":
    unittest.main()
