"""Read the classic iPod On-The-Go playlist files.

Between syncs the iPod stores the On-The-Go queue (and, on older firmware,
each saved OTG list) in ``iPod_Control/iTunes/OTGPlaylistInfo*`` files. Like
``Play Counts``, entries are positional: each is a u32 index into the
iTunesDB's track list, so resolving them needs the device's current database.

Format (little-endian): header ``mhpo``, header length, entry length (4),
entry count, then ``count`` u32 track indices.

iTunes imports these as playlists on sync and deletes the files; ingesting them
into a Plex/local playlist and clearing the files reproduces that behavior.
"""
from __future__ import annotations

import glob
import os
import struct

OTG_DIR_REL = os.path.join("iPod_Control", "iTunes")
OTG_GLOB = "OTGPlaylistInfo*"


def find_files(device_path: str) -> list[str]:
    return sorted(glob.glob(
        os.path.join(device_path, OTG_DIR_REL, OTG_GLOB)))


def parse(path: str) -> list[int]:
    """Track-list indices in the playlist's play order."""
    with open(path, "rb") as fh:
        data = fh.read()
    if data[0:4] != b"mhpo":
        raise ValueError("not an On-The-Go playlist file (missing mhpo header)")
    header_len = struct.unpack_from("<I", data, 4)[0]
    entry_len = struct.unpack_from("<I", data, 8)[0] or 4
    count = struct.unpack_from("<I", data, 0x0C)[0]
    out: list[int] = []
    off = header_len
    for _ in range(count):
        if off + 4 > len(data):
            break
        out.append(struct.unpack_from("<I", data, off)[0])
        off += entry_len
    return out


def build_plans(lib, device_path: str) -> list[dict]:
    """Resolve every OTG file on the device against the iTunesDB and the
    iAmped manifest. Each plan carries the tracks that mapped to Plex
    rating keys plus the ones that could only be matched (or not) by tags."""
    from . import itunesdb

    files = find_files(device_path)
    if not files:
        return []
    db_path = os.path.join(device_path, itunesdb.ITUNESDB_REL)
    with open(db_path, "rb") as fh:
        db = fh.read()
    order = itunesdb.read_tracks_full(db)
    manifest = itunesdb.read_manifest(device_path) or {"tracks": []}
    by_tid = {t["track_id"]: t for t in manifest.get("tracks", [])}

    plans = []
    for n, path in enumerate(files, start=1):
        try:
            indices = parse(path)
        except (OSError, ValueError):
            continue
        tracks, unmatched = [], 0
        for i in indices:
            if i >= len(order):
                continue
            row = order[i]
            managed = by_tid.get(row["track_id"])
            rating_key = managed.get("rating_key") if managed else None
            if not rating_key:
                # foreign track — try a conservative tag match into the library
                rating_key = lib.find_by_meta(
                    row.get("artist") or "", row.get("title") or "",
                    row.get("album") or "")
            if not rating_key:
                unmatched += 1
            tracks.append({
                "rating_key": rating_key,
                "title": row.get("title"),
                "artist": row.get("artist"),
                "album": row.get("album"),
            })
        if tracks:
            plans.append({
                "file": os.path.basename(path),
                "title": f"On-The-Go {n}" if len(files) > 1 else "On-The-Go",
                "tracks": tracks,
                "matched": sum(1 for t in tracks if t["rating_key"]),
                "unmatched": unmatched,
            })
    return plans


def clear(device_path: str) -> int:
    """Delete the OTG files (what iTunes does after importing them). The iPod
    rebuilds them as the user queues tracks. Returns how many were removed."""
    removed = 0
    for path in find_files(device_path):
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass
    return removed
