# iAmped

Builds a music library for **classic iPods and USB MP3 players** from your
**Plexamp / Plex Media Server** metadata — play counts, ratings and playlists —
then dynamically fills a device to its free capacity.

The machine running iAmped, the Plex server, and the music files can all be in
different places on your network: metadata is read over the Plex HTTP API and
the actual audio is downloaded over the same API, so no file share is required.

## What it does

1. **Sign in with Plex** and choose a discovered Plex Media Server. A server
   URL + token can still be entered manually as an optional fallback.
2. **Build a local library** — caches every track's metadata (plays, rating,
   format, size, duration) and all audio playlists into `~/.iamped/library.db`.
3. **Pick a target device** — a classic iPod or a USB mass-storage player. The
   app reads the device's *actual free space*.
4. **Choose content** — select Plex playlists to include and a fill strategy
   (most-played / top-rated / recently-played / random / alphabetical) for the
   remaining space, or select songs in the library and drag them directly onto
   an iPod/MP3 player in the sidebar.
5. **Review** each addition, update, and removal, then **sync** — audio is
   downloaded (and cached for reuse), optionally transcoded, and written to the
   device.

Downloaded audio is cached under `~/.iamped/cache/`, so filling a second device
reuses files instead of re-downloading.

### Incremental mirror and recovery

With **Mirror this selection** enabled, iAmped compares the desired Plex
selection with the files it previously wrote to that device:

- unchanged tracks are kept without copying;
- new or changed Plex tracks are added/updated;
- stale iAmped-managed tracks and playlists are removed;
- files from iTunes, another sync tool, or manual copies are preserved.

Device copies, playlists, manifests and `iTunesDB` updates are published through
temporary files and atomic replacement. A transaction journal is checkpointed
after each copied track. If a sync is interrupted, reconnect the same device and
run the same preview/sync again; verified completed files are reused and pending
cleanup resumes. iAmped rejects a different sync plan while an interrupted
transaction is pending rather than guessing which files are safe to remove.

Before a modifying sync, iAmped creates a device-local rollback snapshot. The
device screen can restore a prior manifest, database/playlists, removed audio,
and native iPod artwork. Writes and readback resets use a per-device lock, and
the device can be safely ejected from the same screen.

### Artwork, profiles, matching, and playback

- Plex album artwork is cached once per album. USB players receive embedded
  cover tags plus `cover.jpg`; supported iPods receive native ArtworkDB/ITHMB
  thumbnails.
- Each device has a stable identity and saved profile for reserve space, fill
  strategy, playlists, transcoding, artwork, and mirror behavior.
- Files without Plex IDs can be reconciled conservatively using normalized
  tags, duration, and optional Chromaprint (`fpcalc`) confirmation. Matches are
  informational and never make foreign files eligible for automatic removal.
- Device readback summarizes foreign/unmatched entries instead of flooding the
  review with one warning per track.
- The View menu includes an audioMotion status visualizer and a Butterchurn full-screen visualizer, both vendored under `iamped/web/vendor/`.

## Device support

| Target | How it's written | Notes |
|---|---|---|
| **USB MP3 player** | `Music/Artist/Album/…` tree + `.m3u8` playlists | Works on virtually any player; also ideal for iPods running **Rockbox**. |
| **Classic iPod** | native `iTunesDB` | iPod 1G–5.5G (incl. Video), mini, nano 1G–3G. |

> **Not supported for native iPod mode:** iPod Classic 6G/7G and nano 6G+.
> Those require Apple's proprietary `hashAB` checksum, which was never fully
> reverse-engineered. Use **USB mode + Rockbox** for those models. iAmped backs
> up any existing `iTunesDB` (to `iTunesDB.iamped.bak`) before writing, and the
> native writer is verified for structure but should be treated as experimental
> on first use against real hardware.

## Requirements

- Python 3.10+
- `ffmpeg` on `PATH` (optional — only for transcoding lossless files to MP3)
- `fpcalc`/Chromaprint on `PATH` (optional — strengthens ambiguous file matches)

## Run

```bash
./run.sh
```

First launch creates a virtualenv and installs dependencies, then opens a
desktop window. To run headless / in a browser instead:

```bash
./run.sh --no-window
```

## Build a portable app

To produce a standalone, double-clickable app (no Python needed on the target
machine):

```bash
./build_linux_mac.sh        # macOS -> dist/iAmped.app,  Linux -> dist/iAmped
build_windows.bat           # Windows -> dist\iAmped.exe
```

The build is **portable**: your settings (Plex URL + token), the library cache
and downloaded audio are stored in an `iAmped-data/` folder created **next to
the app** on first run — so you can drop the app on a USB stick and carry your
configuration with it.

### Where settings are stored

| How you run it | Data directory |
|---|---|
| `./run.sh` (source) | `~/.iamped/` |
| Portable build | `iAmped-data/` next to the app/exe |
| `IAMPED_HOME=/path` set | exactly that path |

`config.json` holds the Plex connection details and your last-used sync
settings; they persist automatically once entered in the UI (or saved via the
Connect step).

## Manual Plex connection (optional)

The recommended setup is **Sign in with Plex**, which discovers your servers
and stores the selected server URL and access token automatically. To connect
manually instead, enter both the Plex Media Server URL and an
`X-Plex-Token`.

## Project layout

```
iamped/
  plex_client.py     Plex API: metadata, playlists, downloads
  library.py         SQLite metadata cache (the reusable pool)
  filler.py          capacity-aware track selection
  artwork.py         Plex artwork cache + portable tag embedding
  matcher.py         metadata/Chromaprint device reconciliation
  device_management.py profiles, locks, rollback, safe eject
  sync/
    massstorage.py   USB player: file tree + M3U8
    itunesdb.py      classic iPod: pure-Python iTunesDB writer
    ipod_artwork.py  native ArtworkDB + ITHMB writer
  server.py          Flask REST API + background jobs
  app.py             desktop window (pywebview) launcher
  web/               single-page UI
```
