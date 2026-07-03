"""On-The-Go playlist parsing and resolution."""
from __future__ import annotations

import os
import struct
import tempfile
import unittest
from unittest import mock

from iamped.sync import otg


def write_otg(device: str, name: str, indices: list[int]) -> str:
    d = os.path.join(device, "iPod_Control", "iTunes")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    with open(path, "wb") as fh:
        fh.write(b"mhpo")
        fh.write(struct.pack("<III", 0x14, 4, len(indices)))
        fh.write(b"\0" * 4)                     # pad to the 0x14 header
        for i in indices:
            fh.write(struct.pack("<I", i))
    return path


class FakeLib:
    def find_by_meta(self, artist, title, album=""):
        return "match-42" if title == "Foreign Song" else None


class OTGTests(unittest.TestCase):
    def test_parse_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_otg(td, "OTGPlaylistInfo", [3, 0, 7])
            self.assertEqual(otg.parse(path), [3, 0, 7])

    def test_parse_rejects_other_files(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "x")
            with open(path, "wb") as fh:
                fh.write(b"mhdp" + b"\0" * 20)
            with self.assertRaises(ValueError):
                otg.parse(path)

    def test_build_plans_resolves_managed_and_foreign(self):
        with tempfile.TemporaryDirectory() as td:
            write_otg(td, "OTGPlaylistInfo", [0, 1, 2, 9])
            db_path = os.path.join(td, "iPod_Control", "iTunes", "iTunesDB")
            with open(db_path, "wb") as fh:
                fh.write(b"stub")

            order = [
                {"track_id": 101, "title": "Managed", "artist": "A", "album": "X"},
                {"track_id": 102, "title": "Foreign Song", "artist": "B", "album": "Y"},
                {"track_id": 103, "title": "Unknown", "artist": "C", "album": "Z"},
            ]
            manifest = {"tracks": [
                {"track_id": 101, "rating_key": "rk-1"},
            ]}
            with mock.patch("iamped.sync.itunesdb.read_tracks_full",
                            return_value=order), \
                    mock.patch("iamped.sync.itunesdb.read_manifest",
                               return_value=manifest):
                plans = otg.build_plans(FakeLib(), td)

            self.assertEqual(len(plans), 1)
            plan = plans[0]
            self.assertEqual(plan["title"], "On-The-Go")
            # index 9 is out of range and dropped; 3 tracks remain
            self.assertEqual(len(plan["tracks"]), 3)
            self.assertEqual(plan["tracks"][0]["rating_key"], "rk-1")
            self.assertEqual(plan["tracks"][1]["rating_key"], "match-42")
            self.assertIsNone(plan["tracks"][2]["rating_key"])
            self.assertEqual(plan["matched"], 2)
            self.assertEqual(plan["unmatched"], 1)

    def test_clear_removes_files(self):
        with tempfile.TemporaryDirectory() as td:
            write_otg(td, "OTGPlaylistInfo", [0])
            write_otg(td, "OTGPlaylistInfo_1", [1])
            self.assertEqual(otg.clear(td), 2)
            self.assertEqual(otg.find_files(td), [])


if __name__ == "__main__":
    unittest.main()
