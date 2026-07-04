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

    def test_transcodes_clamp_to_ipod_safe_rate_and_channels(self):
        # A hi-res/multichannel source re-encoded at its native rate/layout plays
        # back glitchy on classic iPods (they decode only 44.1 kHz stereo). Both
        # audio transcoders must force -ar 44100 -ac 2.
        for fn, dst in (("transcode_to_mp3", "/tmp/out.mp3"),
                        ("transcode_to_aac", "/tmp/out.m4a")):
            with patch.object(base.os, "makedirs"), \
                    patch.object(base.os, "replace"), \
                    patch.object(base.subprocess, "run") as run:
                getattr(base, fn)("/tmp/in.flac", dst)
            cmd = run.call_args[0][0]
            self.assertIn("-ar", cmd)
            self.assertEqual(cmd[cmd.index("-ar") + 1], "44100")
            self.assertIn("-ac", cmd)
            self.assertEqual(cmd[cmd.index("-ac") + 1], "2")

    def test_aac_transcode_uses_resolved_encoder(self):
        # ffmpeg's native `aac` stutters on old iPods; Apple's `aac_at`
        # (AudioToolbox, macOS) does not. transcode_to_aac must emit the
        # resolved encoder, and it must be one of the two known-good choices.
        self.assertIn(base.aac_codec(), ("aac_at", "aac"))
        with patch.object(base.os, "makedirs"), \
                patch.object(base.os, "replace"), \
                patch.object(base.subprocess, "run") as run:
            base.transcode_to_aac("/tmp/in.flac", "/tmp/out.m4a", 256)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[cmd.index("-c:a") + 1], base.aac_codec())

    def test_aac_encoder_prefers_apple_grade(self):
        # libfdk_aac (fre:ac's encoder) preferred first, then Apple's aac_at,
        # native aac only as last resort.
        with patch.object(base, "_ffmpeg_encoders",
                          return_value="aac aac_at libfdk_aac alac"):
            self.assertEqual(base.aac_encoder(), "libfdk_aac")
        with patch.object(base, "_ffmpeg_encoders", return_value="aac aac_at alac"):
            self.assertEqual(base.aac_encoder(), "aac_at")
        with patch.object(base, "_ffmpeg_encoders", return_value="aac alac"):
            self.assertEqual(base.aac_encoder(), "aac")

    def test_alac_transcode_is_lossless_16bit_cd_rate(self):
        with patch.object(base.os, "makedirs"), \
                patch.object(base.os, "replace"), \
                patch.object(base.subprocess, "run") as run:
            path, ext = base.transcode("/tmp/in.flac", "/tmp/out", "alac")
        self.assertEqual(ext, ".m4a")
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "alac")
        self.assertEqual(cmd[cmd.index("-ar") + 1], "44100")
        self.assertEqual(cmd[cmd.index("-sample_fmt") + 1], "s16p")
        self.assertNotIn("-b:a", cmd)               # lossless — no bitrate

    def test_should_transcode_to_alac_only_lossless(self):
        flac = {"container": "flac", "codec": "flac", "bitrate": 900}
        mp3 = {"container": "mp3", "codec": "mp3", "bitrate": 320}
        # ALAC target: re-encode lossless sources, pass lossy through untouched.
        self.assertTrue(filler.should_transcode_to(flac, "alac", True))
        self.assertFalse(filler.should_transcode_to(mp3, "alac", True, 256))
        # AAC target keeps the plain lossless/over-bitrate rule.
        self.assertTrue(filler.should_transcode_to(flac, "aac", True, 256))


if __name__ == "__main__":
    unittest.main()
