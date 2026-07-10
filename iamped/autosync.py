"""Sync-on-connect: the iTunes dock behavior.

A background watcher polls the device list. When a device with an
``autosync``-enabled profile appears, it runs the full dock ritual as one
background job, visible to the UI via /api/jobs/active:

  1. read the device's play counts / ratings and push them to Plex
     (the automatic round-trip; per-profile ``auto_readback``, default on),
  2. refresh the local library from Plex (delta — seconds, not minutes),
  3. sync the device from its saved profile.

Every step is best-effort: a device with no play counts, or a Plex server
that's momentarily unreachable, degrades to skipping that step rather than
aborting the ritual.
"""
from __future__ import annotations

import threading
import time

from . import config, device_management, readback

POLL_SECONDS = 8

_WATCH = {"thread": None, "present": set(), "started": False}


def _profile_sync_params(dev, path: str, dtype: str, profile: dict) -> dict:
    """Turn a saved device profile into the params dict /api/sync would send."""
    params = {
        "device_path": path,
        "device_type": dtype,
        "device_name": profile.get("name") or dev.name,
        "reserve_mb": profile.get("reserve_mb", 20),
        "fill_strategy": profile.get("fill_strategy", "most_played"),
        "transcode_lossless": profile.get("transcode_lossless", True),
        "sync_artwork": profile.get("sync_artwork", True),
        "mirror": profile.get("mirror", True),
        "playlist_ids": profile.get("playlist_ids", []),
    }
    if dtype == "ipod":
        params["ipod_generation"] = getattr(dev, "ipod_generation", "") or ""
    if profile.get("target_bitrate_k"):
        params["target_bitrate_k"] = profile["target_bitrate_k"]
    if profile.get("ipod_format"):
        params["ipod_format"] = profile["ipod_format"]
    for key in ("transport", "layout"):
        if profile.get(key):
            params[key] = profile[key]
    return params


def _autosync_job(job, params, dtype_profile):
    from .server import _lib, get_server
    from .api import library as library_api
    from .api import syncing

    path = params["device_path"]
    dtype = params["device_type"]
    profile = dtype_profile

    # 1. round-trip plays/ratings from the device into Plex
    if profile.get("auto_readback", True):
        job["phase"] = "readback"
        job["message"] = "Importing plays and ratings from the device…"
        try:
            lib = _lib()
            plan = readback.build_plan(lib, path, dtype, True, True,
                                       "fill_blanks")
            if plan["plan"]:
                with device_management.device_lock(path, dtype):
                    res = readback.apply_plan(get_server(), lib, path, dtype,
                                              plan["plan"], reset=True)
                job["readback"] = res
                job["message"] = (
                    f"Imported {res['plays_added']} play(s), "
                    f"{res['ratings_set']} rating(s) from the device.")
        except Exception as exc:  # noqa: BLE001 - nothing to import is normal
            job["readback"] = {"skipped": str(exc)}

    # 2. freshen the catalog so the sync plans against current Plex state
    section = config.load().get("music_section")
    if section:
        job["phase"] = "library"
        job["message"] = "Refreshing the library from Plex…"
        try:
            library_api._build_library_job(dict(job), section, "delta")
        except Exception:  # noqa: BLE001 - a stale catalog still syncs fine
            pass

    # 3. the sync itself, from the saved profile
    job["phase"] = "planning"
    job["message"] = "Starting the profile sync…"
    syncing._sync_job(job, params)


def _scan_once() -> None:
    from .devices import list_devices
    from .server import JOBS, start_job

    try:
        devices = list_devices(include_raw=False)
    except Exception:  # noqa: BLE001 - enumeration hiccups are routine
        return
    seen_now = set()
    for dev in devices:
        path = dev.mountpoint
        if not path or not dev.writable:
            continue
        dtype = "ipod" if dev.is_ipod else "massstorage"
        try:
            did = device_management.device_id(path, dtype)
        except Exception:  # noqa: BLE001
            continue
        seen_now.add(did)
        if did in _WATCH["present"]:
            continue                          # already connected — not an event
        _WATCH["present"].add(did)

        profile = device_management.profiles().get(did) or {}
        if not profile.get("autosync"):
            continue
        active_source = config.active_source(config.load()) or {}
        if profile.get("source_id") and profile.get("source_id") != active_source.get("id"):
            continue  # profiles never silently sync content from another server
        if any(j.get("status") == "running" and j.get("device_path") == path
               for j in JOBS.values()):
            continue                          # something already works on it
        params = _profile_sync_params(dev, path, dtype, profile)
        start_job(_autosync_job, params, profile)
    # forget devices that were unplugged so replugging triggers again
    _WATCH["present"] &= seen_now


def _loop() -> None:
    # Baseline pass: devices already connected at app start are recorded but
    # not synced — autosync reacts to *plugging in*, not to launching the app
    # next to an iPod that never left the dock.
    try:
        from .devices import list_devices
        for dev in list_devices(include_raw=False):
            if dev.mountpoint:
                dtype = "ipod" if dev.is_ipod else "massstorage"
                try:
                    _WATCH["present"].add(
                        device_management.device_id(dev.mountpoint, dtype))
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass
    while True:
        time.sleep(POLL_SECONDS)
        if not config.load().get("autosync_enabled", True):
            continue
        _scan_once()


def start_watcher() -> None:
    if _WATCH["started"]:
        return
    _WATCH["started"] = True
    thread = threading.Thread(target=_loop, daemon=True, name="autosync")
    _WATCH["thread"] = thread
    thread.start()
