"""MTP transport for Creative Zen-class players, via libmtp's CLI tools.

Unlike a USB mass-storage player, an MTP device is not a filesystem we can copy
into — we hand each track to the device *with its metadata* and the device folds
it into its own music database. ``mtp-sendtr`` is the right tool: it sends a
track plus title/artist/album/etc. so the song actually shows up in the player's
library (a plain ``mtp-sendfile`` would leave it unindexed).

Because the device has nowhere for us to keep a manifest, iAmped's record of
"what we already put on this player" lives **host-side**, keyed by the device's
bus address, so reruns stay additive instead of duplicating tracks.

NOTE: this path requires ``libmtp`` (``brew install libmtp`` / ``apt install
libmtp-dev mtp-tools``). It is exercised end-to-end only with a real MTP device
attached; everything here degrades to a clear error when libmtp or the device is
absent.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

from .. import config

# libmtp's CLI tools warn and may mangle non-ASCII metadata unless the locale is
# UTF-8; the host locale is often unset under a GUI launch, so force it.
_ENV = {**os.environ, "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}


class MTPError(RuntimeError):
    pass


def have_libmtp() -> bool:
    return shutil.which("mtp-sendtr") is not None and \
        shutil.which("mtp-detect") is not None


def _run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True,
                          env=_ENV, timeout=timeout)


def require() -> None:
    if not have_libmtp():
        raise MTPError(
            "MTP support needs libmtp. Install it with `brew install libmtp` "
            "(macOS) or `apt install mtp-tools` (Linux), then reconnect the "
            "player.")


def storage_free(timeout: int = 30) -> tuple[int, int]:
    """(total, free) bytes of the device's primary storage, via mtp-detect."""
    try:
        out = _run(["mtp-detect"], timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return 0, 0
    total = free = 0
    for key, val in re.findall(r"(MaxCapacity|FreeSpaceInBytes):\s*(\d+)", out):
        if key == "MaxCapacity":
            total = max(total, int(val))
        else:
            free = max(free, int(val))
    return total, free


# --------------------------------------------------------------------------- #
# Host-side record of what we've pushed to a given device.                     #
# --------------------------------------------------------------------------- #
def _state_path(busloc: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z]+", "_", busloc or "mtp")
    return os.path.join(str(config.APP_DIR), "mtp-state", f"{safe}.json")


def read_state(busloc: str) -> dict:
    try:
        with open(_state_path(busloc), encoding="utf-8") as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(busloc: str, state: dict) -> None:
    path = _state_path(busloc)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


class MTPBackend:
    """Sends tracks to an MTP device and tracks them host-side."""

    label = "MTP player"

    def __init__(self, busloc: str, folder: str = "Music"):
        self.busloc = busloc or "mtp"
        self.folder = folder
        self._state = read_state(self.busloc)
        self._records: dict[str, dict] = dict(self._state.get("tracks", {}))

    def prepare(self) -> None:
        require()

    def existing_keys(self) -> set[str]:
        """rating_keys we've already pushed to this device (additive dedup)."""
        return set(self._records)

    def add_track(self, track: dict, src_path: str, ext: str,
                  art_path: str | None = None) -> dict:
        key = str(track["rating_key"])
        remote = os.path.basename(src_path)
        args = ["mtp-sendtr", "-q",
                "-t", track.get("title") or "Untitled",
                "-a", track.get("artist") or "",
                "-A", track.get("album_artist") or track.get("artist") or "",
                "-w", track.get("album") or "",
                "-g", track.get("genre") or "",
                "-f", self.folder]
        if track.get("track_number"):
            args += ["-n", str(int(track["track_number"]))]
        if track.get("year"):
            args += ["-y", str(int(track["year"]))]
        if track.get("duration_ms"):
            args += ["-d", str(int(int(track["duration_ms"]) / 1000))]
        args += [src_path, remote]
        result = _run(args)
        if result.returncode != 0:
            raise MTPError(
                (result.stderr or result.stdout or "mtp-sendtr failed").strip())
        record = {
            "rating_key": key,
            "title": track.get("title"),
            "artist": track.get("artist"),
            "album": track.get("album"),
            "size": (os.path.getsize(src_path)
                     if os.path.exists(src_path) else 0),
            "source_signature": track.get("_sync_signature"),
        }
        self._records[key] = record
        return record

    def finalize(self) -> None:
        self._state["tracks"] = self._records
        write_state(self.busloc, self._state)

    def records(self) -> list[dict]:
        return list(self._records.values())
