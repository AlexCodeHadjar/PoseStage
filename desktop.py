"""Desktop mode: the podium and poses right on the desktop, without a window or WebView2.

A per-pixel-alpha layered window (UpdateLayeredWindow) shows a podium pre-rendered from the
3D studio (web/desktop/podium.png + podium.json, made by tools/render_podium.py). Poses are
placed by the same rules as web/scene/placement.js: one body size for the collection and
contacts on the podium top. Fully transparent pixels let clicks through to the desktop.

Mouse: drag moves the podium, wheel changes the pose, double-click opens the PoseStage window,
right-click shows the menu. Keys (when focused): ← → / A D, Home / End, Esc closes.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
import subprocess
import sys
import threading
import time

from PIL import Image, ImageChops, ImageDraw, ImageFilter

import poses

SIZES = {"small": 520, "medium": 700, "large": 900}  # window height, logical px
DEFAULT_SIZE = "medium"
# Standing body height / podium diameter, as in the window at 1920×1080 (0.61·1080 / 930.6 px),
# so poses look the same size relative to the podium in both modes.
BODY_TO_PODIUM = 0.708
TRANSITION_S = 0.28
SHADOW_OPACITY = 0.36
SHADOW_BLUR = 3.5  # px at the podium's native 1400 px height


# --- Geometry: a port of web/scene/placement.js for the pre-rendered podium ---------------------

def load_meta(folder: Path) -> dict:
    return json.loads((folder / "podium.json").read_text(encoding="utf-8"))


def to_ground(meta, x, y):
    """Screen point (px from the canvas centre, y up) → (x, z) on the podium top plane."""
    e, u, v = meta["inverse"], x * 2 / meta["width"], y * 2 / meta["height"]
    w = e[2] * u + e[5] * v + e[8]
    return (e[0] * u + e[3] * v + e[6]) / w, (e[1] * u + e[4] * v + e[7]) / w


def project(meta, x, y, z):
    """World point → screen px from the canvas centre, y up."""
    m = meta["viewProjection"]  # column-major 4×4
    cx = m[0] * x + m[4] * y + m[8] * z + m[12]
    cy = m[1] * x + m[5] * y + m[9] * z + m[13]
    cw = m[3] * x + m[7] * y + m[11] * z + m[15]
    return cx / cw * meta["width"] / 2, cy / cw * meta["height"] / 2


def support_data(pose):
    x1, y1, x2, y2 = pose["box"]
    support = pose.get("support") or {}
    threshold = y1 + (support.get("cut", .93)) * (y2 - y1)
    contacts = [p for p in support.get("profile") or [] if p[1] >= threshold] or [[(x1 + x2) / 2, y2]]
    xs, ys = [p[0] for p in contacts], [p[1] for p in contacts]
    return contacts, (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2


def fit_placement(pose, meta):
    """Scale (canvas px per source px) and bbox centre (px from the canvas centre, y up)."""
    x1, y1, x2, y2 = pose["box"]
    width, height = meta["width"], meta["height"]
    contacts, cx, cy = support_data(pose)
    stature = pose.get("stature") or (y2 - y1)
    adjust = pose.get("scale_adjust") or 1
    desired = BODY_TO_PODIUM * meta["diameter"] / stature * adjust
    half = width * .47
    high = min(desired,
               (half + meta["centerX"]) / max(1e-9, cx - x1),
               (half - meta["centerX"]) / max(1e-9, x2 - cx),
               (height * .48 - meta["centerY"]) / max(1e-9, cy - y1),
               (meta["centerY"] + height * .49) / max(1e-9, y2 - cy))

    def radius_at(scale):
        return max(math.hypot(*to_ground(meta, meta["centerX"] + (x - cx) * scale, meta["centerY"] + (cy - y) * scale))
                   for x, y in contacts)

    low = 0.0
    if radius_at(high) <= meta["supportRadius"]:
        low = high
    else:
        for _ in range(22):
            mid = (low + high) / 2
            if radius_at(mid) <= meta["supportRadius"]:
                low = mid
            else:
                high = mid
    return {"scale": low, "limited": low < desired * .999, "cx": cx, "cy": cy,
            "x": meta["centerX"] + ((x1 + x2) / 2 - cx) * low, "y": meta["centerY"] + (cy - (y1 + y2) / 2) * low}


def podium_top(meta, points=96):
    """The podium top's outline in canvas image coordinates (y down)."""
    w, h = meta["width"], meta["height"]
    outline = []
    for i in range(points):
        a = i / points * 2 * math.pi
        x, y = project(meta, math.cos(a) * meta["radius"], meta["top"], math.sin(a) * meta["radius"])
        outline.append((w / 2 + x, h / 2 - y))
    return outline


def shadow_step(meta):
    """Image-space (dx, dy) of the cast shadow per pixel of figure height above the ground."""
    dx, dz = meta["shadowDirection"]
    x0, y0 = project(meta, 0, meta["top"], 0)
    x1, y1 = project(meta, dx, meta["top"], dz)
    per_unit = meta["diameter"] / (2 * meta["radius"])  # screen px per world unit at the podium
    return (x1 - x0) / per_unit, -(y1 - y0) / per_unit


def order(items, prefs, library):
    """Indices to cycle through: the gallery filter saved by the PoseStage window (or all)."""
    rule = prefs.get("filter") if isinstance(prefs.get("filter"), dict) else {}
    mode, favorites = rule.get("mode", "all"), bool(rule.get("favorites"))
    tag = (rule.get("tag") or "").casefold() or None
    query = (rule.get("search") or "").casefold() or None
    favs, tags = set(library.get("favorites", [])), library.get("tags", {})
    chosen = [i for i, pose in enumerate(items)
              if (mode == "all" or pose["support"]["mode"] == mode)
              and (not favorites or pose["file"] in favs)
              and (not tag or any(t.casefold() == tag for t in tags.get(pose["file"], [])))
              and (not query or query in pose["file"].casefold())]
    return chosen or list(range(len(items)))


# --- Composition --------------------------------------------------------------------------------

class Stage:
    """Podium sprite plus pose layers at one window size."""

    def __init__(self, folder: Path, height: int):
        self.meta = load_meta(folder)
        self.k = height / self.meta["height"]
        size = (round(self.meta["width"] * self.k), height)
        self.podium = Image.open(folder / "podium.png").convert("RGBA").resize(size, Image.LANCZOS)
        self.size = size
        mask = Image.new("L", size, 0)
        ImageDraw.Draw(mask).polygon([(x * self.k, y * self.k) for x, y in podium_top(self.meta)], fill=255)
        self.top_mask = mask
        self.step = shadow_step(self.meta)

    def layers(self, pose, image):
        """(figure layer, shadow layer) as full-size RGBA images, plus the placement."""
        p = fit_placement(pose, self.meta)
        x1, y1, x2, y2 = pose["box"]
        k, w, h = self.k, self.meta["width"], self.meta["height"]
        size = (max(1, round((x2 - x1) * p["scale"] * k)), max(1, round((y2 - y1) * p["scale"] * k)))
        figure = image.crop((x1, y1, x2, y2)).resize(size, Image.LANCZOS)
        left = round((w / 2 + p["x"]) * k - size[0] / 2)
        top = round((h / 2 - p["y"]) * k - size[1] / 2)
        layer = Image.new("RGBA", self.size, (0, 0, 0, 0))
        layer.alpha_composite(figure, (left, top))

        # Cast shadow: the silhouette above the support line, sheared along the key light.
        base = (h / 2 - self.meta["centerY"]) * k
        silhouette = Image.new("L", self.size, 0)
        silhouette.paste(figure.getchannel("A").point(lambda a: int(a * SHADOW_OPACITY)), (left, top))
        ImageDraw.Draw(silhouette).rectangle((0, round(base), self.size[0], self.size[1]), fill=0)
        a, c = self.step
        c = c if abs(c) > 1e-3 else -1e-3
        warped = silhouette.transform(self.size, Image.AFFINE,
                                      (1, -a / c, a / c * base, 0, -1 / c, base + base / c), Image.BICUBIC)
        warped = ImageChops.multiply(warped.filter(ImageFilter.GaussianBlur(SHADOW_BLUR * k * 2)), self.top_mask)
        shadow = Image.new("RGBA", self.size, (0, 0, 0, 0))
        shadow.putalpha(warped)
        return layer, shadow, p

    def frame(self, parts):
        """parts: [(figure layer, shadow layer, opacity)] → straight-alpha RGBA frame."""
        out = self.podium.copy()
        for _, shadow, opacity in parts:
            out.alpha_composite(shadow if opacity >= 1 else fade(shadow, opacity))
        for figure, _, opacity in parts:
            out.alpha_composite(figure if opacity >= 1 else fade(figure, opacity))
        return out


def fade(layer, opacity):
    faded = layer.copy()
    faded.putalpha(layer.getchannel("A").point(lambda v: int(v * opacity)))
    return faded


def premultiplied_bgra(frame: Image.Image) -> bytes:
    r, g, b, a = frame.split()
    return Image.merge("RGBA", [ImageChops.multiply(ch, a) for ch in (b, g, r)] + [a]).tobytes()


# --- Settings -----------------------------------------------------------------------------------

def read_settings() -> dict:
    data = poses.read_dict(poses.DATA_DIR / "desktop.json")
    settings = {"size": data.get("size") if data.get("size") in SIZES else DEFAULT_SIZE,
                "on_top": data.get("on_top") if isinstance(data.get("on_top"), bool) else True}
    if all(type(data.get(k)) is int for k in ("x", "y")):
        settings["x"], settings["y"] = data["x"], data["y"]
    return settings


def save_settings(settings: dict):
    try:
        poses.write_json(poses.DATA_DIR / "desktop.json", settings)
    except OSError:
        logging.exception("Cannot save desktop settings")


def launch(command_args):
    """Starts another PoseStage process (the window mode) detached from this one."""
    import os
    frozen = getattr(sys, "frozen", False)
    command = [sys.executable, *([] if frozen else [str(Path(__file__).with_name("app.py"))]), *command_args]
    environment = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    subprocess.Popen(command, env=environment, close_fds=True)


# --- Win32 window -------------------------------------------------------------------------------

def run(resource_dir: Path, executable_dir: Path | None) -> int:
    import ctypes
    from ctypes import wintypes as wt

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass
    user32, gdi32, kernel32 = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.kernel32
    user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]

    # One podium per user session: a second start brings the running one back instead.
    checked = ctypes.WinDLL("kernel32", use_last_error=True)  # reliable GetLastError through ctypes
    checked.CreateMutexW.restype = wt.HANDLE
    checked.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
    mutex = checked.CreateMutexW(None, False, "Local\\PoseStageDesktop")  # lives until the process ends
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        user32.FindWindowW.restype = wt.HWND
        user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
        running = user32.FindWindowW("PoseStageDesktop", None)
        if running:
            user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
            user32.PostMessageW(running, 0x8000 + 1, 0, 0)
        logging.info("Desktop mode already running: brought it back")
        return 0

    folder, recursive = poses.find_source(executable_dir)
    catalog = poses.PoseCatalog(folder, "/desktop", recursive=recursive)
    catalog.scan()
    import app
    api = app.Api(catalog, fullscreen=False)
    items = catalog.get()
    prefs, library = poses.read_view_prefs(), api.get_library()
    cycle = order(items, prefs, library)
    state = api.get_state()
    settings = read_settings()

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
    user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.CreateWindowExW.restype = wt.HWND
    user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
    user32.GetDpiForWindow.argtypes = [wt.HWND]
    user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
    user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
    user32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.LPVOID]
    user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    # Handles are pointer-sized: never let ctypes truncate them to int.
    user32.GetDC.restype, gdi32.CreateCompatibleDC.restype = wt.HDC, wt.HDC
    user32.GetDC.argtypes, gdi32.CreateCompatibleDC.argtypes = [wt.HWND], [wt.HDC]
    user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    user32.CreatePopupMenu.restype = wt.HMENU
    user32.DestroyMenu.argtypes = [wt.HMENU]
    user32.LoadCursorW.restype, user32.LoadCursorW.argtypes = wt.HANDLE, [wt.HINSTANCE, ctypes.c_void_p]
    kernel32.GetModuleHandleW.restype, kernel32.GetModuleHandleW.argtypes = wt.HMODULE, [wt.LPCWSTR]
    user32.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p]
    user32.KillTimer.argtypes = [wt.HWND, ctypes.c_size_t]
    user32.SetForegroundWindow.argtypes = [wt.HWND]
    user32.DestroyWindow.argtypes = [wt.HWND]
    user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    gdi32.CreateDIBSection.restype = wt.HBITMAP
    gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
    gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
    gdi32.SelectObject.restype = wt.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wt.HDC]
    user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE), wt.HDC,
                                           ctypes.POINTER(wt.POINT), wt.DWORD, ctypes.c_void_p, wt.DWORD]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
                    ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

    class BLENDFUNCTION(ctypes.Structure):
        _fields_ = [("op", ctypes.c_ubyte), ("flags", ctypes.c_ubyte), ("alpha", ctypes.c_ubyte), ("format", ctypes.c_ubyte)]

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                    ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                    ("lpszClassName", wt.LPCWSTR)]

    WM_DESTROY, WM_NCHITTEST, WM_KEYDOWN, WM_TIMER, WM_MOUSEWHEEL = 0x02, 0x84, 0x100, 0x113, 0x20A
    WM_NCLBUTTONDBLCLK, WM_NCRBUTTONUP, WM_EXITSIZEMOVE, WM_DPICHANGED = 0xA3, 0xA5, 0x232, 0x2E0
    WM_CONTEXTMENU, WM_DISPLAYCHANGE, WM_SHOW_AGAIN = 0x7B, 0x7E, 0x8000 + 1  # WM_APP + 1
    HTCAPTION, HWND_TOPMOST, HWND_NOTOPMOST = 2, wt.HWND(-1), wt.HWND(-2)
    MENU = {1: "next", 2: "prev", 10: "small", 11: "medium", 12: "large", 20: "on_top", 30: "window", 31: "close"}

    view = {"stage": None, "index": None, "layers": None, "anim": None, "frame": None, "hwnd": None}
    cache: dict[str, Image.Image] = {}
    lock = threading.Lock()

    def load_image(index):
        pose = items[index]
        with lock:
            if pose["file"] in cache:
                return cache[pose["file"]]
        path = catalog.resolve(pose["file"])
        image = Image.open(path).convert("RGBA") if path else Image.new("RGBA", (pose["w"], pose["h"]))
        with lock:
            cache[pose["file"]] = image
            while len(cache) > 6:
                cache.pop(next(iter(cache)))
        return image

    def preload(index):
        if cycle:
            for delta in (1, -1):
                neighbour = cycle[(cycle.index(index) + delta) % len(cycle)] if index in cycle else cycle[0]
                threading.Thread(target=load_image, args=(neighbour,), daemon=True).start()

    def scale():
        hwnd = view["hwnd"]
        dpi = user32.GetDpiForWindow(hwnd) if hwnd else 0
        return (dpi or 96) / 96

    def make_stage():
        """Stage at the chosen size, never taller than the work area of the window's monitor."""
        height = round(SIZES[settings["size"]] * scale())
        if view["hwnd"]:
            left, top, right, bottom = app.work_area(view["hwnd"])
            height = min(height, bottom - top)
        view["stage"] = Stage(resource_dir / "web" / "desktop", height)

    def keep_on_screen():
        """Moves the podium fully into the nearest monitor's work area (after monitor changes, or
        a position saved on a display that is gone): a tool window without a taskbar button must
        never get lost."""
        hwnd = view["hwnd"]
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        left, top, right, bottom = app.work_area(hwnd)  # nearest monitor
        width, height = rect.right - rect.left, rect.bottom - rect.top
        x = min(max(rect.left, left), max(left, right - width))
        y = min(max(rect.top, top), max(top, bottom - height))
        if (x, y) != (rect.left, rect.top):
            user32.SetWindowPos(hwnd, None, x, y, 0, 0, 0x0015)  # NOSIZE | NOZORDER | NOACTIVATE
            present(view["frame"])
            remember_position()

    def present(frame):
        """Pushes a straight-alpha frame to the layered window (premultiplied BGRA DIB)."""
        view["frame"] = frame
        hwnd, (width, height) = view["hwnd"], frame.size
        data = premultiplied_bgra(frame)
        screen = user32.GetDC(None)
        memory = gdi32.CreateCompatibleDC(screen)
        bitmap = old = None
        try:
            header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0)
            bits = ctypes.c_void_p()
            bitmap = gdi32.CreateDIBSection(screen, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
            if not bitmap:
                raise OSError("CreateDIBSection failed")
            ctypes.memmove(bits, data, len(data))
            old = gdi32.SelectObject(memory, bitmap)
            rect = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            user32.UpdateLayeredWindow(hwnd, screen, ctypes.byref(wt.POINT(rect.left, rect.top)),
                                       ctypes.byref(wt.SIZE(width, height)), memory, ctypes.byref(wt.POINT(0, 0)), 0,
                                       ctypes.byref(BLENDFUNCTION(0, 0, 255, 1)), 2)
        finally:
            if old:
                gdi32.SelectObject(memory, old)
            if bitmap:
                gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(memory)
            user32.ReleaseDC(None, screen)

    def show(index, animate=True):
        stage = view["stage"]
        if index is None or not items:
            present(stage.podium.copy())
            return
        figure, shadow, _ = stage.layers(items[index], load_image(index))
        previous = view["layers"]
        view["index"], view["layers"] = index, (figure, shadow)
        if animate and previous:
            view["anim"] = (time.perf_counter(), previous, (figure, shadow))
            user32.SetTimer(view["hwnd"], 1, 15, None)
        else:
            present(stage.frame([(figure, shadow, 1)]))
        api.save_state(index, items[index]["file"])
        preload(index)

    def step(delta):
        if not cycle:
            return
        if view["anim"]:  # finish the crossfade first, then take the queued steps at once
            view["pending"] = view.get("pending", 0) + delta
            return
        current = view["index"]
        if current in cycle:
            target = cycle[(cycle.index(current) + delta) % len(cycle)]
        else:
            target = cycle[0] if delta > 0 else cycle[-1]
        show(target)

    def tick():
        started, old, new = view["anim"]
        t = min(1.0, (time.perf_counter() - started) / TRANSITION_S)
        eased = t * t * (3 - 2 * t)
        if t >= 1:
            user32.KillTimer(view["hwnd"], 1)
            view["anim"] = None
            present(view["stage"].frame([(new[0], new[1], 1)]))
            pending, view["pending"] = view.get("pending", 0), 0
            if pending:
                step(pending)
        else:
            present(view["stage"].frame([(old[0], old[1], 1 - eased), (new[0], new[1], eased)]))

    def resize_to(size_name):
        settings["size"] = size_name
        user32.KillTimer(view["hwnd"], 1)  # a running crossfade holds layers of the old size
        view["anim"], view["pending"] = None, 0
        rect = wt.RECT()
        user32.GetWindowRect(view["hwnd"], ctypes.byref(rect))
        bottom, centre = rect.bottom, (rect.left + rect.right) // 2  # keep the podium's footprint in place
        make_stage()
        width, height = view["stage"].size
        user32.SetWindowPos(view["hwnd"], None, centre - width // 2, bottom - height, width, height, 0x0014)
        view["layers"] = None
        show(view["index"], animate=False)
        remember_position()

    def set_on_top(on):
        settings["on_top"] = on
        user32.SetWindowPos(view["hwnd"], HWND_TOPMOST if on else HWND_NOTOPMOST, 0, 0, 0, 0, 0x0013)
        save_settings(settings)

    def remember_position():
        rect = wt.RECT()
        user32.GetWindowRect(view["hwnd"], ctypes.byref(rect))
        settings["x"], settings["y"] = rect.left, rect.top
        save_settings(settings)

    def menu(x, y):
        handle = user32.CreatePopupMenu()
        add = lambda flags, ident, text: user32.AppendMenuW(handle, flags, ident, text)
        add(0, 1, "Следующая поза\t→")
        add(0, 2, "Предыдущая поза\t←")
        add(0x800, 0, None)
        for ident, name, title in ((10, "small", "Маленький"), (11, "medium", "Средний"), (12, "large", "Большой")):
            add(0x8 if settings["size"] == name else 0, ident, f"Размер: {title}")
        add(0x8 if settings["on_top"] else 0, 20, "Поверх всех окон")
        add(0x800, 0, None)
        add(0, 30, "Открыть окно PoseStage")
        add(0, 31, "Закрыть\tEsc")
        if x == -1 and y == -1:  # keyboard (Shift+F10 / menu key): open at the podium's centre
            rect = wt.RECT()
            user32.GetWindowRect(view["hwnd"], ctypes.byref(rect))
            x, y = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
        user32.SetForegroundWindow(view["hwnd"])
        chosen = MENU.get(user32.TrackPopupMenu(handle, 0x100 | 0x2, x, y, 0, view["hwnd"], None))
        user32.PostMessageW(view["hwnd"], 0, 0, 0)  # WM_NULL: lets the menu close reliably
        user32.DestroyMenu(handle)
        if chosen == "next":
            step(1)
        elif chosen == "prev":
            step(-1)
        elif chosen in SIZES:
            resize_to(chosen)
        elif chosen == "on_top":
            set_on_top(not settings["on_top"])
        elif chosen == "window":
            open_window()
        elif chosen == "close":
            user32.DestroyWindow(view["hwnd"])

    def open_window():
        try:
            launch([])
        except OSError:
            logging.exception("Cannot open the PoseStage window")
            return
        user32.DestroyWindow(view["hwnd"])

    def window_proc(hwnd, message, wparam, lparam):
        try:
            if message == WM_NCHITTEST:
                return HTCAPTION  # drag anywhere opaque; transparent pixels never reach us
            if message == WM_NCLBUTTONDBLCLK:
                open_window()
                return 0
            if message in (WM_NCRBUTTONUP, WM_CONTEXTMENU):
                x, y = ctypes.c_short(lparam & 0xFFFF).value, ctypes.c_short((lparam >> 16) & 0xFFFF).value
                menu(x, y)
                return 0
            if message == WM_MOUSEWHEEL:
                step(-1 if ctypes.c_short((wparam >> 16) & 0xFFFF).value > 0 else 1)
                return 0
            if message == WM_KEYDOWN:
                if wparam in (0x27, 0x44):  # → D
                    step(1)
                elif wparam in (0x25, 0x41):  # ← A
                    step(-1)
                elif wparam == 0x24 and cycle:  # Home
                    show(cycle[0])
                elif wparam == 0x23 and cycle:  # End
                    show(cycle[-1])
                elif wparam == 0x1B:  # Esc
                    user32.DestroyWindow(hwnd)
                return 0
            if message == WM_TIMER and view["anim"]:
                tick()
                return 0
            if message == WM_EXITSIZEMOVE:
                remember_position()
                return 0
            if message == WM_DPICHANGED:
                resize_to(settings["size"])
                keep_on_screen()
                return 0
            if message == WM_DISPLAYCHANGE:
                resize_to(settings["size"])
                keep_on_screen()
                return 0
            if message == WM_SHOW_AGAIN:  # a second `--desktop` start: come back on screen, in front
                keep_on_screen()
                user32.ShowWindow(hwnd, 5)
                user32.SetForegroundWindow(hwnd)
                return 0
            if message == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            logging.exception("Desktop mode: message %#x failed", message)
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    procedure = WNDPROC(window_proc)  # keep a reference for the window's lifetime
    instance = kernel32.GetModuleHandleW(None)
    window_class = WNDCLASSW(style=0x8, lpfnWndProc=procedure, hInstance=instance,  # CS_DBLCLKS
                             hCursor=user32.LoadCursorW(None, 32512), lpszClassName="PoseStageDesktop")
    user32.RegisterClassW(ctypes.byref(window_class))
    ex_style = 0x80000 | 0x80 | (0x8 if settings["on_top"] else 0)  # LAYERED | TOOLWINDOW | TOPMOST
    hwnd = user32.CreateWindowExW(ex_style, "PoseStageDesktop", "PoseStage", 0x80000000, 0, 0, 1, 1,
                                  None, None, instance, None)
    view["hwnd"] = hwnd
    make_stage()
    width, height = view["stage"].size
    if "x" in settings:
        x, y = settings["x"], settings["y"]
    else:
        left, top, right, bottom = app.work_area(hwnd)
        x, y = right - width - round(24 * scale()), bottom - height
    user32.SetWindowPos(hwnd, None, x, y, width, height, 0x0014)
    start = state.get("index", 0) if items else None
    show(start if start is None or start < len(items) else 0, animate=False)
    if "x" in settings:  # saved on a monitor that may be gone: also fixes the size for that monitor
        make_stage()
        if view["stage"].size != (width, height):
            resize_to(settings["size"])
        keep_on_screen()
    user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
    logging.info("Desktop mode: %d poses, %d in rotation, size %s", len(items), len(cycle), settings["size"])

    message = wt.MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(message))
        user32.DispatchMessageW(ctypes.byref(message))
    return 0
