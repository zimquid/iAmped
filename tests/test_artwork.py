from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from iamped import artwork, config, plex_client


class ArtworkCacheTests(unittest.TestCase):
    def test_materialize_reuses_existing_valid_cache_file(self):
        track = {"album_key": "album-1", "album_thumb": "/library/thumb/1"}
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td) / "cache"
            with patch.object(config, "load",
                              return_value={"cache_dir": str(cache_dir)}):
                cached = Path(artwork.cache_path(track))
                cached.parent.mkdir(parents=True)
                Image.new("RGB", (64, 64), (30, 100, 220)).save(
                    cached, "JPEG")

                with patch.object(plex_client, "download_image") as download:
                    result = artwork.materialize(object(), track)

        self.assertEqual(result, str(cached))
        download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
