"""Music sync: plan/diff building, materialization, and the sync jobs."""
from __future__ import annotations

import os
import threading
import time

from flask import Blueprint, jsonify, request

from .. import artwork, cache, config, device_management, filler, plex_client
from ..library import Library
from ..server import _fmt_bytes, _lib, get_server, start_job
from ..sync import (BACKENDS, free_bytes, target_format, total_bytes,
                    transcode)
from ..sync import device_state
from ..sync.base import aac_codec, have_ffmpeg

bp = Blueprint("syncing", __name__)


def _ext_for(track: dict) -> str:
    c = (track.get("container") or "").lower()
    if c:
        return "." + c
    e = os.path.splitext(track.get("server_file") or "")[1]
    return e or ".mp3"


# Per-artifact locks make materialization safe to run from several places at
# once (a sync job, the plan prefetcher, the playback cache) — without them two
# threads could write the same .part file. The ffmpeg semaphore separately
# bounds concurrent transcodes to the CPU so a wide download pool can't
# oversubscribe the encoder.
_MAT_LOCKS: dict[str, threading.Lock] = {}
_MAT_GUARD = threading.Lock()
_TRANSCODE_SEM = threading.BoundedSemaphore(max(2, os.cpu_count() or 4))


def _mat_lock(key: str) -> threading.Lock:
    with _MAT_GUARD:
        return _MAT_LOCKS.setdefault(key, threading.Lock())


def _materialize(server, lib: Library, track: dict, transcode_lossless: bool,
                 fmt: str, bitrate_k: int | None = None):
    """Ensure a local audio file exists in the device's target format. Returns
    (local_path, ext_with_dot, download_secs, transcode_secs). `fmt` is 'aac'
    (iPod) or 'mp3' (USB). A source already in a device-playable format at an
    acceptable bitrate is passed through untouched (no ffmpeg) — see
    ``filler.should_transcode``; only lossless (FLAC/ALAC) or over-bitrate
    sources are re-encoded."""
    cache = config.load()["cache_dir"]
    rk = track["rating_key"]
    orig_ext = _ext_for(track)
    orig = os.path.join(cache, f"{rk}{orig_ext}")
    dl_s = 0.0
    with _mat_lock(f"dl:{rk}"):
        if not (os.path.exists(orig) and os.path.getsize(orig) > 0):
            t0 = time.perf_counter()
            plex_client.download_part(server, track["part_key"], orig)
            dl_s = time.perf_counter() - t0
            lib.set_cached(rk, orig, os.path.getsize(orig))

    if filler.should_transcode_to(
            track, fmt, transcode_lossless, bitrate_k) and have_ffmpeg():
        ext = ".mp3" if fmt == "mp3" else ".m4a"
        # Tag the cache key with the AAC encoder so an encoder change (e.g.
        # native ``aac`` → Apple ``aac_at`` for iPod compatibility) re-encodes
        # instead of silently reusing an older, differently-encoded file.
        enc = f".{aac_codec()}" if fmt == "aac" else ""
        output_base = os.path.join(
            cache, f"{rk}.{fmt}.{bitrate_k or 0}k{enc}")
        out = output_base + ext
        tx_s = 0.0
        with _mat_lock(f"tx:{rk}:{fmt}:{bitrate_k or 0}"):
            if not (os.path.exists(out) and os.path.getsize(out) > 0):
                t0 = time.perf_counter()
                try:
                    with _TRANSCODE_SEM:
                        transcode(orig, output_base, fmt, bitrate_k)
                except Exception:
                    return orig, orig_ext, dl_s, 0.0   # fall back to the original
                tx_s = time.perf_counter() - t0
        return out, ext, dl_s, tx_s
    return orig, orig_ext, dl_s, 0.0               # passthrough — no re-encode


def materialize(server, lib: Library, track: dict, transcode_lossless: bool,
                fmt: str, bitrate_k: int | None = None):
    """(local_path, ext_with_dot) — thin wrapper over :func:`_materialize`."""
    path, ext, _dl, _tx = _materialize(
        server, lib, track, transcode_lossless, fmt, bitrate_k)
    return path, ext


def _resolve_fmt(dtype: str, params: dict) -> str:
    """Target audio format for this device. iPods default to AAC but can opt
    into ALAC (Apple Lossless) via ``ipod_format``; mass storage stays MP3."""
    if dtype == "ipod":
        choice = str(params.get("ipod_format")
                     or config.load().get("ipod_format") or "aac").lower()
        if choice in ("aac", "alac"):
            return choice
    return target_format(dtype)


def _sync_timing_note(timing: dict, n: int, workers: int) -> str:
    """Summarize where a sync spent its time — turns 'it's slow' into a
    diagnosis. Download/transcode are wall-clock summed across worker threads
    (so they exceed real elapsed time by ~the worker count); device write is
    serial and real. Whichever dwarfs the others is the true bottleneck:
    download → the Plex link, transcode → CPU/ffmpeg, device → the iPod/USB bus.
    Returned so it can ride along in the sync result the UI shows; also logged
    to stdout for a terminal run."""
    mb = timing["bytes"] / (1024 * 1024)
    dl, tx, dev = timing["download"], timing["transcode"], timing["device"]
    dl_mbps = (mb / dl) if dl else 0.0
    note = (
        f"{n} tracks via {workers} workers — "
        f"download {dl:.0f}s ({dl_mbps:.1f} MB/s aggregate), "
        f"transcode {tx:.0f}s, device write {dev:.0f}s, {mb:.0f} MB")
    print(f"[sync] {note}", flush=True)
    return note


def _resolve_capability(params: dict):
    """Auto-detect the transport + on-disk layout for the target device, with a
    saved/explicit ``transport``/``layout`` override taking precedence.

    Detection mirrors how MediaMonkey/iTunes pick a transport without asking:
    iPods use the iTunes DB, MTP players are handed indexed tracks, flat-scan
    USB players (Creative MuVo) get a one-level layout, and everything else
    defaults to flat for maximum compatibility.
    """
    from ..devices import capabilities, list_devices
    override = {k: params[k] for k in ("transport", "layout")
               if params.get(k) in (
                   capabilities.LAYOUT_FLAT, capabilities.LAYOUT_NESTED,
                   capabilities.TRANSPORT_UMS, capabilities.TRANSPORT_MTP,
                   capabilities.TRANSPORT_IPOD)}
    path = params.get("device_path")
    busloc = params.get("mtp_busloc")
    sd_on = bool(config.load().get("sd_card_enabled"))
    try:
        for dev in list_devices(include_raw=False):
            if (dev.mountpoint and dev.mountpoint == path) or \
                    (busloc and dev.mtp_busloc == busloc):
                return capabilities.resolve(dev, override, sd_mode=sd_on)
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
    from .. import inventory
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
    fmt = _resolve_fmt(dtype, params)
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
    # Tag transcoded output with the AAC encoder so switching encoders (native
    # ``aac`` → Apple ``aac_at``) counts as a content change: mirror-mode syncs
    # then re-copy the improved file instead of treating the on-device track as
    # already current.
    transcode_tag = f"{fmt}:{bitrate_k}k" + (
        f":{aac_codec()}" if fmt == "aac" else "")
    for track in plan["tracks"]:
        output = transcode_tag if filler.should_transcode_to(
            track, fmt, transcode_lossless, bitrate_k) \
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


# --------------------------------------------------------------------------- prefetch
# While the user reads the plan preview and ticks checkboxes, the network and
# CPU are idle — so /api/plan kicks off a background warm-up that materializes
# the planned tracks into the cache. When the user clicks Sync, most files are
# already local and the sync collapses to device writes. A monotonically
# increasing token supersedes the warm-up whenever a newer plan or a real sync
# arrives; the per-artifact locks above make overlap with the sync harmless.
_PREFETCH = {"token": 0}


def _cancel_prefetch() -> int:
    _PREFETCH["token"] += 1
    return _PREFETCH["token"]


def _prefetch_job(job, params, token):
    server = get_server()
    lib = _lib()
    diff = _device_plan(params)
    tracks = diff["add_tracks"]
    dtype = params.get("device_type", "massstorage")
    fmt = _resolve_fmt(dtype, params)
    tl = bool(params.get("transcode_lossless", True))
    bitrate_k = int(params.get("target_bitrate_k")
                    or (256 if fmt == "aac" else 320))
    job["total"] = len(tracks)
    job["message"] = "Warming the cache for the planned sync…"

    from concurrent.futures import ThreadPoolExecutor
    done = 0

    def warm(tr):
        if _PREFETCH["token"] != token:
            return False                     # superseded — stop quietly
        _materialize(server, lib, tr, tl, fmt, bitrate_k)
        return True

    workers = max(1, min(len(tracks), max(4, os.cpu_count() or 4)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for ok in pool.map(warm, tracks):
            if not ok:
                break
            done += 1
            job["done"] = done
            job["message"] = f"Prepared {done}/{len(tracks)} track(s) ahead of sync"
    job["result"] = {"prepared": done, "of": len(tracks),
                     "superseded": _PREFETCH["token"] != token}


def _start_prefetch(params: dict) -> str | None:
    if not config.load().get("prefetch_on_plan", True):
        return None
    token = _cancel_prefetch()
    return start_job(_prefetch_job, dict(params), token)


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
    fmt = _resolve_fmt(dtype, params)
    bitrate_k = int(params.get("target_bitrate_k")
                    or (256 if fmt == "aac" else 320))
    want_artwork = bool(params.get("sync_artwork", True))
    # Downloading from Plex and transcoding are the slow, independent parts; the
    # actual copy onto the iPod/USB must stay serial (the iTunesDB and file tree
    # aren't written concurrently). So prefetch materialization in a thread pool
    # and consume the results in order — the network/CPU work for track N+k
    # overlaps the device write for track N.
    #
    # Two separate pools: a wide one for the heavy download+transcode work,
    # sized to the CPU (ffmpeg and downloads both release the GIL, so one job
    # per core keeps every core busy), and a small dedicated one for cover art.
    # Art must NOT share the audio pool — it's submitted after the audio jobs,
    # so it would queue behind all of them and stall the serial writer, which
    # blocks on each track's art before it can copy.
    from concurrent.futures import ThreadPoolExecutor

    workers = max(1, min(len(remaining), max(4, os.cpu_count() or 4)))
    timing = {"download": 0.0, "transcode": 0.0, "device": 0.0, "bytes": 0}
    with ThreadPoolExecutor(max_workers=workers) as pool, \
            ThreadPoolExecutor(max_workers=4) as art_pool:
        futures = [pool.submit(
            _materialize, server, lib, tr, tl, fmt, bitrate_k)
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
                future = art_pool.submit(artwork.materialize, server, tr)
                album_futures[album_id] = future
            art_futures.append(future)
        base_done = job["done"]
        for i, tr in enumerate(remaining):
            src, ext, dl_s, tx_s = futures[i].result()
            timing["download"] += dl_s
            timing["transcode"] += tx_s
            art_path = art_futures[i].result() if art_futures[i] else None
            t0 = time.perf_counter()
            record = backend.add_track(tr, src, ext, art_path)
            timing["device"] += time.perf_counter() - t0
            timing["bytes"] += int(record.get("size") or 0)
            device_state.record_completed(path, dtype, tx, str(tr["rating_key"]), record)
            job["done"] = base_done + i + 1
            job["message"] = f"{tr.get('artist','')} – {tr.get('title','')}"
    timing_note = _sync_timing_note(timing, len(remaining), workers) \
        if remaining else ""
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
    # Sweep files the freshly-written database doesn't reference — junk from
    # interrupted or older-build syncs that the iPod counts as "Other" and that
    # would otherwise eat capacity forever. Safe: every real file (ours and
    # foreign) is staged into the DB by import_existing/add_track above.
    swept = backend.sweep_orphans() if is_ipod else {"removed": 0, "freed_bytes": 0}
    removed = device_state.finish_cleanup(path, dtype, tx)
    # Bound the local download/transcode cache now that this sync's files are
    # safely on the device (no-op in aggressive mode).
    cache.enforce_limit()
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
        "timing": timing_note,
        "orphans_removed": swept["removed"],
        "orphans_freed_bytes": swept["freed_bytes"],
    }
    job["message"] = (
        f"Added {added} new track(s)"
        + (f", updated {updated}" if updated else "")
        + (f", preserved {carried} already on the device" if carried else "")
        + (f", removed {removed + recovered} stale track(s)" if removed + recovered else "")
        + (f", reclaimed {_fmt_bytes(swept['freed_bytes'])} of 'Other' junk"
           if swept["removed"] else "")
        + f" → {carried + len(tracks)} total on {params['device_path']}."
        + (f"  ⏱ {timing_note}." if timing_note else ""))


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
    from ..sync import mtp
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


# --------------------------------------------------------------------------- routes
@bp.get("/api/volumes")
def api_volumes():
    # MTP scanning is slow and seizes the device, so it's opt-in via ?mtp=1
    # (a user-triggered "scan for MTP players"), never on the hot poll path.
    from ..devices import capabilities, list_devices
    include_mtp = request.args.get("mtp") in ("1", "true", "yes")
    # SD-card mode (File → Enable SD card) is a hidden opt-in: without it, plain
    # cards/drives stay out of the device list. Honour the request flag, else
    # the saved setting, so a fresh toggle takes effect immediately.
    sd_on = (request.args.get("sd") in ("1", "true", "yes")
             or bool(config.load().get("sd_card_enabled")))
    vols = []
    for d in list_devices(include_mtp=include_mtp):
        if not sd_on and capabilities.is_plain_volume(d):
            continue  # hidden until the user enables SD-card mode
        cap = capabilities.classify(d, sd_mode=sd_on)
        path = d.mountpoint or d.raw_path or d.id
        vols.append({
            "path": path, "name": d.name, "total": d.total, "free": d.free,
            "is_ipod": d.is_ipod, "is_sd": d.is_sd, "fs": d.fs,
            "mounted": d.mounted,
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


@bp.get("/api/freespace")
def api_freespace():
    path = request.args.get("path", "")
    ok = bool(path) and os.path.isdir(path)
    return jsonify({"ok": ok, "free": free_bytes(path) if ok else 0})


@bp.post("/api/plan")
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
        **({"ipod_format": p["ipod_format"]}
           if p.get("device_type") == "ipod" and p.get("ipod_format")
           in ("aac", "alac") else {}),
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
    # Start warming the cache while the user reviews the plan — unless an
    # interrupted transaction is pending (the user must resolve that first).
    prefetch = None
    if diff["add_tracks"] and not diff["pending_transaction"]:
        prefetch = _start_prefetch(p)
    return jsonify({
        "prefetch_job": prefetch,
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


@bp.post("/api/sync")
def api_sync():
    p = request.json or {}
    # A real sync owns the pools now; any plan warm-up yields to it. Files it
    # already prepared stay in the cache and are picked up as instant hits.
    _cancel_prefetch()
    # MTP players have no filesystem path — they're addressed by bus location.
    if p.get("device_type") == "mtp" or p.get("transport") == "mtp" \
            or p.get("mtp_busloc"):
        if not p.get("mtp_busloc"):
            return jsonify({"error": "No MTP player selected."}), 400
        return jsonify({"job": start_job(_mtp_sync_job, p)})
    if not os.path.isdir(p.get("device_path", "")):
        return jsonify({"error": "Device path does not exist."}), 400
    return jsonify({"job": start_job(_sync_job, p)})


from .. import server as _server  # noqa: E402  (self-registration; see api/__init__)
_server.app.register_blueprint(bp)
