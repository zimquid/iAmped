"""``python -m iamped.devices`` — print discovered devices as a table.

Handy for verifying the device layer on each OS. Add ``--no-raw`` to skip the
raw-disk scan (and its admin prompt) and list only mounted volumes.
"""
from __future__ import annotations

import sys

from . import list_devices


def _human(n: int) -> str:
    if not n:
        return "-"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def main(argv: list[str]) -> int:
    include_raw = "--no-raw" not in argv
    devs = list_devices(include_raw=include_raw)
    if not devs:
        print("No devices found.")
        return 0

    print(f"{'NAME':<22} {'KIND':<11} {'FS':<7} {'MNT':<4} "
          f"{'WRITE':<6} {'SIZE':<9} LOCATION / NOTE")
    print("-" * 92)
    for d in devs:
        loc = d.mountpoint or d.raw_path or ""
        note = f"  ⚠ {d.note}" if d.note else ""
        print(f"{d.name[:22]:<22} {d.kind:<11} {d.fs:<7} "
              f"{'yes' if d.mounted else 'no':<4} "
              f"{'yes' if d.writable else 'no':<6} "
              f"{_human(d.total):<9} {loc}{note}")
        if d.needs_conversion:
            print(f"{'':<22} → convert to FAT32 to manage this iPod here")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
