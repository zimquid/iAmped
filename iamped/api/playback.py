"""Playback control, artwork proxy, and audio streaming routes."""
from __future__ import annotations

import os
import subprocess

from flask import Blueprint, Response, jsonify, request, send_file

from .. import playback, plex_client
from ..library import Library
from ..server import _lib, get_server
from ..sync.base import have_ffmpeg
# Runtime attribute access (not `from .syncing import materialize`): keeps this
# module importable while iamped.api.syncing is itself mid-import.
from . import syncing

bp = Blueprint("playback", __name__)


@bp.get("/api/playback/status")
def api_playback_status():
    return jsonify(playback.PLAYER.status())


@bp.post("/api/playback/play")
def api_playback_play():
    data = request.json or {}
    rk = str(data.get("rating_key") or "")
    if not rk:
        return jsonify({"error": "rating_key is required"}), 400
    lib = _lib()
    tr = lib.get_tracks([rk]).get(rk)
    if not tr:
        return jsonify({"error": "unknown track"}), 404
    try:
        server = get_server()
        url = plex_client.stream_url(server, tr["part_key"])
        start = max(0.0, float(data.get("start") or 0))
        return jsonify(playback.PLAYER.play(url, start))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc), **playback.PLAYER.status()}), 503


@bp.post("/api/playback/next")
def api_playback_next():
    """Queue the up-next track in mpv so the transition is gapless."""
    data = request.json or {}
    rk = str(data.get("rating_key") or "")
    if not rk:
        return jsonify({"error": "rating_key is required"}), 400
    tr = _lib().get_tracks([rk]).get(rk)
    if not tr:
        return jsonify({"error": "unknown track"}), 404
    try:
        server = get_server()
        url = plex_client.stream_url(server, tr["part_key"])
        return jsonify(playback.PLAYER.set_next(url))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc), **playback.PLAYER.status()}), 503


@bp.post("/api/playback/pause")
def api_playback_pause():
    try:
        return jsonify(playback.PLAYER.pause())
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc), **playback.PLAYER.status()}), 503


@bp.post("/api/playback/resume")
def api_playback_resume():
    try:
        return jsonify(playback.PLAYER.resume())
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc), **playback.PLAYER.status()}), 503


@bp.post("/api/playback/stop")
def api_playback_stop():
    return jsonify(playback.PLAYER.stop())


@bp.post("/api/playback/seek")
def api_playback_seek():
    try:
        position = max(0.0, float((request.json or {}).get("position") or 0))
        return jsonify(playback.PLAYER.seek(position))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc), **playback.PLAYER.status()}), 503


@bp.post("/api/playback/volume")
def api_playback_volume():
    try:
        volume = float((request.json or {}).get("volume") or 0)
        return jsonify(playback.PLAYER.set_volume(volume))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc), **playback.PLAYER.status()}), 503


@bp.get("/api/artwork")
def api_artwork():
    key = request.args.get("key", "")
    if not key:
        return jsonify({"error": "key is required"}), 400
    try:
        server = get_server()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400
    url = server.url(key, includeToken=True)
    r = server._session.get(url, timeout=15)
    resp = Response(r.content, status=r.status_code,
                    content_type=r.headers.get("Content-Type", "image/jpeg"))
    if r.status_code == 200:
        # Plex image keys are versioned (…/thumb/<epoch>), so the bytes for a
        # given key never change — let the webview cache them instead of
        # re-fetching every tile on each library scroll.
        resp.headers["Cache-Control"] = "public, max-age=604800, immutable"
    return resp


_NATIVE_STREAM = {"mp3", "m4a", "aac", "mp4", "ogg", "oga", "opus", "wav", "wma"}


def _audio_stream_headers(resp: Response) -> Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
    return resp


def _cached_playback_response(server, lib: Library, track: dict) -> Response | None:
    path, ext = syncing.materialize(server, lib, track, True, "mp3", 192)
    if ext != ".mp3" or not os.path.exists(path) or os.path.getsize(path) <= 0:
        return None
    resp = send_file(path, mimetype="audio/mpeg", conditional=True)
    resp.headers["Accept-Ranges"] = "bytes"
    return _audio_stream_headers(resp)


def _offline_response(track: dict) -> Response | None:
    """Serve a previously-synced cache file directly when Plex is unreachable,
    so the library keeps playing offline. Native-streamable containers only —
    without Plex there is nothing to transcode FLAC against a dead ffmpeg URL."""
    path = track.get("cached_path")
    if not path or not os.path.exists(path) or os.path.getsize(path) <= 0:
        return None
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    if ext not in _NATIVE_STREAM and not have_ffmpeg():
        return None
    if ext in _NATIVE_STREAM:
        mime = "audio/mp4" if ext in ("m4a", "mp4", "aac") else f"audio/{ext}"
        resp = send_file(path, mimetype=mime, conditional=True)
        resp.headers["Accept-Ranges"] = "bytes"
        return _audio_stream_headers(resp)
    # lossless in cache + ffmpeg present: transcode the local file on the fly
    command = ["ffmpeg", "-loglevel", "error", "-i", path, "-map", "0:a:0",
               "-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3", "-"]
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
    return _audio_stream_headers(Response(gen(), mimetype="audio/mpeg"))


@bp.get("/api/stream/<rk>")
def api_stream(rk):
    lib = _lib()
    tr = lib.get_tracks([rk]).get(rk)
    if not tr:
        return jsonify({"error": "unknown track"}), 404
    try:
        server = get_server()
    except Exception as exc:  # noqa: BLE001
        # Plex is unreachable/unconfigured — fall back to the sync cache so the
        # player still works offline.
        offline = _offline_response(tr)
        if offline is not None:
            return offline
        return jsonify({"error": str(exc)}), 400
    url = plex_client.stream_url(server, tr["part_key"])
    native = (tr.get("container") or "").lower() in _NATIVE_STREAM
    try:
        start = max(0.0, float(request.args.get("start", "0") or 0))
    except ValueError:
        return jsonify({"error": "invalid start time"}), 400
    try:
        duration = max(0.0, min(120.0, float(request.args.get("duration", "0") or 0)))
    except ValueError:
        return jsonify({"error": "invalid duration"}), 400

    if not native and not start and not duration and have_ffmpeg():
        cached = _cached_playback_response(server, lib, tr)
        if cached is not None:
            return cached

    if (native and not start and not duration) or not have_ffmpeg():
        # serve the sync cache when we already hold the original locally —
        # no Plex round-trip, instant seek — else proxy the original,
        # forwarding Range so the browser can seek
        cached_path = tr.get("cached_path")
        if native and cached_path and os.path.exists(cached_path) \
                and os.path.getsize(cached_path) > 0:
            cext = os.path.splitext(cached_path)[1].lstrip(".").lower()
            mime = ("audio/mp4" if cext in ("m4a", "mp4", "aac")
                    else "audio/mpeg" if cext == "mp3" else f"audio/{cext}")
            resp = send_file(cached_path, mimetype=mime, conditional=True)
            resp.headers["Accept-Ranges"] = "bytes"
            return _audio_stream_headers(resp)
        req_range = request.headers.get("Range")
        headers = {"Range": req_range} if req_range else {}
        up = server._session.get(url, headers=headers, stream=True, timeout=(10, None))
        resp = Response(up.iter_content(65536), status=up.status_code)
        for h in ("Content-Type", "Content-Length", "Accept-Ranges", "Content-Range"):
            if h in up.headers:
                resp.headers[h] = up.headers[h]
        resp.headers.setdefault("Content-Type", "audio/mpeg")
        return _audio_stream_headers(resp)

    # transcode lossless -> mp3 on the fly for in-browser playback
    command = ["ffmpeg", "-loglevel", "error"]
    if start:
        command.extend(["-ss", f"{start:.3f}"])
    if duration:
        command.extend(["-t", f"{duration:.3f}"])
    command.extend(
        ["-reconnect", "1", "-reconnect_streamed", "1",
         "-reconnect_delay_max", "5", "-i", url, "-map", "0:a:0",
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
    return _audio_stream_headers(Response(gen(), mimetype="audio/mpeg"))


from .. import server as _server  # noqa: E402  (self-registration; see api/__init__)
_server.app.register_blueprint(bp)
