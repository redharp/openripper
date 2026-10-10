"""Windows companion shell; the same API and dashboard also run headless in Docker."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

import uvicorn

from .api import create_app
from .config import Settings


def server_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("Use a server address such as http://192.168.1.20:8080")
    _ = parsed.port  # Reject invalid ports before opening the native window.
    return value.rstrip("/")


def read_json(url: str) -> dict:
    # Local traffic must not be sent through an inherited HTTP proxy.
    with build_opener(ProxyHandler({})).open(url, timeout=2) as response:
        return json.load(response)


def is_openripper(url: str) -> bool:
    try:
        return (
            read_json(url + "/openapi.json").get("info", {}).get("title") == "OpenRipper"
            and read_json(url + "/healthz").get("status") == "ok"
        )
    except (OSError, ValueError, URLError):
        return False


def desktop_settings(data_dir: Path, *, simulate: bool = False) -> Settings:
    """Use per-user state, independent of the install folder or working directory."""
    data_dir.mkdir(parents=True, exist_ok=True)
    defaults = {
        "OPENRIPPER_DATABASE_URL": f"sqlite:///{(data_dir / 'openripper.db').as_posix()}",
        "OPENRIPPER_PREFERENCES_PATH": str(data_dir / "preferences.json"),
        "OPENRIPPER_LIBRARY_ROOT": str(data_dir / "Library"),
        "OPENRIPPER_AUTO_RIP": "false",
        "OPENRIPPER_UDEV_DISCOVERY": "false",
    }
    mkv = shutil.which("makemkvcon64") or shutil.which("makemkvcon")
    if not mkv:
        for variable in ("ProgramFiles(x86)", "ProgramFiles"):
            candidate = Path(os.environ.get(variable, "C:/Program Files")) / "MakeMKV"
            for name in ("makemkvcon64.exe", "makemkvcon.exe"):
                if (candidate / name).is_file():
                    mkv = str(candidate / name)
                    break
    defaults["OPENRIPPER_MAKEMKV_BIN"] = mkv or "makemkvcon"
    # Don't mutate process environment or override existing deployment settings.
    previous = dict(os.environ)
    try:
        for key, value in defaults.items():
            if key not in os.environ:
                os.environ[key] = value
        settings = Settings.from_env()
    finally:
        for key in defaults:
            if key not in previous:
                os.environ.pop(key, None)
    if simulate:
        # Smoke checks must never touch real hardware, shares, or the user's database.
        settings = Settings(
            simulate=True,
            auto_rip=False,
            udev_discovery=False,
            library_root=data_dir / "Library",
            movie_root=data_dir / "Library/Movies",
            tv_root=data_dir / "Library/TV",
            database_url=f"sqlite:///{(data_dir / 'smoke.db').as_posix()}",
            preferences_path=data_dir / "smoke-preferences.json",
        )
    return settings


class LocalServer:
    def __init__(self, settings: Settings, port: int = 8080):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if sys.platform == "win32":
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            self.socket.bind(("127.0.0.1", port))
            self.socket.listen(128)
            self.url = f"http://127.0.0.1:{self.socket.getsockname()[1]}"
            self.server = uvicorn.Server(
                uvicorn.Config(
                    create_app(settings),
                    log_config=None,
                    access_log=False,
                    loop="asyncio",
                    http="h11",
                    ws="websockets",
                    timeout_graceful_shutdown=5,
                )
            )
        except BaseException:
            self.socket.close()
            raise
        self.thread = threading.Thread(
            target=self.server.run,
            kwargs={"sockets": [self.socket]},
            daemon=True,
            name="openripper-service",
        )

    def start(self) -> str:
        self.thread.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.server.started:
                return self.url
            if not self.thread.is_alive():
                break
            time.sleep(0.1)
        self.stop()
        raise RuntimeError("OpenRipper could not start. See desktop.log in the app data folder.")

    def stop(self) -> None:
        self.server.should_exit = True
        if self.thread.is_alive():
            self.thread.join(timeout=15)
        self.socket.close()


def check_native_window(url: str, data_dir: Path) -> None:
    """Exercise bundled Python.NET/WebView2 without showing a test window."""
    import webview

    passed = threading.Event()
    window = webview.create_window("OpenRipper packaging check", url, hidden=True)

    def loaded():
        try:
            if window.evaluate_js("document.title").startswith("OpenRipper"):
                passed.set()
        finally:
            window.destroy()

    window.events.loaded += loaded
    timeout = threading.Timer(30, window.destroy)
    timeout.daemon = True
    timeout.start()
    try:
        webview.start(gui="edgechromium", storage_path=str(data_dir / "TestWebView"))
    finally:
        timeout.cancel()
    if not passed.is_set():
        raise RuntimeError("The packaged WebView2 window did not load the dashboard")


def run() -> None:
    parser = argparse.ArgumentParser(description="OpenRipper desktop companion")
    parser.add_argument("--connect", type=server_url, help="Open an existing OpenRipper server")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local/share")))
        / "OpenRipper",
    )
    parser.add_argument(
        "--smoke-test", action="store_true", help="Test bundled server, no hardware"
    )
    parser.add_argument(
        "--smoke-window",
        action="store_true",
        help="Also test the native window (with --smoke-test)",
    )
    args = parser.parse_args()
    if args.smoke_window and not args.smoke_test:
        parser.error("--smoke-window requires --smoke-test")
    args.data_dir = args.data_dir.resolve()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=args.data_dir / "desktop.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    local = None
    try:
        if args.smoke_test:
            local = LocalServer(desktop_settings(args.data_dir, simulate=True), port=0)
            url = local.start()
            health = read_json(url + "/healthz")
            with build_opener(ProxyHandler({})).open(url + "/assets/app.js") as response:
                assert response.status == 200 and len(response.read()) > 100
            assert health["simulation"] and not health["auto_rip"]
            assert not read_json(url + "/api/overview")["active_jobs"]
            if args.smoke_window:
                check_native_window(url, args.data_dir)
            (args.data_dir / "smoke-result.json").write_text(
                json.dumps(
                    {"status": "passed", "native_window": args.smoke_window, "health": health}
                ),
                encoding="utf-8",
            )
            return
        url = args.connect or "http://127.0.0.1:8080"
        if not is_openripper(url):
            if args.connect:
                raise RuntimeError(f"No healthy OpenRipper server at {url}")
            local = LocalServer(desktop_settings(args.data_dir))
            url = local.start()
        import webview

        webview.create_window(
            "OpenRipper",
            url,
            width=1280,
            height=850,
            min_size=(760, 560),
            confirm_close=local is not None,
        )
        webview.start(
            gui="edgechromium",
            private_mode=False,
            storage_path=str(args.data_dir / "WebView"),
            localization={
                "global.quitConfirmation": (
                    "Close OpenRipper and stop its local service? Active rips will be interrupted. "
                    "Minimize the window to keep ripping."
                )
            },
        )
    except Exception as exc:
        logging.exception("Desktop startup failed")
        if sys.platform == "win32" and not args.smoke_test:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None, f"{exc}\n\nLog: {args.data_dir / 'desktop.log'}", "OpenRipper", 0x10
            )
        raise SystemExit(1) from exc
    finally:
        if local:
            local.stop()


if __name__ == "__main__":
    run()
