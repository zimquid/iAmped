"""Read (and reset) the classic iPod ``iPod_Control/iTunes/Play Counts`` file.

The iPod appends playback feedback here between syncs: per track, the number of
plays since the last sync, the last-played time, the current star rating and a
skip count. Entries are positional — the Nth entry corresponds to the Nth track
in the iTunesDB track list. iTunes folds these into the database and deletes the
file on sync; we do the same (see itunesdb.patch_playcounts + reset_play_counts).

Format (little-endian): header "mhdp", then ``count`` fixed-size entries.
"""
from __future__ import annotations

import os
import struct

MAC_EPOCH_OFFSET = 2082844800


def mac_to_unix(mac: int) -> int:
    return (mac - MAC_EPOCH_OFFSET) if mac else 0


def parse(path: str) -> list[dict]:
    """Return one dict per track (in iTunesDB order)."""
    with open(path, "rb") as fh:
        data = fh.read()
    if data[0:4] != b"mhdp":
        raise ValueError("not a Play Counts file (missing mhdp header)")
    header_len = struct.unpack_from("<I", data, 4)[0]
    entry_len = struct.unpack_from("<I", data, 8)[0]
    count = struct.unpack_from("<I", data, 0x0C)[0]
    out: list[dict] = []
    off = header_len
    for i in range(count):
        e = data[off:off + entry_len]
        if len(e) < entry_len:
            break
        play_count = struct.unpack_from("<I", e, 0)[0]
        last = struct.unpack_from("<I", e, 4)[0] if entry_len >= 8 else 0
        rating = struct.unpack_from("<I", e, 0x0C)[0] if entry_len >= 0x10 else 0
        skips = struct.unpack_from("<I", e, 0x14)[0] if entry_len >= 0x18 else 0
        out.append({
            "index": i, "play_count": play_count,
            "last_played": last, "last_played_unix": mac_to_unix(last),
            "rating": rating, "skip_count": skips,
        })
        off += entry_len
    return out


def reset_play_counts(device_path: str) -> bool:
    """Delete the Play Counts file so the same plays aren't imported twice.
    The iPod recreates it as it plays. Returns True if a file was removed."""
    path = os.path.join(device_path, "iPod_Control", "iTunes", "Play Counts")
    if os.path.exists(path):
        os.remove(path)
        return True
    return False
