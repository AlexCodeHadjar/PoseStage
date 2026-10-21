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
# "file" sizing, the default (placement.js FILE_FIGURE_SHARE): one scale for every picture,
# as drawn in its file, only centred on the podium. "body": one estimated body size.
FILE_FIGURE_SHARE = 0.86
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


def fit_placement(pose, meta, sizing="file"):
    """Scale (canvas px per source px) and bbox centre (px from the canvas centre, y up)."""
    x1, y1, x2, y2 = pose["box"]
    width, height = meta["width"], meta["height"]
    contacts, cx, cy = support_data(pose)
    if sizing == "file":
        scale = BODY_TO_PODIUM * meta["diameter"] / (FILE_FIGURE_SHARE * pose["h"])
        return {"scale": scale, "limited": False, "cx": cx, "cy": cy,
                "x": meta["centerX"] + ((x1 + x2) / 2 - cx) * scale, "y": meta["centerY"] + (cy - (y1 + y2) / 2) * scale}
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

# --- Idle animation: a port of web/scene/idle.js ------------------------------------------------

IDLE_LOOP = 6.0  # seconds; every motion's period divides it
IDLE_SWAY, IDLE_HEAD, IDLE_HAND = math.radians(1.25), math.radians(2.4), math.radians(2.2)
IDLE_LIFT = .028
IDLE_FPS = 12
IDLE_GRID = 18  # display px between warp mesh vertices


def smooth(edge0, edge1, x):
    t = min(1.0, max(0.0, (x - edge0) / (edge1 - edge0)))
    return t * t * (3 - 2 * t)


def rotate(x, y, cx, cy, angle):
    c, s = math.cos(angle), math.sin(angle)
    return cx + c * (x - cx) - s * (y - cy), cy + s * (x - cx) + c * (y - cy)


def estimate_joints(pose):
    """A skeleton from the box alone, for poses without detected or corrected joints."""
    x1, y1, x2, y2 = pose["box"]
    h, cx = y2 - y1, (x1 + x2) / 2
    nose, shoulder, hip = {"kneel": (.09, .26, .62), "ground": (.12, .32, .75)}.get(
        (pose.get("support") or {}).get("mode"), (.07, .2, .5))

    def side(y, half):
        return [[cx + half, y1 + y * h], [cx - half, y1 + y * h]]
    shoulders = side(shoulder, .11 * h)
    return {"nose": [cx, y1 + nose * h], "shoulders": shoulders, "elbows": shoulders, "wrists": shoulders,
            "hips": side(hip, .07 * h)}


class IdleRig:
    """Displacement field (source px, y down) for breathing, weight shift, head and free hands;
    supports stay pinned. Same math as createRig() in web/scene/idle.js."""

    def __init__(self, pose):
        j = pose.get("joints") or estimate_joints(pose)

        def mid(pair):
            return (pair[0][0] + pair[1][0]) / 2, (pair[0][1] + pair[1][1]) / 2
        self.shoulders, self.hips, self.nose = mid(j["shoulders"]), mid(j["hips"]), tuple(j["nose"])
        ax, ay = self.shoulders[0] - self.hips[0], self.shoulders[1] - self.hips[1]
        self.torso = max(1.0, math.hypot(ax, ay))
        self.axis = (ax / self.torso, ay / self.torso)
        self.neck = (self.shoulders[0] + (self.nose[0] - self.shoulders[0]) * .35,
                     self.shoulders[1] + (self.nose[1] - self.shoulders[1]) * .35)
        head = math.hypot(self.nose[0] - self.shoulders[0], self.nose[1] - self.shoulders[1])
        self.head_radius = max(head * 1.25, self.torso * .45)
        self.neck_along = self.along(*self.neck)
        width = max(1.0, math.hypot(j["shoulders"][0][0] - j["shoulders"][1][0],
                                    j["shoulders"][0][1] - j["shoulders"][1][1]))
        self.hands = []
        for elbow, wrist in zip(j["elbows"], j["wrists"]):
            reach = math.hypot(wrist[0] - elbow[0], wrist[1] - elbow[1])
            from_axis = abs((wrist[0] - self.hips[0]) * self.axis[1] - (wrist[1] - self.hips[1]) * self.axis[0])
            if reach > width * .25 and from_axis > width * .9:
                self.hands.append((tuple(elbow), tuple(wrist)))
        self.contacts = support_data(pose)[0]
        self.pin = .06 * (pose["box"][3] - pose["box"][1])

    def along(self, x, y):
        return ((x - self.hips[0]) * self.axis[0] + (y - self.hips[1]) * self.axis[1]) / self.torso

    def lock_at(self, x, y):
        nearest = min(math.hypot(x - cx, y - cy) for cx, cy in self.contacts)
        return 1 - smooth(self.pin * .5, self.pin * 1.6, nearest)

    def displace(self, x, y, t, lock=None):
        lock = self.lock_at(x, y) if lock is None else lock
        if lock >= 1:
            return 0.0, 0.0
        w = 2 * math.pi / IDLE_LOOP
        breath, sway = math.sin(2 * w * t), math.sin(w * t)
        nod = math.sin(w * t + 1.3) + .35 * math.sin(3 * w * t + .4)
        s = self.along(x, y)
        nx, ny = x, y
        weight = smooth(-.12, .25, s)
        rx, ry = rotate(nx, ny, *self.hips, IDLE_SWAY * sway)
        nx, ny = nx + (rx - nx) * weight, ny + (ry - ny) * weight
        lift = IDLE_LIFT * self.torso * (breath * .5 + .5) * smooth(.15, .85, s)
        nx, ny = nx + self.axis[0] * lift, ny + self.axis[1] * lift
        distance = math.hypot(x - self.nose[0], y - self.nose[1])
        head = smooth(-.05, .35, (s - self.neck_along) / .5) * (
            1 - smooth(self.head_radius * .8, self.head_radius * 1.4, distance))
        if head > 0:
            rx, ry = rotate(nx, ny, *self.neck, IDLE_HEAD * nod)
            nx, ny = nx + (rx - nx) * head, ny + (ry - ny) * head
        for (ex, ey), (wx, wy) in self.hands:
            reach = math.hypot(wx - ex, wy - ey)
            hand = 1 - smooth(reach * .5, reach * 1.1, math.hypot(x - wx, y - wy))
            if hand <= 0:
                continue
            rx, ry = rotate(nx, ny, ex, ey, IDLE_HAND * math.sin(2 * w * t + ex * .01))
            nx, ny = nx + (rx - nx) * hand, ny + (ry - ny) * hand
        free = 1 - lock
        return (nx - x) * free, (ny - y) * free


# --- Composition --------------------------------------------------------------------------------

class Sprite:
    """An RGBA image placed at (x, y) on the stage canvas."""
    __slots__ = ("image", "x", "y")

    def __init__(self, image, x, y):
        self.image, self.x, self.y = image, x, y


def place(out, image, x, y):
    """alpha_composite that clips sprites hanging over the canvas edge (a lifted hand)."""
    cx, cy = max(0, -x), max(0, -y)
    if cx >= image.width or cy >= image.height:
        return
    if cx or cy:
        image = image.crop((cx, cy, image.width, image.height))
    out.alpha_composite(image, (x + cx, y + cy))


class Stage:
    """Podium sprite plus pose sprites at one window size."""

    def __init__(self, folder: Path, height: int, sizing: str = "file"):
        self.meta = load_meta(folder)
        self.sizing = sizing
        self.k = height / self.meta["height"]
        size = (round(self.meta["width"] * self.k), height)
        self.podium = Image.open(folder / "podium.png").convert("RGBA").resize(size, Image.LANCZOS)
        self.size = size
        mask = Image.new("L", size, 0)
        ImageDraw.Draw(mask).polygon([(x * self.k, y * self.k) for x, y in podium_top(self.meta)], fill=255)
        self.top_mask = mask
        self.step = shadow_step(self.meta)
        self.base = (self.meta["height"] / 2 - self.meta["centerY"]) * self.k  # support line, image y

    def prepare(self, pose, image):
        """Placement and the figure resized for this stage (the idle rig is built on demand)."""
        p = fit_placement(pose, self.meta, self.sizing)
        x1, y1, x2, y2 = pose["box"]
        k, w, h = self.k, self.meta["width"], self.meta["height"]
        size = (max(1, round((x2 - x1) * p["scale"] * k)), max(1, round((y2 - y1) * p["scale"] * k)))
        figure = image.crop((x1, y1, x2, y2)).resize(size, Image.LANCZOS)
        left = round((w / 2 + p["x"]) * k - size[0] / 2)
        top = round((h / 2 - p["y"]) * k - size[1] / 2)
        return {"pose": pose, "placement": p, "figure": figure, "left": left, "top": top,
                "per_source": size[0] / (x2 - x1), "rig": None, "mesh": None}

    def sprites(self, prepared, t=None):
        """(figure sprite, shadow sprite); `t` (seconds) animates the idle loop."""
        if t is None:
            figure = Sprite(prepared["figure"], prepared["left"], prepared["top"])
        else:
            figure = self.animate(prepared, t)
        return figure, self.cast_shadow(figure)

    def animate(self, prepared, t):
        """The figure warped by the idle rig: an inverse mesh warp of the display-size image."""
        pose, k = prepared["pose"], prepared["per_source"]
        x1, y1 = pose["box"][:2]
        source = prepared["figure"]
        pad = max(4, round(.06 * source.height))
        if prepared["mesh"] is None:
            rig = prepared["rig"] = IdleRig(pose)
            width, height = source.width + 2 * pad, source.height + 2 * pad
            xs = list(range(0, width, IDLE_GRID)) + [width]
            ys = list(range(0, height, IDLE_GRID)) + [height]

            def to_source(qx, qy):
                return x1 + (qx - pad) / k, y1 + (qy - pad) / k
            locks = {(qx, qy): rig.lock_at(*to_source(qx, qy)) for qx in xs for qy in ys}
            prepared["mesh"] = (width, height, xs, ys, to_source, locks)
            canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            canvas.alpha_composite(source, (pad, pad))
            prepared["padded"] = canvas
        width, height, xs, ys, to_source, locks = prepared["mesh"]
        rig = prepared["rig"]
        moved = {}
        for point, lock in locks.items():
            dx, dy = rig.displace(*to_source(*point), t, lock)
            moved[point] = (point[0] - dx * k, point[1] - dy * k)  # the output point samples here
        mesh = []
        for left, right in zip(xs, xs[1:]):
            for top, bottom in zip(ys, ys[1:]):
                quad = [*moved[(left, top)], *moved[(left, bottom)], *moved[(right, bottom)], *moved[(right, top)]]
                mesh.append(((left, top, right, bottom), quad))
        warped = prepared["padded"].transform((width, height), Image.MESH, mesh, Image.BICUBIC)
        return Sprite(warped, prepared["left"] - pad, prepared["top"] - pad)

    def cast_shadow(self, figure: Sprite) -> Sprite:
        """The silhouette above the support line, sheared along the key light onto the podium top."""
        a, c = self.step
        c = c if abs(c) > 1e-3 else -1e-3
        base, margin = self.base, max(6, round(SHADOW_BLUR * self.k * 6))
        rise = max(0.0, base - figure.y)  # figure height above the support line
        ox = figure.x - margin + min(0, round(a * rise))
        oy = round(min(figure.y, base + c * rise)) - margin
        width = figure.image.width + round(abs(a) * rise) + 2 * margin
        height = max(1, round(base - oy) + margin)
        local_base = base - oy
        silhouette = Image.new("L", (width, height), 0)
        silhouette.paste(figure.image.getchannel("A").point(lambda v: int(v * SHADOW_OPACITY)),
                         (figure.x - ox, figure.y - oy))
        ImageDraw.Draw(silhouette).rectangle((0, round(local_base), width, height), fill=0)
        warped = silhouette.transform((width, height), Image.AFFINE,
                                      (1, -a / c, a / c * local_base, 0, -1 / c, local_base + local_base / c),
                                      Image.BICUBIC)
        warped = ImageChops.multiply(warped.filter(ImageFilter.GaussianBlur(SHADOW_BLUR * self.k * 2)),
                                     self.top_mask.crop((ox, oy, ox + width, oy + height)))
        shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        shadow.putalpha(warped)
        return Sprite(shadow, ox, oy)

    def layers(self, pose, image):
        """(figure layer, shadow layer) as full-size RGBA images, plus the placement."""
        prepared = self.prepare(pose, image)
        full = []
        for sprite in self.sprites(prepared):
            layer = Image.new("RGBA", self.size, (0, 0, 0, 0))
            place(layer, sprite.image, sprite.x, sprite.y)
            full.append(layer)
        return full[0], full[1], prepared["placement"]

    def frame(self, parts):
        """parts: [(figure, shadow, opacity)], each a Sprite or a full-size image → RGBA frame."""
        out = self.podium.copy()

        def put(layer, opacity):
            image, x, y = (layer.image, layer.x, layer.y) if isinstance(layer, Sprite) else (layer, 0, 0)
            place(out, image if opacity >= 1 else fade(image, opacity), x, y)
        for _, shadow, opacity in parts:
            put(shadow, opacity)
        for figure, _, opacity in parts:
            put(figure, opacity)
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
    WM_WTSSESSION_CHANGE = 0x2B1
    HTCAPTION, HWND_TOPMOST, HWND_NOTOPMOST = 2, wt.HWND(-1), wt.HWND(-2)
    MENU = {1: "next", 2: "prev", 10: "small", 11: "medium", 12: "large", 20: "on_top", 21: "sizing",
            22: "idle", 30: "window", 31: "close"}

    view = {"stage": None, "index": None, "layers": None, "anim": None, "frame": None, "hwnd": None,
            "sizing": prefs.get("sizing") if prefs.get("sizing") in ("body", "file") else "file",
            "idle": prefs.get("idle") is True, "prepared": None, "region": None, "idle_shadow": None}
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
        view["stage"] = Stage(resource_dir / "web" / "desktop", height, view.get("sizing", "file"))

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
            push()
            remember_position()

    dib = {"size": None}

    def release_dib():
        if dib.get("memory"):
            gdi32.SelectObject(dib["memory"], dib["old"])
            gdi32.DeleteObject(dib["bitmap"])
            gdi32.DeleteDC(dib["memory"])
        dib.clear()
        dib["size"] = None

    def ensure_dib(size):
        """One persistent premultiplied BGRA DIB per window size (idle frames patch it)."""
        if dib["size"] == size:
            return
        release_dib()
        width, height = size
        screen = user32.GetDC(None)
        try:
            memory = gdi32.CreateCompatibleDC(screen)
            header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0)
            bits = ctypes.c_void_p()
            bitmap = gdi32.CreateDIBSection(screen, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
        finally:
            user32.ReleaseDC(None, screen)
        if not bitmap:
            gdi32.DeleteDC(memory)
            raise OSError("CreateDIBSection failed")
        dib.update(size=size, memory=memory, bitmap=bitmap, bits=bits, old=gdi32.SelectObject(memory, bitmap))

    def push():
        """Shows the DIB's current contents in the layered window."""
        if not dib.get("memory"):
            return
        width, height = dib["size"]
        rect = wt.RECT()
        user32.GetWindowRect(view["hwnd"], ctypes.byref(rect))
        screen = user32.GetDC(None)
        try:
            user32.UpdateLayeredWindow(view["hwnd"], screen, ctypes.byref(wt.POINT(rect.left, rect.top)),
                                       ctypes.byref(wt.SIZE(width, height)), dib["memory"],
                                       ctypes.byref(wt.POINT(0, 0)), 0, ctypes.byref(BLENDFUNCTION(0, 0, 255, 1)), 2)
        finally:
            user32.ReleaseDC(None, screen)

    def present(frame):
        """Pushes a straight-alpha frame to the layered window (premultiplied BGRA DIB)."""
        ensure_dib(frame.size)
        data = premultiplied_bgra(frame)
        ctypes.memmove(dib["bits"], data, len(data))
        push()

    def present_patch(patch, x, y):
        """Replaces one rectangle of the shown frame (the idle animation's moving region)."""
        width = dib["size"][0]
        data = premultiplied_bgra(patch)
        row = patch.width * 4
        base = dib["bits"].value
        for r in range(patch.height):
            ctypes.memmove(base + ((y + r) * width + x) * 4, data[r * row:(r + 1) * row], row)
        push()

    def bounds(*sprites):
        """Union rectangle of sprites, clipped to the canvas."""
        width, height = view["stage"].size
        left = max(0, min(s.x for s in sprites))
        top = max(0, min(s.y for s in sprites))
        right = min(width, max(s.x + s.image.width for s in sprites))
        bottom = min(height, max(s.y + s.image.height for s in sprites))
        return left, top, max(left + 1, right), max(top + 1, bottom)

    def show(index, animate=True):
        stage = view["stage"]
        if index is None or not items:
            view["prepared"] = None
            present(stage.podium.copy())
            return
        prepared = stage.prepare(items[index], load_image(index))
        figure, shadow = stage.sprites(prepared)
        # Cross-fade from what is on screen: the animated frame while the pose is alive.
        previous = view.get("live") or view["layers"]
        view["live"] = None
        view["index"], view["layers"], view["prepared"] = index, (figure, shadow), prepared
        view["region"], view["idle_shadow"] = bounds(figure, shadow), None
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

    def idle_tick():
        """One idle-animation frame: only the rectangle around the figure and its shadow."""
        prepared = view["prepared"]
        if view["anim"] or not view["idle"] or view.get("locked") or prepared is None or not dib.get("memory"):
            return
        stage = view["stage"]
        figure = stage.animate(prepared, time.perf_counter() - view["idle_start"])
        view["idle_frame"] = view.get("idle_frame", 0) + 1
        if view["idle_shadow"] is None or view["idle_frame"] % 2 == 0:  # the shadow moves slowly
            view["idle_shadow"] = stage.cast_shadow(figure)
        shadow = view["idle_shadow"]
        now = bounds(figure, shadow)
        last = view["region"]
        left, top = min(now[0], last[0]), min(now[1], last[1])
        right, bottom = max(now[2], last[2]), max(now[3], last[3])
        patch = stage.podium.crop((left, top, right, bottom))
        place(patch, shadow.image, shadow.x - left, shadow.y - top)
        place(patch, figure.image, figure.x - left, figure.y - top)
        present_patch(patch, left, top)
        view["region"], view["live"] = now, (figure, shadow)

    def set_idle(on):
        view["idle"] = on
        poses.save_view_prefs({"idle": on})  # shared with the PoseStage window
        if on:
            view["idle_start"] = time.perf_counter()
            user32.SetTimer(view["hwnd"], 2, 1000 // IDLE_FPS, None)
        else:
            user32.KillTimer(view["hwnd"], 2)
            if view["layers"]:
                present(view["stage"].frame([(*view["layers"], 1)]))
                view["region"] = bounds(*view["layers"])

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
        add(0x8 if view["sizing"] == "body" else 0, 21, "Единый размер тела")
        add(0x8 if view["idle"] else 0, 22, "Оживление позы")
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
        elif chosen == "idle":
            set_idle(not view["idle"])
        elif chosen == "sizing":
            view["sizing"] = "body" if view["sizing"] == "file" else "file"
            poses.save_view_prefs({"sizing": view["sizing"]})  # shared with the PoseStage window
            resize_to(settings["size"])
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
            if message == WM_TIMER:
                if wparam == 1 and view["anim"]:
                    tick()
                elif wparam == 2:
                    idle_tick()
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
            if message == WM_WTSSESSION_CHANGE and wparam in (7, 8):  # session lock / unlock
                view["locked"] = wparam == 7  # no animation frames while nobody can see them
                return 0
            if message == WM_DESTROY:
                try:
                    ctypes.windll.wtsapi32.WTSUnRegisterSessionNotification(hwnd)
                except (AttributeError, OSError):
                    pass
                release_dib()
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
    try:  # lock / unlock notifications pause the idle animation
        ctypes.windll.wtsapi32.WTSRegisterSessionNotification.argtypes = [wt.HWND, wt.DWORD]
        ctypes.windll.wtsapi32.WTSRegisterSessionNotification(hwnd, 0)  # NOTIFY_FOR_THIS_SESSION
        ctypes.windll.wtsapi32.WTSUnRegisterSessionNotification.argtypes = [wt.HWND]
    except (AttributeError, OSError):
        logging.warning("Session notifications unavailable: the animation keeps running while locked")
    if view["idle"]:
        set_idle(True)
    logging.info("Desktop mode: %d poses, %d in rotation, size %s", len(items), len(cycle), settings["size"])

    message = wt.MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(message))
        user32.DispatchMessageW(ctypes.byref(message))
    return 0
