"""Decide *how* to sync to a device — automatically, the way iTunes/MediaMonkey
pick a transport without asking the user.

Traditional players fall into a few families that need fundamentally different
treatment:

    - **iPod** — proprietary iTunesDB, handled by the iPod backend.
    - **MTP / database players** (Creative Zen, many modern players) — speak the
      Media Transfer Protocol; files must be handed over with metadata so the
      device indexes them into its own library. A plain file copy in mass-storage
      mode often leaves songs invisible.
    - **Flat-scan USB players** (Creative MuVo, lots of cheap players) — read the
      raw FAT filesystem but only recognise tracks in the root or ONE folder
      level deep. Per the MuVo manual: "Tracks stored in sub-folders of a folder
      will not be recognized."
    - **Recursive USB players** (Rockbox, most SanDisk Sansa) — walk the whole
      tree and usually read ID3 tags too, so a tidy ``Music/Artist/Album`` tree
      is fine.

:func:`classify` maps a :class:`~iamped.devices.model.Device` to a
:class:`Capability` describing the transport and on-disk layout to use. The
result is advisory: a saved device profile may override either field.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, asdict


# Layouts a mass-storage device can be written with.
LAYOUT_NESTED = "nested"   # Music/<Artist>/<Album>/<Track>   (recursive players)
LAYOUT_FLAT = "flat"       # <Artist> - <Album>/<Track>       (root + 1 level)

# Transports.
TRANSPORT_UMS = "ums"      # mount as a disk, copy files
TRANSPORT_MTP = "mtp"      # Media Transfer Protocol, hand files to the device
TRANSPORT_IPOD = "ipod"    # iTunesDB

# USB model-name markers (lower-cased substring match) for players that only
# scan the root and one folder level. These need the flat layout to be visible.
_FLAT_SCAN_MARKERS = (
    "muvo",          # Creative MuVo / MuVo TX / MuVo V100 / N200 / Slim
    "zen stone",     # Creative Zen Stone (UMS, flat scanner — distinct from MTP Zens)
    "zen nano",
    "nomad",         # Creative NOMAD MuVo
)

# Markers for players known to recurse and read tags — keep the tidy nested tree.
_RECURSIVE_MARKERS = (
    "rockbox",
    "sansa",         # SanDisk Sansa (Clip/Fuze) in MSC mode recurse + read tags
    "clip",
)


@dataclass
class Capability:
    transport: str          # TRANSPORT_*
    layout: str             # LAYOUT_* (or "ipod"/"mtp" — informational)
    reason: str             # human explanation of why this was auto-picked
    source: str = "auto"    # "auto" | "override"

    def to_dict(self) -> dict:
        return asdict(self)


def _has_rockbox(mountpoint: str | None) -> bool:
    if not mountpoint:
        return False
    try:
        return os.path.isdir(os.path.join(mountpoint, ".rockbox"))
    except OSError:
        return False


def is_plain_volume(device) -> bool:
    """True for a generic mounted volume with no player identity — a plain SD
    card or unbranded USB drive, as opposed to an iPod, an MTP player, or a
    recognised USB player.

    These are hidden from the device list unless the user turns on SD-card mode
    (File → Enable SD card), so the app stays focused on real players by default.
    """
    if getattr(device, "is_ipod", False):
        return False
    if getattr(device, "transport", "") == TRANSPORT_MTP or \
            getattr(device, "mtp_busloc", ""):
        return False
    if not getattr(device, "mounted", False):
        return False
    if _has_rockbox(getattr(device, "mountpoint", None)):
        return False
    name = f"{device.name} {device.model}".lower()
    if any(m in name for m in _RECURSIVE_MARKERS) or \
            any(m in name for m in _FLAT_SCAN_MARKERS):
        return False
    return True


def classify(device, sd_mode: bool = False) -> Capability:
    """Best-guess transport + layout for *device*. Never raises.

    ``sd_mode`` reflects the user's File → Enable SD card toggle: when on, a
    plain mounted card/drive is treated as an SD-card target (nested Music tree)
    rather than falling through to the flat unknown-player default.
    """
    name = f"{device.name} {device.model}".lower()

    # 1. iPod — its own database format.
    if getattr(device, "is_ipod", False):
        return Capability(TRANSPORT_IPOD, "ipod", "iPod — uses the iTunes database")

    # 2. MTP device (discovered via libmtp; never mounts as a disk).
    if getattr(device, "transport", "") == TRANSPORT_MTP or device.mtp_busloc:
        return Capability(
            TRANSPORT_MTP, "mtp",
            "MTP player — files are handed to the device so it can index them")

    # 3. Rockbox / known recursive players — keep the tidy nested tree.
    if _has_rockbox(device.mountpoint):
        return Capability(
            TRANSPORT_UMS, LAYOUT_NESTED,
            "Rockbox firmware — reads nested folders and tags")
    if any(m in name for m in _RECURSIVE_MARKERS):
        return Capability(
            TRANSPORT_UMS, LAYOUT_NESTED,
            "Known recursive player — handles nested folders")

    # 4. Known flat-scan player → must be flat to be visible.
    if any(m in name for m in _FLAT_SCAN_MARKERS):
        return Capability(
            TRANSPORT_UMS, LAYOUT_FLAT,
            "Flat-scan player — only sees root and one folder level")

    # 5. SD card / plain USB drive, only when the user has enabled SD-card mode.
    #    Given a real filesystem to browse, a card in a phone, car head-unit or
    #    Rockbox player reads a nested tree with tags, so use the tidy
    #    Music/<Artist>/<Album> layout rather than the flat fallback.
    if sd_mode and is_plain_volume(device):
        try:
            device.is_sd = True
        except AttributeError:
            pass
        return Capability(
            TRANSPORT_UMS, LAYOUT_NESTED,
            "SD card — clean nested Music/Artist/Album tree")

    # 6. Unknown mass-storage device: default to FLAT. One-level folders are
    #    recognised by both flat-scan and recursive players, so it is the safe
    #    universal choice; a nested tree breaks the flat scanners.
    return Capability(
        TRANSPORT_UMS, LAYOUT_FLAT,
        "Unknown USB player — flat layout for maximum compatibility")


def video_profile(device, capability: "Capability"):
    """Pick the :class:`~iamped.sync.video.VideoProfile` for *device*, or
    ``None`` when it can't play video iAmped can sync.

    - iPod → the iPod video profile, but only for video-capable generations
      (5G/5.5G video, classic, nano 3G–5G); ``None`` for older/touch models.
    - MTP player → the MTP/Zen profile (best-effort).
    - mass-storage player → the generic 720p profile.
    """
    from ..sync import video  # local import avoids an import cycle

    if capability.transport == TRANSPORT_IPOD:
        from . import ipodmodel
        label = getattr(device, "ipod_generation", "") or \
            getattr(device, "ipod_model", "") or getattr(device, "model", "")
        return video.IPOD_VIDEO if ipodmodel.is_video_capable(label) else None
    if capability.transport == TRANSPORT_MTP:
        return video.MTP_VIDEO
    if capability.transport == TRANSPORT_UMS:
        return video.GENERIC_VIDEO
    return None


def resolve(device, profile: dict | None = None,
            sd_mode: bool = False) -> Capability:
    """Auto-classify *device*, then apply any ``transport``/``layout`` override
    from a saved device profile."""
    cap = classify(device, sd_mode=sd_mode)
    if not profile:
        return cap
    over_transport = profile.get("transport")
    over_layout = profile.get("layout")
    if over_transport or over_layout:
        return Capability(
            transport=over_transport or cap.transport,
            layout=over_layout or cap.layout,
            reason="Overridden in device settings",
            source="override")
    return cap
