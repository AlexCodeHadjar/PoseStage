"""PoseStage: loopback-only static server + native WebView2 window."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import mimetypes
import os
from pathlib import Path
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from poses import DATA_DIR, PoseCatalog, find_folder, pose_files, read_json, write_json

RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
EXE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
OFFLINE_ARGUMENTS = ("--disable-background-networking --disable-component-update --no-first-run "
                     "--disable-domain-reliability --disable-sync --disable-breakpad "
                     "--proxy-server=http://127.0.0.1:9 --proxy-bypass-list=127.0.0.1;localhost")


def setup_logging():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[RotatingFileHandler(DATA_DIR / "log.txt", maxBytes=2_000_000,
                                                      backupCount=2, encoding="utf-8")])


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, catalog):
        self.catalog = catalog
        self.web_root = (RESOURCE_DIR / "web").resolve()
        super().__init__(("127.0.0.1", 0), Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.serve(False)

    def do_HEAD(self):
        self.serve(True)

    def serve(self, head):
        host = self.headers.get("Host", "")
        if host != f"127.0.0.1:{self.server.server_port}":
            self.send_error(403)
            return
        prefix = self.server.catalog.prefix
        path = unquote(urlsplit(self.path).path)
        file = None
        if path.startswith(prefix + "/poses/"):
            name = path[len(prefix + "/poses/"):]
            with self.server.catalog.lock:
                file = self.server.catalog.allowed.get(name)
            if file and (file.resolve().parent != self.server.catalog.folder.resolve()):
                file = None
        elif path.startswith(prefix + "/web/"):
            relative = path[len(prefix + "/web/"):]
            # Reject Windows drive/UNC/device paths before resolve() can touch them.
            if (not relative or any(part == ".." for part in relative.split("/"))
                    or any(char in relative for char in ("\\", ":", "\0"))):
                self.send_error(404)
                return
            candidate = (self.server.web_root / relative).resolve()
            if candidate.is_relative_to(self.server.web_root) and candidate.is_file():
                file = candidate
        if file is None:
            self.send_error(404)
            return
        try:
            stream = file.open("rb")
        except OSError:
            self.send_error(404)
            return
        with stream:
            size = os.fstat(stream.fileno()).st_size
            self.send_response(200)
            mime = {".js": "text/javascript", ".json": "application/json"}.get(file.suffix.lower())
            self.send_header("Content-Type", mime or mimetypes.guess_type(file.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-src 'none'")
            self.end_headers()
            if not head:
                try:
                    while chunk := stream.read(256 * 1024):
                        self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass


class Api:
    def __init__(self, catalog):
        # pywebview recursively exposes PUBLIC attributes. Keep all implementation
        # objects private so it cannot walk the native window or threading locks.
        self._catalog = catalog
        self._window = None
        self._select_lock = threading.Lock()

    def get_poses(self):
        return self._catalog.get()

    def get_state(self):
        state = read_json(DATA_DIR / "state.json", {})
        index = state.get("index", 0) if isinstance(state, dict) else 0
        return {"index": index if isinstance(index, int) and 0 <= index < len(self._catalog.get()) else 0}

    def save_state(self, index):
        if isinstance(index, int) and 0 <= index < len(self._catalog.items):
            try:
                write_json(DATA_DIR / "state.json", {"index": index})
            except OSError:
                logging.exception("Cannot save state")

    def toggle_fullscreen(self):
        if self._window:
            self._window.toggle_fullscreen()

    def quit(self):
        if self._window:
            self._window.destroy()

    def report_error(self, message):
        logging.error("Frontend: %s", str(message)[:4000])

    def choose_folder(self):
        import webview
        if not self._window or not self._select_lock.acquire(blocking=False):
            return self.get_poses()
        try:
            chosen = self._window.create_file_dialog(webview.FileDialog.FOLDER)
            if chosen:
                folder = Path(chosen[0]).resolve()
                if pose_files(folder):
                    write_json(DATA_DIR / "config.json", {"poses_folder": str(folder)})
                    self._catalog.folder = folder
                    self._catalog.scan()
                else:
                    logging.warning("No pose files in selected directory: %s", folder)
            return self.get_poses()
        finally:
            self._select_lock.release()


def create_app(*, hidden=False, fullscreen=True):
    setup_logging()
    import webview
    webview.settings["ALLOW_FILE_URLS"] = False
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = OFFLINE_ARGUMENTS
    portable_runtime = EXE_DIR / "WebView2"
    if (portable_runtime / "msedgewebview2.exe").is_file():
        webview.settings["WEBVIEW2_RUNTIME_PATH"] = str(portable_runtime)
    catalog = PoseCatalog(find_folder(EXE_DIR), "/" + secrets.token_urlsafe(24))
    threading.Thread(target=catalog.scan, name="pose-scan", daemon=True).start()
    server = LocalServer(catalog)
    threading.Thread(target=server.serve_forever, name="loopback-http", daemon=True).start()
    api = Api(catalog)
    window = webview.create_window(
        "PoseStage", f"http://127.0.0.1:{server.server_port}{catalog.prefix}/web/index.html",
        js_api=api, fullscreen=fullscreen, hidden=hidden, frameless=True, background_color="#16181b",
        width=1920, height=1080,
        min_size=(640, 480), text_select=False, zoomable=False,
    )
    api._window = window
    origin = f"http://127.0.0.1:{server.server_port}"

    def configure_native():
        """WebView2 native events also block navigation outside the local app."""
        if not getattr(window.native, "webview", None):
            return

        def focus_event(focused):
            if window.events.loaded.is_set():
                script = "window.dispatchEvent(new Event('focus'))" if focused else "window.dispatchEvent(new Event('blur'))"
                threading.Thread(target=lambda: window.run_js(script), daemon=True).start()

        window.native.Activated += lambda *_: focus_event(True)
        window.native.Deactivate += lambda *_: focus_event(False)

        def initialized(sender, args):
            if not args.IsSuccess:
                logging.error("WebView2 initialization failed: %s", args.InitializationException)
                import ctypes
                ctypes.windll.user32.MessageBoxW(None,
                    "Не найден рабочий Microsoft Edge WebView2 Runtime.\n"
                    "Установите или восстановите WebView2 Runtime и запустите PoseStage снова.",
                    "PoseStage", 0x10)
                window.native.Close()
                return
            core = sender.CoreWebView2

            def navigation(_sender, event):
                if str(event.Uri) != f"{origin}{catalog.prefix}/web/index.html":
                    event.Cancel = True

            def resource(_sender, event):
                uri = str(event.Request.Uri)
                if not (uri.startswith(origin + catalog.prefix + "/") or uri.startswith(("data:", "blob:"))):
                    from System.IO import MemoryStream
                    event.Response = core.Environment.CreateWebResourceResponse(MemoryStream(), 403, "Offline only", "Content-Type: text/plain")
                    logging.warning("Blocked nonlocal WebView request")

            def new_window(_sender, event):
                event.Handled = True

            core.NavigationStarting += navigation
            core.WebResourceRequested += resource
            core.NewWindowRequested += new_window

        window.native.webview.CoreWebView2InitializationCompleted += initialized

    window.events.before_show += configure_native
    return window, api, server


def main():
    if "--self-check" in sys.argv:
        setup_logging()
        from diagnostics import self_check
        result = self_check(RESOURCE_DIR, EXE_DIR)
        if sys.stdout:
            import json
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["passed"] else 2
    import webview
    # Keep the WebView profile/cache out of the source directory and image folder.
    window, api, server = create_app()
    try:
        webview.start(gui="edgechromium", debug=False, private_mode=False,
                      storage_path=str(DATA_DIR / "webview"))
    except Exception:
        logging.exception("Cannot start WebView2")
        # Native error dialog is also useful if WebView2 itself cannot open a page.
        import ctypes
        ctypes.windll.user32.MessageBoxW(None,
            "Не удалось запустить PoseStage. Проверьте наличие Microsoft Edge WebView2.\n"
            f"Подробности: {DATA_DIR / 'log.txt'}", "PoseStage", 0x10)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
