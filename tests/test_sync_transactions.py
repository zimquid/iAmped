from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from iamped import server
from iamped.sync import device_state, hash58
from iamped.sync.itunesdb import ITunesDBBackend, read_manifest, read_tracks_full
from iamped.sync.massstorage import MassStorageBackend


def track(key: str, title: str = "Song") -> dict:
    return {
        "rating_key": key,
        "title": title,
        "artist": "Artist",
        "album_artist": "Artist",
        "album": "Album",
        "duration_ms": 60_000,
        "track_number": 1,
        "view_count": 2,
        "user_rating": 8,
        "container": "mp3",
    }


class DeviceTransactionTest(unittest.TestCase):
    def test_hash58_sign_and_verify(self):
        database = bytearray(0xF4)
        database[:4] = b"mhbd"
        database[4:8] = (0xF4).to_bytes(4, "little")
        signed = hash58.sign(bytes(database), "000A27001A6E36F0")
        self.assertTrue(hash58.verify(signed, "000A27001A6E36F0"))
        damaged = bytearray(signed)
        damaged[-1] ^= 1
        self.assertFalse(hash58.verify(bytes(damaged), "000A27001A6E36F0"))

    def test_mirror_plan_diffs_keep_add_and_remove(self):
        class FakeLibrary:
            def all_playlists(self):
                return []

            def ordered_tracks(self, _strategy):
                return [
                    {**track("keep", "Keep"), "file_size": 10},
                    {**track("new", "New"), "file_size": 10},
                ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            keep_path = root / "Music" / "keep.mp3"
            stale_path = root / "Music" / "stale.mp3"
            keep_path.parent.mkdir()
            keep_path.write_bytes(b"k" * 10)
            stale_path.write_bytes(b"s" * 10)
            device_state.write_manifest(tmp, "massstorage", {
                "tracks": [
                    {"rating_key": "keep", "path": "Music/keep.mp3", "size": 10},
                    {"rating_key": "stale", "path": "Music/stale.mp3", "size": 10},
                ]
            })
            params = {
                "device_path": tmp,
                "device_type": "massstorage",
                "mirror": True,
                "reserve_mb": 0,
                "fill_strategy": "most_played",
                "playlist_ids": [],
                "transcode_lossless": False,
                "max_tracks": 2,
            }
            with patch.object(server, "_lib", return_value=FakeLibrary()), \
                    patch.object(server, "free_bytes", return_value=100):
                diff = server._device_plan(params)

            self.assertEqual(set(diff["keep_tracks"]), {"keep"})
            self.assertEqual([t["rating_key"] for t in diff["add_tracks"]], ["new"])
            self.assertEqual([r["rating_key"] for r in diff["removals"]], ["stale"])

    def test_sync_job_mirrors_managed_usb_tracks_end_to_end(self):
        class FakeLibrary:
            def all_playlists(self):
                return []

            def ordered_tracks(self, _strategy):
                return [
                    {**track("keep", "Keep"), "file_size": 10},
                    {**track("new", "New"), "file_size": 10},
                ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp3"
            source.write_bytes(b"new-audio")
            keep_path = root / "Music" / "keep.mp3"
            stale_path = root / "Music" / "stale.mp3"
            keep_path.parent.mkdir()
            keep_path.write_bytes(b"k" * 10)
            stale_path.write_bytes(b"s" * 10)
            device_state.write_manifest(tmp, "massstorage", {
                "tracks": [
                    {"rating_key": "keep", "path": "Music/keep.mp3", "size": 10},
                    {"rating_key": "stale", "path": "Music/stale.mp3", "size": 10},
                ]
            })
            params = {
                "device_path": tmp,
                "device_type": "massstorage",
                "mirror": True,
                "reserve_mb": 0,
                "fill_strategy": "most_played",
                "playlist_ids": [],
                "transcode_lossless": False,
                "max_tracks": 2,
            }
            job = {}
            with patch.object(server, "_lib", return_value=FakeLibrary()), \
                    patch.object(server, "get_server", return_value=object()), \
                    patch.object(server, "materialize",
                                 return_value=(str(source), ".mp3")):
                server._sync_job(job, params)

            manifest = device_state.read_manifest(tmp, "massstorage")
            self.assertEqual(
                {r["rating_key"] for r in manifest["tracks"]}, {"keep", "new"})
            self.assertTrue(keep_path.exists())
            self.assertFalse(stale_path.exists())
            self.assertEqual(job["result"]["tracks_added"], 1)
            self.assertEqual(job["result"]["tracks_removed"], 1)
            self.assertFalse(Path(device_state.journal_path(
                tmp, "massstorage")).exists())

    def test_mirror_plan_updates_track_when_plex_source_changes(self):
        class FakeLibrary:
            def all_playlists(self):
                return []

            def ordered_tracks(self, _strategy):
                return [{
                    **track("changed", "Changed"),
                    "file_size": 20,
                    "part_key": "/library/parts/new",
                }]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "Music" / "changed.mp3"
            media.parent.mkdir()
            media.write_bytes(b"x" * 10)
            device_state.write_manifest(tmp, "massstorage", {
                "tracks": [{
                    "rating_key": "changed",
                    "path": "Music/changed.mp3",
                    "size": 10,
                    "source_signature": "/library/parts/old|10|mp3||mp3",
                }]
            })
            params = {
                "device_path": tmp,
                "device_type": "massstorage",
                "mirror": True,
                "reserve_mb": 0,
                "fill_strategy": "most_played",
                "playlist_ids": [],
                "transcode_lossless": False,
                "max_tracks": 1,
            }
            with patch.object(server, "_lib", return_value=FakeLibrary()), \
                    patch.object(server, "free_bytes", return_value=100):
                diff = server._device_plan(params)

            self.assertEqual(diff["update_keys"], {"changed"})
            self.assertEqual(len(diff["add_tracks"]), 1)
            self.assertEqual(len(diff["removals"]), 1)

    def test_massstorage_manifest_supports_incremental_restore_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp3"
            source.write_bytes(b"audio-data")

            first = MassStorageBackend(tmp)
            first.prepare()
            record = first.add_track(track("1"), str(source), ".mp3")
            first.add_playlist("Road Trip", ["1"])
            first.finalize()

            manifest = device_state.read_manifest(tmp, "massstorage")
            self.assertEqual(manifest["tracks"][0]["rating_key"], "1")
            self.assertTrue((root / record["path"]).exists())

            second = MassStorageBackend(tmp)
            second.prepare()
            self.assertEqual(second.import_existing({"1": track("1")}), 1)
            second.finalize()
            self.assertTrue((root / record["path"]).exists())
            self.assertFalse((root / "Playlists" / "Road Trip.m3u8").exists())

            tx, resumed = device_state.start_or_resume(
                tmp, "massstorage", "plan-2", [manifest["tracks"][0]])
            self.assertFalse(resumed)
            removed = device_state.finish_cleanup(tmp, "massstorage", tx)
            self.assertEqual(removed, 1)
            self.assertFalse((root / record["path"]).exists())

    def test_matching_transaction_reuses_completed_atomic_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp3"
            source.write_bytes(b"x" * 4096)
            destination = root / "Music" / "Artist" / "Song.mp3"
            size = device_state.atomic_copy(str(source), str(destination))
            record = {
                "rating_key": "7",
                "path": os.path.relpath(destination, root),
                "size": size,
                "new_file": True,
            }

            tx, _ = device_state.start_or_resume(
                tmp, "massstorage", "same-plan", [])
            device_state.record_completed(tmp, "massstorage", tx, "7", record)
            resumed_tx, resumed = device_state.start_or_resume(
                tmp, "massstorage", "same-plan", [])

            self.assertTrue(resumed)
            self.assertTrue(device_state.record_is_valid(tmp, resumed_tx["completed"]["7"]))
            self.assertFalse(Path(str(destination) + device_state.PART_SUFFIX).exists())

    def test_different_plan_is_rejected_while_transaction_is_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            device_state.start_or_resume(tmp, "massstorage", "plan-a", [])
            with self.assertRaisesRegex(RuntimeError, "different settings"):
                device_state.start_or_resume(tmp, "massstorage", "plan-b", [])

    def test_native_ipod_database_and_manifest_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.m4a"
            source.write_bytes(b"native-audio")

            backend = ITunesDBBackend(tmp, "Test iPod")
            backend.prepare()
            record = backend.add_track(track("42"), str(source), ".m4a")
            backend.add_playlist("Favorites", ["42"])
            backend.finalize()

            db_path = root / "iPod_Control" / "iTunes" / "iTunesDB"
            rows = read_tracks_full(db_path.read_bytes())
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["title"], "Song")
            self.assertTrue((root / record["path"]).exists())

            manifest = read_manifest(tmp)
            self.assertEqual(manifest["device_name"], "Test iPod")
            self.assertEqual(manifest["tracks"][0]["rating_key"], "42")
            self.assertEqual(manifest["tracks"][0]["path"], record["path"])

    def test_native_resume_does_not_duplicate_track_after_database_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.m4a"
            source.write_bytes(b"native-audio")

            committed = ITunesDBBackend(tmp, "Test iPod")
            committed.prepare()
            record = committed.add_track(track("42"), str(source), ".m4a")
            # Simulate the narrow failure window: iTunesDB was committed but the
            # old/absent manifest was not replaced.
            db = committed._build_db()
            db_path = root / "iPod_Control" / "iTunes" / "iTunesDB"
            device_state.atomic_write_bytes(str(db_path), db)

            resumed = ITunesDBBackend(tmp, "Test iPod")
            resumed.prepare()
            carried = resumed.import_existing(
                {"42": track("42")}, {"42": record})
            resumed.restore_track(track("42"), record)
            resumed.finalize()

            self.assertEqual(carried, 0)
            self.assertEqual(len(read_tracks_full(db_path.read_bytes())), 1)


if __name__ == "__main__":
    unittest.main()
