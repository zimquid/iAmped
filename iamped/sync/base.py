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


# Classic iPods (1G–5.5G, mini, nano 1G–3G) and virtually every mass-storage
# player decode audio only at CD rates: 44.1 kHz, stereo. A hi-res (88.2/96/192
# kHz) or multichannel (5.1) lossless source re-encoded at its native rate/layout
# plays back garbled — the very "glitchy, unpleasant" audio users hear. Force the
# output to 44.1 kHz stereo so it stays inside the hardware's spec (and matches
# the sample-rate the iTunesDB writer records for every track).
IPOD_SAMPLE_RATE = "44100"
IPOD_CHANNELS = "2"


def _ffmpeg_encoders() -> str:
    try:
        return subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, check=True).stdout
    except Exception:
        return ""


# Apple-grade AAC encoders, best first. ffmpeg's *native* ``aac`` emits a
# technically-valid stream that nonetheless stutters every few seconds on older
# iPods (the 1st-gen nano's PortalPlayer decoder is intolerant of non-Apple AAC
# framing — the "glitchy, unpleasant" audio). The clean, iPod-safe encoders are
# Fraunhofer FDK (``libfdk_aac`` — what fre:ac and most good converters use,
# cross-platform when the ffmpeg build enables it) and Apple's AudioToolbox
# (``aac_at`` — macOS only). Prefer those; fall back to native ``aac`` only when
# neither is present, and let the app warn in that case.
_AAC_PREFERENCE = ("libfdk_aac", "aac_at", "aac")


def aac_encoder() -> str:
    """The AAC encoder ffmpeg should use for iPod-bound audio (best available)."""
    out = _ffmpeg_encoders()
    for enc in _AAC_PREFERENCE:
        if enc == "aac" or enc in out:
            return enc
    return "aac"


# Resolve once — the encoder set can't change under a running process.
try:
    _AAC_ENCODER = aac_encoder()
except Exception:
    _AAC_ENCODER = "aac"


def aac_codec() -> str:
    """The AAC encoder actually in use ('libfdk_aac' / 'aac_at' / 'aac')."""
    return _AAC_ENCODER


def is_apple_grade_aac() -> bool:
    """True when the AAC encoder produces clean audio for old iPods. The native
    ``aac`` encoder does not — the UI surfaces a warning / MP3 hint when False."""
    return _AAC_ENCODER in ("libfdk_aac", "aac_at")


def alac_codec() -> str:
    """ALAC (Apple Lossless) encoder. Lossless → output is bit-exact regardless
    of encoder, so the always-present native ``alac`` is fine everywhere."""
    return "alac"


def transcode_to_mp3(src: str, dst: str, bitrate_k: int = 320) -> str:
    """Transcode any source to CBR MP3 using ffmpeg. Raises on failure."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".part.mp3"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-i", src,
        "-map", "0:a:0", "-c:a", "libmp3lame", "-b:a", f"{bitrate_k}k",
        "-ar", IPOD_SAMPLE_RATE, "-ac", IPOD_CHANNELS,
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
        "-map", "0:a:0", "-c:a", _AAC_ENCODER, "-b:a", f"{bitrate_k}k",
        "-ar", IPOD_SAMPLE_RATE, "-ac", IPOD_CHANNELS,
        "-movflags", "+faststart", "-f", "ipod", tmp,
    ]
    subprocess.run(cmd, check=True)
    os.replace(tmp, dst)
    return dst


def transcode_to_alac(src: str, dst: str, bitrate_k: int | None = None) -> str:
    """Transcode to ALAC (Apple Lossless) in an .m4a container. Lossless, so no
    bitrate — the ``bitrate_k`` arg is ignored (kept for a uniform signature).
    Forced to 44.1 kHz / 16-bit stereo, the CD-rate every click-wheel iPod
    decodes; hi-res/multichannel sources are down-converted to stay in spec."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".part.m4a"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-i", src,
        "-map", "0:a:0", "-c:a", "alac", "-sample_fmt", "s16p",
        "-ar", IPOD_SAMPLE_RATE, "-ac", IPOD_CHANNELS,
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
    """Transcode to fmt ('aac'|'alac'|'mp3'); returns (path, ext)."""
    if fmt == "alac":
        return transcode_to_alac(src, dst_base + ".m4a"), ".m4a"
    if fmt == "aac":
        return transcode_to_aac(
            src, dst_base + ".m4a", bitrate_k or 256), ".m4a"
    return transcode_to_mp3(
        src, dst_base + ".mp3", bitrate_k or 320), ".mp3"


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None
