"""Cross-platform device discovery for iAmped.

The rest of the app asks one thing of this package: *what can I sync to right
now, and what shape is it in?* :func:`list_devices` answers that on macOS,
Windows and Linux, merging two sources:

    - mounted volumes the OS already knows about (:mod:`.volumes`)
    - iPods found by reading raw disks, including HFS+ iPods the host cannot
      mount (:mod:`.rawdisk`) — the basis of the rescue→convert flow

Results are de-duplicated so a single physical iPod never appears twice.
"""
from __future__ import annotations

import re

from .model import Device, host_writability
from . import volumes, rawdisk, fsdetect

__all__ = ["Device", "host_writability", "list_devices", "fsdetect"]

# Strip a partition suffix to get the parent whole-disk node, so a mounted
# volume (/dev/disk4s2, /dev/sdb2) can be matched to a raw disk (/dev/disk4,
# /dev/sdb).
_WHOLE_DISK_RE = [
    (re.compile(r"^(/dev/disk\d+)s\d+$"), r"\1"),        # macOS
    (re.compile(r"^(/dev/nvme\d+n\d+)p\d+$"), r"\1"),    # Linux NVMe
    (re.compile(r"^(/dev/mmcblk\d+)p\d+$"), r"\1"),      # Linux SD/eMMC
    # Linux sd*/hd*/vd* only — must NOT match macOS /dev/diskN, whose whole-disk
    # node has no partition suffix and is handled by the diskNsM rule above.
    (re.compile(r"^(/dev/(?:sd|hd|vd|xvd)[a-z]+)\d+$"), r"\1"),
]


def _whole_disk(dev_path: str | None) -> str | None:
    if not dev_path:
        return None
    for rx, repl in _WHOLE_DISK_RE:
        if rx.match(dev_path):
            return rx.sub(repl, dev_path)
    return dev_path


def list_devices(include_raw: bool = True) -> list[Device]:
    """All sync targets for this host, de-duplicated.

    ``include_raw`` scans physical disks for unmountable iPods; set False to
    skip it (e.g. to avoid the admin prompt) and only list mounted volumes.
    """
    mounted = volumes.mounted_devices()
    if not include_raw:
        return mounted

    mounted_disks = {_whole_disk(d.raw_path) for d in mounted if d.raw_path}
    mounted_has_ipod = any(d.is_ipod for d in mounted)

    extra: list[Device] = []
    for raw in rawdisk.raw_ipods():
        # Correlate against mounted volumes by parent whole-disk node.
        if _whole_disk(raw.raw_path) in mounted_disks:
            continue
        # Windows can't correlate by path (mounts are drive letters). There, a
        # FAT iPod would already be mounted, so a raw FAT hit is a duplicate of
        # an existing mount; only Apple-formatted (unmountable) iPods are new.
        if (raw.raw_path or "").upper().startswith("\\\\.\\PHYSICALDRIVE"):
            if mounted_has_ipod and fsdetect.is_fat(raw.fs):
                continue
        extra.append(raw)

    return mounted + extra
