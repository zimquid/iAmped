"""Identify a connected iPod from the USB bus — the way iTunes does it.

When an iPod's on-disk ``SysInfo`` is blank (restored units, iPods never
managed by iTunes, libimobiledevice restores), there is still a perfectly
reliable signal: the device announces itself over USB with Apple's vendor ID
(0x05AC) and a product ID that pins down the generation (e.g. 0x120A = iPod
nano 1st gen). iTunes on Windows reads exactly this. We do the same per OS and,
where possible, correlate the USB device back to the specific mounted disk so
the right model lands on the right volume when several are attached.

Everything here is best-effort and wrapped so a failure only means "no USB
hint", never a crash in device enumeration.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import struct
import subprocess

from . import ipodmodel

_SYSTEM = platform.system()
_APPLE_VID = 0x05AC
_HASH58_GENERATIONS = {
    "iPod nano (3rd generation)", "iPod nano (4th generation)", "iPod classic",
}
_GENERATION_CAPACITIES = {
    "iPod nano (1st generation)": {"1 GB", "2 GB", "4 GB"},
    "iPod nano (2nd generation)": {"2 GB", "4 GB", "8 GB"},
    "iPod nano (3rd generation)": {"4 GB", "8 GB"},
    "iPod nano (4th generation)": {"4 GB", "8 GB", "16 GB"},
    "iPod classic": {"80 GB", "120 GB", "160 GB"},
}


def _whole(name: str) -> str:
    """Strip a partition suffix from a block-device basename.

    disk4s2→disk4, sdb1→sdb, mmcblk0p1→mmcblk0, nvme0n1p1→nvme0n1.
    """
    for rx, repl in (
        (r"^(disk\d+)s\d+$", r"\1"),
        (r"^(nvme\d+n\d+)p\d+$", r"\1"),
        (r"^(mmcblk\d+)p\d+$", r"\1"),
        (r"^((?:sd|hd|vd|xvd)[a-z]+)\d+$", r"\1"),
    ):
        m = re.match(rx, name)
        if m:
            return re.sub(rx, repl, name)
    return name


# --------------------------------------------------------------------------- #
# Per-OS enumeration → list of {"pid": int, "serial": str, "bsd": set[str]}    #
# --------------------------------------------------------------------------- #
def _macos_raw() -> list[dict]:
    # Modern macOS no longer accepts the historical
    # ``ioreg -rc IOUSBHostDevice -a`` query reliably. Parse the stable text
    # IOUSB plane, one ``+-o`` device block at a time.
    try:
        out = subprocess.run(
            ["ioreg", "-p", "IOUSB", "-l", "-w", "0"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []

    found: list[dict] = []
    blocks = re.split(r"(?m)^(?=\s*[| ]*\+-o\s)", out)
    for block in blocks:
        vid = re.search(r'"idVendor"\s*=\s*(\d+)', block)
        pid = re.search(r'"idProduct"\s*=\s*(\d+)', block)
        if not (vid and pid) or int(vid.group(1)) != _APPLE_VID:
            continue
        serial = re.search(
            r'"(?:USB Serial Number|kUSBSerialNumberString)"\s*=\s*"([^"]+)"',
            block,
        )
        found.append({
            "pid": int(pid.group(1)),
            "serial": serial.group(1) if serial else "",
            "bsd": set(re.findall(r'"BSD Name"\s*=\s*"([^"]+)"', block)),
        })
    return found


def _linux_raw() -> list[dict]:
    base = "/sys/bus/usb/devices"
    out: list[dict] = []
    try:
        entries = os.listdir(base)
    except OSError:
        return []
    for name in entries:
        d = os.path.join(base, name)
        try:
            with open(os.path.join(d, "idVendor")) as fh:
                if int(fh.read().strip(), 16) != _APPLE_VID:
                    continue
            with open(os.path.join(d, "idProduct")) as fh:
                pid = int(fh.read().strip(), 16)
        except (OSError, ValueError):
            continue
        serial = ""
        try:
            with open(os.path.join(d, "serial")) as fh:
                serial = fh.read().strip()
        except OSError:
            pass
        # Correlate to block devices that live under this USB node.
        bsd: set[str] = set()
        for root, dirs, _files in os.walk(d):
            if os.path.basename(root) == "block":
                bsd.update(dirs)
        out.append({"pid": pid, "serial": serial, "bsd": bsd})
    return out


def _windows_raw() -> list[dict]:
    # PnP device instance IDs look like USB\VID_05AC&PID_120A\000A2700128DF29C.
    ps = ("Get-CimInstance Win32_PnPEntity | "
          "Where-Object { $_.PNPDeviceID -like 'USB*VID_05AC*' } | "
          "Select-Object -ExpandProperty PNPDeviceID")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=12).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    res: list[dict] = []
    for line in out.splitlines():
        m = re.search(r"PID_([0-9A-Fa-f]{4}).*?\\([^\\\s]+)\s*$", line)
        if not m:
            m = re.search(r"PID_([0-9A-Fa-f]{4})", line)
            if not m:
                continue
            res.append({"pid": int(m.group(1), 16), "serial": "", "bsd": set()})
            continue
        res.append({"pid": int(m.group(1), 16), "serial": m.group(2), "bsd": set()})
    return res


def _enumerate() -> list[dict]:
    try:
        if _SYSTEM == "Darwin":
            return _macos_raw()
        if _SYSTEM == "Linux":
            return _linux_raw()
        if _SYSTEM == "Windows":
            return _windows_raw()
    except Exception:  # noqa: BLE001 - detection must never break enumeration
        return []
    return []


def ipod_models() -> list[dict]:
    """Connected Apple iPods (mass-storage PIDs only), de-duplicated.

    Each entry: ``{"pid", "serial", "bsd": set, "model", "generation"}``.
    """
    # One physical iPod shows up several times (ioreg lists it once per USB
    # interface; some records carry the serial, some don't). Collapse records
    # that describe the same device — same disk if we have BSD names, else same
    # (pid, serial) — merging in whichever copy actually had the serial.
    merged: dict[tuple, dict] = {}
    for r in _enumerate():
        pid = r["pid"]
        if pid not in ipodmodel.IPOD_USB_PIDS:
            continue  # an Apple device, but not an iPod in disk mode
        bsd = set(r.get("bsd") or [])
        serial = r.get("serial") or ""
        key = (pid, frozenset(bsd)) if bsd else (pid, serial)
        cur = merged.get(key)
        if cur is None:
            name = ipodmodel.model_from_usb_pid(pid)
            merged[key] = {"pid": pid, "serial": serial, "bsd": bsd,
                           "model": name, "generation": name}
        else:
            cur["bsd"] |= bsd
            cur["serial"] = cur["serial"] or serial
    return list(merged.values())


def match(raw_path: str | None = None, mountpoint: str | None = None) -> dict | None:
    """Best USB model hint for a specific mounted iPod.

    Correlates by the disk's BSD/block name when we know it (``raw_path``);
    otherwise, if exactly one iPod is on the bus, returns that one.
    """
    ipods = ipod_models()
    if not ipods:
        return None
    if raw_path:
        base = os.path.basename(raw_path.rstrip("/\\"))
        whole = _whole(base)
        for ip in ipods:
            if base in ip["bsd"] or whole in ip["bsd"]:
                return ip
    if mountpoint and len(ipods) > 1:
        # macOS sometimes omits BSD names from the USB device record. The
        # mounted iTunesDB still tells us whether this is a hash58 generation,
        # which is enough to distinguish common simultaneously-connected
        # legacy and nano-video devices without guessing from volume names.
        db_path = os.path.join(
            mountpoint, "iPod_Control", "iTunes", "iTunesDB")
        scheme = None
        try:
            with open(db_path, "rb") as fh:
                header = fh.read(0xF4)
            if header[:4] == b"mhbd" and len(header) >= 0x32:
                scheme = struct.unpack_from("<H", header, 0x30)[0] \
                    if struct.unpack_from("<I", header, 4)[0] >= 0xF4 else 0
        except (OSError, struct.error):
            pass
        if scheme is not None:
            expects_hash58 = scheme == 1
            candidates = [
                ip for ip in ipods
                if (ip.get("generation") in _HASH58_GENERATIONS) == expects_hash58
            ]
            if len(candidates) == 1:
                return candidates[0]

        try:
            cap = ipodmodel.nominal_capacity(shutil.disk_usage(mountpoint).total)
        except OSError:
            cap = ""
        candidates = [
            ip for ip in ipods
            if cap in _GENERATION_CAPACITIES.get(ip.get("generation"), set())
        ]
        if len(candidates) == 1:
            return candidates[0]
    if len(ipods) == 1:
        return ipods[0]
    return None
