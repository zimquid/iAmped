"""Parse an Audioscrobbler log (``.scrobbler.log``) from a USB player / Rockbox.

This is the de-facto standard for portable players that report listens: a
tab-separated text log at the device root. Each 'L' (listened) line is one play.
Spec: https://www.audioscrobbler.net/wiki/Portable_Player_Logging

Columns: artist, album, title, tracknumber, length(s), rating(L/S), unix-ts, mbid
"""
from __future__ import annotations

import os
from collections import defaultdict

CANDIDATES = [".scrobbler.log", "scrobbler.log",
              os.path.join(".rockbox", ".scrobbler.log")]


def find_log(device_path: str) -> str | None:
    for rel in CANDIDATES:
        p = os.path.join(device_path, rel)
        if os.path.isfile(p):
            return p
    return None


def parse(path: str) -> list[dict]:
    """Aggregate listens per (artist, album, title). Returns dicts with
    play_count and last_played_unix."""
    agg: dict[tuple, dict] = defaultdict(
        lambda: {"play_count": 0, "last_played_unix": 0})
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) < 7:
                continue
            artist, album, title = cols[0], cols[1], cols[2]
            rating = cols[5].strip().upper()
            if rating != "L":                 # only completed listens count
                continue
            try:
                ts = int(cols[6])
            except ValueError:
                ts = 0
            key = (artist, album, title)
            rec = agg[key]
            rec["artist"], rec["album"], rec["title"] = artist, album, title
            rec["play_count"] += 1
            rec["last_played_unix"] = max(rec["last_played_unix"], ts)
    return list(agg.values())
