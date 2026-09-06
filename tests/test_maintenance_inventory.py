import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "docker"))

from autotranslate.maintenance.inventory import (  # noqa: E402
    build_inventory,
    build_scoped_inventory,
)


class MaintenanceInventoryTests(unittest.TestCase):
    def test_overlapping_roots_inventory_each_directory_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "media"
            child = root / "season"
            child.mkdir(parents=True)
            (child / "show.mkv").write_bytes(b"video")
            (child / "show.et.srt").write_text("subtitle", encoding="utf-8")

            with patch(
                "autotranslate.maintenance.inventory.os.walk", wraps=os.walk,
            ) as walk:
                inventory = build_inventory(
                    (root, root, child), (".mkv",),
                )

            self.assertEqual(walk.call_count, 1)
            self.assertEqual(inventory.directories_scanned, 2)
            self.assertEqual(len(inventory.videos), 1)
            self.assertEqual(
                inventory.sidecars_for(child / "show.mkv"),
                (child / "show.et.srt",),
            )

    def test_longest_video_stem_owns_exact_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            movie = root / "movie.mkv"
            extended = root / "movie.extended.mkv"
            for path in (movie, extended):
                path.write_bytes(b"video")
            for name in (
                "movie.et.srt", "movie.extended.et.srt",
                "movie.extended.commentary.srt",
            ):
                (root / name).write_text("subtitle", encoding="utf-8")

            inventory = build_inventory((root,), (".mkv",))

            self.assertEqual(
                inventory.sidecars_for(movie), (root / "movie.et.srt",),
            )
            self.assertEqual(
                inventory.sidecars_for(extended),
                (
                    root / "movie.extended.commentary.srt",
                    root / "movie.extended.et.srt",
                ),
            )

    def test_scoped_inventory_excludes_unsupplied_videos_and_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requested = root / "requested.mkv"
            unrelated = root / "unrelated.mkv"
            for path in (requested, unrelated):
                path.write_bytes(b"video")
            for path in (root / "requested.et.srt", root / "unrelated.et.srt"):
                path.write_text("subtitle", encoding="utf-8")

            inventory = build_scoped_inventory((requested,), (".mkv",))

            self.assertEqual([entry.path for entry in inventory.videos], [requested])
            self.assertEqual(inventory.sidecars, (root / "requested.et.srt",))
            self.assertIsNone(inventory.video_for_sidecar(root / "unrelated.et.srt"))


if __name__ == "__main__":
    unittest.main()
