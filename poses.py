"""Read-only pose discovery, alpha bounds and per-user stores.

Everything is written to DATA_DIR (%APPDATA%\\PoseStage); pose folders are never modified.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

from PIL import Image, ImageOps

EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
FORMATS = frozenset({"PNG", "JPEG", "MPO", "WEBP"})
MODES = ("feet", "kneel", "ground")
CUTS = {"feet": .93, "kneel": .78, "ground": .55}
MAX_FILES = 50_000
THUMB_SIZE = 320
THUMB_MAX_BYTES = 256 * 1024 * 1024  # LRU cap for DATA_DIR/thumbs
THUMB_GRACE, THUMB_TOUCH = 600, 300  # never prune thumbnails used within 10 min; refresh mtime every 5
MAX_TAGS, MAX_TAG_LENGTH = 16, 32
MAX_SEARCH_LENGTH = 100  # view_prefs filter.search
SCALE_LIMITS = (.5, 2.0)  # user's per-pose size correction ([ and ] keys)
DEFAULT_REFERENCE = 2.6  # stature / sqrt(silhouette area) when a collection has no standing poses
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
DATA_DIR = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / "PoseStage"
_write_lock = threading.Lock()
_store_lock = threading.RLock()  # read-modify-write of the JSON stores below
_thumb_locks = [threading.Lock() for _ in range(16)]
SUPPORT_VERSION = 1


def read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def read_dict(path: Path) -> dict:
    data = read_json(path, {})
    return data if isinstance(data, dict) else {}


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
    w, h, box, area = value.get("w"), value.get("h"), value.get("box"), value.get("area")
    if not (type(w) is int and type(h) is int and isinstance(box, list)
            and len(box) == 4 and all(type(n) is int for n in box)
            and type(area) is int and 0 < area <= w * h):
        return False
    support = value.get("support", {})
    if not (0 <= box[0] < box[2] <= w and 0 <= box[1] < box[3] <= h
            and isinstance(support, dict) and support.get("version") == SUPPORT_VERSION
            and support.get("mode") in MODES and support.get("cut") == CUTS[support["mode"]]
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
    return {"version": SUPPORT_VERSION, "mode": mode, "cut": CUTS[mode], "profile": profile}


def auto_mode(box) -> str:
    return "ground" if (box[2]-box[0])/(box[3]-box[1]) > 1.05 else "feet"


def natural_key(relative: str):
    """`pose-2` < `pose-10`, compared folder by folder, case-insensitive."""
    return tuple(tuple(int(token) if i % 2 else token for i, token in enumerate(re.split(r"(\d+)", part.casefold())))
                 for part in relative.split("/")), relative


def inside(path: Path, root: Path) -> bool:
    """True if `path` really resolves inside `root` (symlinks/junctions cannot escape)."""
    try:
        return path.resolve(strict=True).is_relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return False


def _walk(root: Path, recursive=True):
    """(relative POSIX path, path) of supported images below an already resolved root."""
    stack = [root]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if not recursive:
                            continue
                        hidden = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x6
                        if not (entry.name.startswith("_") or hidden or entry.is_junction()):
                            stack.append(Path(entry.path))
                    elif os.path.splitext(entry.name)[1].lower() in EXTENSIONS and entry.is_file():
                        path = Path(entry.path)
                        if inside(path, root):
                            yield path.relative_to(root).as_posix(), path
        except OSError:
            continue


def pose_files(folder: Path | None, recursive=True) -> list[tuple[str, Path]]:
    """Naturally sorted, then capped at MAX_FILES; hidden/service folders (`.`/`_`) are skipped."""
    try:
        root = folder.resolve(strict=True) if folder else None
        if root is None or not root.is_dir():
            return []
        found = sorted(_walk(root, recursive), key=lambda item: natural_key(item[0]))
    except (OSError, ValueError):
        return []
    if len(found) > MAX_FILES:
        logging.warning("Pose folder truncated at %d of %d files: %s", MAX_FILES, len(found), root)
    return found[:MAX_FILES]


def has_poses(folder: Path | None, recursive=True) -> bool:
    try:
        root = folder.resolve(strict=True) if folder else None
        return bool(root and root.is_dir() and next(_walk(root, recursive), None))
    except (OSError, ValueError):
        return False


def url_path(relative: str) -> str:
    return "/".join(quote(part, safe="") for part in relative.split("/"))


def bundled_modes() -> dict[str, str]:
    """web/scene/pose_support_modes.json: bare file name (casefold) -> mode."""
    data = read_dict(RESOURCE_DIR / "web/scene/pose_support_modes.json")
    return {k.casefold(): v for k, v in data.items() if isinstance(k, str) and v in MODES}


def make_pose(prefix: str, relative: str, geometry: dict, overrides: dict, bundled: dict) -> dict:
    """Pose for the frontend; support mode priority: user > bundled (top-level files only) > auto."""
    if overrides.get(relative) in MODES:
        mode, source = overrides[relative], "user"
    elif "/" not in relative and bundled.get(relative.casefold()) in MODES:
        mode, source = bundled[relative.casefold()], "bundled"
    else:
        mode, source = auto_mode(geometry["box"]), "auto"
    path = url_path(relative)
    return {"url": f"{prefix}/poses/{path}", "thumb": f"{prefix}/thumbs/{path}", "file": relative,
            "box": list(geometry["box"]), "w": geometry["w"], "h": geometry["h"], "area": geometry["area"],
            "support": {**geometry["support"], "mode": mode, "cut": CUTS[mode]}, "support_source": source}


def body_reference(poses: list[dict]) -> float:
    """Collection's stature per sqrt(silhouette area): the median over standing, transparent poses.

    The silhouette area of one body barely depends on the pose (a raised arm adds height, not area),
    so stature ≈ reference·√area gives every pose the same body size, whatever its bounding box."""
    ratios = sorted((p["box"][3] - p["box"][1]) / math.sqrt(p["area"]) for p in poses
                    if p["support"]["mode"] == "feet" and not opaque(p))
    if not ratios:
        return DEFAULT_REFERENCE
    middle = len(ratios) // 2
    return ratios[middle] if len(ratios) % 2 else (ratios[middle - 1] + ratios[middle]) / 2


def opaque(pose: dict) -> bool:
    """A photo or other picture without transparency: no cut-out silhouette to measure."""
    return pose["area"] >= .98 * pose["w"] * pose["h"]


def with_body(pose: dict, reference: float, scales: dict) -> dict:
    """Adds the estimated standing height (source px) and the user's size correction.
    Opaque pictures have no body to measure: their box height stands in for it."""
    x1, y1, x2, y2 = pose["box"]
    stature = y2 - y1 if opaque(pose) else reference * math.sqrt(pose["area"])
    return {**pose, "stature": round(stature, 3), "scale_adjust": scales.get(pose["file"], 1.0)}


# --- Collections: config.json {"collections": [{id, name, folder}], "active": id} -----------------

def collection_id(folder: Path | str) -> str:
    return hashlib.sha256(str(folder).casefold().encode("utf-8")).hexdigest()[:12]


def load_config() -> dict:
    """Validated collections config; migrates the old {"poses_folder": ...} format."""
    with _store_lock:
        raw = read_dict(DATA_DIR / "config.json")
        entries = raw.get("collections") if isinstance(raw.get("collections"), list) else []
        legacy, legacy_id = raw.get("poses_folder"), None
        if isinstance(legacy, str) and legacy.strip():
            try:
                legacy = str(Path(legacy).resolve())
                entries, legacy_id = entries + [{"folder": legacy}], collection_id(legacy)
            except (OSError, ValueError):
                logging.warning("Ignoring invalid poses_folder in config.json")
        collections, seen = [], set()
        for entry in entries:
            folder = entry.get("folder") if isinstance(entry, dict) else None
            if (not isinstance(folder, str) or not folder.strip() or "\0" in folder
                    or collection_id(folder) in seen):
                continue
            name = entry.get("name")
            name = name.strip()[:80] if isinstance(name, str) and name.strip() else Path(folder).name or folder
            collections.append({"id": collection_id(folder), "name": name, "folder": folder})
            seen.add(collection_id(folder))
        active = raw.get("active") if isinstance(raw.get("active"), str) else None
        if legacy_id and active not in seen:
            active = legacy_id
        config = {"collections": collections, "active": active if active in seen else
                  (collections[0]["id"] if collections else None)}
        if "poses_folder" in raw:
            write_json(DATA_DIR / "config.json", config)
        return config


def save_config(config: dict):
    write_json(DATA_DIR / "config.json", {"collections": config["collections"], "active": config["active"]})


def remember_collection(folder: Path) -> str:
    """Add (if new) and activate a resolved folder; returns its id."""
    with _store_lock:
        config = load_config()
        cid = collection_id(folder)
        if all(c["id"] != cid for c in config["collections"]):
            config["collections"].append({"id": cid, "name": folder.name or str(folder), "folder": str(folder)})
        config["active"] = cid
        save_config(config)
        return cid


def set_active(cid: str):
    with _store_lock:
        config = load_config()
        if any(c["id"] == cid for c in config["collections"]) and config["active"] != cid:
            config["active"] = cid
            save_config(config)


def forget_collection(cid: str) -> dict:
    """Remove a collection from config (its library/overrides stay for a later re-add)."""
    with _store_lock:
        config = load_config()
        config["collections"] = [c for c in config["collections"] if c["id"] != cid]
        if config["active"] == cid:
            config["active"] = config["collections"][0]["id"] if config["collections"] else None
        save_config(config)
        return config


def cache_folders() -> list[Path]:
    """Folders recorded in poses_cache.json keys, most frequent first."""
    counts = Counter(key.split("|", 1)[0] for key in read_dict(DATA_DIR / "poses_cache.json"))
    return [Path(folder) for folder, _ in counts.most_common() if folder and "\0" not in folder]


def find_source(executable_dir: Path | None) -> tuple[Path | None, bool]:
    """(folder, recursive): active collection → other saved collections → if none are saved, the
    folder recorded in the scan cache (upgrade from builds without collections; saved) →
    top-level images next to the EXE (pass None when not frozen; this session only) → None."""
    config = load_config()
    for entry in sorted(config["collections"], key=lambda c: c["id"] != config["active"]):
        if has_poses(Path(entry["folder"])):
            set_active(entry["id"])
            return Path(entry["folder"]), True
    if not config["collections"]:
        for folder in cache_folders():
            if has_poses(folder):
                folder = folder.resolve()
                remember_collection(folder)
                logging.info("Pose folder restored from the scan cache: %s", folder)
                return folder, True
    if executable_dir and has_poses(executable_dir, recursive=False):
        return executable_dir.resolve(), False
    return None, True


def find_folder(executable_dir: Path | None) -> Path | None:
    return find_source(executable_dir)[0]


# --- Per-collection stores: {collection id: section} ----------------------------------------

def read_section(name: str, cid: str | None) -> dict:
    section = read_dict(DATA_DIR / name).get(cid) if cid else None
    return section if isinstance(section, dict) else {}


def update_section(name: str, cid: str, change):
    """Atomic read-modify-write of one collection's section; `change` mutates it in place."""
    with _store_lock:
        data = read_dict(DATA_DIR / name)
        section = data.get(cid) if isinstance(data.get(cid), dict) else {}
        result = change(section)
        if section:
            data[cid] = section
        else:
            data.pop(cid, None)
        write_json(DATA_DIR / name, data)
        return result


def read_overrides(cid: str | None) -> dict[str, str]:
    return {k: v for k, v in read_section("support_overrides.json", cid).items() if isinstance(k, str) and v in MODES}


def set_override(cid: str, relative: str, mode: str):
    """`mode` "auto" removes the user's override."""
    def change(section):
        clean = {k: v for k, v in section.items() if isinstance(k, str) and v in MODES}
        clean.pop(relative, None)
        if mode in MODES:
            clean[relative] = mode
        section.clear()
        section.update(clean)
    update_section("support_overrides.json", cid, change)


def valid_scale(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and SCALE_LIMITS[0] <= value <= SCALE_LIMITS[1]


def read_scales(cid: str | None) -> dict[str, float]:
    return {k: float(v) for k, v in read_section("scale_overrides.json", cid).items()
            if isinstance(k, str) and valid_scale(v)}


def set_scale(cid: str, relative: str, factor: float):
    """Stores the user's size correction; 1.0 removes it."""
    def change(section):
        clean = {k: v for k, v in section.items() if isinstance(k, str) and valid_scale(v)}
        clean.pop(relative, None)
        if abs(factor - 1) > 1e-6:
            clean[relative] = round(factor, 4)
        section.clear()
        section.update(clean)
    update_section("scale_overrides.json", cid, change)


def normalize_tags(tags) -> list[str]:
    if not isinstance(tags, list):
        raise ValueError("tags must be a list of strings")
    result, seen = [], set()
    for tag in tags:
        tag = " ".join(tag.split())[:MAX_TAG_LENGTH].strip() if isinstance(tag, str) else ""
        if tag and tag.casefold() not in seen:
            seen.add(tag.casefold())
            result.append(tag)
            if len(result) == MAX_TAGS:
                break
    return result


def clean_library(section: dict) -> dict:
    favorites = section.get("favorites") if isinstance(section.get("favorites"), list) else []
    tags = section.get("tags") if isinstance(section.get("tags"), dict) else {}
    tags = {k: normalize_tags(v) for k, v in tags.items() if isinstance(k, str) and isinstance(v, list)}
    return {"favorites": list(dict.fromkeys(f for f in favorites if isinstance(f, str))),
            "tags": {k: v for k, v in tags.items() if v}}


def read_library(cid: str | None) -> dict:
    return clean_library(read_section("library.json", cid))


def toggle_favorite(cid: str, relative: str) -> bool:
    def change(section):
        library = clean_library(section)
        on = relative not in library["favorites"]
        library["favorites"] = library["favorites"] + [relative] if on else \
            [f for f in library["favorites"] if f != relative]
        section.clear()
        section.update({k: v for k, v in library.items() if v})
        return on
    return update_section("library.json", cid, change)


def set_tags(cid: str, relative: str, tags) -> list[str]:
    tags = normalize_tags(tags)

    def change(section):
        library = clean_library(section)
        library["tags"].pop(relative, None)
        if tags:
            library["tags"][relative] = tags
        section.clear()
        section.update({k: v for k, v in library.items() if v})
    update_section("library.json", cid, change)
    return tags


def read_state(cid: str | None) -> tuple[object, object]:
    """(file, index) for a collection; old {"index": N} files apply to any collection."""
    data = read_dict(DATA_DIR / "state.json")
    entries = data.get("collections") if isinstance(data.get("collections"), dict) else {}
    entry = entries.get(cid) if cid in entries else (data if "collection" not in data else {})
    entry = entry if isinstance(entry, dict) else {}
    return entry.get("file"), entry.get("index")


def write_state(cid: str, relative: str, index: int):
    with _store_lock:
        old = read_dict(DATA_DIR / "state.json").get("collections")
        entries = {k: v for k, v in (old if isinstance(old, dict) else {}).items() if isinstance(v, dict)}
        entries[cid] = {"file": relative, "index": index}
        write_json(DATA_DIR / "state.json", {"file": relative, "index": index, "collection": cid,
                                             "collections": entries})


VIEW_PREFS = {"flip": bool, "gray": bool, "on_top": bool, "compact": bool, "grid": ("off", "thirds", "proportions"),
              "parallax": ("off", "soft", "strong")}
FILTER_PREFS = {"mode": ("all", *MODES), "favorites": bool}


def _valid_pref(value, rule) -> bool:
    return type(value) is bool if rule is bool else isinstance(value, str) and value in rule


def clean_view_prefs(prefs) -> dict:
    """Only known keys with valid types survive; unknown keys are dropped."""
    if not isinstance(prefs, dict):
        return {}
    result = {k: prefs[k] for k, rule in VIEW_PREFS.items() if k in prefs and _valid_pref(prefs[k], rule)}
    raw = prefs.get("filter")
    if isinstance(raw, dict):
        filter_ = {k: raw[k] for k, rule in FILTER_PREFS.items() if k in raw and _valid_pref(raw[k], rule)}
        if "tag" in raw and (raw["tag"] is None or isinstance(raw["tag"], str)):
            tag = normalize_tags([raw["tag"]]) if raw["tag"] else []
            filter_["tag"] = tag[0] if tag else None
        if isinstance(raw.get("search"), str):
            filter_["search"] = raw["search"][:MAX_SEARCH_LENGTH]
        if filter_:
            result["filter"] = filter_
    background = clean_background_prefs(prefs.get("background"))
    if background:
        result["background"] = background
    return result


def read_view_prefs() -> dict:
    return clean_view_prefs(read_dict(DATA_DIR / "view_prefs.json"))


def save_view_prefs(prefs) -> dict:
    """Merges validated keys into the stored prefs and returns the result."""
    update = clean_view_prefs(prefs)
    with _store_lock:
        current = read_view_prefs()
        for key in ("filter", "background"):
            if key in update:
                update[key] = {**current.get(key, {}), **update[key]}
        current.update(update)
        write_json(DATA_DIR / "view_prefs.json", current)
        return current


# --- Thumbnails --------------------------------------------------------------------------------

def _cached_thumb(target: Path) -> bool:
    """True if cached; refreshes mtime (at most every THUMB_TOUCH s) so pruning drops least recently
    used first and never a thumbnail in use (see THUMB_GRACE)."""
    try:
        if time.time() - target.stat().st_mtime > THUMB_TOUCH:
            os.utime(target)
        return True
    except OSError:
        return False


def prune_thumbs(max_bytes=THUMB_MAX_BYTES):
    """Delete least recently used thumbnails beyond `max_bytes`, and stale temp files.
    Files used within THUMB_GRACE seconds are never deleted. Run after the first scan."""
    now, files = time.time(), []
    try:
        with os.scandir(DATA_DIR / "thumbs") as entries:
            for entry in entries:
                stat = entry.stat()
                used = max(stat.st_mtime, stat.st_atime)
                if entry.name.endswith(".tmp"):
                    if now - used > 3600:
                        Path(entry.path).unlink(missing_ok=True)
                elif entry.name.endswith(".png"):
                    files.append((used, stat.st_size, entry.path))
    except OSError:
        return
    total = 0
    for used, size, path in sorted(files, reverse=True):
        total += size
        if total > max_bytes and now - used > THUMB_GRACE:
            try:
                os.unlink(path)
            except OSError:  # being served right now
                pass


def thumbnail(path: Path) -> Path | None:
    """PNG preview (long side ≤ THUMB_SIZE, alpha kept) cached in DATA_DIR/thumbs, never in the pose folder."""
    try:
        stat = path.stat()
        key = hashlib.sha256(f"{path}|{stat.st_size}|{stat.st_mtime_ns}|{THUMB_SIZE}".encode("utf-8")).hexdigest()[:40]
        target = DATA_DIR / "thumbs" / f"{key}.png"
        if _cached_thumb(target):
            return target
        with _thumb_locks[int(key[:4], 16) % len(_thumb_locks)]:
            if _cached_thumb(target):
                return target
            with Image.open(path) as im:
                if im.format not in FORMATS:
                    raise ValueError(f"Unsupported image format: {im.format}")
                im.draft(None, (THUMB_SIZE * 2, THUMB_SIZE * 2))  # fast JPEG downscale; no-op otherwise
                alpha = "A" in im.getbands() or "transparency" in im.info
                image = ImageOps.exif_transpose(im).convert("RGBA" if alpha else "RGB")
            image.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.Resampling.LANCZOS)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f"{key}.{os.getpid()}.{threading.get_ident()}.tmp")
            image.save(tmp, "PNG")
            try:
                tmp.replace(target)
            except OSError:  # another process won the race and is serving it
                tmp.unlink(missing_ok=True)
                if not target.is_file():
                    raise
        return target
    except Exception:
        logging.exception("Cannot create thumbnail: %s", path)
        return None


# --- Catalog -----------------------------------------------------------------------------------

class PoseCatalog:
    def __init__(self, folder: Path | None, prefix: str, recursive=True):
        self.folder = folder  # resolved by scan()
        self.prefix = prefix
        self.recursive = recursive  # False only for the session-only EXE-folder fallback
        self.items: list[dict] = []
        self.allowed: dict[str, Path] = {}
        self.reference = DEFAULT_REFERENCE  # body_reference() of the current items
        self.ready = threading.Event()
        self.lock = threading.RLock()
        self._scan_lock = threading.Lock()

    @property
    def id(self) -> str | None:
        with self.lock:
            return collection_id(self.folder) if self.folder else None

    def scan(self, folder: Path | None = None):
        """Scan `folder` (a collection: recursive) or the current one, then swap folder, poses and
        allowlist at once."""
        with self._scan_lock:
            self.ready.clear()
            try:
                recursive = True if folder else self.recursive
                root = folder or self.folder
                root = root.resolve() if root else None
                items, allowed, reference = self._build(root, recursive)
                with self.lock:
                    self.folder, self.items, self.allowed, self.recursive = root, items, allowed, recursive
                    self.reference = reference
            except Exception:
                logging.exception("Pose scan failed")
            finally:
                self.ready.set()

    def _build(self, root: Path | None, recursive=True):
        cached = read_dict(DATA_DIR / "poses_cache.json")
        files = pose_files(root, recursive)
        cid = collection_id(root) if root else None
        overrides, bundled = read_overrides(cid), bundled_modes()

        def inspect(item):
            relative, path = item
            try:
                stat = path.stat()
                key = f"{path.parent}|{path.name}|{stat.st_size}|{stat.st_mtime_ns}"
                value = cached.get(key)
                if not valid_entry(value):
                    with Image.open(path) as im:
                        if im.format not in FORMATS:
                            raise ValueError(f"Unsupported image format: {im.format}")
                        im.load()  # detect truncated image data as well as broken headers
                        # Browsers apply EXIF orientation, so measure the oriented image. Opaque → full box.
                        rgba = ImageOps.exif_transpose(im).convert("RGBA")
                    mask = rgba.getchannel("A").point(lambda a: 255 if a > 8 else 0)
                    box = mask.getbbox()
                    if box is None:
                        raise ValueError("Empty alpha silhouette")
                    value = {"box": list(box), "w": rgba.width, "h": rgba.height,
                             "area": mask.histogram()[255],
                             "support": support_profile(mask, box, auto_mode(box))}
                return key, value, relative, path
            except Exception:
                logging.exception("Skipping unreadable pose: %s", path)
                return None

        # PIL releases the GIL while decoding. Preserve input sort order.
        with ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 1)) as pool:
            results = list(pool.map(inspect, files))
        # This folder's entries are rebuilt; keep only those inside other configured collections
        # (component-wise path comparison: a collection at a drive root cannot swallow the others).
        others = [Path(c["folder"]) for c in load_config()["collections"] if c["id"] != cid]

        def keep(key):
            try:
                parent = Path(key.split("|", 1)[0])
                return any(parent.is_relative_to(folder) for folder in others)
            except ValueError:
                return False
        entries = {k: v for k, v in cached.items() if keep(k)}
        items, allowed = [], {}
        for result in results:
            if result is None:
                continue
            key, value, relative, path = result
            entries[key] = value
            items.append(make_pose(self.prefix, relative, value, overrides, bundled))
            allowed[relative] = path
        if entries != cached:
            write_json(DATA_DIR / "poses_cache.json", entries)
        reference, scales = body_reference(items), read_scales(cid)
        return [with_body(pose, reference, scales) for pose in items], allowed, reference

    def get(self) -> list[dict]:
        self.ready.wait()
        with self.lock:
            return list(self.items)

    def snapshot(self) -> tuple[str | None, list[dict]]:
        """(collection id, poses) taken together."""
        self.ready.wait()
        with self.lock:
            return (collection_id(self.folder) if self.folder else None), list(self.items)

    def resolve(self, relative: str) -> Path | None:
        """Allowlisted pose, only if it still resolves to a file inside the collection folder."""
        with self.lock:
            path, root = self.allowed.get(relative), self.folder
        if path is None or root is None:
            return None
        try:
            real = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        return real if real.is_relative_to(root) and real.is_file() else None

    def require(self, relative, wait=True) -> str:
        """Collection id if `relative` is a pose of the active collection, else ValueError."""
        if wait:
            self.ready.wait()
        with self.lock:
            if not isinstance(relative, str) or relative not in self.allowed or not self.folder:
                raise ValueError(f"Unknown pose: {relative!r}")
            return collection_id(self.folder)

    def set_mode(self, relative, mode) -> dict:
        if mode not in (*MODES, "auto"):
            raise ValueError(f"Unknown support mode: {mode!r}")
        self.ready.wait()
        with self._scan_lock:  # never interleave with a rescan (ready stays set while we hold it)
            with self.lock:
                cid = self.require(relative, wait=False)
                set_override(cid, relative, mode)
                index = next(i for i, pose in enumerate(self.items) if pose["file"] == relative)
                pose = make_pose(self.prefix, relative, self.items[index], read_overrides(cid), bundled_modes())
                # The collection's reference stays as scanned, so other poses keep their size.
                pose = with_body(pose, self.reference, read_scales(cid))
                self.items = self.items[:index] + [pose] + self.items[index + 1:]
                return pose

    def set_scale(self, relative, factor) -> dict:
        if not valid_scale(factor):
            raise ValueError(f"Size correction must be within {SCALE_LIMITS}: {factor!r}")
        self.ready.wait()
        with self._scan_lock:
            with self.lock:
                cid = self.require(relative, wait=False)
                set_scale(cid, relative, float(factor))
                index = next(i for i, pose in enumerate(self.items) if pose["file"] == relative)
                pose = with_body(self.items[index], self.reference, read_scales(cid))
                self.items = self.items[:index] + [pose] + self.items[index + 1:]
                return pose


# --- Background images (view pref "background") --------------------------------------------------
# Copies of user-chosen pictures live in DATA_DIR/backgrounds/<sha256-16>.(png|jpg); originals are
# only read. The id is the first 16 hex digits of the SHA-256 of the original file's bytes.

BACKGROUND_ID_RE = re.compile(r"[0-9a-f]{16}")
BACKGROUND_FILE_RE = re.compile(r"([0-9a-f]{16})\.(png|jpg)")
BACKGROUND_MAX_SIDE = 4096
BACKGROUND_MAX_BYTES = 256 * 1024 * 1024  # refuse absurd source files before decoding
BACKGROUND_MODES = ("studio", "image")
BACKGROUND_LIMITS = {"blur": (0.0, 24.0), "dim": (0.0, 0.85)}


def clean_background_prefs(raw) -> dict:
    """{mode, id, blur, dim} with valid values only; numbers are clamped into their range."""
    if not isinstance(raw, dict):
        return {}
    result = {}
    if raw.get("mode") in BACKGROUND_MODES:
        result["mode"] = raw["mode"]
    if "id" in raw and (raw["id"] is None or (isinstance(raw["id"], str) and BACKGROUND_ID_RE.fullmatch(raw["id"]))):
        result["id"] = raw["id"]
    for key, (low, high) in BACKGROUND_LIMITS.items():
        value = raw.get(key)
        if type(value) in (int, float) and math.isfinite(value):
            result[key] = round(min(high, max(low, float(value))), 3)
    return result


def background_dir() -> Path:
    return DATA_DIR / "backgrounds"


def background_file(name: str) -> Path | None:
    """`<id>.<ext>` inside DATA_DIR/backgrounds (strict name, no traversal, real file), else None."""
    if not isinstance(name, str) or not BACKGROUND_FILE_RE.fullmatch(name):
        return None
    root = background_dir()
    try:
        root = root.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    path = root / name
    return path if inside(path, root) and path.is_file() else None


def _background_entry(prefix: str, path: Path) -> dict | None:
    match = BACKGROUND_FILE_RE.fullmatch(path.name)
    if not match:
        return None
    try:
        with Image.open(path) as im:
            w, h = im.size
    except Exception:
        logging.exception("Unreadable stored background: %s", path)
        return None
    return {"id": match.group(1), "url": f"{prefix}/backgrounds/{path.name}", "w": w, "h": h}


def list_backgrounds(prefix: str) -> list[dict]:
    """Stored backgrounds, oldest first."""
    try:
        with os.scandir(background_dir()) as entries:
            files = [(entry.stat().st_mtime_ns, entry.name, Path(entry.path)) for entry in entries
                     if entry.is_file() and BACKGROUND_FILE_RE.fullmatch(entry.name)]
    except OSError:
        return []
    result = []
    for _, _, path in sorted(files):
        entry = _background_entry(prefix, path)
        if entry and all(e["id"] != entry["id"] for e in result):
            result.append(entry)
    return result


def _to_srgb(image, mode: str):
    """`image` as `mode` ("RGB"/"RGBA") in sRGB. An embedded ICC profile (Display P3, Adobe RGB,
    CMYK, ...) is converted to sRGB like a browser would; without one, or if the profile is
    unusable, it is a plain conversion (values taken as sRGB)."""
    icc = image.info.get("icc_profile")
    if icc:
        try:
            import io
            from PIL import ImageCms
            alpha = image.convert("RGBA").getchannel("A") if mode == "RGBA" else None
            base = image if image.mode in ("RGB", "CMYK", "L") else image.convert("RGB")
            result = ImageCms.profileToProfile(
                base, ImageCms.ImageCmsProfile(io.BytesIO(icc)), ImageCms.createProfile("sRGB"),
                renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC, outputMode="RGB")
            if alpha is not None:
                result.putalpha(alpha)
            return result
        except Exception:
            logging.warning("Ignoring an unusable ICC profile in a background image", exc_info=True)
    return image.convert(mode)


def import_background(source: Path, prefix: str) -> dict:
    """Validates `source` (full decode, EXIF orientation), stores a copy (long side ≤ 4096 px; alpha →
    PNG, otherwise JPEG q92) and returns {id, url, w, h}. The source file is never modified.
    Unreadable or unsupported images raise ValueError."""
    try:
        source = Path(source)
        if source.suffix.lower() not in EXTENSIONS:
            raise ValueError("unsupported extension")
        if source.stat().st_size > BACKGROUND_MAX_BYTES:
            raise ValueError("file too large")
        data = source.read_bytes()
        digest = hashlib.sha256(data).hexdigest()[:16]
        for ext in ("png", "jpg"):
            existing = background_file(f"{digest}.{ext}")
            if existing:
                os.utime(existing)  # moves to the end of the list
                return _background_entry(prefix, existing)
        import io
        with Image.open(io.BytesIO(data)) as im:
            if im.format not in FORMATS:
                raise ValueError(f"format {im.format}")
            im.load()
            alpha = "A" in im.getbands() or "transparency" in im.info
            image = _to_srgb(ImageOps.exif_transpose(im), "RGBA" if alpha else "RGB")
        if alpha and image.getchannel("A").getextrema()[0] == 255:
            image, alpha = image.convert("RGB"), False  # an opaque PNG is stored as JPEG
        image.thumbnail((BACKGROUND_MAX_SIDE, BACKGROUND_MAX_SIDE), Image.Resampling.LANCZOS)
    except ValueError as error:
        raise ValueError(f"Не удалось открыть изображение: {error}") from None
    except (OSError, Image.DecompressionBombError, SyntaxError) as error:
        raise ValueError(f"Не удалось открыть изображение: {error}") from None
    ext = "png" if alpha else "jpg"
    folder = background_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{digest}.{ext}"
    tmp = folder / f"{digest}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        if alpha:
            image.save(tmp, "PNG")
        else:
            image.save(tmp, "JPEG", quality=92, subsampling=0, optimize=True)
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    return {"id": digest, "url": f"{prefix}/backgrounds/{target.name}", "w": image.width, "h": image.height}


def remove_background(background_id) -> bool:
    """Deletes a stored copy; a selected background falls back to the studio. False if unknown."""
    if not isinstance(background_id, str) or not BACKGROUND_ID_RE.fullmatch(background_id):
        return False
    removed = False
    for ext in ("png", "jpg"):
        path = background_file(f"{background_id}.{ext}")
        if path:
            try:
                path.unlink()
                removed = True
            except OSError:
                logging.exception("Cannot remove background: %s", path)
    with _store_lock:
        if read_view_prefs().get("background", {}).get("id") == background_id:
            save_view_prefs({"background": {"mode": "studio", "id": None}})
    return removed
