from .base import (free_bytes, list_volumes, target_format, total_bytes,
                   transcode, transcode_to_aac, transcode_to_mp3)
from .massstorage import MassStorageBackend
from .itunesdb import ITunesDBBackend
from .mtp import MTPBackend
from .video import (GENERIC_VIDEO, IPOD_VIDEO, MTP_VIDEO, VideoProfile,
                    should_transcode_video, transcode_video)

BACKENDS = {
    "massstorage": MassStorageBackend,
    "ipod": ITunesDBBackend,
    "mtp": MTPBackend,
}

__all__ = ["BACKENDS", "MassStorageBackend", "ITunesDBBackend", "MTPBackend",
           "free_bytes", "list_volumes", "target_format", "total_bytes",
           "transcode", "transcode_to_aac", "transcode_to_mp3",
           "VideoProfile", "transcode_video", "should_transcode_video",
           "IPOD_VIDEO", "MTP_VIDEO", "GENERIC_VIDEO"]
