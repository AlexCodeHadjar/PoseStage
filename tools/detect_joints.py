"""Finds body joints for every pose and writes web/scene/pose_joints.json (pose animation).

Dev-only: needs `pip install mediapipe==0.10.14 "numpy<2"` in a separate environment; the
program itself only reads the JSON. Usage:
    python tools/detect_joints.py "D:\\Позы"
Each pose is tried with several paddings / backgrounds until MediaPipe Pose finds a skeleton.
Poses without one fall back to the silhouette-based rig at run time.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import mediapipe as mp
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "web" / "scene" / "pose_joints.json"
VARIANTS = [(2, 0, 255), (2, 256, 255), (2, 256, 128), (1, 256, 255), (1, 512, 200), (2, 512, 90)]
NAMES = {"nose": [0], "shoulders": [11, 12], "elbows": [13, 14], "wrists": [15, 16], "hips": [23, 24]}


def render(image, pad, gray):
    canvas = Image.new("RGBA", (image.width + 2 * pad, image.height + 2 * pad), (gray, gray, gray, 255))
    canvas.alpha_composite(image, (pad, pad))
    return np.asarray(canvas.convert("RGB"))


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
            joints = {name: [[round(points[i][0], 1), round(points[i][1], 1)] for i in ids] for name, ids in NAMES.items()}
            joints["nose"] = joints["nose"][0]
            joints["visibility"] = round(sum(points[i][2] for ids in NAMES.values() for i in ids) / 9, 3)
            result[path.name.casefold()] = joints
            break
        print(f"{path.name}: {'ok' if path.name.casefold() in result else 'not found'}", flush=True)
    OUT.write_text(json.dumps({"version": 1, "poses": result}, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    print(f"{len(result)} of {len(files)} poses -> {OUT}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
