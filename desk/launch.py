"""Open the curation desk.

A native window when ``pywebview`` is installed, the default browser
otherwise. Neither is a dependency of the package: the desk is the same page
either way, and the window is a convenience, not a requirement.
"""

from __future__ import annotations

import logging
import threading
import webbrowser

from minimalma.config import Config

from .server import Desk, serve

log = logging.getLogger("minimalma.desk")

WINDOW_TITLE = "minimalma"
WINDOW_SIZE = (1180, 780)
WINDOW_MIN = (900, 600)


def open_desk(
    config: Config,
    host: str = "127.0.0.1",
    port: int = 0,
    prefer_window: bool = True,
    desk: Desk | None = None,
) -> int:
    """Serve the desk and show it. Blocks until the window or process ends."""
    httpd, desk, url = serve(config, host=host, port=port, desk=desk)
    thread = threading.Thread(target=httpd.serve_forever, name="desk", daemon=True)
    thread.start()
    print(f"  desk    {url}")
    print(f"  data    {config.home}")

    window = _window_backend() if prefer_window else None
    try:
        if window is not None:
            window.create_window(
                WINDOW_TITLE,
                url,
                width=WINDOW_SIZE[0],
                height=WINDOW_SIZE[1],
                min_size=WINDOW_MIN,
                background_color="#131313",
            )
            window.start()
        else:
            if prefer_window:
                print("  window  pywebview is not installed — opening a browser tab")
                print("          (pip install pywebview for a native window)")
            webbrowser.open(url)
            _wait_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass
    finally:
        desk.stop_bot()
        httpd.shutdown()
        httpd.server_close()
    return 0


def _window_backend():
    try:
        import webview
    except ImportError:
        return None
    return webview


def _wait_forever() -> None:  # pragma: no cover - interactive
    stop = threading.Event()
    try:
        while not stop.wait(3600):
            pass
    except KeyboardInterrupt:
        pass
