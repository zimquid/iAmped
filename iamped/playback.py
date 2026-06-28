"""Native playback controller.

The web UI remains the controller, but audio playback can run outside WebKit.
mpv is preferred because it is cross-platform, open source, and exposes a small
JSON IPC API that handles FLAC, seeking, and HTTP streams reliably.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


class MPVPlayback:
    backend = "mpv"

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._socket_path = ""
        self._last_url = ""

    def _mpv_path(self) -> str | None:
        found = shutil.which("mpv")
        if found:
            return found
        for path in ("/opt/homebrew/bin/mpv", "/usr/local/bin/mpv"):
            if os.path.exists(path):
                return path
        return None

    def available(self) -> bool:
        return self._mpv_path() is not None

    def stop_process(self) -> None:
        if self._process and self._process.poll() is None:
            try:
                self._command(["quit"])
            except Exception:
                pass
            try:
                self._process.terminate()
                self._process.wait(timeout=2)
            except Exception:
                self._process.kill()
        self._process = None
        if self._socket_path:
            try:
                Path(self._socket_path).unlink(missing_ok=True)
            except OSError:
                pass
        self._socket_path = ""

    def play(self, url: str, start: float = 0) -> dict[str, Any]:
        if not self.available():
            raise RuntimeError("mpv is not installed.")
        self._ensure_process()
        self._last_url = url
        self._command(["loadfile", url, "replace"])
        self._command(["set_property", "pause", False])
        if start > 0:
            # mpv accepts the seek immediately after loadfile and applies it
            # once the stream is ready.
            self._command(["seek", float(start), "absolute", "exact"])
        return self.status()

    def pause(self) -> dict[str, Any]:
        self._ensure_process()
        self._command(["set_property", "pause", True])
        return self.status()

    def resume(self) -> dict[str, Any]:
        self._ensure_process()
        self._command(["set_property", "pause", False])
        return self.status()

    def stop(self) -> dict[str, Any]:
        if self._process and self._process.poll() is None:
            self._command(["stop"])
        return self.status()

    def seek(self, position: float) -> dict[str, Any]:
        self._ensure_process()
        self._command(["seek", max(0.0, float(position)), "absolute", "exact"])
        return self.status()

    def set_volume(self, volume: float) -> dict[str, Any]:
        self._ensure_process()
        self._command(["set_property", "volume", max(0, min(100, int(volume * 100)))])
        return self.status()

    def status(self) -> dict[str, Any]:
        if not self.available():
            return {"available": False, "backend": self.backend, "state": "missing"}
        if not self._process or self._process.poll() is not None:
            return {"available": True, "backend": self.backend, "state": "stopped"}
        idle = bool(self._get_property("idle-active"))
        paused = bool(self._get_property("pause"))
        position = self._get_property("time-pos") or 0
        duration = self._get_property("duration") or 0
        return {
            "available": True,
            "backend": self.backend,
            "duration": float(duration or 0),
            "paused": paused,
            "position": float(position or 0),
            "state": "stopped" if idle else ("paused" if paused else "playing"),
        }

    def _ensure_process(self) -> None:
        if self._process and self._process.poll() is None:
            return
        sock = os.path.join(tempfile.gettempdir(), f"iamped-mpv-{os.getpid()}.sock")
        try:
            Path(sock).unlink(missing_ok=True)
        except OSError:
            pass
        self._socket_path = sock
        self._process = subprocess.Popen(
            [
                self._mpv_path() or "mpv",
                "--no-config",
                "--idle=yes",
                "--no-video",
                "--force-window=no",
                "--terminal=no",
                f"--input-ipc-server={sock}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 5
        while time.time() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError("mpv exited while starting.")
            if os.path.exists(sock):
                return
            time.sleep(0.05)
        raise RuntimeError("mpv did not open its control socket.")

    def _command(self, command: list[Any]) -> Any:
        payload = json.dumps({"command": command}).encode() + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(self._socket_path)
            client.sendall(payload)
            chunks = []
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
        response = json.loads(b"".join(chunks).splitlines()[0].decode())
        if response.get("error") not in (None, "success"):
            raise RuntimeError(response.get("error") or "mpv command failed")
        return response.get("data")

    def _get_property(self, name: str) -> Any:
        try:
            return self._command(["get_property", name])
        except Exception:
            return None


PLAYER = MPVPlayback()
