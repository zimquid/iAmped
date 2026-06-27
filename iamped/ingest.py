"""Ingest foreign music from a device back into the Plex server.

This is the inverse of syncing: tracks that are on a device but *not* in Plex
(old iPods, files from other tools) can be folded back into the library so all
music lives on the server. Plex has no HTTP media-upload API, so the flow is:

  1. plan()  -> classify each selected device track as already-in-Plex (skip,
                Plex's copy wins) or to-ingest (no confident match).
  2. apply() -> copy each to-ingest file into a Plex-watched folder, trigger a
                path-scoped scan, poll until Plex confirms the track exists, and
                only then remove the confirmed file from the device. A file Plex
                never picks up is left on the device and reported, so nothing is
                lost.

Existing Plex files are never overwritten or deleted; duplicates are skipped.
"""
from __future__ import annotations

import os
import re
import shutil
import time

from . import inventory, matcher, plex_client
from .library import Library

MATCH_THRESHOLD = 0.88        # same bar the device-matches UI uses for "confident"


def _enrich(lib: Library, track: dict) -> dict:
    """Fill in artist/title/album from the file's own tags where the device DB
    is thin (mass-storage rows only know the filename), so we can place the file
    sensibly and verify it after the scan."""
    meta = matcher._metadata(track.get("path") or "", track)
    return {
        "title": meta.get("title") or track.get("title"),
        "artist": meta.get("artist") or track.get("artist"),
        "album": meta.get("album") or track.get("album"),
    }


def _select(tracks: list[dict], device_type: str,
            track_ids: set | None, locations: set | None) -> list[dict]:
    out = []
    for t in tracks:
        if t.get("media") == "video":
            continue
        if device_type == "ipod":
            if track_ids is not None and t.get("track_id") not in track_ids:
                continue
        else:
            if locations is not None and t.get("location") not in locations:
                continue
        out.append(t)
    return out


def plan(lib: Library, device_path: str, device_type: str,
         track_ids: list | None = None, locations: list | None = None) -> dict:
    inv = inventory.read_device_library(device_path, device_type)
    tid = {int(i) for i in track_ids} if track_ids is not None else None
    locs = set(locations) if locations is not None else None
    selected = _select(inv.get("tracks", []), device_type, tid, locs)

    items = []
    for t in selected:
        tags = _enrich(lib, t)
        m = matcher.match_track(lib, t)
        exists = bool(m.get("rating_key")) and m.get("confidence", 0) >= MATCH_THRESHOLD
        items.append({
            "title": tags["title"], "artist": tags["artist"], "album": tags["album"],
            "size": t.get("size") or 0,
            "track_id": t.get("track_id"), "location": t.get("location"),
            "path": t.get("path"), "origin": t.get("origin"),
            "status": "skip_exists" if exists else "ingest",
            "match": ({"title": m.get("title"), "artist": m.get("artist"),
                       "confidence": m.get("confidence")} if exists else None),
        })
    to_ingest = [i for i in items if i["status"] == "ingest"]
    return {
        "device_type": device_type,
        "items": items,
        "ingest_count": len(to_ingest),
        "skip_count": len(items) - len(to_ingest),
        "ingest_bytes": sum(i["size"] for i in to_ingest),
    }


def _sanitize(value: str | None, fallback: str) -> str:
    value = (value or "").strip() or fallback
    value = re.sub(r'[/\\:*?"<>|\x00]', "_", value)
    return value[:120].rstrip(". ") or fallback


def _dest_path(ingest_dir: str, src: str, item: dict) -> str:
    artist = _sanitize(item.get("artist"), "Unknown Artist")
    album = _sanitize(item.get("album"), "Unknown Album")
    ext = os.path.splitext(src)[1] or ".mp3"
    title = _sanitize(item.get("title"),
                      os.path.splitext(os.path.basename(src))[0] or "track")
    return os.path.join(ingest_dir, artist, album, f"{title}{ext}")


def apply(server, lib: Library, device_path: str, device_type: str,
          items: list[dict], ingest_dir: str, section: str,
          progress=None, batch_remove=None,
          poll_interval: float = 5.0, max_polls: int = 12) -> dict:
    to_ingest = [i for i in items if i.get("status") == "ingest"]
    skipped_existing = sum(1 for i in items if i.get("status") == "skip_exists")
    os.makedirs(ingest_dir, exist_ok=True)

    copied, copy_errors = [], []
    for n, it in enumerate(to_ingest):
        src = it.get("path")
        if not src or not os.path.isfile(src):
            copy_errors.append({"title": it.get("title"),
                                "reason": "file not found on device"})
            continue
        dest = _dest_path(ingest_dir, src, it)
        try:
            if not os.path.exists(dest):
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                tmp = dest + ".part"
                shutil.copy2(src, tmp)
                os.replace(tmp, dest)
            copied.append({**it, "dest": dest})
        except OSError as exc:
            copy_errors.append({"title": it.get("title"), "reason": str(exc)})
        if progress:
            progress("copying", n + 1, len(to_ingest))

    confirmed, pending = [], list(copied)
    if copied:
        plex_client.scan_path(server, section, ingest_dir)
        for _ in range(max_polls):
            if not pending:
                break
            time.sleep(poll_interval)
            still = []
            for it in pending:
                if plex_client.track_in_library(
                        server, section, it.get("artist") or "", it.get("title") or ""):
                    confirmed.append(it)
                else:
                    still.append(it)
            pending = still
            if progress:
                progress("verifying", len(confirmed), len(copied))

    removed = freed = 0
    if confirmed and batch_remove:
        res = batch_remove(confirmed) or {}
        removed, freed = res.get("removed", 0), res.get("freed_bytes", 0)

    return {
        "ingested": len(copied),
        "confirmed": len(confirmed),
        "removed_from_device": removed,
        "freed_bytes": freed,
        "skipped_existing": skipped_existing,
        "unconfirmed": [
            {"title": i.get("title"), "artist": i.get("artist")} for i in pending],
        "copy_errors": copy_errors,
    }
