import unittest
from types import SimpleNamespace

from iamped import plex_client


class PlexClientProgressTests(unittest.TestCase):
    def _track(self, key):
        part = SimpleNamespace(key=f"/parts/{key}", container="mp3",
                               size=1234, file=f"/music/{key}.mp3")
        media = SimpleNamespace(parts=[part], container="mp3",
                                audioCodec="mp3", bitrate=320)
        return SimpleNamespace(
            ratingKey=str(key),
            title=f"Track {key}",
            grandparentTitle="Artist",
            parentTitle="Album",
            parentRatingKey="album-1",
            parentThumb="",
            parentYear=2024,
            index=1,
            parentIndex=1,
            duration=180000,
            viewCount=0,
            userRating=None,
            lastViewedAt=None,
            media=[media],
        )

    def test_iter_tracks_reports_track_query_total_across_pages(self):
        class Page(list):
            totalSize = 3

        class Section:
            def __init__(self, pages):
                self.pages = pages
                self.starts = []

            def searchTracks(self, **kwargs):
                self.starts.append(kwargs["container_start"])
                return self.pages.get(kwargs["container_start"], Page())

        section = Section({
            0: Page([self._track(1), self._track(2)]),
            2: Page([self._track(3)]),
        })
        server = SimpleNamespace(
            library=SimpleNamespace(section=lambda title: section))
        progress = []

        tracks = list(plex_client.iter_tracks(
            server, "Music", lambda done, total: progress.append((done, total))))

        self.assertEqual([t.rating_key for t in tracks], ["1", "2", "3"])
        self.assertEqual(section.starts, [0, 2])
        self.assertEqual(progress[0], (0, 3))
        self.assertEqual(progress[-1], (3, 3))


if __name__ == "__main__":
    unittest.main()
