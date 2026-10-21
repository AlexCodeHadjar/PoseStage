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
THUMB_LIMITS = (500, 64 * 1024 * 1024)  # LRU cap for DATA_DIR/thumbs: files, bytes
MAX_TAGS, MAX_TAG_LENGTH = 16, 32
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
    w, h, box = value.get("w"), value.get("h"), value.get("box")
    if not (type(w) is int and type(h) is int and isinstance(box, list)
            and len(box) == 4 and all(type(n) is int for n in box)):
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
            "box": list(geometry["box"]), "w": geometry["w"], "h": geometry["h"],
            "support": {**geometry["support"], "mode": mode, "cut": CUTS[mode]}, "support_source": source}


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


VIEW_PREFS = {"flip": bool, "gray": bool, "on_top": bool, "compact": bool, "grid": ("off", "thirds", "proportions")}
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
        if filter_:
            result["filter"] = filter_
    return result


def read_view_prefs() -> dict:
    return clean_view_prefs(read_dict(DATA_DIR / "view_prefs.json"))


def save_view_prefs(prefs) -> dict:
    """Merges validated keys into the stored prefs and returns the result."""
    update = clean_view_prefs(prefs)
    with _store_lock:
        current = read_view_prefs()
        if "filter" in update:
            update["filter"] = {**current.get("filter", {}), **update["filter"]}
        current.update(update)
        write_json(DATA_DIR / "view_prefs.json", current)
        return current


# --- Thumbnails --------------------------------------------------------------------------------

def _cached_thumb(target: Path) -> bool:
    """True if cached; refreshes mtime (at most hourly) so pruning drops least recently used first."""
    try:
        if time.time() - target.stat().st_mtime > 3600:
            os.utime(target)
        return True
    except OSError:
        return False


def prune_thumbs(max_files=THUMB_LIMITS[0], max_bytes=THUMB_LIMITS[1]):
    """Delete least recently used thumbnails beyond the limits, and stale temp files."""
    now, files = time.time(), []
    try:
        with os.scandir(DATA_DIR / "thumbs") as entries:
            for entry in entries:
                stat = entry.stat()
                if entry.name.endswith(".tmp"):
                    if now - stat.st_mtime > 3600:
                        Path(entry.path).unlink(missing_ok=True)
                elif entry.name.endswith(".png"):
                    files.append((stat.st_mtime, stat.st_size, entry.path))
    except OSError:
        return
    total = 0
    for count, (_, size, path) in enumerate(sorted(files, reverse=True), 1):
        total += size
        if count > max_files or total > max_bytes:
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
                items, allowed = self._build(root, recursive)
                with self.lock:
                    self.folder, self.items, self.allowed, self.recursive = root, items, allowed, recursive
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
        return items, allowed

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
                self.items = self.items[:index] + [pose] + self.items[index + 1:]
                return pose
