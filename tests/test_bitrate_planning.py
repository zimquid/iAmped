from __future__ import annotations

import unittest
from unittest.mock import patch

from iamped import filler
from iamped.sync import base


def audio(key: str, *, bitrate: int = 320, size: int = 3_000_000) -> dict:
    return {
        "rating_key": key,
        "title": f"Track {key}",
        "artist": "Artist",
        "duration_ms": 60_000,
        "container": "mp3",
        "codec": "mp3",
        "bitrate": bitrate,
        "file_size": size,
    }


class FakeLibrary:
    def __init__(self):
        self.rows = {"1": audio("1"), "2": audio("2")}

    def playlist_track_keys_any(self, _pid):
        return ["1", "2"]

    def get_tracks(self, keys):
        return {key: self.rows[key] for key in keys if key in self.rows}

    def all_playlists(self):
        return [{"id": "playlist", "title": "Playlist"}]

    def ordered_tracks(self, _strategy):
        raise AssertionError("playlist-only planning must not fill from library")


class BitratePlanningTest(unittest.TestCase):
    def test_lower_target_bitrate_reduces_high_bitrate_source_estimate(self):
        track = audio("1", bitrate=320, size=3_000_000)
        reduced = filler.device_size(track, True, "mp3", 128)
        original = filler.device_size(track, True, "mp3", 320)

        self.assertLess(reduced, original)
        self.assertEqual(original, track["file_size"])

    def test_playlist_only_plan_reports_oversize_and_preserves_order(self):
        plan = filler.plan(
            FakeLibrary(), capacity_bytes=1_600_000, reserve_bytes=0,
            fill_strategy="most_played", include_playlist_ids=["playlist"],
            transcode_lossless=True, target_format="mp3",
            target_bitrate_k=128, fill_remaining=False)

        self.assertEqual(plan["requested_track_count"], 2)
        self.assertEqual(plan["track_count"], 1)
        self.assertEqual(plan["skipped_for_space"], 1)
        self.assertEqual(plan["playlists"][0]["track_keys"], ["1"])

    def test_transcode_dispatches_selected_bitrate(self):
        with patch.object(
                base, "transcode_to_mp3", return_value="/tmp/out.mp3") as mp3:
            path, ext = base.transcode("/tmp/in.flac", "/tmp/out", "mp3", 160)

        self.assertEqual((path, ext), ("/tmp/out.mp3", ".mp3"))
        mp3.assert_called_once_with("/tmp/in.flac", "/tmp/out.mp3", 160)


if __name__ == "__main__":
    unittest.main()
