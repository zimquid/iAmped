"""Video (movies / TV) browsing and device sync routes."""
from __future__ import annotations

import os

from flask import Blueprint, Response, jsonify, request

from .. import config, device_management, inventory, plex_client, providers
from ..server import get_server, start_job
from ..sync import itunesdb
from ..sync import video as video_mod
from ..sync.base import have_ffmpeg
# Runtime attribute access (not `from .syncing import …`): keeps this module
# importable while iamped.api.syncing is itself mid-import.
from . import syncing

bp = Blueprint("video", __name__)


def _plex_server():
    """Video sync remains Plex-only until a provider exposes video metadata."""
    connection = providers.normalize(get_server())
    if connection.kind != "plex":
        raise RuntimeError("Video sync is currently available for Plex sources only.")
    return connection.client


def _resolve_device_and_profile(params: dict):
    """Find the target Device and its (capability, video_profile). Returns
    ``(device, capability, profile)``; ``profile`` is None when the device can't
    play video iAmped can sync."""
    from ..devices import capabilities, list_devices
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
    cap = syncing._resolve_capability(params)
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
    from ..sync.itunesdb import MEDIATYPE_MOVIE, MEDIATYPE_TVSHOW
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
    from ..sync import should_transcode_video, transcode_video
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
    server = _plex_server()
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
        from ..sync import mtp
        backend = mtp.MTPBackend(params.get("mtp_busloc"), folder="Video")
        backend.prepare()
        existing = backend.existing_keys()
    elif transport == "ipod":
        backend = itunesdb.ITunesDBBackend(
            params["device_path"], params.get("device_name") or "iPod",
            params.get("ipod_generation"))
        backend.prepare()
        backend.import_existing()           # preserve everything already on it
        existing = set(backend._by_key)
    else:                                    # ums / mass storage
        from ..sync import MassStorageBackend
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
        lock = syncing._mtp_lock(busloc)
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


@bp.get("/api/video/sections")
def api_video_sections():
    try:
        server = _plex_server()
        return jsonify({"sections": plex_client.video_sections(server)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400


@bp.get("/api/video/items")
def api_video_items():
    section = request.args.get("section", "")
    kind = request.args.get("kind", "movie")
    if not section:
        return jsonify({"error": "No video library selected."}), 400
    try:
        server = _plex_server()
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


@bp.get("/api/video/episodes")
def api_video_episodes():
    show_key = request.args.get("show_key", "")
    if not show_key:
        return jsonify({"error": "No show selected."}), 400
    try:
        server = _plex_server()
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


@bp.get("/api/video/thumb")
def api_video_thumb():
    key = request.args.get("key", "")
    if not key:
        return Response(status=404)
    try:
        server = _plex_server()
        from urllib.parse import quote
        url = server.url(
            f"/photo/:/transcode?width=320&height=480&minSize=1&upscale=0"
            f"&url={quote(key, safe='')}", includeToken=True)
        r = server._session.get(url, timeout=30)
        r.raise_for_status()
        resp = Response(r.content,
                        content_type=r.headers.get("Content-Type", "image/jpeg"))
        resp.headers["Cache-Control"] = "public, max-age=604800, immutable"
        return resp
    except Exception:  # noqa: BLE001
        return Response(status=404)


@bp.get("/api/video/device")
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


@bp.post("/api/video/sync")
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


@bp.get("/api/device/videos")
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


from .. import server as _server  # noqa: E402  (self-registration; see api/__init__)
_server.app.register_blueprint(bp)
