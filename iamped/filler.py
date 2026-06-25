"""Pick a set of tracks that fits a device's free capacity.

Strategy: selected Plex playlists are honoured first (they're the explicit
intent), then the remaining space is filled by the chosen ranking
(most-played, top-rated, recent, random).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

from .library import Library

LOSSLESS = {"flac", "alac", "wav", "aiff", "aif", "ape", "wv"}
TARGET_BITRATE = {"aac": 256_000, "mp3": 320_000}   # bits/sec per device format


def _norm(s: str | None) -> str:
    """Loose normalization for fuzzy track matching: fold unicode, lowercase,
    drop punctuation (so curly vs straight apostrophes, ``feat.`` vs ``feat``
    collapse), and squeeze whitespace."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def match_key(artist: str | None, title: str | None) -> str:
    """Stable (artist, title) identity used to tell whether a track is already
    on a device, independent of album, container or source. Deliberately
    album-agnostic so the same song on a single and an album dedupes."""
    return _norm(artist) + "\x00" + _norm(title)


def is_lossless(track: dict) -> bool:
    return (track.get("container") or "").lower() in LOSSLESS \
        or (track.get("codec") or "").lower() in LOSSLESS


def device_size(track: dict, transcode_lossless: bool,
                target_format: str = "mp3") -> int:
    """Estimated bytes the track will occupy on the device."""
    if track.get("cached_size") and not (transcode_lossless and is_lossless(track)):
        return int(track["cached_size"])
    if transcode_lossless and is_lossless(track):
        secs = (track.get("duration_ms") or 0) / 1000.0
        bitrate = TARGET_BITRATE.get(target_format, 320_000)
        # +5% container overhead, minimum a sane floor
        return max(int(secs * bitrate / 8 * 1.05), 256_000)
    return int(track.get("file_size") or 0)


def bound_tracks(tracks: list[dict], max_tracks: Optional[int] = None,
                 max_bytes: Optional[int] = None, transcode_lossless: bool = True,
                 target_format: str = "aac") -> dict:
    """Trim an ordered track list to fit a song-count and/or size budget — used
    to size a generated radio/playlist to an iPod (or a portion of one).

    Stops at whichever limit is hit first. A track too big for the remaining
    size budget is skipped (later, smaller tracks may still fit). Returns
    ``{tracks, total_bytes, dropped}``.
    """
    out: list[dict] = []
    used = 0
    dropped = 0
    for t in tracks:
        if max_tracks is not None and len(out) >= max_tracks:
            dropped += 1
            continue
        sz = device_size(t, transcode_lossless, target_format)
        if max_bytes is not None and used + sz > max_bytes:
            dropped += 1
            continue
        out.append(t)
        used += sz
    return {"tracks": out, "total_bytes": used, "dropped": dropped}


def explicit_plan(lib: Library, rating_keys: list[str], capacity_bytes: int,
                  reserve_bytes: int, transcode_lossless: bool,
                  target_format: str = "mp3") -> dict:
    """Plan an iTunes-style drag/drop selection in the supplied row order."""
    rows = lib.get_tracks(rating_keys)
    ordered = [rows[k] for k in rating_keys if k in rows]
    bounded = bound_tracks(
        ordered, max_bytes=max(capacity_bytes - reserve_bytes, 0),
        transcode_lossless=transcode_lossless, target_format=target_format)
    selected = []
    for track in bounded["tracks"]:
        item = dict(track)
        item["_size"] = device_size(item, transcode_lossless, target_format)
        selected.append(item)
    return {
        "tracks": selected,
        "playlists": [],
        "total_bytes": bounded["total_bytes"],
        "track_count": len(selected),
        "budget_bytes": max(capacity_bytes - reserve_bytes, 0),
        "capacity_bytes": capacity_bytes,
        "reserve_bytes": reserve_bytes,
        "skipped_for_space": bounded["dropped"],
        "skipped_present": 0,
        "transcode_lossless": transcode_lossless,
        "fill_strategy": "manual",
    }


def plan(lib: Library, capacity_bytes: int, reserve_bytes: int,
         fill_strategy: str, include_playlist_ids: list[str],
         transcode_lossless: bool, max_tracks: Optional[int] = None,
         target_format: str = "mp3",
         exclude_keys: Optional[set[str]] = None,
         exclude_meta: Optional[set[str]] = None) -> dict:
    budget = max(capacity_bytes - reserve_bytes, 0)
    used = 0
    chosen: dict[str, dict] = {}          # rating_key -> track row (+ _size)
    order: list[str] = []                 # rating_keys in selection order
    skipped_for_space = 0
    skipped_present = 0
    exclude_keys = exclude_keys or set()  # rating_keys already iAmped-written
    exclude_meta = exclude_meta or set()  # (artist,title) already on the device

    def try_add(track: dict) -> bool:
        nonlocal used, skipped_for_space, skipped_present
        rk = track["rating_key"]
        if rk in chosen:
            return True
        # Already on the device — by exact Plex id (iAmped wrote it) or by
        # artist/title match (any source, incl. iTunes). Don't duplicate it.
        if rk in exclude_keys or \
                match_key(track.get("artist"), track.get("title")) in exclude_meta:
            skipped_present += 1
            return False
        sz = device_size(track, transcode_lossless, target_format)
        if sz <= 0:
            return False
        if used + sz > budget:
            skipped_for_space += 1
            return False
        if max_tracks and len(chosen) >= max_tracks:
            return False
        track = dict(track)
        track["_size"] = sz
        chosen[rk] = track
        order.append(rk)
        used += sz
        return True

    # 1) selected playlists first, in playlist order
    planned_playlists: list[dict] = []
    all_pls = lib.all_playlists()
    for pid in include_playlist_ids:
        keys = lib.playlist_track_keys_any(pid)
        rows = lib.get_tracks(keys)
        present: list[str] = []
        for rk in keys:
            tr = rows.get(rk)
            if tr and try_add(tr):
                present.append(rk)
        meta = next((p for p in all_pls if p["id"] == pid), None)
        planned_playlists.append({
            "plex_id": pid,
            "title": meta["title"] if meta else pid,
            "track_keys": present,
            "requested": len(keys),
        })

    # 2) fill the rest by ranking
    if not max_tracks or len(chosen) < max_tracks:
        for tr in lib.ordered_tracks(fill_strategy):
            if used >= budget:
                break
            try_add(tr)

    selected = [chosen[rk] for rk in order]
    return {
        "tracks": selected,
        "playlists": planned_playlists,
        "total_bytes": used,
        "track_count": len(selected),
        "budget_bytes": budget,
        "capacity_bytes": capacity_bytes,
        "reserve_bytes": reserve_bytes,
        "skipped_for_space": skipped_for_space,
        "skipped_present": skipped_present,
        "transcode_lossless": transcode_lossless,
        "fill_strategy": fill_strategy,
    }
