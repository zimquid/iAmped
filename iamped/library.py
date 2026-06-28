"""Local SQLite catalog of Plex metadata + bookkeeping for cached audio.

This is the reusable "pool": build it once from Plex, then plan/sync many
devices from it without re-querying the server every time.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterable, Optional

from .plex_client import PlaylistMeta, TrackMeta

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    rating_key   TEXT PRIMARY KEY,
    title        TEXT, artist TEXT, album TEXT, album_artist TEXT,
    genre        TEXT, year INTEGER, track_number INTEGER, disc_number INTEGER,
    duration_ms  INTEGER, view_count INTEGER DEFAULT 0,
    user_rating  REAL, last_viewed_at INTEGER,
    container    TEXT, codec TEXT, bitrate INTEGER, file_size INTEGER,
    part_key     TEXT, server_file TEXT, album_key TEXT, album_thumb TEXT,
    cached_path  TEXT, cached_size INTEGER
);
CREATE TABLE IF NOT EXISTS playlists (
    plex_id TEXT PRIMARY KEY, title TEXT, smart INTEGER,
    duration_ms INTEGER, item_count INTEGER
);
CREATE TABLE IF NOT EXISTS playlist_items (
    plex_id TEXT, rating_key TEXT, position INTEGER,
    PRIMARY KEY (plex_id, position)
);
CREATE TABLE IF NOT EXISTS local_playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT, kind TEXT DEFAULT 'manual', created INTEGER, rules TEXT
);
CREATE TABLE IF NOT EXISTS local_playlist_items (
    playlist_id INTEGER, rating_key TEXT, position INTEGER,
    PRIMARY KEY (playlist_id, position)
);
CREATE INDEX IF NOT EXISTS idx_tracks_views ON tracks(view_count DESC);
CREATE INDEX IF NOT EXISTS idx_tracks_rating ON tracks(user_rating DESC);
"""

BROWSE_ORDER = {
    "most_played": "view_count DESC, user_rating DESC",
    "top_rated": "user_rating DESC, view_count DESC",
    "recently_played": "last_viewed_at DESC",
    "random": "RANDOM()",
    "title": "title COLLATE NOCASE",
    "artist": "artist COLLATE NOCASE, album COLLATE NOCASE, disc_number, track_number",
    "album": "album COLLATE NOCASE, disc_number, track_number",
    "plays": "view_count DESC",
}

TRACK_COLS = [
    "rating_key", "title", "artist", "album", "album_artist", "genre", "year",
    "track_number", "disc_number", "duration_ms", "view_count", "user_rating",
    "last_viewed_at", "container", "codec", "bitrate", "file_size",
    "part_key", "server_file", "album_key", "album_thumb",
]


class Library:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            existing = {r["name"] for r in c.execute("PRAGMA table_info(tracks)")}
            for name in ("album_key", "album_thumb"):
                if name not in existing:
                    c.execute(f"ALTER TABLE tracks ADD COLUMN {name} TEXT")

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- writing metadata -------------------------------------------------
    def upsert_tracks(self, tracks: Iterable[TrackMeta]) -> int:
        rows = [tuple(getattr(t, col) for col in TRACK_COLS) for t in tracks]
        if not rows:
            return 0
        placeholders = ",".join(["?"] * len(TRACK_COLS))
        # preserve any existing cached_path/cached_size on update
        sql = (
            f"INSERT INTO tracks ({','.join(TRACK_COLS)}) VALUES ({placeholders}) "
            f"ON CONFLICT(rating_key) DO UPDATE SET "
            + ",".join(f"{c}=excluded.{c}" for c in TRACK_COLS if c != "rating_key")
        )
        with self._conn() as c:
            c.executemany(sql, rows)
        return len(rows)

    def replace_playlists(self, playlists: list[PlaylistMeta]) -> int:
        with self._conn() as c:
            c.execute("DELETE FROM playlists")
            c.execute("DELETE FROM playlist_items")
            for pl in playlists:
                c.execute(
                    "INSERT INTO playlists VALUES (?,?,?,?,?)",
                    (pl.plex_id, pl.title, int(pl.smart), pl.duration_ms,
                     len(pl.item_keys)),
                )
                c.executemany(
                    "INSERT OR REPLACE INTO playlist_items VALUES (?,?,?)",
                    [(pl.plex_id, rk, i) for i, rk in enumerate(pl.item_keys)],
                )
        return len(playlists)

    def set_cached(self, rating_key: str, path: Optional[str], size: Optional[int]):
        with self._conn() as c:
            c.execute(
                "UPDATE tracks SET cached_path=?, cached_size=? WHERE rating_key=?",
                (path, size, rating_key),
            )

    # ---- reading ----------------------------------------------------------
    def stats(self) -> dict:
        with self._conn() as c:
            t = c.execute("SELECT COUNT(*) n, COALESCE(SUM(file_size),0) sz, "
                          "COALESCE(SUM(duration_ms),0) dur FROM tracks").fetchone()
            cached = c.execute("SELECT COUNT(*) n FROM tracks "
                               "WHERE cached_path IS NOT NULL").fetchone()
            pl = c.execute("SELECT COUNT(*) n FROM playlists").fetchone()
        return {
            "tracks": t["n"], "total_bytes": t["sz"], "total_ms": t["dur"],
            "cached": cached["n"], "playlists": pl["n"],
        }

    def playlists(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT plex_id, title, smart, duration_ms, item_count "
                "FROM playlists ORDER BY title COLLATE NOCASE").fetchall()]

    def playlist_track_keys(self, plex_id: str) -> list[str]:
        with self._conn() as c:
            return [r["rating_key"] for r in c.execute(
                "SELECT rating_key FROM playlist_items WHERE plex_id=? "
                "ORDER BY position", (plex_id,)).fetchall()]

    def get_tracks(self, rating_keys: list[str]) -> dict[str, dict]:
        if not rating_keys:
            return {}
        out: dict[str, dict] = {}
        with self._conn() as c:
            CHUNK = 500
            for i in range(0, len(rating_keys), CHUNK):
                chunk = rating_keys[i:i + CHUNK]
                q = f"SELECT * FROM tracks WHERE rating_key IN ({','.join('?'*len(chunk))})"
                for r in c.execute(q, chunk):
                    out[r["rating_key"]] = dict(r)
        return out

    def ordered_tracks(self, strategy: str, limit: Optional[int] = None) -> list[dict]:
        order = BROWSE_ORDER.get(strategy, "view_count DESC")
        sql = f"SELECT * FROM tracks ORDER BY {order}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql).fetchall()]

    def search_candidates(self, artist: str, title: str, limit: int = 20) -> list[dict]:
        """Small candidate set for device-file reconciliation."""
        title_bits = [p for p in title.replace("-", " ").split() if len(p) > 2]
        artist_bits = [p for p in artist.replace("-", " ").split() if len(p) > 2]
        terms = title_bits[:2] + artist_bits[:1]
        if not terms:
            return []
        where = " OR ".join("(title LIKE ? OR artist LIKE ?)" for _ in terms)
        params = [value for term in terms for value in (f"%{term}%", f"%{term}%")]
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM tracks WHERE {where} LIMIT ?",
                params + [int(limit)]).fetchall()
        return [dict(r) for r in rows]

    def browse_tracks(self, search: str = "", sort: str = "artist",
                      offset: int = 0, limit: int = 200,
                      min_rating: float = 0, min_plays: int = 0,
                      artist: str = "", album: str = "",
                      genre: str = "", album_artist: str = "") -> dict:
        order = BROWSE_ORDER.get(sort, BROWSE_ORDER["artist"])
        where, params = [], []
        if search:
            where.append("(title LIKE ? OR artist LIKE ? OR album LIKE ?)")
            params += [f"%{search}%"] * 3
        if artist:
            where.append("artist = ? COLLATE NOCASE"); params.append(artist)
        if album:
            where.append("album = ? COLLATE NOCASE"); params.append(album)
        if album_artist:
            where.append("album_artist = ? COLLATE NOCASE"); params.append(album_artist)
        if genre:
            where.append("genre = ? COLLATE NOCASE"); params.append(genre)
        if min_rating:
            where.append("COALESCE(user_rating,0) >= ?"); params.append(min_rating)
        if min_plays:
            where.append("COALESCE(view_count,0) >= ?"); params.append(min_plays)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        with self._conn() as c:
            total = c.execute(f"SELECT COUNT(*) n FROM tracks {clause}", params).fetchone()["n"]
            rows = c.execute(
                f"SELECT * FROM tracks {clause} ORDER BY {order} LIMIT ? OFFSET ?",
                params + [int(limit), int(offset)]).fetchall()
        return {"total": total, "tracks": [dict(r) for r in rows]}

    def facets(self) -> dict[str, list[dict]]:
        def rows_for(column: str, order: str) -> list[dict]:
            with self._conn() as c:
                return [dict(r) for r in c.execute(
                    f"SELECT {column} name, COUNT(*) count "
                    f"FROM tracks WHERE COALESCE({column}, '') != '' "
                    f"GROUP BY {column} COLLATE NOCASE ORDER BY {order}"
                ).fetchall()]

        with self._conn() as c:
            albums = [dict(r) for r in c.execute(
                "SELECT album name, album_artist artist, COUNT(*) count, "
                "MIN(year) year, MIN(album_thumb) album_thumb "
                "FROM tracks WHERE COALESCE(album, '') != '' "
                "GROUP BY album COLLATE NOCASE, album_artist COLLATE NOCASE "
                "ORDER BY album COLLATE NOCASE"
            ).fetchall()]
        return {
            "artists": rows_for("artist", "artist COLLATE NOCASE"),
            "albums": albums,
            "genres": rows_for("genre", "genre COLLATE NOCASE"),
        }

    def query_keys(self, sort: str, limit: int, min_rating: float = 0,
                   min_plays: int = 0) -> list[str]:
        res = self.browse_tracks("", sort, 0, limit, min_rating, min_plays)
        return [t["rating_key"] for t in res["tracks"]]

    # ---- local (custom) playlists ----------------------------------------
    def create_local_playlist(self, title: str, kind: str, created: int,
                              rules: str, rating_keys: list[str]) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO local_playlists (title, kind, created, rules) VALUES (?,?,?,?)",
                (title, kind, created, rules))
            pid = cur.lastrowid
            c.executemany(
                "INSERT INTO local_playlist_items VALUES (?,?,?)",
                [(pid, rk, i) for i, rk in enumerate(rating_keys)])
        return pid

    def local_playlists(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT lp.id, lp.title, lp.kind, "
                "(SELECT COUNT(*) FROM local_playlist_items i WHERE i.playlist_id=lp.id) n "
                "FROM local_playlists lp ORDER BY lp.created DESC").fetchall()]

    def local_playlist_track_keys(self, pid: int) -> list[str]:
        with self._conn() as c:
            return [r["rating_key"] for r in c.execute(
                "SELECT rating_key FROM local_playlist_items WHERE playlist_id=? "
                "ORDER BY position", (pid,)).fetchall()]

    def add_to_local_playlist(self, pid: int, rating_keys: list[str]) -> int:
        with self._conn() as c:
            existing = {r["rating_key"] for r in c.execute(
                "SELECT rating_key FROM local_playlist_items WHERE playlist_id=?",
                (pid,)).fetchall()}
            start = (c.execute("SELECT COALESCE(MAX(position),-1) m FROM "
                               "local_playlist_items WHERE playlist_id=?", (pid,))
                     .fetchone()["m"]) + 1
            fresh = []
            for rk in rating_keys:
                if rk not in existing:
                    existing.add(rk)
                    fresh.append(rk)
            c.executemany("INSERT INTO local_playlist_items VALUES (?,?,?)",
                          [(pid, rk, start + i) for i, rk in enumerate(fresh)])
        return len(fresh)

    def delete_local_playlist(self, pid: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM local_playlists WHERE id=?", (pid,))
            c.execute("DELETE FROM local_playlist_items WHERE playlist_id=?", (pid,))

    # ---- unified playlist access (Plex + local) --------------------------
    def all_playlists(self) -> list[dict]:
        plex = [{"id": p["plex_id"], "title": p["title"], "smart": bool(p["smart"]),
                 "item_count": p["item_count"], "source": "plex"}
                for p in self.playlists()]
        local = [{"id": f"local:{r['id']}", "title": r["title"],
                  "smart": r["kind"] != "manual", "item_count": r["n"],
                  "source": "local", "kind": r["kind"]}
                 for r in self.local_playlists()]
        return local + plex

    def playlist_track_keys_any(self, pid: str) -> list[str]:
        if str(pid).startswith("local:"):
            return self.local_playlist_track_keys(int(str(pid).split(":", 1)[1]))
        return self.playlist_track_keys(pid)

    # ---- device → library writeback -------------------------------------
    def add_local_plays(self, rating_key: str, n: int,
                        last_played: int | None = None) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE tracks SET view_count = COALESCE(view_count,0) + ?, "
                "last_viewed_at = MAX(COALESCE(last_viewed_at,0), COALESCE(?,0)) "
                "WHERE rating_key = ?", (n, last_played, rating_key))

    def set_local_rating(self, rating_key: str, rating_0_to_10: float) -> None:
        with self._conn() as c:
            c.execute("UPDATE tracks SET user_rating = ? WHERE rating_key = ?",
                      (rating_0_to_10, rating_key))

    def find_by_meta(self, artist: str, title: str,
                     album: str = "") -> str | None:
        """Best-effort match a (artist, title) pair to a cached track —
        used for Audioscrobbler-log matching on non-iPod players."""
        with self._conn() as c:
            row = c.execute(
                "SELECT rating_key FROM tracks WHERE title = ? COLLATE NOCASE "
                "AND artist = ? COLLATE NOCASE "
                "ORDER BY (album = ? COLLATE NOCASE) DESC LIMIT 1",
                (title, artist, album)).fetchone()
        return row["rating_key"] if row else None
