"""Detect when Apple's own device-sync machinery is actively managing a
connected iPod.

This matters because macOS (Music.app / the AMPDevices framework) and Windows
(Apple Devices / iTunes) will rewrite an iPod's ``iTunesDB`` out from under us:
they renumber track IDs, fold the ``Play Counts`` delta file into the database,
and — if the device is set to auto-sync against an empty or different library —
can *delete tracks*. iAmped must never write to a device while that is going on,
and should warn the user even for read operations because the database it just
read may change moments later.

The check is intentionally cheap and best-effort: a running ``AMPDevicesAgent``
(macOS) or ``AppleMobileDeviceProcess`` / ``iTunes`` (Windows) is treated as
"Apple is in the loop." Absence of these is a good signal it is safe; presence
is a reason to confirm before writing.
"""
from __future__ import annotations

import platform
import subprocess

_SYSTEM = platform.system()

# Process names that indicate Apple's device layer is live and may touch an iPod.
_MACOS_AGENTS = ("AMPDevicesAgent",)            # spawned to actively manage a device
_WINDOWS_AGENTS = ("AppleMobileDeviceProcess.exe", "iTunes.exe", "AppleMobileDeviceService.exe")


def _running_processes() -> list[str]:
    try:
        if _SYSTEM == "Windows":
            out = subprocess.run(["tasklist"], capture_output=True, text=True,
                                 timeout=10).stdout
        else:
            out = subprocess.run(["ps", "-axo", "comm"], capture_output=True,
                                 text=True, timeout=10).stdout
        return out.splitlines()
    except (OSError, subprocess.SubprocessError):
        return []


def apple_sync_active() -> dict:
    """Best-effort: is Apple's device-sync layer actively managing a device?

    Returns ``{"active": bool, "agents": [...], "note": str}``. ``active`` is
    advisory — callers should *warn before writing* when True, not hard-fail,
    since the user may have already disabled auto-sync.
    """
    wanted = _WINDOWS_AGENTS if _SYSTEM == "Windows" else _MACOS_AGENTS
    if _SYSTEM == "Linux":
        return {"active": False, "agents": [],
                "note": "No Apple sync layer on Linux."}

    lines = _running_processes()
    found = []
    for name in wanted:
        needle = name.lower()
        if any(needle in line.lower() for line in lines):
            found.append(name)

    if found:
        note = ("Apple's device-sync agent is running and may rewrite or sync "
                "this iPod. Turn off automatic syncing in Music/Apple Devices "
                "before writing with iAmped.")
    else:
        note = "Apple's device-sync agent is not running."
    return {"active": bool(found), "agents": found, "note": note}
