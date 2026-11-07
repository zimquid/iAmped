"""Flask backend core: the app object, background-job machinery, and shared
helpers. Routes live in the :mod:`iamped.api` blueprints (auth, library,
playback, syncing, devices, video, podcasts), each of which registers itself on
:data:`app` when imported — see ``iamped/api/__init__.py`` for why."""
from __future__ import annotations

import os
import threading
import time
import traceback
import uuid
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_from_directory

from . import __version__, cache, config, playback, providers
from .library import Library
from .sync.base import aac_codec, have_ffmpeg, is_apple_grade_aac

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
app = Flask(__name__, static_folder=None)

_state: dict = {"server": None, "source_id": None}
JOBS: dict[str, dict] = {}
# Finished jobs are kept this long so the UI can collect their result, then
# pruned so a long-running session doesn't accumulate every job forever.
_JOB_TTL_S = 3600

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _hostname(hostport: str) -> str:
    """Host part of a ``Host`` header value (``"127.0.0.1:5000"`` → ``"127.0.0.1"``)."""
    hostport = (hostport or "").strip().lower()
    if hostport.startswith("["):
        return hostport.split("]", 1)[0] + "]"
    return hostport.rsplit(":", 1)[0] if hostport.count(":") == 1 else hostport


@app.before_request
def _local_only():
    """The API is unauthenticated and can read device/local files, so only the
    app's own page may talk to it.

    * The ``Host`` header must be a loopback name — defeats DNS rebinding, where
      a remote page re-points its own hostname at 127.0.0.1.
    * Browser requests from any other site are refused, judged by
      ``Sec-Fetch-Site`` where the engine sends it (Chromium/WebView2, WebKit,
      Firefox) and by ``Origin`` otherwise. Non-browser clients send neither and
      can already reach loopback directly, so they are unaffected.
    """
    if _hostname(request.host) not in _LOOPBACK_HOSTS:
        return jsonify({"error": "forbidden host"}), 403
    site = request.headers.get("Sec-Fetch-Site")
    if site is not None:
        if site not in ("same-origin", "none"):
            return jsonify({"error": "cross-site request refused"}), 403
        return None
    origin = request.headers.get("Origin")
    if origin is not None:
        parts = urlsplit(origin)
        if (parts.scheme not in ("http", "https")
                or parts.netloc.lower() != request.host.lower()):
            return jsonify({"error": "cross-origin request refused"}), 403
    return None


# --------------------------------------------------------------------------- helpers
def _lib() -> Library:
    return Library(str(config.DB_PATH))


def get_server():
    cfg = config.load()
    source = config.active_source(cfg)
    if source is None:
        raise RuntimeError("No media server is configured yet.")
    if _state["server"] is None or _state.get("source_id") != source.get("id"):
        _state["server"] = providers.connect(source)
        _state["source_id"] = source.get("id")
    return _state["server"]


def _prune_jobs(now: float) -> None:
    for jid, job in list(JOBS.items()):
        ended = job.get("ended_at")
        if job.get("status") != "running" and ended and now - ended > _JOB_TTL_S:
            JOBS.pop(jid, None)


def start_job(target, *args) -> str:
    _prune_jobs(time.time())
    jid = uuid.uuid4().hex[:12]
    job = {"status": "running", "phase": "starting", "done": 0, "total": 0,
           "message": "", "result": None, "error": None, "code": None,
           "detail": None, "kind": getattr(target, "__name__", "").strip("_")}
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
            job["code"] = getattr(exc, "code", None)      # e.g. pending_transaction
            job["detail"] = {"completed": getattr(exc, "completed", None)} \
                if getattr(exc, "code", None) else None
            job["trace"] = traceback.format_exc()
        finally:
            job["ended_at"] = time.time()
    threading.Thread(target=runner, daemon=True).start()
    return jid


def _fmt_bytes(n: int) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


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
    # The frontend only needs connection state, never reusable credentials.
    # Keep legacy Plex fields blank too; old portable config files remain
    # readable by the backend through config.load().
    cfg.pop("plex_token", None)
    cfg["sources"] = [providers.public_source(
        source, source.get("id") == cfg.get("active_source_id"))
        for source in cfg.get("sources", [])]
    cfg["has_ffmpeg"] = have_ffmpeg()
    cfg["native_playback"] = playback.PLAYER.status()
    # AAC encoder quality: 'apple_grade' → libfdk_aac/aac_at (clean on old
    # iPods); 'native' → ffmpeg's aac, which stutters on 1st-gen-era hardware.
    cfg["aac_encoder"] = aac_codec()
    cfg["aac_apple_grade"] = is_apple_grade_aac()
    cfg["cache_status"] = cache.status()
    cfg["version"] = __version__
    return jsonify(cfg)


@app.post("/api/config")
def api_set_config():
    return jsonify(config.save(request.json or {}))


@app.get("/api/cache/status")
def api_cache_status():
    return jsonify(cache.status())


@app.post("/api/cache/clear")
def api_cache_clear():
    return jsonify(cache.clear_all())


@app.get("/api/job/<jid>")
def api_job(jid):
    job = JOBS.get(jid)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(job)


@app.get("/api/jobs/active")
def api_jobs_active():
    """Running (or very recently started) jobs, so the UI can pick up work it
    didn't start itself — e.g. an autosync kicked off by plugging a device in."""
    out = {}
    for jid, job in JOBS.items():
        if job.get("status") == "running":
            out[jid] = {k: job.get(k) for k in
                        ("status", "phase", "done", "total", "message",
                         "kind", "device_path")}
    return jsonify(out)


def create_app():
    config.ensure_dirs()
    from . import autosync
    autosync.start_watcher()
    return app


# Importing the blueprint modules registers their routes on `app` (each module
# self-registers at its bottom — that stays correct regardless of whether
# iamped.server or an iamped.api.* module was imported first).
from .api import auth, devices, library, playback as playback_api  # noqa: E402,F401
from .api import podcasts as podcasts_api, syncing, video  # noqa: E402,F401
