"""Find iPods at the *raw disk* level — including ones the host can't mount.

This is the module that makes cross-ecosystem support real. When you plug a
Mac-formatted (HFS+) iPod into Windows, Windows can't mount it; it appears as a
RAW, unmounted disk and :mod:`iamped.devices.volumes` never sees it. Here we go
under the OS: enumerate physical disks, parse their partition tables (MBR for
Windows-format iPods, Apple Partition Map for Mac-format ones, plus GPT), read a
few kilobytes from each partition and identify the filesystem ourselves.

Reading raw disks needs Administrator/root. Without it we degrade gracefully:
the disk still appears, flagged ``needs admin`` rather than silently dropped.

Only devices that *look like iPods* are emitted, so the user's system disk and
unrelated drives don't clutter the picker.
"""
from __future__ import annotations

import os
import platform
import struct
from dataclasses import dataclass

from . import fsdetect
from .model import Device

_SYSTEM = platform.system()
_SECTOR = 512

# GPT partition-type GUIDs we care about (stored mixed-endian on disk).
_GPT_HFS = bytes.fromhex("0053464800 00AA11 AA11 003065 43ECAC".replace(" ", ""))
_GPT_MSDATA = bytes.fromhex("A2A0D0EBE5B93344 87C0 68B6B72699C7")


# --------------------------------------------------------------------------- #
# Raw readers                                                                   #
# --------------------------------------------------------------------------- #
class _PosixReader:
    """Read arbitrary byte ranges from a block device (/dev/diskN, /dev/sdX)."""

    def __init__(self, path: str):
        self._f = open(path, "rb", buffering=0)

    def read_at(self, offset: int, length: int) -> bytes:
        self._f.seek(offset)
        return self._f.read(length)

    def close(self) -> None:
        try:
            self._f.close()
        except OSError:
            pass


class _WinReader:
    """Read from \\\\.\\PhysicalDriveN. Reads must be sector-aligned, so we
    round the offset down and the length up, then slice."""

    def __init__(self, path: str):
        import ctypes
        from ctypes import wintypes

        self._ct = ctypes
        GENERIC_READ = 0x80000000
        FILE_SHARE = 0x00000001 | 0x00000002  # read | write
        OPEN_EXISTING = 3
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32 = k32
        handle = k32.CreateFileW(
            ctypes.c_wchar_p(path), GENERIC_READ, FILE_SHARE, None,
            OPEN_EXISTING, 0, None)
        if handle == ctypes.c_void_p(-1).value or handle == -1:
            raise PermissionError(ctypes.get_last_error())
        self._h = handle

    def read_at(self, offset: int, length: int) -> bytes:
        ctypes = self._ct
        base = (offset // _SECTOR) * _SECTOR
        pad = offset - base
        size = ((pad + length + _SECTOR - 1) // _SECTOR) * _SECTOR
        # SetFilePointerEx
        newpos = ctypes.c_longlong(0)
        if not self._k32.SetFilePointerEx(self._h, ctypes.c_longlong(base),
                                          ctypes.byref(newpos), 0):
            return b""
        buf = ctypes.create_string_buffer(size)
        read = ctypes.c_ulong(0)
        if not self._k32.ReadFile(self._h, buf, size, ctypes.byref(read), None):
            return b""
        return buf.raw[pad:pad + length]

    def close(self) -> None:
        try:
            self._k32.CloseHandle(self._h)
        except Exception:
            pass


def _open_reader(path: str):
    if _SYSTEM == "Windows":
        return _WinReader(path)
    return _PosixReader(path)


# --------------------------------------------------------------------------- #
# Partition-table parsing                                                       #
# --------------------------------------------------------------------------- #
@dataclass
class _Part:
    start: int          # byte offset on the disk
    size: int           # bytes (0 if unknown)
    fs: str = "unknown"
    name: str = ""


def _parse_apm(reader) -> list[_Part]:
    """Apple Partition Map — used by Mac-formatted iPods. Big-endian."""
    ddm = reader.read_at(0, _SECTOR)
    if len(ddm) < 4 or ddm[0:2] != b"ER":
        return []
    blk = struct.unpack(">H", ddm[2:4])[0] or _SECTOR
    parts: list[_Part] = []
    count = 1
    i = 1
    while i <= count and i < 64:
        ent = reader.read_at(i * blk, max(_SECTOR, 512))
        if len(ent) < 84 or ent[0:2] != b"PM":
            break
        count = struct.unpack(">I", ent[4:8])[0] or count
        start_blk = struct.unpack(">I", ent[8:12])[0]
        size_blk = struct.unpack(">I", ent[12:16])[0]
        name = ent[16:48].split(b"\x00", 1)[0].decode("ascii", "replace")
        ptype = ent[48:80].split(b"\x00", 1)[0].decode("ascii", "replace")
        parts.append(_Part(start_blk * blk, size_blk * blk, name=name or ptype))
        i += 1
    return parts


def _parse_gpt(reader) -> list[_Part]:
    hdr = reader.read_at(_SECTOR, _SECTOR)
    if len(hdr) < 92 or hdr[0:8] != b"EFI PART":
        return []
    entry_lba = struct.unpack("<Q", hdr[72:80])[0]
    num = struct.unpack("<I", hdr[80:84])[0]
    esize = struct.unpack("<I", hdr[84:88])[0] or 128
    num = min(num, 128)
    blob = reader.read_at(entry_lba * _SECTOR, num * esize)
    parts: list[_Part] = []
    for k in range(num):
        ent = blob[k * esize:(k + 1) * esize]
        if len(ent) < 56 or ent[0:16] == b"\x00" * 16:
            continue
        first = struct.unpack("<Q", ent[32:40])[0]
        last = struct.unpack("<Q", ent[40:48])[0]
        name = ent[56:128].decode("utf-16-le", "replace").split("\x00", 1)[0]
        fs = "unknown"
        if ent[0:16] == _GPT_HFS:
            fs = "hfs+"
        parts.append(_Part(first * _SECTOR, (last - first + 1) * _SECTOR,
                           fs=fs, name=name))
    return parts


def _parse_mbr(reader) -> list[_Part]:
    """Classic MBR — used by Windows-formatted iPods. Little-endian."""
    mbr = reader.read_at(0, _SECTOR)
    if len(mbr) < 512 or mbr[510:512] != b"\x55\xaa":
        return []
    parts: list[_Part] = []
    for k in range(4):
        ent = mbr[446 + k * 16:446 + (k + 1) * 16]
        ptype = ent[4]
        lba = struct.unpack("<I", ent[8:12])[0]
        secs = struct.unpack("<I", ent[12:16])[0]
        if ptype == 0 or secs == 0:
            continue
        if ptype == 0xEE:  # protective MBR → real table is GPT
            return _parse_gpt(reader)
        fs = "hfs+" if ptype == 0xAF else "unknown"
        parts.append(_Part(lba * _SECTOR, secs * _SECTOR, fs=fs))
    return parts


def _partitions(reader) -> tuple[str, list[_Part]]:
    """Return (scheme, partitions) and fill in each partition's filesystem."""
    parts = _parse_apm(reader)
    scheme = "apm" if parts else ""
    if not parts:
        parts = _parse_mbr(reader)
        scheme = "mbr" if parts else ""
    # Sniff filesystem for partitions we couldn't type from the table alone.
    for p in parts:
        if p.fs in ("unknown", ""):
            try:
                head = reader.read_at(p.start, fsdetect.SNIFF_LEN)
                p.fs = fsdetect.sniff_fs(head)
            except OSError:
                p.fs = "unknown"
    return scheme, parts


# --------------------------------------------------------------------------- #
# iPod recognition                                                              #
# --------------------------------------------------------------------------- #
def _data_partition(parts: list[_Part]) -> _Part | None:
    """The audio/library partition is the largest data partition (the other is
    the small firmware partition)."""
    candidates = [p for p in parts if p.size > 0]
    return max(candidates, key=lambda p: p.size) if candidates else None


def _looks_like_ipod(scheme: str, parts: list[_Part], model: str,
                     removable: bool) -> bool:
    if "ipod" in (model or "").lower():
        return True
    if any("ipod" in (p.name or "").lower() for p in parts):
        return True
    # Classic two-partition iPod layout: a small (<300 MB) firmware partition
    # followed by a large FAT32/HFS+ data partition, on removable media.
    sized = sorted((p for p in parts if p.size > 0), key=lambda p: p.start)
    if removable and len(sized) >= 2:
        firmware = sized[0]
        data = _data_partition(parts)
        if (firmware.size < 300 * 1024 * 1024 and data is not None
                and data is not firmware
                and (fsdetect.is_fat(data.fs) or fsdetect.is_apple(data.fs))):
            return True
    return False


# --------------------------------------------------------------------------- #
# Physical-disk enumeration (per OS)                                            #
# --------------------------------------------------------------------------- #
@dataclass
class _Disk:
    path: str
    model: str = ""
    removable: bool = True


def _enumerate_linux() -> list[_Disk]:
    disks: list[_Disk] = []
    base = "/sys/block"
    try:
        names = os.listdir(base)
    except OSError:
        return disks
    for name in sorted(names):
        if name.startswith(("loop", "ram", "dm-", "sr", "zram")):
            continue
        try:
            with open(f"{base}/{name}/removable") as fh:
                removable = fh.read().strip() == "1"
        except OSError:
            removable = True
        model = ""
        for rel in ("device/model", "device/name"):
            try:
                with open(f"{base}/{name}/{rel}") as fh:
                    model = fh.read().strip()
                    break
            except OSError:
                continue
        disks.append(_Disk(f"/dev/{name}", model, removable))
    return disks


def _enumerate_macos() -> list[_Disk]:
    import plistlib
    import subprocess
    disks: list[_Disk] = []
    try:
        out = subprocess.run(["diskutil", "list", "-plist"],
                            capture_output=True, timeout=10).stdout
        plist = plistlib.loads(out)
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException):
        return disks
    for ident in plist.get("WholeDisks", []):
        model, removable = "", True
        try:
            info = plistlib.loads(subprocess.run(
                ["diskutil", "info", "-plist", ident],
                capture_output=True, timeout=10).stdout)
            model = info.get("MediaName", "") or info.get("IORegistryEntryName", "")
            removable = bool(info.get("RemovableMedia", info.get("Removable", True)))
        except Exception:
            pass
        disks.append(_Disk(f"/dev/{ident}", model, removable))
    return disks


def _enumerate_windows() -> list[_Disk]:
    # Probe PhysicalDrive0..15; CreateFile tells us which exist. Model/removable
    # are best-effort via WMI through PowerShell (optional).
    disks: list[_Disk] = []
    models: dict[int, tuple[str, bool]] = {}
    try:
        import subprocess
        ps = ("Get-CimInstance Win32_DiskDrive | "
              "Select-Object Index,Model,MediaType | ConvertTo-Json -Compress")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                            capture_output=True, text=True, timeout=15).stdout
        import json
        data = json.loads(out) if out.strip() else []
        if isinstance(data, dict):
            data = [data]
        for d in data:
            idx = int(d.get("Index", -1))
            removable = "removable" in str(d.get("MediaType", "")).lower()
            models[idx] = (str(d.get("Model", "")), removable)
    except Exception:
        pass

    for n in range(16):
        path = f"\\\\.\\PhysicalDrive{n}"
        try:
            r = _WinReader(path)
            r.close()
        except Exception:
            continue
        model, removable = models.get(n, ("", True))
        disks.append(_Disk(path, model, removable))
    return disks


def _enumerate_disks() -> list[_Disk]:
    if _SYSTEM == "Linux":
        return _enumerate_linux()
    if _SYSTEM == "Darwin":
        return _enumerate_macos()
    if _SYSTEM == "Windows":
        return _enumerate_windows()
    return []


# --------------------------------------------------------------------------- #
# Public API                                                                    #
# --------------------------------------------------------------------------- #
def raw_ipods() -> list[Device]:
    """iPods discovered by inspecting raw disks (mounted or not).

    The caller (``devices.list_devices``) is responsible for de-duplicating
    these against already-mounted volumes.
    """
    found: list[Device] = []
    for disk in _enumerate_disks():
        try:
            reader = _open_reader(disk.path)
        except PermissionError:
            # Can't read this disk without elevation. We can't confirm it's an
            # iPod, but if the OS model string says so, surface it anyway.
            if "ipod" in (disk.model or "").lower():
                found.append(Device(
                    id=disk.path, name=disk.model or "iPod", fs="unknown",
                    raw_path=disk.path, mounted=False, is_ipod=True,
                    note="needs admin to read this disk").capability())
            continue
        except OSError:
            continue

        try:
            scheme, parts = _partitions(reader)
        finally:
            reader.close()

        if not parts or not _looks_like_ipod(scheme, parts, disk.model,
                                            disk.removable):
            continue

        data = _data_partition(parts)
        if data is None:
            continue
        dev = Device(
            id=disk.path,
            name=disk.model or "iPod",
            fs=data.fs,
            raw_path=disk.path,
            total=data.size,
            free=0,
            mounted=False,
            is_ipod=True,
            ipod_format=data.fs,
        ).capability()
        found.append(dev)
    return found
