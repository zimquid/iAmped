"""Mass-storage backend for USB MP3 players (and iPods running Rockbox).

Copies files into a tidy Music/<Artist>/<Album>/ tree and writes .m3u8
playlists with relative paths — the lowest-common-denominator that virtually
every player understands.
"""
from __future__ import annotations

import os

from .. import artwork
from .base import free_bytes, sanitize
from .device_state import (atomic_copy, atomic_write_text, managed_records,
                           read_manifest, record_is_valid, write_manifest)


class MassStorageBackend:
    label = "USB mass-storage player"

    def __init__(self, device_path: str):
        self.root = device_path
        self.music_dir = os.path.join(device_path, "Music")
        self.playlist_dir = os.path.join(device_path, "Playlists")
        self._rel: dict[str, str] = {}     # rating_key -> path relative to root
        self._meta: dict[str, dict] = {}
        self._records: dict[str, dict] = {}
        self._playlist_paths: list[str] = []

    def free_bytes(self) -> int:
        return free_bytes(self.root)

    def prepare(self) -> None:
        os.makedirs(self.music_dir, exist_ok=True)

    def import_existing(self, keep_tracks: dict[str, dict] | None = None,
                        resume_records: dict[str, dict] | None = None) -> int:
        """Register valid files from the prior iAmped manifest.

        Files not in ``keep_tracks`` are deliberately not registered; mirror
        cleanup removes them only after the new playlists/manifest commit.
        Unmanaged files on the volume are never considered for deletion.
        """
        prior = managed_records(read_manifest(self.root, "massstorage"))
        if keep_tracks is None:
            keep_tracks = {
                key: {
                    "rating_key": key,
                    "title": record.get("title"),
                    "artist": record.get("artist"),
                    "duration_ms": record.get("duration_ms") or 0,
                }
                for key, record in prior.items()
            }
        carried = 0
        for key, track in keep_tracks.items():
            record = prior.get(str(key))
            if not record or not record_is_valid(self.root, record):
                continue
            self.restore_track(track, record)
            carried += 1
        return carried

    def restore_track(self, track: dict, record: dict) -> None:
        key = str(track["rating_key"])
        rel = record["path"]
        self._rel[key] = rel
        self._meta[key] = track
        restored = dict(record)
        if track.get("_sync_signature"):
            restored["source_signature"] = track["_sync_signature"]
        self._records[key] = restored

    def add_track(self, track: dict, src_path: str, ext: str,
                  art_path: str | None = None) -> dict:
        artist = sanitize(track.get("album_artist") or track.get("artist"),
                          "Unknown Artist")
        album = sanitize(track.get("album"), "Unknown Album")
        tn = track.get("track_number")
        prefix = f"{int(tn):02d} - " if tn else ""
        fname = sanitize(f"{prefix}{track.get('title', 'Untitled')}", "Untitled") + ext
        rel = os.path.join("Music", artist, album, fname)
        dst = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        # avoid clobbering distinct tracks that sanitize to the same name
        base, e = os.path.splitext(dst)
        n = 1
        while os.path.exists(dst) and track["rating_key"] not in self._rel:
            dst = f"{base} ({n}){e}"
            n += 1
        size = atomic_copy(src_path, dst)
        if art_path:
            artwork.write_cover_jpg(art_path, os.path.dirname(dst))
            artwork.embed(dst, art_path)
            size = os.path.getsize(dst)
        key = str(track["rating_key"])
        rel = os.path.relpath(dst, self.root)
        record = {
            "rating_key": key,
            "path": rel,
            "size": size,
            "title": track.get("title"),
            "artist": track.get("artist"),
            "duration_ms": track.get("duration_ms") or 0,
            "source_signature": track.get("_sync_signature"),
            "new_file": True,
        }
        self._rel[key] = rel
        self._meta[key] = track
        self._records[key] = record
        return record

    def add_playlist(self, name: str, rating_keys: list[str]) -> None:
        present = [rk for rk in rating_keys if rk in self._rel]
        if not present:
            return
        os.makedirs(self.playlist_dir, exist_ok=True)
        path = os.path.join(self.playlist_dir, sanitize(name, "Playlist") + ".m3u8")
        lines = ["#EXTM3U"]
        for rk in present:
            tr = self._meta[rk]
            secs = int((tr.get("duration_ms") or 0) / 1000)
            artist = tr.get("artist", "")
            title = tr.get("title", "")
            lines.append(f"#EXTINF:{secs},{artist} - {title}")
            # playlists live in /Playlists, media in /Music -> step up one level
            rel = os.path.relpath(os.path.join(self.root, self._rel[rk]),
                                  self.playlist_dir)
            lines.append(rel.replace(os.sep, "/"))
        atomic_write_text(path, "\n".join(lines) + "\n")
        self._playlist_paths.append(os.path.relpath(path, self.root))

    def finalize(self) -> None:
        prior = read_manifest(self.root, "massstorage") or {}
        current = set(self._playlist_paths)
        write_manifest(self.root, "massstorage", {
            "tracks": [
                {k: v for k, v in record.items() if k != "new_file"}
                for record in self._records.values()
            ],
            "playlists": self._playlist_paths,
        })
        for rel in prior.get("playlists", []):
            if rel in current:
                continue
            full = os.path.realpath(os.path.join(self.root, rel))
            root = os.path.realpath(self.root)
            if full.startswith(root + os.sep):
                try:
                    os.remove(full)
                except FileNotFoundError:
                    pass

    def records(self) -> list[dict]:
        return list(self._records.values())
