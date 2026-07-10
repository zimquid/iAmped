"""Library catalog routes: provider-backed browsing, playlists, and radio."""
from __future__ import annotations

import json
import os
import time

from flask import Blueprint, jsonify, request

from .. import config, filler, plex_client, providers
from ..server import _lib, get_server, start_job
from ..sync import free_bytes, target_format

bp = Blueprint("library", __name__)

# Plays and ratings don't bump updatedAt on the Plex server, so a delta refresh
# sweeps all three change signals; each sweep only returns tracks changed since
# the watermark, so all three together are still a few-second operation.
_DELTA_FIELDS = ("updatedAt", "lastViewedAt", "lastRatedAt")
_LAST_SYNC_KEY = "last_sync_watermark"


def _connection():
    return providers.normalize(get_server())


def _source_id() -> str:
    source = config.active_source(config.load())
    return str((source or {}).get("id") or "plex")


def _upsert_stream(lib, metas, on_meta=None) -> int:
    """Batch-upsert a TrackMeta stream; returns the number written."""
    batch, count = [], 0
    for meta in metas:
        batch.append(meta)
        if on_meta:
            on_meta(meta)
        if len(batch) >= 200:
            count += lib.upsert_tracks(batch)
            batch = []
    if batch:
        count += lib.upsert_tracks(batch)
    return count


def _build_library_job(job, section: str, mode: str = "auto"):
    """Refresh the local catalog from the active provider.

    mode='full'  — read every track, then prune rows Plex no longer has.
    mode='delta' — read only tracks changed since the stored watermark.
    mode='auto'  — delta when a watermark exists, escalating to full if the
                   local count disagrees with the server afterwards (that's
                   the deletion case the delta filters can't see).
    """
    connection = _connection()
    provider = connection.provider
    source_id = connection.source_id
    lib = _lib()
    # The legacy Plex source keeps its original metadata key so an existing
    # cache still receives a true incremental refresh after this migration.
    watermark_key = (_LAST_SYNC_KEY if source_id == "plex"
                     else f"{_LAST_SYNC_KEY}:{source_id}")
    watermark = int(lib.get_meta(watermark_key, "0") or 0)
    incremental = connection.capabilities.incremental_refresh
    if mode == "auto":
        mode = "delta" if incremental and watermark else "full"
    if mode == "delta" and not incremental:
        mode = "full"
    # The next watermark comes from the wall clock at sweep start (Plex stamps
    # updatedAt with its own clock; a small overlap window absorbs skew — the
    # upserts are idempotent so re-reading a few tracks is free).
    sweep_start = int(time.time()) - 120

    count = 0
    if mode == "delta":
        job["phase"] = "delta"
        seen: set[str] = set()
        for field in _DELTA_FIELDS:
            job["message"] = f"Checking {provider.display_name} for changes ({field})…"
            fresh = (m for m in provider.iter_tracks(
                connection, section, since=watermark, since_field=field)
                if m.rating_key not in seen)
            count += _upsert_stream(lib, fresh,
                                    on_meta=lambda m: seen.add(m.rating_key))
            job["done"] = count
            job["message"] = f"Updated {count} changed track(s)…"
        try:
            server_total = provider.section_track_count(connection, section)
        except Exception:  # noqa: BLE001 - a failed count never breaks a refresh
            server_total = None
        if server_total is not None and server_total != lib.track_count(source_id):
            # Tracks were deleted on the server (or something drifted): the
            # delta filters can't express that, so fall through to a full pass.
            job["message"] = "Library counts differ — doing a full rebuild…"
            mode = "full"

    if mode == "full":
        job["phase"] = "tracks"

        def prog(done, total):
            job["done"], job["total"] = done, total or 0
            job["message"] = f"Reading track metadata… {done}"

        seen_keys: list[str] = []
        count = _upsert_stream(
            lib, provider.iter_tracks(connection, section, prog),
            on_meta=lambda m: seen_keys.append(m.rating_key))
        pruned = lib.prune_missing(seen_keys, source_id) if seen_keys else 0
    else:
        pruned = 0

    job["phase"] = "playlists"
    job["message"] = "Reading playlists…"
    pls = provider.list_playlists(connection)
    lib.replace_playlists(pls, source_id)
    lib.set_meta(watermark_key, str(sweep_start))
    job["result"] = {"tracks": count, "playlists": len(pls),
                     "mode": mode, "pruned": pruned,
                     "capabilities": connection.capabilities.as_dict()}
    job["message"] = (
        f"Updated {count} changed track(s) and {len(pls)} playlists."
        if mode == "delta" else
        f"Cached {count} tracks and {len(pls)} playlists"
        + (f" (removed {pruned} no longer on the server)" if pruned else "") + ".")
    # Preserve the cache across app rebuilds so this slow sync isn't repeated.
    config.mirror_library_to_stable()


@bp.post("/api/library/build")
def api_build():
    d = request.json or {}
    section = d.get("section") or config.load()["music_section"]
    if not section:
        return jsonify({"error": "No music section selected."}), 400
    mode = d.get("mode", "auto")
    if mode not in ("auto", "full", "delta"):
        return jsonify({"error": "mode must be auto, full or delta"}), 400
    config.save({"music_section": section})
    return jsonify({"job": start_job(_build_library_job, section, mode)})


@bp.get("/api/library/stats")
def api_stats():
    return jsonify(_lib().stats(_source_id()))


@bp.get("/api/playlists")
def api_playlists():
    return jsonify(_lib().all_playlists(_source_id()))


def _track_view(t: dict) -> dict:
    return {
        "rating_key": t["rating_key"], "title": t.get("title"),
        "artist": t.get("artist"), "album": t.get("album"),
        "plays": t.get("view_count") or 0, "rating": t.get("user_rating"),
        "genre": t.get("genre"), "year": t.get("year"),
        "album_thumb": t.get("album_thumb"),
        "duration_ms": t.get("duration_ms") or 0,
        "size": t.get("file_size") or 0, "container": t.get("container"),
        "lossless": filler.is_lossless(t),
    }


@bp.get("/api/library/facets")
def api_library_facets():
    return jsonify(_lib().facets(_source_id()))


@bp.get("/api/tracks")
def api_tracks():
    a = request.args
    res = _lib().browse_tracks(
        search=a.get("search", ""), sort=a.get("sort", "artist"),
        offset=int(a.get("offset", 0)), limit=int(a.get("limit", 200)),
        artist=a.get("artist", ""), album=a.get("album", ""),
        genre=a.get("genre", ""), album_artist=a.get("album_artist", ""),
        source_id=_source_id())
    return jsonify({"total": res["total"],
                    "tracks": [_track_view(t) for t in res["tracks"]]})


@bp.get("/api/playlist/<path:pid>/tracks")
def api_playlist_tracks(pid):
    lib = _lib()
    keys = lib.playlist_track_keys_any(pid)
    rows = lib.get_tracks(keys)
    tracks = [_track_view(rows[rk]) for rk in keys if rk in rows]
    return jsonify({"total": len(tracks), "tracks": tracks})


@bp.post("/api/track/<rk>/rating")
def api_track_rating(rk):
    d = request.json or {}
    try:
        rating = max(0.0, min(10.0, float(d.get("rating") or 0)))
        connection = _connection()
        track = _lib().get_tracks([rk]).get(rk)
        if not track:
            return jsonify({"error": "unknown track"}), 404
        connection.provider.set_rating(connection, track, rating)
        _lib().set_local_rating(rk, rating)
        return jsonify({"ok": True, "rating": rating})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400


@bp.post("/api/track/<rk>/scrobble")
def api_track_scrobble(rk):
    try:
        connection = _connection()
        track = _lib().get_tracks([rk]).get(rk)
        if not track:
            return jsonify({"error": "unknown track"}), 404
        connection.provider.scrobble(connection, track)
        _lib().add_local_plays(rk, 1, int(time.time()))
        return jsonify({"ok": True})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400


@bp.post("/api/playlist/sonic")
def api_playlist_sonic():
    d = request.json or {}
    seed = d.get("rating_key")
    if not seed:
        return jsonify({"error": "No seed track."}), 400
    try:
        connection = _connection()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400
    if connection.kind != "plex":
        return jsonify({"error": "Sonic playlists are a Plex-only feature."}), 409
    metas, err = plex_client.sonically_similar(
        connection.client, seed, int(d.get("limit", 50)))
    if err and len(metas) <= 1:
        return jsonify({"error": err}), 400
    lib = _lib()
    lib.upsert_tracks(metas)                     # make sure they're cached locally
    seed_meta = metas[0]
    title = d.get("title") or f"Sonic: {seed_meta.title}"
    pid = lib.create_local_playlist(
        title, "sonic", int(time.time()), json.dumps({"seed": seed}),
        [m.rating_key for m in metas], _source_id())
    return jsonify({"ok": True, "id": f"local:{pid}", "title": title,
                    "count": len(metas), "warning": err})


def _bound_and_save(d: dict, metas: list, kind: str, title: str,
                    rules: dict) -> dict:
    """Shared tail for the radio/station builders: size a generated track list
    to a count/size/device-fraction budget, cache it, and persist a local
    playlist that /api/sync can write to a device."""
    import dataclasses
    fmt = target_format(d.get("device_type", "ipod"))
    tl = bool(d.get("transcode_lossless", True))
    bitrate_k = int(d.get("target_bitrate_k")
                    or (256 if fmt == "aac" else 320))
    max_tracks = int(d["max_tracks"]) if d.get("max_tracks") else None
    max_bytes = int(float(d["max_mb"]) * 1024 * 1024) if d.get("max_mb") else None
    # "fit a part of the iPod": cap by a fraction of the device's free space
    if d.get("device_path") and os.path.isdir(d["device_path"]):
        fit = int(free_bytes(d["device_path"]) * float(d.get("fraction", 1.0)))
        max_bytes = min(max_bytes, fit) if max_bytes else fit

    meta_by_key = {m.rating_key: m for m in metas}
    bounded = filler.bound_tracks([dataclasses.asdict(m) for m in metas],
                                  max_tracks, max_bytes, tl, fmt, bitrate_k)
    keys = [t["rating_key"] for t in bounded["tracks"]]
    lib = _lib()
    lib.upsert_tracks([meta_by_key[k] for k in keys])   # cache for the sync
    pid = lib.create_local_playlist(title, kind, int(time.time()),
                                    json.dumps(rules), keys, _source_id())
    preview = [{"artist": t.get("artist"), "title": t.get("title"),
                "album": t.get("album"), "duration_ms": t.get("duration_ms"),
                "size": filler.device_size(t, tl, fmt, bitrate_k)}
               for t in bounded["tracks"][:300]]
    return {"ok": True, "id": f"local:{pid}", "title": title,
            "count": len(keys), "total_bytes": bounded["total_bytes"],
            "fetched": len(metas), "dropped": bounded["dropped"],
            "preview": preview}


@bp.post("/api/playlist/radio")
def api_playlist_radio():
    """Generate an 'artist radio' from Plex and size it to fit a device (or a
    portion of one). Body: artist, method ('station'|'sonic'), max_distance
    (0..1 familiar↔discovery), and any of max_tracks / max_mb / (device_path +
    fraction). Saves a local playlist a later /api/sync can write to the iPod."""
    d = request.json or {}
    name = (d.get("artist") or "").strip()
    if not name:
        return jsonify({"error": "No artist given."}), 400
    try:
        connection = _connection()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400
    if connection.kind != "plex":
        return jsonify({"error": "Radio playlists are a Plex-only feature."}), 409

    section = config.load()["music_section"]
    artist = plex_client.find_artist(connection.client, section, name)
    if artist is None:
        return jsonify({"error": f"Artist “{name}” not found in Plex."}), 404
    md = float(d["max_distance"]) if d.get("max_distance") is not None else None
    metas, warn = plex_client.artist_radio(
        connection.client, artist, d.get("method", "station"), int(d.get("fetch", 200)), md)
    if not metas:
        return jsonify({"error": warn or "Plex returned no radio tracks."}), 400

    title = d.get("title") or f"{artist.title} Radio"
    resp = _bound_and_save(d, metas, "radio", title,
                           {"artist": artist.title,
                            "method": d.get("method", "station"),
                            "max_distance": md})
    resp["artist"] = artist.title
    resp["warning"] = warn
    return jsonify(resp)


@bp.get("/api/stations")
def api_stations():
    """List the library's built-in radio stations (Plexamp's Stations menu)."""
    try:
        connection = _connection()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400
    if connection.kind != "plex":
        return jsonify({"stations": []})
    section = config.load()["music_section"]
    return jsonify({"stations": plex_client.list_stations(connection.client, section)})


@bp.post("/api/playlist/station")
def api_playlist_station():
    """Materialize one of the library's built-in stations (Library Radio, Deep
    Cuts, Time Travel, Random Album, …) into a device-sized playlist. Body:
    station (title), plus the same budget fields as /api/playlist/radio."""
    d = request.json or {}
    station = (d.get("station") or "").strip()
    if not station:
        return jsonify({"error": "No station given."}), 400
    try:
        connection = _connection()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400
    if connection.kind != "plex":
        return jsonify({"error": "Library stations are a Plex-only feature."}), 409
    section = config.load()["music_section"]
    metas, warn = plex_client.station_tracks(
        connection.client, section, station, int(d.get("fetch", 200)))
    if not metas:
        return jsonify({"error": warn or "Station returned no tracks."}), 400
    title = d.get("title") or station
    resp = _bound_and_save(d, metas, "station", title, {"station": station})
    resp["warning"] = warn
    return jsonify(resp)


@bp.post("/api/playlist/local")
def api_playlist_local():
    d = request.json or {}
    lib = _lib()
    title = d.get("title") or "New Playlist"
    keys = d.get("rating_keys", [])
    pid = lib.create_local_playlist(title, "manual", int(time.time()), "", keys,
                                    _source_id())
    return jsonify({"ok": True, "id": f"local:{pid}", "title": title,
                    "count": len(keys)})


@bp.post("/api/playlist/local/<int:pid>/add")
def api_playlist_local_add(pid):
    d = request.json or {}
    _lib().add_to_local_playlist(pid, d.get("rating_keys", []))
    return jsonify({"ok": True})


@bp.delete("/api/playlist/local/<int:pid>")
def api_playlist_local_delete(pid):
    _lib().delete_local_playlist(pid)
    return jsonify({"ok": True})


from .. import server as _server  # noqa: E402  (self-registration; see api/__init__)
_server.app.register_blueprint(bp)
