"""Autosync watcher: connect detection, profile gating, and job dispatch."""
from __future__ import annotations

import unittest
from unittest import mock

from iamped import autosync


class FakeDev:
    def __init__(self, mountpoint, is_ipod=False, writable=True, name="Player"):
        self.mountpoint = mountpoint
        self.is_ipod = is_ipod
        self.writable = writable
        self.name = name


class AutosyncScanTests(unittest.TestCase):
    def setUp(self):
        autosync._WATCH["present"] = set()

    def _scan(self, devices, profiles, jobs=None, started=None):
        started = started if started is not None else []

        def fake_start_job(target, *args):
            started.append(args)
            return "jid"

        with mock.patch("iamped.devices.list_devices",
                        return_value=devices), \
                mock.patch.object(autosync.device_management, "device_id",
                                  side_effect=lambda p, t: f"id:{p}"), \
                mock.patch.object(autosync.device_management, "profiles",
                                  return_value=profiles), \
                mock.patch("iamped.server.JOBS", jobs or {}), \
                mock.patch("iamped.server.start_job", fake_start_job):
            autosync._scan_once()
        return started

    def test_new_device_with_autosync_profile_triggers(self):
        dev = FakeDev("/Volumes/IPOD", is_ipod=True)
        profiles = {"id:/Volumes/IPOD": {
            "autosync": True, "name": "My iPod", "fill_strategy": "top_rated",
            "playlist_ids": ["7"], "target_bitrate_k": 256}}
        started = self._scan([dev], profiles)
        self.assertEqual(len(started), 1)
        params, profile = started[0]
        self.assertEqual(params["device_path"], "/Volumes/IPOD")
        self.assertEqual(params["device_type"], "ipod")
        self.assertEqual(params["fill_strategy"], "top_rated")
        self.assertEqual(params["playlist_ids"], ["7"])
        self.assertEqual(params["device_name"], "My iPod")
        self.assertTrue(profile["autosync"])

    def test_profile_without_autosync_is_ignored(self):
        dev = FakeDev("/Volumes/USB")
        started = self._scan([dev], {"id:/Volumes/USB": {"name": "USB"}})
        self.assertEqual(started, [])
        # still recorded as present so enabling autosync later needs a replug
        self.assertIn("id:/Volumes/USB", autosync._WATCH["present"])

    def test_still_connected_device_does_not_retrigger(self):
        dev = FakeDev("/Volumes/IPOD", is_ipod=True)
        profiles = {"id:/Volumes/IPOD": {"autosync": True}}
        started = self._scan([dev], profiles)
        self.assertEqual(len(started), 1)
        started2 = self._scan([dev], profiles)
        self.assertEqual(started2, [])

    def test_unplug_then_replug_retriggers(self):
        dev = FakeDev("/Volumes/IPOD", is_ipod=True)
        profiles = {"id:/Volumes/IPOD": {"autosync": True}}
        self._scan([dev], profiles)
        self._scan([], profiles)          # unplugged
        started = self._scan([dev], profiles)
        self.assertEqual(len(started), 1)

    def test_device_with_running_job_is_skipped(self):
        dev = FakeDev("/Volumes/IPOD", is_ipod=True)
        profiles = {"id:/Volumes/IPOD": {"autosync": True}}
        jobs = {"j1": {"status": "running", "device_path": "/Volumes/IPOD"}}
        started = self._scan([dev], profiles, jobs=jobs)
        self.assertEqual(started, [])


class PrefetchTokenTests(unittest.TestCase):
    def test_new_plan_supersedes_old_prefetch(self):
        from iamped.api import syncing
        t1 = syncing._cancel_prefetch()
        t2 = syncing._cancel_prefetch()
        self.assertGreater(t2, t1)
        self.assertNotEqual(syncing._PREFETCH["token"], t1)
        self.assertEqual(syncing._PREFETCH["token"], t2)


if __name__ == "__main__":
    unittest.main()
