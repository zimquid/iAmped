"""The unified device model shared by every part of iAmped.

A *device* is anything the user might sync to: a mounted USB stick, a FAT32
iPod, or a Mac-formatted (HFS+) iPod that the current host can see at the disk
level but not mount. The model captures, in one place, the two questions the UI
and sync code actually ask:

    - *What is it?*   (kind, filesystem, is it an iPod, what format)
    - *Can I use it here, right now?*  (mounted? writable on THIS host?)

The second question is the crux of cross-ecosystem support, so the writability
rules live here, keyed on the running OS.
"""
from __future__ import annotations

import platform
from dataclasses import dataclass, asdict
from typing import Optional

from . import fsdetect

_SYSTEM = platform.system()  # "Darwin" | "Windows" | "Linux"


def host_writability(fs: str) -> tuple[bool, str]:
    """Can the *current* OS write this filesystem with stock drivers?

    Returns ``(writable, note)`` where ``note`` explains a False (or a caveat
    on a True). This is the rule table behind iAmped's whole reason to exist.
    """
    fs = (fs or "").lower()

    # FAT family is the universal exchange format — writable everywhere.
    if fsdetect.is_fat(fs):
        return True, ""

    if fs == "ntfs":
        if _SYSTEM == "Windows":
            return True, ""
        if _SYSTEM == "Linux":
            return True, "via ntfs3 driver"
        return False, "NTFS is read-only on macOS without a third-party driver"

    if fs in ("hfs+", "hfsx", "hfs"):
        if _SYSTEM == "Darwin":
            return True, ""
        if _SYSTEM == "Linux":
            return False, ("HFS+ write on Linux needs journaling disabled and "
                           "is unsafe — convert to FAT32 instead")
        return False, ("Mac-formatted (HFS+) — Windows cannot write it without "
                       "Paragon/MacDrive; convert to FAT32 to manage here")

    if fs == "apfs":
        if _SYSTEM == "Darwin":
            return True, ""
        return False, "APFS is not writable on this OS"

    if fs in ("", "unknown", "raw"):
        return False, "Unrecognized or unreadable filesystem"

    return False, f"Unsupported filesystem: {fs}"


@dataclass
class Device:
    """A target the user can pick. Capacity fields are only meaningful when the
    device is mounted; raw/unmounted devices report ``total`` from the partition
    size when known and ``free == 0``.
    """

    id: str                       # stable handle: mountpoint, or raw disk id
    name: str                     # human label (volume name / model)
    fs: str                       # canonical filesystem (see fsdetect)

    mountpoint: Optional[str] = None   # path if mounted, else None
    raw_path: Optional[str] = None     # \\.\PhysicalDriveN or /dev/diskN

    total: int = 0
    free: int = 0

    mounted: bool = False
    is_ipod: bool = False
    ipod_format: Optional[str] = None  # the iPod data partition's fs
    ipod_model: str = ""               # friendly model, e.g. "iPod nano (1st generation)"
    ipod_generation: str = ""          # coarse generation label, e.g. "iPod nano (1st generation)"

    model: str = ""                    # USB product / media name, e.g. "MuVo TX FM"
    transport: str = ""                # "ums" | "mtp" | "ipod" (filled by classify)
    mtp_busloc: str = ""               # libmtp bus/dev address for MTP devices

    # Derived capability for the running host; filled by capability().
    writable: bool = False
    note: str = ""

    @property
    def kind(self) -> str:
        """Coarse category used by the UI and to pick a sync backend."""
        if self.is_ipod:
            return "ipod"
        return "massstorage"

    @property
    def needs_conversion(self) -> bool:
        """True for an iPod this host can't write as-is but could after a
        convert-to-FAT32 (the rescue→convert flow)."""
        return self.is_ipod and not self.writable and fsdetect.is_apple(self.fs)

    def capability(self) -> "Device":
        """Populate ``writable``/``note`` from the host rules. A mounted Apple
        volume implies a working third-party driver is present, so we treat it
        as writable even on Windows/Linux (with a caveat note)."""
        writable, note = host_writability(self.fs)
        if self.mounted and fsdetect.is_apple(self.fs) and not writable:
            # It mounted read/write somewhere it normally can't → a driver
            # (Paragon/MacDrive, or macFUSE) is doing the work. Trust the mount.
            writable, note = True, "via third-party driver"
        self.writable = writable and (self.mounted or self.fs != "")
        self.note = note
        return self

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind"] = self.kind
        d["needs_conversion"] = self.needs_conversion
        return d
