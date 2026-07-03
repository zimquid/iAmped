"""Podcast feed parsing, subscription storage, and episode selection."""
from __future__ import annotations

import tempfile
import unittest

from iamped import podcasts
from iamped.library import Library

FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>Retro Computing</title>
    <itunes:author>The Hosts</itunes:author>
    <itunes:image href="https://example.com/cover.jpg"/>
    <item>
      <title>Episode 2: The iPod</title>
      <guid>ep-2</guid>
      <pubDate>Wed, 01 Jul 2026 08:00:00 +0000</pubDate>
      <itunes:duration>1:02:30</itunes:duration>
      <enclosure url="https://example.com/ep2.mp3" length="5000" type="audio/mpeg"/>
    </item>
    <item>
      <title>Episode 1</title>
      <guid>ep-1</guid>
      <pubDate>Mon, 01 Jun 2026 08:00:00 +0000</pubDate>
      <itunes:duration>1800</itunes:duration>
      <enclosure url="https://example.com/ep1.ogg" type="audio/ogg"/>
    </item>
    <item>
      <title>Video special (skipped)</title>
      <guid>ep-video</guid>
      <enclosure url="https://example.com/sp.mp4" type="video/mp4"/>
    </item>
  </channel>
</rss>
"""


class FeedParseTests(unittest.TestCase):
    def test_parse_feed(self):
        feed = podcasts.parse_feed(FEED)
        self.assertEqual(feed["title"], "Retro Computing")
        self.assertEqual(feed["author"], "The Hosts")
        self.assertEqual(feed["image"], "https://example.com/cover.jpg")
        eps = feed["episodes"]
        self.assertEqual([e["guid"] for e in eps], ["ep-2", "ep-1"])
        self.assertEqual(eps[0]["duration_ms"], 3750000)   # 1:02:30
        self.assertEqual(eps[1]["duration_ms"], 1800000)   # bare seconds
        self.assertEqual(eps[0]["size"], 5000)
        self.assertGreater(eps[0]["pub_date"], eps[1]["pub_date"])


class SubscriptionTests(unittest.TestCase):
    def test_upsert_preserves_played_state(self):
        with tempfile.TemporaryDirectory() as td:
            lib = Library(f"{td}/library.db")
            feed = podcasts.parse_feed(FEED)
            pid = lib.add_podcast("https://example.com/rss", feed["title"],
                                  feed["author"], feed["image"], 1000)
            self.assertEqual(lib.upsert_episodes(pid, feed["episodes"]), 2)

            eps = lib.episodes(pid)
            self.assertEqual(len(eps), 2)
            self.assertEqual(eps[0]["guid"], "ep-2")       # newest first

            lib.mark_episodes_played([eps[0]["id"]])
            # a re-refresh must not resurrect or duplicate episodes
            self.assertEqual(lib.upsert_episodes(pid, feed["episodes"]), 0)
            self.assertEqual(len(lib.episodes(pid)), 2)
            unplayed = lib.episodes(pid, unplayed_only=True)
            self.assertEqual([e["guid"] for e in unplayed], ["ep-1"])

            # re-adding the same URL updates rather than duplicates
            pid2 = lib.add_podcast("https://example.com/rss", "New Title",
                                   "", "", 2000)
            self.assertEqual(pid, pid2)
            self.assertEqual(len(lib.podcasts()), 1)
            self.assertEqual(lib.podcasts()[0]["unplayed"], 1)

    def test_episode_track_shape(self):
        show = {"title": "Retro Computing", "author": "The Hosts"}
        ep = {"id": 7, "guid": "ep-2", "title": "Episode 2",
              "url": "https://example.com/ep2.mp3", "duration_ms": 100,
              "size": 5000, "podcast_id": 1}
        t = podcasts.episode_track(show, ep, 1)
        self.assertEqual(t["rating_key"], "podcast:7")
        self.assertEqual(t["genre"], "Podcast")
        self.assertEqual(t["album"], "Retro Computing")
        self.assertEqual(t["track_number"], 1)

    def test_episode_path_extension(self):
        ep = {"guid": "g", "podcast_id": 3, "url": "https://x/e.ogg?tok=1"}
        self.assertTrue(podcasts.episode_path(ep).endswith(".ogg"))
        ep["url"] = "https://x/e"
        self.assertTrue(podcasts.episode_path(ep).endswith(".mp3"))


if __name__ == "__main__":
    unittest.main()
