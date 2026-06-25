from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from mutagen.id3 import ID3

from iamped import device_management, server
from iamped.devices import usbdetect
from iamped.sync import device_state, ipod_artwork
from iamped.sync.itunesdb import ITunesDBBackend, read_tracks_full
from iamped.sync.massstorage import MassStorageBackend


def audio_track(key="1"):
    return {
        "rating_key": key, "title": f"Track {key}", "artist": "Artist",
        "album_artist": "Artist", "album": "Album", "album_key": "album-1",
        "duration_ms": 1000, "track_number": 1, "container": "mp3",
        "file_size": 100,
    }


class DeviceFeatureTests(unittest.TestCase):
    def test_multi_ipod_usb_match_uses_database_scheme(self):
        connected = [
            {"pid": 0x120A, "serial": "nano1", "bsd": set(),
             "model": "iPod nano (1st generation)",
             "generation": "iPod nano (1st generation)"},
            {"pid": 0x1262, "serial": "nano3", "bsd": set(),
             "model": "iPod nano (3rd generation)",
             "generation": "iPod nano (3rd generation)"},
        ]
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "iPod_Control/iTunes/iTunesDB"
            db.parent.mkdir(parents=True)
            header = bytearray(0xF4)
            header[:4] = b"mhbd"
            header[4:8] = (0xF4).to_bytes(4, "little")
            header[0x30:0x32] = (1).to_bytes(2, "little")
            db.write_bytes(header)
            with patch.object(usbdetect, "ipod_models", return_value=connected):
                match = usbdetect.match(mountpoint=td)
            self.assertEqual(match["serial"], "nano3")

    def test_massstorage_artwork_cover_and_embedding(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source.mp3"
            ID3().save(source)
            cover = root / "cover.jpg"
            Image.new("RGB", (320, 240), (30, 100, 220)).save(cover, "JPEG")
            backend = MassStorageBackend(str(root / "device"))
            backend.prepare()
            record = backend.add_track(
                audio_track(), str(source), ".mp3", str(cover))
            copied = root / "device" / record["path"]
            self.assertTrue((copied.parent / "cover.jpg").is_file())
            self.assertEqual(len(ID3(copied).getall("APIC")), 1)

    def test_native_artworkdb_is_written_for_nano3(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source.m4a"
            source.write_bytes(b"audio")
            cover = root / "cover.jpg"
            Image.new("RGB", (320, 320), (220, 40, 70)).save(cover, "JPEG")
            backend = ITunesDBBackend(td, "Nano")
            backend._generation = "iPod nano (3rd generation)"
            backend._hash58 = True
            backend._firewire_guid = "000A27001A6E36F0"
            backend.prepare()
            backend.add_track(
                audio_track("nano"), str(source), ".m4a", str(cover))
            backend.finalize()
            rows = read_tracks_full(
                (root / "iPod_Control/iTunes/iTunesDB").read_bytes())
            self.assertGreater(rows[0]["mhii_link"], 0)
            self.assertEqual(
                ipod_artwork.validate(td),
                {"datasets": 3, "images": 1, "thumbnails": 4})

    def test_backup_restore_and_device_profile(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "Music/old.mp3"
            media.parent.mkdir()
            media.write_bytes(b"old")
            device_state.write_manifest(td, "massstorage", {
                "tracks": [{"rating_key": "old", "path": "Music/old.mp3",
                            "size": 3}]})
            backup = device_management.create_backup(
                td, "massstorage",
                [{"rating_key": "old", "path": "Music/old.mp3", "size": 3}])
            media.unlink()
            new_media = root / "Music/new.mp3"
            new_media.write_bytes(b"new")
            device_state.write_manifest(td, "massstorage", {
                "tracks": [{"rating_key": "new", "path": "Music/new.mp3",
                            "size": 3}]})
            restored = device_management.restore_backup(
                td, "massstorage", backup["id"])
            self.assertEqual(restored["managed_tracks"], 1)
            self.assertTrue(media.exists())
            self.assertFalse(new_media.exists())

            profiles = root / "profiles.json"
            with patch.object(device_management, "PROFILES_PATH", profiles):
                saved = device_management.save_profile(
                    td, "massstorage",
                    {"name": "MuVo", "reserve_mb": 64, "sync_artwork": True})
                loaded = device_management.get_profile(td, "massstorage")
            self.assertEqual(saved["device_id"], loaded["device_id"])
            self.assertEqual(loaded["reserve_mb"], 64)

    def test_review_can_preserve_a_planned_removal(self):
        class FakeLibrary:
            def all_playlists(self):
                return []

            def ordered_tracks(self, _strategy):
                return [audio_track("keep")]

            def get_tracks(self, keys):
                return {key: audio_track(key) for key in keys}

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for key in ("keep", "stale"):
                media = root / f"Music/{key}.mp3"
                media.parent.mkdir(exist_ok=True)
                media.write_bytes(key.encode())
            device_state.write_manifest(td, "massstorage", {
                "tracks": [
                    {"rating_key": "keep", "path": "Music/keep.mp3", "size": 4},
                    {"rating_key": "stale", "path": "Music/stale.mp3", "size": 5},
                ]})
            params = {
                "device_path": td, "device_type": "massstorage",
                "mirror": True, "reserve_mb": 0,
                "fill_strategy": "most_played", "playlist_ids": [],
                "transcode_lossless": False, "review_actions": [],
            }
            with patch.object(server, "_lib", return_value=FakeLibrary()), \
                    patch.object(server, "free_bytes", return_value=1000):
                diff = server._device_plan(params)
            self.assertEqual(diff["removals"], [])
            self.assertIn("stale", diff["keep_tracks"])


if __name__ == "__main__":
    unittest.main()
