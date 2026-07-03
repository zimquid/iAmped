"""Podcast subscriptions: RSS feeds → downloaded episodes → the device.

Plex has no first-class podcast support, so subscriptions are plain RSS URLs
stored in the local library database. Episodes download into the same cache
pool as music and ride the normal device backends (iTunesDB / mass storage),
tagged with genre "Podcast", album = show, and grouped into a per-show
playlist — browsable on every player iAmped supports, including a 2005 nano.
"""
from __future__ import annotations

import email.utils
import hashlib
import os
import re
import xml.etree.ElementTree as ET

from . import config

USER_AGENT = "iAmped/podcast"
_AUDIO_EXT = re.compile(r"\.(mp3|m4a|aac|ogg|opus|wav)(\?|$)", re.I)


def _text(el) -> str:
    return (el.text or "").strip() if el is not None else ""


def _find(parent, *names):
    """First matching child by local tag name, namespace-agnostic (feeds mix
    plain RSS, itunes: and atom: freely)."""
    want = {n.lower() for n in names}
    for child in parent:
        tag = child.tag.rsplit("}", 1)[-1].lower()
        if tag in want:
            return child
    return None


def _duration_ms(raw: str) -> int:
    raw = (raw or "").strip()
    if not raw:
        return 0
    if raw.isdigit():
        return int(raw) * 1000
    parts = raw.split(":")
    try:
        secs = 0
        for p in parts:
            secs = secs * 60 + int(float(p))
        return secs * 1000
    except ValueError:
        return 0


def _pub_epoch(raw: str) -> int:
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        return int(dt.timestamp()) if dt else 0
    except (TypeError, ValueError):
        return 0


def parse_feed(data: bytes) -> dict:
    """Parse an RSS feed into {title, author, image, episodes:[…]}."""
    root = ET.fromstring(data)
    channel = _find(root, "channel")
    if channel is None:
        channel = root
    title = _text(_find(channel, "title")) or "Untitled podcast"
    author = _text(_find(channel, "author")) or ""
    image = ""
    img = _find(channel, "image")
    if img is not None:
        image = img.get("href") or _text(_find(img, "url")) or ""

    episodes = []
    for item in channel:
        if item.tag.rsplit("}", 1)[-1].lower() != "item":
            continue
        enclosure = None
        for child in item:
            if child.tag.rsplit("}", 1)[-1].lower() == "enclosure":
                etype = (child.get("type") or "").lower()
                eurl = child.get("url") or ""
                if etype.startswith("audio/") or (
                        not etype and _AUDIO_EXT.search(eurl)):
                    enclosure = child
                    break
        if enclosure is None:
            continue
        guid = _text(_find(item, "guid")) or enclosure.get("url") or ""
        if not guid:
            continue
        episodes.append({
            "guid": guid,
            "title": _text(_find(item, "title")) or "Untitled episode",
            "url": enclosure.get("url") or "",
            "size": int(enclosure.get("length") or 0),
            "pub_date": _pub_epoch(_text(_find(item, "pubdate"))),
            "duration_ms": _duration_ms(_text(_find(item, "duration"))),
        })
    return {"title": title, "author": author, "image": image,
            "episodes": episodes}


def fetch_feed(url: str, timeout: int = 30) -> dict:
    import requests
    r = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    return parse_feed(r.content)


def refresh(lib, url: str, added: int) -> dict:
    """Fetch one feed and fold it into the library. Returns the subscription
    row info plus how many new episodes appeared."""
    feed = fetch_feed(url)
    pid = lib.add_podcast(url, feed["title"], feed["author"], feed["image"],
                          added)
    fresh = lib.upsert_episodes(pid, feed["episodes"])
    return {"id": pid, "title": feed["title"], "new_episodes": fresh,
            "total_episodes": len(feed["episodes"])}


def _episode_ext(url: str) -> str:
    m = _AUDIO_EXT.search(url or "")
    return f".{m.group(1).lower()}" if m else ".mp3"


def episode_path(episode: dict) -> str:
    """Deterministic cache location for an episode's audio."""
    cache = config.load()["cache_dir"]
    digest = hashlib.sha1(episode["guid"].encode()).hexdigest()[:16]
    return os.path.join(cache, "podcasts",
                        f"p{episode['podcast_id']}-{digest}"
                        + _episode_ext(episode["url"]))


def download_episode(lib, episode: dict, timeout: int = 60) -> str:
    """Download an episode into the cache (idempotent). Returns the path."""
    import requests
    dest = episode.get("downloaded_path") or episode_path(episode)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    with requests.get(episode["url"], stream=True, timeout=timeout,
                      headers={"User-Agent": USER_AGENT}) as r:
        r.raise_for_status()
        with open(tmp, "wb") as fh:
            for block in r.iter_content(chunk_size=1 << 20):
                if block:
                    fh.write(block)
    os.replace(tmp, dest)
    lib.set_episode_downloaded(episode["id"], dest, os.path.getsize(dest))
    return dest


def episode_track(show: dict, episode: dict, n: int) -> dict:
    """Track-shaped dict for the device backends. Newest episode is track 1,
    like the iPod's own Podcasts ordering."""
    return {
        "rating_key": f"podcast:{episode['id']}",
        "title": episode["title"],
        "artist": show["title"],
        "album": show["title"],
        "album_artist": show.get("author") or show["title"],
        "genre": "Podcast",
        "duration_ms": episode.get("duration_ms") or 0,
        "year": 0,
        "track_number": n,
        "disc_number": 1,
        "view_count": 0,
        "user_rating": None,
        "_size": episode.get("size") or 0,
        "_sync_signature": f"podcast:{episode['guid']}",
    }
