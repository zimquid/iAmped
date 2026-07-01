"""The cross-build login + library cache: a rebuild wipes the portable app dir,
but the Plex login and the metadata cache are restored from the stable store."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from iamped import config


class CrossBuildCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.stable = self.tmp / "stable"
        self._saved = (config.APP_DIR, config.CONFIG_PATH, config.DB_PATH,
                       config.CACHE_DIR, config.STABLE_DIR, config.CRED_PATH,
                       config.STABLE_DB)

    def tearDown(self):
        (config.APP_DIR, config.CONFIG_PATH, config.DB_PATH, config.CACHE_DIR,
         config.STABLE_DIR, config.CRED_PATH, config.STABLE_DB) = self._saved

    def _point_at(self, app: Path):
        # Emulate a frozen build whose portable data dir differs from the
        # stable per-user directory.
        config.APP_DIR = app
        config.CONFIG_PATH = app / "config.json"
        config.DB_PATH = app / "library.db"
        config.CACHE_DIR = app / "cache"
        config.STABLE_DIR = self.stable
        config.CRED_PATH = self.stable / "plex-auth.json"
        config.STABLE_DB = self.stable / "library.db"

    def test_login_and_library_survive_rebuild(self):
        build1 = self.tmp / "build1" / "iAmped-data"
        build1.mkdir(parents=True)
        self._point_at(build1)
        config.save({"plex_baseurl": "http://x:32400", "plex_token": "TOK",
                     "music_section": "Music"})
        config.DB_PATH.write_text("CACHED")
        config.mirror_library_to_stable()
        self.assertTrue(config.CRED_PATH.exists())
        self.assertTrue(config.STABLE_DB.exists())

        # Rebuild: brand-new empty portable dir; dist (and its data) was wiped.
        build2 = self.tmp / "build2" / "iAmped-data"
        self._point_at(build2)
        config.ensure_dirs()               # seeds library.db from the stable copy
        cfg = config.load()                # restores login from the stable copy
        self.assertEqual(cfg["plex_token"], "TOK")
        self.assertEqual(cfg["music_section"], "Music")
        self.assertTrue(config.DB_PATH.exists())
        self.assertEqual(config.DB_PATH.read_text(), "CACHED")

    def test_dev_run_writes_no_separate_cred_file(self):
        # When the stable dir == app dir (plain source run), there's no separate
        # credential cache — config.json already lives somewhere stable.
        app = self.tmp / "devhome"
        app.mkdir()
        config.APP_DIR = app
        config.CONFIG_PATH = app / "config.json"
        config.DB_PATH = app / "library.db"
        config.CACHE_DIR = app / "cache"
        config.STABLE_DIR = app
        config.CRED_PATH = app / "plex-auth.json"
        config.STABLE_DB = app / "library.db"
        config.save({"plex_token": "TOK"})
        self.assertFalse(config.CRED_PATH.exists())


if __name__ == "__main__":
    unittest.main()
