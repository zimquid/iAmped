"""Read and understand the library *already* on a connected device.

This is the read-only counterpart to syncing: before iAmped copies anything
over, it needs to know what is already there and where each track came from.
That answers two things the rest of the app (and the user) care about:

  * **What's on it** — a full track listing parsed straight from the device's
    own database (the iPod ``iTunesDB``), with metadata, file paths, sizes and
    play counts, independent of Plex.
  * **Provenance** — which tracks iAmped itself put there versus what was
    already on the device or came from another source (iTunes, another tool).
    iAmped records every track it writes in its manifest; anything not in the
    manifest is "foreign" and must be preserved untouched by an additive sync.

Provenance is matched on the on-device **file location**, not the track ID:
Apple's sync agent renumbers track IDs when it rewrites the database, but it
does not move or rename the audio files, so the location is the stable key.
"""
from __future__ import annotations

import os

from .devices import applesync
from .sync import device_state, itunesdb

_AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".aif", ".aiff", ".wav", ".alac", ".m4b"}

ORIGIN_IAMPED = "iamped"
ORIGIN_FOREIGN = "foreign"


def _manifest_index(device_path: str, device_type: str = "ipod") -> dict:
    """Return lookup sets for provenance from iAmped's manifest, keyed by the
    stable on-device location (with track_id as a fallback for older manifests
    written before locations were recorded)."""
    manifest = device_state.read_manifest(device_path, device_type)
    by_location: dict[str, dict] = {}
    by_track_id: dict[int, dict] = {}
    if manifest:
        for t in manifest.get("tracks", []):
            loc = (t.get("location") or "").lstrip(":")
            if loc:
                by_location[loc] = t
            if t.get("track_id") is not None:
                by_track_id[t["track_id"]] = t
    return {"present": bool(manifest), "manifest": manifest,
            "by_location": by_location, "by_track_id": by_track_id}


def _classify(track: dict, idx: dict) -> tuple[str, str | None]:
    """Return (origin, rating_key) for one device track."""
    loc = (track.get("location") or "").lstrip(":")
    hit = idx["by_location"].get(loc)
    if hit is None and track.get("track_id") is not None:
        hit = idx["by_track_id"].get(track["track_id"])
    if hit is not None:
        return ORIGIN_IAMPED, hit.get("rating_key")
    return ORIGIN_FOREIGN, None


def read_ipod_library(device_path: str) -> dict:
    db_path = os.path.join(device_path, itunesdb.ITUNESDB_REL)
    if not os.path.exists(db_path):
        raise RuntimeError("No iTunesDB on this device — it has no iPod library "
                            "to read (is it a classic iPod, and mounted?).")
    with open(db_path, "rb") as fh:
        db = fh.read()
    rows = itunesdb.read_tracks_full(db)
    idx = _manifest_index(device_path, "ipod")

    tracks = []
    counts = {ORIGIN_IAMPED: 0, ORIGIN_FOREIGN: 0}
    total_bytes = total_plays = 0
    for r in rows:
        origin, rating_key = _classify(r, idx)
        counts[origin] += 1
        total_bytes += r.get("size") or 0
        total_plays += r.get("play_count") or 0
        rel = itunesdb.location_to_relpath(r["location"]) if r.get("location") else None
        path = os.path.join(device_path, rel) if rel else None
        tracks.append({
            "track_id": r["track_id"],
            "title": r.get("title"), "artist": r.get("artist"),
            "album": r.get("album"), "genre": r.get("genre"),
            "year": r.get("year") or None,
            "duration_ms": r.get("duration_ms") or 0,
            "size": r.get("size") or 0,
            "play_count": r.get("play_count") or 0,
            "stars": r.get("stars") or 0,
            "location": r.get("location"),
            "path": path,
            "exists": bool(path and os.path.exists(path)),
            "origin": origin,
            "rating_key": rating_key,
        })

    manifest = idx["manifest"] or {}
    return {
        "device_type": "ipod",
        "device_path": device_path,
        "track_count": len(tracks),
        "tracks": tracks,
        "by_origin": counts,
        "total_bytes": total_bytes,
        "total_plays": total_plays,
        "manifest": {
            "present": idx["present"],
            "device_name": manifest.get("device_name"),
            "written_at": manifest.get("written_at"),
            "count": len(manifest.get("tracks", [])),
        },
        "apple_managed": applesync.apple_sync_active(),
    }


def _scan_massstorage(device_path: str) -> dict:
    """Inventory a plain MP3-player / USB volume by walking its audio files.
    Provenance for these comes from iAmped's manifest if present, else foreign."""
    idx = _manifest_index(device_path, "massstorage")
    # Manifest locations are stored colon-joined (iTunesDB form); normalize to
    # OS-relative paths so they compare against os.walk results.
    by_rel = {k.replace(":", os.sep) for k in idx["by_location"]}
    tracks = []
    counts = {ORIGIN_IAMPED: 0, ORIGIN_FOREIGN: 0}
    total_bytes = 0
    for root, _dirs, files in os.walk(device_path):
        if os.path.basename(root).startswith("."):
            continue
        for fn in files:
            if os.path.splitext(fn)[1].lower() not in _AUDIO_EXTS:
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, device_path)
            origin = ORIGIN_IAMPED if rel in by_rel else ORIGIN_FOREIGN
            counts[origin] += 1
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            total_bytes += size
            tracks.append({"title": os.path.splitext(fn)[0], "path": full,
                           "location": rel, "size": size, "exists": True,
                           "origin": origin})
    return {
        "device_type": "massstorage", "device_path": device_path,
        "track_count": len(tracks), "tracks": tracks, "by_origin": counts,
        "total_bytes": total_bytes, "total_plays": 0,
        "manifest": {"present": idx["present"]},
        "apple_managed": {"active": False, "agents": [], "note": "n/a"},
    }


def read_device_library(device_path: str, device_type: str = "ipod") -> dict:
    """Full read-only inventory of whatever is already on a device, with each
    track tagged by provenance (``iamped`` vs ``foreign``)."""
    if not os.path.isdir(device_path):
        raise RuntimeError("Device path does not exist.")
    if device_type == "ipod":
        return read_ipod_library(device_path)
    return _scan_massstorage(device_path)
