"""Desktop entry point.

Starts the Flask backend on a local port, then opens a native desktop window
(pywebview) pointing at it. Falls back to the default browser if pywebview or a
GUI toolkit is unavailable (e.g. headless).
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time

from . import cache
from .server import create_app

HOST = "127.0.0.1"


def _on_exit() -> None:
    """Wipe the transient download/transcode cache on close (no-op in aggressive
    mode). Best-effort — a failure here must never block shutdown."""
    try:
        cache.clear_transient()
    except Exception:
        pass


def _free_port() -> int:
    s = socket.socket()
    s.bind((HOST, 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _serve(app, port: int):
    app.run(host=HOST, port=port, threaded=True, use_reloader=False)


def _macos_menus(window_ref: dict[str, object]):
    """Expose the page's existing commands through the macOS menu bar."""
    from webview.menu import Menu, MenuAction, MenuSeparator

    def dispatch(action: str):
        def handler() -> None:
            window = window_ref.get("window")
            if window is not None:
                window.evaluate_js(
                    "window.iAmpedNativeMenuAction && window.iAmpedNativeMenuAction("
                    + json.dumps(action) + ");"
                )

        return handler

    return [
        Menu("File", [
            MenuAction("Server", dispatch("server")),
            MenuAction("Music", dispatch("music")),
            MenuSeparator(),
            MenuAction("Enable SD card", dispatch("toggle-sd")),
            MenuAction("Auto-sync connected devices", dispatch("toggle-autosync")),
            MenuAction("Rebuild library (full)", dispatch("rebuild-library")),
        ]),
        Menu("View", [
            MenuAction("Status Visualizer", dispatch("toggle-visualizer")),
            MenuAction("Full Screen Visualizer", dispatch("fullscreen-visualizer")),
            MenuSeparator(),
            MenuAction("audioMotion Bars", dispatch("viz-bars")),
            MenuAction("audioMotion Spectrum", dispatch("viz-spectrum")),
            MenuAction("audioMotion LEDs", dispatch("viz-led")),
            MenuAction("Butterchurn Full Screen", dispatch("viz-butterchurn")),
            MenuSeparator(),
            MenuAction("Device Inspector", dispatch("inspector")),
        ]),
    ]


def main() -> int:
    import atexit
    atexit.register(_on_exit)
    app = create_app()
    port = int(os.environ.get("IAMPED_PORT", 0)) or _free_port()
    url = f"http://{HOST}:{port}"

    threading.Thread(target=_serve, args=(app, port), daemon=True).start()
    time.sleep(0.6)  # let Flask bind

    if "--no-window" in sys.argv or os.environ.get("IAMPED_NO_WINDOW"):
        print(f"iAmped running at {url}  (Ctrl-C to quit)")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0

    try:
        import webview
        window_ref: dict[str, object] = {}
        if sys.platform == "darwin":
            # Keep one intentional File/View pair instead of pywebview's
            # default View menu plus our application View menu.
            webview.settings["SHOW_DEFAULT_MENUS"] = False
        menu = _macos_menus(window_ref) if sys.platform == "darwin" else []
        window = webview.create_window("iAmped", url, width=1080, height=820,
                                       min_size=(820, 620), menu=menu)
        window_ref["window"] = window
        if sys.platform == "darwin" and window is not None:
            window.events.loaded += lambda: window.evaluate_js(
                "document.documentElement.classList.add('native-macos');"
            )
        webview.start()
        return 0
    except Exception as exc:  # noqa: BLE001
        import webbrowser
        print(f"Desktop window unavailable ({exc}); opening in browser: {url}")
        webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
