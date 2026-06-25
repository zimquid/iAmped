"""Filesystem identification from raw bytes.

The OS tells us the filesystem of *mounted* volumes, but the whole point of
iAmped's cross-ecosystem support is handling iPods the host OS *cannot* mount —
most importantly an HFS+ ("Mac-formatted") iPod plugged into Windows, where it
shows up as a RAW/unmounted volume. To classify those we read the first few
kilobytes of the partition ourselves and match on-disk signatures.

Everything here is pure and side-effect free: callers hand us a ``bytes`` blob
(read from a mounted volume's backing device, or from a raw partition slice) and
we return a canonical filesystem name. Detection works on the *partition* level
(give us bytes starting at the partition's first sector).

Canonical names returned: ``fat32``, ``fat16``, ``fat12``, ``exfat``, ``ntfs``,
``hfs+``, ``hfsx``, ``hfs``, ``apfs``, or ``unknown``.
"""
from __future__ import annotations

# How many bytes a caller should read for a reliable verdict. The HFS+ volume
# header lives at offset 1024 and is 512 bytes long, so 1536 is the minimum;
# we ask for a bit more headroom.
SNIFF_LEN = 2048

# Offsets within a partition's first sector(s).
_HFS_VH_OFFSET = 1024          # HFS/HFS+ volume header (and HFS wrapper)
_APFS_MAGIC_OFFSET = 32        # nx_magic within the APFS container superblock


def sniff_fs(data: bytes) -> str:
    """Identify the filesystem at the start of a partition.

    ``data`` must begin at the partition's first byte (sector 0 of the volume).
    Returns a canonical lowercase name, or ``"unknown"``.
    """
    if not data or len(data) < 512:
        return "unknown"

    # --- exFAT / NTFS: 8-byte OEM signature at offset 3 ---------------------
    oem = data[3:11]
    if oem == b"EXFAT   ":
        return "exfat"
    if oem == b"NTFS    ":
        return "ntfs"

    # --- APFS container superblock: magic "NXSB" at offset 32 ---------------
    # iPods are never APFS, but identifying it avoids mislabelling modern Mac
    # media and lets the UI say "unsupported format" rather than "unknown".
    if data[_APFS_MAGIC_OFFSET:_APFS_MAGIC_OFFSET + 4] == b"NXSB":
        return "apfs"

    # --- HFS+ / HFSX / HFS: 2-byte signature at offset 1024 -----------------
    if len(data) >= _HFS_VH_OFFSET + 2:
        sig = data[_HFS_VH_OFFSET:_HFS_VH_OFFSET + 2]
        if sig == b"H+":
            return "hfs+"
        if sig == b"HX":
            return "hfsx"
        if sig == b"BD":
            # Old HFS, or an HFS wrapper around an embedded HFS+ volume (how
            # early Macs shipped HFS+). Either way the *host* can't write it,
            # which is all iAmped needs to know — report it as plain hfs.
            return "hfs"

    # --- FAT family: filesystem-type string + boot signature ---------------
    # FAT32 stores "FAT32   " at offset 82; FAT12/16 store theirs at offset 54.
    # We also require the 0x55AA boot signature to avoid false positives.
    boot_sig = data[510:512] == b"\x55\xaa"
    if boot_sig:
        if data[82:90] == b"FAT32   ":
            return "fat32"
        fat1x = data[54:62]
        if fat1x == b"FAT16   ":
            return "fat16"
        if fat1x == b"FAT12   ":
            return "fat12"
        # Some FAT32 formatters leave the type string blank; fall back to the
        # BPB: 16-bit total-sectors == 0 and a non-zero 32-bit count, with a
        # FAT32-style sectors-per-FAT field, is FAT32.
        bytes_per_sec = int.from_bytes(data[11:13], "little")
        total16 = int.from_bytes(data[19:21], "little")
        total32 = int.from_bytes(data[32:36], "little")
        spf16 = int.from_bytes(data[22:24], "little")
        if bytes_per_sec in (512, 1024, 2048, 4096) and total16 == 0 \
                and total32 > 0 and spf16 == 0:
            return "fat32"

    return "unknown"


# Filesystems iAmped considers part of the "universal" FAT family — writable by
# every supported host OS, and therefore the conversion target for cross-
# ecosystem use.
FAT_FAMILY = frozenset({"fat32", "fat16", "fat12", "exfat"})
# Apple filesystems an iPod might use that aren't universally writable.
APPLE_FS = frozenset({"hfs+", "hfsx", "hfs", "apfs"})


def is_fat(fs: str) -> bool:
    return fs.lower() in FAT_FAMILY


def is_apple(fs: str) -> bool:
    return fs.lower() in APPLE_FS
