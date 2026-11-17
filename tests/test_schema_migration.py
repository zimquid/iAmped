"""A library.db written before multi-source support must still open."""
import sqlite3

from iamped.library import Library

OLD_TRACKS = """
CREATE TABLE tracks (
    rating_key TEXT PRIMARY KEY, title TEXT, artist TEXT, album TEXT,
    album_artist TEXT, genre TEXT, year INTEGER, track_number INTEGER,
    disc_number INTEGER, duration_ms INTEGER, view_count INTEGER DEFAULT 0,
    user_rating REAL, last_viewed_at INTEGER, container TEXT, codec TEXT,
    bitrate INTEGER, file_size INTEGER, part_key TEXT, server_file TEXT,
    cached_path TEXT, cached_size INTEGER
);
CREATE TABLE playlists (
    plex_id TEXT PRIMARY KEY, title TEXT, smart INTEGER,
    duration_ms INTEGER, item_count INTEGER
);
CREATE TABLE local_playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT, kind TEXT DEFAULT 'manual', created INTEGER, rules TEXT
);
INSERT INTO tracks (rating_key, title, artist) VALUES ('1', 'Song', 'Band');
"""


def test_pre_source_id_database_opens_and_migrates(tmp_path):
    db = tmp_path / "library.db"
    with sqlite3.connect(db) as c:
        c.executescript(OLD_TRACKS)

    lib = Library(str(db))

    with sqlite3.connect(db) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(tracks)")}
        idx = {r[1] for r in c.execute("PRAGMA index_list(tracks)")}
        src = c.execute("SELECT source_id FROM tracks WHERE rating_key='1'").fetchone()
    assert {"source_id", "remote_id", "album_key"} <= cols
    assert "idx_tracks_source" in idx
    assert src == ("plex",)
    assert lib.stats("plex")["tracks"] == 1
