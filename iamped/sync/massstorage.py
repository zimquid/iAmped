"""Mass-storage backend for USB MP3 players (and iPods running Rockbox).

Copies files into a tidy Music/<Artist>/<Album>/ tree and writes .m3u8
playlists with relative paths — the lowest-common-denominator that virtually
every player understands.
"""
from __future__ import annotations

import os
import shutil

from .. import artwork
from .base import free_bytes, sanitize
from .device_state import (atomic_copy, atomic_write_text, managed_records,
                           read_manifest, record_is_valid, write_manifest)


def _prune_empty(directory: str, stop_at: str) -> None:
    """Walk up from *directory*, removing each dir until it's non-empty or
    we reach *stop_at* (the device root — never removed)."""
    stop = os.path.realpath(stop_at)
    while True:
        real = os.path.realpath(directory)
        if real == stop or not real.startswith(stop + os.sep):
            break
        try:
            os.rmdir(directory)
            directory = os.path.dirname(directory)
        except OSError:
            break


class MassStorageBackend:
    label = "USB mass-storage player"

    def __init__(self, device_path: str, layout: str = "nested"):
        # layout "nested": Music/<Artist>/<Album>/<Track>  (recursive players)
        # layout "flat":   <Artist> - <Album>/<Track>      (root + one level —
        #                  required by flat-scan players like the Creative MuVo,
        #                  which ignore anything in a sub-folder of a folder).
        self.root = device_path
        self.layout = layout
        self.music_dir = (device_path if layout == "flat"
                          else os.path.join(device_path, "Music"))
        self.playlist_dir = os.path.join(device_path, "Playlists")
        self._rel: dict[str, str] = {}     # rating_key -> path relative to root
        self._meta: dict[str, dict] = {}
        self._records: dict[str, dict] = {}
        self._playlist_paths: list[str] = []

    def free_bytes(self) -> int:
        return free_bytes(self.root)

    def prepare(self) -> None:
        os.makedirs(self.music_dir, exist_ok=True)

    def _migrate_record(self, record: dict) -> dict:
        """Move a file from its stored path to match the current layout.

        Called when resuming an existing sync after the user changes the device
        profile from nested to flat (or vice versa).  Moves the file, prunes
        now-empty directories, and returns an updated record.  No-ops if the
        file is already in the right layout or cannot be moved cleanly.
        """
        old_rel = record.get("path", "").replace("\\", "/")
        in_nested = old_rel.startswith("Music/")
        in_flat = not in_nested and old_rel.count("/") == 1

        if self.layout == "flat" and in_nested:
            # Music/<Artist>/<Album>/<Track> → <Artist> - <Album>/<Track>
            parts = old_rel.split("/")
            if len(parts) == 4:
                artist_dir, album_dir, fname = parts[1], parts[2], parts[3]
                folder = sanitize(f"{artist_dir} - {album_dir}", "Music")
                new_rel = os.path.join(folder, fname)
                old_abs = os.path.join(self.root, old_rel)
                new_abs = os.path.join(self.root, new_rel)
                if os.path.exists(old_abs) and not os.path.exists(new_abs):
                    os.makedirs(os.path.dirname(new_abs), exist_ok=True)
                    shutil.move(old_abs, new_abs)
                    _prune_empty(os.path.dirname(old_abs), self.root)
                    return {**record, "path": new_rel}

        elif self.layout == "nested" and in_flat:
            # <Artist> - <Album>/<Track> → Music/<Artist>/<Album>/<Track>
            # Use stored artist/album when available so the folder names are
            # consistent with what add_track would produce.
            artist = sanitize(record.get("artist", ""), "Unknown Artist")
            album = sanitize(record.get("album", ""), "Unknown Album")
            parts = old_rel.split("/")
            if len(parts) == 2 and artist and album:
                fname = parts[1]
                new_rel = os.path.join("Music", artist, album, fname)
                old_abs = os.path.join(self.root, old_rel)
                new_abs = os.path.join(self.root, new_rel)
                if os.path.exists(old_abs) and not os.path.exists(new_abs):
                    os.makedirs(os.path.dirname(new_abs), exist_ok=True)
                    shutil.move(old_abs, new_abs)
                    _prune_empty(os.path.dirname(old_abs), self.root)
                    return {**record, "path": new_rel}

        return record

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
                    "album": record.get("album"),
                    "duration_ms": record.get("duration_ms") or 0,
                }
                for key, record in prior.items()
            }
        carried = 0
        for key, track in keep_tracks.items():
            record = prior.get(str(key))
            if not record or not record_is_valid(self.root, record):
                continue
            record = self._migrate_record(record)
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
        if self.layout == "flat":
            # One folder deep: "<Artist> - <Album>/<Track>". Flat-scan players
            # (e.g. the Creative MuVo) only recognise the root and a single
            # folder level, so artist/album collapse into one folder name.
            folder = sanitize(f"{artist} - {album}", "Music")
            rel = os.path.join(folder, fname)
        else:
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
            "album": track.get("album"),
            "duration_ms": track.get("duration_ms") or 0,
            "source_signature": track.get("_sync_signature"),
            "new_file": True,
        }
        self._rel[key] = rel
        self._meta[key] = track
        self._records[key] = record
        return record

    def add_video(self, track: dict, src_path: str, ext: str,
                  mediatype: int = 0, video: dict | None = None,
                  art_path: str | None = None) -> dict:
        """Copy a movie/episode into a ``Video/`` tree: TV under
        ``Video/<Show>/Season N/``, movies under ``Video/Movies/``. Recorded with
        a ``media:"video"`` marker so manifest dedup/cleanup keeps audio and video
        independent."""
        v = video or {}
        title = sanitize(track.get("title", "Untitled"), "Untitled")
        show = v.get("show")
        if show:
            season = int(v.get("season") or 0)
            rel_dir = os.path.join("Video", sanitize(show, "Show"),
                                   f"Season {season:02d}")
        else:
            rel_dir = os.path.join("Video", "Movies")
        fname = title + ext
        dst = os.path.join(self.root, rel_dir, fname)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        base, e = os.path.splitext(dst)
        n = 1
        while os.path.exists(dst) and track["rating_key"] not in self._rel:
            dst = f"{base} ({n}){e}"
            n += 1
        size = atomic_copy(src_path, dst)
        key = str(track["rating_key"])
        rel = os.path.relpath(dst, self.root)
        record = {
            "rating_key": key,
            "path": rel,
            "size": size,
            "title": track.get("title"),
            "media": "video",
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

    def forget_locations(self, rels: set[str]) -> int:
        """Drop manifest records whose file path is in *rels* (the caller has
        already deleted the files). Leaves playlists and other tracks intact."""
        manifest = read_manifest(self.root, "massstorage") or {}
        want = {r.replace(":", os.sep) for r in rels}
        kept = [rec for rec in manifest.get("tracks", [])
                if (rec.get("path") or "").replace(":", os.sep) not in want]
        dropped = len(manifest.get("tracks", [])) - len(kept)
        if dropped:
            manifest["tracks"] = kept
            write_manifest(self.root, "massstorage", manifest)
        return dropped

    def records(self) -> list[dict]:
        return list(self._records.values())
