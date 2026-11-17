"""Tests for Library.catalog_rows — the one-shot catalog payload that powers
client-side (in-memory) browsing."""
from __future__ import annotations

import tempfile
import unittest

from iamped.library import Library
from tests.test_library_facets import track


class CatalogRows(unittest.TestCase):
    def test_returns_all_tracks_with_browse_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lib = Library(f"{td}/library.db")
            lib.upsert_tracks([
                track("1", "The Trooper", "Iron Maiden", "Piece of Mind", "Metal"),
                track("2", "Aces High", "Iron Maiden", "Powerslave", "Metal"),
            ])
            rows = lib.catalog_rows()
            self.assertEqual(len(rows), 2)
            got = {r["rating_key"]: r for r in rows}
            self.assertEqual(got["1"]["title"], "The Trooper")
            self.assertEqual(got["1"]["artist"], "Iron Maiden")
            # Sort-relevant fields the paged /api/tracks view never exposed
            # must be present so the client can replicate BROWSE_ORDER exactly.
            for field in ("track_number", "disc_number", "last_viewed_at",
                          "album_artist", "container", "view_count",
                          "user_rating", "duration_ms", "file_size"):
                self.assertIn(field, got["1"], f"missing {field}")

    def test_source_scoping(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lib = Library(f"{td}/library.db")
            lib.upsert_tracks([track("1", "A", "B", "C", "G")])
            self.assertEqual(len(lib.catalog_rows("plex")), 1)
            self.assertEqual(lib.catalog_rows("jellyfin-xyz"), [])

    def test_empty_library(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lib = Library(f"{td}/library.db")
            self.assertEqual(lib.catalog_rows(), [])


if __name__ == "__main__":
    unittest.main()
