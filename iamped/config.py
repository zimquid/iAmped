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
import shutil
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


def _stable_dir() -> Path:
    """A build-independent home that survives app rebuilds.

    A frozen (PyInstaller) build keeps its working data in ``iAmped-data`` next
    to the app so the whole thing travels on a USB stick — but that folder is
    wiped whenever the app is rebuilt or replaced, taking the Plex login and the
    cached library with it. That's why every rebuild otherwise forces a fresh
    sign-in and a full, slow library re-sync.

    To avoid that, the login credentials and the library cache are also mirrored
    to a stable per-user directory (``~/.iamped``) and restored from it on the
    next launch. For a plain source run — or when ``IAMPED_HOME`` pins the data
    dir explicitly (e.g. tests) — the app already lives somewhere stable, so the
    stable dir is just the app dir and all mirroring becomes a no-op.
    """
    if getattr(sys, "frozen", False) and not os.environ.get("IAMPED_HOME"):
        return HOME / ".iamped"
    return APP_DIR


STABLE_DIR = _stable_dir()
CRED_PATH = STABLE_DIR / "plex-auth.json"
STABLE_DB = STABLE_DIR / "library.db"

# The subset of settings worth surviving a rebuild: the Plex connection plus the
# server-side selections that would otherwise force a reconnect and re-scan.
LOGIN_KEYS = (
    "plex_baseurl", "plex_token", "plex_client_id",
    "music_section", "ingest_dir", "ingest_section", "sources",
    "active_source_id",
)

DEFAULTS: dict[str, Any] = {
    "plex_baseurl": "",
    "plex_token": "",
    "plex_client_id": "",        # stable device identity used by Plex OAuth
    # ``plex_*`` remains for legacy/OAuth compatibility. New provider
    # connections live in sources and the active source scopes catalog/sync.
    "sources": [],
    "active_source_id": "plex",
    "music_section": "",        # name of the Plex music library section
    "ingest_dir": "",           # writable, Plex-watched folder for ingest-back
    "ingest_section": "",       # music section to scan after ingest (defaults to music_section)
    "cache_dir": str(CACHE_DIR),
    # last-used sync settings, remembered for convenience
    "last_device_path": "",
    "last_device_type": "massstorage",   # "massstorage" | "ipod"
    "reserve_mb": 20,
    "fill_strategy": "most_played",
    "transcode_lossless": True,
    "aac_bitrate_k": 256,
    "mp3_bitrate_k": 320,
    "ipod_format": "aac",       # iPod audio target: "aac" (lossy) | "alac" (lossless)
    "cache_limit_mb": 3072,     # LRU-evict the download/transcode cache past this
    "aggressive_cache": False,  # keep everything for fast re-syncs (no cap, no
                                # clear-on-exit) at the cost of local disk
    "sync_artwork": True,
    "mirror": True,
    "sd_card_enabled": False,   # hidden: surface plain SD cards as sync targets
    "prefetch_on_plan": True,   # warm the download/transcode cache while the
                                # user reviews a sync plan
    "autosync_enabled": True,   # master switch for sync-on-connect (each device
                                # additionally opts in via its profile)
}


def ensure_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    Path(load().get("cache_dir", CACHE_DIR)).mkdir(parents=True, exist_ok=True)
    _seed_library_from_stable()


def load() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    # Restore the login from the stable cross-build cache when the portable
    # config doesn't carry it — e.g. right after a rebuild wiped the app dir.
    if STABLE_DIR != APP_DIR and not cfg.get("plex_token") and not cfg.get("sources"):
        creds = _read_credentials()
        for key in LOGIN_KEYS:
            if not cfg.get(key) and creds.get(key):
                cfg[key] = creds[key]
    return _normalize_sources(cfg)


def save(updates: dict[str, Any]) -> dict[str, Any]:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load()
    cfg.update({k: v for k, v in updates.items() if k in DEFAULTS})
    cfg = _normalize_sources(cfg)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    _mirror_credentials(cfg)
    return cfg


def _normalize_sources(cfg: dict[str, Any]) -> dict[str, Any]:
    """Represent the historic Plex login as the default source.

    Keeping ``plex`` as its original id means existing SQLite rows, cache file
    names and device manifests remain valid while newer server types receive
    namespaced catalog keys.
    """
    raw = cfg.get("sources")
    sources = [dict(source) for source in raw if isinstance(source, dict)] \
        if isinstance(raw, list) else []
    if not sources and (cfg.get("plex_baseurl") or cfg.get("plex_token")):
        sources = [{
            "id": "plex", "kind": "plex", "name": "Plex",
            "baseurl": cfg.get("plex_baseurl", ""),
            "token": cfg.get("plex_token", ""),
            "device_id": cfg.get("plex_client_id", ""),
        }]
    clean = []
    for source in sources:
        kind = str(source.get("kind") or "plex").lower()
        sid = str(source.get("id") or kind).strip() or kind
        clean.append({
            "id": sid, "kind": kind,
            "name": str(source.get("name") or ""),
            "baseurl": str(source.get("baseurl") or ""),
            "token": str(source.get("token") or ""),
            "username": str(source.get("username") or ""),
            "password": str(source.get("password") or ""),
            "user_id": str(source.get("user_id") or ""),
            "device_id": str(source.get("device_id") or ""),
        })
    cfg["sources"] = clean
    active = str(cfg.get("active_source_id") or "")
    if clean and active not in {source["id"] for source in clean}:
        cfg["active_source_id"] = clean[0]["id"]
    return cfg


def active_source(cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
    cfg = cfg or load()
    active = str(cfg.get("active_source_id") or "")
    return next((source for source in cfg.get("sources", [])
                 if source.get("id") == active), None)


def upsert_source(source: dict[str, Any], active: bool = True) -> dict[str, Any]:
    """Save one provider source without exposing its secret through APIs."""
    cfg = load()
    sources = [s for s in cfg.get("sources", []) if s.get("id") != source.get("id")]
    sources.append(dict(source))
    updates: dict[str, Any] = {"sources": sources}
    if active:
        updates["active_source_id"] = source.get("id")
    # Preserve the old Plex OAuth/manual fields for the legacy entrypoints.
    if source.get("kind") == "plex":
        updates.update({
            "plex_baseurl": source.get("baseurl", ""),
            "plex_token": source.get("token", ""),
            "plex_client_id": source.get("device_id") or cfg.get("plex_client_id", ""),
        })
    return save(updates)


def set_active_source(source_id: str) -> dict[str, Any]:
    cfg = load()
    if source_id not in {str(s.get("id")) for s in cfg.get("sources", [])}:
        raise ValueError("Unknown media source.")
    return save({"active_source_id": source_id})


# --------------------------------------------------------------------------- #
# Cross-build persistence (login + library cache)                             #
# --------------------------------------------------------------------------- #
def _read_credentials() -> dict[str, Any]:
    try:
        data = json.loads(CRED_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _mirror_credentials(cfg: dict[str, Any]) -> None:
    """Persist the login subset to the stable cache so a rebuild can restore it.

    No-op for a plain source run (stable dir == app dir), where config.json is
    already in a stable place.
    """
    if STABLE_DIR == APP_DIR:
        return
    if not cfg.get("plex_token") and not any(
            s.get("token") or s.get("username") for s in cfg.get("sources", [])):
        return
    creds = {key: cfg.get(key, "") for key in LOGIN_KEYS}
    try:
        STABLE_DIR.mkdir(parents=True, exist_ok=True)
        CRED_PATH.write_text(json.dumps(creds, indent=2))
    except OSError:
        pass


def _seed_library_from_stable() -> None:
    """Restore the cached library from the stable store when the app dir has no
    copy yet (a fresh rebuild), so the user can browse without a Plex re-sync."""
    if STABLE_DIR == APP_DIR:
        return
    try:
        if STABLE_DB.exists() and not DB_PATH.exists():
            APP_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(STABLE_DB, DB_PATH)
    except OSError:
        pass


def mirror_library_to_stable() -> None:
    """Copy the freshly-built library cache into the stable store, so a future
    rebuild can restore it instead of re-querying Plex. Called after a build."""
    if STABLE_DIR == APP_DIR:
        return
    try:
        if DB_PATH.exists():
            STABLE_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(DB_PATH, STABLE_DB)
    except OSError:
        pass
