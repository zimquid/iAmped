"""Tests for ingest.folder_status — the reverse-feed folder validator that
powers the Plex-library-folder picker in Settings."""
from __future__ import annotations

import os
import tempfile
import unittest

from iamped import ingest


class WithinOrEqual(unittest.TestCase):
    def test_equal_and_nested(self) -> None:
        self.assertTrue(ingest._within_or_equal("/media/Music", "/media/Music"))
        self.assertTrue(ingest._within_or_equal("/media/Music/incoming", "/media/Music"))

    def test_sibling_prefix_is_not_within(self) -> None:
        # /media/Music2 must NOT count as inside /media/Music
        self.assertFalse(ingest._within_or_equal("/media/Music2", "/media/Music"))

    def test_trailing_slash_and_dots_normalized(self) -> None:
        self.assertTrue(ingest._within_or_equal("/media/Music/", "/media/Music"))
        self.assertTrue(ingest._within_or_equal("/media/Music/./incoming", "/media/Music"))

    def test_empty_inputs(self) -> None:
        self.assertFalse(ingest._within_or_equal("", "/media/Music"))
        self.assertFalse(ingest._within_or_equal("/media/Music", ""))


class FolderStatus(unittest.TestCase):
    def test_empty_path(self) -> None:
        st = ingest.folder_status("", [{"title": "Music", "locations": ["/x"]}])
        self.assertFalse(st["ok"])
        self.assertFalse(st["writable"])
        self.assertIn("No ingest folder", st["message"])

    def test_nonexistent_path(self) -> None:
        st = ingest.folder_status("/definitely/not/here/xyz", [])
        self.assertFalse(st["exists"])
        self.assertFalse(st["writable"])
        self.assertFalse(st["plex_watched"])
        self.assertFalse(st["ok"])

    def test_writable_but_not_watched(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            st = ingest.folder_status(d, [{"title": "Music", "locations": ["/some/plex/root"]}])
            self.assertTrue(st["exists"])
            self.assertTrue(st["writable"])
            self.assertFalse(st["plex_watched"])
            self.assertFalse(st["ok"])
            self.assertIn("doesn't look like a folder Plex watches", st["message"])

    def test_path_is_a_watched_location(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            sections = [{"title": "My Music", "locations": [d]}]
            st = ingest.folder_status(d, sections)
            self.assertTrue(st["ok"])
            self.assertTrue(st["plex_watched"])
            self.assertEqual(st["watched_by"], "My Music")
            self.assertIn("Ready", st["message"])

    def test_subfolder_of_watched_location(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            sub = os.path.join(d, "incoming")
            os.makedirs(sub)
            sections = [{"title": "Tunes", "locations": [d]}]
            st = ingest.folder_status(sub, sections)
            self.assertTrue(st["ok"])
            self.assertTrue(st["plex_watched"])
            self.assertEqual(st["watched_by"], "Tunes")

    def test_matches_second_section(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            sections = [
                {"title": "Movies", "locations": ["/movies"]},
                {"title": "Music", "locations": ["/nope", d]},
            ]
            st = ingest.folder_status(d, sections)
            self.assertTrue(st["plex_watched"])
            self.assertEqual(st["watched_by"], "Music")


if __name__ == "__main__":
    unittest.main()
