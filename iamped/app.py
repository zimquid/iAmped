"""Desktop entry point.

Starts the Flask backend on a local port, then opens a native desktop window
(pywebview) pointing at it. Falls back to the default browser if pywebview or a
GUI toolkit is unavailable (e.g. headless).
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time

from .server import create_app

HOST = "127.0.0.1"


def _free_port() -> int:
    s = socket.socket()
    s.bind((HOST, 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _serve(app, port: int):
    app.run(host=HOST, port=port, threaded=True, use_reloader=False)


def main() -> int:
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
        webview.create_window("iAmped", url, width=1080, height=820,
                              min_size=(820, 620))
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
