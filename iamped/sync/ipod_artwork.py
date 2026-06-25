"""Native click-wheel iPod ArtworkDB and ITHMB writer.

Implements the libgpod ArtworkDB structures and RGB565 thumbnail formats used
by supported photo/video iPods. Existing artwork records and ITHMB content are
preserved; new iAmped records are appended.
"""
from __future__ import annotations

import os
import shutil
import struct
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps


MAC_EPOCH_OFFSET = 2082844800


@dataclass(frozen=True)
class Format:
    format_id: int
    width: int
    height: int

    @property
    def size(self) -> int:
        return self.width * self.height * 2


CLASSIC_NANO3 = (
    Format(1061, 56, 56),
    Format(1055, 128, 128),
    Format(1068, 128, 128),
    Format(1060, 320, 320),
)
NANO4 = (
    Format(1055, 128, 128), Format(1068, 128, 128),
    Format(1071, 240, 240), Format(1074, 50, 50),
    Format(1078, 80, 80), Format(1084, 240, 240),
)
VIDEO = (Format(1028, 100, 100), Format(1029, 200, 200))
PHOTO = (Format(1017, 56, 56), Format(1016, 140, 140))
NANO12 = (Format(1031, 42, 42), Format(1027, 100, 100))


def formats_for(generation: str) -> tuple[Format, ...]:
    low = (generation or "").lower()
    if "nano (4th" in low:
        return NANO4
    if "nano (3rd" in low or "classic" in low:
        return CLASSIC_NANO3
    if "5th generation" in low or "5.5 generation" in low:
        return VIDEO
    if "photo" in low or "4th generation" in low:
        return PHOTO
    if "nano (1st" in low or "nano (2nd" in low:
        return NANO12
    return ()


def _header(magic: bytes, size: int) -> bytearray:
    value = bytearray(size)
    value[:4] = magic
    struct.pack_into("<I", value, 4, size)
    return value


def _mhod_filename(filename: str) -> bytes:
    data = filename.encode("utf-16-le")
    padding = (-len(data)) % 4
    value = bytearray(36 + len(data) + padding)
    value[:4] = b"mhod"
    struct.pack_into("<I", value, 4, 24)
    struct.pack_into("<I", value, 8, len(value))
    struct.pack_into("<H", value, 12, 3)
    value[15] = padding
    struct.pack_into("<I", value, 24, len(data))
    value[28] = 2
    value[36:36 + len(data)] = data
    return bytes(value)


def _mhni(fmt: Format, offset: int, filename: str) -> bytes:
    child = _mhod_filename(filename)
    value = _header(b"mhni", 0x4C)
    struct.pack_into("<I", value, 8, len(value) + len(child))
    struct.pack_into("<I", value, 12, 1)
    struct.pack_into("<I", value, 16, fmt.format_id)
    struct.pack_into("<I", value, 20, offset)
    struct.pack_into("<I", value, 24, fmt.size)
    struct.pack_into("<H", value, 32, fmt.height)
    struct.pack_into("<H", value, 34, fmt.width)
    return bytes(value) + child


def _location_mhod(fmt: Format, offset: int, filename: str) -> bytes:
    child = _mhni(fmt, offset, filename)
    value = _header(b"mhod", 24)
    struct.pack_into("<I", value, 8, 24 + len(child))
    struct.pack_into("<H", value, 12, 2)
    return bytes(value) + child


def _mhii(image_id: int, song_id: int, original_size: int,
          thumbs: list[tuple[Format, int, str]]) -> bytes:
    children = b"".join(_location_mhod(*thumb) for thumb in thumbs)
    value = _header(b"mhii", 0x98)
    struct.pack_into("<I", value, 8, len(value) + len(children))
    struct.pack_into("<I", value, 12, len(thumbs))
    struct.pack_into("<I", value, 16, image_id)
    struct.pack_into("<Q", value, 20, song_id)
    now = int(time.time()) + MAC_EPOCH_OFFSET
    struct.pack_into("<I", value, 40, now)
    struct.pack_into("<I", value, 44, now)
    struct.pack_into("<I", value, 48, original_size)
    return bytes(value) + children


def _mhsd(index: int, body: bytes) -> bytes:
    value = _header(b"mhsd", 0x60)
    struct.pack_into("<I", value, 8, len(value) + len(body))
    struct.pack_into("<H", value, 12, index)
    return bytes(value) + body


def _mhli(records: list[bytes]) -> bytes:
    value = _header(b"mhli", 0x5C)
    struct.pack_into("<I", value, 8, len(records))
    return bytes(value) + b"".join(records)


def _mhlf(formats: tuple[Format, ...]) -> bytes:
    records = []
    for fmt in formats:
        value = _header(b"mhif", 0x7C)
        struct.pack_into("<I", value, 8, 0x7C)
        struct.pack_into("<I", value, 16, fmt.format_id)
        struct.pack_into("<I", value, 20, fmt.size)
        records.append(bytes(value))
    header = _header(b"mhlf", 0x5C)
    struct.pack_into("<I", header, 8, len(records))
    return bytes(header) + b"".join(records)


def _empty_mhla() -> bytes:
    return bytes(_header(b"mhla", 0x5C))


def _parse_existing(path: Path) -> tuple[list[bytes], bytes | None, bytes | None, int]:
    if not path.exists():
        return [], None, None, 99
    data = path.read_bytes()
    if data[:4] != b"mhfd":
        return [], None, None, 99
    records: list[bytes] = []
    mhla = mhlf = None
    max_id = 99
    off = struct.unpack_from("<I", data, 4)[0]
    for _ in range(struct.unpack_from("<I", data, 20)[0]):
        if data[off:off + 4] != b"mhsd":
            break
        total = struct.unpack_from("<I", data, off + 8)[0]
        index = struct.unpack_from("<H", data, off + 12)[0]
        inner = off + struct.unpack_from("<I", data, off + 4)[0]
        if index == 1 and data[inner:inner + 4] == b"mhli":
            p = inner + struct.unpack_from("<I", data, inner + 4)[0]
            count = struct.unpack_from("<I", data, inner + 8)[0]
            for _ in range(count):
                if data[p:p + 4] != b"mhii":
                    break
                length = struct.unpack_from("<I", data, p + 8)[0]
                records.append(data[p:p + length])
                max_id = max(max_id, struct.unpack_from("<I", data, p + 16)[0])
                p += length
        elif index == 2:
            mhla = data[inner:off + total]
        elif index == 3:
            mhlf = data[inner:off + total]
        off += total
    return records, mhla, mhlf, max_id


def _rgb565(path: str, fmt: Format) -> bytes:
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        image = ImageOps.fit(
            image, (fmt.width, fmt.height), method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
        out = bytearray(fmt.size)
        pos = 0
        pixels = (image.get_flattened_data()
                  if hasattr(image, "get_flattened_data") else image.getdata())
        for red, green, blue in pixels:
            pixel = ((red >> 3) << 11) | ((green >> 2) << 5) | (blue >> 3)
            struct.pack_into("<H", out, pos, pixel)
            pos += 2
        return bytes(out)


def _db(records: list[bytes], mhla: bytes | None, mhlf: bytes | None,
        formats: tuple[Format, ...], next_id: int) -> bytes:
    datasets = [
        _mhsd(1, _mhli(records)),
        _mhsd(2, mhla or _empty_mhla()),
        _mhsd(3, mhlf or _mhlf(formats)),
    ]
    header = _header(b"mhfd", 0x84)
    total = len(header) + sum(map(len, datasets))
    struct.pack_into("<I", header, 8, total)
    struct.pack_into("<I", header, 16, 2)
    struct.pack_into("<I", header, 20, 3)
    struct.pack_into("<I", header, 28, next_id)
    header[48] = 2
    return bytes(header) + b"".join(datasets)


def validate(device_path: str, database: bytes | None = None) -> dict:
    """Validate ArtworkDB structure and all referenced ITHMB byte ranges."""
    directory = Path(device_path) / "iPod_Control" / "Artwork"
    data = database if database is not None else (directory / "ArtworkDB").read_bytes()
    if data[:4] != b"mhfd" or len(data) < 0x84:
        raise ValueError("not an ArtworkDB")
    if struct.unpack_from("<I", data, 8)[0] != len(data):
        raise ValueError("ArtworkDB total length mismatch")
    dataset_count = struct.unpack_from("<I", data, 20)[0]
    off = struct.unpack_from("<I", data, 4)[0]
    images = thumbs = 0
    for _ in range(dataset_count):
        if data[off:off + 4] != b"mhsd":
            raise ValueError(f"expected mhsd at {off}")
        total = struct.unpack_from("<I", data, off + 8)[0]
        index = struct.unpack_from("<H", data, off + 12)[0]
        end = off + total
        if total < 0x60 or end > len(data):
            raise ValueError("invalid mhsd length")
        inner = off + struct.unpack_from("<I", data, off + 4)[0]
        if index == 1:
            if data[inner:inner + 4] != b"mhli":
                raise ValueError("artwork list missing mhli")
            p = inner + struct.unpack_from("<I", data, inner + 4)[0]
            count = struct.unpack_from("<I", data, inner + 8)[0]
            for _ in range(count):
                if data[p:p + 4] != b"mhii":
                    raise ValueError("artwork list missing mhii")
                record_end = p + struct.unpack_from("<I", data, p + 8)[0]
                child_count = struct.unpack_from("<I", data, p + 12)[0]
                q = p + struct.unpack_from("<I", data, p + 4)[0]
                for _ in range(child_count):
                    if data[q:q + 4] != b"mhod":
                        raise ValueError("mhii missing location mhod")
                    mhod_end = q + struct.unpack_from("<I", data, q + 8)[0]
                    n = q + struct.unpack_from("<I", data, q + 4)[0]
                    if data[n:n + 4] != b"mhni":
                        raise ValueError("location mhod missing mhni")
                    fmt = struct.unpack_from("<I", data, n + 16)[0]
                    ithmb_offset = struct.unpack_from("<I", data, n + 20)[0]
                    image_size = struct.unpack_from("<I", data, n + 24)[0]
                    s = n + struct.unpack_from("<I", data, n + 4)[0]
                    strlen = struct.unpack_from("<I", data, s + 24)[0]
                    filename = data[s + 36:s + 36 + strlen].decode("utf-16-le")
                    path = directory / filename.lstrip(":").replace(":", os.sep)
                    if not path.exists() or path.stat().st_size < ithmb_offset + image_size:
                        raise ValueError(
                            f"F{fmt} thumbnail range is outside {path.name}"
                        )
                    thumbs += 1
                    q = mhod_end
                if q != record_end:
                    raise ValueError("mhii child lengths do not reach record end")
                images += 1
                p = record_end
        off = end
    if off != len(data):
        raise ValueError("ArtworkDB datasets do not reach end of file")
    return {"datasets": dataset_count, "images": images, "thumbnails": thumbs}


def write(device_path: str, generation: str, entries) -> dict:
    formats = formats_for(generation)
    if not formats:
        return {"artwork": 0, "formats": 0}

    directory = Path(device_path) / "iPod_Control" / "Artwork"
    directory.mkdir(parents=True, exist_ok=True)
    db_path = directory / "ArtworkDB"
    existing, mhla, mhlf, max_id = _parse_existing(db_path)

    handles = {}
    offsets = {}
    for fmt in formats:
        path = directory / f"F{fmt.format_id}_1.ithmb"
        handle = open(path, "ab")
        handles[fmt.format_id] = handle
        offsets[fmt.format_id] = handle.tell()

    record_by_image_id = {
        struct.unpack_from("<I", record, 16)[0]: record
        for record in existing if len(record) >= 0x98
    }
    existing_by_album = {}
    for entry in entries:
        image_id = int(entry.track.get("_artwork_id") or 0)
        if image_id and image_id in record_by_image_id:
            key = (
                (entry.track.get("album_artist") or entry.track.get("artist") or "").casefold(),
                (entry.track.get("album") or "").casefold(),
            )
            existing_by_album.setdefault(key, image_id)

    album_thumbs: dict[str, tuple[int, list[tuple[Format, int, str]], int]] = {}
    records = list(existing)
    try:
        for entry in entries:
            if not entry.art_path:
                continue
            album_key = str(entry.track.get("album_key") or entry.art_path)
            metadata_key = (
                (entry.track.get("album_artist") or entry.track.get("artist") or "").casefold(),
                (entry.track.get("album") or "").casefold(),
            )
            existing_id = existing_by_album.get(metadata_key)
            if existing_id:
                original = bytearray(record_by_image_id[existing_id])
                dbid = int(entry.track.get("dbid") or entry.track_id)
                struct.pack_into("<Q", original, 20, dbid)
                entry.track["_artwork_id"] = existing_id
                entry.track["_artwork_size"] = struct.unpack_from("<I", original, 48)[0]
                records.append(bytes(original))
                continue
            shared = album_thumbs.get(album_key)
            if shared is None:
                max_id += 1
                thumbs = []
                for fmt in formats:
                    offset = offsets[fmt.format_id]
                    pixels = _rgb565(entry.art_path, fmt)
                    handles[fmt.format_id].write(pixels)
                    offsets[fmt.format_id] += len(pixels)
                    thumbs.append((fmt, offset, f":F{fmt.format_id}_1.ithmb"))
                shared = (max_id, thumbs, os.path.getsize(entry.art_path))
                album_thumbs[album_key] = shared
            image_id, thumbs, original_size = shared
            dbid = int(entry.track.get("dbid") or entry.track_id)
            entry.track["_artwork_id"] = image_id
            entry.track["_artwork_size"] = original_size
            records.append(_mhii(image_id, dbid, original_size, thumbs))
    finally:
        for handle in handles.values():
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()

    if not album_thumbs:
        return {"artwork": 0, "formats": len(formats)}
    database = _db(records, mhla, mhlf, formats, max_id + 1)
    if db_path.exists():
        shutil.copyfile(db_path, str(db_path) + ".iamped.bak")
    tmp = Path(str(db_path) + ".iamped.tmp")
    with open(tmp, "wb") as fh:
        fh.write(database)
        fh.flush()
        os.fsync(fh.fileno())
    validate(device_path, database)
    os.replace(tmp, db_path)
    return {"artwork": len(album_thumbs), "formats": len(formats)}
