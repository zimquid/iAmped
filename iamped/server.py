"""Flask backend core: the app object, background-job machinery, and shared
helpers. Routes live in the :mod:`iamped.api` blueprints (auth, library,
playback, syncing, devices, video, podcasts), each of which registers itself on
:data:`app` when imported — see ``iamped/api/__init__.py`` for why."""
from __future__ import annotations

import os
import threading
import traceback
import uuid

from flask import Flask, jsonify, request, send_from_directory

from . import config, playback
from .library import Library
from .sync.base import have_ffmpeg

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
app = Flask(__name__, static_folder=None)

_state: dict = {"server": None}
JOBS: dict[str, dict] = {}


# --------------------------------------------------------------------------- helpers
def _lib() -> Library:
    return Library(str(config.DB_PATH))


def get_server():
    from . import plex_client
    cfg = config.load()
    if not cfg["plex_baseurl"] or not cfg["plex_token"]:
        raise RuntimeError("Plex is not configured yet.")
    if _state["server"] is None:
        _state["server"] = plex_client.connect(cfg["plex_baseurl"], cfg["plex_token"])
    return _state["server"]


def start_job(target, *args) -> str:
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
    cfg["has_ffmpeg"] = have_ffmpeg()
    cfg["native_playback"] = playback.PLAYER.status()
    return jsonify(cfg)


@app.post("/api/config")
def api_set_config():
    return jsonify(config.save(request.json or {}))


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
