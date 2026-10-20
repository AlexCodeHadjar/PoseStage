"""Read-only pose discovery and alpha bounds. No writes to the image directory."""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

from PIL import Image

POSE_RE = re.compile(r"^photo-\d{6}__pose-\d{2}\.png$", re.IGNORECASE)
DEFAULT_FOLDER = Path(r"C:\Users\Alast\Desktop\композиции\позы скетчи\позы скетчи painterest\after codex poses real photo")
DATA_DIR = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / "PoseStage"
_write_lock = threading.Lock()
SUPPORT_VERSION = 1


def read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def write_json(path: Path, data):
    """All callers use DATA_DIR; atomic replacement also survives forced exit."""
    with _write_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)


def valid_entry(value):
    if not isinstance(value, dict):
        return False
    w, h, box = value.get("w"), value.get("h"), value.get("box")
    if not (type(w) is int and type(h) is int and isinstance(box, list)
            and len(box) == 4 and all(type(n) is int for n in box)):
        return False
    support = value.get("support", {})
    if not (0 <= box[0] < box[2] <= w and 0 <= box[1] < box[3] <= h
            and isinstance(support, dict) and support.get("version") == SUPPORT_VERSION
            and support.get("mode") in ("feet", "kneel", "ground")
            and support.get("cut") == {"feet": .93, "kneel": .78, "ground": .55}[support["mode"]]
            and isinstance(support.get("profile"), list) and 0 < len(support["profile"]) <= 200):
        return False
    previous_x = -1
    for point in support["profile"]:
        if not (isinstance(point, list) and len(point) == 2
                and all(type(n) in (int, float) and math.isfinite(n) for n in point)
                and box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]
                and point[0] > previous_x):
            return False
        previous_x = point[0]
    return True


def support_profile(mask, box, mode):
    """Lower silhouette in source pixels, not the transparent square's bottom."""
    x1, y1, x2, y2 = box
    step = max(1, (x2-x1)//96)
    profile = []
    for x in range(x1, x2, step):
        right = min(x2, x+step)
        band = mask.crop((x, y1, right, y2)).getbbox()
        if band:
            profile.append([(x+right)/2, y1+band[3]])
    cut = {"feet": .93, "kneel": .78, "ground": .55}[mode]
    return {"version": SUPPORT_VERSION, "mode": mode, "cut": cut, "profile": profile}


def pose_files(folder: Path) -> list[Path]:
    try:
        root = folder.resolve()
        return sorted(
            (p for p in root.iterdir() if POSE_RE.fullmatch(p.name)
             and p.is_file() and p.resolve().parent == root),
            key=lambda p: (p.name.casefold(), p.name),
        )
    except OSError:
        return []


def find_folder(executable_dir: Path) -> Path | None:
    config = read_json(DATA_DIR / "config.json", {})
    saved = config.get("poses_folder") if isinstance(config, dict) else None
    candidates = ([Path(saved)] if isinstance(saved, str) else []) + [executable_dir, DEFAULT_FOLDER]
    for folder in candidates:
        if pose_files(folder):
            return folder.resolve()
    return None


class PoseCatalog:
    def __init__(self, folder: Path | None, prefix: str):
        self.folder = folder
        self.prefix = prefix
        self.items: list[dict] = []
        self.allowed: dict[str, Path] = {}
        self.ready = threading.Event()
        self.lock = threading.RLock()

    def scan(self):
        self.ready.clear()
        try:
            cached = read_json(DATA_DIR / "poses_cache.json", {})
            cached = cached if isinstance(cached, dict) else {}
            files = pose_files(self.folder) if self.folder else []
            entries = {}
            resources = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
            modes = read_json(resources / "web/scene/pose_support_modes.json", {})

            def inspect(path):
                try:
                    stat = path.stat()
                    key = f"{path.parent}|{path.name}|{stat.st_size}|{stat.st_mtime_ns}"
                    value = cached.get(key)
                    expected_mode = modes.get(path.name.casefold())
                    if not valid_entry(value) or (expected_mode in ("feet", "kneel", "ground")
                                                  and value["support"]["mode"] != expected_mode):
                        with Image.open(path) as im:
                            im.load()  # detect truncated image data as well as broken headers
                            if im.format != "PNG":
                                raise ValueError("File is not PNG")
                            rgba = im.convert("RGBA")
                            mask = rgba.getchannel("A").point(lambda a: 255 if a > 8 else 0)
                            box = mask.getbbox()
                            if box is None:
                                raise ValueError("Empty alpha silhouette")
                            mode = modes.get(path.name.casefold())
                            if mode not in ("feet", "kneel", "ground"):
                                mode = "ground" if (box[2]-box[0])/(box[3]-box[1]) > 1.05 else "feet"
                            value = {"box": list(box), "w": im.width, "h": im.height,
                                     "support": support_profile(mask, box, mode)}
                    return key, value, path
                except Exception:
                    logging.exception("Skipping unreadable pose: %s", path)
                    return None

            # PIL releases the GIL while decoding PNGs. Preserve input sort order.
            with ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 1)) as pool:
                results = list(pool.map(inspect, files))
            items, allowed = [], {}
            for result in results:
                if result is None:
                    continue
                key, value, path = result
                entries[key] = value
                items.append({"url": f"{self.prefix}/poses/{quote(path.name)}", "file": path.name, **value})
                allowed[path.name] = path
            with self.lock:
                self.items, self.allowed = items, allowed
            if entries != cached:
                write_json(DATA_DIR / "poses_cache.json", entries)
        except Exception:
            logging.exception("Pose scan failed")
        finally:
            self.ready.set()

    def get(self):
        self.ready.wait()
        with self.lock:
            return list(self.items)
