from .base import (free_bytes, list_volumes, target_format, transcode,
                   transcode_to_aac, transcode_to_mp3)
from .massstorage import MassStorageBackend
from .itunesdb import ITunesDBBackend

BACKENDS = {
    "massstorage": MassStorageBackend,
    "ipod": ITunesDBBackend,
}

__all__ = ["BACKENDS", "MassStorageBackend", "ITunesDBBackend",
           "free_bytes", "list_volumes", "target_format", "transcode",
           "transcode_to_aac", "transcode_to_mp3"]
