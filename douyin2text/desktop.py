# SPDX-License-Identifier: MIT
"""Desktop entry point: the local UI in a native window.

The web server runs on a background thread and a native WebKit window points at
it, so closing the window shuts everything down. There is no terminal to babysit
and no browser tab left behind.

If ``pywebview`` is unavailable the same server is started and the default
browser is opened instead, so the app still works - it just looks less like an
app.
"""

from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path
from typing import Any

from . import __version__
from .pipeline import TranscriptionService
from .server import create_app, free_port

WINDOW_TITLE = "抖音视频转文字"
WINDOW_SIZE = (920, 1000)
MIN_SIZE = (560, 620)


class Application:
    """Owns the server thread and the window, and tears both down together."""

    def __init__(self, *, port: int = 8765, model_dir: Path | None = None,
                 threads: int = 4, verbose: bool = False) -> None:
        self.port = free_port(port)
        self.url = f"http://127.0.0.1:{self.port}/"
        self.verbose = verbose
        self._service = TranscriptionService(model_dir=model_dir, threads=threads)
        self._server: Any = None
        self._window: Any = None
        self._app = create_app(self._service, on_shutdown=self.stop)

    def _serve(self) -> None:
        import uvicorn

        config = uvicorn.Config(
            self._app,
            host="127.0.0.1",
            port=self.port,
            log_level="info" if self.verbose else "warning",
        )
        self._server = uvicorn.Server(config)
        self._server.run()

    def _wait_until_ready(self, timeout: float = 15.0) -> bool:
        import urllib.error
        import urllib.request

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{self.url}api/status", timeout=1):  # noqa: S310
                    return True
            except (urllib.error.URLError, OSError):
                time.sleep(0.15)
        return False

    def stop(self) -> None:
        """Quit. Safe to call from the window thread or an HTTP handler."""
        if self._server is not None:
            self._server.should_exit = True
        window = self._window
        if window is not None:
            try:
                window.destroy()
            except Exception:  # noqa: BLE001 - the window may already be gone
                pass

    def run(self, *, native: bool = True) -> int:
        thread = threading.Thread(target=self._serve, daemon=True, name="douyin2text-http")
        thread.start()

        if not self._wait_until_ready():
            print("服务启动失败。")
            return 1

        if native:
            try:
                import webview

                self._window = webview.create_window(
                    WINDOW_TITLE,
                    self.url,
                    width=WINDOW_SIZE[0],
                    height=WINDOW_SIZE[1],
                    min_size=MIN_SIZE,
                    text_select=True,
                )
                webview.start()
                self.stop()
                return 0
            except Exception as exc:  # noqa: BLE001 - fall back to a browser
                print(f"原生窗口不可用（{exc}），改用默认浏览器打开。")

        import webbrowser

        print(f"界面地址：{self.url}")
        webbrowser.open(self.url)
        try:
            thread.join()
        except KeyboardInterrupt:
            self.stop()
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="douyin2text-app", description="抖音视频转文字（图形界面）"
    )
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    parser.add_argument("--browser", action="store_true", help="用默认浏览器而不是原生窗口")
    parser.add_argument("--model-dir", help="SenseVoice 模型目录")
    parser.add_argument("--threads", type=int, default=4, help="转写线程数")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="打印 HTTP 访问日志，便于排查"
    )
    parser.add_argument(
        "--version", action="version", version=f"douyin2text-app {__version__}"
    )
    args = parser.parse_args(argv)

    return Application(
        port=args.port,
        model_dir=Path(args.model_dir) if args.model_dir else None,
        threads=args.threads,
        verbose=args.verbose,
    ).run(native=not args.browser)


__all__ = ["Application", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
