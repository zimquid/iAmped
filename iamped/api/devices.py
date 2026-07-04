"""Device management routes: profiles, backups, inventory, readback, ingest,
removal, and cleanup of interrupted syncs."""
from __future__ import annotations

import os

from flask import Blueprint, jsonify, request, send_file

from .. import (config, device_management, ingest, inventory, matcher,
                readback)
from ..server import JOBS, _lib, get_server, start_job
from ..sync import device_state, itunesdb, otg

bp = Blueprint("devices", __name__)


def _remove_ipod_videos(path: str, name: str, track_ids: set) -> dict:
    """Rebuild the iTunesDB without the given video track_ids and delete their
    files. Everything else on the device (music + foreign content) is preserved
    by import_existing(); the existing DB is backed up by finalize()."""
    backend = itunesdb.ITunesDBBackend(path, name or "iPod")
    backend.prepare()
    backend.import_existing()
    keep, drop = [], []
    for e in backend._entries:
        if e.track_id in track_ids and e.mediatype in itunesdb._VIDEO_TYPES:
            drop.append(e)
        else:
            keep.append(e)
    if not drop:
        return {"removed": 0, "freed_bytes": 0}
    backend._entries = keep
    backend._by_key = {k: v for k, v in backend._by_key.items() if v in keep}
    backend.finalize()                       # writes backup + rebuilt DB
    freed = 0
    for e in drop:                           # then delete the media files
        rel = itunesdb.location_to_relpath(e.location)
        full = os.path.join(path, rel) if rel else None
        if full and os.path.exists(full):
            try:
                freed += os.path.getsize(full)
                os.remove(full)
            except OSError:
                pass
    return {"removed": len(drop), "freed_bytes": freed}


def _remove_ipod_tracks(path: str, name: str, track_ids: set) -> dict:
    """Rebuild the iTunesDB without the given track_ids (audio or video) and
    delete their files. Mirrors _remove_ipod_videos but is media-agnostic, so it
    can drop music. Everything else (foreign content) is preserved by
    import_existing(); finalize() backs up the existing DB first."""
    backend = itunesdb.ITunesDBBackend(path, name or "iPod")
    backend.prepare()
    backend.import_existing()
    keep, drop = [], []
    for e in backend._entries:
        (drop if e.track_id in track_ids else keep).append(e)
    if not drop:
        return {"removed": 0, "freed_bytes": 0}
    backend._entries = keep
    backend._by_key = {k: v for k, v in backend._by_key.items() if v in keep}
    backend.finalize()                       # writes backup + rebuilt DB
    freed = 0
    for e in drop:                           # then delete the media files
        rel = itunesdb.location_to_relpath(e.location)
        full = os.path.join(path, rel) if rel else None
        if full and os.path.exists(full):
            try:
                freed += os.path.getsize(full)
                os.remove(full)
            except OSError:
                pass
    return {"removed": len(drop), "freed_bytes": freed}


def _reclaim_ipod_orphans(path: str, name: str) -> dict:
    """Delete on-device audio the iTunesDB doesn't reference — the "Other" junk
    that eats capacity. Reads the existing DB (never rewrites it) and removes
    only files no track points at. Refuses if there's no readable iTunesDB, so a
    missing/unreadable database can't be mistaken for "everything is an orphan"
    and wipe the music."""
    backend = itunesdb.ITunesDBBackend(path, name or "iPod")
    if backend._existing_db is None:
        raise RuntimeError(
            "No iTunesDB found on this iPod — refusing to sweep so real music "
            "can't be deleted. Do a sync first, then reclaim.")
    backend.prepare()
    backend.import_existing()                # stage every track the DB references
    return backend.sweep_orphans()           # then delete anything unreferenced


def _remove_massstorage_files(path: str, rels: set) -> dict:
    from ..sync import MassStorageBackend
    removed = freed = 0
    for rel in rels:
        full = os.path.join(path, rel)
        if os.path.commonpath([os.path.abspath(full), os.path.abspath(path)]) \
                != os.path.abspath(path):
            continue                         # guard against path escapes
        if os.path.exists(full):
            try:
                freed += os.path.getsize(full)
                os.remove(full)
                removed += 1
            except OSError:
                pass
    MassStorageBackend(path, None).forget_locations(rels)
    return {"removed": removed, "freed_bytes": freed}


@bp.get("/api/device/sync/pending")
def api_device_sync_pending():
    """Report an interrupted transaction that would block a differently-configured
    sync, so the UI can surface a resume/discard choice up front."""
    path = request.args.get("device_path", "")
    dtype = "ipod" if request.args.get("device_type") == "ipod" else "massstorage"
    if not os.path.isdir(path):
        return jsonify({"pending": None})
    return jsonify({"pending": device_state.pending_transaction(path, dtype)})


@bp.post("/api/device/sync/discard")
def api_device_sync_discard():
    """Abandon an interrupted sync: drop the journal that blocks new syncs, then
    clean up the half-copied files it left behind (on an iPod those are exactly
    the orphaned 'Other' files, so we sweep them; on mass storage we delete the
    recorded partial locations). Lets the user start fresh with one click."""
    p = request.json or {}
    path = p.get("device_path", "")
    dtype = "ipod" if p.get("device_type") == "ipod" else "massstorage"
    if not os.path.isdir(path):
        return jsonify({"error": "Device path does not exist."}), 400
    try:
        with device_management.device_lock(path, dtype):
            info = device_state.discard_transaction(path, dtype)
            if dtype == "ipod":
                swept = _reclaim_ipod_orphans(path, p.get("device_name"))
            else:
                rels = {r.get("path") or itunesdb.location_to_relpath(
                    r.get("location") or "") for r in info["records"]}
                swept = _remove_massstorage_files(path, {r for r in rels if r})
    except Exception as exc:  # noqa: BLE001 - surfaced to UI
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "discarded": info["discarded"],
                    "interrupted_tracks": info["completed"],
                    "removed": swept.get("removed", 0),
                    "freed_bytes": swept.get("freed_bytes", 0)})


@bp.post("/api/device/reclaim")
def api_device_reclaim():
    """One-click cleanup of iPod 'Other' junk (orphaned audio files) without a
    full sync. iPod-only — mass-storage players have no separate index, so a
    file present is a file that plays."""
    p = request.json or {}
    path = p.get("device_path", "")
    if p.get("device_type") != "ipod":
        return jsonify({"error": "Reclaim only applies to iPods."}), 400
    if not os.path.isdir(path):
        return jsonify({"error": "Device path does not exist."}), 400
    try:
        with device_management.device_lock(path, "ipod"):
            res = _reclaim_ipod_orphans(path, p.get("device_name"))
    except Exception as exc:  # noqa: BLE001 - surfaced to UI
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, **res})


@bp.post("/api/device/video/remove")
def api_device_video_remove():
    p = request.json or {}
    path = p.get("device_path", "")
    dtype = "ipod" if p.get("device_type") == "ipod" else "massstorage"
    if not os.path.isdir(path):
        return jsonify({"error": "Device path does not exist."}), 400
    try:
        if dtype == "ipod":
            ids = {int(i) for i in p.get("track_ids", [])}
            if not ids:
                return jsonify({"error": "No videos selected."}), 400
            with device_management.device_lock(path, "ipod"):
                res = _remove_ipod_videos(path, p.get("device_name"), ids)
        else:
            rels = set(p.get("locations", []))
            if not rels:
                return jsonify({"error": "No videos selected."}), 400
            with device_management.device_lock(path, "massstorage"):
                res = _remove_massstorage_files(path, rels)
    except Exception as exc:  # noqa: BLE001 - surfaced to UI
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, **res})


@bp.post("/api/device/music/remove")
def api_device_music_remove():
    """Delete selected music tracks from the device. The Plex library is never
    touched — this only removes the on-device copy and rebuilds the device's
    own database (iPod) / forgets the locations (mass storage)."""
    p = request.json or {}
    path = p.get("device_path", "")
    dtype = "ipod" if p.get("device_type") == "ipod" else "massstorage"
    if not os.path.isdir(path):
        return jsonify({"error": "Device path does not exist."}), 400
    try:
        if dtype == "ipod":
            ids = {int(i) for i in p.get("track_ids", [])}
            if not ids:
                return jsonify({"error": "No tracks selected."}), 400
            with device_management.device_lock(path, "ipod"):
                res = _remove_ipod_tracks(path, p.get("device_name"), ids)
        else:
            rels = set(p.get("locations", []))
            if not rels:
                return jsonify({"error": "No tracks selected."}), 400
            with device_management.device_lock(path, "massstorage"):
                res = _remove_massstorage_files(path, rels)
    except Exception as exc:  # noqa: BLE001 - surfaced to UI
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, **res})


@bp.get("/api/plex/music/locations")
def api_plex_music_locations():
    """Server-side folder paths for each music section, so the UI can hint which
    folder Plex actually watches when picking an ingest destination."""
    try:
        server = get_server()
        out = []
        for s in server.library.sections():
            if s.type == "artist":
                out.append({"title": s.title,
                            "locations": list(getattr(s, "locations", []) or [])})
        return jsonify({"sections": out})
    except Exception as exc:  # noqa: BLE001 - surfaced to UI
        return jsonify({"error": str(exc)}), 400


@bp.post("/api/device/ingest/preview")
def api_device_ingest_preview():
    p = request.json or {}
    path = p.get("device_path", "")
    dtype = "ipod" if p.get("device_type") == "ipod" else "massstorage"
    if not os.path.isdir(path):
        return jsonify({"error": "Device path does not exist."}), 400
    try:
        res = ingest.plan(_lib(), path, dtype,
                          p.get("track_ids"), p.get("locations"))
    except Exception as exc:  # noqa: BLE001 - surfaced to UI
        return jsonify({"error": str(exc)}), 400
    return jsonify(res)


def _ingest_job(job, p):
    path = p["device_path"]
    dtype = "ipod" if p.get("device_type") == "ipod" else "massstorage"
    cfg = config.load()
    ingest_dir = cfg.get("ingest_dir") or ""
    section = cfg.get("ingest_section") or cfg.get("music_section") or ""
    name = p.get("device_name")

    def batch_remove(confirmed):
        if dtype == "ipod":
            ids = {int(i["track_id"]) for i in confirmed
                   if i.get("track_id") is not None}
            return _remove_ipod_tracks(path, name, ids) if ids else {}
        rels = {i["location"] for i in confirmed if i.get("location")}
        return _remove_massstorage_files(path, rels) if rels else {}

    def prog(phase, done, total):
        job["phase"] = phase
        job["done"], job["total"] = done, total
        job["message"] = (f"Copying to Plex… {done}/{total}" if phase == "copying"
                          else f"Waiting for Plex to scan… {done}/{total}")

    with device_management.device_lock(path, dtype):
        server = get_server()
        job["result"] = ingest.apply(
            server, _lib(), path, dtype, p.get("items", []),
            ingest_dir, section, progress=prog, batch_remove=batch_remove)
    r = job["result"]
    job["message"] = (
        f"Ingested {r['confirmed']}/{r['ingested']} into Plex"
        + (f", removed {r['removed_from_device']} from device" if r['removed_from_device'] else "")
        + (f", {len(r['unconfirmed'])} left on device (not yet in Plex)" if r['unconfirmed'] else "")
        + ".")


@bp.post("/api/device/ingest")
def api_device_ingest():
    p = request.json or {}
    if not os.path.isdir(p.get("device_path", "")):
        return jsonify({"error": "Device path does not exist."}), 400
    if not [i for i in (p.get("items") or []) if i.get("status") == "ingest"]:
        return jsonify({"error": "Nothing to ingest."}), 400
    cfg = config.load()
    ingest_dir = cfg.get("ingest_dir") or ""
    if not ingest_dir:
        return jsonify({"error": "Set an ingest folder first (Device → Ingest folder)."}), 400
    try:
        os.makedirs(ingest_dir, exist_ok=True)
        if not os.access(ingest_dir, os.W_OK):
            raise OSError("not writable")
    except OSError:
        return jsonify({"error": f"Ingest folder is not writable: {ingest_dir}"}), 400
    if not (cfg.get("ingest_section") or cfg.get("music_section")):
        return jsonify({"error": "No music section configured to scan."}), 400
    return jsonify({"job": start_job(_ingest_job, p)})


@bp.get("/api/device/profile")
def api_device_profile():
    path = request.args.get("device_path", "")
    dtype = request.args.get("device_type", "massstorage")
    if not os.path.isdir(path):
        return jsonify({"error": "Device path does not exist."}), 400
    return jsonify(device_management.get_profile(path, dtype))


@bp.put("/api/device/profile")
def api_device_profile_save():
    p = request.json or {}
    if not os.path.isdir(p.get("device_path", "")):
        return jsonify({"error": "Device path does not exist."}), 400
    return jsonify(device_management.save_profile(
        p["device_path"], p.get("device_type", "massstorage"), p))


@bp.get("/api/device/backups")
def api_device_backups():
    path = request.args.get("device_path", "")
    dtype = request.args.get("device_type", "massstorage")
    if not os.path.isdir(path):
        return jsonify({"error": "Device path does not exist."}), 400
    return jsonify({"backups": device_management.list_backups(path, dtype)})


def _restore_job(job, p):
    path = p["device_path"]
    dtype = p.get("device_type", "massstorage")
    with device_management.device_lock(path, dtype):
        job["phase"] = "restore"
        job["message"] = "Restoring device snapshot…"
        job["result"] = device_management.restore_backup(
            path, dtype, p["backup_id"])
        job["message"] = f"Restored backup {p['backup_id']}."


@bp.post("/api/device/restore")
def api_device_restore():
    p = request.json or {}
    if not os.path.isdir(p.get("device_path", "")):
        return jsonify({"error": "Device path does not exist."}), 400
    if not p.get("backup_id"):
        return jsonify({"error": "Choose a backup."}), 400
    return jsonify({"job": start_job(_restore_job, p)})


@bp.post("/api/device/eject")
def api_device_eject():
    p = request.json or {}
    path = p.get("device_path", "")
    if not os.path.isdir(path):
        return jsonify({"error": "Device path does not exist."}), 400
    for job in JOBS.values():
        if job.get("status") == "running" and job.get("device_path") == path:
            return jsonify({"error": "A device operation is still running."}), 409
    try:
        device_management.eject(path)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True})


@bp.get("/api/device/matches")
def api_device_matches():
    path = request.args.get("device_path", "")
    dtype = request.args.get("device_type", "massstorage")
    if not os.path.isdir(path):
        return jsonify({"error": "Device path does not exist."}), 400
    try:
        inv = inventory.read_device_library(path, dtype)
        rows = []
        for track in inv.get("tracks", []):
            if track.get("rating_key"):
                continue
            rows.append({**track, "match": matcher.match_track(_lib(), track)})
        return jsonify({"matches": rows, "count": len(rows),
                        "chromaprint_available": bool(
                            __import__("shutil").which("fpcalc"))})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@bp.get("/api/device/stream")
def api_device_stream():
    """Stream an audio file that physically lives on the connected device.

    Tracks read back from an iPod/USB player aren't in Plex, so ``/api/stream``
    (which resolves against the library) can't serve them — the in-app player
    would 404. This serves the on-device file straight from the mount, with
    Range support so the browser ``<audio>`` element can scrub."""
    path = request.args.get("device_path", "")
    location = request.args.get("location", "")
    if not os.path.isdir(path):
        return jsonify({"error": "Device path does not exist."}), 400
    if not location:
        return jsonify({"error": "location is required"}), 400
    rel = itunesdb.location_to_relpath(location)
    root = os.path.realpath(path)
    target = os.path.realpath(os.path.join(root, rel))
    # Contain the lookup to the device mount — never serve a path that escapes it.
    if target != root and os.path.commonpath([target, root]) != root:
        return jsonify({"error": "invalid location"}), 400
    if not os.path.isfile(target):
        return jsonify({"error": "File not found on device."}), 404
    ext = os.path.splitext(target)[1].lstrip(".").lower()
    mime = ("audio/mp4" if ext in ("m4a", "mp4", "aac", "m4b", "m4p")
            else "audio/mpeg" if ext == "mp3"
            else "audio/wav" if ext in ("wav", "aiff", "aif")
            else f"audio/{ext or 'mpeg'}")
    resp = send_file(target, mimetype=mime, conditional=True)
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
    return resp


@bp.get("/api/device/inventory")
def api_device_inventory():
    """Read-only listing of what's already on a device, tagged by provenance
    (what iAmped wrote vs. what was already there). ``summary=1`` drops the
    per-track rows and returns just the counts."""
    a = request.args
    path = a.get("device_path", "")
    if not os.path.isdir(path):
        return jsonify({"error": "Device path does not exist."}), 400
    try:
        inv = inventory.read_device_library(path, a.get("device_type", "ipod"))
    except Exception as exc:  # noqa: BLE001 - surfaced to UI
        return jsonify({"error": str(exc)}), 400
    if a.get("summary") in ("1", "true", "yes"):
        inv = {k: v for k, v in inv.items() if k != "tracks"}
    return jsonify(inv)


@bp.get("/api/device/otg")
def api_device_otg():
    """On-The-Go playlists queued on the iPod since the last sync, resolved
    against the device database and the local library."""
    path = request.args.get("device_path", "")
    if not os.path.isdir(path):
        return jsonify({"error": "Device path does not exist."}), 400
    try:
        plans = otg.build_plans(_lib(), path)
    except FileNotFoundError:
        return jsonify({"error": "No iTunesDB on this device."}), 400
    except Exception as exc:  # noqa: BLE001 - surfaced to UI
        return jsonify({"error": str(exc)}), 400
    return jsonify({"playlists": plans})


@bp.post("/api/device/otg/import")
def api_device_otg_import():
    """Save an On-The-Go playlist as a local playlist (and optionally a real
    Plex playlist), then — like iTunes — clear the OTG files so the same queue
    isn't imported twice. Body: device_path, file, title?, to_plex?, clear?."""
    import time as _time
    p = request.json or {}
    path = p.get("device_path", "")
    if not os.path.isdir(path):
        return jsonify({"error": "Device path does not exist."}), 400
    try:
        plans = otg.build_plans(_lib(), path)
    except Exception as exc:  # noqa: BLE001 - surfaced to UI
        return jsonify({"error": str(exc)}), 400
    plan = next((x for x in plans if x["file"] == p.get("file")), None)
    if plan is None:
        return jsonify({"error": "That On-The-Go playlist is no longer on the device."}), 404
    keys = [t["rating_key"] for t in plan["tracks"] if t["rating_key"]]
    if not keys:
        return jsonify({"error": "None of the On-The-Go tracks matched the library."}), 400
    title = (p.get("title") or "").strip() or plan["title"]
    lib = _lib()
    pid = lib.create_local_playlist(title, "otg", int(_time.time()), "", keys)
    plex_key = None
    plex_error = None
    if p.get("to_plex"):
        try:
            plex_key = plex_client_create(title, keys)
        except Exception as exc:  # noqa: BLE001
            plex_error = str(exc)
    cleared = otg.clear(path) if p.get("clear", True) else 0
    return jsonify({"ok": True, "id": f"local:{pid}", "title": title,
                    "count": len(keys), "skipped": len(plan["tracks"]) - len(keys),
                    "plex_playlist": plex_key, "plex_error": plex_error,
                    "cleared_files": cleared})


def plex_client_create(title: str, keys: list[str]):
    from .. import plex_client
    return plex_client.create_playlist(get_server(), title, keys)


@bp.post("/api/device/readback")
def api_readback():
    p = request.json or {}
    if not os.path.isdir(p.get("device_path", "")):
        return jsonify({"error": "Device path does not exist."}), 400
    try:
        res = readback.build_plan(
            _lib(), p["device_path"], p.get("device_type", "ipod"),
            bool(p.get("want_plays", True)), bool(p.get("want_ratings", True)),
            p.get("policy", "fill_blanks"))
    except Exception as exc:  # noqa: BLE001 - surfaced to UI
        return jsonify({"error": str(exc)}), 400
    return jsonify(res)


def _readback_apply_job(job, p):
    dtype = p.get("device_type", "ipod")
    with device_management.device_lock(p["device_path"], dtype):
        server = get_server()
        lib = _lib()
        plan = p.get("plan", [])
        job["phase"] = "applying"
        job["total"] = len(plan)

        def prog(done, total):
            job["done"], job["total"] = done, total
            job["message"] = f"Writing to Plex… {done}/{total}"

        res = readback.apply_plan(server, lib, p["device_path"],
                                  dtype, plan, bool(p.get("reset", True)), prog)
        job["result"] = res
        job["message"] = (f"Added {res['plays_added']} plays, set "
                          f"{res['ratings_set']} ratings"
                          + (" · device reset" if res["reset"] else "") + ".")


@bp.post("/api/device/readback/apply")
def api_readback_apply():
    p = request.json or {}
    if not p.get("plan"):
        return jsonify({"error": "Nothing to apply."}), 400
    return jsonify({"job": start_job(_readback_apply_job, p)})


from .. import server as _server  # noqa: E402  (self-registration; see api/__init__)
_server.app.register_blueprint(bp)
