"""Radio/station builders must not emit the same song twice — including the
same track from a different album (a different ratingKey) and unicode/
punctuation variants. Regression test for the duplicate-songs bug."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from iamped import plex_client


def _track(rk, artist, title):
    """Minimal stand-in for a plexapi Track that track_to_meta() can read."""
    part = SimpleNamespace(container="mp3", size=1000, key=f"/p/{rk}", file="")
    media = SimpleNamespace(parts=[part], container="mp3", audioCodec="mp3",
                            bitrate=256)
    return SimpleNamespace(
        ratingKey=rk, title=title, grandparentTitle=artist, parentTitle="Album",
        originalTitle=artist, parentYear=2000, year=2000, index=1, parentIndex=1,
        duration=1000, viewCount=0, userRating=None, lastViewedAt=None,
        media=[media], type="track")


class RadioDedupTests(unittest.TestCase):
    def _station(self, tracks):
        station = SimpleNamespace(title="Library Radio", key="/station/1")
        section = SimpleNamespace(stations=lambda: [station])
        server = SimpleNamespace(library=SimpleNamespace(section=lambda _t: section))
        pq = SimpleNamespace(items=tracks)
        with patch.object(plex_client.PlayQueue, "fromStationKey",
                          return_value=pq):
            metas, warn = plex_client.station_tracks(
                server, "Music", "Library Radio", limit=50)
        return metas

    def test_same_song_other_album_is_deduped(self):
        # Same song, two different albums => two ratingKeys, but one song.
        metas = self._station([
            _track("1", "Gorillaz", "Clint Eastwood"),
            _track("2", "Gorillaz", "Clint Eastwood"),   # other album / single
            _track("3", "Gorillaz", "Feel Good Inc."),
        ])
        self.assertEqual(len(metas), 2)

    def test_unicode_and_punctuation_variants_deduped(self):
        metas = self._station([
            _track("1", "Beyoncé", "Déjà Vu (feat. Jay-Z)"),
            _track("2", "Beyonce", "Deja Vu (feat Jay Z)"),
            _track("3", "Adele", "Hello"),
        ])
        self.assertEqual(len(metas), 2)

    def test_remaster_and_live_variants_deduped(self):
        # The same recording reissued under version tags must collapse to one.
        metas = self._station([
            _track("1", "Pink Floyd", "Time"),
            _track("2", "Pink Floyd", "Time (2011 Remastered Version)"),
            _track("3", "Pink Floyd", "Time - 2011 Remaster"),
            _track("4", "Pink Floyd", "Money (Radio Edit)"),
            _track("5", "Pink Floyd", "Money"),
        ])
        self.assertEqual(len(metas), 2)
        self.assertEqual(
            sorted(m.title for m in metas)[0].split(" ")[0], "Money")

    def test_variant_only_title_not_collapsed_to_artist(self):
        # A title that is *only* a tag must not fold two different songs into one.
        metas = self._station([
            _track("1", "Various", "(Live)"),
            _track("2", "Various", "(Acoustic)"),
        ])
        self.assertEqual(len(metas), 2)


if __name__ == "__main__":
    unittest.main()
