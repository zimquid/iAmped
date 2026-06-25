"""Identify *which* iPod is plugged in.

Every iPod records its factory model number in ``iPod_Control/Device/SysInfo``
(a ``ModelNumStr:`` line) and, on later units, in a ``SysInfoExtended`` plist.
We read that and map it to a friendly generation/capacity label so the UI can
say "iPod nano (1st generation) — 1 GB" instead of just "IPOD".

The table is a curated subset covering the models iAmped actually supports
(1G–5.5G, mini, nano 1G–3G); anything unknown falls back to the raw model
number so the user still sees *something* specific.
"""
from __future__ import annotations

import os
import re
from typing import Optional

# Keyed on the 4-character model core (the part after the region letter in a
# ModelNumStr like "xA350"). Value: (generation label, capacity/colour detail).
_MODEL_TABLE: dict[str, tuple[str, str]] = {
    # ---- original click/scroll/touch wheel ----
    "8541": ("iPod (1st generation)", ""),
    "8697": ("iPod (1st generation)", ""),
    "8709": ("iPod (1st generation)", ""),
    "8737": ("iPod (2nd generation)", ""),
    "8740": ("iPod (2nd generation)", ""),
    "8741": ("iPod (2nd generation)", ""),
    "8946": ("iPod (3rd generation)", ""),
    "8976": ("iPod (3rd generation)", ""),
    "9244": ("iPod (3rd generation)", ""),
    "9245": ("iPod (3rd generation)", ""),
    "9460": ("iPod (3rd generation)", ""),
    "9461": ("iPod (3rd generation)", ""),
    "9268": ("iPod (4th generation)", ""),
    "9282": ("iPod (4th generation)", ""),
    "9787": ("iPod (4th generation)", ""),
    "9788": ("iPod (4th generation)", ""),
    # ---- iPod photo / colour (4th gen family) ----
    "9585": ("iPod photo (4th generation)", ""),
    "9586": ("iPod photo (4th generation)", ""),
    "9830": ("iPod photo (4th generation)", ""),
    "A079": ("iPod photo (4th generation)", ""),
    "A127": ("iPod photo (4th generation)", ""),
    # ---- iPod mini ----
    "9160": ("iPod mini (1st generation)", ""),
    "9434": ("iPod mini (1st generation)", ""),
    "9435": ("iPod mini (1st generation)", ""),
    "9436": ("iPod mini (1st generation)", ""),
    "9437": ("iPod mini (1st generation)", ""),
    "9800": ("iPod mini (2nd generation)", ""),
    "9801": ("iPod mini (2nd generation)", ""),
    "9802": ("iPod mini (2nd generation)", ""),
    "9803": ("iPod mini (2nd generation)", ""),
    "9804": ("iPod mini (2nd generation)", ""),
    "9805": ("iPod mini (2nd generation)", ""),
    "9806": ("iPod mini (2nd generation)", ""),
    "9807": ("iPod mini (2nd generation)", ""),
    # ---- iPod nano 1G (the validated reference unit) ----
    "A350": ("iPod nano (1st generation)", "1 GB, white"),
    "A352": ("iPod nano (1st generation)", "2 GB, white"),
    "A004": ("iPod nano (1st generation)", "4 GB, white"),
    "A005": ("iPod nano (1st generation)", "2 GB, black"),
    "A099": ("iPod nano (1st generation)", "4 GB, black"),
    "A107": ("iPod nano (1st generation)", "1 GB, black"),
    # ---- iPod nano 2G (aluminium) ----
    "A477": ("iPod nano (2nd generation)", "2 GB"),
    "A426": ("iPod nano (2nd generation)", "4 GB"),
    "A428": ("iPod nano (2nd generation)", "4 GB"),
    "A487": ("iPod nano (2nd generation)", "8 GB"),
    "A489": ("iPod nano (2nd generation)", "8 GB"),
    # ---- iPod nano 3G (video) ----
    "A978": ("iPod nano (3rd generation)", "4 GB"),
    "B261": ("iPod nano (3rd generation)", "8 GB"),
    "B257": ("iPod nano (3rd generation)", "8 GB"),
    # ---- iPod 5G / 5.5G (video) ----
    "A002": ("iPod (5th generation, video)", "30 GB"),
    "A146": ("iPod (5th generation, video)", "60 GB"),
    "A444": ("iPod (5.5 generation, video)", "30 GB"),
    "A446": ("iPod (5.5 generation, video)", "30 GB"),
    "A448": ("iPod (5.5 generation, video)", "80 GB"),
}


# Apple USB product IDs (vendor 0x05AC) for iPods enumerated as mass storage.
# This is the signal iTunes itself keys on: it asks the *hardware* what it is
# over USB rather than trusting files on the disk, so it works even when the
# on-disk SysInfo is blank. Sourced from the canonical linux-usb.org usb.ids;
# DFU/WTF recovery-mode PIDs are intentionally omitted (those never mount).
_USB_PID_MODELS: dict[int, str] = {
    0x1201: "iPod (3rd generation)",
    0x1202: "iPod (2nd generation)",
    0x1203: "iPod (4th generation)",
    0x1204: "iPod photo",
    0x1205: "iPod mini",
    0x1209: "iPod (5th generation, video)",
    0x120A: "iPod nano (1st generation)",
    0x1260: "iPod nano (2nd generation)",
    0x1261: "iPod classic",
    0x1262: "iPod nano (3rd generation)",
    0x1263: "iPod nano (4th generation)",
    0x1265: "iPod nano (5th generation)",
    0x1266: "iPod nano (6th generation)",
    0x1267: "iPod nano (7th generation)",
    0x1291: "iPod touch (1st generation)",
    0x1293: "iPod touch (2nd generation)",
    0x1299: "iPod touch (3rd generation)",
    0x129E: "iPod touch (4th generation)",
    0x12AA: "iPod touch (5th generation)",
    0x1300: "iPod shuffle (1st generation)",
    0x1301: "iPod shuffle (2nd generation)",
    0x1302: "iPod shuffle (3rd generation)",
    0x1303: "iPod shuffle (4th generation)",
}

# These USB product IDs identify an Apple device as an iPod (mass-storage mode);
# used to tell an iPod apart from other Apple USB gear (keyboards, etc.).
IPOD_USB_PIDS = frozenset(_USB_PID_MODELS)


def model_from_usb_pid(pid: int) -> Optional[str]:
    """Friendly generation label for an Apple USB product ID, or None."""
    return _USB_PID_MODELS.get(pid)


def _device_dir(mountpoint: str) -> Optional[str]:
    for name in ("iPod_Control", "IPOD_CONTROL"):
        p = os.path.join(mountpoint, name, "Device")
        if os.path.isdir(p):
            return p
    return None


def _read_model_num(devdir: str) -> Optional[str]:
    """Pull ModelNumStr from SysInfo, falling back to SysInfoExtended."""
    sysinfo = os.path.join(devdir, "SysInfo")
    try:
        with open(sysinfo, "r", errors="replace") as fh:
            for line in fh:
                if line.lower().lstrip().startswith("modelnumstr"):
                    val = line.split(":", 1)[1].strip() if ":" in line else ""
                    if val:
                        return val
    except OSError:
        pass

    ext = os.path.join(devdir, "SysInfoExtended")
    try:
        with open(ext, "r", errors="replace") as fh:
            txt = fh.read()
        m = re.search(r"<key>\s*ModelNumStr\s*</key>\s*<string>([^<]+)</string>",
                      txt, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    except OSError:
        pass
    return None


def nominal_capacity(total_bytes: int) -> str:
    """Marketing capacity ("2 GB", "8 GB", …) from the raw partition size.

    iPod flash/disk sizes cluster on round nominal values, so the FAT total maps
    cleanly back to the label printed on the case.
    """
    if not total_bytes:
        return ""
    gib = total_bytes / (1024 ** 3)
    for hi, label in ((1.6, "1 GB"), (3, "2 GB"), (5, "4 GB"), (7, "6 GB"),
                      (10, "8 GB"), (14, "10 GB"), (18, "16 GB"), (26, "20 GB"),
                      (34, "30 GB"), (70, "60 GB"), (96, "80 GB"),
                      (140, "120 GB"), (180, "160 GB")):
        if gib < hi:
            return label
    return f"{round(gib)} GB"


def detect(mountpoint: str, total_bytes: int = 0,
           usb: Optional[dict] = None) -> Optional[dict]:
    """Identify the iPod mounted at ``mountpoint``.

    Detection mirrors how iTunes/libgpod resolve a model, in priority order:

    1. ``SysInfo``/``SysInfoExtended`` model number — exact, includes
       capacity/colour. Often blank on restored or non-iTunes-managed iPods.
    2. The USB product ID (passed in as ``usb``) — what iTunes on Windows keys
       on; gives the precise generation straight from the hardware. Combined
       with the case capacity from the disk size.
    3. Capacity alone — last resort, no generation claimed.

    Returns ``{"model", "generation", "detail", "model_number", "source"}`` or
    ``None`` if this isn't an iPod.
    """
    devdir = _device_dir(mountpoint)
    if not devdir:
        return None
    raw = _read_model_num(devdir)
    cap = nominal_capacity(total_bytes)

    if raw:
        key = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
        for code, (gen, detail) in _MODEL_TABLE.items():
            if code in key:
                model = f"{gen} — {detail}" if detail else gen
                return {"model": model, "generation": gen, "detail": detail,
                        "model_number": raw, "source": "sysinfo"}
        return {"model": f"iPod (model {raw})", "generation": "iPod",
                "detail": "", "model_number": raw, "source": "sysinfo"}

    # No on-disk model number → ask the hardware (USB), as iTunes does.
    if usb and usb.get("model"):
        gen = usb["model"]
        return {"model": f"{gen} · {cap}" if cap else gen, "generation": gen,
                "detail": cap, "model_number": "", "source": "usb"}

    # Last resort: the capacity printed on the case, no generation guessed.
    return {"model": f"iPod · {cap}" if cap else "iPod", "generation": "iPod",
            "detail": cap, "model_number": "", "source": "capacity"}
