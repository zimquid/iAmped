"""iAmped — build iPod / MP3-player libraries from Plex, Jellyfin, or Kodi.

The app reads play counts, ratings and playlists from a media server, caches
the metadata locally, downloads the actual audio over the server API, and fills
a target device (classic iPod or USB mass-storage player) to its capacity.

Copyright (C) 2026 Kristopher Heath and iAmped contributors.
Licensed under the GNU General Public License v3.0 or later; see LICENSE.
"""

# Single source of truth for the app version: the PyInstaller spec reads it for
# the macOS bundle, and /api/config reports it to the UI.
__version__ = "1.0.1"
