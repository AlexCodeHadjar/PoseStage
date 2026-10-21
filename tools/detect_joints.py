"""Finds body joints for every pose and writes web/scene/pose_joints.json (skeleton version 2,
docs/SKELETON.md) for the pose animation and the skeleton editor.

Dev-only: needs `pip install mediapipe==0.10.14 "numpy<2"` in a separate environment; the
program itself only reads the JSON. Usage:
    python tools/detect_joints.py "D:\\Позы"
Each pose is tried with several paddings / backgrounds until MediaPipe Pose finds a skeleton.
Poses without one fall back to a template skeleton at run time (the editor can fix both).
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import sys

import mediapipe as mp
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "web" / "scene" / "pose_joints.json"
VARIANTS = [(2, 0, 255), (2, 256, 255), (2, 256, 128), (1, 256, 255), (1, 512, 200), (2, 512, 90)]
# MediaPipe landmark per skeleton point (the figure's own left / right).
LANDMARKS = {"shoulder_l": 11, "shoulder_r": 12, "elbow_l": 13, "elbow_r": 14, "wrist_l": 15, "wrist_r": 16,
             "hip_l": 23, "hip_r": 24, "knee_l": 25, "knee_r": 26, "foot_l": 27, "foot_r": 28}


def render(image, pad, gray):
    canvas = Image.new("RGBA", (image.width + 2 * pad, image.height + 2 * pad), (gray, gray, gray, 255))
    canvas.alpha_composite(image, (pad, pad))
    return np.asarray(canvas.convert("RGB"))


def on_silhouette(alpha, x, y, radius=4):
    """Is there drawing (alpha > 8) at or right around (x, y)?"""
    box = (max(0, int(x) - radius), max(0, int(y) - radius), int(x) + radius + 1, int(y) + radius + 1)
    region = alpha.crop(box)
    return region.getbbox() is not None and max(region.getdata()) > 8


def skeleton(points, image):
    """MediaPipe landmarks (x, y, visibility in source px) → skeleton version 2."""
    alpha = image.getchannel("A")
    w, h = image.size

    def mid(a, b):
        return [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]

    def lerp(a, b, t):
        return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]
    xy = {name: [points[i][0], points[i][1]] for name, i in LANDMARKS.items()}
    pelvis = mid(xy["hip_l"], xy["hip_r"])
    chest = lerp(mid(xy["shoulder_l"], xy["shoulder_r"]), pelvis, .15)
    out = {"head": {"xy": [points[0][0], points[0][1]], "state": "visible"},
           "chest": {"xy": chest, "state": "visible"},
           "navel": {"xy": lerp(chest, pelvis, .55), "state": "visible"},
           "pelvis": {"xy": pelvis, "state": "visible"}}
    for name, i in LANDMARKS.items():
        x, y, visibility = points[i]
        if not (0 <= x < w and 0 <= y < h):
            out[name] = {"state": "absent"}
            continue
        # MediaPipe under-rates visibility on drawings: the silhouette decides, unless the
        # model is nearly sure the point is covered.
        visible = visibility >= .2 and on_silhouette(alpha, x, y)
        out[name] = {"xy": [x, y], "state": "visible" if visible else "hidden"}
    for point in out.values():
        if "xy" in point:
            point["xy"] = [round(point["xy"][0], 1), round(point["xy"][1], 1)]
    torso = math.hypot(chest[0] - pelvis[0], chest[1] - pelvis[1]) or 1
    shoulders = math.hypot(xy["shoulder_l"][0] - xy["shoulder_r"][0], xy["shoulder_l"][1] - xy["shoulder_r"][1])
    if shoulders < .25 * torso:
        view = "side"
    elif shoulders < .55 * torso:
        view = "three_quarter"
    else:
        # MediaPipe's left / right labels are unreliable on drawings; a face that cannot be seen
        # while the ears or shoulders can is a much better sign of a back view.
        face = max(points[i][2] for i in (0, 2, 5))  # nose, eyes
        behind = max(points[i][2] for i in (7, 8, 11, 12))  # ears, shoulders
        view = "back" if face < .3 and behind >= .6 else "front"
    return {"version": 2, "view": view, "points": out}


def main(folder: Path):
    models = {c: mp.solutions.pose.Pose(static_image_mode=True, model_complexity=c) for c in (1, 2)}
    result = {}
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() == ".png")
    for path in files:
        image = Image.open(path).convert("RGBA")
        for complexity, pad, gray in VARIANTS:
            found = models[complexity].process(render(image, pad, gray))
            if not found.pose_landmarks:
                continue
            width, height = image.width + 2 * pad, image.height + 2 * pad
            points = [(p.x * width - pad, p.y * height - pad, p.visibility) for p in found.pose_landmarks.landmark]
            result[path.name.casefold()] = skeleton(points, image)
            break
        print(f"{path.name}: {'ok' if path.name.casefold() in result else 'not found'}", flush=True)
    OUT.write_text(json.dumps({"version": 2, "poses": result}, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    print(f"{len(result)} of {len(files)} poses -> {OUT}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
