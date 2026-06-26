"""Video transcoding for the device backends.

Plex video parts arrive in whatever container/codec the user ripped them in
(MKV/H.265, AVI/MPEG-4, MP4/H.264, …). Each device family plays only a narrow
envelope, so we re-encode to a per-family :class:`VideoProfile` with the same
ffmpeg we already use for audio (see :mod:`iamped.sync.base`).

The profiles are deliberately conservative — H.264 Baseline at the device's
maximum supported resolution — because the classic iPods reject anything outside
their decoder envelope (wrong profile/level, too-high resolution → "this video
cannot be played"). Aspect ratio is preserved; we only ever downscale.
"""
from __future__ import annotations

import os
import platform
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class VideoProfile:
    name: str
    container: str          # ".m4v" | ".mp4"
    vcodec: str             # ffmpeg encoder, e.g. "libx264"
    vprofile: str           # H.264 profile, e.g. "baseline" | "high"
    max_w: int
    max_h: int
    vbitrate_k: int
    acodec: str             # "aac"
    abitrate_k: int
    fps_cap: int            # 0 = no cap
    ipod_mode: bool = False  # emit with ffmpeg's "-f ipod" muxer


# Click-wheel / classic / nano video iPods: H.264 Baseline 3.0, 640x480 max,
# ~1.5 Mbps, AAC stereo. The "-f ipod" muxer writes the .m4v atoms these models
# expect.
IPOD_VIDEO = VideoProfile(
    name="iPod video", container=".m4v", vcodec="libx264", vprofile="baseline",
    max_w=640, max_h=480, vbitrate_k=1500, acodec="aac", abitrate_k=160,
    fps_cap=30, ipod_mode=True)

# Creative Zen / generic MTP players. Most index and play 640x480 H.264 MP4;
# best-effort (see mtp backend note).
MTP_VIDEO = VideoProfile(
    name="MTP player", container=".mp4", vcodec="libx264", vprofile="baseline",
    max_w=640, max_h=480, vbitrate_k=1500, acodec="aac", abitrate_k=160,
    fps_cap=30, ipod_mode=False)

# Generic USB mass-storage players (and anything we just copy files onto). These
# usually have more capable decoders, so allow 720p High.
GENERIC_VIDEO = VideoProfile(
    name="Generic player", container=".mp4", vcodec="libx264", vprofile="high",
    max_w=1280, max_h=720, vbitrate_k=2500, acodec="aac", abitrate_k=192,
    fps_cap=0, ipod_mode=False)


# --------------------------------------------------------------------------- encoder
# Software x264 at the default preset re-encodes a feature film in real time or
# slower — minutes to hours. Every platform we target ships a hardware H.264
# encoder that offloads this to a dedicated media engine (10-30x faster, almost
# no CPU), so we use it whenever present and fall back to a *fast* x264 preset
# only when no accelerator is available.
_HW_ENCODER: Optional[str] = None  # None = not yet probed, "" = none found


def _detect_hw_h264() -> str:
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001 - probing must never break a sync
        return ""
    if platform.system() == "Darwin" and "h264_videotoolbox" in out:
        return "h264_videotoolbox"       # Apple Silicon / Intel Mac media engine
    if "h264_nvenc" in out:
        return "h264_nvenc"              # NVIDIA NVENC
    if "h264_qsv" in out:
        return "h264_qsv"                # Intel Quick Sync
    return ""


def hw_h264_encoder() -> str:
    """Name of the hardware H.264 encoder for this machine, or "" if none. Probed
    once and cached."""
    global _HW_ENCODER
    if _HW_ENCODER is None:
        _HW_ENCODER = _detect_hw_h264()
    return _HW_ENCODER


def _encoder_args(profile: VideoProfile) -> tuple[str, list[str]]:
    """Pick the encoder and its tuning flags. Hardware encoders honour
    ``-profile:v`` and ``-b:v`` just like x264, so the device envelope (Baseline
    for iPods, High for generic) is preserved either way — verified to emit
    Baseline/level-3.0 from videotoolbox, which the click-wheel iPods accept."""
    enc = hw_h264_encoder()
    if enc == "h264_videotoolbox":
        # -allow_sw 1 lets it fall back to software if the media engine is busy,
        # so an encode never hard-fails; -realtime 0 favours quality over latency.
        return enc, ["-profile:v", profile.vprofile, "-allow_sw", "1",
                     "-realtime", "0"]
    if enc == "h264_nvenc":
        return enc, ["-profile:v", profile.vprofile, "-preset", "p2"]
    if enc == "h264_qsv":
        return enc, ["-profile:v", profile.vprofile, "-preset", "veryfast"]
    return "libx264", ["-profile:v", profile.vprofile, "-preset", "veryfast"]


def _scale_filter(profile: VideoProfile) -> str:
    """Aspect-preserving downscale clamped to the profile's max dimensions, with
    both output dimensions forced even (H.264 requires it)."""
    return (
        f"scale=w='min({profile.max_w},iw)':h='min({profile.max_h},ih)':"
        "force_original_aspect_ratio=decrease,"
        "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    )


def transcode_video(src: str, dst: str, profile: VideoProfile,
                    duration_ms: int = 0,
                    progress: Optional[Callable[[float], None]] = None) -> str:
    """Transcode *src* to *dst* in the device's :class:`VideoProfile`. Raises on
    ffmpeg failure. Writes atomically via a ``.part`` temp file.

    Uses a hardware encoder when available (see :func:`hw_h264_encoder`). If
    *duration_ms* and *progress* are given, ffmpeg's ``-progress`` stream is
    parsed and *progress* is called with a 0..1 completion fraction as encoding
    advances, so the UI can show a live bar instead of a frozen one."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".part" + profile.container
    vcodec, vargs = _encoder_args(profile)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostats",
        "-i", src,
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", _scale_filter(profile),
        "-c:v", vcodec, *vargs,
        "-b:v", f"{profile.vbitrate_k}k", "-pix_fmt", "yuv420p",
        "-c:a", profile.acodec, "-b:a", f"{profile.abitrate_k}k", "-ac", "2",
        "-movflags", "+faststart",
    ]
    if profile.fps_cap:
        cmd += ["-r", str(profile.fps_cap)]
    if profile.ipod_mode:
        cmd += ["-f", "ipod"]
    cmd += ["-progress", "pipe:1", tmp]

    total_us = int(duration_ms) * 1000 if duration_ms else 0
    # stderr to a temp file so a chatty encoder can't deadlock against our
    # line-by-line read of the progress stream on stdout.
    with tempfile.TemporaryFile() as errf:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf,
                                text=True)
        for line in proc.stdout:                     # one key=value per line
            if not (progress and total_us):
                continue
            line = line.strip()
            if line.startswith("out_time_us="):
                val = line.split("=", 1)[1]
                if val.isdigit():
                    progress(max(0.0, min(0.999, int(val) / total_us)))
        proc.wait()
        if proc.returncode != 0:
            errf.seek(0)
            raise subprocess.CalledProcessError(
                proc.returncode, cmd,
                stderr=errf.read().decode("utf-8", "replace"))
    if progress:
        progress(1.0)
    os.replace(tmp, dst)
    return dst


def should_transcode_video(meta, profile: VideoProfile) -> bool:
    """Skip transcoding only when the source is already H.264 within the
    profile's container and dimension envelope; otherwise transcode."""
    want_container = profile.container.lstrip(".")
    container = (getattr(meta, "container", "") or "").lower()
    codec = (getattr(meta, "video_codec", "") or "").lower()
    w = getattr(meta, "width", None) or 0
    h = getattr(meta, "height", None) or 0
    compatible = (
        codec in ("h264", "avc1")
        and container in (want_container, "mp4", "m4v")
        and 0 < w <= profile.max_w and 0 < h <= profile.max_h
    )
    return not compatible
