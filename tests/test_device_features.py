from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from mutagen.id3 import ID3

from iamped import device_management
from iamped.api import syncing as server
from iamped.devices import usbdetect
from iamped.sync import device_state, ipod_artwork
from iamped.sync.itunesdb import ITunesDBBackend, read_tracks_full
from iamped.sync.massstorage import MassStorageBackend


class _ArtEntry:
    """Minimal stand-in for itunesdb._Entry, enough to drive ipod_artwork."""
    def __init__(self, track_id, art_path):
        self.track_id = track_id
        self.track = {"artist": "Artist", "album": f"Album{track_id}",
                      "dbid": track_id}
        self.art_path = art_path


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

    def test_corrupt_artworkdb_never_blocks_music(self):
        # A device carrying an ArtworkDB our validator rejects must still get a
        # complete iTunesDB: artwork is cosmetic, and if a failed art write
        # aborts finalize the copied audio ends up on disk with no track record
        # (the "Other" bucket) and never appears in the iPod's Music menu.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source.m4a"
            source.write_bytes(b"audio")
            cover = root / "cover.jpg"
            Image.new("RGB", (320, 320), (10, 200, 90)).save(cover, "JPEG")
            artdir = root / "iPod_Control/Artwork"
            artdir.mkdir(parents=True)
            # Seed a *structurally parseable but invalid* ArtworkDB, matching the
            # corruption seen on hardware: records that _parse_existing happily
            # returns but whose thumbnail pointer fails validate(). Build a real
            # one, then break the inner 'mhni' magic so validation rejects it.
            seed = ipod_artwork.write(
                td, "iPod nano (1st generation)",
                [_ArtEntry(1, str(cover))])
            self.assertEqual(seed["artwork"], 1)
            db_bytes = (artdir / "ArtworkDB").read_bytes()
            self.assertIn(b"mhni", db_bytes)
            (artdir / "ArtworkDB").write_bytes(db_bytes.replace(b"mhni", b"XXXX"))
            with self.assertRaises(Exception):
                ipod_artwork.validate(td)

            backend = ITunesDBBackend(td, "Nano")
            backend._generation = "iPod nano (1st generation)"
            backend.prepare()
            backend.add_track(audio_track("a"), str(source), ".m4a", str(cover))
            backend.finalize()

            rows = read_tracks_full(
                (root / "iPod_Control/iTunes/iTunesDB").read_bytes())
            self.assertEqual(len(rows), 1)             # music is registered
            self.assertEqual(rows[0]["mediatype"], 1)  # filed as audio, not Other
            # The corrupt ArtworkDB was rebuilt into something valid, not carried
            # forward (which would re-fail on every future sync).
            ipod_artwork.validate(td)

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
