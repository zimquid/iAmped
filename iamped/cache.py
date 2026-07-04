"""Download/transcode cache housekeeping.

The sync pipeline caches Plex originals and their transcodes under
``cache_dir`` so re-syncs and in-app playback are instant. Left alone it grows
without bound (a lossless library is tens of GB). This module keeps it bounded:

* **default** — cap the cache at ``cache_limit_mb`` and evict least-recently-used
  audio files past that; wipe the transient audio cache when the app closes.
* **aggressive** (``aggressive_cache``) — keep everything, no cap and no
  clear-on-exit, trading disk for the fastest possible re-syncs.

Only the top-level audio files are managed. Sub-directories (``artwork/`` etc.)
are tiny and cheap to keep, so they're left untouched. Files touched within the
grace window are skipped so an in-flight sync is never pulled out from under.
"""
from __future__ import annotations

import os
import time

from . import config

# Don't evict/clear a file touched within this window — it may belong to a sync
# or a stream that's still running.
_GRACE_SECS = 120


def _cache_dir() -> str:
    return config.load().get("cache_dir", "")


def _audio_files(cache: str) -> list[str]:
    """Top-level cached audio files (originals + transcodes), newest-safe first
    filtering left to the caller. Skips sub-directories like ``artwork/``."""
    out = []
    try:
        entries = os.scandir(cache)
    except OSError:
        return out
    with entries:
        for e in entries:
            try:
                if e.is_file():
                    out.append(e.path)
            except OSError:
                continue
    return out


def _stat(path: str) -> tuple[int, float] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    # atime tracks last read (playback/copy); mtime guards freshly written files.
    return st.st_size, max(st.st_atime, st.st_mtime)


def size_bytes() -> int:
    cache = _cache_dir()
    total = 0
    for p in _audio_files(cache):
        s = _stat(p)
        if s:
            total += s[0]
    return total


def status() -> dict:
    cfg = config.load()
    return {
        "bytes": size_bytes(),
        "limit_mb": int(cfg.get("cache_limit_mb", 3072)),
        "aggressive": bool(cfg.get("aggressive_cache", False)),
        "dir": cfg.get("cache_dir", ""),
    }


def enforce_limit() -> dict:
    """Evict least-recently-used audio files until the cache is under its cap.
    No-op in aggressive mode. Returns ``{removed, freed_bytes}``."""
    cfg = config.load()
    if cfg.get("aggressive_cache"):
        return {"removed": 0, "freed_bytes": 0}
    limit = int(cfg.get("cache_limit_mb", 3072)) * 1024 * 1024
    cache = _cache_dir()
    now = time.time()
    files = []
    total = 0
    for p in _audio_files(cache):
        s = _stat(p)
        if not s:
            continue
        size, used_at = s
        total += size
        if now - used_at > _GRACE_SECS:      # in-flight files are off-limits
            files.append((used_at, size, p))
    if total <= limit:
        return {"removed": 0, "freed_bytes": 0}
    files.sort()                              # oldest access first
    removed = freed = 0
    for _used_at, size, path in files:
        if total <= limit:
            break
        try:
            os.remove(path)
        except OSError:
            continue
        total -= size
        removed += 1
        freed += size
    return {"removed": removed, "freed_bytes": freed}


def clear_transient() -> dict:
    """Wipe cached audio (originals + transcodes) — called on app exit in the
    default (non-aggressive) mode. Leaves sub-directories (artwork) in place."""
    if config.load().get("aggressive_cache"):
        return {"removed": 0, "freed_bytes": 0}
    return clear_all()


def clear_all() -> dict:
    """Remove every top-level cached audio file regardless of mode (manual
    'clear cache now')."""
    cache = _cache_dir()
    now = time.time()
    removed = freed = 0
    for p in _audio_files(cache):
        s = _stat(p)
        if not s:
            continue
        size, used_at = s
        if now - used_at <= _GRACE_SECS:      # never yank an in-flight file
            continue
        try:
            os.remove(p)
        except OSError:
            continue
        removed += 1
        freed += size
    return {"removed": removed, "freed_bytes": freed}
