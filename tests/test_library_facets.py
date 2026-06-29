from __future__ import annotations

import tempfile
import unittest

from iamped.library import Library
from iamped.plex_client import PlaylistMeta, TrackMeta


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

    def test_playlist_items_can_be_loaded_lazily(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Library(f"{td}/library.db")
            lib.replace_playlists([
                PlaylistMeta(
                    plex_id="playlist-1", title="Road Trip", smart=False,
                    duration_ms=300000, item_count=2, item_keys=[]),
            ])

            self.assertFalse(lib.playlist_items_loaded("playlist-1"))
            self.assertEqual(lib.playlist_track_keys("playlist-1"), [])
            self.assertEqual(lib.playlists()[0]["item_count"], 2)

            lib.replace_playlist_items("playlist-1", ["7", "8"])

            self.assertTrue(lib.playlist_items_loaded("playlist-1"))
            self.assertEqual(lib.playlist_track_keys("playlist-1"), ["7", "8"])


if __name__ == "__main__":
    unittest.main()
