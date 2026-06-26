"""Structural round-trip tests for iTunesDB *video* records (movies + TV).

The video iTunesDB layout can't be validated against hardware here, so we verify
that what add_video() writes — mediatype, season/episode numbers, and the TV
show/episode/description strings — decodes back correctly, and that audio tracks
are left untouched (mediatype 0) so the hardware-validated music path doesn't
regress.
"""
from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from iamped.sync.itunesdb import (ITunesDBBackend, MEDIATYPE_AUDIO,
                                  MEDIATYPE_MOVIE, MEDIATYPE_TVSHOW,
                                  read_tracks_full)


def _audio(key: str) -> dict:
    return {
        "rating_key": key, "title": "Song", "artist": "Artist",
        "album_artist": "Artist", "album": "Album", "duration_ms": 60_000,
        "track_number": 1, "view_count": 2, "user_rating": 8,
    }


def _movie(key: str) -> tuple[dict, dict]:
    track = {"rating_key": key, "title": "Blade Runner", "artist": "",
             "album": "", "duration_ms": 6_000_000, "year": 1982}
    video = {"summary": "A blade runner must pursue replicants."}
    return track, video


def _episode(key: str) -> tuple[dict, dict]:
    track = {"rating_key": key, "title": "Ozymandias", "artist": "Breaking Bad",
             "album": "Breaking Bad", "duration_ms": 2_800_000, "year": 2013}
    video = {"show": "Breaking Bad", "subtitle": "Ozymandias",
             "episode_id": "S05E14", "summary": "Everything falls apart.",
             "season": 5, "episode": 14}
    return track, video


class VideoITunesDBTest(unittest.TestCase):
    def _build(self, root: str):
        # a fake source file each entry copies from
        src = Path(root) / "src.bin"
        src.write_bytes(b"x" * 32)
        backend = ITunesDBBackend(root, "Test iPod")
        backend.prepare()
        backend.add_track(_audio("a1"), str(src), ".m4a")
        mt, vt = _movie("m1")
        backend.add_video(mt, str(src), ".m4v", MEDIATYPE_MOVIE, vt)
        et, ve = _episode("e1")
        backend.add_video(et, str(src), ".m4v", MEDIATYPE_TVSHOW, ve)
        summary = backend.self_test(backend._build_db())
        backend.finalize()
        return summary

    def test_movie_and_episode_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = self._build(tmp)
            self.assertEqual(summary["tracks"], 3)
            self.assertEqual(summary["videos"], 2)

            db = (Path(tmp) / "iPod_Control" / "iTunes" / "iTunesDB").read_bytes()
            rows = {r["title"]: r for r in read_tracks_full(db)}

            # audio entry must be flagged ITDB_MEDIATYPE_AUDIO (1), NOT 0 — a 0
            # makes the 5.5G/classic leak songs into the Videos / Music Videos
            # menus. This is the regression guard for that bug.
            self.assertEqual(rows["Song"]["mediatype"], MEDIATYPE_AUDIO)

            movie = rows["Blade Runner"]
            self.assertEqual(movie["mediatype"], MEDIATYPE_MOVIE)
            self.assertEqual(movie["description"],
                             "A blade runner must pursue replicants.")

            ep = rows["Ozymandias"]
            self.assertEqual(ep["mediatype"], MEDIATYPE_TVSHOW)
            self.assertEqual(ep["season_number"], 5)
            self.assertEqual(ep["episode_number"], 14)
            self.assertEqual(ep["tv_show"], "Breaking Bad")
            self.assertEqual(ep["tv_episode_id"], "S05E14")
            self.assertEqual(ep["subtitle"], "Ozymandias")

    def test_mediatype_at_libgpod_absolute_offsets(self):
        # Guard the exact byte offsets independently of read_tracks_full, so a
        # matched-but-wrong read/write pair can't pass. Per libgpod
        # itdb_itunesdb.c: mediatype @ mhit+0xD0, season_nr @ +0xD4,
        # episode_nr @ +0xD8.
        with tempfile.TemporaryDirectory() as tmp:
            self._build(tmp)
            db = (Path(tmp) / "iPod_Control" / "iTunes" / "iTunesDB").read_bytes()
            at = {r["title"]: r["offset"] for r in read_tracks_full(db)}
            u32 = lambda off: struct.unpack_from("<I", db, off)[0]

            self.assertEqual(u32(at["Song"] + 0xD0), MEDIATYPE_AUDIO)
            self.assertEqual(u32(at["Blade Runner"] + 0xD0), MEDIATYPE_MOVIE)
            ep = at["Ozymandias"]
            self.assertEqual(u32(ep + 0xD0), MEDIATYPE_TVSHOW)
            self.assertEqual(u32(ep + 0xD4), 5)    # season_nr
            self.assertEqual(u32(ep + 0xD8), 14)   # episode_nr

    def test_video_flag_survives_reimport(self):
        # A re-sync re-stages existing tracks via import_existing(); the video
        # flag and TV fields must survive so they aren't demoted to music.
        with tempfile.TemporaryDirectory() as tmp:
            self._build(tmp)
            backend = ITunesDBBackend(tmp, "Test iPod")
            backend.prepare()
            backend.import_existing()
            db = backend._build_db()
            backend.self_test(db)
            rows = {r["title"]: r for r in read_tracks_full(db)}
            self.assertEqual(rows["Blade Runner"]["mediatype"], MEDIATYPE_MOVIE)
            self.assertEqual(rows["Ozymandias"]["mediatype"], MEDIATYPE_TVSHOW)
            self.assertEqual(rows["Ozymandias"]["season_number"], 5)
            self.assertEqual(rows["Ozymandias"]["episode_number"], 14)
            self.assertEqual(rows["Song"]["mediatype"], MEDIATYPE_AUDIO)


if __name__ == "__main__":
    unittest.main()
