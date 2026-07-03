"""Incremental library refresh: meta watermark, pruning, and delta sweeps."""
from __future__ import annotations

import tempfile
import unittest
from unittest import mock

from iamped.library import Library
from iamped.plex_client import TrackMeta


def track(key: str, title: str = "T", views: int = 0) -> TrackMeta:
    return TrackMeta(
        rating_key=key, title=title, artist="A", album="AL", album_artist="A",
        genre="", year=2001, track_number=1, disc_number=1, duration_ms=1000,
        view_count=views, user_rating=None, last_viewed_at=None,
        container="mp3", codec="mp3", bitrate=192, file_size=1000,
        part_key=f"/parts/{key}.mp3", server_file=f"/m/{key}.mp3")


class LibraryMetaTests(unittest.TestCase):
    def test_meta_roundtrip_and_prune(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Library(f"{td}/library.db")
            self.assertEqual(lib.get_meta("last_sync_watermark", "0"), "0")
            lib.set_meta("last_sync_watermark", "1234")
            lib.set_meta("last_sync_watermark", "5678")   # upsert, not dup
            self.assertEqual(lib.get_meta("last_sync_watermark"), "5678")

            lib.upsert_tracks([track("1"), track("2"), track("3")])
            self.assertEqual(lib.track_count(), 3)
            pruned = lib.prune_missing(["1", "3"])
            self.assertEqual(pruned, 1)
            self.assertEqual(lib.track_count(), 2)
            self.assertEqual(set(lib.get_tracks(["1", "2", "3"])), {"1", "3"})


class DeltaBuildTests(unittest.TestCase):
    def _run(self, td, mode, iter_side_effect, server_total):
        from iamped.api import library as srv
        lib = Library(f"{td}/library.db")
        job: dict = {}
        with mock.patch.object(srv, "get_server"), \
                mock.patch.object(srv, "_lib", return_value=lib), \
                mock.patch.object(srv.plex_client, "iter_tracks",
                                  side_effect=iter_side_effect) as it, \
                mock.patch.object(srv.plex_client, "section_track_count",
                                  return_value=server_total), \
                mock.patch.object(srv.plex_client, "list_playlists",
                                  return_value=[]), \
                mock.patch.object(srv.config, "mirror_library_to_stable"):
            srv._build_library_job(job, "Music", mode)
        return lib, job, it

    def test_first_run_is_full_and_sets_watermark(self):
        with tempfile.TemporaryDirectory() as td:
            def full(server, section, progress=None, **kw):
                return iter([track("1"), track("2")])
            lib, job, it = self._run(td, "auto", full, server_total=2)
            self.assertEqual(job["result"]["mode"], "full")
            self.assertEqual(job["result"]["tracks"], 2)
            self.assertGreater(int(lib.get_meta("last_sync_watermark")), 0)
            # full mode never passes a since filter
            self.assertIsNone(it.call_args.kwargs.get("since"))

    def test_delta_sweeps_three_fields_and_dedups(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Library(f"{td}/library.db")
            lib.upsert_tracks([track("1"), track("2")])
            lib.set_meta("last_sync_watermark", "1000")

            # same track comes back from two sweeps; a new one from the third
            def delta(server, section, progress=None, since=None,
                      since_field=""):
                if since_field == "updatedAt":
                    return iter([track("2", views=5)])
                if since_field == "lastViewedAt":
                    return iter([track("2", views=5), track("3")])
                return iter([])

            from iamped.api import library as srv
            job: dict = {}
            with mock.patch.object(srv, "get_server"), \
                    mock.patch.object(srv, "_lib", return_value=lib), \
                    mock.patch.object(srv.plex_client, "iter_tracks",
                                      side_effect=delta) as it, \
                    mock.patch.object(srv.plex_client, "section_track_count",
                                      return_value=3), \
                    mock.patch.object(srv.plex_client, "list_playlists",
                                      return_value=[]), \
                    mock.patch.object(srv.config, "mirror_library_to_stable"):
                srv._build_library_job(job, "Music", "auto")

            self.assertEqual(job["result"]["mode"], "delta")
            self.assertEqual(job["result"]["tracks"], 2)   # deduped across sweeps
            self.assertEqual(lib.track_count(), 3)
            fields = [c.kwargs["since_field"] for c in it.call_args_list]
            self.assertEqual(fields,
                             ["updatedAt", "lastViewedAt", "lastRatedAt"])
            self.assertTrue(all(c.kwargs["since"] == 1000
                                for c in it.call_args_list))

    def test_delta_escalates_to_full_on_server_deletion(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Library(f"{td}/library.db")
            lib.upsert_tracks([track("1"), track("2"), track("gone")])
            lib.set_meta("last_sync_watermark", "1000")

            def side(server, section, progress=None, since=None, **kw):
                if since:                        # delta sweeps see nothing new
                    return iter([])
                return iter([track("1"), track("2")])   # full pass

            from iamped.api import library as srv
            job: dict = {}
            with mock.patch.object(srv, "get_server"), \
                    mock.patch.object(srv, "_lib", return_value=lib), \
                    mock.patch.object(srv.plex_client, "iter_tracks",
                                      side_effect=side), \
                    mock.patch.object(srv.plex_client, "section_track_count",
                                      return_value=2), \
                    mock.patch.object(srv.plex_client, "list_playlists",
                                      return_value=[]), \
                    mock.patch.object(srv.config, "mirror_library_to_stable"):
                srv._build_library_job(job, "Music", "auto")

            self.assertEqual(job["result"]["mode"], "full")
            self.assertEqual(job["result"]["pruned"], 1)
            self.assertEqual(lib.track_count(), 2)


if __name__ == "__main__":
    unittest.main()
