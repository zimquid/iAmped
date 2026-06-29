"""Album-art acquisition, normalization, and audio-tag embedding."""
from __future__ import annotations

import hashlib
import io
import os
import shutil
from pathlib import Path

from PIL import Image, ImageOps

from . import config, plex_client


def album_identity(track: dict) -> str:
    key = str(track.get("album_key") or "").strip()
    raw = f"plex:{key}" if key else "\0".join((
        track.get("album_artist") or track.get("artist") or "",
        track.get("album") or "",
    ))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def cache_path(track: dict) -> str:
    return str(Path(config.load()["cache_dir"]) / "artwork" /
               f"{album_identity(track)}.jpg")


def materialize(server, track: dict) -> str | None:
    thumb = track.get("album_thumb")
    if not thumb:
        return None
    path = cache_path(track)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        try:
            with Image.open(path) as existing:
                existing.verify()
            return path
        except OSError:
            try:
                os.unlink(path)
            except OSError:
                pass
    plex_client.download_image(server, thumb, path)
    try:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            image.save(path, "JPEG", quality=92, optimize=True)
    except OSError:
        try:
            os.unlink(path)
        except OSError:
            pass
        return None
    return path


def jpeg_bytes(path: str, size: int | None = None) -> bytes:
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        if size:
            image = ImageOps.fit(
                image, (size, size), method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5))
        out = io.BytesIO()
        image.save(out, "JPEG", quality=90, optimize=True)
        return out.getvalue()


def write_cover_jpg(art_path: str, album_dir: str) -> str:
    os.makedirs(album_dir, exist_ok=True)
    dest = os.path.join(album_dir, "cover.jpg")
    if not os.path.exists(dest):
        shutil.copyfile(art_path, dest)
    return dest


def embed(audio_path: str, art_path: str) -> bool:
    """Embed JPEG cover art into common portable-player audio formats."""
    data = jpeg_bytes(art_path, 1000)
    ext = Path(audio_path).suffix.lower()
    try:
        if ext == ".mp3":
            from mutagen.id3 import APIC, ID3, ID3NoHeaderError
            try:
                tags = ID3(audio_path)
            except ID3NoHeaderError:
                tags = ID3()
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3,
                          desc="Cover", data=data))
            tags.save(audio_path, v2_version=3)
            return True
        if ext in (".m4a", ".mp4", ".m4b", ".aac"):
            from mutagen.mp4 import MP4, MP4Cover
            audio = MP4(audio_path)
            audio["covr"] = [MP4Cover(data, imageformat=MP4Cover.FORMAT_JPEG)]
            audio.save()
            return True
        if ext == ".flac":
            from mutagen.flac import FLAC, Picture
            audio = FLAC(audio_path)
            picture = Picture()
            picture.type = 3
            picture.mime = "image/jpeg"
            picture.desc = "Cover"
            picture.data = data
            with Image.open(io.BytesIO(data)) as image:
                picture.width, picture.height = image.size
                picture.depth = 24
            audio.clear_pictures()
            audio.add_picture(picture)
            audio.save()
            return True
        if ext in (".ogg", ".oga", ".opus"):
            import base64
            from mutagen import File
            from mutagen.flac import Picture
            picture = Picture()
            picture.type = 3
            picture.mime = "image/jpeg"
            picture.desc = "Cover"
            picture.data = data
            audio = File(audio_path)
            audio["metadata_block_picture"] = [
                base64.b64encode(picture.write()).decode("ascii")]
            audio.save()
            return True
    except Exception:
        return False
    return False
