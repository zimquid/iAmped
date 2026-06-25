"""Durable per-device sync state and transaction helpers.

The manifest is the ownership boundary: iAmped may update or remove only paths
recorded there.  The journal makes copied audio reusable after an interrupted
sync and keeps post-commit cleanup restartable.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from typing import Any

STATE_VERSION = 2
PART_SUFFIX = ".iamped-part"


def state_dir(device_path: str, device_type: str) -> str:
    if device_type == "ipod":
        return os.path.join(device_path, "iPod_Control", ".iamped")
    return os.path.join(device_path, ".iamped")


def manifest_path(device_path: str, device_type: str) -> str:
    return os.path.join(state_dir(device_path, device_type), "manifest.json")


def journal_path(device_path: str, device_type: str) -> str:
    return os.path.join(state_dir(device_path, device_type), "transaction.json")


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(os.path.dirname(path))


def atomic_write_text(path: str, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: str, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def atomic_copy(src: str, dst: str) -> int:
    """Copy to a temporary sibling, fsync it, then publish with os.replace."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + PART_SUFFIX
    try:
        with open(src, "rb") as inp, open(tmp, "wb") as out:
            shutil.copyfileobj(inp, out, 1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
        expected = os.path.getsize(src)
        actual = os.path.getsize(tmp)
        if actual != expected:
            raise OSError(f"incomplete device copy: expected {expected}, wrote {actual}")
        os.replace(tmp, dst)
        _fsync_dir(os.path.dirname(dst))
        return actual
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def read_manifest(device_path: str, device_type: str) -> dict | None:
    return read_json(manifest_path(device_path, device_type))


def write_manifest(device_path: str, device_type: str, manifest: dict) -> None:
    value = dict(manifest)
    value["version"] = STATE_VERSION
    value["device_type"] = device_type
    value["written_at"] = int(time.time())
    atomic_write_json(manifest_path(device_path, device_type), value)


def plan_hash(device_type: str, desired_keys: list[str], params: dict) -> str:
    stable = {
        "device_type": device_type,
        "desired_keys": desired_keys,
        "playlist_ids": params.get("playlist_ids", []),
        "fill_strategy": params.get("fill_strategy"),
        "transcode_lossless": bool(params.get("transcode_lossless")),
        "mirror": bool(params.get("mirror")),
    }
    raw = json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def start_or_resume(device_path: str, device_type: str, digest: str,
                    removals: list[dict]) -> tuple[dict, bool]:
    """Return a matching unfinished transaction or create a new one."""
    path = journal_path(device_path, device_type)
    prior = read_json(path)
    if prior and prior.get("plan_hash") == digest and \
            prior.get("status") in {"copying", "metadata", "cleanup"}:
        return prior, True

    if prior and prior.get("status") in {"copying", "metadata"}:
        raise RuntimeError(
            "An interrupted sync with different settings is still pending. "
            "Restore the previous selection and run Sync again to resume it.")

    tx = {
        "version": STATE_VERSION,
        "id": f"sync-{int(time.time())}",
        "device_type": device_type,
        "plan_hash": digest,
        "status": "copying",
        "completed": {},
        "removals": removals,
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }
    atomic_write_json(path, tx)
    return tx, False


def reclaim_for_copy(device_path: str, device_type: str, tx: dict,
                     bytes_needed: int) -> int:
    """Remove stale owned files early when replacements need their space.

    The operation is journaled after every file. A restarted sync with the same
    plan continues safely; a different plan is rejected while this transaction
    is unfinished.
    """
    reclaimed = int(tx.get("reclaimed_bytes") or 0)
    pending = tx.get("removals", [])
    while reclaimed < bytes_needed and pending:
        record = pending[0]
        existed = _safe_remove_record(device_path, record)
        if existed:
            reclaimed += int(record.get("size") or 0)
            tx["pre_removed_count"] = int(tx.get("pre_removed_count") or 0) + 1
        tx.setdefault("pre_removed", []).append(record)
        pending.pop(0)
        tx["reclaimed_bytes"] = reclaimed
        checkpoint(device_path, device_type, tx)
    if reclaimed < bytes_needed:
        raise OSError(
            f"not enough device space: needed {bytes_needed} bytes of reclaim, "
            f"recovered {reclaimed}")
    return reclaimed


def checkpoint(device_path: str, device_type: str, tx: dict) -> None:
    tx["updated_at"] = int(time.time())
    atomic_write_json(journal_path(device_path, device_type), tx)


def record_completed(device_path: str, device_type: str, tx: dict,
                     rating_key: str, record: dict) -> None:
    tx.setdefault("completed", {})[str(rating_key)] = record
    checkpoint(device_path, device_type, tx)


def record_is_valid(device_path: str, record: dict) -> bool:
    rel = record.get("path")
    expected = int(record.get("size") or 0)
    if not rel:
        return False
    full = os.path.join(device_path, rel)
    try:
        return os.path.isfile(full) and (not expected or os.path.getsize(full) == expected)
    except OSError:
        return False


def _safe_remove_record(device_path: str, record: dict) -> bool:
    rel = record.get("path")
    if not rel:
        return False
    root = os.path.realpath(device_path)
    full = os.path.realpath(os.path.join(device_path, rel))
    if full == root or not full.startswith(root + os.sep):
        raise RuntimeError(f"refusing to remove path outside device: {rel}")
    try:
        os.remove(full)
        return True
    except FileNotFoundError:
        return False


def finish_cleanup(device_path: str, device_type: str, tx: dict) -> int:
    """Delete manifest-owned stale files and make cleanup restartable."""
    tx["status"] = "cleanup"
    checkpoint(device_path, device_type, tx)
    removed = int(tx.get("pre_removed_count") or 0)
    pending = tx.get("removals", [])
    while pending:
        record = pending[0]
        removed += int(_safe_remove_record(device_path, record))
        pending.pop(0)
        checkpoint(device_path, device_type, tx)
    tx["status"] = "committed"
    checkpoint(device_path, device_type, tx)
    try:
        os.remove(journal_path(device_path, device_type))
    except OSError:
        pass
    prune_empty_dirs(device_path, device_type)
    return removed


def recover_cleanup(device_path: str, device_type: str) -> int:
    tx = read_json(journal_path(device_path, device_type))
    if not tx or tx.get("status") != "cleanup":
        return 0
    return finish_cleanup(device_path, device_type, tx)


def prune_empty_dirs(device_path: str, device_type: str) -> None:
    roots = [os.path.join(device_path, "Music")]
    if device_type == "ipod":
        roots = [os.path.join(device_path, "iPod_Control", "Music")]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for current, dirs, _files in os.walk(root, topdown=False):
            for name in dirs:
                try:
                    os.rmdir(os.path.join(current, name))
                except OSError:
                    pass


def managed_records(manifest: dict | None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for record in (manifest or {}).get("tracks", []):
        key = record.get("rating_key")
        if key:
            normalized = dict(record)
            if not normalized.get("path") and normalized.get("location"):
                normalized["path"] = normalized["location"].lstrip(":").replace(":", os.sep)
            out[str(key)] = normalized
    return out


def managed_bytes(manifest: dict | None) -> int:
    return sum(int(r.get("size") or 0) for r in managed_records(manifest).values())
