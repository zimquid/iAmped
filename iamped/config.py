"""Persistent configuration and well-known paths.

Everything iAmped stores lives in one data directory:
    config.json   — Plex connection details + last-used settings
    library.db    — SQLite cache of Plex metadata
    cache/        — downloaded audio files (the reusable "pool")

Where that directory lives depends on how iAmped is run:
    * IAMPED_HOME set        -> exactly there (testing / multi-profile)
    * frozen portable build  -> "iAmped-data" next to the app  (travels on USB)
    * plain source / dev run -> ~/.iamped
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

HOME = Path(os.path.expanduser("~"))


def _default_app_dir() -> Path:
    # Explicit override always wins.
    env = os.environ.get("IAMPED_HOME")
    if env:
        return Path(env).expanduser()
    # Frozen (PyInstaller) => portable: keep data beside the app so the whole
    # thing can be carried on a USB stick. For a macOS .app, sit the folder
    # next to the bundle rather than inside Contents/MacOS.
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        base = exe.parent
        for parent in exe.parents:
            if parent.suffix == ".app":
                base = parent.parent
                break
        return base / "iAmped-data"
    # Plain source run.
    return HOME / ".iamped"


APP_DIR = _default_app_dir()
CONFIG_PATH = APP_DIR / "config.json"
DB_PATH = APP_DIR / "library.db"
CACHE_DIR = APP_DIR / "cache"

DEFAULTS: dict[str, Any] = {
    "plex_baseurl": "",
    "plex_token": "",
    "plex_client_id": "",        # stable device identity used by Plex OAuth
    "music_section": "",        # name of the Plex music library section
    "ingest_dir": "",           # writable, Plex-watched folder for ingest-back
    "ingest_section": "",       # music section to scan after ingest (defaults to music_section)
    "cache_dir": str(CACHE_DIR),
    # last-used sync settings, remembered for convenience
    "last_device_path": "",
    "last_device_type": "massstorage",   # "massstorage" | "ipod"
    "reserve_mb": 200,
    "fill_strategy": "most_played",
    "transcode_lossless": True,
    "aac_bitrate_k": 256,
    "mp3_bitrate_k": 320,
    "sync_artwork": True,
    "mirror": True,
}


def ensure_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    Path(load().get("cache_dir", CACHE_DIR)).mkdir(parents=True, exist_ok=True)


def load() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def save(updates: dict[str, Any]) -> dict[str, Any]:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load()
    cfg.update({k: v for k, v in updates.items() if k in DEFAULTS})
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    return cfg
