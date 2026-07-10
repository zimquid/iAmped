"""Provider contracts and source-aware catalog regression coverage."""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from iamped import server as server_module
from iamped.library import Library
from iamped.plex_client import PlaylistMeta, TrackMeta
from iamped import providers


def track(key: str, source: str = "plex") -> TrackMeta:
    return TrackMeta(
        rating_key=key, title="Song", artist="Artist", album="Album",
        album_artist="Artist", genre="", year=2024, track_number=1,
        disc_number=1, duration_ms=180_000, view_count=0, user_rating=None,
        last_viewed_at=None, container="mp3", codec="mp3", bitrate=192,
        file_size=4_000_000, part_key=key, server_file="", source_id=source,
        remote_id=key.split(":")[-1])


class ProviderKeyTests(unittest.TestCase):
    def test_legacy_plex_keys_remain_stable_and_new_sources_are_scoped(self):
        self.assertEqual(providers.track_key("plex", "123"), "123")
        self.assertEqual(providers.track_key("jellyfin-home", "123"),
                         "jellyfin-home:123")
        self.assertEqual(providers.track_key("kodi-den", "123"), "kodi-den:123")

    def test_jellyfin_item_maps_to_portable_track_metadata(self):
        provider = providers.JellyfinProvider()
        connection = providers.ProviderConnection(
            provider, object(), {"id": "jellyfin-home", "kind": "jellyfin"})
        meta = provider._meta(connection, {
            "Id": "a1", "Name": "Ocean", "Artists": ["June"],
            "Album": "Blue", "AlbumArtist": "June", "AlbumId": "album1",
            "Genres": ["Ambient"], "ProductionYear": 2024, "IndexNumber": 2,
            "ParentIndexNumber": 1, "RunTimeTicks": 1_230_000_000,
            "UserData": {"PlayCount": 7},
            "MediaSources": [{"Container": "flac", "Size": 12_000_000,
                              "Bitrate": 900_000,
                              "MediaStreams": [{"Type": "Audio", "Codec": "flac"}]}],
        })
        self.assertIsNotNone(meta)
        assert meta is not None
        self.assertEqual(meta.rating_key, "jellyfin-home:a1")
        self.assertEqual(meta.remote_id, "a1")
        self.assertEqual(meta.duration_ms, 123_000)
        self.assertEqual(meta.album_thumb, "jellyfin:album1")

    def test_kodi_item_has_a_scoped_key_and_capacity_estimate(self):
        provider = providers.KodiProvider()
        connection = providers.ProviderConnection(
            provider, object(), {"id": "kodi-den", "kind": "kodi"})
        meta = provider._meta(connection, {
            "songid": 9, "label": "Night Drive", "artist": ["Mira"],
            "album": "Road", "duration": 200, "playcount": 2,
            "file": "smb://music/Mira/Night Drive.mp3", "thumbnail": "image://cover"})
        self.assertIsNotNone(meta)
        assert meta is not None
        self.assertEqual(meta.rating_key, "kodi-den:9")
        self.assertEqual(meta.remote_id, "9")
        self.assertGreater(meta.file_size, 0)


class SourceCatalogTests(unittest.TestCase):
    def test_tracks_and_playlists_are_scoped_to_the_active_source(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Library(f"{td}/library.db")
            lib.upsert_tracks([track("1", "plex"), track("jellyfin-home:1", "jellyfin-home")])
            self.assertEqual(lib.track_count("plex"), 1)
            self.assertEqual(lib.track_count("jellyfin-home"), 1)
            self.assertEqual(
                [row["rating_key"] for row in lib.browse_tracks(source_id="jellyfin-home")["tracks"]],
                ["jellyfin-home:1"])

            lib.replace_playlists([
                PlaylistMeta("1", "Plex list", False, 0, ["1"], source_id="plex")], "plex")
            lib.replace_playlists([
                PlaylistMeta("jellyfin-home:1", "Jellyfin list", False, 0,
                             ["jellyfin-home:1"], source_id="jellyfin-home")],
                "jellyfin-home")
            self.assertEqual([p["title"] for p in lib.all_playlists("plex")], ["Plex list"])
            self.assertEqual([p["title"] for p in lib.all_playlists("jellyfin-home")],
                             ["Jellyfin list"])


class SourceApiTests(unittest.TestCase):
    def test_config_endpoint_returns_source_state_without_credentials(self):
        server_module.app.config.update(TESTING=True)
        cfg = {
            "plex_token": "legacy-secret",
            "sources": [{
                "id": "jellyfin-home", "kind": "jellyfin", "name": "Home",
                "baseurl": "https://media.example", "token": "secret-token",
                "password": "secret-password",
            }],
            "active_source_id": "jellyfin-home",
        }
        with patch.object(server_module.config, "load", return_value=cfg):
            body = server_module.app.test_client().get("/api/config").get_json()
        self.assertNotIn("plex_token", body)
        self.assertEqual(body["sources"][0]["id"], "jellyfin-home")
        self.assertNotIn("token", body["sources"][0])
        self.assertNotIn("password", body["sources"][0])


if __name__ == "__main__":
    unittest.main()
