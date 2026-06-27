"""Thin wrapper around plexapi for the bits iAmped needs.

Plexamp is only a *client*; the metadata (play counts, ratings, playlists) lives
on the Plex Media Server. We talk to the server's HTTP API with a token.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from itertools import zip_longest
from typing import Iterator, Optional
from urllib.parse import quote

from plexapi.myplex import MyPlexAccount, MyPlexPinLogin
from plexapi.playqueue import PlayQueue
from plexapi.server import PlexServer

from . import __version__


@dataclass
class TrackMeta:
    rating_key: str
    title: str
    artist: str
    album: str
    album_artist: str
    genre: str
    year: Optional[int]
    track_number: Optional[int]
    disc_number: Optional[int]
    duration_ms: int
    view_count: int
    user_rating: Optional[float]      # Plex scale 0..10
    last_viewed_at: Optional[int]     # epoch seconds
    container: str                    # mp3 / flac / m4a ...
    codec: str
    bitrate: Optional[int]
    file_size: int
    part_key: str                     # /library/parts/.../file.ext  (for download)
    server_file: str                  # path on the Plex server (informational)
    album_key: str = ""
    album_thumb: str = ""


@dataclass
class VideoMeta:
    """A movie or TV episode pulled from a Plex video library. Mirrors
    :class:`TrackMeta`, but carries the video/TV fields the device backends need
    to build the on-device Movies / TV Shows menus."""
    rating_key: str
    kind: str                         # "movie" | "episode"
    title: str
    summary: str
    year: Optional[int]
    duration_ms: int
    container: str                    # mp4 / mkv / avi ...
    video_codec: str                  # h264 / hevc / mpeg4 ...
    audio_codec: str
    width: Optional[int]
    height: Optional[int]
    bitrate: Optional[int]            # kbps, whole-file
    file_size: int
    part_key: str                     # /library/parts/.../file.ext  (for download)
    server_file: str                  # path on the Plex server (informational)
    thumb: str = ""                   # poster / episode thumb image key
    # Episode-only grouping fields (empty/0 for movies):
    show_title: str = ""
    show_key: str = ""
    season_number: int = 0
    episode_number: int = 0


def connect(baseurl: str, token: str, timeout: int = 30) -> PlexServer:
    return PlexServer(baseurl.rstrip("/"), token, timeout=timeout)


def start_oauth(client_identifier: str, timeout: int = 10) -> MyPlexPinLogin:
    """Create a Plex OAuth/PIN login request for this iAmped installation."""
    headers = {
        "X-Plex-Client-Identifier": client_identifier,
        "X-Plex-Product": "iAmped",
        "X-Plex-Version": __version__,
        "X-Plex-Device-Name": "iAmped",
    }
    return MyPlexPinLogin(
        oauth=True, headers=headers, requestTimeout=timeout)


def oauth_account(token: str, timeout: int = 10) -> MyPlexAccount:
    return MyPlexAccount(token=token, timeout=timeout)


def account_servers(account: MyPlexAccount) -> list:
    """Return only Plex Media Server resources available to the account."""
    return [r for r in account.resources()
            if "server" in {
                item.strip()
                for item in (getattr(r, "provides", "") or "").split(",")
            }]


def connect_resource(resource, timeout: int = 10) -> PlexServer:
    """Connect using Plex's preferred local/remote/relay URL ordering."""
    return resource.connect(timeout=timeout)


def server_info(server: PlexServer) -> dict:
    return {
        "name": server.friendlyName,
        "version": server.version,
        "platform": server.platform,
    }


def music_sections(server: PlexServer) -> list[str]:
    return [s.title for s in server.library.sections() if s.type == "artist"]


def _first(obj, *names, default=None):
    for n in names:
        v = getattr(obj, n, None)
        if v not in (None, ""):
            return v
    return default


def track_to_meta(track) -> Optional[TrackMeta]:
    """Convert a plexapi Track into our flat TrackMeta. Returns None if it has
    no playable media part."""
    media = getattr(track, "media", None) or []
    if not media or not media[0].parts:
        return None
    part = media[0].parts[0]
    m = media[0]

    last = getattr(track, "lastViewedAt", None)
    return TrackMeta(
        rating_key=str(track.ratingKey),
        title=_first(track, "title", default="Unknown"),
        artist=_first(track, "grandparentTitle", "originalTitle", default="Unknown Artist"),
        album=_first(track, "parentTitle", default="Unknown Album"),
        album_artist=_first(track, "grandparentTitle", default="Unknown Artist"),
        genre="",  # genres require an extra fetch; filled lazily elsewhere if needed
        year=_first(track, "parentYear", "year"),
        track_number=getattr(track, "index", None),
        disc_number=getattr(track, "parentIndex", None),
        duration_ms=int(getattr(track, "duration", 0) or 0),
        view_count=int(getattr(track, "viewCount", 0) or 0),
        user_rating=getattr(track, "userRating", None),
        last_viewed_at=int(last.timestamp()) if last else None,
        container=(getattr(part, "container", None) or getattr(m, "container", "") or "").lower(),
        codec=(getattr(m, "audioCodec", "") or "").lower(),
        bitrate=getattr(m, "bitrate", None),
        file_size=int(getattr(part, "size", 0) or 0),
        part_key=part.key,
        server_file=getattr(part, "file", "") or "",
        album_key=str(getattr(track, "parentRatingKey", "") or ""),
        album_thumb=getattr(track, "parentThumb", "") or "",
    )


def iter_tracks(server: PlexServer, section_title: str,
                progress=None) -> Iterator[TrackMeta]:
    """Yield every track in a music section. `progress(done, total)` optional."""
    section = server.library.section(section_title)
    total = section.totalSize if hasattr(section, "totalSize") else None
    done = 0
    # searchTracks paginates internally in plexapi via container args.
    for track in section.searchTracks():
        meta = track_to_meta(track)
        done += 1
        if progress and (done % 50 == 0 or done == total):
            progress(done, total)
        if meta:
            yield meta


def video_sections(server: PlexServer) -> list[dict]:
    """Movie and TV-show libraries on the server (the video counterpart to
    :func:`music_sections`)."""
    return [{"title": s.title, "type": s.type}
            for s in server.library.sections()
            if s.type in ("movie", "show")]


def _video_media_part(video):
    """Return (media, part) for the first playable media of a movie/episode, or
    (None, None) when it has no downloadable part."""
    media = getattr(video, "media", None) or []
    if not media or not media[0].parts:
        return None, None
    return media[0], media[0].parts[0]


def movie_to_meta(video) -> Optional[VideoMeta]:
    """Convert a plexapi Movie into our flat VideoMeta. None if unplayable."""
    m, part = _video_media_part(video)
    if part is None:
        return None
    return VideoMeta(
        rating_key=str(video.ratingKey),
        kind="movie",
        title=_first(video, "title", default="Untitled"),
        summary=_first(video, "summary", default="") or "",
        year=_first(video, "year"),
        duration_ms=int(getattr(video, "duration", 0) or 0),
        container=(getattr(part, "container", None) or getattr(m, "container", "") or "").lower(),
        video_codec=(getattr(m, "videoCodec", "") or "").lower(),
        audio_codec=(getattr(m, "audioCodec", "") or "").lower(),
        width=getattr(m, "width", None),
        height=getattr(m, "height", None),
        bitrate=getattr(m, "bitrate", None),
        file_size=int(getattr(part, "size", 0) or 0),
        part_key=part.key,
        server_file=getattr(part, "file", "") or "",
        thumb=getattr(video, "thumb", "") or "",
    )


def episode_to_meta(ep) -> Optional[VideoMeta]:
    """Convert a plexapi Episode into VideoMeta with show/season/episode
    grouping. None if unplayable."""
    m, part = _video_media_part(ep)
    if part is None:
        return None
    return VideoMeta(
        rating_key=str(ep.ratingKey),
        kind="episode",
        title=_first(ep, "title", default="Untitled"),
        summary=_first(ep, "summary", default="") or "",
        year=_first(ep, "year"),
        duration_ms=int(getattr(ep, "duration", 0) or 0),
        container=(getattr(part, "container", None) or getattr(m, "container", "") or "").lower(),
        video_codec=(getattr(m, "videoCodec", "") or "").lower(),
        audio_codec=(getattr(m, "audioCodec", "") or "").lower(),
        width=getattr(m, "width", None),
        height=getattr(m, "height", None),
        bitrate=getattr(m, "bitrate", None),
        file_size=int(getattr(part, "size", 0) or 0),
        part_key=part.key,
        server_file=getattr(part, "file", "") or "",
        thumb=getattr(ep, "thumb", "") or "",
        show_title=_first(ep, "grandparentTitle", default="") or "",
        show_key=str(getattr(ep, "grandparentRatingKey", "") or ""),
        season_number=int(getattr(ep, "parentIndex", 0) or 0),
        episode_number=int(getattr(ep, "index", 0) or 0),
    )


def iter_movies(server: PlexServer, section_title: str,
                progress=None) -> Iterator[VideoMeta]:
    """Yield every movie in a movie section. `progress(done, total)` optional."""
    section = server.library.section(section_title)
    total = getattr(section, "totalSize", None)
    done = 0
    for video in section.all():
        meta = movie_to_meta(video)
        done += 1
        if progress and (done % 25 == 0 or done == total):
            progress(done, total)
        if meta:
            yield meta


def list_shows(server: PlexServer, section_title: str) -> list[dict]:
    """TV shows in a show section: {key, title, year, thumb, episode_count}."""
    section = server.library.section(section_title)
    out: list[dict] = []
    for show in section.all():
        out.append({
            "key": str(show.ratingKey),
            "title": _first(show, "title", default="Untitled"),
            "year": _first(show, "year"),
            "thumb": getattr(show, "thumb", "") or "",
            "episode_count": int(getattr(show, "leafCount", 0) or 0),
        })
    return out


def iter_episodes(server: PlexServer, show_key: str) -> Iterator[VideoMeta]:
    """Yield every episode of a show (in season/episode order)."""
    show = fetch_track(server, show_key)   # fetchItem is type-agnostic
    if show is None:
        return
    try:
        episodes = show.episodes()
    except Exception:
        episodes = []
    for ep in episodes:
        meta = episode_to_meta(ep)
        if meta:
            yield meta


@dataclass
class PlaylistMeta:
    plex_id: str
    title: str
    smart: bool
    duration_ms: int
    item_keys: list[str] = field(default_factory=list)   # ordered rating keys


def list_playlists(server: PlexServer) -> list[PlaylistMeta]:
    out: list[PlaylistMeta] = []
    for pl in server.playlists():
        if getattr(pl, "playlistType", None) != "audio":
            continue
        try:
            items = pl.items()
        except Exception:
            items = []
        out.append(PlaylistMeta(
            plex_id=str(pl.ratingKey),
            title=pl.title,
            smart=bool(getattr(pl, "smart", False)),
            duration_ms=int(getattr(pl, "duration", 0) or 0),
            item_keys=[str(i.ratingKey) for i in items],
        ))
    return out


def fetch_track(server: PlexServer, rating_key: str):
    """Return the plexapi Track for a rating key (or None)."""
    try:
        return server.fetchItem(int(rating_key))
    except Exception:
        return None


def sonically_similar(server: PlexServer, rating_key: str,
                      limit: int = 50) -> tuple[list[TrackMeta], Optional[str]]:
    """Plex Sonic Analysis neighbours of a seed track.

    Returns (tracks, error). error is a human message when the server has no
    sonic analysis for the track (or the plexapi build lacks the call).
    """
    track = fetch_track(server, rating_key)
    if track is None:
        return [], "Could not load the seed track from Plex."
    if not hasattr(track, "sonicallySimilar"):
        return [], "This plexapi build has no sonic-similarity support."
    try:
        similar = track.sonicallySimilar(limit=limit)
    except Exception as exc:  # server without Sonic Analysis raises
        return [], f"Sonic Analysis unavailable for this track ({exc})."
    from .filler import radio_key
    metas = []
    seen_keys: set[str] = set()
    seen_song: set[str] = set()                 # fold same song from other albums/variants
    for t in [track] + list(similar):          # seed first
        m = track_to_meta(t)
        if not m or m.rating_key in seen_keys:
            continue
        song = radio_key(m.artist, m.title)
        if song in seen_song:
            continue
        seen_keys.add(m.rating_key)
        seen_song.add(song)
        metas.append(m)
    if len(metas) <= 1:
        return metas, "No sonically similar tracks were returned (is Sonic Analysis enabled?)."
    return metas, None


def list_stations(server: PlexServer, section_title: str) -> list[dict]:
    """The library's built-in radio stations (Plexamp's Stations menu) —
    e.g. Library Radio, Deep Cuts Radio, Time Travel Radio, Random Album Radio.
    The exact set depends on the library's Sonic Analysis state."""
    try:
        stations = server.library.section(section_title).stations() or []
    except Exception:
        return []
    return [{"title": s.title, "key": s.key} for s in stations]


def station_tracks(server: PlexServer, section_title: str, station_title: str,
                   limit: int = 200) -> tuple[list[TrackMeta], Optional[str]]:
    """Materialize tracks from a built-in station. Stations are *endless* — each
    play-queue draw returns a fresh ~20-track batch — so we poll repeatedly and
    accumulate unique tracks until we reach `limit` (or it stops yielding new
    ones). Returns (tracks, warning)."""
    try:
        stations = server.library.section(section_title).stations() or []
    except Exception as exc:  # noqa: BLE001
        return [], f"Could not read stations ({exc})."
    st = next((s for s in stations if s.title == station_title), None)
    if st is None:
        return [], f"Station “{station_title}” is not available for this library."

    from .filler import radio_key
    metas: list[TrackMeta] = []
    seen: set[str] = set()                     # rating_keys already taken
    seen_song: set[str] = set()                # song identity — same song, any album/variant
    for _ in range(12):                       # cap the polling
        try:
            pq = PlayQueue.fromStationKey(server, st.key)
        except Exception as exc:  # noqa: BLE001
            if metas:
                break
            return [], f"Could not start station “{station_title}” ({exc})."
        fresh = 0
        for t in pq.items:
            if getattr(t, "type", None) != "track":
                continue
            m = track_to_meta(t)
            if not m or m.rating_key in seen:
                continue
            song = radio_key(m.artist, m.title)
            if song in seen_song:              # same song from another album/variant
                continue
            seen.add(m.rating_key)
            seen_song.add(song)
            metas.append(m)
            fresh += 1
            if len(metas) >= limit:
                return metas, None
        if fresh == 0:                        # station exhausted / repeating
            break
    if not metas:
        return [], f"Station “{station_title}” returned no tracks."
    return metas, None


def find_artist(server: PlexServer, section_title: str, name: str):
    """Best-effort artist lookup by name within a music section."""
    section = server.library.section(section_title)
    try:
        hits = section.searchArtists(title=name, maxresults=5)
    except Exception:
        hits = section.search(libtype="artist", title=name, maxresults=5)
    if not hits:
        return None
    # prefer an exact (case-insensitive) title match, else the top hit
    low = name.strip().lower()
    return next((a for a in hits if (a.title or "").lower() == low), hits[0])


def _has_sonic(track) -> bool:
    h = getattr(track, "hasSonicAnalysis", None)
    try:
        return h() if callable(h) else bool(h)
    except Exception:
        return False


def _artist_tracks(artist) -> list:
    """All of an artist's tracks, with an album-walk fallback."""
    try:
        ts = artist.tracks()
        if ts:
            return list(ts)
    except Exception:
        pass
    out = []
    try:
        for al in artist.albums():
            out.extend(al.tracks())
    except Exception:
        pass
    return out


def artist_radio(server: PlexServer, artist, method: str = "station",
                 limit: int = 200,
                 max_distance: Optional[float] = None) -> tuple[list[TrackMeta], Optional[str]]:
    """Build an 'artist radio' track list using Plex Sonic Analysis.

    Plex's artist-level station/``sonicallySimilar`` return *artists*, not a
    playable track list, so we seed from the artist's own tracks and expand via
    *track*-level sonic similarity (the same engine Plexamp's radio uses). If no
    track has sonic analysis, we fall back to pulling tracks from sonically
    similar artists. method='sonic' front-loads the neighbours; otherwise the
    artist's own catalogue is interleaved in.

    ``max_distance`` (0..1, Plex default ~0.25) tunes familiar↔discovery: lower
    keeps tracks sonically tighter to the seed, higher roams further afield.
    Returns (tracks, warning).
    """
    warn = None
    own = _artist_tracks(artist)
    seeds = [t for t in own if _has_sonic(t)][:6]

    def _similar(track, n):
        kw = {"limit": n}
        if max_distance is not None:
            kw["maxDistance"] = max_distance
        return track.sonicallySimilar(**kw)

    neighbours: list = []
    if seeds:
        per = max(20, limit // len(seeds))
        for s in seeds:
            try:
                neighbours.extend(_similar(s, per))
            except Exception:
                pass
    if not neighbours:
        warn = ("No sonic analysis for this artist's tracks; "
                "built the station from similar artists instead.")
        try:
            for sim in artist.sonicallySimilar(limit=8):
                neighbours.extend(_artist_tracks(sim)[:6])
        except Exception:
            pass

    # Assemble: pure-sonic front-loads neighbours; default interleaves the
    # artist's own catalogue with the neighbours for an on-artist station feel.
    if method == "sonic":
        ordered = neighbours + own
    else:
        ordered = []
        for a, b in zip_longest(own, neighbours):
            if a is not None:
                ordered.append(a)
            if b is not None:
                ordered.append(b)

    from .filler import radio_key
    metas: list[TrackMeta] = []
    seen_keys: set[str] = set()
    seen_song: set[str] = set()       # song identity — drop same song, other album/variant
    for t in ordered:
        if getattr(t, "type", "track") != "track":
            continue
        m = track_to_meta(t)
        if not m or m.rating_key in seen_keys:
            continue
        # Album-agnostic identity (folds unicode/punctuation/feat./remaster/live)
        # so the same song from a different album or release doesn't duplicate.
        song = radio_key(m.artist, m.title)
        if song in seen_song:
            continue
        seen_keys.add(m.rating_key)
        seen_song.add(song)
        metas.append(m)
        if len(metas) >= limit:
            break
    if not metas:
        return [], warn or "Plex returned no playable radio tracks for this artist."
    return metas, warn


def scan_path(server: PlexServer, section_title: str, path: str | None = None) -> None:
    """Trigger a Plex library scan, optionally scoped to a single folder so the
    server picks up newly-dropped files without a full rescan."""
    section = server.library.section(section_title)
    if path:
        section.update(path=path)
    else:
        section.update()


def track_in_library(server: PlexServer, section_title: str,
                     artist: str, title: str) -> bool:
    """Whether a track matching (artist, title) now exists in the section —
    used to confirm an ingested file was actually scanned in before we delete
    it from the device. Album-agnostic, same identity as the radio dedup."""
    from .filler import match_key
    want = match_key(artist, title)
    try:
        section = server.library.section(section_title)
        hits = section.searchTracks(title=title, maxresults=30)
    except Exception:  # noqa: BLE001
        return False
    for t in hits:
        cand_artist = _first(t, "grandparentTitle", "originalTitle", default="")
        if match_key(cand_artist, getattr(t, "title", "")) == want:
            return True
    return False


def stream_url(server: PlexServer, part_key: str) -> str:
    return server.url(f"{part_key}?download=1", includeToken=True)


_IDENT = "com.plexapp.plugins.library"


def scrobble(server: PlexServer, rating_key: str) -> None:
    """Mark a track played once — increments Plex viewCount and sets
    lastViewedAt. Call once per play to add a delta."""
    server.query(f"/:/scrobble?identifier={_IDENT}&key={rating_key}")


def set_rating(server: PlexServer, rating_key: str, rating_0_to_10: float) -> None:
    """Set a track's user rating (Plex scale 0..10)."""
    server.query(f"/:/rate?identifier={_IDENT}&key={rating_key}"
                 f"&rating={rating_0_to_10}")


def download_part(server: PlexServer, part_key: str, dest_path: str,
                  progress=None, chunk: int = 1 << 20) -> str:
    """Download the original audio file for a track to dest_path."""
    url = server.url(f"{part_key}?download=1", includeToken=True)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    tmp = dest_path + ".part"
    with server._session.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0) or 0)
        done = 0
        with open(tmp, "wb") as fh:
            for block in r.iter_content(chunk_size=chunk):
                if not block:
                    continue
                fh.write(block)
                done += len(block)
                if progress:
                    progress(done, total)
    os.replace(tmp, dest_path)
    return dest_path


def download_image(server: PlexServer, image_key: str, dest_path: str,
                   width: int = 600, height: int = 600) -> str:
    """Download a Plex image resource, transcoded to a bounded JPEG."""
    if not image_key:
        raise ValueError("No Plex image key")
    url = server.url(
        f"/photo/:/transcode?width={int(width)}&height={int(height)}"
        f"&minSize=1&upscale=0&url={quote(image_key, safe='')}",
        includeToken=True)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    tmp = dest_path + ".part"
    with server._session.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with open(tmp, "wb") as fh:
            for block in response.iter_content(chunk_size=1 << 16):
                if block:
                    fh.write(block)
    os.replace(tmp, dest_path)
    return dest_path
