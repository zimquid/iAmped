"""Podcast subscription routes and the podcast → device sync job."""
from __future__ import annotations

import os
import time

from flask import Blueprint, jsonify, request

from .. import device_management, podcasts
from ..server import _lib, start_job
from ..sync import BACKENDS
from ..sync.base import have_ffmpeg, transcode_to_mp3

bp = Blueprint("podcasts", __name__)

# Formats classic iPods (and most USB players) actually decode; anything else
# gets transcoded to MP3 before it goes on the device.
_DEVICE_SAFE = {".mp3", ".m4a", ".aac"}


@bp.get("/api/podcasts")
def api_podcasts():
    return jsonify({"podcasts": _lib().podcasts()})


@bp.post("/api/podcasts")
def api_podcasts_add():
    url = ((request.json or {}).get("url") or "").strip()
    if not url:
        return jsonify({"error": "Enter a podcast RSS URL."}), 400
    try:
        res = podcasts.refresh(_lib(), url, int(time.time()))
    except Exception as exc:  # noqa: BLE001 - bad URL / not a feed
        return jsonify({"error": f"Could not read that feed: {exc}"}), 400
    return jsonify({"ok": True, **res})


@bp.delete("/api/podcast/<int:pid>")
def api_podcast_delete(pid):
    _lib().remove_podcast(pid)
    return jsonify({"ok": True})


@bp.get("/api/podcast/<int:pid>/episodes")
def api_podcast_episodes(pid):
    limit = int(request.args.get("limit", 50))
    return jsonify({"episodes": _lib().episodes(pid, limit)})


@bp.post("/api/podcast/episodes/played")
def api_podcast_mark_played():
    d = request.json or {}
    ids = [int(i) for i in d.get("ids", [])]
    _lib().mark_episodes_played(ids, bool(d.get("played", True)))
    return jsonify({"ok": True, "count": len(ids)})


def _refresh_job(job, urls_by_id):
    lib = _lib()
    job["total"] = len(urls_by_id)
    fresh = 0
    for n, (pid, url) in enumerate(urls_by_id.items(), start=1):
        job["message"] = f"Checking {url}…"
        try:
            res = podcasts.refresh(lib, url, int(time.time()))
            fresh += res["new_episodes"]
        except Exception:  # noqa: BLE001 - one dead feed shouldn't stop the rest
            pass
        job["done"] = n
    job["result"] = {"feeds": len(urls_by_id), "new_episodes": fresh}
    job["message"] = f"Refreshed {len(urls_by_id)} feed(s), {fresh} new episode(s)."


@bp.post("/api/podcasts/refresh")
def api_podcasts_refresh():
    subs = _lib().podcasts()
    if not subs:
        return jsonify({"error": "No podcast subscriptions yet."}), 400
    urls = {p["id"]: p["url"] for p in subs}
    return jsonify({"job": start_job(_refresh_job, urls)})


def _device_safe(lib, episode: dict, src: str) -> tuple[str, str]:
    """(path, ext) of a device-playable file for the episode, transcoding
    Ogg/Opus-style feeds to MP3 when ffmpeg is present."""
    ext = os.path.splitext(src)[1].lower()
    if ext in _DEVICE_SAFE or not have_ffmpeg():
        return src, ext
    out = os.path.splitext(src)[0] + ".mp3"
    if not (os.path.exists(out) and os.path.getsize(out) > 0):
        transcode_to_mp3(src, out, 192)
    return out, ".mp3"


def _podcast_sync_job(job, params):
    lib = _lib()
    path = params["device_path"]
    dtype = "ipod" if params.get("device_type") == "ipod" else "massstorage"
    per_show = max(1, int(params.get("per_show", 3)))
    unplayed_only = bool(params.get("unplayed_only", True))
    want = {int(i) for i in params.get("podcast_ids", [])} or None

    shows = [p for p in lib.podcasts() if want is None or p["id"] in want]
    worklist: list[tuple[dict, dict]] = []
    for show in shows:
        for ep in lib.episodes(show["id"], per_show, unplayed_only):
            if ep.get("url"):
                worklist.append((show, ep))
    if not worklist:
        raise RuntimeError(
            "No podcast episodes to sync — refresh the feeds, or allow "
            "played episodes.")

    job["total"] = len(worklist)
    job["phase"] = "syncing"
    with device_management.device_lock(path, dtype):
        backend_cls = BACKENDS[dtype]
        if dtype == "ipod":
            backend = backend_cls(
                path, params.get("device_name") or "iPod",
                params.get("ipod_generation"))
        else:
            backend = backend_cls(path, params.get("layout") or "nested")
        backend.prepare()
        backend.import_existing()            # keep everything already there
        existing = set(backend._by_key) if dtype == "ipod" \
            else set(backend._records)

        added = 0
        by_show: dict[str, list[str]] = {}
        for n, (show, ep) in enumerate(worklist, start=1):
            track = podcasts.episode_track(show, ep, n)
            key = track["rating_key"]
            by_show.setdefault(show["title"], []).append(key)
            if key in existing:
                job["done"] = n
                continue
            job["message"] = f"{show['title']} – {ep['title']}"
            src = podcasts.download_episode(lib, ep)
            src, ext = _device_safe(lib, ep, src)
            backend.add_track(track, src, ext, None)
            added += 1
            job["done"] = n

        job["phase"] = "playlists"
        for show_title, keys in by_show.items():
            backend.add_playlist(f"Podcast – {show_title}", keys)

        job["phase"] = "finalizing"
        job["message"] = "Finalizing device database…"
        backend.finalize()

    job["result"] = {"episodes_added": added,
                     "episodes_skipped": len(worklist) - added,
                     "shows": len(by_show)}
    job["message"] = (f"Synced {added} episode(s) across {len(by_show)} "
                      f"show(s)"
                      + (f" ({len(worklist) - added} already on the device)"
                         if len(worklist) - added else "") + ".")


@bp.post("/api/podcasts/sync")
def api_podcasts_sync():
    p = request.json or {}
    if not os.path.isdir(p.get("device_path", "")):
        return jsonify({"error": "Device path does not exist."}), 400
    return jsonify({"job": start_job(_podcast_sync_job, p)})


from .. import server as _server  # noqa: E402  (self-registration; see api/__init__)
_server.app.register_blueprint(bp)
