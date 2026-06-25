"""Saved device profiles, safe locks, rollback snapshots, and OS eject."""
from __future__ import annotations

import json
import hashlib
import os
import platform
import plistlib
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import config
from .devices import usbdetect
from .sync import device_state

PROFILES_PATH = config.APP_DIR / "device-profiles.json"


def device_id(path: str, dtype: str, persist: bool = False) -> str:
    manifest = device_state.read_manifest(path, dtype) or {}
    value = manifest.get("device_id")
    if value:
        return value
    hardware = ""
    if dtype == "ipod":
        hardware = (usbdetect.match(mountpoint=path) or {}).get("serial") or ""
    if not hardware and platform.system() == "Darwin":
        try:
            info = subprocess.run(
                ["diskutil", "info", "-plist", path], capture_output=True,
                timeout=10, check=True).stdout
            values = plistlib.loads(info)
            hardware = values.get("VolumeUUID") or values.get("DeviceIdentifier") or ""
        except (OSError, plistlib.InvalidFileException, subprocess.SubprocessError):
            pass
    try:
        total = shutil.disk_usage(path).total
    except OSError:
        total = 0
    seed = f"{dtype}\0{hardware or Path(path).name}\0{total}"
    value = hashlib.sha256(seed.encode()).hexdigest()[:24]
    if persist:
        manifest["device_id"] = value
        manifest.setdefault("tracks", [])
        device_state.write_manifest(path, dtype, manifest)
    return value


def profiles() -> dict:
    try:
        value = json.loads(PROFILES_PATH.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def get_profile(path: str, dtype: str) -> dict:
    did = device_id(path, dtype)
    return {"device_id": did, **profiles().get(did, {})}


def save_profile(path: str, dtype: str, values: dict) -> dict:
    did = device_id(path, dtype, persist=True)
    all_profiles = profiles()
    allowed = {
        "name", "device_type", "reserve_mb", "fill_strategy",
        "transcode_lossless", "target_bitrate_k",
        "sync_artwork", "mirror", "playlist_ids",
    }
    current = all_profiles.get(did, {})
    current.update({k: v for k, v in values.items() if k in allowed})
    current["updated_at"] = int(time.time())
    all_profiles[did] = current
    config.APP_DIR.mkdir(parents=True, exist_ok=True)
    device_state.atomic_write_json(str(PROFILES_PATH), all_profiles)
    return {"device_id": did, **current}


@contextmanager
def device_lock(path: str, dtype: str):
    directory = device_state.state_dir(path, dtype)
    os.makedirs(directory, exist_ok=True)
    lock = os.path.join(directory, "sync.lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = time.time() - os.path.getmtime(lock)
        except OSError:
            age = 0
        if age > 6 * 3600:
            os.remove(lock)
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        else:
            raise RuntimeError("This device is already being written by iAmped.")
    try:
        os.write(fd, json.dumps({"pid": os.getpid(), "time": time.time()}).encode())
        os.close(fd)
        yield
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


def _backup_root(path: str, dtype: str) -> Path:
    return Path(device_state.state_dir(path, dtype)) / "backups"


def create_backup(path: str, dtype: str, removals: list[dict]) -> dict:
    stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    root = _backup_root(path, dtype) / stamp
    root.mkdir(parents=True, exist_ok=False)
    manifest = device_state.read_manifest(path, dtype) or {"tracks": []}
    device_state.atomic_write_json(str(root / "manifest.json"), manifest)
    if dtype == "ipod":
        source = Path(path) / "iPod_Control" / "iTunes" / "iTunesDB"
        if source.exists():
            shutil.copy2(source, root / "iTunesDB")
        artwork = Path(path) / "iPod_Control" / "Artwork"
        if artwork.exists():
            shutil.copytree(artwork, root / "Artwork")
    else:
        playlists = Path(path) / "Playlists"
        if playlists.exists():
            shutil.copytree(playlists, root / "Playlists")
    files = root / "files"
    for record in removals:
        rel = record.get("path")
        if not rel:
            continue
        source = Path(path) / rel
        if source.is_file():
            target = files / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    meta = {
        "id": stamp, "created_at": int(time.time()), "device_type": dtype,
        "managed_tracks": len(manifest.get("tracks", [])),
    }
    device_state.atomic_write_json(str(root / "backup.json"), meta)
    return meta


def list_backups(path: str, dtype: str) -> list[dict]:
    root = _backup_root(path, dtype)
    out = []
    if root.exists():
        for item in sorted(root.iterdir(), reverse=True):
            value = device_state.read_json(str(item / "backup.json"))
            if value:
                out.append(value)
    return out


def restore_backup(path: str, dtype: str, backup_id: str) -> dict:
    root = _backup_root(path, dtype) / backup_id
    prior = device_state.read_json(str(root / "manifest.json"))
    if not prior:
        raise RuntimeError("Backup manifest is missing.")
    current = device_state.read_manifest(path, dtype) or {"tracks": []}
    prior_paths = {r.get("path") for r in prior.get("tracks", [])}
    for record in current.get("tracks", []):
        rel = record.get("path")
        if rel and rel not in prior_paths:
            try:
                os.remove(os.path.join(path, rel))
            except FileNotFoundError:
                pass
    files = root / "files"
    if files.exists():
        for source in files.rglob("*"):
            if source.is_file():
                target = Path(path) / source.relative_to(files)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    if dtype == "ipod" and (root / "iTunesDB").exists():
        target = Path(path) / "iPod_Control" / "iTunes" / "iTunesDB"
        device_state.atomic_copy(str(root / "iTunesDB"), str(target))
        artwork = Path(path) / "iPod_Control" / "Artwork"
        if artwork.exists():
            shutil.rmtree(artwork)
        if (root / "Artwork").exists():
            shutil.copytree(root / "Artwork", artwork)
    if dtype == "massstorage":
        target = Path(path) / "Playlists"
        if target.exists():
            shutil.rmtree(target)
        if (root / "Playlists").exists():
            shutil.copytree(root / "Playlists", target)
    device_state.write_manifest(path, dtype, prior)
    try:
        os.remove(device_state.journal_path(path, dtype))
    except FileNotFoundError:
        pass
    return {"restored": backup_id, "managed_tracks": len(prior.get("tracks", []))}


def eject(path: str) -> None:
    system = platform.system()
    if system == "Darwin":
        command = ["diskutil", "eject", path]
    elif system == "Windows":
        command = ["powershell", "-NoProfile", "-Command",
                   f"(New-Object -comObject Shell.Application).Namespace(17)."
                   f"ParseName('{path}').InvokeVerb('Eject')"]
    else:
        mounted = subprocess.run(
            ["findmnt", "-no", "SOURCE", "--target", path],
            capture_output=True, text=True, timeout=10)
        source = mounted.stdout.strip()
        if not source:
            raise RuntimeError("Could not resolve the device block path.")
        command = ["udisksctl", "unmount", "-b", source]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "Eject failed").strip())
