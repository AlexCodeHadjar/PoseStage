"""PoseStage: loopback-only static server + native WebView2 window."""
from __future__ import annotations

import base64
import hashlib
import logging
from logging.handlers import RotatingFileHandler
import mimetypes
import os
from pathlib import Path
import re
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

import poses
from poses import DATA_DIR, PoseCatalog

RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
EXE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
OFFLINE_ARGUMENTS = ("--disable-background-networking --disable-component-update --no-first-run "
                     "--disable-domain-reliability --disable-sync --disable-breakpad "
                     "--proxy-server=http://127.0.0.1:9 --proxy-bypass-list=127.0.0.1;localhost")
MIME = {".js": "text/javascript", ".json": "application/json", ".png": "image/png",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
INLINE_RE = re.compile(r"<(script|style)\b([^>]*)>(.*?)</\1\s*>", re.IGNORECASE | re.DOTALL)
COMPACT_SIZE = (480, 720)
LAUNCH = {"runtime": None, "fallback": False}  # WebView2 runtime chosen for this run; relaunch flag


def setup_logging():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[RotatingFileHandler(DATA_DIR / "log.txt", maxBytes=2_000_000,
                                                      backupCount=2, encoding="utf-8")])


def content_security_policy(html: bytes | None = None) -> str:
    """CSP with hashes of the exact inline <script>/<style> blocks of the served HTML (the importmap).

    'unsafe-eval' stays: pywebview's injected api.js builds window.pywebview.api with `new Function`.
    """
    hashes = {"script": [], "style": []}
    for tag, attributes, text in INLINE_RE.findall(html.decode("utf-8", "replace") if html else ""):
        if not re.search(r"\bsrc\s*=", attributes, re.IGNORECASE):
            # The HTML parser normalizes newlines before the browser hashes the element text.
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            digest = base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()
            hashes[tag.lower()].append(f" 'sha256-{digest}'")
    return ("default-src 'self'; script-src 'self' 'unsafe-eval'" + "".join(hashes["script"]) +
            "; style-src 'self'" + "".join(hashes["style"]) + "; img-src 'self' data: blob:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-src 'none'")


def runtime_failed(window=None) -> bool:
    """The chosen WebView2 could not start. If it was the system runtime and a portable one exists,
    schedule one relaunch with the portable runtime (closing the window) and return True."""
    from diagnostics import can_fall_back
    if not LAUNCH["fallback"] and can_fall_back(LAUNCH["runtime"]):
        LAUNCH["fallback"] = True
        logging.warning("System WebView2 %s failed to start; restarting with the portable runtime",
                        LAUNCH["runtime"]["system"].get("version"))
        if window is not None:
            threading.Thread(target=window.destroy, daemon=True).start()
    return LAUNCH["fallback"]


def relaunch_portable(argv=None):
    """Remember the broken system version (later runs go straight to portable until it changes)
    and start this program again with the portable runtime forced. The forced run never relaunches."""
    import subprocess
    from diagnostics import FAILED_MARKER, FORCE_ENV
    try:
        poses.write_json(DATA_DIR / FAILED_MARKER, {"version": LAUNCH["runtime"]["system"].get("version")})
    except OSError:
        logging.exception("Cannot save the WebView2 failure marker")
    argv = sys.argv if argv is None else argv
    command = [sys.executable, *(argv[1:] if getattr(sys, "frozen", False) else argv)]
    # PYINSTALLER_RESET_ENVIRONMENT: the onefile child unpacks itself instead of reusing our temp dir.
    environment = {**os.environ, FORCE_ENV: "portable", "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    logging.info("Relaunching with the portable WebView2: %s", command)
    return subprocess.Popen(command, env=environment, close_fds=True)


class RuntimeFailureWatch(logging.Handler):
    """pywebview logs WebView2 initialization failures itself; treat them as a failed runtime too."""

    def __init__(self, window):
        super().__init__(logging.ERROR)
        self.window = window

    def emit(self, record):
        if "WebView2 initialization failed" in record.getMessage():
            runtime_failed(self.window)


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

    def host_allowed(self):
        return self.headers.get("Host", "") == f"127.0.0.1:{self.server.server_port}"

    def serve(self, head):
        if not self.host_allowed():
            self.send_error(403)
            return
        catalog = self.server.catalog
        path = unquote(urlsplit(self.path).path)
        kind, _, relative = path[len(catalog.prefix) + 1:].partition("/") \
            if path.startswith(catalog.prefix + "/") else ("", "", "")
        file = None
        if kind in ("poses", "thumbs"):
            # Allowlisted relative path + containment check inside the collection folder.
            file = catalog.resolve(relative)
            if file is not None and kind == "thumbs":
                file = poses.thumbnail(file)
        elif kind == "web":
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
            html = stream.read() if file.suffix.lower() == ".html" else None
            self.send_response(200)
            self.send_header("Content-Type", MIME.get(file.suffix.lower())
                             or mimetypes.guess_type(file.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(html) if html is not None else os.fstat(stream.fileno()).st_size))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Security-Policy", content_security_policy(html))
            self.end_headers()
            if not head:
                try:
                    if html is not None:
                        self.wfile.write(html)
                    while html is None and (chunk := stream.read(256 * 1024)):
                        self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass


def work_area(hwnd=None):
    """(left, top, right, bottom) work area of the window's monitor (or the primary one), in pixels."""
    import ctypes
    from ctypes import wintypes

    class MonitorInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    if hwnd:
        user32.MonitorFromWindow.restype = ctypes.c_void_p
        user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        info = MonitorInfo(cbSize=ctypes.sizeof(MonitorInfo))
        monitor = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        if monitor and user32.GetMonitorInfoW(ctypes.c_void_p(monitor), ctypes.byref(info)):
            rect = info.rcWork
    if rect.right <= rect.left:
        user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
    return rect.left, rect.top, rect.right, rect.bottom


class Api:
    def __init__(self, catalog, *, fullscreen=True):
        # pywebview recursively exposes PUBLIC attributes. Keep all implementation
        # objects private so it cannot walk the native window or threading locks.
        self._catalog = catalog
        self._window = None
        self._select_lock = threading.Lock()
        self._collection_lock = threading.Lock()
        self._window_lock = threading.RLock()
        self._fullscreen = fullscreen
        self._on_top = False
        self._compact = False

    # --- Poses and state ---

    def get_poses(self):
        return self._catalog.get()

    def get_state(self):
        cid, items = self._catalog.snapshot()
        file, index = poses.read_state(cid)
        found = next((i for i, pose in enumerate(items) if pose["file"] == file), None) \
            if isinstance(file, str) else None
        if found is None:
            found = min(max(index, 0), len(items) - 1) if type(index) is int and items else 0
        return {"index": found, "file": items[found]["file"] if items else None}

    def save_state(self, index, file=None):
        """Saves by `file` when given (ignored unless it is in the current catalog), else by index."""
        cid, items = self._catalog.snapshot()
        if file is not None:
            index = next((i for i, pose in enumerate(items) if pose["file"] == file), None)
        if type(index) is int and 0 <= index < len(items):
            try:
                poses.write_state(cid, items[index]["file"], index)
            except OSError:
                logging.exception("Cannot save state")

    def set_support_mode(self, file, mode):
        return self._catalog.set_mode(file, mode)

    # --- Collections ---

    def get_collections(self):
        config = poses.load_config()
        current = self._catalog.snapshot()[0]
        result = [{**entry, "active": entry["id"] == current} for entry in config["collections"]]
        if current and all(entry["id"] != current for entry in result):
            folder = self._catalog.folder  # e.g. tools/dev_server.py --folder (not saved)
            result.append({"id": current, "name": folder.name or str(folder), "folder": str(folder), "active": True})
        return result

    def _data(self):
        return {"collections": self.get_collections(), "poses": self.get_poses()}

    def _pick_folder(self) -> Path | None:
        """Native folder dialog; None without a window (tests, dev server) or on cancel."""
        if not self._window:
            return None
        import webview
        chosen = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return Path(chosen[0]) if chosen else None

    def add_collection(self):
        if not self._select_lock.acquire(blocking=False):
            return self._data()
        try:
            folder = self._pick_folder()
            if folder:
                folder = folder.resolve()
                if poses.has_poses(folder):
                    with self._collection_lock:
                        poses.remember_collection(folder)
                        self._catalog.scan(folder)
                else:
                    logging.warning("No pose files in selected directory: %s", folder)
        finally:
            self._select_lock.release()
        return self._data()

    def switch_collection(self, cid):
        with self._collection_lock:
            entry = next((c for c in poses.load_config()["collections"] if c["id"] == cid), None)
            if entry is None and cid != self._catalog.id:
                raise ValueError(f"Unknown collection: {cid!r}")
            if entry is not None:
                if cid != self._catalog.id and not poses.has_poses(Path(entry["folder"])):
                    raise ValueError(f"Папка недоступна: {entry['folder']}")
                poses.set_active(cid)
                if cid != self._catalog.id:
                    self._catalog.scan(Path(entry["folder"]))
        return self._data()

    def remove_collection(self, cid):
        with self._collection_lock:
            collections = self.get_collections()
            if len(collections) > 1 and any(c["id"] == cid for c in collections):
                config = poses.forget_collection(cid)
                if cid == self._catalog.id:
                    target = next((c for c in config["collections"] if c["id"] == config["active"]), None)
                    target = target or next(c for c in collections if c["id"] != cid)
                    poses.set_active(target["id"])
                    self._catalog.scan(Path(target["folder"]))
        return self._data()

    def choose_folder(self):
        return self.add_collection()["poses"]

    # --- Library and view prefs ---

    def get_library(self):
        return poses.read_library(self._catalog.snapshot()[0])

    def toggle_favorite(self, file):
        return poses.toggle_favorite(self._catalog.require(file), file)

    def set_tags(self, file, tags):
        return poses.set_tags(self._catalog.require(file), file, tags)

    def get_view_prefs(self):
        return poses.read_view_prefs()

    def save_view_prefs(self, prefs):
        return poses.save_view_prefs(prefs)

    # --- Window (harmless no-ops without a native window) ---

    def toggle_fullscreen(self):
        with self._window_lock:
            if self._compact:
                self.set_compact(False)
                return
            if self._window:
                self._window.toggle_fullscreen()
            self._fullscreen = not self._fullscreen

    def set_on_top(self, on):
        on = bool(on)
        with self._window_lock:
            self._on_top = on
            if self._window:
                try:
                    self._window.on_top = on
                except Exception:
                    logging.exception("Cannot change on-top state")
        return on

    def set_compact(self, on):
        """Compact: ~480×720 at the right edge of the work area, on top. Off: back to fullscreen."""
        on = bool(on)
        with self._window_lock:
            if on == self._compact:
                return on
            self._compact = on
            window = self._window
            if window is None:
                return on
            try:
                if on:
                    if self._fullscreen:
                        window.toggle_fullscreen()
                        self._fullscreen = False
                    self._place_compact(window)
                    window.on_top = True
                else:
                    window.on_top = self._on_top
                    if not self._fullscreen:
                        window.toggle_fullscreen()
                        self._fullscreen = True
            except Exception:
                logging.exception("Cannot change compact mode")
        return on

    @staticmethod
    def _place_compact(window):
        import ctypes
        try:
            hwnd = int(window.native.Handle.ToInt64())
        except Exception:
            hwnd = None
        left, top, right, bottom = work_area(hwnd)
        try:
            scale = (ctypes.windll.user32.GetDpiForWindow(ctypes.c_void_p(hwnd)) or 96) / 96 if hwnd else 1
        except (AttributeError, OSError):
            scale = 1
        width = min(round(COMPACT_SIZE[0] * scale), right - left)
        height = min(round(COMPACT_SIZE[1] * scale), bottom - top)
        x = max(left, right - width - round(12 * scale))
        y = top + max(0, (bottom - top - height) // 2)
        if hwnd:  # physical pixels; pywebview's resize() applies its own DPI scale
            user32 = ctypes.windll.user32
            user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + [ctypes.c_int] * 4 + [ctypes.c_uint]
            user32.SetWindowPos(hwnd, None, x, y, width, height, 0x0004 | 0x0010)  # NOZORDER | NOACTIVATE
        else:
            window.resize(width, height)
            window.move(x, y)

    def quit(self):
        if self._window:
            self._window.destroy()

    def report_error(self, message):
        logging.error("Frontend: %s", str(message)[:4000])


def create_app(*, hidden=False, fullscreen=True):
    setup_logging()
    import webview
    from diagnostics import select_runtime
    webview.settings["ALLOW_FILE_URLS"] = False
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = OFFLINE_ARGUMENTS
    portable_runtime = EXE_DIR / "WebView2"
    runtime = LAUNCH["runtime"] = select_runtime(portable_runtime, DATA_DIR)
    if runtime["mode"] == "portable":
        webview.settings["WEBVIEW2_RUNTIME_PATH"] = str(portable_runtime)
    logging.log(logging.INFO if runtime["mode"] else logging.ERROR, "WebView2 runtime selected: %s", runtime)
    try:  # the EXE-folder fallback applies only to the packaged program
        folder, recursive = poses.find_source(EXE_DIR if getattr(sys, "frozen", False) else None)
    except Exception:
        logging.exception("Cannot determine the pose folder")
        folder, recursive = None, True
    catalog = PoseCatalog(folder, "/" + secrets.token_urlsafe(24), recursive=recursive)
    threading.Thread(target=catalog.scan, name="pose-scan", daemon=True).start()
    threading.Thread(target=poses.prune_thumbs, name="thumb-prune", daemon=True).start()
    server = LocalServer(catalog)
    threading.Thread(target=server.serve_forever, name="loopback-http", daemon=True).start()
    api = Api(catalog, fullscreen=fullscreen)
    window = webview.create_window(
        "PoseStage", f"http://127.0.0.1:{server.server_port}{catalog.prefix}/web/index.html",
        js_api=api, fullscreen=fullscreen, hidden=hidden, frameless=True, background_color="#16181b",
        width=1920, height=1080, easy_drag=False,  # only `.pywebview-drag-region` elements drag
        min_size=(360, 480), text_select=False, zoomable=False,
    )
    api._window = window
    logging.getLogger("pywebview").addHandler(RuntimeFailureWatch(window))
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
                if runtime_failed(window):
                    return
                import ctypes
                ctypes.windll.user32.MessageBoxW(None,
                    "Не найден рабочий Microsoft Edge WebView2 Runtime.\n"
                    "Установите или восстановите WebView2 Runtime и запустите PoseStage снова.",
                    "PoseStage", 0x10)
                window.native.Close()
                return
            core = sender.CoreWebView2
            logging.info("WebView2 started: %s runtime %s", runtime["mode"], core.Environment.BrowserVersionString)

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
        if not runtime_failed():
            # Native error dialog is also useful if WebView2 itself cannot open a page.
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,
                "Не удалось запустить PoseStage. Проверьте наличие Microsoft Edge WebView2.\n"
                f"Подробности: {DATA_DIR / 'log.txt'}", "PoseStage", 0x10)
    finally:
        server.shutdown()
        server.server_close()
    if LAUNCH["fallback"]:
        relaunch_portable()
    return 0


if __name__ == "__main__":
    sys.exit(main())
