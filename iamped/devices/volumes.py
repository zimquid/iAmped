"""Enumerate *mounted* volumes on macOS, Windows and Linux.

These are the easy cases: the OS has already mounted the volume, so it tells us
the filesystem and we can read free space with the stdlib. The hard cases
(unmounted / RAW iPods) live in :mod:`iamped.devices.rawdisk`.

Each function returns a list of :class:`~iamped.devices.model.Device` with
capability already computed.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess

from . import ipodmodel, usbdetect
from .model import Device

_SYSTEM = platform.system()


# Map OS-reported filesystem identifiers to our canonical names.
_FSTYPE_CANON = {
    # FAT family
    "msdos": "fat32", "vfat": "fat32", "fat": "fat32", "fat32": "fat32",
    "fat16": "fat16", "fat12": "fat12",
    "exfat": "exfat",
    # NTFS (Linux may report ntfs3 or fuseblk for ntfs-3g)
    "ntfs": "ntfs", "ntfs3": "ntfs", "fuseblk": "ntfs",
    # Apple
    "hfs": "hfs", "hfs+": "hfs+", "hfsplus": "hfs+", "hfsx": "hfsx",
    "apfs": "apfs",
}


def _canon_fs(raw: str) -> str:
    return _FSTYPE_CANON.get((raw or "").strip().lower(), (raw or "").lower())


def _is_ipod(mountpoint: str) -> bool:
    """An iPod always carries an ``iPod_Control`` directory at its volume root.

    The check is case-insensitive because the directory is ``iPod_Control`` on
    HFS+ but is often seen as ``IPOD_CONTROL`` on the 8.3-mangled FAT view.
    """
    try:
        for entry in os.listdir(mountpoint):
            if entry.lower() == "ipod_control":
                return os.path.isdir(os.path.join(mountpoint, entry))
    except OSError:
        pass
    return False


def _capacity(path: str) -> tuple[int, int]:
    try:
        u = shutil.disk_usage(path)
        return u.total, u.free
    except OSError:
        return 0, 0


def _make(mountpoint: str, name: str, fs: str, raw_path: str | None = None) -> Device:
    total, free = _capacity(mountpoint)
    ipod = _is_ipod(mountpoint)
    dev = Device(
        id=mountpoint,
        name=name or os.path.basename(mountpoint.rstrip("/\\")) or mountpoint,
        fs=_canon_fs(fs),
        mountpoint=mountpoint,
        raw_path=raw_path,
        total=total,
        free=free,
        mounted=True,
        is_ipod=ipod,
        ipod_format=_canon_fs(fs) if ipod else None,
    )
    if ipod:
        usb = usbdetect.match(raw_path=raw_path, mountpoint=mountpoint)
        info = ipodmodel.detect(mountpoint, total, usb=usb)
        if info:
            dev.ipod_model = info["model"]
            dev.ipod_generation = info["generation"]
    return dev.capability()


# --------------------------------------------------------------------------- #
# macOS                                                                        #
# --------------------------------------------------------------------------- #
def _macos_volumes() -> list[Device]:
    # Drive the list from `mount`, keeping real mounts under /Volumes. This
    # excludes the boot disk (mounts at /) and APFS firmlinks like
    # "/Volumes/Macintosh HD" (which aren't separate mounts), leaving genuine
    # removable media. Lines look like:
    #   /dev/disk4s2 on /Volumes/IPOD (msdos, local, nodev, nosuid, noowners)
    devices: list[Device] = []
    try:
        out = subprocess.run(["mount"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    for line in out.splitlines():
        if " on /Volumes/" not in line or "(" not in line:
            continue
        try:
            dev, rest = line.split(" on ", 1)
            mp, paren = rest.rsplit(" (", 1)
            fs = paren.split(",", 1)[0].strip(") ")
        except ValueError:
            continue
        if not os.path.isdir(mp):
            continue
        devices.append(_make(mp, os.path.basename(mp), fs,
                            raw_path=dev.strip() or None))
    return devices


# --------------------------------------------------------------------------- #
# Linux                                                                        #
# --------------------------------------------------------------------------- #
_LINUX_MOUNT_ROOTS = ("/media", "/run/media", "/mnt")


def _linux_volumes() -> list[Device]:
    devices: list[Device] = []
    seen: set[str] = set()
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        lines = []

    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        dev, mp, fs = parts[0], parts[1].replace("\\040", " "), parts[2]
        if not dev.startswith("/dev/"):
            continue
        if not any(mp == r or mp.startswith(r + "/") for r in _LINUX_MOUNT_ROOTS):
            continue
        if mp in seen:
            continue
        seen.add(mp)
        devices.append(_make(mp, os.path.basename(mp), fs, raw_path=dev))
    return devices


# --------------------------------------------------------------------------- #
# Windows                                                                      #
# --------------------------------------------------------------------------- #
def _windows_volumes() -> list[Device]:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    DRIVE_REMOVABLE, DRIVE_FIXED = 2, 3

    devices: list[Device] = []
    bitmask = k32.GetLogicalDrives()
    for i in range(26):
        if not (bitmask >> i) & 1:
            continue
        letter = f"{chr(ord('A') + i)}:\\"
        dtype = k32.GetDriveTypeW(ctypes.c_wchar_p(letter))
        if dtype not in (DRIVE_REMOVABLE, DRIVE_FIXED):
            continue

        vol_name = ctypes.create_unicode_buffer(261)
        fs_name = ctypes.create_unicode_buffer(261)
        ok = k32.GetVolumeInformationW(
            ctypes.c_wchar_p(letter),
            vol_name, ctypes.sizeof(vol_name),
            None, None, None,
            fs_name, ctypes.sizeof(fs_name),
        )
        if not ok:
            continue  # drive letter present but no media inserted
        devices.append(_make(letter, vol_name.value, fs_name.value))
    return devices


def mounted_devices() -> list[Device]:
    """All mounted volumes the user could plausibly sync to, for this OS."""
    if _SYSTEM == "Darwin":
        return _macos_volumes()
    if _SYSTEM == "Linux":
        return _linux_volumes()
    if _SYSTEM == "Windows":
        return _windows_volumes()
    return []
