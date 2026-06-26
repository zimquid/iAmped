"""Flask backend: REST API, background jobs, and sync orchestration."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import traceback
import uuid
import webbrowser

from flask import Flask, Response, jsonify, request, send_from_directory

from . import (artwork, config, device_management, filler, inventory, matcher,
               plex_client, readback)
from .library import Library
from .sync import (BACKENDS, free_bytes, list_volumes, target_format,
                   total_bytes, transcode)
from .sync import device_state, itunesdb
from .sync import video as video_mod
from .sync.base import have_ffmpeg

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
app = Flask(__name__, static_folder=None)

_state: dict = {"server": None}
JOBS: dict[str, dict] = {}
PLEX_OAUTH: dict[str, dict] = {}
PLEX_OAUTH_TTL = 300


# --------------------------------------------------------------------------- helpers
def _lib() -> Library:
    return Library(str(config.DB_PATH))


def get_server():
    cfg = config.load()
    if not cfg["plex_baseurl"] or not cfg["plex_token"]:
        raise RuntimeError("Plex is not configured yet.")
    if _state["server"] is None:
        _state["server"] = plex_client.connect(cfg["plex_baseurl"], cfg["plex_token"])
    return _state["server"]


def _connection_result(server) -> dict:
    info = plex_client.server_info(server)
    sections = plex_client.music_sections(server)
    if sections and not config.load()["music_section"]:
        config.save({"music_section": sections[0]})
    return {"ok": True, "server": info, "sections": sections}


def _store_server(server) -> dict:
    config.save({
        "plex_baseurl": server._baseurl,
        "plex_token": server._token,
    })
    _state["server"] = server
    return _connection_result(server)


def _oauth_server_view(resource) -> dict:
    return {
        "id": resource.clientIdentifier,
        "name": resource.name,
        "owned": bool(resource.owned),
        "online": bool(resource.presence),
        "platform": resource.platform or "",
    }


def _expire_oauth_logins() -> None:
    cutoff = time.time() - PLEX_OAUTH_TTL
    for login_id, state in list(PLEX_OAUTH.items()):
        if state["created"] < cutoff:
            PLEX_OAUTH.pop(login_id, None)


def start_job(target, *args) -> str:
    jid = uuid.uuid4().hex[:12]
    job = {"status": "running", "phase": "starting", "done": 0, "total": 0,
           "message": "", "result": None, "error": None}
    JOBS[jid] = job
    if args and isinstance(args[-1], dict):
        job["device_path"] = args[-1].get("device_path")

    def runner():
        try:
            target(job, *args)
            job["status"] = "done"
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            job["status"] = "error"
            job["error"] = str(exc)
            job["trace"] = traceback.format_exc()
    threading.Thread(target=runner, daemon=True).start()
    return jid


def _ext_for(track: dict) -> str:
    c = (track.get("container") or "").lower()
    if c:
        return "." + c
    e = os.path.splitext(track.get("server_file") or "")[1]
    return e or ".mp3"


def materialize(server, lib: Library, track: dict, transcode_lossless: bool,
                fmt: str, bitrate_k: int | None = None):
    """Ensure a local audio file exists in the device's target format. Returns
    (local_path, ext_with_dot). `fmt` is 'aac' (iPod) or 'mp3' (USB)."""
    cache = config.load()["cache_dir"]
    rk = track["rating_key"]
    orig_ext = _ext_for(track)
    orig = os.path.join(cache, f"{rk}{orig_ext}")
    if not (os.path.exists(orig) and os.path.getsize(orig) > 0):
        plex_client.download_part(server, track["part_key"], orig)
        lib.set_cached(rk, orig, os.path.getsize(orig))

    if filler.should_transcode(
            track, transcode_lossless, bitrate_k) and have_ffmpeg():
        ext = ".m4a" if fmt == "aac" else ".mp3"
        output_base = os.path.join(
            cache, f"{rk}.{fmt}.{bitrate_k or 0}k")
        out = output_base + ext
        if not (os.path.exists(out) and os.path.getsize(out) > 0):
            try:
                transcode(orig, output_base, fmt, bitrate_k)
            except Exception:
                return orig, orig_ext          # fall back to the original
        return out, ext
    return orig, orig_ext


def _resolve_capability(params: dict):
    """Auto-detect the transport + on-disk layout for the target device, with a
    saved/explicit ``transport``/``layout`` override taking precedence.

    Detection mirrors how MediaMonkey/iTunes pick a transport without asking:
    iPods use the iTunes DB, MTP players are handed indexed tracks, flat-scan
    USB players (Creative MuVo) get a one-level layout, and everything else
    defaults to flat for maximum compatibility.
    """
    from .devices import capabilities, list_devices
    override = {k: params[k] for k in ("transport", "layout")
               if params.get(k) in (
                   capabilities.LAYOUT_FLAT, capabilities.LAYOUT_NESTED,
                   capabilities.TRANSPORT_UMS, capabilities.TRANSPORT_MTP,
                   capabilities.TRANSPORT_IPOD)}
    path = params.get("device_path")
    busloc = params.get("mtp_busloc")
    try:
        for dev in list_devices(include_raw=False):
            if (dev.mountpoint and dev.mountpoint == path) or \
                    (busloc and dev.mtp_busloc == busloc):
                return capabilities.resolve(dev, override)
    except Exception:  # noqa: BLE001 - detection must never break a sync
        pass
    # Device not enumerable (unmounted, or called before a scan): honour an
    # explicit override, else fall back to the safe flat default.
    return capabilities.Capability(
        transport=override.get("transport", capabilities.TRANSPORT_UMS),
        layout=override.get("layout", capabilities.LAYOUT_FLAT),
        reason="default", source="override" if override else "auto")


def _reserve_bytes(path: str, params: dict) -> int:
    """Headroom to keep free on the device, capped for small players.

    The configured reserve (default 200 MB) is sensible for iPods and large
    USB drives, but on a sub-1 GB player like a Creative MuVo it can swallow
    most of the disk — the sync budget is ``free - reserve``, so a flat 200 MB
    on a 495 MB device collapses the budget to near-zero and every track gets
    "skipped for space". Cap the reserve at 5% of total capacity so small
    devices keep a sane, proportional amount of headroom instead.
    """
    requested = int(params.get("reserve_mb", 200)) * 1024 * 1024
    total = total_bytes(path)
    if total:
        return min(requested, int(total * 0.05))
    return requested


def _device_plan(params: dict) -> dict:
    """Build the desired device state and its incremental diff.

    Mirror mode budgets against free space plus space occupied by files owned by
    iAmped, because those files may be retained or reclaimed. Additive mode
    retains the historical behavior: only choose tracks that are not present.
    """
    path = params["device_path"]
    dtype = params.get("device_type", "massstorage")
    mirror = bool(params.get("mirror", True))
    lib = _lib()
    manifest = device_state.read_manifest(path, dtype)
    pending_tx = device_state.read_json(device_state.journal_path(path, dtype))
    prior = device_state.managed_records(manifest)
    valid_prior = {
        key: record for key, record in prior.items()
        if device_state.record_is_valid(path, record)
    }

    exclude_keys: set[str] = set()
    exclude_meta: set[str] = set()
    capacity = free_bytes(path)
    if mirror:
        capacity += sum(int(r.get("size") or 0) for r in valid_prior.values())
    else:
        exclude_keys = set(valid_prior)

    if dtype == "ipod":
        try:
            inv = inventory.read_device_library(path, "ipod")
            rows = inv["tracks"]
            if mirror:
                foreign_meta = {
                    filler.match_key(t.get("artist"), t.get("title"))
                    for t in rows
                    if t.get("origin") == inventory.ORIGIN_FOREIGN
                }
                managed_meta = {
                    filler.match_key(t.get("artist"), t.get("title"))
                    for t in rows
                    if t.get("origin") == inventory.ORIGIN_IAMPED
                }
                exclude_meta = foreign_meta - managed_meta
            else:
                exclude_meta = {
                filler.match_key(t.get("artist"), t.get("title")) for t in rows
                }
        except Exception:  # noqa: BLE001 - best-effort foreign-file protection
            exclude_meta = set()

    reserve = _reserve_bytes(path, params)
    fmt = target_format(dtype)
    bitrate_k = int(params.get("target_bitrate_k")
                    or (256 if fmt == "aac" else 320))
    if "rating_keys" in params:
        plan = filler.explicit_plan(
            lib, [str(k) for k in params.get("rating_keys", [])], capacity,
            reserve, bool(params.get("transcode_lossless", True)),
            fmt, bitrate_k, params.get("max_tracks"))
    else:
        plan = filler.plan(
            lib, capacity, reserve, params.get("fill_strategy", "most_played"),
            params.get("playlist_ids", []),
            bool(params.get("transcode_lossless", True)), params.get("max_tracks"),
            fmt, exclude_keys=exclude_keys, exclude_meta=exclude_meta,
            target_bitrate_k=bitrate_k,
            fill_remaining=not bool(params.get("playlist_only")))

    transcode_lossless = bool(params.get("transcode_lossless", True))
    for track in plan["tracks"]:
        output = f"{fmt}:{bitrate_k}k" if filler.should_transcode(
            track, transcode_lossless, bitrate_k) \
            else (track.get("container") or track.get("codec") or "original")
        track["_sync_signature"] = "|".join([
            str(track.get("part_key") or ""),
            str(track.get("file_size") or 0),
            str(track.get("container") or ""),
            str(track.get("codec") or ""),
            output,
        ])

    desired = {str(t["rating_key"]): t for t in plan["tracks"]}
    if mirror:
        update_keys = {
            key for key in set(desired) & set(valid_prior)
            if valid_prior[key].get("source_signature") and
            valid_prior[key]["source_signature"] != desired[key]["_sync_signature"]
        }
        keep_keys = (set(desired) & set(valid_prior)) - update_keys
        add_tracks = [t for t in plan["tracks"] if str(t["rating_key"]) not in keep_keys]
        removals = [
            record for key, record in prior.items()
            if key not in desired or key in update_keys
        ]
        keep_tracks = {key: desired[key] for key in keep_keys}
    else:
        update_keys = set()
        add_tracks = [
            t for t in plan["tracks"]
            if str(t["rating_key"]) not in valid_prior
        ]
        removals = []
        keep_tracks = None

    # The review UI sends the checked operation IDs back to sync. Deselecting
    # an update/removal preserves the prior managed file; deselecting an
    # addition removes it from the desired state.
    selected = params.get("review_actions")
    if isinstance(selected, list):
        selected = set(selected)
        prior_rows = lib.get_tracks(list(prior))
        kept_removals = []
        for record in removals:
            key = str(record.get("rating_key"))
            action = f"update:{key}" if key in update_keys else f"remove:{key}"
            if action in selected:
                kept_removals.append(record)
                continue
            track = desired.get(key) or prior_rows.get(key) or {
                "rating_key": key, "title": record.get("title"),
                "artist": record.get("artist"), "album": record.get("album"),
                "duration_ms": record.get("duration_ms") or 0,
            }
            desired[key] = track
            if keep_tracks is not None:
                keep_tracks[key] = track
        removals = kept_removals
        filtered = []
        for track in add_tracks:
            key = str(track["rating_key"])
            action = f"update:{key}" if key in update_keys else f"add:{key}"
            if action in selected:
                filtered.append(track)
            elif key not in prior:
                desired.pop(key, None)
        add_tracks = filtered
        update_keys = {key for key in update_keys if f"update:{key}" in selected}
        plan["tracks"] = [t for t in plan["tracks"]
                          if str(t["rating_key"]) in desired]
        plan["track_count"] = len(desired)
        plan["total_bytes"] = sum(
            int(t.get("_size") or 0) for t in plan["tracks"])

    return {
        "plan": plan,
        "manifest": manifest,
        "prior": prior,
        "valid_prior": valid_prior,
        "desired": desired,
        "keep_tracks": keep_tracks,
        "add_tracks": add_tracks,
        "removals": removals,
        "update_keys": update_keys,
        "pending_transaction": bool(
            pending_tx and pending_tx.get("status") in {"copying", "metadata", "cleanup"}),
        "mirror": mirror,
    }


def _bitrate_options(params: dict, budget_bytes: int) -> list[dict]:
    """Estimate a dragged selection at each supported target bitrate."""
    lib = _lib()
    keys = [str(k) for k in params.get("rating_keys", [])]
    if not keys:
        for pid in params.get("playlist_ids", []):
            keys.extend(lib.playlist_track_keys_any(pid))
    keys = list(dict.fromkeys(keys))
    rows = lib.get_tracks(keys)
    ordered = [rows[key] for key in keys if key in rows]
    fmt = target_format(params.get("device_type", "massstorage"))
    enabled = bool(params.get("transcode_lossless", True))
    options = []
    for bitrate_k in filler.BITRATE_PRESETS[fmt]:
        requested_bytes = sum(
            filler.device_size(t, enabled, fmt, bitrate_k) for t in ordered)
        bounded = filler.bound_tracks(
            ordered, max_bytes=budget_bytes,
            transcode_lossless=enabled, target_format=fmt,
            target_bitrate_k=bitrate_k)
        options.append({
            "bitrate_k": bitrate_k,
            "requested_bytes": requested_bytes,
            "fitting_tracks": len(bounded["tracks"]),
            "fits": bounded["dropped"] == 0,
        })
    return options


# --------------------------------------------------------------------------- jobs
def _build_library_job(job, section: str):
    server = get_server()
    lib = _lib()
    job["phase"] = "tracks"

    def prog(done, total):
        job["done"], job["total"] = done, total or 0
        job["message"] = f"Reading track metadata… {done}"

    batch, count = [], 0
    for meta in plex_client.iter_tracks(server, section, prog):
        batch.append(meta)
        if len(batch) >= 200:
            count += lib.upsert_tracks(batch)
            batch = []
    if batch:
        count += lib.upsert_tracks(batch)

    job["phase"] = "playlists"
    job["message"] = "Reading playlists…"
    pls = plex_client.list_playlists(server)
    lib.replace_playlists(pls)
    job["result"] = {"tracks": count, "playlists": len(pls)}
    job["message"] = f"Cached {count} tracks and {len(pls)} playlists."


def _sync_job_inner(job, params):
    server = get_server()
    lib = _lib()
    is_ipod = params["device_type"] == "ipod"
    dtype = params["device_type"]
    path = params["device_path"]
    recovered = device_state.recover_cleanup(path, dtype)
    job["phase"] = "planning"
    job["message"] = "Choosing tracks to fit the device…"
    diff = _device_plan(params)
    plan = diff["plan"]
    tracks = diff["add_tracks"]
    desired_state = [
        f"{key}:{track.get('_sync_signature', '')}"
        for key, track in diff["desired"].items()
    ]
    digest = device_state.plan_hash(dtype, desired_state, params)
    backup = None
    if (tracks or diff["removals"]) and not diff["pending_transaction"]:
        job["phase"] = "backup"
        job["message"] = "Creating rollback snapshot…"
        backup = device_management.create_backup(path, dtype, diff["removals"])
    tx, resumed = device_state.start_or_resume(
        path, dtype, digest, diff["removals"])
    if diff["mirror"]:
        if "target_reclaim_bytes" not in tx:
            add_bytes = sum(int(t.get("_size") or 0) for t in tracks)
            writable_free = max(
                free_bytes(path) - _reserve_bytes(path, params),
                0)
            tx["target_reclaim_bytes"] = max(add_bytes - writable_free, 0)
            device_state.checkpoint(path, dtype, tx)
        if tx["target_reclaim_bytes"]:
            job["message"] = "Reclaiming space from stale iAmped tracks…"
            device_state.reclaim_for_copy(
                path, dtype, tx, int(tx["target_reclaim_bytes"]))

    backend_cls = BACKENDS[dtype]
    if is_ipod:
        backend = backend_cls(path, params.get("device_name") or "iPod")
    else:
        backend = backend_cls(path, _resolve_capability(params).layout)
    backend.prepare()

    job["message"] = "Reading what's already on the device…"
    carried = backend.import_existing(diff["keep_tracks"], tx.get("completed", {}))

    # A matching interrupted transaction may already have atomically published
    # some audio files. Re-stage those records instead of downloading/copying.
    completed = tx.get("completed", {})
    remaining = []
    for tr in tracks:
        key = str(tr["rating_key"])
        record = completed.get(key)
        if record and device_state.record_is_valid(path, record):
            backend.restore_track(tr, record)
        else:
            if record:
                completed.pop(key, None)
            remaining.append(tr)
    if len(remaining) != len(tracks):
        device_state.checkpoint(path, dtype, tx)

    job["total"] = len(tracks)
    job["done"] = len(tracks) - len(remaining)
    job["phase"] = "syncing"
    tl = bool(params["transcode_lossless"])
    fmt = target_format(dtype)
    bitrate_k = int(params.get("target_bitrate_k")
                    or (256 if fmt == "aac" else 320))
    want_artwork = bool(params.get("sync_artwork", True))
    # Downloading from Plex and transcoding are the slow, independent parts; the
    # actual copy onto the iPod/USB must stay serial (the iTunesDB and file tree
    # aren't written concurrently). So prefetch materialization in a small thread
    # pool and consume the results in order — the network/CPU work for track N+k
    # overlaps the device write for track N, which is what made the old
    # one-at-a-time loop slower than iTunes.
    from concurrent.futures import ThreadPoolExecutor

    workers = max(1, min(4, len(remaining)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(
            materialize, server, lib, tr, tl, fmt, bitrate_k)
                   for tr in remaining]
        album_futures = {}
        art_futures = []
        for tr in remaining:
            if not (want_artwork and tr.get("album_thumb")):
                art_futures.append(None)
                continue
            album_id = artwork.album_identity(tr)
            future = album_futures.get(album_id)
            if future is None:
                future = pool.submit(artwork.materialize, server, tr)
                album_futures[album_id] = future
            art_futures.append(future)
        base_done = job["done"]
        for i, tr in enumerate(remaining):
            src, ext = futures[i].result()
            art_path = art_futures[i].result() if art_futures[i] else None
            record = backend.add_track(tr, src, ext, art_path)
            device_state.record_completed(path, dtype, tx, str(tr["rating_key"]), record)
            job["done"] = base_done + i + 1
            job["message"] = f"{tr.get('artist','')} – {tr.get('title','')}"
    job["done"] = len(tracks)

    job["phase"] = "playlists"
    job["message"] = "Writing playlists…"
    for pl in plan["playlists"]:
        backend.add_playlist(pl["title"], pl["track_keys"])

    job["phase"] = "finalizing"
    job["message"] = "Finalizing device database…"
    tx["status"] = "metadata"
    device_state.checkpoint(path, dtype, tx)
    backend.finalize()
    removed = device_state.finish_cleanup(path, dtype, tx)
    updated = len(diff["update_keys"])
    added = len(tracks) - updated
    job["result"] = {
        "tracks_added": added,
        "tracks_updated": updated,
        "tracks_preserved": carried,
        "tracks_removed": removed + recovered,
        "tracks_total": carried + len(tracks),
        "managed_tracks_total": len(diff["desired"]) if diff["mirror"]
        else len(diff["valid_prior"]) + added,
        "playlists": len(plan["playlists"]),
        "bytes": plan["total_bytes"],
        "skipped_already_present": plan.get("skipped_present", 0),
        "resumed": resumed,
        "backup": backup,
        "artwork": want_artwork,
    }
    job["message"] = (
        f"Added {added} new track(s)"
        + (f", updated {updated}" if updated else "")
        + (f", preserved {carried} already on the device" if carried else "")
        + (f", removed {removed + recovered} stale track(s)" if removed + recovered else "")
        + f" → {carried + len(tracks)} total on {params['device_path']}.")


def _sync_job(job, params):
    with device_management.device_lock(
            params["device_path"], params["device_type"]):
        _sync_job_inner(job, params)


# --------------------------------------------------------------------------- MTP
_MTP_LOCKS: dict[str, threading.Lock] = {}


def _mtp_lock(busloc: str) -> threading.Lock:
    return _MTP_LOCKS.setdefault(busloc or "mtp", threading.Lock())


def _mtp_sync_job_inner(job, params):
    """Push tracks to an MTP player (Creative Zen-class) via libmtp.

    MTP devices index tracks into their own database, so this is additive and
    deliberately simpler than the mass-storage path: no on-device manifest,
    transactions, or backups (the device manages its own library). What we've
    sent is recorded host-side so reruns don't duplicate.
    """
    from .sync import mtp
    server = get_server()
    lib = _lib()
    backend = mtp.MTPBackend(params.get("mtp_busloc"),
                             folder=params.get("mtp_folder") or "Music")
    job["phase"] = "planning"
    job["message"] = "Checking the MTP player…"
    backend.prepare()                       # raises MTPError if libmtp absent
    total, free = mtp.storage_free()
    if total <= 0:
        # A connected MTP device always reports a positive MaxCapacity; zero
        # means libmtp couldn't reach the player.
        raise mtp.MTPError(
            "Couldn't reach the MTP player — reconnect it, make sure no other "
            "program (Music app / Android File Transfer) holds it, then scan "
            "again.")
    capacity = free or total
    reserve = int(params.get("reserve_mb", 0)) * 1024 * 1024
    fmt, bitrate_k = "mp3", int(params.get("target_bitrate_k") or 320)
    tl = bool(params.get("transcode_lossless", True))
    existing = backend.existing_keys()

    if "rating_keys" in params:
        plan = filler.explicit_plan(
            lib, [str(k) for k in params.get("rating_keys", [])], capacity,
            reserve, tl, fmt, bitrate_k, params.get("max_tracks"))
    else:
        plan = filler.plan(
            lib, capacity, reserve, params.get("fill_strategy", "most_played"),
            params.get("playlist_ids", []), tl, params.get("max_tracks"), fmt,
            exclude_keys=existing, target_bitrate_k=bitrate_k,
            fill_remaining=not bool(params.get("playlist_only")))

    tracks = [t for t in plan["tracks"] if str(t["rating_key"]) not in existing]
    job["total"] = len(tracks)
    job["done"] = 0
    job["phase"] = "syncing"
    for i, tr in enumerate(tracks):
        src, ext = materialize(server, lib, tr, tl, fmt, bitrate_k)
        backend.add_track(tr, src, ext, None)
        job["done"] = i + 1
        job["message"] = f"{tr.get('artist', '')} – {tr.get('title', '')}"

    job["phase"] = "finalizing"
    backend.finalize()
    job["result"] = {
        "tracks_added": len(tracks),
        "tracks_total": len(existing) + len(tracks),
        "transport": "mtp",
        "bytes": sum(int(t.get("_size") or 0) for t in tracks),
    }
    job["message"] = (f"Sent {len(tracks)} track(s) to the MTP player "
                      f"→ {len(existing) + len(tracks)} total.")


def _mtp_sync_job(job, params):
    busloc = params.get("mtp_busloc")
    lock = _mtp_lock(busloc)
    if not lock.acquire(blocking=False):
        raise RuntimeError("This MTP player is already being written by iAmped.")
    try:
        _mtp_sync_job_inner(job, params)
    finally:
        lock.release()


# --------------------------------------------------------------------------- video
def _resolve_device_and_profile(params: dict):
    """Find the target Device and its (capability, video_profile). Returns
    ``(device, capability, profile)``; ``profile`` is None when the device can't
    play video iAmped can sync."""
    from .devices import capabilities, list_devices
    path = params.get("device_path")
    busloc = params.get("mtp_busloc")
    device = None
    try:
        for dev in list_devices(include_raw=False, include_mtp=bool(busloc)):
            if (dev.mountpoint and dev.mountpoint == path) or \
                    (busloc and dev.mtp_busloc == busloc):
                device = dev
                break
    except Exception:  # noqa: BLE001
        pass
    cap = _resolve_capability(params)
    profile = capabilities.video_profile(device, cap) if device else \
        capabilities.video_profile(_DeviceShim(params), cap)
    return device, cap, profile


class _DeviceShim:
    """Minimal stand-in when the device isn't enumerable (called before a scan):
    carries just the fields video_profile() reads."""
    def __init__(self, params):
        self.ipod_generation = params.get("ipod_generation", "")
        self.ipod_model = params.get("ipod_model", "")
        self.model = params.get("model", "")


def _video_track_dict(meta) -> dict:
    """Flatten a plex_client.VideoMeta into the track-shaped dict the backends
    expect, plus the (mediatype, video) the iPod writer needs."""
    from .sync.itunesdb import MEDIATYPE_MOVIE, MEDIATYPE_TVSHOW
    is_ep = meta.kind == "episode"
    track = {
        "rating_key": meta.rating_key,
        "title": meta.title,
        "artist": meta.show_title if is_ep else "",
        "album": meta.show_title if is_ep else "",
        "album_artist": meta.show_title if is_ep else "",
        "genre": "",
        "duration_ms": meta.duration_ms,
        "year": meta.year or 0,
        "_size": meta.file_size,
        "_sync_signature": f"{meta.rating_key}:{meta.file_size}",
    }
    if is_ep:
        mediatype = MEDIATYPE_TVSHOW
        video = {
            "show": meta.show_title,
            "subtitle": meta.title,
            "episode_id": f"S{meta.season_number:02d}E{meta.episode_number:02d}",
            "summary": meta.summary,
            "season": meta.season_number,
            "episode": meta.episode_number,
        }
    else:
        mediatype = MEDIATYPE_MOVIE
        video = {"summary": meta.summary}
    return track, mediatype, video


def materialize_video(server, meta, profile, on_phase=None, on_progress=None):
    """Ensure a local video file in the device's profile exists. Returns
    (local_path, ext_with_dot).

    *on_phase(name)* is called as work moves through "downloading"/"transcoding";
    *on_progress(frac)* receives a 0..1 fraction during the (slow) transcode so
    the caller can drive a live progress bar."""
    from .sync import should_transcode_video, transcode_video
    cache = config.load()["cache_dir"]
    rk = meta.rating_key
    orig_ext = "." + (meta.container or "mp4")
    orig = os.path.join(cache, f"v{rk}{orig_ext}")
    if not (os.path.exists(orig) and os.path.getsize(orig) > 0):
        if on_phase:
            on_phase("downloading")
        plex_client.download_part(server, meta.part_key, orig)
    if should_transcode_video(meta, profile) and have_ffmpeg():
        out = os.path.join(
            cache, f"v{rk}.{profile.max_w}x{profile.max_h}{profile.container}")
        if not (os.path.exists(out) and os.path.getsize(out) > 0):
            if on_phase:
                on_phase("transcoding")
            transcode_video(orig, out, profile,
                            duration_ms=meta.duration_ms, progress=on_progress)
        elif on_progress:
            on_progress(1.0)                  # cached → already done
        return out, profile.container
    return orig, orig_ext


def _video_selection(server, params) -> list:
    """Resolve the requested rating_keys into VideoMeta objects (movies and/or
    episodes), in the order requested."""
    metas = []
    for rk in params.get("rating_keys", []):
        item = plex_client.fetch_track(server, str(rk))
        if item is None:
            continue
        kind = getattr(item, "type", "")
        if kind == "episode":
            m = plex_client.episode_to_meta(item)
        elif kind == "movie":
            m = plex_client.movie_to_meta(item)
        else:
            m = None
        if m:
            metas.append(m)
    return metas


def _video_sync_job_inner(job, params):
    server = get_server()
    job["phase"] = "planning"
    job["message"] = "Checking the device…"
    device, cap, profile = _resolve_device_and_profile(params)
    if profile is None:
        raise RuntimeError(
            "This device can't play video iAmped can sync. Supported: video "
            "iPods (5G/5.5G, classic, nano 3G–5G), Creative Zen / MTP players, "
            "and generic USB players.")
    if not have_ffmpeg():
        raise RuntimeError(
            "Video sync needs ffmpeg to transcode. Install it and try again.")

    metas = _video_selection(server, params)
    if not metas:
        raise RuntimeError("None of the selected items have a playable file.")

    transport = cap.transport
    if transport == "mtp":
        from .sync import mtp
        backend = mtp.MTPBackend(params.get("mtp_busloc"), folder="Video")
        backend.prepare()
        existing = backend.existing_keys()
    elif transport == "ipod":
        backend = itunesdb.ITunesDBBackend(
            params["device_path"], params.get("device_name") or "iPod")
        backend.prepare()
        backend.import_existing()           # preserve everything already on it
        existing = set(backend._by_key)
    else:                                    # ums / mass storage
        from .sync import MassStorageBackend
        backend = MassStorageBackend(params["device_path"], cap.layout)
        backend.prepare()
        backend.import_existing()
        existing = set(backend._records)

    todo = [m for m in metas if m.rating_key not in existing]
    job["total"] = len(todo)
    job["done"] = 0
    job["item_progress"] = 0.0
    job["encoder"] = video_mod.hw_h264_encoder() or "libx264 (software)"
    job["phase"] = "syncing"
    added = 0
    for i, meta in enumerate(todo):
        label = (f"{meta.show_title} – {meta.title}"
                 if meta.kind == "episode" else meta.title)
        job["item_progress"] = 0.0

        def _phase(name, _label=label):
            job["message"] = (f"Downloading {_label}…" if name == "downloading"
                              else f"Transcoding {_label}…")

        def _prog(frac):
            job["item_progress"] = frac

        _phase("transcoding")                # default message before download check
        src, ext = materialize_video(server, meta, profile,
                                     on_phase=_phase, on_progress=_prog)
        job["message"] = f"Writing {label} to device…"
        track, mediatype, video = _video_track_dict(meta)
        backend.add_video(track, src, ext, mediatype, video)
        added += 1
        job["done"] = i + 1
        job["item_progress"] = 0.0
        job["message"] = label

    job["phase"] = "finalizing"
    backend.finalize()
    job["result"] = {
        "videos_added": added,
        "videos_skipped": len(metas) - len(todo),
        "transport": transport,
        "profile": profile.name,
    }
    job["message"] = (f"Synced {added} video(s) to the device"
                      + (f" ({len(metas) - len(todo)} already present)"
                         if len(metas) != len(todo) else "") + ".")


def _video_sync_job(job, params):
    if params.get("transport") == "mtp" or params.get("mtp_busloc"):
        busloc = params.get("mtp_busloc")
        lock = _mtp_lock(busloc)
        if not lock.acquire(blocking=False):
            raise RuntimeError("This MTP player is already being written by iAmped.")
        try:
            _video_sync_job_inner(job, params)
        finally:
            lock.release()
        return
    path = params["device_path"]
    dtype = "ipod" if params.get("device_type") == "ipod" else "massstorage"
    with device_management.device_lock(path, dtype):
        _video_sync_job_inner(job, params)


# --------------------------------------------------------------------------- routes
@app.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.route("/<path:fname>")
def static_files(fname):
    return send_from_directory(WEB_DIR, fname)


@app.get("/api/config")
def api_get_config():
    cfg = config.load()
    cfg["has_ffmpeg"] = have_ffmpeg()
    return jsonify(cfg)


@app.post("/api/config")
def api_set_config():
    return jsonify(config.save(request.json or {}))


@app.post("/api/connect")
def api_connect():
    data = request.json or {}
    baseurl = (data.get("baseurl") or "").strip()
    token = (data.get("token") or "").strip()
    if not baseurl or not token:
        return jsonify({
            "ok": False,
            "error": "Enter both a server URL and token, or use Sign in with Plex.",
        }), 400
    try:
        server = plex_client.connect(baseurl, token)
        result = _connection_result(server)
        config.save({"plex_baseurl": server._baseurl,
                     "plex_token": server._token})
        _state["server"] = server
        return jsonify(result)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/plex/oauth/start")
def api_plex_oauth_start():
    _expire_oauth_logins()
    cfg = config.load()
    client_id = cfg["plex_client_id"] or uuid.uuid4().hex
    if not cfg["plex_client_id"]:
        config.save({"plex_client_id": client_id})
    try:
        login = plex_client.start_oauth(client_id)
        auth_url = login.oauthUrl()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Could not start Plex sign-in: {exc}"}), 502

    login_id = uuid.uuid4().hex
    PLEX_OAUTH[login_id] = {
        "created": time.time(),
        "login": login,
        "resources": None,
    }
    # The desktop webview cannot reliably create an external popup on every
    # platform, so ask the OS to open Plex in the user's normal browser.
    try:
        webbrowser.open(auth_url, new=2)
    except Exception:
        pass
    return jsonify({
        "login_id": login_id,
        "auth_url": auth_url,
        "expires_in": PLEX_OAUTH_TTL,
    })


@app.get("/api/plex/oauth/status/<login_id>")
def api_plex_oauth_status(login_id):
    state = PLEX_OAUTH.get(login_id)
    if not state:
        return jsonify({
            "status": "expired",
            "error": "Plex sign-in expired. Start it again.",
        }), 404
    if time.time() - state["created"] >= PLEX_OAUTH_TTL:
        PLEX_OAUTH.pop(login_id, None)
        return jsonify({
            "status": "expired",
            "error": "Plex sign-in expired. Start it again.",
        }), 410

    if state["resources"] is not None:
        return jsonify({
            "status": "authorized",
            "servers": [_oauth_server_view(r)
                        for r in state["resources"].values()],
        })

    login = state["login"]
    try:
        if not login.checkLogin():
            return jsonify({"status": "pending"})
        account = plex_client.oauth_account(login.token)
        resources = plex_client.account_servers(account)
    except Exception as exc:  # noqa: BLE001
        PLEX_OAUTH.pop(login_id, None)
        return jsonify({
            "status": "error",
            "error": f"Plex sign-in failed: {exc}",
        }), 502

    if not resources:
        PLEX_OAUTH.pop(login_id, None)
        return jsonify({
            "status": "error",
            "error": "No Plex Media Servers are available to this account.",
        }), 404
    state["resources"] = {r.clientIdentifier: r for r in resources}
    return jsonify({
        "status": "authorized",
        "servers": [_oauth_server_view(r) for r in resources],
    })


@app.post("/api/plex/oauth/connect")
def api_plex_oauth_connect():
    data = request.json or {}
    login_id = data.get("login_id")
    server_id = data.get("server_id")
    state = PLEX_OAUTH.get(login_id)
    if not state or state["resources"] is None:
        return jsonify({"error": "Plex sign-in is not ready or has expired."}), 400
    resource = state["resources"].get(server_id)
    if resource is None:
        return jsonify({"error": "Select an available Plex Media Server."}), 400
    try:
        server = plex_client.connect_resource(resource)
        result = _store_server(server)
    except Exception as exc:  # noqa: BLE001
        return jsonify({
            "error": f"Could not connect to {resource.name}: {exc}",
        }), 502
    PLEX_OAUTH.pop(login_id, None)
    return jsonify(result)


@app.post("/api/library/build")
def api_build():
    section = (request.json or {}).get("section") or config.load()["music_section"]
    if not section:
        return jsonify({"error": "No music section selected."}), 400
    config.save({"music_section": section})
    return jsonify({"job": start_job(_build_library_job, section)})


@app.get("/api/library/stats")
def api_stats():
    return jsonify(_lib().stats())


@app.get("/api/playlists")
def api_playlists():
    return jsonify(_lib().all_playlists())


def _track_view(t: dict) -> dict:
    return {
        "rating_key": t["rating_key"], "title": t.get("title"),
        "artist": t.get("artist"), "album": t.get("album"),
        "plays": t.get("view_count") or 0, "rating": t.get("user_rating"),
        "duration_ms": t.get("duration_ms") or 0,
        "size": t.get("file_size") or 0, "container": t.get("container"),
        "lossless": filler.is_lossless(t),
    }


@app.get("/api/tracks")
def api_tracks():
    a = request.args
    res = _lib().browse_tracks(
        search=a.get("search", ""), sort=a.get("sort", "artist"),
        offset=int(a.get("offset", 0)), limit=int(a.get("limit", 200)))
    return jsonify({"total": res["total"],
                    "tracks": [_track_view(t) for t in res["tracks"]]})


@app.get("/api/playlist/<path:pid>/tracks")
def api_playlist_tracks(pid):
    lib = _lib()
    keys = lib.playlist_track_keys_any(pid)
    rows = lib.get_tracks(keys)
    tracks = [_track_view(rows[rk]) for rk in keys if rk in rows]
    return jsonify({"total": len(tracks), "tracks": tracks})


@app.post("/api/playlist/sonic")
def api_playlist_sonic():
    d = request.json or {}
    seed = d.get("rating_key")
    if not seed:
        return jsonify({"error": "No seed track."}), 400
    try:
        server = get_server()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400
    metas, err = plex_client.sonically_similar(server, seed, int(d.get("limit", 50)))
    if err and len(metas) <= 1:
        return jsonify({"error": err}), 400
    lib = _lib()
    lib.upsert_tracks(metas)                     # make sure they're cached locally
    seed_meta = metas[0]
    title = d.get("title") or f"Sonic: {seed_meta.title}"
    pid = lib.create_local_playlist(
        title, "sonic", int(time.time()), json.dumps({"seed": seed}),
        [m.rating_key for m in metas])
    return jsonify({"ok": True, "id": f"local:{pid}", "title": title,
                    "count": len(metas), "warning": err})


def _bound_and_save(d: dict, metas: list, kind: str, title: str,
                    rules: dict) -> dict:
    """Shared tail for the radio/station builders: size a generated track list
    to a count/size/device-fraction budget, cache it, and persist a local
    playlist that /api/sync can write to a device."""
    import dataclasses
    fmt = target_format(d.get("device_type", "ipod"))
    tl = bool(d.get("transcode_lossless", True))
    bitrate_k = int(d.get("target_bitrate_k")
                    or (256 if fmt == "aac" else 320))
    max_tracks = int(d["max_tracks"]) if d.get("max_tracks") else None
    max_bytes = int(float(d["max_mb"]) * 1024 * 1024) if d.get("max_mb") else None
    # "fit a part of the iPod": cap by a fraction of the device's free space
    if d.get("device_path") and os.path.isdir(d["device_path"]):
        fit = int(free_bytes(d["device_path"]) * float(d.get("fraction", 1.0)))
        max_bytes = min(max_bytes, fit) if max_bytes else fit

    meta_by_key = {m.rating_key: m for m in metas}
    bounded = filler.bound_tracks([dataclasses.asdict(m) for m in metas],
                                  max_tracks, max_bytes, tl, fmt, bitrate_k)
    keys = [t["rating_key"] for t in bounded["tracks"]]
    lib = _lib()
    lib.upsert_tracks([meta_by_key[k] for k in keys])   # cache for the sync
    pid = lib.create_local_playlist(title, kind, int(time.time()),
                                    json.dumps(rules), keys)
    preview = [{"artist": t.get("artist"), "title": t.get("title"),
                "album": t.get("album"), "duration_ms": t.get("duration_ms"),
                "size": filler.device_size(t, tl, fmt, bitrate_k)}
               for t in bounded["tracks"][:300]]
    return {"ok": True, "id": f"local:{pid}", "title": title,
            "count": len(keys), "total_bytes": bounded["total_bytes"],
            "fetched": len(metas), "dropped": bounded["dropped"],
            "preview": preview}


@app.post("/api/playlist/radio")
def api_playlist_radio():
    """Generate an 'artist radio' from Plex and size it to fit a device (or a
    portion of one). Body: artist, method ('station'|'sonic'), max_distance
    (0..1 familiar↔discovery), and any of max_tracks / max_mb / (device_path +
    fraction). Saves a local playlist a later /api/sync can write to the iPod."""
    d = request.json or {}
    name = (d.get("artist") or "").strip()
    if not name:
        return jsonify({"error": "No artist given."}), 400
    try:
        server = get_server()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400

    section = config.load()["music_section"]
    artist = plex_client.find_artist(server, section, name)
    if artist is None:
        return jsonify({"error": f"Artist “{name}” not found in Plex."}), 404
    md = float(d["max_distance"]) if d.get("max_distance") is not None else None
    metas, warn = plex_client.artist_radio(
        server, artist, d.get("method", "station"), int(d.get("fetch", 200)), md)
    if not metas:
        return jsonify({"error": warn or "Plex returned no radio tracks."}), 400

    title = d.get("title") or f"{artist.title} Radio"
    resp = _bound_and_save(d, metas, "radio", title,
                           {"artist": artist.title,
                            "method": d.get("method", "station"),
                            "max_distance": md})
    resp["artist"] = artist.title
    resp["warning"] = warn
    return jsonify(resp)


@app.get("/api/stations")
def api_stations():
    """List the library's built-in radio stations (Plexamp's Stations menu)."""
    try:
        server = get_server()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400
    section = config.load()["music_section"]
    return jsonify({"stations": plex_client.list_stations(server, section)})


@app.post("/api/playlist/station")
def api_playlist_station():
    """Materialize one of the library's built-in stations (Library Radio, Deep
    Cuts, Time Travel, Random Album, …) into a device-sized playlist. Body:
    station (title), plus the same budget fields as /api/playlist/radio."""
    d = request.json or {}
    station = (d.get("station") or "").strip()
    if not station:
        return jsonify({"error": "No station given."}), 400
    try:
        server = get_server()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400
    section = config.load()["music_section"]
    metas, warn = plex_client.station_tracks(
        server, section, station, int(d.get("fetch", 200)))
    if not metas:
        return jsonify({"error": warn or "Station returned no tracks."}), 400
    title = d.get("title") or station
    resp = _bound_and_save(d, metas, "station", title, {"station": station})
    resp["warning"] = warn
    return jsonify(resp)


@app.post("/api/playlist/local")
def api_playlist_local():
    d = request.json or {}
    lib = _lib()
    title = d.get("title") or "New Playlist"
    keys = d.get("rating_keys", [])
    pid = lib.create_local_playlist(title, "manual", int(time.time()), "", keys)
    return jsonify({"ok": True, "id": f"local:{pid}", "title": title,
                    "count": len(keys)})


@app.post("/api/playlist/local/<int:pid>/add")
def api_playlist_local_add(pid):
    d = request.json or {}
    _lib().add_to_local_playlist(pid, d.get("rating_keys", []))
    return jsonify({"ok": True})


@app.delete("/api/playlist/local/<int:pid>")
def api_playlist_local_delete(pid):
    _lib().delete_local_playlist(pid)
    return jsonify({"ok": True})


_NATIVE_STREAM = {"mp3", "m4a", "aac", "mp4", "ogg", "oga", "opus", "wav", "wma"}


@app.get("/api/stream/<rk>")
def api_stream(rk):
    lib = _lib()
    tr = lib.get_tracks([rk]).get(rk)
    if not tr:
        return jsonify({"error": "unknown track"}), 404
    try:
        server = get_server()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400
    url = plex_client.stream_url(server, tr["part_key"])
    native = (tr.get("container") or "").lower() in _NATIVE_STREAM
    try:
        start = max(0.0, float(request.args.get("start", "0") or 0))
    except ValueError:
        return jsonify({"error": "invalid start time"}), 400

    if (native and not start) or not have_ffmpeg():
        # proxy the original, forwarding Range so the browser can seek
        req_range = request.headers.get("Range")
        headers = {"Range": req_range} if req_range else {}
        up = server._session.get(url, headers=headers, stream=True, timeout=60)
        resp = Response(up.iter_content(65536), status=up.status_code)
        for h in ("Content-Type", "Content-Length", "Accept-Ranges", "Content-Range"):
            if h in up.headers:
                resp.headers[h] = up.headers[h]
        resp.headers.setdefault("Content-Type", "audio/mpeg")
        return resp

    # transcode lossless -> mp3 on the fly for in-browser playback
    command = ["ffmpeg", "-loglevel", "error"]
    if start:
        command.extend(["-ss", f"{start:.3f}"])
    command.extend(
        ["-i", url, "-map", "0:a:0",
         "-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3", "-"],
    )
    proc = subprocess.Popen(command, stdout=subprocess.PIPE)

    def gen():
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            proc.kill()
    return Response(gen(), mimetype="audio/mpeg")


@app.get("/api/volumes")
def api_volumes():
    # MTP scanning is slow and seizes the device, so it's opt-in via ?mtp=1
    # (a user-triggered "scan for MTP players"), never on the hot poll path.
    from .devices import capabilities, list_devices
    include_mtp = request.args.get("mtp") in ("1", "true", "yes")
    vols = []
    for d in list_devices(include_mtp=include_mtp):
        cap = capabilities.classify(d)
        path = d.mountpoint or d.raw_path or d.id
        vols.append({
            "path": path, "name": d.name, "total": d.total, "free": d.free,
            "is_ipod": d.is_ipod, "fs": d.fs, "mounted": d.mounted,
            "writable": d.writable, "ipod_format": d.ipod_format,
            "ipod_model": d.ipod_model, "ipod_generation": d.ipod_generation,
            "needs_conversion": d.needs_conversion, "raw_path": d.raw_path,
            "note": d.note, "model": d.model,
            "transport": cap.transport, "layout": cap.layout,
            "capability_reason": cap.reason, "mtp_busloc": d.mtp_busloc,
            "device_id": device_management.device_id(
                path, "ipod" if d.is_ipod else "massstorage"),
        })
    return jsonify(vols)


@app.get("/api/freespace")
def api_freespace():
    path = request.args.get("path", "")
    ok = bool(path) and os.path.isdir(path)
    return jsonify({"ok": ok, "free": free_bytes(path) if ok else 0})


@app.post("/api/plan")
def api_plan():
    p = request.json or {}
    if not os.path.isdir(p.get("device_path", "")):
        return jsonify({"error": "Device path does not exist."}), 400
    config.save({
        "last_device_path": p["device_path"],
        "last_device_type": p.get("device_type", "massstorage"),
        "reserve_mb": int(p.get("reserve_mb", 200)),
        "fill_strategy": p.get("fill_strategy", "most_played"),
        "transcode_lossless": bool(p.get("transcode_lossless", True)),
        f"{target_format(p.get('device_type', 'massstorage'))}_bitrate_k":
            int(p.get("target_bitrate_k") or (
                256 if p.get("device_type") == "ipod" else 320)),
        "sync_artwork": bool(p.get("sync_artwork", True)),
        "mirror": bool(p.get("mirror", True)),
    })
    diff = _device_plan(p)
    plan = diff["plan"]
    preview = [{
        "rating_key": str(t.get("rating_key")),
        "artist": t.get("artist"), "title": t.get("title"),
        "album": t.get("album"), "size": t.get("_size"),
        "views": t.get("view_count"), "rating": t.get("user_rating"),
        "lossless": filler.is_lossless(t),
    } for t in plan["tracks"][:300]]
    review = []
    updates = set(diff["update_keys"])
    for track in diff["add_tracks"]:
        key = str(track["rating_key"])
        action = "update" if key in updates else "add"
        review.append({
            "id": f"{action}:{key}", "action": action, "checked": True,
            "rating_key": key, "title": track.get("title"),
            "artist": track.get("artist"), "album": track.get("album"),
            "size": int(track.get("_size") or 0),
        })
    for record in diff["removals"]:
        key = str(record.get("rating_key"))
        if key in updates:
            continue
        review.append({
            "id": f"remove:{key}", "action": "remove", "checked": True,
            "rating_key": key, "title": record.get("title"),
            "artist": record.get("artist"), "album": record.get("album"),
            "size": int(record.get("size") or 0),
        })
    transfer_options = _bitrate_options(
        p, plan["budget_bytes"]) if p.get("transfer_request") else []
    return jsonify({
        "track_count": len(diff["add_tracks"]),
        "update_count": len(diff["update_keys"]),
        "add_count": len(diff["add_tracks"]) - len(diff["update_keys"]),
        "desired_track_count": plan["track_count"],
        "keep_count": len(diff["keep_tracks"] or {}),
        "remove_count": len(diff["removals"]),
        "remove_bytes": sum(int(r.get("size") or 0) for r in diff["removals"]),
        "mirror": diff["mirror"],
        "pending_transaction": diff["pending_transaction"],
        "total_bytes": plan["total_bytes"],
        "budget_bytes": plan["budget_bytes"],
        "capacity_bytes": plan["capacity_bytes"],
        "reserve_bytes": plan["reserve_bytes"],
        "skipped_for_space": plan["skipped_for_space"],
        "skipped_for_limit": plan.get("skipped_for_limit", 0),
        "requested_track_count": plan.get(
            "requested_track_count", plan["track_count"]),
        "requested_bytes": plan.get("requested_bytes", plan["total_bytes"]),
        "target_bitrate_k": plan.get("target_bitrate_k"),
        "target_format": target_format(p.get("device_type", "massstorage")),
        "bitrate_options": transfer_options,
        "playlists": [{"title": pl["title"], "count": len(pl["track_keys"]),
                       "requested": pl["requested"]} for pl in plan["playlists"]],
        "preview": preview,
        "review": review,
    })


@app.post("/api/sync")
def api_sync():
    p = request.json or {}
    # MTP players have no filesystem path — they're addressed by bus location.
    if p.get("device_type") == "mtp" or p.get("transport") == "mtp" \
            or p.get("mtp_busloc"):
        if not p.get("mtp_busloc"):
            return jsonify({"error": "No MTP player selected."}), 400
        return jsonify({"job": start_job(_mtp_sync_job, p)})
    if not os.path.isdir(p.get("device_path", "")):
        return jsonify({"error": "Device path does not exist."}), 400
    return jsonify({"job": start_job(_sync_job, p)})


@app.get("/api/video/sections")
def api_video_sections():
    try:
        server = get_server()
        return jsonify({"sections": plex_client.video_sections(server)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400


@app.get("/api/video/items")
def api_video_items():
    section = request.args.get("section", "")
    kind = request.args.get("kind", "movie")
    if not section:
        return jsonify({"error": "No video library selected."}), 400
    try:
        server = get_server()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400
    if kind == "show":
        return jsonify({"kind": "show",
                        "items": plex_client.list_shows(server, section)})
    items = [{
        "rating_key": m.rating_key, "title": m.title, "year": m.year,
        "duration_ms": m.duration_ms, "thumb": m.thumb,
        "width": m.width, "height": m.height, "size": m.file_size,
        "video_codec": m.video_codec, "container": m.container,
    } for m in plex_client.iter_movies(server, section)]
    return jsonify({"kind": "movie", "items": items})


@app.get("/api/video/episodes")
def api_video_episodes():
    show_key = request.args.get("show_key", "")
    if not show_key:
        return jsonify({"error": "No show selected."}), 400
    try:
        server = get_server()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400
    eps = [{
        "rating_key": m.rating_key, "title": m.title,
        "show_title": m.show_title, "season_number": m.season_number,
        "episode_number": m.episode_number, "duration_ms": m.duration_ms,
        "thumb": m.thumb, "size": m.file_size, "video_codec": m.video_codec,
        "width": m.width, "height": m.height,
    } for m in plex_client.iter_episodes(server, show_key)]
    return jsonify({"items": eps})


@app.get("/api/video/thumb")
def api_video_thumb():
    key = request.args.get("key", "")
    if not key:
        return Response(status=404)
    try:
        server = get_server()
        from urllib.parse import quote
        url = server.url(
            f"/photo/:/transcode?width=320&height=480&minSize=1&upscale=0"
            f"&url={quote(key, safe='')}", includeToken=True)
        r = server._session.get(url, timeout=30)
        r.raise_for_status()
        return Response(r.content,
                        content_type=r.headers.get("Content-Type", "image/jpeg"))
    except Exception:  # noqa: BLE001
        return Response(status=404)


@app.get("/api/video/device")
def api_video_device():
    """Whether the currently-selected device can receive video, for the UI to
    gate the Video tab's sync button."""
    params = {k: request.args.get(k, "") for k in
              ("device_path", "device_type", "mtp_busloc", "transport",
               "ipod_generation")}
    _device, cap, profile = _resolve_device_and_profile(params)
    return jsonify({
        "video_support": profile is not None,
        "transport": cap.transport,
        "profile": profile.name if profile else None,
        "reason": (None if profile else
                   "This device can't play video iAmped can sync."),
    })


@app.post("/api/video/sync")
def api_video_sync():
    p = request.json or {}
    if not p.get("rating_keys"):
        return jsonify({"error": "Select at least one movie or episode."}), 400
    if p.get("transport") == "mtp" or p.get("mtp_busloc"):
        if not p.get("mtp_busloc"):
            return jsonify({"error": "No MTP player selected."}), 400
    elif not os.path.isdir(p.get("device_path", "")):
        return jsonify({"error": "Device path does not exist."}), 400
    return jsonify({"job": start_job(_video_sync_job, p)})


@app.get("/api/device/videos")
def api_device_videos():
    """Videos already on a device (iPod iTunesDB or a USB Video/ tree), so the
    UI can list and manage what was synced over."""
    a = request.args
    path = a.get("device_path", "")
    dtype = a.get("device_type", "ipod")
    if not os.path.isdir(path):
        return jsonify({"error": "Device path does not exist."}), 400
    try:
        inv = inventory.read_device_library(path, dtype)
    except Exception as exc:  # noqa: BLE001 - surfaced to UI
        return jsonify({"error": str(exc)}), 400
    vids = [t for t in inv.get("tracks", []) if t.get("media") == "video"]
    return jsonify({
        "device_type": dtype, "device_path": path,
        "count": len(vids),
        "total_bytes": sum(t.get("size") or 0 for t in vids),
        "videos": vids,
    })


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


def _remove_massstorage_videos(path: str, rels: set) -> dict:
    from .sync import MassStorageBackend
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


@app.post("/api/device/video/remove")
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
                res = _remove_massstorage_videos(path, rels)
    except Exception as exc:  # noqa: BLE001 - surfaced to UI
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, **res})


@app.get("/api/device/profile")
def api_device_profile():
    path = request.args.get("device_path", "")
    dtype = request.args.get("device_type", "massstorage")
    if not os.path.isdir(path):
        return jsonify({"error": "Device path does not exist."}), 400
    return jsonify(device_management.get_profile(path, dtype))


@app.put("/api/device/profile")
def api_device_profile_save():
    p = request.json or {}
    if not os.path.isdir(p.get("device_path", "")):
        return jsonify({"error": "Device path does not exist."}), 400
    return jsonify(device_management.save_profile(
        p["device_path"], p.get("device_type", "massstorage"), p))


@app.get("/api/device/backups")
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


@app.post("/api/device/restore")
def api_device_restore():
    p = request.json or {}
    if not os.path.isdir(p.get("device_path", "")):
        return jsonify({"error": "Device path does not exist."}), 400
    if not p.get("backup_id"):
        return jsonify({"error": "Choose a backup."}), 400
    return jsonify({"job": start_job(_restore_job, p)})


@app.post("/api/device/eject")
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


@app.get("/api/device/matches")
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


@app.get("/api/device/inventory")
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


@app.post("/api/device/readback")
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


@app.post("/api/device/readback/apply")
def api_readback_apply():
    p = request.json or {}
    if not p.get("plan"):
        return jsonify({"error": "Nothing to apply."}), 400
    return jsonify({"job": start_job(_readback_apply_job, p)})


@app.get("/api/job/<jid>")
def api_job(jid):
    job = JOBS.get(jid)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(job)


def create_app():
    config.ensure_dirs()
    return app
