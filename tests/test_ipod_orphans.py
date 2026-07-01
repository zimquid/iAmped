"""The iPod 'Other' problem: audio files under iPod_Control/Music that no
iTunesDB entry references (left by interrupted or older-build syncs) silently
eat capacity. sweep_orphans() must delete exactly those — and nothing the DB
still points at."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from iamped.sync.device_state import PART_SUFFIX
from iamped.sync.itunesdb import ITunesDBBackend


def _audio(key: str) -> dict:
    return {
        "rating_key": key, "title": f"Song {key}", "artist": "Artist",
        "album_artist": "Artist", "album": "Album", "duration_ms": 60_000,
        "track_number": 1, "view_count": 0, "user_rating": 0,
    }


class OrphanSweepTest(unittest.TestCase):
    def test_sweep_removes_only_unreferenced_files(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src.bin"
            src.write_bytes(b"x" * 64)

            backend = ITunesDBBackend(td, "Test iPod")
            backend.prepare()
            rec = backend.add_track(_audio("a1"), str(src), ".m4a")
            backend.add_track(_audio("a2"), str(src), ".m4a")
            backend.finalize()

            music = Path(td) / "iPod_Control" / "Music"
            kept = Path(td) / rec["path"]     # a real, DB-referenced track file
            self.assertTrue(kept.exists())

            # Plant junk: an orphaned audio file and a stray temp file from a
            # copy that died mid-write. Neither is in the iTunesDB.
            orphan = music / "F07" / "99999.m4a"
            orphan.write_bytes(b"y" * 5000)
            half = music / "F07" / ("00042.m4a" + PART_SUFFIX)
            half.write_bytes(b"z" * 3000)

            # Re-open (as the standalone reclaim does): stage what the DB
            # references, then sweep.
            reopened = ITunesDBBackend(td, "Test iPod")
            reopened.prepare()
            reopened.import_existing()
            res = reopened.sweep_orphans()

            self.assertEqual(res["removed"], 2)
            self.assertEqual(res["freed_bytes"], 8000)
            self.assertFalse(orphan.exists())
            self.assertFalse(half.exists())
            # Real tracks the DB references are untouched.
            self.assertTrue(kept.exists())

    def test_sweep_noop_when_clean(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src.bin"
            src.write_bytes(b"x" * 64)
            backend = ITunesDBBackend(td, "Test iPod")
            backend.prepare()
            backend.add_track(_audio("a1"), str(src), ".m4a")
            backend.finalize()

            reopened = ITunesDBBackend(td, "Test iPod")
            reopened.prepare()
            reopened.import_existing()
            res = reopened.sweep_orphans()
            self.assertEqual(res["removed"], 0)
            self.assertEqual(res["freed_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
