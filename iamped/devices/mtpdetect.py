"""Discover MTP players (Creative Zen and friends) via libmtp.

MTP devices do **not** mount as a disk, so the ordinary volume enumeration can
never see them — we have to ask libmtp directly. ``mtp-detect`` is the standard
probe, but it is slow (a second or more) and momentarily takes over the device,
so this must never run on the hot ``/api/volumes`` polling path. It is opt-in:
:func:`list_mtp` is only called when the user explicitly scans for MTP players.

Everything is best-effort and guarded — a missing ``libmtp`` or an unplugged
device yields an empty list, never an exception.
"""
from __future__ import annotations

import re
import shutil
import subprocess

from .capabilities import TRANSPORT_MTP
from .model import Device


def have_libmtp() -> bool:
    return shutil.which("mtp-detect") is not None


def _probe() -> str:
    try:
        # mtp-detect prints the raw-device summary first, then a slow full dump.
        # We only need the summary, so a short timeout is fine; on timeout we
        # still parse whatever was captured.
        proc = subprocess.run(
            ["mtp-detect"], capture_output=True, text=True, timeout=25)
        return (proc.stdout or "") + "\n" + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        return (exc.stdout or b"").decode("utf-8", "replace") if isinstance(
            exc.stdout, bytes) else (exc.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return ""


# Example summary line libmtp prints:
#   Creative Technology Ltd: ZEN (041e:4133) @ bus 0, dev 5
_RAW_RE = re.compile(
    r"^\s*(?P<vendor>.+?):\s*(?P<model>.+?)\s*"
    r"\((?P<vid>[0-9a-fA-F]{4}):(?P<pid>[0-9a-fA-F]{4})\)"
    r"(?:\s*@\s*bus\s*(?P<bus>\d+),\s*dev\s*(?P<dev>\d+))?",
    re.MULTILINE)


def list_mtp() -> list[Device]:
    """Connected MTP players as :class:`Device` entries (transport=mtp)."""
    if not have_libmtp():
        return []
    text = _probe()
    out: list[Device] = []
    seen: set[str] = set()
    for m in _RAW_RE.finditer(text):
        vid, pid = m.group("vid").lower(), m.group("pid").lower()
        key = f"{vid}:{pid}:{m.group('bus') or ''}:{m.group('dev') or ''}"
        if key in seen:
            continue
        seen.add(key)
        bus, dev = m.group("bus"), m.group("dev")
        busloc = f"{bus},{dev}" if bus is not None and dev is not None else f"{vid}:{pid}"
        model = m.group("model").strip()
        vendor = m.group("vendor").strip()
        dev_obj = Device(
            id=f"mtp:{busloc}",
            name=f"{vendor} {model}".strip(),
            fs="mtp",
            mountpoint=None,
            mounted=True,        # reachable for sync, just not as a filesystem
            model=model,
            transport=TRANSPORT_MTP,
            mtp_busloc=busloc,
        )
        dev_obj.writable = True
        dev_obj.note = "MTP device"
        out.append(dev_obj)
    return out
