import unittest
from types import SimpleNamespace
from xml.etree import ElementTree as ET

from iamped import plex_client


def _track_elem(key):
    tr = ET.Element("Track", {
        "ratingKey": str(key),
        "title": f"Track {key}",
        "grandparentTitle": "Artist",
        "parentTitle": "Album",
        "parentRatingKey": "album-1",
        "parentThumb": "",
        "parentYear": "2024",
        "index": "1",
        "parentIndex": "1",
        "duration": "180000",
        "viewCount": "0",
    })
    media = ET.SubElement(tr, "Media", {
        "container": "mp3", "audioCodec": "mp3", "bitrate": "320"})
    ET.SubElement(media, "Part", {
        "key": f"/parts/{key}", "container": "mp3",
        "size": "1234", "file": f"/music/{key}.mp3"})
    return tr


def _container(total, tracks):
    mc = ET.Element("MediaContainer", {"totalSize": str(total)})
    for t in tracks:
        mc.append(t)
    return mc


class PlexClientProgressTests(unittest.TestCase):
    def test_iter_tracks_pages_and_reports_total(self):
        pages = {
            0: [_track_elem(1), _track_elem(2)],
            2: [_track_elem(3)],
        }
        starts = []

        def query(ekey, params=None, headers=None):
            start = int(headers["X-Plex-Container-Start"])
            starts.append(start)
            return _container(3, pages.get(start, []))

        server = SimpleNamespace(
            query=query,
            library=SimpleNamespace(
                section=lambda title: SimpleNamespace(key=13)))
        progress = []

        tracks = list(plex_client.iter_tracks(
            server, "Music", lambda done, total: progress.append((done, total))))

        self.assertEqual([t.rating_key for t in tracks], ["1", "2", "3"])
        self.assertEqual(starts, [0, 2])
        self.assertEqual(progress[0], (0, 3))
        self.assertEqual(progress[-1], (3, 3))

    def test_iter_tracks_preserves_album_year(self):
        def query(ekey, params=None, headers=None):
            start = int(headers["X-Plex-Container-Start"])
            return _container(1, [_track_elem(1)] if start == 0 else [])

        server = SimpleNamespace(
            query=query,
            library=SimpleNamespace(
                section=lambda title: SimpleNamespace(key=13)))

        (track,) = list(plex_client.iter_tracks(server, "Music"))
        self.assertEqual(track.year, 2024)
        self.assertEqual(track.container, "mp3")
        self.assertEqual(track.bitrate, 320)
        self.assertEqual(track.file_size, 1234)


if __name__ == "__main__":
    unittest.main()
