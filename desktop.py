"""Desktop mode: the podium and poses right on the desktop, without a window or WebView2.

A per-pixel-alpha layered window (UpdateLayeredWindow) shows a podium pre-rendered from the
3D studio (web/desktop/podium.png + podium.json, made by tools/render_podium.py). Poses are
placed by the same rules as web/scene/placement.js: one body size for the collection and
contacts on the podium top. Fully transparent pixels let clicks through to the desktop.

Mouse: drag moves the podium, wheel changes the pose, double-click opens the PoseStage window,
right-click shows the menu (with «Танцы»: a dance phrase played on the podium). Keys (when
focused): ← → / A D, Home / End, Space pauses a dance, Esc stops it, else closes.
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

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

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


JOINT_AXIS = ("head", "chest", "navel", "pelvis")
JOINT_IDS = JOINT_AXIS + tuple(f"{n}_{s}" for n in ("shoulder", "elbow", "wrist", "hip", "knee", "foot") for s in "lr")
# Template skeletons in box coordinates (u, v), the figure's left first (web/scene/idle.js).
_FRONT = {"head": (.5, .07), "chest": (.5, .24), "navel": (.5, .4), "pelvis": (.5, .5),
          "shoulder": ((.64, .2), (.36, .2)), "elbow": ((.7, .35), (.3, .35)), "wrist": ((.72, .48), (.28, .48)),
          "hip": ((.57, .52), (.43, .52)), "knee": ((.57, .74), (.43, .74)), "foot": ((.57, .96), (.43, .96))}
_FAR_SIDE = ("shoulder_r", "elbow_r", "wrist_r", "hip_r", "knee_r", "foot_r")
TEMPLATES = {
    "front": _FRONT,
    "back": {k: (v[1], v[0]) if isinstance(v[0], tuple) else v for k, v in _FRONT.items()},
    "side": {"head": (.5, .07), "chest": (.53, .24), "navel": (.51, .4), "pelvis": (.48, .5),
             "shoulder": ((.5, .2), (.47, .2)), "elbow": ((.5, .36), (.47, .36)), "wrist": ((.5, .5), (.47, .5)),
             "hip": ((.49, .52), (.47, .52)), "knee": ((.5, .74), (.47, .74)), "foot": ((.5, .96), (.47, .96)),
             "hidden": _FAR_SIDE},
    "sit": {"head": (.5, .1), "chest": (.5, .3), "navel": (.5, .45), "pelvis": (.5, .6),
            "shoulder": ((.6, .27), (.4, .27)), "elbow": ((.68, .42), (.32, .42)), "wrist": ((.72, .6), (.28, .6)),
            "hip": ((.56, .62), (.44, .62)), "knee": ((.75, .75), (.25, .75)), "foot": ((.85, .95), (.15, .95)),
            "support": ("pelvis",)},
    "lie": {"head": (.08, .5), "chest": (.25, .5), "navel": (.4, .52), "pelvis": (.52, .55),
            "shoulder": ((.23, .42), (.23, .58)), "elbow": ((.35, .4), (.35, .6)), "wrist": ((.45, .4), (.45, .6)),
            "hip": ((.53, .5), (.53, .6)), "knee": ((.72, .5), (.72, .6)), "foot": ((.92, .55), (.92, .62)),
            "hidden": _FAR_SIDE, "support": ("pelvis",)},
}
TEMPLATE_VIEW = {"front": "front", "side": "side", "back": "back", "sit": "front", "lie": "side"}


def template_joints(pose, kind):
    """A skeleton of `kind` ("front", "side", "back", "sit", "lie") fitted to the pose box."""
    template = TEMPLATES.get(kind, TEMPLATES["front"])
    x1, y1, x2, y2 = pose["box"]

    def at(uv):
        return [round((x1 + uv[0] * (x2 - x1)) * 10) / 10, round((y1 + uv[1] * (y2 - y1)) * 10) / 10]
    points = {name: {"xy": at(template[name]), "state": "visible"} for name in JOINT_AXIS}
    for name in ("shoulder", "elbow", "wrist", "hip", "knee", "foot"):
        for i, side in enumerate("lr"):
            key = f"{name}_{side}"
            points[key] = {"xy": at(template[name][i]),
                           "state": "hidden" if key in template.get("hidden", ()) else "visible"}
    for key in template.get("support", ()):
        points[key]["support"] = True
    return {"version": 2, "view": TEMPLATE_VIEW.get(kind, "front"), "points": points}


def estimate_joints(pose):
    """The skeleton to use when a pose has none: a template from the support mode / box."""
    x1, y1, x2, y2 = pose["box"]
    if (x2 - x1) / max(1, y2 - y1) > 1.4:
        return template_joints(pose, "lie")
    mode = (pose.get("support") or {}).get("mode")
    return template_joints(pose, "sit" if mode in ("kneel", "ground") else "front")


class IdleRig:
    """Displacement field (source px, y down) from the skeleton version 2: breathing, weight
    shift, head and free hands; supports stay pinned. Same math as createRig() in idle.js."""

    def __init__(self, pose):
        joints = pose.get("joints")
        joints = joints if isinstance(joints, dict) and joints.get("version") == 2 else estimate_joints(pose)
        points = joints["points"]

        def xy(key):
            return tuple(points[key]["xy"]) if points.get(key, {}).get("xy") else None

        def visible(key):
            return points.get(key, {}).get("state") == "visible" and xy(key) is not None
        head, chest, pelvis = xy("head"), xy("chest"), xy("pelvis")
        self.head, self.pelvis = head, pelvis
        self.torso = max(1.0, math.hypot(chest[0] - pelvis[0], chest[1] - pelvis[1]))
        self.axis = ((chest[0] - pelvis[0]) / self.torso, (chest[1] - pelvis[1]) / self.torso)
        lying = abs(self.axis[1]) < .5
        support_mode = (pose.get("support") or {}).get("mode")
        sitting = support_mode in ("kneel", "ground")
        view = joints.get("view", "front")
        self.sway = 0.0 if lying else IDLE_SWAY * (.6 if view == "side" else 1) * (.8 if sitting else 1)
        lift = (0.0, -1.0) if lying else self.axis
        if view == "side" and not lying:
            length = max(1.0, math.hypot(head[0] - pelvis[0], head[1] - pelvis[1]))
            line = ((head[0] - pelvis[0]) / length, (head[1] - pelvis[1]) / length)
            offset = (chest[0] - pelvis[0], chest[1] - pelvis[1])
            along = offset[0] * line[0] + offset[1] * line[1]
            perp = (offset[0] - line[0] * along, offset[1] - line[1] * along)
            size = math.hypot(*perp)
            forward = (perp[0] / size, perp[1] / size) if size > 1e-3 else (-self.axis[1], self.axis[0])
            raw = (self.axis[0] + forward[0] * .5, self.axis[1] + forward[1] * .5)
            norm = math.hypot(*raw)
            lift = (raw[0] / norm, raw[1] / norm)  # as strong as in front view, tilted forward
        self.lift = lift
        self.neck = (chest[0] + (head[0] - chest[0]) * .35, chest[1] + (head[1] - chest[1]) * .35)
        self.head_radius = max(math.hypot(head[0] - chest[0], head[1] - chest[1]) * 1.25, self.torso * .45)
        self.neck_along = self.along(*self.neck)
        if visible("shoulder_l") and visible("shoulder_r"):
            (ax, ay), (bx, by) = xy("shoulder_l"), xy("shoulder_r")
            width = max(1.0, math.hypot(ax - bx, ay - by))
        else:
            width = self.torso * .6
        self.hands = []
        for side in "lr":
            if not (visible(f"elbow_{side}") and visible(f"wrist_{side}")) or points[f"wrist_{side}"].get("support") is True:
                continue
            elbow, wrist = xy(f"elbow_{side}"), xy(f"wrist_{side}")
            reach = math.hypot(wrist[0] - elbow[0], wrist[1] - elbow[1])
            from_axis = abs((wrist[0] - pelvis[0]) * self.axis[1] - (wrist[1] - pelvis[1]) * self.axis[0])
            if reach > width * .25 and from_axis > width * .9:
                self.hands.append((elbow, wrist))
        self.pin = .06 * (pose["box"][3] - pose["box"][1])
        released = [xy(k) for k in JOINT_IDS if points.get(k, {}).get("support") is False and xy(k)]
        self.pins = [c for c in support_data(pose)[0]
                     if all(math.hypot(c[0] - rx, c[1] - ry) > self.pin * 1.5 for rx, ry in released)]
        self.pins += [list(xy(k)) for k in JOINT_IDS if points.get(k, {}).get("support") is True and xy(k)]

    def along(self, x, y):
        return ((x - self.pelvis[0]) * self.axis[0] + (y - self.pelvis[1]) * self.axis[1]) / self.torso

    def lock_at(self, x, y):
        nearest = min((math.hypot(x - cx, y - cy) for cx, cy in self.pins), default=math.inf)
        return 1 - smooth(self.pin * .5, self.pin * 1.6, nearest)

    def displace(self, x, y, t, lock=None):
        lock = self.lock_at(x, y) if lock is None else lock
        if lock >= 1:
            return 0.0, 0.0
        w = 2 * math.pi / IDLE_LOOP
        breath, swing = math.sin(2 * w * t), math.sin(w * t)
        nod = math.sin(w * t + 1.3) + .35 * math.sin(3 * w * t + .4)
        s = self.along(x, y)
        nx, ny = x, y
        if self.sway:
            weight = smooth(-.12, .25, s)
            rx, ry = rotate(nx, ny, *self.pelvis, self.sway * swing)
            nx, ny = nx + (rx - nx) * weight, ny + (ry - ny) * weight
        rise = IDLE_LIFT * self.torso * (breath * .5 + .5) * smooth(.15, .85, s)
        nx, ny = nx + self.lift[0] * rise, ny + self.lift[1] * rise
        distance = math.hypot(x - self.head[0], y - self.head[1])
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


# --- Dances (docs/DANCE.md, stage 5) -------------------------------------------------------------
DANCE_FPS = 20
DANCE_GRID = 14  # warp mesh cell, display px (coarser than the window's: frames are made on the CPU)
DANCE_BPMS = (90, 100, 110, 120, 130, 140)
DANCE_SPEEDS = (1, .5, .25)


def display_joints(prepared):
    """Skeleton points of a prepared pose in stage pixels (an estimated skeleton if it has none)."""
    pose, k = prepared["pose"], prepared["per_source"]
    joints = pose.get("joints") if (pose.get("joints") or {}).get("version") == 2 else estimate_joints(pose)
    x1, y1 = pose["box"][:2]
    return {key: (prepared["left"] + (point["xy"][0] - x1) * k, prepared["top"] + (point["xy"][1] - y1) * k)
            for key, point in joints["points"].items() if point.get("xy") and point.get("state") != "absent"}


class DanceFrames:
    """Every frame of one cycle of a dance phrase on a Stage, made once in a background thread
    (a bone-driven frame costs ~0.1 s on the CPU; playback then only composites). Frame i shows
    dance time i / fps as (figure Sprite, shadow Sprite)."""

    def __init__(self, stage, entries, loop, style="soft", bpm=120, fps=DANCE_FPS):
        import dance_motion
        self.motion = dance_motion
        self.stage, self.style, self.fps = stage, style if style in dance_motion.STYLES else "soft", fps
        self.beat = 60 / bpm
        self.prepared = [stage.prepare(pose, image) for pose, image in entries]
        self.order = dance_motion.cycle(len(self.prepared), loop)
        self.count = max(1, round((len(self.order) - 1) * self.beat * fps))
        self.frames = [None] * self.count
        self.ready = 0  # frames made, in order
        self.cancelled = False
        self.error = None
        self.region = None  # union of every frame's sprites (the part of the window that changes)

    def progress(self) -> float:
        return self.ready / self.count

    def done(self) -> bool:
        return self.region is not None  # set after the last frame

    def canvas(self, prepared):
        canvas = Image.new("RGBA", self.stage.size, (0, 0, 0, 0))
        canvas.alpha_composite(prepared["figure"], (prepared["left"], prepared["top"]))
        return canvas

    def sprite(self, layer):
        box = layer.getbbox()
        if box is None:
            return Sprite(Image.new("RGBA", (1, 1), (0, 0, 0, 0)), 0, 0)
        return Sprite(layer.crop(box), box[0], box[1])

    def render(self):
        """Runs in a thread; stops early when `cancelled` is set."""
        try:
            motion = self.motion
            share, curve_name = motion.STYLES[self.style]
            ease = motion.curve(curve_name)
            canvases = [self.canvas(p) for p in self.prepared]
            joints = [display_joints(p) for p in self.prepared]
            still = {}  # pose index → (figure, shadow) while it holds (soft style)
            move = self.beat * share
            step_frames = self.beat * self.fps
            for step, (a, b) in enumerate(zip(self.order, self.order[1:])):
                transition = motion.Transition(canvases[a], joints[a], canvases[b], joints[b], grid=DANCE_GRID)
                first, last = round(step * step_frames), round((step + 1) * step_frames)
                for i in range(first, min(last, self.count)):
                    if self.cancelled:
                        return
                    t = i / self.fps - step * self.beat
                    u = t / move
                    if u >= 1 and self.style == "soft":
                        if b not in still:
                            figure = Sprite(self.prepared[b]["figure"], self.prepared[b]["left"], self.prepared[b]["top"])
                            still[b] = (figure, self.stage.cast_shadow(figure))
                        self.frames[i] = still[b]
                    else:
                        target = transition.skeleton(u, ease)
                        if self.style == "groove":
                            target = motion.groove(target, i / self.fps, self.beat, transition.height)
                        figure = self.sprite(transition.layer(target, u, ease))
                        self.frames[i] = (figure, self.stage.cast_shadow(figure))
                    self.ready = i + 1
            sprites = [sprite for pair in self.frames for sprite in pair]
            width, height = self.stage.size
            self.region = (max(0, min(s.x for s in sprites)), max(0, min(s.y for s in sprites)),
                           min(width, max(s.x + s.image.width for s in sprites)),
                           min(height, max(s.y + s.image.height for s in sprites)))
        except Exception as error:  # noqa: BLE001 — shown as "not available", logged with the traceback
            logging.exception("Dance frames failed")
            self.error = error
            self.cancelled = True

    def at(self, seconds: float):
        """(figure, shadow) at dance time `seconds` (the cycle repeats)."""
        return self.frames[int(seconds * self.fps) % self.count]


def progress_frame(stage, prepared, progress: float) -> Image.Image:
    """The first pose on the podium with a thin bar under it while the dance is being made."""
    figure = Sprite(prepared["figure"], prepared["left"], prepared["top"])
    frame = stage.frame([(figure, stage.cast_shadow(figure), 1)])
    width, height = stage.size
    bar_w, bar_h = round(width * .34), max(3, round(height * .007))
    x, y = (width - bar_w) // 2, round(height * .955)
    draw = ImageDraw.Draw(frame)
    draw.rounded_rectangle((x, y, x + bar_w, y + bar_h), radius=bar_h, fill=(40, 42, 46, 150))
    if progress > 0:
        draw.rounded_rectangle((x, y, x + max(bar_h, round(bar_w * progress)), y + bar_h), radius=bar_h,
                               fill=(235, 238, 242, 230))
    return frame


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
            22: "idle", 30: "window", 31: "close", 40: "dance_stop", 41: "dance_pause", 42: "soft", 43: "groove"}
    DANCE_BPM_IDS = {50 + i: bpm for i, bpm in enumerate(DANCE_BPMS)}
    DANCE_SPEED_IDS = {60 + i: speed for i, speed in enumerate(DANCE_SPEEDS)}
    DANCE_PHRASE_ID = 1000  # + index in the menu's phrase list

    view = {"stage": None, "index": None, "layers": None, "anim": None, "frame": None, "hwnd": None,
            "sizing": prefs.get("sizing") if prefs.get("sizing") in ("body", "file") else "file",
            "idle": prefs.get("idle") is True, "prepared": None, "region": None, "idle_shadow": None,
            "dance": None}
    dance_prefs = {"style": "soft", "bpm": 120, "speed": 1, **poses.clean_dance_prefs(prefs.get("dance"))}
    by_file = {pose["file"]: i for i, pose in enumerate(items)}
    api.get_dances()  # starts the phrase search in the background (seconds once outfits are cached)
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
        stop_dance(redraw=False)
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
        if view["dance"]:
            stop_dance()
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
        if (view["anim"] or view["dance"] or not view["idle"] or view.get("locked") or prepared is None
                or not dib.get("memory")):
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
        dancing = view["dance"]
        show(view["index"], animate=False)
        if dancing:  # the frames were made for the old size: make them again
            start_dance(dancing["name"], dancing["entries"], dancing["loop"])
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
        phrases = dance_menu(handle)
        add(0x800, 0, None)
        add(0, 30, "Открыть окно PoseStage")
        add(0, 31, "Закрыть\tEsc")
        if x == -1 and y == -1:  # keyboard (Shift+F10 / menu key): open at the podium's centre
            rect = wt.RECT()
            user32.GetWindowRect(view["hwnd"], ctypes.byref(rect))
            x, y = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
        user32.SetForegroundWindow(view["hwnd"])
        raw = user32.TrackPopupMenu(handle, 0x100 | 0x2, x, y, 0, view["hwnd"], None)
        chosen = MENU.get(raw)
        user32.PostMessageW(view["hwnd"], 0, 0, 0)  # WM_NULL: lets the menu close reliably
        user32.DestroyMenu(handle)  # with its submenus
        if raw >= DANCE_PHRASE_ID and raw - DANCE_PHRASE_ID < len(phrases):
            start_dance(*phrases[raw - DANCE_PHRASE_ID])
        elif raw in DANCE_BPM_IDS:
            set_dance_pref("bpm", DANCE_BPM_IDS[raw])
        elif raw in DANCE_SPEED_IDS:
            set_dance_pref("speed", DANCE_SPEED_IDS[raw])
        elif chosen in ("soft", "groove"):
            set_dance_pref("style", chosen)
        elif chosen == "dance_stop":
            stop_dance()
        elif chosen == "dance_pause":
            pause_dance()
        elif chosen == "next":
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

    # --- Dances: phrases from the PoseStage window's search and the user's own --------------------
    def dance_phrases():
        """(status, [(name, [(file, mirror)], loop)]): own phrases first, then the found ones."""
        out = []
        for sequence in api.get_sequences():
            entries = [(e["file"], e["mirror"]) for e in sequence["poses"] if e["file"] in by_file]
            if len(entries) >= 2:
                out.append(("★ " + sequence["name"], entries, sequence["loop"]))
        found = api.get_dances()
        for phrase in found["phrases"]:
            out.append((phrase["name"], [(e["file"], e["mirror"]) for e in phrase["poses"]], phrase["loop"]))
        return found["status"], out[:40]

    def dance_menu(handle):
        """Adds the «Танцы» submenu; returns the phrases in menu order."""
        sub = user32.CreatePopupMenu()
        add = lambda flags, ident, text: user32.AppendMenuW(sub, flags, ident, text)
        status, phrases = dance_phrases()
        dancing = view["dance"]
        if dancing:
            add(0, 40, "Остановить танец\tEsc")
            add(0, 41, ("Продолжить" if dancing["paused"] else "Пауза") + "\tПробел")
            add(0x800, 0, None)
        for i, (name, entries, loop) in enumerate(phrases):
            playing = dancing and dancing["name"] == name
            add(0x8 if playing else 0, DANCE_PHRASE_ID + i, f"{name} — {len(entries)}{' ↻' if loop else ''}")
        if status == "working":
            add(0x1, 0, "Ищу связки…")
        elif status == "error":
            add(0x1, 0, "Связки не найдены (ошибка)")
        elif not phrases:
            add(0x1, 0, "Связок нет")
        add(0x800, 0, None)
        add(0x8 if dance_prefs["style"] == "soft" else 0, 42, "Плавно")
        add(0x8 if dance_prefs["style"] == "groove" else 0, 43, "Ритм: качание, замах и отдача")
        add(0x800, 0, None)
        for ident, bpm in DANCE_BPM_IDS.items():
            add(0x8 if dance_prefs["bpm"] == bpm else 0, ident, f"{bpm} BPM")
        add(0x800, 0, None)
        for ident, speed in DANCE_SPEED_IDS.items():
            add(0x8 if dance_prefs["speed"] == speed else 0, ident, f"Скорость ×{str(speed).replace('.', ',')}")
        user32.AppendMenuW(handle, 0x10, sub, "Танцы")  # MF_POPUP
        return phrases

    def start_dance(name, entries, loop):
        """Makes the phrase's frames in the background (a bar under the podium shows how far),
        then plays them in a loop."""
        stop_dance(redraw=False)
        user32.KillTimer(view["hwnd"], 1)  # a pose crossfade would draw over the dance
        if view["anim"]:
            view["anim"], view["pending"] = None, 0
        stage = view["stage"]
        posed = []
        for file, mirror in entries:
            index = by_file.get(file)
            if index is None:
                continue
            pose, image = items[index], load_image(index)
            if mirror:
                import dance
                pose, image = dance.mirror_pose(pose), ImageOps.mirror(image)
            posed.append((pose, image))
        if len(posed) < 2:
            return
        frames = DanceFrames(stage, posed, loop, dance_prefs["style"], dance_prefs["bpm"])
        threading.Thread(target=frames.render, daemon=True, name="dance-frames").start()
        view["dance"] = {"name": name, "entries": entries, "loop": loop, "frames": frames, "clock": 0.0,
                         "last": None, "paused": False, "shown": -1, "progress_at": 0.0}
        user32.SetTimer(view["hwnd"], 3, 1000 // DANCE_FPS, None)
        dance_tick()
        logging.info("Desktop dance: %s, %d poses", name, len(posed))

    def stop_dance(redraw=True):
        dancing, view["dance"] = view["dance"], None
        if not dancing:
            return
        dancing["frames"].cancelled = True
        user32.KillTimer(view["hwnd"], 3)
        if redraw and view["layers"]:
            present(view["stage"].frame([(*view["layers"], 1)]))
            view["region"], view["live"] = bounds(*view["layers"]), None
            if view["idle"]:
                view["idle_start"] = time.perf_counter()

    def pause_dance():
        if view["dance"]:
            view["dance"]["paused"] = not view["dance"]["paused"]

    def set_dance_pref(key, value):
        dance_prefs[key] = value
        poses.save_view_prefs({"dance": {key: value}})  # shared with the PoseStage window
        dancing = view["dance"]
        if dancing and key in ("style", "bpm"):  # other frames: make them again
            start_dance(dancing["name"], dancing["entries"], dancing["loop"])

    def dance_tick():
        dancing = view["dance"]
        if not dancing or view.get("locked") or not dib.get("memory"):
            return
        frames, stage, now = dancing["frames"], view["stage"], time.perf_counter()
        if frames.error is not None:
            stop_dance()
            return
        if not frames.done():
            if now - dancing["progress_at"] >= .25:  # the bar: a few updates a second are enough
                dancing["progress_at"] = now
                present(progress_frame(stage, frames.prepared[0], frames.progress()))
            return
        if dancing["last"] is None:  # first frame after making them: redraw all, then patches
            dancing["last"] = now
            figure, shadow = frames.at(0)
            present(stage.frame([(figure, shadow, 1)]))
            return
        if not dancing["paused"]:
            dancing["clock"] += (now - dancing["last"]) * dance_prefs["speed"]
        dancing["last"] = now
        index = int(dancing["clock"] * frames.fps) % frames.count
        if index == dancing["shown"]:
            return
        dancing["shown"] = index
        figure, shadow = frames.frames[index]
        left, top, right, bottom = frames.region
        patch = stage.podium.crop((left, top, right, bottom))
        place(patch, shadow.image, shadow.x - left, shadow.y - top)
        place(patch, figure.image, figure.x - left, figure.y - top)
        present_patch(patch, left, top)

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
                elif wparam == 0x20 and view["dance"]:  # Space
                    pause_dance()
                elif wparam == 0x1B:  # Esc: stops a dance, else closes
                    if view["dance"]:
                        stop_dance()
                    else:
                        user32.DestroyWindow(hwnd)
                return 0
            if message == WM_TIMER:
                if wparam == 1 and view["anim"]:
                    tick()
                elif wparam == 2:
                    idle_tick()
                elif wparam == 3:
                    dance_tick()
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
                stop_dance(redraw=False)
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
