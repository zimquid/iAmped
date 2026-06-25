"""Read playback stats back off a device and reconcile them into Plex.

Flow:
  build_plan()  -> dry-run: what would change in Plex (plays added, ratings set)
  apply_plan()  -> scrobble/rate to Plex, update the local cache, and (if asked)
                   fold the stats into the device's iTunesDB and clear its
                   Play Counts so the same plays aren't imported twice.

Rating policy is "fill blanks": a device rating is only pushed to Plex when Plex
has no rating for that track; existing ratings are never overwritten.
"""
from __future__ import annotations

import os

from . import plex_client
from .library import Library
from .sync import itunesdb, playcounts, scrobblelog
from .sync.device_state import atomic_write_bytes
from .sync.playcounts import MAC_EPOCH_OFFSET


def _ipod_entries(lib: Library, device_path: str) -> tuple[list[dict], list[str], dict]:
    manifest = itunesdb.read_manifest(device_path)
    if not manifest:
        raise RuntimeError(
            "No iAmped manifest on this iPod — playback can only be mapped back "
            "to Plex for libraries that iAmped synced. Sync to it first.")
    by_tid = {t["track_id"]: t for t in manifest["tracks"]}

    db_path = os.path.join(device_path, itunesdb.ITUNESDB_REL)
    pc_path = os.path.join(device_path, itunesdb.PLAYCOUNTS_REL)
    if not os.path.exists(pc_path):
        return [], [], {"foreign_tracks_ignored": 0, "unmatched": 0}
    with open(db_path, "rb") as fh:
        order = itunesdb.read_tracks(fh.read())
    counts = playcounts.parse(pc_path)

    entries, notes = [], []
    foreign_ignored = 0
    for pc in counts:
        i = pc["index"]
        if i >= len(order):
            break
        tid = order[i]["track_id"]
        m = by_tid.get(tid)
        if not m:
            foreign_ignored += 1
            continue
        if pc["play_count"] == 0 and pc["rating"] == 0:
            continue
        entries.append({
            "rating_key": m["rating_key"], "track_id": tid,
            "title": m.get("title"), "artist": m.get("artist"),
            "plays": pc["play_count"], "device_rating": pc["rating"],
            "last_played_unix": pc["last_played_unix"],
        })
    return entries, notes, {
        "foreign_tracks_ignored": foreign_ignored, "unmatched": 0}


def _scrobble_entries(lib: Library, device_path: str) -> tuple[list[dict], list[str], dict]:
    log = scrobblelog.find_log(device_path)
    if not log:
        raise RuntimeError(
            "No .scrobbler.log on this device. Enable Audioscrobbler logging "
            "(e.g. Rockbox → Settings → Playback → Last.fm Log) and play some tracks.")
    entries, notes, unmatched, unmatched_count = [], [], [], 0
    for rec in scrobblelog.parse(log):
        rk = lib.find_by_meta(rec.get("artist", ""), rec.get("title", ""),
                              rec.get("album", ""))
        if not rk:
            unmatched_count += 1
            if len(unmatched) < 10:
                unmatched.append(
                    f"{rec.get('artist')} – {rec.get('title')}")
            continue
        entries.append({
            "rating_key": rk, "track_id": None,
            "title": rec.get("title"), "artist": rec.get("artist"),
            "plays": rec["play_count"], "device_rating": 0,
            "last_played_unix": rec["last_played_unix"],
        })
    return entries, notes, {
        "foreign_tracks_ignored": 0, "unmatched": unmatched_count,
        "unmatched_examples": unmatched}


def build_plan(lib: Library, device_path: str, device_type: str,
               want_plays: bool = True, want_ratings: bool = True,
               policy: str = "fill_blanks") -> dict:
    if device_type == "ipod":
        entries, notes, diagnostics = _ipod_entries(lib, device_path)
        source = "iPod Play Counts"
    else:
        entries, notes, diagnostics = _scrobble_entries(lib, device_path)
        source = "Audioscrobbler log"

    cur = lib.get_tracks([e["rating_key"] for e in entries])
    plan: list[dict] = []
    for e in entries:
        tr = cur.get(e["rating_key"])
        if not tr:
            notes.append(f"not in local library: {e['title']}")
            continue
        item = {
            "rating_key": e["rating_key"], "track_id": e["track_id"],
            "title": tr.get("title"), "artist": tr.get("artist"),
            "plays_delta": e["plays"] if want_plays else 0,
            "device_rating": e["device_rating"],
            "last_played_unix": e["last_played_unix"],
            "current_rating": tr.get("user_rating"),
            "new_rating": None,
        }
        if want_ratings and e["device_rating"]:
            dev10 = round(e["device_rating"] / 10)        # 0..100 -> 0..10
            if policy == "fill_blanks":
                if not tr.get("user_rating") and dev10:
                    item["new_rating"] = dev10
            elif policy == "device":
                if dev10 != tr.get("user_rating"):
                    item["new_rating"] = dev10
        if item["plays_delta"] or item["new_rating"] is not None:
            plan.append(item)

    return {
        "source": source, "device_type": device_type,
        "plan": plan, "notes": notes,
        "diagnostics": diagnostics,
        "total_plays": sum(i["plays_delta"] for i in plan),
        "total_rating_changes": sum(1 for i in plan if i["new_rating"] is not None),
    }


def apply_plan(server, lib: Library, device_path: str, device_type: str,
               plan: list[dict], reset: bool = True, progress=None) -> dict:
    plays_added = ratings_set = 0
    for n, item in enumerate(plan):
        rk = item["rating_key"]
        for _ in range(int(item.get("plays_delta") or 0)):
            plex_client.scrobble(server, rk)
            plays_added += 1
        if item.get("plays_delta"):
            lib.add_local_plays(rk, item["plays_delta"], item.get("last_played_unix"))
        if item.get("new_rating") is not None:
            plex_client.set_rating(server, rk, item["new_rating"])
            lib.set_local_rating(rk, item["new_rating"])
            ratings_set += 1
        if progress:
            progress(n + 1, len(plan))

    reset_done = False
    if reset:
        reset_done = _reset_device(device_path, device_type, plan)
    return {"plays_added": plays_added, "ratings_set": ratings_set,
            "reset": reset_done}


def _reset_device(device_path: str, device_type: str, plan: list[dict]) -> bool:
    if device_type == "ipod":
        db_path = os.path.join(device_path, itunesdb.ITUNESDB_REL)
        updates = {
            i["track_id"]: {
                "add_plays": i.get("plays_delta") or 0,
                "last_played": (i["last_played_unix"] + MAC_EPOCH_OFFSET)
                if i.get("last_played_unix") else 0,
                "rating": i["device_rating"] or None,
            }
            for i in plan if i.get("track_id") is not None
        }
        with open(db_path, "rb") as fh:
            db = fh.read()
        patched = itunesdb.patch_playcounts(db, updates)
        patched = itunesdb.resign_for_device(device_path, patched)
        atomic_write_bytes(db_path, patched)
        return playcounts.reset_play_counts(device_path)
    # mass-storage: archive the scrobbler log so it isn't re-imported
    log = scrobblelog.find_log(device_path)
    if log:
        os.replace(log, log + ".imported")
        return True
    return False
