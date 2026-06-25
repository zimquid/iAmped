"""Conservative matching of device files to Plex tracks without Plex IDs."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unicodedata
from difflib import SequenceMatcher

from .library import Library


def _norm(value: str | None) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(c for c in value if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", value)).strip()


def _metadata(path: str, fallback: dict) -> dict:
    out = dict(fallback)
    try:
        from mutagen import File
        audio = File(path, easy=True)
        if audio:
            for key in ("title", "artist", "album"):
                value = audio.get(key)
                if value:
                    out[key] = str(value[0])
            if getattr(audio, "info", None):
                out["duration_ms"] = int(audio.info.length * 1000)
    except Exception:
        pass
    return out


def _fpcalc(path: str) -> dict | None:
    exe = shutil.which("fpcalc")
    if not exe or not path or not os.path.isfile(path):
        return None
    try:
        result = subprocess.run(
            [exe, "-json", "-length", "120", path], capture_output=True,
            text=True, timeout=150, check=True)
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def match_track(lib: Library, device_track: dict) -> dict:
    """Return the best library match with a confidence score and method."""
    meta = _metadata(device_track.get("path") or "", device_track)
    artist, title = _norm(meta.get("artist")), _norm(meta.get("title"))
    album = _norm(meta.get("album"))
    if not title:
        return {"rating_key": None, "confidence": 0, "method": "none"}
    candidates = lib.search_candidates(meta.get("artist") or "",
                                       meta.get("title") or "", limit=20)
    best = None
    for row in candidates:
        title_score = SequenceMatcher(None, title, _norm(row.get("title"))).ratio()
        artist_score = SequenceMatcher(None, artist, _norm(row.get("artist"))).ratio() \
            if artist else .5
        album_score = SequenceMatcher(None, album, _norm(row.get("album"))).ratio() \
            if album else .5
        score = title_score * .58 + artist_score * .32 + album_score * .10
        duration = int(meta.get("duration_ms") or 0)
        row_duration = int(row.get("duration_ms") or 0)
        if duration and row_duration:
            delta = abs(duration - row_duration)
            score += .08 if delta <= 2500 else (-.15 if delta > 10000 else 0)
        if best is None or score > best[0]:
            best = (score, row)
    if not best or best[0] < .62:
        return {"rating_key": None, "confidence": round(best[0], 3) if best else 0,
                "method": "metadata"}
    method = "metadata"
    # Chromaprint is intentionally only used to strengthen an ambiguous match.
    # Plex candidates need a cached local file, so this never triggers downloads.
    if best[0] < .88 and best[1].get("cached_path"):
        left = _fpcalc(device_track.get("path") or "")
        right = _fpcalc(best[1]["cached_path"])
        if left and right and left.get("fingerprint") == right.get("fingerprint"):
            best = (1.0, best[1])
            method = "chromaprint"
    row = best[1]
    return {
        "rating_key": row["rating_key"], "confidence": round(min(best[0], 1), 3),
        "method": method, "title": row.get("title"), "artist": row.get("artist"),
        "album": row.get("album"),
    }
