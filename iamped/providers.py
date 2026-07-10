"""Provider-neutral access to self-hosted music servers.

The device writers deliberately know nothing about where an audio file came
from.  This module is the boundary between that reusable sync pipeline and the
different server APIs.  ``rating_key`` remains the catalog/device identifier
for compatibility, while ``remote_id`` is the provider's native item id.

Plex's historical, un-prefixed keys are intentionally retained for the default
``plex`` source so existing library caches and device manifests continue to
mirror correctly.  New provider keys are namespaced by source id.
"""
from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Iterator
from urllib.parse import quote

import requests

from . import __version__, plex_client


class ProviderError(RuntimeError):
    """A connection or capability error safe to display in the UI."""


@dataclass(frozen=True)
class ProviderCapabilities:
    playlists: bool = True
    original_download: bool = True
    artwork: bool = True
    play_state_write: bool = False
    rating_write: bool = False
    incremental_refresh: bool = False
    sonic_radio: bool = False
    video: bool = False
    native_playback: bool = True

    def as_dict(self) -> dict[str, bool]:
        return {
            "playlists": self.playlists,
            "original_download": self.original_download,
            "artwork": self.artwork,
            "play_state_write": self.play_state_write,
            "rating_write": self.rating_write,
            "incremental_refresh": self.incremental_refresh,
            "sonic_radio": self.sonic_radio,
            "video": self.video,
            "native_playback": self.native_playback,
        }


@dataclass
class ProviderConnection:
    provider: "MediaProvider"
    client: Any
    source: dict[str, Any]

    @property
    def source_id(self) -> str:
        return str(self.source.get("id") or self.provider.kind)

    @property
    def kind(self) -> str:
        return self.provider.kind

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self.provider.capabilities(self)


def _safe_id(value: str, fallback: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", value or "").strip(".-")
    return value[:80] or fallback


def track_key(source_id: str, remote_id: str) -> str:
    """Catalog key for an item.

    ``plex`` is the pre-provider source and deliberately stays unprefixed.
    Every new server source includes its id, preventing cross-server item-id
    collisions without invalidating current manifests or cached downloads.
    """
    if source_id == "plex":
        return str(remote_id)
    return f"{source_id}:{remote_id}"


def _remote_id(track: dict | plex_client.TrackMeta) -> str:
    if isinstance(track, dict):
        return str(track.get("remote_id") or track.get("rating_key") or "")
    return str(getattr(track, "remote_id", "") or track.rating_key)


def _download_response(response: requests.Response, dest_path: str,
                       progress=None, chunk: int = 1 << 20) -> str:
    response.raise_for_status()
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    tmp = dest_path + ".part"
    have = 0
    try:
        have = os.path.getsize(tmp)
    except OSError:
        pass
    # Callers issue the request with Range when a partial file is present.
    resumed = bool(have and response.status_code == 206)
    mode = "ab" if resumed else "wb"
    total = int(response.headers.get("Content-Length", 0) or 0) + (
        have if resumed else 0)
    done = have if resumed else 0
    with open(tmp, mode) as fh:
        for block in response.iter_content(chunk_size=chunk):
            if not block:
                continue
            fh.write(block)
            done += len(block)
            if progress:
                progress(done, total)
    os.replace(tmp, dest_path)
    return dest_path


class MediaProvider:
    kind = "unknown"
    display_name = "Unknown"
    _capabilities = ProviderCapabilities()

    def capabilities(self, connection: ProviderConnection) -> ProviderCapabilities:
        return self._capabilities

    def connect(self, source: dict[str, Any]) -> ProviderConnection:
        raise NotImplementedError

    def server_info(self, connection: ProviderConnection) -> dict[str, str]:
        raise NotImplementedError

    def music_sections(self, connection: ProviderConnection) -> list[dict[str, str]]:
        raise NotImplementedError

    def iter_tracks(self, connection: ProviderConnection, section: str,
                    progress=None, since: int | None = None,
                    since_field: str = "updatedAt") -> Iterator[plex_client.TrackMeta]:
        raise NotImplementedError

    def section_track_count(self, connection: ProviderConnection, section: str) -> int:
        raise NotImplementedError

    def list_playlists(self, connection: ProviderConnection) -> list[plex_client.PlaylistMeta]:
        return []

    def download_track(self, connection: ProviderConnection, track: dict,
                       dest_path: str, progress=None) -> str:
        raise ProviderError(f"{self.display_name} does not provide original downloads.")

    def download_image(self, connection: ProviderConnection, image_key: str,
                       dest_path: str, width: int = 600, height: int = 600) -> str:
        raise ProviderError(f"{self.display_name} does not provide artwork.")

    def stream_url(self, connection: ProviderConnection, track: dict) -> str:
        raise ProviderError(f"{self.display_name} cannot provide a direct playback URL.")

    def stream_response(self, connection: ProviderConnection, track: dict,
                        headers: dict[str, str] | None = None,
                        stream: bool = True) -> requests.Response:
        raise ProviderError(f"{self.display_name} cannot proxy playback.")

    def scrobble(self, connection: ProviderConnection, track: dict) -> None:
        raise ProviderError(f"{self.display_name} does not support play-state updates.")

    def set_rating(self, connection: ProviderConnection, track: dict,
                   rating: float) -> None:
        raise ProviderError(f"{self.display_name} does not support ratings.")


class PlexProvider(MediaProvider):
    kind = "plex"
    display_name = "Plex"
    _capabilities = ProviderCapabilities(
        playlists=True, original_download=True, artwork=True,
        play_state_write=True, rating_write=True, incremental_refresh=True,
        sonic_radio=True, video=True, native_playback=True)

    def connect(self, source: dict[str, Any]) -> ProviderConnection:
        baseurl = str(source.get("baseurl") or "").strip()
        token = str(source.get("token") or "").strip()
        if not baseurl or not token:
            raise ProviderError("Enter both a Plex server URL and token, or use Sign in with Plex.")
        return ProviderConnection(self, plex_client.connect(baseurl, token), source)

    def server_info(self, connection: ProviderConnection) -> dict[str, str]:
        return plex_client.server_info(connection.client)

    def music_sections(self, connection: ProviderConnection) -> list[dict[str, str]]:
        return [{"id": s, "name": s} for s in plex_client.music_sections(connection.client)]

    def _meta(self, connection: ProviderConnection,
              meta: plex_client.TrackMeta) -> plex_client.TrackMeta:
        remote = str(meta.rating_key)
        sid = connection.source_id
        return replace(meta, rating_key=track_key(sid, remote),
                       source_id=sid, remote_id=remote)

    def iter_tracks(self, connection: ProviderConnection, section: str,
                    progress=None, since: int | None = None,
                    since_field: str = "updatedAt") -> Iterator[plex_client.TrackMeta]:
        for meta in plex_client.iter_tracks(
                connection.client, section, progress=progress,
                since=since, since_field=since_field):
            yield self._meta(connection, meta)

    def section_track_count(self, connection: ProviderConnection, section: str) -> int:
        return plex_client.section_track_count(connection.client, section)

    def list_playlists(self, connection: ProviderConnection) -> list[plex_client.PlaylistMeta]:
        sid = connection.source_id
        out = []
        for playlist in plex_client.list_playlists(connection.client):
            out.append(replace(
                playlist,
                plex_id=track_key(sid, playlist.plex_id),
                item_keys=[track_key(sid, key) for key in playlist.item_keys],
                source_id=sid,
                remote_id=str(playlist.plex_id),
            ))
        return out

    def download_track(self, connection: ProviderConnection, track: dict,
                       dest_path: str, progress=None) -> str:
        return plex_client.download_part(connection.client, track["part_key"], dest_path,
                                         progress=progress)

    def download_image(self, connection: ProviderConnection, image_key: str,
                       dest_path: str, width: int = 600, height: int = 600) -> str:
        return plex_client.download_image(connection.client, image_key, dest_path,
                                          width=width, height=height)

    def stream_url(self, connection: ProviderConnection, track: dict) -> str:
        return plex_client.stream_url(connection.client, track["part_key"])

    def stream_response(self, connection: ProviderConnection, track: dict,
                        headers: dict[str, str] | None = None,
                        stream: bool = True) -> requests.Response:
        url = self.stream_url(connection, track)
        return connection.client._session.get(url, headers=headers or {}, stream=stream,
                                              timeout=(10, None))

    def scrobble(self, connection: ProviderConnection, track: dict) -> None:
        plex_client.scrobble(connection.client, _remote_id(track))

    def set_rating(self, connection: ProviderConnection, track: dict,
                   rating: float) -> None:
        plex_client.set_rating(connection.client, _remote_id(track), rating)


@dataclass
class JellyfinClient:
    baseurl: str
    token: str
    user_id: str
    device_id: str
    session: requests.Session = field(default_factory=requests.Session)

    def __post_init__(self):
        self.baseurl = self.baseurl.rstrip("/")
        auth = (
            f'MediaBrowser Client="iAmped", Device="iAmped", '
            f'DeviceId="{self.device_id}", Version="{__version__}", '
            f'Token="{self.token}"')
        self.session.headers.update({"Authorization": auth, "X-Emby-Token": self.token})

    def url(self, path: str) -> str:
        return f"{self.baseurl}/{path.lstrip('/')}"

    def get_json(self, path: str, **params) -> dict:
        response = self.session.get(self.url(path), params=params or None, timeout=30)
        response.raise_for_status()
        return response.json()


class JellyfinProvider(MediaProvider):
    kind = "jellyfin"
    display_name = "Jellyfin"
    _capabilities = ProviderCapabilities(
        playlists=True, original_download=True, artwork=True,
        play_state_write=True, rating_write=False, incremental_refresh=False,
        sonic_radio=False, video=False, native_playback=True)

    @staticmethod
    def _device_id(source: dict[str, Any]) -> str:
        return str(source.get("device_id") or f"iamped-{uuid.uuid4().hex}")

    def connect(self, source: dict[str, Any]) -> ProviderConnection:
        baseurl = str(source.get("baseurl") or "").strip().rstrip("/")
        if not baseurl:
            raise ProviderError("Enter a Jellyfin server URL.")
        device_id = self._device_id(source)
        token = str(source.get("token") or "").strip()
        user_id = str(source.get("user_id") or "").strip()
        if not token:
            username = str(source.get("username") or "").strip()
            password = str(source.get("password") or "")
            if not username or not password:
                raise ProviderError("Enter a Jellyfin access token, or a username and password.")
            auth = (
                f'MediaBrowser Client="iAmped", Device="iAmped", '
                f'DeviceId="{device_id}", Version="{__version__}"')
            response = requests.post(
                f"{baseurl}/Users/AuthenticateByName",
                json={"Username": username, "Pw": password},
                headers={"Authorization": auth}, timeout=30)
            try:
                response.raise_for_status()
                payload = response.json()
                token = str(payload.get("AccessToken") or "")
                user_id = str((payload.get("User") or {}).get("Id") or "")
            except (requests.RequestException, ValueError) as exc:
                raise ProviderError(f"Could not sign in to Jellyfin: {exc}") from exc
        if not token:
            raise ProviderError("Jellyfin did not return an access token.")
        client = JellyfinClient(baseurl, token, user_id, device_id)
        try:
            me = client.get_json("/Users/Me")
        except requests.RequestException as exc:
            raise ProviderError(f"Could not connect to Jellyfin: {exc}") from exc
        client.user_id = str(me.get("Id") or client.user_id)
        if not client.user_id:
            raise ProviderError("Jellyfin did not identify the signed-in user.")
        source = dict(source)
        source.update({"baseurl": baseurl, "token": token, "user_id": client.user_id,
                       "device_id": device_id, "server_id": str(me.get("ServerId") or "")})
        return ProviderConnection(self, client, source)

    def server_info(self, connection: ProviderConnection) -> dict[str, str]:
        client: JellyfinClient = connection.client
        try:
            info = client.get_json("/System/Info")
        except requests.RequestException:
            info = client.get_json("/System/Info/Public")
        return {"name": str(info.get("ServerName") or info.get("Name") or "Jellyfin"),
                "version": str(info.get("Version") or ""), "platform": "Jellyfin"}

    def music_sections(self, connection: ProviderConnection) -> list[dict[str, str]]:
        client: JellyfinClient = connection.client
        data = client.get_json(f"/Users/{client.user_id}/Views")
        return [{"id": str(item["Id"]), "name": str(item.get("Name") or "Music")}
                for item in data.get("Items", [])
                if str(item.get("CollectionType") or "").lower() == "music"]

    @staticmethod
    def _ticks_to_ms(value: Any) -> int:
        try:
            return int(int(value or 0) / 10_000)
        except (TypeError, ValueError):
            return 0

    def _meta(self, connection: ProviderConnection, item: dict) -> plex_client.TrackMeta | None:
        remote = str(item.get("Id") or "")
        if not remote:
            return None
        source = connection.source_id
        media = (item.get("MediaSources") or [{}])[0] or {}
        streams = media.get("MediaStreams") or []
        audio = next((s for s in streams if s.get("Type") == "Audio"), {}) or {}
        user = item.get("UserData") or {}
        artists = item.get("Artists") or []
        album_artist = item.get("AlbumArtist") or (artists[0] if artists else "Unknown Artist")
        image_id = str(item.get("AlbumId") or item.get("PrimaryImageItemId") or remote)
        return plex_client.TrackMeta(
            rating_key=track_key(source, remote),
            title=str(item.get("Name") or "Unknown"),
            artist=str(artists[0] if artists else item.get("Artist") or "Unknown Artist"),
            album=str(item.get("Album") or "Unknown Album"),
            album_artist=str(album_artist or "Unknown Artist"),
            genre=", ".join(str(g) for g in (item.get("Genres") or [])),
            year=item.get("ProductionYear"),
            track_number=item.get("IndexNumber"),
            disc_number=item.get("ParentIndexNumber"),
            duration_ms=self._ticks_to_ms(item.get("RunTimeTicks")),
            view_count=int(user.get("PlayCount") or 0),
            user_rating=user.get("Rating"),
            last_viewed_at=_iso_to_epoch(user.get("LastPlayedDate")),
            container=str(media.get("Container") or item.get("Container") or "").lower(),
            codec=str(audio.get("Codec") or "").lower(),
            bitrate=_int_or_none(media.get("Bitrate") or audio.get("BitRate"), divide=1000),
            file_size=int(media.get("Size") or item.get("Size") or 0),
            part_key=remote,
            server_file=str(item.get("Path") or ""),
            album_key=str(item.get("AlbumId") or ""),
            album_thumb=f"jellyfin:{image_id}" if image_id else "",
            artist_thumb="",
            source_id=source,
            remote_id=remote,
        )

    def iter_tracks(self, connection: ProviderConnection, section: str,
                    progress=None, since: int | None = None,
                    since_field: str = "updatedAt") -> Iterator[plex_client.TrackMeta]:
        del since, since_field  # Jellyfin's query semantics vary by server version.
        client: JellyfinClient = connection.client
        start, total, limit = 0, None, 500
        fields = ",".join([
            "MediaSources", "UserData", "Genres", "AlbumArtists", "Artists",
            "AlbumArtist", "PrimaryImageAspectRatio", "DateCreated",
        ])
        while total is None or start < total:
            data = client.get_json(
                f"/Users/{client.user_id}/Items", ParentId=section,
                IncludeItemTypes="Audio", Recursive="true", Fields=fields,
                EnableUserData="true", StartIndex=start, Limit=limit)
            items = data.get("Items") or []
            total = int(data.get("TotalRecordCount") or len(items))
            for item in items:
                meta = self._meta(connection, item)
                if meta:
                    yield meta
            start += len(items)
            if progress:
                progress(start, total)
            if not items:
                break

    def section_track_count(self, connection: ProviderConnection, section: str) -> int:
        client: JellyfinClient = connection.client
        data = client.get_json(f"/Users/{client.user_id}/Items", ParentId=section,
                               IncludeItemTypes="Audio", Recursive="true", Limit=1)
        return int(data.get("TotalRecordCount") or 0)

    def list_playlists(self, connection: ProviderConnection) -> list[plex_client.PlaylistMeta]:
        client: JellyfinClient = connection.client
        source = connection.source_id
        data = client.get_json(f"/Users/{client.user_id}/Items",
                               IncludeItemTypes="Playlist", Recursive="true", Limit=500)
        playlists = []
        for item in data.get("Items") or []:
            remote = str(item.get("Id") or "")
            if not remote:
                continue
            listing = client.get_json(f"/Playlists/{remote}/Items", UserId=client.user_id,
                                      Limit=10_000, Fields="MediaSources")
            keys = [track_key(source, str(track.get("Id")))
                    for track in listing.get("Items") or [] if track.get("Id")]
            playlists.append(plex_client.PlaylistMeta(
                plex_id=track_key(source, remote), title=str(item.get("Name") or "Playlist"),
                smart=False, duration_ms=self._ticks_to_ms(item.get("RunTimeTicks")),
                item_keys=keys, source_id=source, remote_id=remote))
        return playlists

    def _download_url(self, connection: ProviderConnection, track: dict) -> str:
        client: JellyfinClient = connection.client
        return client.url(f"/Items/{quote(_remote_id(track), safe='')}/Download")

    def download_track(self, connection: ProviderConnection, track: dict,
                       dest_path: str, progress=None) -> str:
        client: JellyfinClient = connection.client
        tmp = dest_path + ".part"
        have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        response = client.session.get(self._download_url(connection, track), headers=headers,
                                      stream=True, timeout=60)
        return _download_response(response, dest_path, progress)

    def download_image(self, connection: ProviderConnection, image_key: str,
                       dest_path: str, width: int = 600, height: int = 600) -> str:
        client: JellyfinClient = connection.client
        item_id = image_key.split(":", 1)[-1]
        response = client.session.get(
            client.url(f"/Items/{quote(item_id, safe='')}/Images/Primary"),
            params={"maxWidth": int(width), "maxHeight": int(height), "quality": 90},
            stream=True, timeout=30)
        return _download_response(response, dest_path)

    def stream_url(self, connection: ProviderConnection, track: dict) -> str:
        # mpv/ffmpeg need a self-contained URL. It is never returned through
        # an iAmped API response, and browser playback is proxied below.
        client: JellyfinClient = connection.client
        return self._download_url(connection, track) + "?api_key=" + quote(client.token, safe="")

    def stream_response(self, connection: ProviderConnection, track: dict,
                        headers: dict[str, str] | None = None,
                        stream: bool = True) -> requests.Response:
        client: JellyfinClient = connection.client
        return client.session.get(self._download_url(connection, track), headers=headers or {},
                                  stream=stream, timeout=(10, None))

    def scrobble(self, connection: ProviderConnection, track: dict) -> None:
        client: JellyfinClient = connection.client
        response = client.session.post(
            client.url(f"/Users/{client.user_id}/PlayedItems/{quote(_remote_id(track), safe='')}"),
            timeout=30)
        response.raise_for_status()


@dataclass
class KodiClient:
    baseurl: str
    username: str = ""
    password: str = ""
    session: requests.Session = field(default_factory=requests.Session)
    _request_id: int = 0
    download_available: bool | None = None

    def __post_init__(self):
        self.baseurl = self.baseurl.rstrip("/")
        if self.username:
            self.session.auth = (self.username, self.password)

    @property
    def endpoint(self) -> str:
        return self.baseurl if self.baseurl.endswith("/jsonrpc") else self.baseurl + "/jsonrpc"

    def rpc(self, method: str, params: dict | None = None) -> Any:
        self._request_id += 1
        response = self.session.post(
            self.endpoint, json={"jsonrpc": "2.0", "id": self._request_id,
                                  "method": method, "params": params or {}},
            timeout=30)
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise ProviderError(f"Kodi {method}: {payload['error'].get('message', 'request failed')}")
        return payload.get("result")


class KodiProvider(MediaProvider):
    kind = "kodi"
    display_name = "Kodi"
    _capabilities = ProviderCapabilities(
        playlists=False, original_download=True, artwork=True,
        play_state_write=False, rating_write=False, incremental_refresh=False,
        sonic_radio=False, video=False, native_playback=False)

    def capabilities(self, connection: ProviderConnection) -> ProviderCapabilities:
        client: KodiClient = connection.client
        # ``None`` means there were no songs to probe at connect time. A false
        # result comes from Files.Download and lets callers disable a futile
        # sync before copying anything to a device.
        return replace(self._capabilities,
                       original_download=client.download_available is not False)

    def connect(self, source: dict[str, Any]) -> ProviderConnection:
        baseurl = str(source.get("baseurl") or "").strip()
        if not baseurl:
            raise ProviderError("Enter a Kodi web-server URL.")
        client = KodiClient(baseurl, str(source.get("username") or ""),
                            str(source.get("password") or ""))
        try:
            client.rpc("JSONRPC.Version")
        except (requests.RequestException, ValueError, ProviderError) as exc:
            raise ProviderError(f"Could not connect to Kodi JSON-RPC: {exc}") from exc
        return ProviderConnection(self, client, dict(source, baseurl=client.baseurl))

    def server_info(self, connection: ProviderConnection) -> dict[str, str]:
        client: KodiClient = connection.client
        result = client.rpc("Application.GetProperties", {"properties": ["name", "version"]}) or {}
        version = result.get("version") or {}
        version_s = ".".join(str(version[k]) for k in ("major", "minor") if k in version)
        return {"name": str(result.get("name") or "Kodi"), "version": version_s,
                "platform": "Kodi"}

    def music_sections(self, connection: ProviderConnection) -> list[dict[str, str]]:
        del connection
        # Kodi exposes one music library through JSON-RPC rather than named
        # server sections.  Keeping this synthetic section avoids a special UI.
        return [{"id": "music", "name": "Music"}]

    def _meta(self, connection: ProviderConnection, item: dict) -> plex_client.TrackMeta | None:
        remote = str(item.get("songid") or "")
        file_path = str(item.get("file") or "")
        if not remote or not file_path:
            return None
        source = connection.source_id
        artists = item.get("artist") or []
        album_artists = item.get("albumartist") or artists
        duration_ms = int(float(item.get("duration") or 0) * 1000)
        # Kodi's JSON-RPC catalog does not guarantee byte sizes.  A conservative
        # duration estimate lets the capacity planner work; the sync still uses
        # the actual downloaded file and reports any source download failure.
        estimate = int(duration_ms / 1000 * 320_000 / 8) if duration_ms else 0
        thumb = str(item.get("thumbnail") or (item.get("art") or {}).get("thumb") or "")
        return plex_client.TrackMeta(
            rating_key=track_key(source, remote), title=str(item.get("label") or "Unknown"),
            artist=str(artists[0] if artists else "Unknown Artist"),
            album=str(item.get("album") or "Unknown Album"),
            album_artist=str(album_artists[0] if album_artists else "Unknown Artist"),
            genre=", ".join(str(g) for g in (item.get("genre") or [])),
            year=_int_or_none(item.get("year")), track_number=_int_or_none(item.get("track")),
            disc_number=_int_or_none(item.get("disc")), duration_ms=duration_ms,
            view_count=int(item.get("playcount") or 0), user_rating=item.get("rating"),
            last_viewed_at=_kodi_last_played(item.get("lastplayed")),
            container=os.path.splitext(file_path)[1].lstrip(".").lower(), codec="",
            bitrate=None, file_size=estimate, part_key=file_path, server_file=file_path,
            album_key=str(item.get("albumid") or ""), album_thumb=thumb,
            artist_thumb="", source_id=source, remote_id=remote)

    _SONG_FIELDS = ["artist", "album", "albumartist", "genre", "year", "track", "disc",
                    "duration", "playcount", "rating", "lastplayed", "file", "thumbnail", "art"]

    def iter_tracks(self, connection: ProviderConnection, section: str,
                    progress=None, since: int | None = None,
                    since_field: str = "updatedAt") -> Iterator[plex_client.TrackMeta]:
        del section, since, since_field
        client: KodiClient = connection.client
        start, total, limit = 0, None, 500
        while total is None or start < total:
            result = client.rpc("AudioLibrary.GetSongs", {
                "properties": self._SONG_FIELDS,
                "limits": {"start": start, "end": start + limit - 1},
            }) or {}
            songs = result.get("songs") or []
            limits = result.get("limits") or {}
            total = int(limits.get("total") or len(songs))
            for item in songs:
                meta = self._meta(connection, item)
                if meta:
                    if client.download_available is None:
                        try:
                            self._download_url(connection, {
                                "part_key": meta.part_key,
                                "remote_id": meta.remote_id,
                            })
                        except ProviderError:
                            client.download_available = False
                    yield meta
            start += len(songs)
            if progress:
                progress(start, total)
            if not songs:
                break

    def section_track_count(self, connection: ProviderConnection, section: str) -> int:
        del section
        result = connection.client.rpc("AudioLibrary.GetSongs", {
            "properties": [], "limits": {"start": 0, "end": 0}}) or {}
        return int((result.get("limits") or {}).get("total") or 0)

    def _download_url(self, connection: ProviderConnection, track: dict) -> str:
        client: KodiClient = connection.client
        path = str(track.get("part_key") or "")
        if not path:
            client.download_available = False
            raise ProviderError(
                "Kodi returned music metadata without a file path.")
        # Kodi's HTTP web server exposes configured music sources through its
        # VFS endpoint.  Files.Download is not part of its current JSON-RPC
        # API, so do not rely on a non-existent RPC method here.
        root = client.baseurl.rsplit("/jsonrpc", 1)[0]
        path = root + "/vfs/" + quote(path, safe="")
        client.download_available = True
        return path

    def download_track(self, connection: ProviderConnection, track: dict,
                       dest_path: str, progress=None) -> str:
        client: KodiClient = connection.client
        tmp = dest_path + ".part"
        have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        response = client.session.get(self._download_url(connection, track), headers=headers,
                                      stream=True, timeout=60)
        return _download_response(response, dest_path, progress)

    def download_image(self, connection: ProviderConnection, image_key: str,
                       dest_path: str, width: int = 600, height: int = 600) -> str:
        del width, height
        client: KodiClient = connection.client
        if not image_key:
            raise ProviderError("Kodi did not provide album artwork for this item.")
        root = client.baseurl.rsplit("/jsonrpc", 1)[0]
        response = client.session.get(root + "/image/" + quote(image_key, safe=""),
                                      stream=True, timeout=30)
        return _download_response(response, dest_path)

    def stream_url(self, connection: ProviderConnection, track: dict) -> str:
        return self._download_url(connection, track)

    def stream_response(self, connection: ProviderConnection, track: dict,
                        headers: dict[str, str] | None = None,
                        stream: bool = True) -> requests.Response:
        client: KodiClient = connection.client
        return client.session.get(self._download_url(connection, track), headers=headers or {},
                                  stream=stream, timeout=(10, None))


def _int_or_none(value: Any, divide: int = 1) -> int | None:
    try:
        return int(int(value) / divide)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _iso_to_epoch(value: Any) -> int | None:
    if not value:
        return None
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def _kodi_last_played(value: Any) -> int | None:
    if not value or str(value).startswith("0000-"):
        return None
    try:
        from datetime import datetime
        return int(datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").timestamp())
    except (TypeError, ValueError):
        return None


_PROVIDERS: dict[str, MediaProvider] = {
    "plex": PlexProvider(),
    "jellyfin": JellyfinProvider(),
    "kodi": KodiProvider(),
}


def kinds() -> list[dict[str, str]]:
    return [{"id": provider.kind, "name": provider.display_name}
            for provider in _PROVIDERS.values()]


def provider_for(kind: str) -> MediaProvider:
    try:
        return _PROVIDERS[str(kind).lower()]
    except KeyError as exc:
        raise ProviderError(f"Unsupported media server type: {kind}") from exc


def connect(source: dict[str, Any]) -> ProviderConnection:
    return provider_for(str(source.get("kind") or "plex")).connect(source)


def normalize(connection: ProviderConnection | Any) -> ProviderConnection:
    """Accept a raw Plex object too, preserving older unit-test seams."""
    if isinstance(connection, ProviderConnection):
        return connection
    return ProviderConnection(_PROVIDERS["plex"], connection, {"id": "plex", "kind": "plex"})


def source_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    kind = str(payload.get("kind") or "plex").lower()
    provider_for(kind)  # validate before persisting anything
    source_id = str(payload.get("id") or "").strip()
    if not source_id:
        if kind == "plex":
            source_id = "plex"
        else:
            source_id = f"{kind}-{uuid.uuid4().hex[:12]}"
    return {
        "id": _safe_id(source_id, kind), "kind": kind,
        "name": str(payload.get("name") or "").strip(),
        "baseurl": str(payload.get("baseurl") or "").strip(),
        "token": str(payload.get("token") or "").strip(),
        "username": str(payload.get("username") or "").strip(),
        "password": str(payload.get("password") or ""),
        "user_id": str(payload.get("user_id") or "").strip(),
        "device_id": str(payload.get("device_id") or "").strip(),
    }


def public_source(source: dict[str, Any], active: bool = False) -> dict[str, Any]:
    provider = provider_for(str(source.get("kind") or "plex"))
    return {"id": source.get("id"), "kind": provider.kind,
            "name": source.get("name") or provider.display_name,
            "baseurl": source.get("baseurl") or "", "active": active,
            "configured": bool(source.get("baseurl") and (
                source.get("token") or source.get("username") or provider.kind == "kodi")),
            "capabilities": provider._capabilities.as_dict()}
