"""Shared helpers for the sync backends."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass

def free_bytes(path: str) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def total_bytes(path: str) -> int:
    try:
        return shutil.disk_usage(path).total
    except OSError:
        return 0


@dataclass
class Volume:
    path: str
    name: str
    total: int
    free: int
    is_ipod: bool = False
    # Cross-ecosystem fields surfaced by the device layer.
    fs: str = ""              # canonical filesystem (fat32/hfs+/exfat/…)
    mounted: bool = True      # False for raw/unmountable iPods
    writable: bool = False    # can this host write to it as-is?
    ipod_format: str = ""     # iPod data-partition filesystem
    ipod_model: str = ""      # friendly model name, e.g. "iPod nano (1st generation)"
    ipod_generation: str = "" # coarse generation label
    needs_conversion: bool = False  # HFS+ iPod that must become FAT32 here
    raw_path: str = ""        # \\.\PhysicalDriveN or /dev/diskN when known
    note: str = ""            # human hint (why it's not writable, etc.)


def list_volumes(include_raw: bool = True) -> list[Volume]:
    """Sync targets for this host (macOS, Windows, Linux).

    Delegates to :mod:`iamped.devices`, which enumerates mounted volumes and —
    crucially for cross-ecosystem use — raw/unmountable iPods (e.g. a
    Mac-formatted HFS+ iPod plugged into Windows). Returns the legacy ``Volume``
    shape, enriched with filesystem/writability fields.
    """
    from ..devices import list_devices  # local import avoids an import cycle

    out: list[Volume] = []
    for d in list_devices(include_raw=include_raw):
        out.append(Volume(
            path=d.mountpoint or d.raw_path or d.id,
            name=d.name,
            total=d.total,
            free=d.free,
            is_ipod=d.is_ipod,
            fs=d.fs,
            mounted=d.mounted,
            writable=d.writable,
            ipod_format=d.ipod_format or "",
            ipod_model=d.ipod_model or "",
            ipod_generation=d.ipod_generation or "",
            needs_conversion=d.needs_conversion,
            raw_path=d.raw_path or "",
            note=d.note,
        ))
    return out


_SAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name: str, fallback: str = "Unknown", maxlen: int = 80) -> str:
    name = (name or "").strip()
    name = _SAFE.sub("_", name).rstrip(". ")
    return (name or fallback)[:maxlen]


def transcode_to_mp3(src: str, dst: str, bitrate_k: int = 320) -> str:
    """Transcode any source to CBR MP3 using ffmpeg. Raises on failure."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".part.mp3"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-i", src,
        "-map", "0:a:0", "-c:a", "libmp3lame", "-b:a", f"{bitrate_k}k",
        "-id3v2_version", "3", tmp,
    ]
    subprocess.run(cmd, check=True)
    os.replace(tmp, dst)
    return dst


def transcode_to_aac(src: str, dst: str, bitrate_k: int = 256) -> str:
    """Transcode to AAC in an .m4a container (native iPod format)."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".part.m4a"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-i", src,
        "-map", "0:a:0", "-c:a", "aac", "-b:a", f"{bitrate_k}k",
        "-movflags", "+faststart", "-f", "ipod", tmp,
    ]
    subprocess.run(cmd, check=True)
    os.replace(tmp, dst)
    return dst


TARGET_FORMAT = {"ipod": "aac", "massstorage": "mp3"}


def target_format(device_type: str) -> str:
    return TARGET_FORMAT.get(device_type, "mp3")


def transcode(src: str, dst_base: str, fmt: str,
              bitrate_k: int | None = None) -> tuple[str, str]:
    """Transcode to fmt ('aac'|'mp3'); returns (path, ext)."""
    if fmt == "aac":
        return transcode_to_aac(
            src, dst_base + ".m4a", bitrate_k or 256), ".m4a"
    return transcode_to_mp3(
        src, dst_base + ".mp3", bitrate_k or 320), ".mp3"


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None
