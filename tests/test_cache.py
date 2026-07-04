from __future__ import annotations

import os
import time
import unittest
from unittest.mock import patch

from iamped import cache


class CacheEvictionTest(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.join(
            os.environ.get("TMPDIR", "/tmp"), f"iamped-cache-test-{os.getpid()}")
        os.makedirs(self.dir, exist_ok=True)
        for f in os.listdir(self.dir):
            os.remove(os.path.join(self.dir, f))

    def tearDown(self):
        for f in os.listdir(self.dir):
            os.remove(os.path.join(self.dir, f))
        os.rmdir(self.dir)

    def _write(self, name, size, age_secs):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as fh:
            fh.write(b"\0" * size)
        old = time.time() - age_secs
        os.utime(path, (old, old))
        return path

    def _cfg(self, limit_mb, aggressive=False):
        return {"cache_dir": self.dir, "cache_limit_mb": limit_mb,
                "aggressive_cache": aggressive}

    def test_evicts_least_recently_used_until_under_limit(self):
        # three 1 MB files, all old enough to be evictable; cap at 2 MB.
        self._write("old.flac", 1_000_000, 3600)
        self._write("mid.flac", 1_000_000, 1800)
        self._write("new.flac", 1_000_000, 300)
        limit_mb = 2 * 1_000_000 / (1024 * 1024)   # ~1.9 MB in MiB
        with patch.object(cache.config, "load", return_value=self._cfg(limit_mb)):
            res = cache.enforce_limit()
        remaining = set(os.listdir(self.dir))
        self.assertNotIn("old.flac", remaining)     # oldest evicted first
        self.assertIn("new.flac", remaining)        # newest kept
        self.assertGreaterEqual(res["removed"], 1)

    def test_grace_window_protects_in_flight_files(self):
        self._write("fresh.flac", 5_000_000, 1)      # written 1s ago
        with patch.object(cache.config, "load",
                          return_value=self._cfg(0.0001)):
            res = cache.enforce_limit()
        # Even with a tiny cap, a file inside the grace window is never yanked.
        self.assertIn("fresh.flac", os.listdir(self.dir))
        self.assertEqual(res["removed"], 0)

    def test_aggressive_mode_never_evicts(self):
        self._write("keep.flac", 5_000_000, 3600)
        with patch.object(cache.config, "load",
                          return_value=self._cfg(0.0001, aggressive=True)):
            res = cache.enforce_limit()
        self.assertIn("keep.flac", os.listdir(self.dir))
        self.assertEqual(res["removed"], 0)


if __name__ == "__main__":
    unittest.main()
