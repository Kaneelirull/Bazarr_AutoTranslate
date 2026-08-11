import sys
import unittest
from pathlib import Path


DOCKER_DIR = Path(__file__).resolve().parents[1] / "docker"
sys.path.insert(0, str(DOCKER_DIR))

from autotranslate.media_identity import resolve_media_identity  # noqa: E402


class MediaIdentityTests(unittest.TestCase):
    def test_episode_prefers_sonarr_id_and_valid_title(self):
        identity = resolve_media_identity(
            {"sonarrSeriesId": 123, "seriesTitle": "Top Gear"},
            "episodes",
            456,
        )
        self.assertEqual(identity, {"key": "sonarr:123", "title": "Top Gear"})

    def test_generic_season_recovers_show_from_parent(self):
        identity = resolve_media_identity(
            {"seriesTitle": "Season 05"},
            "episodes",
            456,
            "/media/The Big Bang Theory/Season 05/show.S05E24.mkv",
        )
        self.assertEqual(identity["title"], "The Big Bang Theory")
        self.assertNotIn("Season 05", identity["key"])

    def test_pathless_generic_episodes_do_not_share_a_key(self):
        first = resolve_media_identity(
            {"seriesTitle": "Season 05"}, "episodes", 1
        )
        second = resolve_media_identity(
            {"seriesTitle": "Season 05"}, "episodes", 2
        )
        self.assertEqual(first["key"], "episode:1")
        self.assertEqual(second["key"], "episode:2")
        self.assertNotEqual(first["key"], second["key"])

    def test_public_identity_never_contains_a_path(self):
        identity = resolve_media_identity(
            {"seriesTitle": r"C:\media\Show\Season 01"},
            "episodes",
            3,
            r"C:\media\Example Show\Season 01\Example.Show.S01E01.mkv",
        )
        self.assertEqual(identity["title"], "Example Show")
        self.assertNotIn("\\", identity["title"])
        self.assertNotIn("/", identity["title"])

    def test_movie_identity_is_unchanged(self):
        identity = resolve_media_identity(
            {"title": "Example Movie"}, "movies", 7
        )
        self.assertEqual(
            identity, {"key": "movies:example movie", "title": "Example Movie"}
        )


if __name__ == "__main__":
    unittest.main()
