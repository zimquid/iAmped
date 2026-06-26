from .base import (free_bytes, list_volumes, target_format, total_bytes,
                   transcode, transcode_to_aac, transcode_to_mp3)
from .massstorage import MassStorageBackend
from .itunesdb import ITunesDBBackend
from .mtp import MTPBackend

BACKENDS = {
    "massstorage": MassStorageBackend,
    "ipod": ITunesDBBackend,
    "mtp": MTPBackend,
}

__all__ = ["BACKENDS", "MassStorageBackend", "ITunesDBBackend", "MTPBackend",
           "free_bytes", "list_volumes", "target_format", "total_bytes",
           "transcode", "transcode_to_aac", "transcode_to_mp3"]
