from __future__ import annotations

import tempfile
import unittest

from iamped.library import Library
from iamped.plex_client import TrackMeta


def track(key: str, title: str, artist: str, album: str, genre: str) -> TrackMeta:
    return TrackMeta(
        rating_key=key,
        title=title,
        artist=artist,
        album=album,
        album_artist=artist,
        genre=genre,
        year=2001,
        track_number=1,
        disc_number=1,
        duration_ms=180000,
        view_count=0,
        user_rating=None,
        last_viewed_at=None,
        container="mp3",
        codec="mp3",
        bitrate=192000,
        file_size=3000000,
        part_key=f"/library/parts/{key}.mp3",
        server_file=f"/music/{key}.mp3",
        album_key=f"album-{album}",
        album_thumb=f"/thumb/{album}.jpg",
    )


class LibraryFacetTests(unittest.TestCase):
    def test_facets_and_exact_filters(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Library(f"{td}/library.db")
            lib.upsert_tracks([
                track("1", "Alpha", "Artist A", "Album A", "Rock"),
                track("2", "Beta", "Artist A", "Album A", "Rock"),
                track("3", "Gamma", "Artist B", "Album B", "Jazz"),
                track("4", "Delta", "Artist B", "Album A", "Jazz"),
            ])

            facets = lib.facets()
            self.assertEqual(facets["artists"][0], {"name": "Artist A", "count": 2})
            self.assertEqual(facets["genres"], [
                {"name": "Jazz", "count": 2},
                {"name": "Rock", "count": 2},
            ])
            self.assertEqual(facets["albums"][0]["name"], "Album A")
            self.assertEqual(facets["albums"][0]["count"], 2)

            rock = lib.browse_tracks(genre="Rock")
            self.assertEqual(rock["total"], 2)
            artist_b = lib.browse_tracks(artist="Artist B")
            self.assertEqual([t["title"] for t in artist_b["tracks"]], ["Delta", "Gamma"])
            album_a_by_b = lib.browse_tracks(album="Album A", album_artist="Artist B")
            self.assertEqual([t["title"] for t in album_a_by_b["tracks"]], ["Delta"])


if __name__ == "__main__":
    unittest.main()
