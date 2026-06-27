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
BITRATE_PRESETS = {
    "aac": [64, 96, 128, 160, 192, 256],
    "mp3": [96, 128, 160, 192, 256, 320],
}


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


# Words that mark a *release variant* of an otherwise-identical recording. The
# same song pulled from a different album/single almost always differs only by
# one of these tags ("(2009 Remaster)", "- Live", "(Radio Edit)"), so radio/
# station dedup must fold them out or the same song reappears from each album.
_VARIANT_WORDS = (
    r"remaster(?:ed)?|remix|re[- ]?recorded|re[- ]?rec|mono|stereo|"
    r"radio edit|radio version|single version|album version|original version|"
    r"extended(?: version| mix)?|deluxe|bonus(?: track)?|anniversary|"
    r"explicit|clean|edit|edited|version|live(?: at .*| from .*| in .*)?|"
    r"acoustic|instrumental|demo|session|reprise|alternate(?: take| version)?|"
    r"take \d+|mix|expanded"
)
# Parenthetical/bracketed group that is *only* a variant tag (optionally with a
# leading year, e.g. "(2011 Remastered)"), or a trailing " - <variant>" segment.
_VARIANT_PAREN = re.compile(
    rf"[\(\[]\s*(?:\d{{4}}\s+)?(?:{_VARIANT_WORDS})\b[^\)\]]*[\)\]]",
    re.IGNORECASE)
_VARIANT_DASH = re.compile(
    rf"\s+-\s+(?:\d{{4}}\s+)?(?:{_VARIANT_WORDS})\b.*$", re.IGNORECASE)
# A "feat./featuring/with" credit — folded out of both artist and title so the
# album cut and the "(feat. X)" single collapse to one song.
_FEAT = re.compile(
    r"[\(\[]?\s*(?:feat\.?|featuring|ft\.?|with)\s+[^\)\]]*[\)\]]?\s*",
    re.IGNORECASE)


def _strip_variants(title: str | None) -> str:
    """Drop release-variant tags (remaster/live/radio-edit/feat./…) from a title
    so the same recording on different albums collapses to one identity."""
    s = title or ""
    s = _VARIANT_PAREN.sub(" ", s)
    s = _VARIANT_DASH.sub(" ", s)
    s = _FEAT.sub(" ", s)
    return s


def radio_key(artist: str | None, title: str | None) -> str:
    """Aggressive song identity for radio/station/playlist dedup: like
    :func:`match_key` but also folds out release-variant tags and feat. credits,
    so a song doesn't reappear once per album/remaster/edit it lives on.

    Kept separate from match_key (which gates device-presence and must not treat
    a remaster as already-present) so the looser folding stays scoped to dedup.
    The stripped title can go empty (a title that is *only* a tag, e.g.
    "(Live)") — fall back to the raw normalized title so it never collapses to
    the artist alone."""
    stripped = _norm(_strip_variants(title))
    if not stripped:
        stripped = _norm(title)
    return _norm(_FEAT.sub(" ", artist or "")) + "\x00" + stripped


def is_lossless(track: dict) -> bool:
    return (track.get("container") or "").lower() in LOSSLESS \
        or (track.get("codec") or "").lower() in LOSSLESS


def should_transcode(track: dict, transcode_enabled: bool,
                     target_bitrate_k: int | None = None) -> bool:
    """Whether reducing/normalizing this source can change its device size."""
    if not transcode_enabled:
        return False
    if is_lossless(track):
        return True
    source_bitrate = int(track.get("bitrate") or 0)
    return bool(target_bitrate_k and source_bitrate > target_bitrate_k)


def device_size(track: dict, transcode_lossless: bool,
                target_format: str = "mp3",
                target_bitrate_k: int | None = None) -> int:
    """Estimated bytes the track will occupy on the device."""
    target_bitrate_k = target_bitrate_k or TARGET_BITRATE.get(
        target_format, 320_000) // 1000
    converting = should_transcode(
        track, transcode_lossless, target_bitrate_k)
    if track.get("cached_size") and not converting:
        return int(track["cached_size"])
    if converting:
        secs = (track.get("duration_ms") or 0) / 1000.0
        bitrate = target_bitrate_k * 1000
        # +5% container overhead, minimum a sane floor
        return max(int(secs * bitrate / 8 * 1.05), 256_000)
    return int(track.get("file_size") or 0)


def bound_tracks(tracks: list[dict], max_tracks: Optional[int] = None,
                 max_bytes: Optional[int] = None, transcode_lossless: bool = True,
                 target_format: str = "aac",
                 target_bitrate_k: int | None = None) -> dict:
    """Trim an ordered track list to fit a song-count and/or size budget — used
    to size a generated radio/playlist to an iPod (or a portion of one).

    Stops at whichever limit is hit first. A track too big for the remaining
    size budget is skipped (later, smaller tracks may still fit). Returns
    ``{tracks, total_bytes, dropped}``.
    """
    out: list[dict] = []
    used = 0
    dropped = 0
    dropped_for_limit = 0
    for t in tracks:
        if max_tracks is not None and len(out) >= max_tracks:
            dropped += 1
            dropped_for_limit += 1
            continue
        sz = device_size(
            t, transcode_lossless, target_format, target_bitrate_k)
        if max_bytes is not None and used + sz > max_bytes:
            dropped += 1
            continue
        out.append(t)
        used += sz
    return {
        "tracks": out, "total_bytes": used, "dropped": dropped,
        "dropped_for_limit": dropped_for_limit,
    }


def explicit_plan(lib: Library, rating_keys: list[str], capacity_bytes: int,
                  reserve_bytes: int, transcode_lossless: bool,
                  target_format: str = "mp3",
                  target_bitrate_k: int | None = None,
                  max_tracks: Optional[int] = None) -> dict:
    """Plan an iTunes-style drag/drop selection in the supplied row order."""
    rows = lib.get_tracks(rating_keys)
    ordered = [rows[k] for k in rating_keys if k in rows]
    requested_bytes = sum(device_size(
        t, transcode_lossless, target_format, target_bitrate_k)
        for t in ordered)
    bounded = bound_tracks(
        ordered, max_tracks=max_tracks,
        max_bytes=max(capacity_bytes - reserve_bytes, 0),
        transcode_lossless=transcode_lossless, target_format=target_format,
        target_bitrate_k=target_bitrate_k)
    selected = []
    for track in bounded["tracks"]:
        item = dict(track)
        item["_size"] = device_size(
            item, transcode_lossless, target_format, target_bitrate_k)
        selected.append(item)
    return {
        "tracks": selected,
        "playlists": [],
        "total_bytes": bounded["total_bytes"],
        "track_count": len(selected),
        "budget_bytes": max(capacity_bytes - reserve_bytes, 0),
        "capacity_bytes": capacity_bytes,
        "reserve_bytes": reserve_bytes,
        "skipped_for_space": (
            bounded["dropped"] - bounded["dropped_for_limit"]),
        "skipped_for_limit": bounded["dropped_for_limit"],
        "requested_track_count": len(ordered),
        "requested_bytes": requested_bytes,
        "skipped_present": 0,
        "transcode_lossless": transcode_lossless,
        "target_bitrate_k": target_bitrate_k,
        "fill_strategy": "manual",
    }


def plan(lib: Library, capacity_bytes: int, reserve_bytes: int,
         fill_strategy: str, include_playlist_ids: list[str],
         transcode_lossless: bool, max_tracks: Optional[int] = None,
         target_format: str = "mp3",
         exclude_keys: Optional[set[str]] = None,
         exclude_meta: Optional[set[str]] = None,
         target_bitrate_k: int | None = None,
         fill_remaining: bool = True) -> dict:
    budget = max(capacity_bytes - reserve_bytes, 0)
    used = 0
    chosen: dict[str, dict] = {}          # rating_key -> track row (+ _size)
    order: list[str] = []                 # rating_keys in selection order
    skipped_for_space = 0
    skipped_for_limit = 0
    skipped_present = 0
    requested_track_count = 0
    requested_bytes = 0
    exclude_keys = exclude_keys or set()  # rating_keys already iAmped-written
    exclude_meta = exclude_meta or set()  # (artist,title) already on the device

    def try_add(track: dict) -> bool:
        nonlocal used, skipped_for_space, skipped_for_limit, skipped_present
        nonlocal requested_track_count, requested_bytes
        rk = track["rating_key"]
        if rk in chosen:
            return True
        # Already on the device — by exact Plex id (iAmped wrote it) or by
        # artist/title match (any source, incl. iTunes). Don't duplicate it.
        if rk in exclude_keys or \
                match_key(track.get("artist"), track.get("title")) in exclude_meta:
            skipped_present += 1
            return False
        sz = device_size(
            track, transcode_lossless, target_format, target_bitrate_k)
        if sz <= 0:
            return False
        requested_track_count += 1
        requested_bytes += sz
        if used + sz > budget:
            skipped_for_space += 1
            return False
        if max_tracks and len(chosen) >= max_tracks:
            skipped_for_limit += 1
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
    if fill_remaining and (not max_tracks or len(chosen) < max_tracks):
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
        "skipped_for_limit": skipped_for_limit,
        "requested_track_count": requested_track_count,
        "requested_bytes": requested_bytes,
        "skipped_present": skipped_present,
        "transcode_lossless": transcode_lossless,
        "target_bitrate_k": target_bitrate_k,
        "fill_strategy": fill_strategy,
    }
