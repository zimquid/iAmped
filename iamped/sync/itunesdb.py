"""Write a classic iPod ``iTunesDB`` from scratch (pure Python).

This targets the *non-hashed* iTunesDB format used by the click-wheel iPods:
iPod 1G–5.5G (incl. iPod Video), iPod mini, and iPod nano 1G–3G. Those models
do not verify the database, so a correctly-structured hand-written DB mounts and
plays.

NOT supported: iPod Classic 6G/7G and nano 6G+. Those require Apple's
proprietary ``hashAB`` checksum (never fully reverse-engineered). For them, use
the mass-storage backend together with Rockbox firmware.

Layout written to the device::

    iPod_Control/Music/F00../F49/<name>.<ext>     (audio, spread over folders)
    iPod_Control/iTunes/iTunesDB                  (the binary database)

Format reference: the iPodLinux "ITunesDB" specification. All integers are
little-endian; text fields are UTF-16LE inside "mhod" string objects.

This writer is well-tested for *structure* (see ``self_test``) but, because it
cannot be validated against physical hardware here, treat first use on a real
iPod as experimental and keep a backup of the device's existing iTunesDB.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import struct
import time

from ..devices import usbdetect
from .base import free_bytes
from .device_state import (atomic_copy, atomic_write_bytes, read_manifest as
                           read_device_manifest, write_manifest)
from . import hash58

MANIFEST_REL = os.path.join("iPod_Control", ".iamped", "manifest.json")
PLAYCOUNTS_REL = os.path.join("iPod_Control", "iTunes", "Play Counts")
ITUNESDB_REL = os.path.join("iPod_Control", "iTunes", "iTunesDB")

MAC_EPOCH_OFFSET = 2082844800  # seconds between 1904-01-01 and 1970-01-01
NUM_MUSIC_FOLDERS = 50         # F00 .. F49

# ---- string object types (mhod) ------------------------------------------
MHOD_TITLE = 1
MHOD_LOCATION = 2
MHOD_ALBUM = 3
MHOD_ARTIST = 4
MHOD_GENRE = 5
MHOD_FILETYPE = 6
MHOD_DESCRIPTION = 14   # long description / summary (podcast + video)
MHOD_SUBTITLE = 18      # episode subtitle / title
MHOD_TVSHOW = 19        # TV show name (groups episodes in the Videos menu)
MHOD_TVEPISODE = 20     # episode id string, e.g. "S01E03"
MHOD_TVNETWORK = 21     # TV network
MHOD_PLAYLIST_POS = 100

# mhit "mediatype" field: how the iPod files a track into its Music / Videos /
# Music Videos / TV Shows menus. Offsets and values per libgpod itdb_itunesdb.c
# (mediatype at mhit+0xD0, season_nr at +0xD4, episode_nr at +0xD8):
#
#   put32lint(cts, track->mediatype);   /* +0xD0 */
#   put32lint(cts, track->season_nr);   /* +0xD4 */
#   put32lint(cts, track->episode_nr);  /* +0xD8 */
#
# Audio MUST be flagged ITDB_MEDIATYPE_AUDIO (1): a 0 here means "unknown", and
# the 5G/5.5G/classic firmware then files the track into *every* menu — which is
# why audio tracks were leaking into the Videos and Music Videos lists.
MEDIATYPE_AUDIO = 0x01
MEDIATYPE_MOVIE = 0x02
MEDIATYPE_MUSICVIDEO = 0x20
MEDIATYPE_TVSHOW = 0x40
# Entries whose mediatype the iPod files under the Videos menus (and for which we
# emit the video string mhods / season / episode fields).
_VIDEO_TYPES = frozenset({MEDIATYPE_MOVIE, MEDIATYPE_MUSICVIDEO, MEDIATYPE_TVSHOW})


def _mac_time(epoch_secs: int | float | None) -> int:
    if not epoch_secs:
        return 0
    return int(epoch_secs) + MAC_EPOCH_OFFSET


def _string_mhod(mhod_type: int, text: str) -> bytes:
    data = (text or "").encode("utf-16-le")
    header = bytearray(24)
    header[0:4] = b"mhod"
    struct.pack_into("<I", header, 4, 24)              # header length
    struct.pack_into("<I", header, 8, 24 + 16 + len(data))  # total length
    struct.pack_into("<I", header, 12, mhod_type)
    body = struct.pack("<IIII", 1, len(data), 0, 0) + data  # pos, len, unk, unk
    return bytes(header) + body


def _position_mhod(position: int) -> bytes:
    b = bytearray(44)
    b[0:4] = b"mhod"
    struct.pack_into("<I", b, 4, 24)        # header length
    struct.pack_into("<I", b, 8, 44)        # total length
    struct.pack_into("<I", b, 12, MHOD_PLAYLIST_POS)
    struct.pack_into("<I", b, 24, position)
    return bytes(b)


def _filetype_label(ext: str) -> str:
    e = ext.lower().lstrip(".")
    return {
        "mp3": "MPEG audio file",
        "m4a": "AAC audio file",
        "aac": "AAC audio file",
        "aif": "AIFF audio file",
        "aiff": "AIFF audio file",
        "wav": "WAV audio file",
        "m4v": "MPEG-4 video file",
        "mp4": "MPEG-4 video file",
        "mov": "QuickTime movie file",
    }.get(e, "Audio file")


class _Entry:
    """One track staged for the database.

    ``origin`` records provenance: ``"new"`` (iAmped just copied it from Plex),
    ``"iamped"`` (already on the device and previously written by iAmped), or
    ``"existing"`` (already on the device from another source — iTunes, etc.).
    Only ``new``/``iamped`` entries are claimed in the manifest.
    """
    __slots__ = ("track_id", "location", "ext", "size", "track", "mac_added",
                 "origin", "art_path", "mediatype", "video")

    def __init__(self, track_id, location, ext, size, track, mac_added,
                 origin="new", art_path=None, mediatype=0, video=None):
        self.track_id = track_id
        self.location = location
        self.ext = ext
        self.size = size
        self.track = track
        self.mac_added = mac_added
        self.origin = origin
        self.art_path = art_path
        self.mediatype = mediatype          # 0 = audio; MEDIATYPE_* for video
        self.video = video or {}            # show/season/episode/network/summary


def _u32(b: bytes, off: int) -> int:
    return struct.unpack_from("<I", b, off)[0]


def read_tracks(db: bytes) -> list[dict]:
    """Walk an iTunesDB and return tracks in list order with the byte offsets
    we need to patch play counts / ratings in place."""
    assert db[0:4] == b"mhbd", "not an iTunesDB"
    n_datasets = _u32(db, 0x14)
    off = _u32(db, 0x04)
    out: list[dict] = []
    for _ in range(n_datasets):
        if db[off:off + 4] != b"mhsd":
            break
        ds_total = _u32(db, off + 8)
        ds_type = _u32(db, off + 12)
        inner = off + _u32(db, off + 4)
        if ds_type == 1 and db[inner:inner + 4] == b"mhlt":
            count = _u32(db, inner + 8)
            p = inner + _u32(db, inner + 4)
            for _ in range(count):
                if db[p:p + 4] != b"mhit":
                    break
                total = _u32(db, p + 8)
                out.append({
                    "track_id": _u32(db, p + 0x10),
                    "offset": p,
                    "play_count_off": p + 0x50,
                    "rating_off": p + 0x1F,
                    "last_played_off": p + 0x58,
                })
                p += total
        off += ds_total
    return out


# String mhod data layout (shared by Apple's writer and ours): the UTF-16LE
# payload's byte-length lives at +0x1C and the text itself begins at +0x28.
_MHOD_STRLEN_OFF = 0x1C
_MHOD_DATA_OFF = 0x28
_MHOD_STRINGS = {
    MHOD_TITLE: "title", MHOD_LOCATION: "location", MHOD_ALBUM: "album",
    MHOD_ARTIST: "artist", MHOD_GENRE: "genre", MHOD_FILETYPE: "filetype",
    MHOD_DESCRIPTION: "description", MHOD_SUBTITLE: "subtitle",
    MHOD_TVSHOW: "tv_show", MHOD_TVEPISODE: "tv_episode_id",
    MHOD_TVNETWORK: "tv_network",
}


def _read_mhod_strings(db: bytes, mhit_off: int) -> dict[str, str]:
    """Decode the string mhods (title/artist/album/genre/location) hanging off
    one ``mhit``. Tolerant of mhod types and header sizes iAmped never writes
    (Apple emits sort-key and extended types), which are simply skipped."""
    hdr = _u32(db, mhit_off + 4)
    num = _u32(db, mhit_off + 0x0C)
    q = mhit_off + hdr
    out: dict[str, str] = {}
    for _ in range(num):
        if db[q:q + 4] != b"mhod":
            break
        tlen = _u32(db, q + 8)
        typ = _u32(db, q + 12)
        key = _MHOD_STRINGS.get(typ)
        if key:
            strlen = _u32(db, q + _MHOD_STRLEN_OFF)
            if 0 < strlen <= tlen:
                try:
                    out[key] = db[q + _MHOD_DATA_OFF:
                                  q + _MHOD_DATA_OFF + strlen].decode("utf-16-le")
                except (UnicodeDecodeError, ValueError):
                    pass
        q += tlen
    return out


def read_tracks_full(db: bytes) -> list[dict]:
    """Like :func:`read_tracks` but also decodes per-track metadata (title,
    artist, album, genre, on-device location) and the fixed numeric fields
    (size, duration, play count, rating, year, bitrate, last-played). Read-only;
    used to inventory whatever library is already on a device."""
    out: list[dict] = []
    for t in read_tracks(db):
        p = t["offset"]
        meta = _read_mhod_strings(db, p)
        rating100 = db[p + 0x1F]
        header_len = _u32(db, p + 4)
        out.append({
            "track_id": t["track_id"],
            "offset": p,
            "title": meta.get("title"),
            "artist": meta.get("artist"),
            "album": meta.get("album"),
            "genre": meta.get("genre"),
            "mediatype": _u32(db, p + 0xD0) if header_len >= 0xD4 else 0,
            "season_number": _u32(db, p + 0xD4) if header_len >= 0xD8 else 0,
            "episode_number": _u32(db, p + 0xD8) if header_len >= 0xDC else 0,
            "tv_show": meta.get("tv_show"),
            "tv_episode_id": meta.get("tv_episode_id"),
            "tv_network": meta.get("tv_network"),
            "description": meta.get("description"),
            "subtitle": meta.get("subtitle"),
            "location": meta.get("location"),     # e.g. ":iPod_Control:Music:F26:WJUW.m4a"
            "size": _u32(db, p + 0x24),
            "duration_ms": _u32(db, p + 0x28),
            "track_number": _u32(db, p + 0x2C),
            "year": _u32(db, p + 0x34),
            "bitrate": _u32(db, p + 0x38),
            "play_count": _u32(db, p + 0x50),
            "rating100": rating100,               # 0..100 (20 per star)
            "stars": round(rating100 / 20, 1) if rating100 else 0,
            "last_played_unix": (lp - MAC_EPOCH_OFFSET
                                 if (lp := _u32(db, p + 0x58)) else 0),
            "dbid": struct.unpack_from("<Q", db, p + 0x70)[0]
                    if header_len >= 0x78 else t["track_id"],
            "artwork_count": struct.unpack_from("<H", db, p + 0x7C)[0]
                    if header_len >= 0x7E else 0,
            "artwork_size": _u32(db, p + 0x80) if header_len >= 0x84 else 0,
            "mhii_link": _u32(db, p + 0x160) if header_len >= 0x164 else 0,
            "raw_header": db[p:p + header_len],
        })
    return out


def read_playlists(db: bytes, dataset_type: int = 2) -> list[dict]:
    """Read playlist names and referenced track IDs."""
    if db[:4] != b"mhbd":
        return []
    off = _u32(db, 4)
    for _ in range(_u32(db, 0x14)):
        if db[off:off + 4] != b"mhsd":
            break
        total = _u32(db, off + 8)
        if _u32(db, off + 12) == dataset_type:
            inner = off + _u32(db, off + 4)
            if db[inner:inner + 4] != b"mhlp":
                return []
            count = _u32(db, inner + 8)
            p = inner + _u32(db, inner + 4)
            playlists = []
            for _ in range(count):
                if db[p:p + 4] != b"mhyp":
                    break
                plen = _u32(db, p + 8)
                end = p + plen
                q = p + _u32(db, p + 4)
                title, track_ids = "", []
                while q + 12 <= end:
                    magic = db[q:q + 4]
                    length = _u32(db, q + 8)
                    if length < 12 or q + length > end:
                        break
                    if magic == b"mhod" and _u32(db, q + 12) == MHOD_TITLE:
                        strlen = _u32(db, q + _MHOD_STRLEN_OFF)
                        try:
                            title = db[q + _MHOD_DATA_OFF:
                                       q + _MHOD_DATA_OFF + strlen].decode("utf-16-le")
                        except (UnicodeDecodeError, ValueError):
                            pass
                    elif magic == b"mhip":
                        track_ids.append(_u32(db, q + 0x18))
                    q += length
                playlists.append({
                    "title": title, "track_ids": track_ids,
                    "master": bool(db[p + 0x14]),
                })
                p = end
            return playlists
        off += total
    return []


def location_to_relpath(location: str) -> str:
    """Convert an iTunesDB ``:iPod_Control:Music:F00:NAME.mp3`` location into a
    device-relative path (``iPod_Control/Music/F00/NAME.mp3``)."""
    return location.lstrip(":").replace(":", os.sep)


def patch_playcounts(db: bytes, by_track_id: dict[int, dict]) -> bytes:
    """Fold imported stats back into the iTunesDB (cumulative, like iTunes).
    by_track_id: {track_id: {add_plays, last_played(mac), rating(0-100)}}."""
    b = bytearray(db)
    for t in read_tracks(db):
        u = by_track_id.get(t["track_id"])
        if not u:
            continue
        if u.get("add_plays"):
            new = _u32(db, t["play_count_off"]) + int(u["add_plays"])
            struct.pack_into("<I", b, t["play_count_off"], new)
            struct.pack_into("<I", b, t["play_count_off"] + 4, new)  # play count 2
        if u.get("last_played"):
            struct.pack_into("<I", b, t["last_played_off"], int(u["last_played"]))
        if u.get("rating") is not None:
            b[t["rating_off"]] = max(0, min(100, int(u["rating"])))
    return bytes(b)


def resign_for_device(device_path: str, db: bytes) -> bytes:
    """Re-sign a patched hash58 database after play-count/rating updates."""
    if len(db) >= 0x6C and _u32(db, 4) >= 0xF4 and \
            struct.unpack_from("<H", db, 0x30)[0] == 1:
        guid = _guid_for_existing_hash58(device_path, db)
        return hash58.sign(db, guid)
    return db


def _guid_for_existing_hash58(device_path: str, db: bytes) -> str:
    """Resolve a mounted device's GUID, using signature verification to
    disambiguate when several iPods are connected simultaneously."""
    try:
        return hash58.guid_from_device(device_path)
    except RuntimeError:
        pass
    candidates = []
    for item in usbdetect.ipod_models():
        serial = item.get("serial") or ""
        try:
            guid = hash58.normalize_guid(serial)
        except ValueError:
            continue
        candidates.append(guid)
        if hash58.verify(db, guid):
            return guid
    if len(candidates) == 1:
        return candidates[0]
    raise RuntimeError(
        "Could not match this hash58 iPod to its connected USB serial.")


def read_manifest(device_path: str) -> dict | None:
    return read_device_manifest(device_path, "ipod")


class ITunesDBBackend:
    label = "Classic iPod (iTunesDB)"

    def __init__(self, device_path: str, device_name: str = "iPod"):
        self.root = device_path
        self.device_name = device_name
        self.ctrl = os.path.join(device_path, "iPod_Control")
        self.music = os.path.join(self.ctrl, "Music")
        self.itunes = os.path.join(self.ctrl, "iTunes")
        self._entries: list[_Entry] = []
        self._by_key: dict[str, _Entry] = {}
        self._playlists: list[tuple[str, list[str]]] = []
        self._preserved_playlists: list[tuple[str, list[int]]] = []
        self._album_ids: dict[tuple[str, str], int] = {}
        self._next_id = 1
        self._existing_db: bytes | None = None
        db_path = os.path.join(self.itunes, "iTunesDB")
        try:
            with open(db_path, "rb") as fh:
                self._existing_db = fh.read()
        except OSError:
            pass
        usb = usbdetect.match(mountpoint=device_path) or {}
        self._generation = usb.get("generation") or ""
        self._hash58 = bool(
            self._existing_db and len(self._existing_db) >= 0x6C
            and _u32(self._existing_db, 4) >= 0xF4
            and struct.unpack_from("<H", self._existing_db, 0x30)[0] == 1
        )
        self._firewire_guid = (
            _guid_for_existing_hash58(device_path, self._existing_db)
            if self._hash58 and self._existing_db else None
        )

    def free_bytes(self) -> int:
        return free_bytes(self.root)

    def prepare(self) -> None:
        for i in range(NUM_MUSIC_FOLDERS):
            os.makedirs(os.path.join(self.music, f"F{i:02d}"), exist_ok=True)
        os.makedirs(self.itunes, exist_ok=True)

    def import_existing(self, keep_tracks: dict[str, dict] | None = None,
                        resume_records: dict[str, dict] | None = None) -> int:
        """Stage the tracks already on the device so :meth:`finalize` *preserves*
        them — this is what makes the sync additive instead of wipe-and-dump.

        Reads the current ``iTunesDB`` and re-stages every track whose audio file
        still exists, keeping its on-device location, track ID and metadata. No
        audio is copied (the files are already in place). Tracks a prior iAmped
        manifest recorded keep ``origin="iamped"``; everything else is treated as
        foreign (``origin="existing"``) and is preserved but never claimed in the
        manifest. Must be called after :meth:`prepare` and before
        :meth:`add_track`/:meth:`finalize`. Returns the count carried over.
        """
        db_path = os.path.join(self.itunes, "iTunesDB")
        if not os.path.exists(db_path):
            return 0
        with open(db_path, "rb") as fh:
            existing_db = fh.read()
            rows = read_tracks_full(existing_db)
        self._preserved_playlists = [
            (p["title"], p["track_ids"])
            for p in read_playlists(existing_db)
            if not p["master"] and p["title"]
        ]
        prior = read_manifest(self.root) or {}
        prior_by_loc = {(t.get("location") or "").lstrip(":"): t
                        for t in prior.get("tracks", [])}
        resume_locs = {
            (r.get("location") or ":" + (r.get("path") or "").replace(os.sep, ":")).lstrip(":")
            for r in (resume_records or {}).values()
            if r.get("location") or r.get("path")
        }

        carried = 0
        for r in rows:
            loc = r.get("location")
            if not loc:
                continue
            full = os.path.join(self.root, location_to_relpath(loc))
            if not os.path.exists(full):
                continue                       # drop dangling DB entries
            prior_hit = prior_by_loc.get(loc.lstrip(":"))
            # If metadata committed but the manifest did not, an interrupted
            # transaction's new tracks are visible in iTunesDB but look foreign
            # under the old manifest. They will be re-staged from the journal.
            if not prior_hit and loc.lstrip(":") in resume_locs:
                continue
            if prior_hit and keep_tracks is not None and \
                    str(prior_hit.get("rating_key")) not in keep_tracks:
                continue
            desired = keep_tracks.get(str(prior_hit.get("rating_key"))) \
                if prior_hit and keep_tracks is not None else None
            track = {
                "title": r.get("title"), "artist": r.get("artist"),
                "album": r.get("album"), "genre": r.get("genre"),
                "view_count": r.get("play_count") or 0,
                "user_rating": (r.get("rating100") or 0) / 10.0,  # 0..100 -> 0..10
                "duration_ms": r.get("duration_ms") or 0,
                "track_number": r.get("track_number") or 0,
                "year": r.get("year") or 0,
                "bitrate": r.get("bitrate") or 0,
                "last_viewed_at": r.get("last_played_unix") or 0,
                "rating_key": (prior_hit or {}).get("rating_key"),
                "dbid": r.get("dbid") or r["track_id"],
                "_artwork_id": r.get("mhii_link") or 0,
                "_artwork_size": r.get("artwork_size") or 0,
                "_mhit_header": r.get("raw_header"),
            }
            if desired:
                # Plex remains authoritative for descriptive metadata while
                # device play/rating fields above remain authoritative until
                # the explicit readback flow imports them.
                track.update({
                    "title": desired.get("title"), "artist": desired.get("artist"),
                    "album": desired.get("album"), "genre": desired.get("genre"),
                    "duration_ms": desired.get("duration_ms") or track["duration_ms"],
                    "track_number": desired.get("track_number") or track["track_number"],
                    "year": desired.get("year") or track["year"],
                    "bitrate": desired.get("bitrate") or track["bitrate"],
                    "rating_key": desired.get("rating_key"),
                    "_sync_signature": desired.get("_sync_signature"),
                })
            # Trust the manifest's video typing over the DB read: it survives an
            # older/broken on-device layout and lets a re-sync rewrite the entry
            # with correct offsets instead of silently demoting it to audio.
            mediatype = (prior_hit or {}).get("mediatype") \
                or (r.get("mediatype") if r.get("mediatype") in _VIDEO_TYPES else 0)
            # Old manifests recorded media:"video" without a numeric type; recover
            # those as a movie so the entry self-heals to the right offsets on the
            # next finalize instead of being demoted to audio.
            if not mediatype and (prior_hit or {}).get("media") == "video":
                mediatype = MEDIATYPE_MOVIE
            if mediatype in _VIDEO_TYPES:
                video = {
                    "show": r.get("tv_show"), "subtitle": r.get("subtitle"),
                    "episode_id": r.get("tv_episode_id"),
                    "network": r.get("tv_network"), "summary": r.get("description"),
                    "season": (prior_hit or {}).get("season_number")
                              or r.get("season_number") or 0,
                    "episode": (prior_hit or {}).get("episode_number")
                               or r.get("episode_number") or 0,
                }
            else:
                video = {}
            e = _Entry(r["track_id"], loc, os.path.splitext(full)[1].lower(),
                       os.path.getsize(full), track, _mac_time(time.time()),
                       origin="iamped" if prior_hit else "existing",
                       mediatype=mediatype, video=video)
            self._entries.append(e)
            if track["rating_key"]:
                self._by_key[track["rating_key"]] = e
            self._next_id = max(self._next_id, r["track_id"] + 1)
            carried += 1
        return carried

    def restore_track(self, track: dict, record: dict) -> None:
        """Re-stage audio completed by an interrupted transaction."""
        rel = record["path"]
        full = os.path.join(self.root, rel)
        location = record.get("location") or ":" + rel.replace(os.sep, ":")
        fid = int(record["track_id"])
        entry = _Entry(fid, location, record.get("ext") or os.path.splitext(full)[1],
                       os.path.getsize(full), track,
                       int(record.get("mac_added") or _mac_time(time.time())),
                       origin="new")
        self._entries.append(entry)
        self._by_key[str(track["rating_key"])] = entry
        self._next_id = max(self._next_id, fid + 1)

    def add_track(self, track: dict, src_path: str, ext: str,
                  art_path: str | None = None) -> dict:
        # Name the on-device file by the (monotonic) track id, which already
        # starts above every carried-over id — so a new file can never overwrite
        # a track preserved by import_existing().
        fid = self._next_id
        folder = fid % NUM_MUSIC_FOLDERS
        name = f"{fid:05d}{ext.lower()}"
        rel = os.path.join("Music", f"F{folder:02d}", name)
        dst = os.path.join(self.ctrl, rel)
        size = atomic_copy(src_path, dst)
        location = ":iPod_Control:" + rel.replace(os.sep, ":")
        entry = _Entry(fid, location, ext.lower(), size, track,
                       _mac_time(time.time()), origin="new", art_path=art_path)
        self._next_id += 1
        self._entries.append(entry)
        self._by_key[str(track["rating_key"])] = entry
        return {
            "rating_key": str(track["rating_key"]),
            "track_id": fid,
            "path": os.path.relpath(dst, self.root),
            "location": location,
            "ext": ext.lower(),
            "size": size,
            "mac_added": entry.mac_added,
            "title": track.get("title"),
            "artist": track.get("artist"),
            "source_signature": track.get("_sync_signature"),
            "new_file": True,
        }

    def add_video(self, track: dict, src_path: str, ext: str,
                  mediatype: int, video: dict | None = None,
                  art_path: str | None = None) -> dict:
        """Stage a movie or TV episode. Same file placement and monotonic-id
        naming as :meth:`add_track`, but the entry is flagged as video so
        :meth:`_build_mhit` writes the mediatype + TV show/season/episode fields
        and the iPod files it under Movies / TV Shows.

        EXPERIMENTAL: the iTunesDB *video* record layout cannot be validated
        against hardware here. Structure is round-trip tested; treat first use on
        a physical iPod as experimental (a backup iTunesDB is written on every
        finalize)."""
        fid = self._next_id
        folder = fid % NUM_MUSIC_FOLDERS
        name = f"{fid:05d}{ext.lower()}"
        rel = os.path.join("Music", f"F{folder:02d}", name)
        dst = os.path.join(self.ctrl, rel)
        size = atomic_copy(src_path, dst)
        location = ":iPod_Control:" + rel.replace(os.sep, ":")
        entry = _Entry(fid, location, ext.lower(), size, track,
                       _mac_time(time.time()), origin="new", art_path=art_path,
                       mediatype=mediatype, video=video or {})
        self._next_id += 1
        self._entries.append(entry)
        self._by_key[str(track["rating_key"])] = entry
        return {
            "rating_key": str(track["rating_key"]),
            "track_id": fid,
            "path": os.path.relpath(dst, self.root),
            "location": location,
            "ext": ext.lower(),
            "size": size,
            "mac_added": entry.mac_added,
            "title": track.get("title"),
            "media": "video",
            "mediatype": mediatype,
            "source_signature": track.get("_sync_signature"),
            "new_file": True,
        }

    def add_playlist(self, name: str, rating_keys: list[str]) -> None:
        present = [rk for rk in rating_keys if rk in self._by_key]
        if present:
            self._playlists.append((name, present))

    # ---- database assembly ------------------------------------------------
    @staticmethod
    def _stable_sql_id(kind: str, value: str) -> int:
        digest = hashlib.sha1(f"{kind}\0{value}".encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "little")

    def _prepare_index_ids(self) -> None:
        self._album_ids.clear()
        for e in self._entries:
            artist = (e.track.get("artist") or "Unknown Artist").strip()
            album_artist = (e.track.get("album_artist") or artist).strip()
            album = (e.track.get("album") or "Unknown Album").strip()
            self._album_ids.setdefault(
                (album, album_artist), len(self._album_ids) + 1)

    def _build_album_dataset(self) -> bytes:
        records = []
        for (album, artist), album_id in self._album_ids.items():
            mhods = [
                _string_mhod(200, album),
                _string_mhod(201, artist),
                _string_mhod(202, artist),
            ]
            header = bytearray(88)
            header[0:4] = b"mhia"
            struct.pack_into("<I", header, 4, 88)
            struct.pack_into("<I", header, 8, 88 + sum(map(len, mhods)))
            struct.pack_into("<I", header, 12, len(mhods))
            struct.pack_into("<I", header, 16, album_id)
            struct.pack_into("<Q", header, 20,
                             self._stable_sql_id("album", f"{album}\0{artist}"))
            struct.pack_into("<I", header, 28, 2)
            records.append(bytes(header) + b"".join(mhods))
        mhla = bytearray(92)
        mhla[0:4] = b"mhla"
        struct.pack_into("<I", mhla, 4, 92)
        struct.pack_into("<I", mhla, 8, len(records))
        return self._wrap_mhsd(4, bytes(mhla) + b"".join(records))

    def _build_mhit(self, e: _Entry) -> bytes:
        t = e.track
        mhods = [
            _string_mhod(MHOD_TITLE, t.get("title", "")),
            _string_mhod(MHOD_LOCATION, e.location),
            _string_mhod(MHOD_ALBUM, t.get("album", "")),
            _string_mhod(MHOD_ARTIST, t.get("artist", "")),
        ]
        if t.get("genre"):
            mhods.append(_string_mhod(MHOD_GENRE, t["genre"]))
        is_video = e.mediatype in _VIDEO_TYPES
        if is_video:
            v = e.video
            if v.get("summary"):
                mhods.append(_string_mhod(MHOD_DESCRIPTION, v["summary"]))
            if e.mediatype == MEDIATYPE_TVSHOW:
                if v.get("show"):
                    mhods.append(_string_mhod(MHOD_TVSHOW, v["show"]))
                if v.get("subtitle"):
                    mhods.append(_string_mhod(MHOD_SUBTITLE, v["subtitle"]))
                if v.get("episode_id"):
                    mhods.append(_string_mhod(MHOD_TVEPISODE, v["episode_id"]))
                if v.get("network"):
                    mhods.append(_string_mhod(MHOD_TVNETWORK, v["network"]))
        mhods.append(_string_mhod(MHOD_FILETYPE, _filetype_label(e.ext)))
        body = b"".join(mhods)

        HDR = 0x248 if self._hash58 else 0xF4
        old_header = t.get("_mhit_header")
        if self._hash58 and old_header and len(old_header) >= HDR:
            b = bytearray(old_header[:HDR])
        else:
            b = bytearray(HDR)
        b[0:4] = b"mhit"
        struct.pack_into("<I", b, 0x04, HDR)
        struct.pack_into("<I", b, 0x08, HDR + len(body))     # total length
        struct.pack_into("<I", b, 0x0C, len(mhods))          # number of mhods
        struct.pack_into("<I", b, 0x10, e.track_id)          # unique id
        struct.pack_into("<I", b, 0x14, 1)                   # visible
        struct.pack_into("<I", b, 0x18, 0)                   # filetype (legacy)
        # 0x1C: type1, type2, compilation, rating (one byte each)
        rating = max(0, min(100, int(round((t.get("user_rating") or 0) * 10))))
        b[0x1F] = rating
        struct.pack_into("<I", b, 0x20, e.mac_added)         # last modified
        struct.pack_into("<I", b, 0x24, e.size & 0xFFFFFFFF)  # file size
        struct.pack_into("<I", b, 0x28, int(t.get("duration_ms") or 0))  # length ms
        struct.pack_into("<I", b, 0x2C, int(t.get("track_number") or 0))
        struct.pack_into("<I", b, 0x30, 0)                   # total tracks
        struct.pack_into("<I", b, 0x34, int(t.get("year") or 0))
        struct.pack_into("<I", b, 0x38, int((t.get("bitrate") or 0)))
        struct.pack_into("<I", b, 0x3C, 44100 << 16)         # sample rate
        struct.pack_into("<I", b, 0x50, int(t.get("view_count") or 0))  # play count
        struct.pack_into("<I", b, 0x54, int(t.get("view_count") or 0))
        struct.pack_into("<I", b, 0x58, _mac_time(t.get("last_viewed_at")))
        struct.pack_into("<I", b, 0x5C, int(t.get("disc_number") or 0))
        struct.pack_into("<I", b, 0x68, e.mac_added)         # date added
        if self._hash58:
            dbid = int(t.get("dbid") or e.track_id)
            struct.pack_into("<Q", b, 0x70, dbid)
            b[0x78] = 1
            struct.pack_into("<Q", b, 0xA8, dbid)
            struct.pack_into("<I", b, 0xD0, 1)
            struct.pack_into("<I", b, 0x12C, e.size & 0xFFFFFFFF)
            struct.pack_into("<Q", b, 0x134, 0x808080808080)
            struct.pack_into("<I", b, 0x168, 1)
            artist = (t.get("artist") or "Unknown Artist").strip()
            album_artist = (t.get("album_artist") or artist).strip()
            album = (t.get("album") or "Unknown Album").strip()
            struct.pack_into("<I", b, 0x120,
                             self._album_ids[(album, album_artist)])
        artwork_id = int(t.get("_artwork_id") or 0)
        if artwork_id:
            struct.pack_into("<H", b, 0x7C, 1)
            b[0xA4] = 1
            struct.pack_into("<I", b, 0x80, int(t.get("_artwork_size") or 0))
            if HDR >= 0x164:
                struct.pack_into("<I", b, 0x160, artwork_id)
        # mediatype (0xD0) is what files the entry into the Music vs Videos
        # menus. Every track gets one: audio → ITDB_MEDIATYPE_AUDIO (1) so it
        # stays out of the Videos lists; video → its movie/TV/music-video type.
        # For TV, season_nr (0xD4) and episode_nr (0xD8) drive show→season
        # grouping. Offsets per libgpod; both fit the 0xF4 and 0x248 headers we
        # emit (the iPod only reads them when header_len >= 0xF4).
        struct.pack_into("<I", b, 0xD0, e.mediatype or MEDIATYPE_AUDIO)
        if is_video:
            b[0xB1] = 1                       # movie_flag — 5G wants it for video
            if e.mediatype == MEDIATYPE_TVSHOW:
                struct.pack_into("<I", b, 0xD4, int(e.video.get("season") or 0))
                struct.pack_into("<I", b, 0xD8, int(e.video.get("episode") or 0))
        return bytes(b) + body

    def _build_track_dataset(self) -> bytes:
        mhits = b"".join(self._build_mhit(e) for e in self._entries)
        mhlt = bytearray(92)
        mhlt[0:4] = b"mhlt"
        struct.pack_into("<I", mhlt, 4, 92)
        struct.pack_into("<I", mhlt, 8, len(self._entries))
        return self._wrap_mhsd(1, bytes(mhlt) + mhits)

    def _build_mhyp(self, title: str, track_ids: list[int], is_master: bool,
                    pl_id: int) -> bytes:
        title_mhod = _string_mhod(MHOD_TITLE, title)
        mhips = b""
        for pos, tid in enumerate(track_ids):
            mhip = bytearray(76)
            mhip[0:4] = b"mhip"
            struct.pack_into("<I", mhip, 0x04, 76)
            pos_mhod = _position_mhod(pos)
            struct.pack_into("<I", mhip, 0x08, 76 + len(pos_mhod))  # total length
            struct.pack_into("<I", mhip, 0x0C, 1)        # one child mhod
            struct.pack_into("<I", mhip, 0x18, tid)      # referenced track id
            mhips += bytes(mhip) + pos_mhod
        body = title_mhod + mhips

        mhyp = bytearray(108)
        mhyp[0:4] = b"mhyp"
        struct.pack_into("<I", mhyp, 0x04, 108)
        struct.pack_into("<I", mhyp, 0x08, 108 + len(body))   # total length
        struct.pack_into("<I", mhyp, 0x0C, 1)                 # number of mhods
        struct.pack_into("<I", mhyp, 0x10, len(track_ids))    # number of mhips
        struct.pack_into("<I", mhyp, 0x14, 1 if is_master else 0)  # master flag
        struct.pack_into("<I", mhyp, 0x18, _mac_time(time.time()))
        struct.pack_into("<Q", mhyp, 0x1C, 0x1000 + pl_id)    # persistent id
        return bytes(mhyp) + body

    def _build_playlist_dataset(self, mhsd_type: int) -> bytes:
        all_ids = [e.track_id for e in self._entries]
        playlists = [self._build_mhyp(self.device_name, all_ids, True, 0)]
        pl_id = 1
        valid_ids = set(all_ids)
        replaced_names = {name.casefold() for name, _keys in self._playlists}
        for name, track_ids in self._preserved_playlists:
            if name.casefold() in replaced_names:
                continue
            ids = [tid for tid in track_ids if tid in valid_ids]
            if not ids:
                continue
            playlists.append(self._build_mhyp(name, ids, False, pl_id))
            pl_id += 1
        for name, keys in self._playlists:
            ids = [self._by_key[k].track_id for k in keys]
            playlists.append(self._build_mhyp(name, ids, False, pl_id))
            pl_id += 1

        mhlp = bytearray(92)
        mhlp[0:4] = b"mhlp"
        struct.pack_into("<I", mhlp, 4, 92)
        struct.pack_into("<I", mhlp, 8, len(playlists))
        return self._wrap_mhsd(mhsd_type, bytes(mhlp) + b"".join(playlists))

    def _wrap_mhsd(self, mhsd_type: int, body: bytes) -> bytes:
        mhsd = bytearray(96)
        mhsd[0:4] = b"mhsd"
        struct.pack_into("<I", mhsd, 4, 96)
        struct.pack_into("<I", mhsd, 8, 96 + len(body))   # total length
        struct.pack_into("<I", mhsd, 12, mhsd_type)
        return bytes(mhsd) + body

    def _existing_datasets(self) -> list[tuple[int, bytes]]:
        db = self._existing_db
        if not db or db[:4] != b"mhbd":
            return []
        out = []
        off = _u32(db, 4)
        for _ in range(_u32(db, 0x14)):
            if db[off:off + 4] != b"mhsd":
                break
            total = _u32(db, off + 8)
            out.append((_u32(db, off + 12), db[off:off + total]))
            off += total
        return out

    def _build_db(self) -> bytes:
        self._prepare_index_ids()
        replacements = {
            1: self._build_track_dataset(),
            2: self._build_playlist_dataset(2),
            3: self._build_playlist_dataset(3),
        }
        if self._hash58:
            replacements[4] = self._build_album_dataset()
            datasets, seen = [], set()
            for typ, original in self._existing_datasets():
                datasets.append(replacements.get(typ, original))
                seen.add(typ)
            for typ in (1, 3, 2, 4):
                if typ not in seen:
                    datasets.append(replacements[typ])
        else:
            datasets = [replacements[1], replacements[2], replacements[3]]
        body = b"".join(datasets)
        if self._hash58 and self._existing_db and \
                _u32(self._existing_db, 4) >= 0xF4:
            mhbd = bytearray(self._existing_db[:0xF4])
        else:
            mhbd = bytearray(0xF4 if self._hash58 else 104)
        mhbd[0:4] = b"mhbd"
        struct.pack_into("<I", mhbd, 0x04, len(mhbd))
        struct.pack_into("<I", mhbd, 0x08, len(mhbd) + len(body))
        struct.pack_into("<I", mhbd, 0x0C, 1)                 # unknown, always 1
        struct.pack_into("<I", mhbd, 0x10, 0x30 if self._hash58 else 0x13)
        struct.pack_into("<I", mhbd, 0x14, len(datasets))     # number of children
        if not self._existing_db:
            struct.pack_into("<Q", mhbd, 0x18, 0x1234567890ABCDEF)
        db = bytes(mhbd) + body
        if self._hash58:
            db = hash58.sign(db, self._firewire_guid or "")
        return db

    def finalize(self) -> None:
        if any(e.art_path for e in self._entries):
            from . import ipod_artwork
            ipod_artwork.write(self.root, self._generation, self._entries)
        db = self._build_db()
        self.self_test(db)
        path = os.path.join(self.itunes, "iTunesDB")
        if os.path.exists(path):
            shutil.copyfile(path, path + ".iamped.bak")
        atomic_write_bytes(path, db)
        self._write_manifest()

    def _write_manifest(self) -> None:
        """Persist track_id → Plex ratingKey so playback stats read back from
        the device can be mapped to the right Plex tracks.

        Only iAmped-written tracks (``origin`` new/iamped) are recorded — tracks
        that were already on the device from another source stay *unclaimed* so
        provenance and read-back never misattribute foreign plays to Plex."""
        tracks = []
        for e in self._entries:
            if e.origin not in ("new", "iamped"):
                continue
            t = e.track
            rating = max(0, min(100, int(round((t.get("user_rating") or 0) * 10))))
            tracks.append({
                "track_id": e.track_id,
                "rating_key": str(t.get("rating_key")),
                "rating_written": rating,
                "location": e.location,      # stable provenance key (survives
                                             # Apple renumbering track IDs)
                "path": location_to_relpath(e.location),
                "size": e.size,
                "ext": e.ext,
                "mac_added": e.mac_added,
                "title": t.get("title"), "artist": t.get("artist"),
                "media": "video" if e.mediatype in _VIDEO_TYPES else "audio",
                # Persist the video typing so a re-sync recovers it even if the
                # on-device record was written by an older (or differently laid
                # out) iAmped — the DB read alone can't be trusted across format
                # fixes. Harmless for audio (mediatype 0/absent).
                "mediatype": e.mediatype if e.mediatype in _VIDEO_TYPES else 0,
                "season_number": int((e.video or {}).get("season") or 0),
                "episode_number": int((e.video or {}).get("episode") or 0),
                "source_signature": t.get("_sync_signature"),
            })
        write_manifest(self.root, "ipod", {
            "device_name": self.device_name,
            "tracks": tracks,
        })

    def records(self) -> list[dict]:
        tracks = []
        for e in self._entries:
            if e.origin not in ("new", "iamped"):
                continue
            tracks.append({
                "rating_key": str(e.track.get("rating_key")),
                "track_id": e.track_id,
                "path": location_to_relpath(e.location),
                "location": e.location,
                "ext": e.ext,
                "size": e.size,
                "mac_added": e.mac_added,
                "title": e.track.get("title"),
                "artist": e.track.get("artist"),
                "source_signature": e.track.get("_sync_signature"),
            })
        return tracks

    # ---- structural verification -----------------------------------------
    def self_test(self, db: bytes | None = None) -> dict:
        """Re-parse the produced database and assert structural invariants.
        Raises AssertionError on mismatch; returns a small summary dict."""
        if db is None:
            with open(os.path.join(self.itunes, "iTunesDB"), "rb") as fh:
                db = fh.read()
        assert db[0:4] == b"mhbd", "missing mhbd header"
        total = struct.unpack_from("<I", db, 8)[0]
        assert total == len(db), f"mhbd total {total} != file {len(db)}"
        n_children = struct.unpack_from("<I", db, 0x14)[0]

        # walk datasets
        track_count = None
        master_seen = False
        off = struct.unpack_from("<I", db, 4)[0]   # past mhbd header
        for _ in range(n_children):
            assert db[off:off + 4] == b"mhsd", f"expected mhsd at {off}"
            mhsd_total = struct.unpack_from("<I", db, off + 8)[0]
            mhsd_type = struct.unpack_from("<I", db, off + 12)[0]
            inner = off + struct.unpack_from("<I", db, off + 4)[0]
            if mhsd_type == 1:
                assert db[inner:inner + 4] == b"mhlt"
                track_count = struct.unpack_from("<I", db, inner + 8)[0]
            elif mhsd_type in (2, 3):
                assert db[inner:inner + 4] == b"mhlp"
                # first playlist must be the master
                ply = inner + struct.unpack_from("<I", db, inner + 4)[0]
                if db[ply:ply + 4] == b"mhyp":
                    master_seen = master_seen or \
                        struct.unpack_from("<I", db, ply + 0x14)[0] == 1
            elif mhsd_type == 4:
                assert db[inner:inner + 4] == b"mhla"
            off += mhsd_total

        assert track_count == len(self._entries), \
            f"track list says {track_count}, staged {len(self._entries)}"
        assert master_seen, "no master playlist found"

        # every location must resolve to a real file on the device
        for e in self._entries:
            rel = e.location.lstrip(":").replace(":", os.sep)
            assert os.path.exists(os.path.join(self.root, rel)), \
                f"missing audio file for track {e.track_id}: {rel}"
        # mediatype must round-trip: video entries keep their movie/TV type, and
        # audio entries must read back as ITDB_MEDIATYPE_AUDIO (never 0, or the
        # iPod leaks them into the Videos menus).
        rows = {r["track_id"]: r for r in read_tracks_full(db)}
        for e in self._entries:
            r = rows.get(e.track_id)
            want = e.mediatype if e.mediatype in _VIDEO_TYPES else MEDIATYPE_AUDIO
            assert r and r["mediatype"] == want, \
                f"track {e.track_id} mediatype {r and r['mediatype']} != {want}"
        if self._hash58:
            assert _u32(db, 4) >= 0xF4, "hash58 database has a short mhbd"
            assert struct.unpack_from("<H", db, 0x30)[0] == 1, \
                "hash58 database has the wrong authentication scheme"
            assert hash58.verify(db, self._firewire_guid or ""), \
                "generated iTunesDB has an invalid hash58"
        return {"tracks": track_count, "bytes": len(db),
                "playlists": len(self._playlists) + 1,
                "videos": sum(1 for e in self._entries
                              if e.mediatype in _VIDEO_TYPES),
                "hash58": self._hash58}
