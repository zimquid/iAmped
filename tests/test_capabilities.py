from __future__ import annotations

import os
import tempfile
import unittest

from iamped.devices.capabilities import (LAYOUT_FLAT, LAYOUT_NESTED,
                                         TRANSPORT_IPOD, TRANSPORT_MTP,
                                         TRANSPORT_UMS, classify,
                                         is_plain_volume, resolve)
from iamped.devices.model import Device
from iamped.sync.massstorage import MassStorageBackend


def dev(**kw) -> Device:
    base = dict(id="x", name="", fs="fat32", mountpoint=None)
    base.update(kw)
    return Device(**base)


class ClassifyTests(unittest.TestCase):
    def test_muvo_is_flat(self):
        cap = classify(dev(name="NO NAME", model="MuVo TX FM", mountpoint="/x"))
        self.assertEqual(cap.transport, TRANSPORT_UMS)
        self.assertEqual(cap.layout, LAYOUT_FLAT)

    def test_unknown_defaults_flat(self):
        cap = classify(dev(name="USB DISK", model="", mountpoint="/x"))
        self.assertEqual(cap.layout, LAYOUT_FLAT)

    def test_ipod(self):
        cap = classify(dev(name="IPOD", is_ipod=True, mountpoint="/x"))
        self.assertEqual(cap.transport, TRANSPORT_IPOD)

    def test_mtp_busloc(self):
        cap = classify(dev(name="Creative ZEN", fs="mtp", mtp_busloc="0,5"))
        self.assertEqual(cap.transport, TRANSPORT_MTP)

    def test_sansa_recursive(self):
        cap = classify(dev(name="SANSA CLIP", model="Sansa Clip", mountpoint="/x"))
        self.assertEqual(cap.layout, LAYOUT_NESTED)

    def test_rockbox_marker_dir(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".rockbox"))
            cap = classify(dev(name="GENERIC", mountpoint=d))
            self.assertEqual(cap.layout, LAYOUT_NESTED)

    def test_profile_override_wins(self):
        device = dev(name="MuVo", model="MuVo TX FM", mountpoint="/x")
        cap = resolve(device, {"layout": LAYOUT_NESTED})
        self.assertEqual(cap.layout, LAYOUT_NESTED)
        self.assertEqual(cap.source, "override")


class FlatLayoutTests(unittest.TestCase):
    def test_flat_layout_places_one_level_deep(self):
        track = {"rating_key": "1", "title": "Song", "artist": "Band",
                 "album_artist": "Band", "album": "Record", "track_number": 3,
                 "duration_ms": 1000}
        with tempfile.TemporaryDirectory() as root:
            src = os.path.join(root, "src.mp3")
            with open(src, "wb") as fh:
                fh.write(b"audio")
            backend = MassStorageBackend(root, layout="flat")
            backend.prepare()
            rec = backend.add_track(track, src, ".mp3")
            # one folder deep, no "Music/" wrapper
            self.assertEqual(rec["path"], os.path.join("Band - Record",
                                                       "03 - Song.mp3"))
            self.assertTrue(os.path.exists(os.path.join(root, rec["path"])))

    def test_nested_layout_unchanged(self):
        track = {"rating_key": "1", "title": "Song", "artist": "Band",
                 "album_artist": "Band", "album": "Record", "track_number": 3,
                 "duration_ms": 1000}
        with tempfile.TemporaryDirectory() as root:
            src = os.path.join(root, "src.mp3")
            with open(src, "wb") as fh:
                fh.write(b"audio")
            backend = MassStorageBackend(root, layout="nested")
            backend.prepare()
            rec = backend.add_track(track, src, ".mp3")
            self.assertEqual(
                rec["path"],
                os.path.join("Music", "Band", "Record", "03 - Song.mp3"))


class MigrationTests(unittest.TestCase):
    """_migrate_record moves files when the layout changes between syncs."""

    def _make_file(self, root: str, rel: str) -> str:
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(b"audio")
        return full

    def test_nested_to_flat_migration(self):
        with tempfile.TemporaryDirectory() as root:
            old_rel = os.path.join("Music", "Band", "Record", "03 - Song.mp3")
            self._make_file(root, old_rel)
            record = {"rating_key": "1", "path": old_rel, "artist": "Band",
                      "album": "Record", "size": 5}
            backend = MassStorageBackend(root, layout="flat")
            new_record = backend._migrate_record(record)
            expected = os.path.join("Band - Record", "03 - Song.mp3")
            self.assertEqual(new_record["path"], expected)
            self.assertTrue(os.path.exists(os.path.join(root, expected)))
            self.assertFalse(os.path.exists(os.path.join(root, old_rel)))
            # old Music/Band/Record/ tree should be pruned
            self.assertFalse(os.path.exists(os.path.join(root, "Music", "Band")))

    def test_flat_to_nested_migration(self):
        with tempfile.TemporaryDirectory() as root:
            old_rel = os.path.join("Band - Record", "03 - Song.mp3")
            self._make_file(root, old_rel)
            record = {"rating_key": "1", "path": old_rel, "artist": "Band",
                      "album": "Record", "size": 5}
            backend = MassStorageBackend(root, layout="nested")
            new_record = backend._migrate_record(record)
            expected = os.path.join("Music", "Band", "Record", "03 - Song.mp3")
            self.assertEqual(new_record["path"], expected)
            self.assertTrue(os.path.exists(os.path.join(root, expected)))
            self.assertFalse(os.path.exists(os.path.join(root, old_rel)))

    def test_no_migration_needed(self):
        with tempfile.TemporaryDirectory() as root:
            old_rel = os.path.join("Band - Record", "03 - Song.mp3")
            self._make_file(root, old_rel)
            record = {"rating_key": "1", "path": old_rel, "artist": "Band",
                      "album": "Record", "size": 5}
            backend = MassStorageBackend(root, layout="flat")
            new_record = backend._migrate_record(record)
            self.assertEqual(new_record["path"], old_rel)


class SdCardModeTests(unittest.TestCase):
    def _card(self):
        return dev(name="UNTITLED", model="", mountpoint="/Volumes/CARD",
                   mounted=True)

    def test_plain_card_detected(self):
        self.assertTrue(is_plain_volume(self._card()))

    def test_ipod_is_not_plain(self):
        self.assertFalse(is_plain_volume(
            dev(name="IPOD", is_ipod=True, mountpoint="/x", mounted=True)))

    def test_known_player_is_not_plain(self):
        self.assertFalse(is_plain_volume(
            dev(name="NO NAME", model="MuVo TX FM", mountpoint="/x",
                mounted=True)))

    def test_unmounted_is_not_plain(self):
        self.assertFalse(is_plain_volume(dev(name="CARD", mountpoint="/x")))

    def test_sd_off_stays_flat(self):
        card = self._card()
        cap = classify(card, sd_mode=False)
        self.assertEqual(cap.layout, LAYOUT_FLAT)
        self.assertFalse(card.is_sd)

    def test_sd_on_uses_nested_and_flags_card(self):
        card = self._card()
        cap = classify(card, sd_mode=True)
        self.assertEqual(cap.transport, TRANSPORT_UMS)
        self.assertEqual(cap.layout, LAYOUT_NESTED)
        self.assertTrue(card.is_sd)

    def test_sd_on_does_not_override_flat_scanner(self):
        # A recognised flat-scan player keeps its required flat layout even in
        # SD mode — its marker wins over the generic-card branch.
        cap = classify(dev(name="NO NAME", model="MuVo TX FM",
                           mountpoint="/x", mounted=True), sd_mode=True)
        self.assertEqual(cap.layout, LAYOUT_FLAT)

    def test_resolve_passes_sd_mode(self):
        card = self._card()
        cap = resolve(card, None, sd_mode=True)
        self.assertEqual(cap.layout, LAYOUT_NESTED)


if __name__ == "__main__":
    unittest.main()
