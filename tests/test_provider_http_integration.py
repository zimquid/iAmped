"""Wire-level provider tests against local HTTP fixtures.

These deliberately do not mock ``requests`` or provider methods: each adapter
opens real loopback HTTP connections, authenticates, enumerates a music
library, follows a playlist, fetches artwork, and downloads original audio.
They are small protocol fixtures, not a claim of validation against a full
Jellyfin/Kodi deployment.
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from iamped import providers


AUDIO = b"iAmped integration audio fixture\n"
ART = b"\xff\xd8\xff\xd9"  # minimal JPEG marker sequence; bytes only matter here


class _FixtureServer:
    def __init__(self, mode: str):
        self.mode = mode
        self.calls: list[tuple[str, str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _json(self, status: int, payload: dict):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _bytes(self, status: int, body: bytes, content_type: str):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
                parsed = urlparse(self.path)
                outer.calls.append(("GET", parsed.path))
                if outer.mode == "jellyfin":
                    self._jellyfin_get(parsed)
                else:
                    self._kodi_get(parsed)

            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
                parsed = urlparse(self.path)
                outer.calls.append(("POST", parsed.path))
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b""
                if outer.mode == "jellyfin":
                    self._jellyfin_post(parsed, body)
                else:
                    self._kodi_post(parsed, body)

            def _jellyfin_get(self, parsed):
                if parsed.path == "/Users/Me":
                    return self._json(200, {"Id": "user-1", "ServerId": "server-1"})
                if parsed.path == "/System/Info":
                    return self._json(200, {"ServerName": "Fixturefin", "Version": "10.10"})
                if parsed.path == "/Users/user-1/Views":
                    return self._json(200, {"Items": [{"Id": "music-1", "Name": "Music", "CollectionType": "music"}]})
                if parsed.path == "/Users/user-1/Items":
                    query = parse_qs(parsed.query)
                    if query.get("IncludeItemTypes") == ["Playlist"]:
                        return self._json(200, {"Items": [{"Id": "playlist-1", "Name": "Road trip"}]})
                    return self._json(200, {"TotalRecordCount": 1, "Items": [_jellyfin_track()]})
                if parsed.path == "/Playlists/playlist-1/Items":
                    return self._json(200, {"Items": [_jellyfin_track()]})
                if parsed.path == "/Items/track-1/Download":
                    return self._bytes(200, AUDIO, "audio/mpeg")
                if parsed.path == "/Items/album-1/Images/Primary":
                    return self._bytes(200, ART, "image/jpeg")
                self._json(404, {"error": "not found"})

            def _jellyfin_post(self, parsed, _body: bytes):
                if parsed.path == "/Users/AuthenticateByName":
                    return self._json(200, {"AccessToken": "fixture-token", "User": {"Id": "user-1"}})
                if parsed.path == "/Users/user-1/PlayedItems/track-1":
                    return self._json(200, {})
                self._json(404, {"error": "not found"})

            def _kodi_get(self, parsed):
                if parsed.path.startswith("/vfs/"):
                    return self._bytes(200, AUDIO, "audio/mpeg")
                if parsed.path.startswith("/image/"):
                    return self._bytes(200, ART, "image/jpeg")
                self._json(404, {"error": "not found"})

            def _kodi_post(self, parsed, body: bytes):
                if parsed.path != "/jsonrpc":
                    return self._json(404, {"error": "not found"})
                request = json.loads(body or b"{}")
                method = request.get("method")
                params = request.get("params") or {}
                result = _kodi_result(method, params, outer.base_url)
                self._json(200, {"jsonrpc": "2.0", "id": request.get("id"), "result": result})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.httpd.shutdown()
        self.thread.join(timeout=3)
        self.httpd.server_close()


def _jellyfin_track() -> dict:
    return {
        "Id": "track-1", "Name": "Loopback Song", "Artists": ["Fixture Artist"],
        "Album": "Fixture Album", "AlbumArtist": "Fixture Artist", "AlbumId": "album-1",
        "IndexNumber": 1, "ParentIndexNumber": 1, "RunTimeTicks": 2_000_000_000,
        "Genres": ["Test"], "ProductionYear": 2026,
        "UserData": {"PlayCount": 3},
        "MediaSources": [{"Container": "mp3", "Size": len(AUDIO), "Bitrate": 192_000,
                          "MediaStreams": [{"Type": "Audio", "Codec": "mp3"}]}],
    }


def _kodi_result(method: str, _params: dict, base_url: str) -> dict:
    if method == "JSONRPC.Version":
        return {"version": {"major": 13, "minor": 11}}
    if method == "Application.GetProperties":
        return {"name": "Fixture Kodi", "version": {"major": 21, "minor": 0}}
    if method == "AudioLibrary.GetSongs":
        return {"limits": {"start": 0, "end": 1, "total": 1}, "songs": [{
            "songid": 7, "label": "Kodi Song", "artist": ["Fixture Artist"],
            "album": "Fixture Album", "albumartist": ["Fixture Artist"],
            "duration": 180, "playcount": 2, "rating": 8,
            "file": "smb://fixture/Music/Kodi Song.mp3", "thumbnail": "image://fixture-art",
        }]}
    raise AssertionError(f"unexpected Kodi RPC method: {method}")


class ProviderHttpIntegrationTests(unittest.TestCase):
    def test_jellyfin_connect_import_playlist_artwork_download_and_play_state(self):
        with _FixtureServer("jellyfin") as fixture, tempfile.TemporaryDirectory() as td:
            connection = providers.connect({
                "id": "jellyfin-fixture", "kind": "jellyfin", "baseurl": fixture.base_url,
                "username": "fixture", "password": "fixture-password",
            })
            self.assertEqual(connection.provider.server_info(connection)["name"], "Fixturefin")
            self.assertEqual(connection.provider.music_sections(connection), [{"id": "music-1", "name": "Music"}])
            track = next(connection.provider.iter_tracks(connection, "music-1"))
            self.assertEqual(track.rating_key, "jellyfin-fixture:track-1")
            self.assertEqual(connection.provider.list_playlists(connection)[0].item_keys,
                             ["jellyfin-fixture:track-1"])

            row = {"rating_key": track.rating_key, "remote_id": track.remote_id,
                   "part_key": track.part_key}
            audio_path = str(Path(td) / "track.mp3")
            art_path = str(Path(td) / "art.jpg")
            connection.provider.download_track(connection, row, audio_path)
            connection.provider.download_image(connection, track.album_thumb, art_path)
            connection.provider.scrobble(connection, row)
            self.assertEqual(Path(audio_path).read_bytes(), AUDIO)
            self.assertEqual(Path(art_path).read_bytes(), ART)
            self.assertIn(("POST", "/Users/user-1/PlayedItems/track-1"), fixture.calls)

    def test_kodi_connect_catalog_probe_artwork_and_download(self):
        with _FixtureServer("kodi") as fixture, tempfile.TemporaryDirectory() as td:
            connection = providers.connect({
                "id": "kodi-fixture", "kind": "kodi", "baseurl": fixture.base_url,
            })
            self.assertEqual(connection.provider.server_info(connection)["name"], "Fixture Kodi")
            self.assertEqual(connection.provider.music_sections(connection), [{"id": "music", "name": "Music"}])
            track = next(connection.provider.iter_tracks(connection, "music"))
            self.assertEqual(track.rating_key, "kodi-fixture:7")
            self.assertTrue(connection.capabilities.original_download)

            row = {"rating_key": track.rating_key, "remote_id": track.remote_id,
                   "part_key": track.part_key}
            audio_path = str(Path(td) / "track.mp3")
            art_path = str(Path(td) / "art.jpg")
            connection.provider.download_track(connection, row, audio_path)
            connection.provider.download_image(connection, track.album_thumb, art_path)
            self.assertEqual(Path(audio_path).read_bytes(), AUDIO)
            self.assertEqual(Path(art_path).read_bytes(), ART)
            self.assertIn(("POST", "/jsonrpc"), fixture.calls)


if __name__ == "__main__":
    unittest.main()
