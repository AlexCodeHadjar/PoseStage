"""Dance frames for the desktop podium, made ahead by background worker processes and kept on
disk (docs/DANCE.md, stage 5).

A bone-driven frame costs ~0.1 s on the CPU, too slow to make while it is shown. So:
- a job (one phrase at one podium size, style and tempo) is split into its transitions; worker
  processes (`PoseStage --dance-worker <parent pid>`, below-normal priority, no GIL shared with
  the podium) claim transitions through files and write each frame as PNG as soon as it is made;
- the frames stay in %APPDATA%\\PoseStage\\dance_frames\\<key>\\ (key: everything that changes the
  picture), so a phrase made once plays instantly from then on; the podium makes every phrase
  in the background while it stands idle;
- the player never waits: a frame not made yet is shown as a cross-fade of the two poses.

Files of a job: spec.json (what to draw), f0000.png (figure, "pos" text chunk x,y) and
s0000.png (shadow alpha, likewise), c<step> (a worker took the transition), d<step> (done),
complete. Jobs to do: jobs/<9 - priority>-<time>-<key>.json.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import time

from PIL import Image, ImageOps, PngImagePlugin

import poses

RENDER_VERSION = 1  # bump when the drawing of frames changes: old ones are not reused
FPS = 20
GRID = 14  # warp mesh cell, display px
MAX_CACHE_BYTES = 800 * 1024 * 1024
CLAIM_STALE_S = 180  # a claim this old without its frames was left by a worker that died
WORKER_IDLE_EXIT_S = 90


def cache_root() -> Path:
    return poses.DATA_DIR / "dance_frames"


class Plan:
    """Timing of one cycle of a phrase: poses in `order`, one per count; frame i shows dance time i / fps."""

    def __init__(self, count, loop, style="soft", bpm=120, fps=FPS):
        import dance_motion
        self.order = dance_motion.cycle(count, loop)
        self.style = style if style in dance_motion.STYLES else "soft"
        share, curve_name = dance_motion.STYLES[self.style]
        self.ease = dance_motion.curve(curve_name)
        self.fps, self.beat = fps, 60 / bpm
        self.move = self.beat * share
        self.steps = max(0, len(self.order) - 1)
        self.count = max(1, round(self.steps * self.beat * fps))

    def frames_of(self, step):
        first = round(step * self.beat * self.fps)
        return range(first, min(round((step + 1) * self.beat * self.fps), self.count))

    def at(self, i):
        """(step, pose a, pose b, progress u) of frame i; u ≥ 1 once b has arrived."""
        per = self.beat * self.fps
        step = int(i / per) if self.steps else 0
        if step + 1 < self.steps and round((step + 1) * per) <= i:  # the same rounding as frames_of()
            step += 1
        elif step > 0 and round(step * per) > i:
            step -= 1
        step = max(0, min(self.steps - 1, step))
        t = i / self.fps - step * self.beat
        return step, self.order[step], self.order[min(step + 1, len(self.order) - 1)], t / self.move


# --- Specs, keys, jobs --------------------------------------------------------------------------

def spec_for(entries, loop, height, sizing, style, bpm, fps=FPS, grid=GRID) -> dict:
    """entries: [(source path, pose dict, mirror)]. The spec says everything a worker needs."""
    out = []
    for path, pose, mirror in entries:
        stat = Path(path).stat()
        clean = {k: v for k, v in pose.items() if k not in ("url", "thumb")}
        out.append({"path": str(path), "size": stat.st_size, "mtime": stat.st_mtime_ns, "mirror": bool(mirror),
                    "pose": clean})
    return {"version": RENDER_VERSION, "height": int(height), "sizing": sizing, "style": style, "bpm": int(bpm),
            "fps": fps, "grid": grid, "loop": bool(loop), "entries": out}


def key_of(spec) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24]


def _write_atomic(path: Path, data: bytes):
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def submit(spec, priority=0) -> str:
    """Queues the phrase (priority 0 background … 9 now); returns its key. Nothing to do if made."""
    root, key = cache_root(), key_of(spec)
    folder = root / key
    folder.mkdir(parents=True, exist_ok=True)
    if not (folder / "spec.json").exists():
        _write_atomic(folder / "spec.json", json.dumps(spec).encode("utf-8"))
    os.utime(folder)  # recently used: kept by prune()
    if (folder / "complete").exists():
        return key
    jobs = root / "jobs"
    jobs.mkdir(exist_ok=True)
    for old in jobs.glob(f"*-{key}.json"):
        if old.name.startswith(f"{9 - priority}-"):
            return key
        old.unlink(missing_ok=True)  # re-queued at another priority
    _write_atomic(jobs / f"{9 - max(0, min(9, priority))}-{time.time_ns()}-{key}.json", b"{}")
    return key


def cancel(keys):
    """Drops queued jobs (frames already made stay in the cache)."""
    jobs = cache_root() / "jobs"
    for key in keys:
        for path in jobs.glob(f"*-{key}.json") if jobs.is_dir() else ():
            path.unlink(missing_ok=True)


def pending_jobs() -> list[str]:
    jobs = cache_root() / "jobs"
    return [p.name.rsplit("-", 1)[1][:-5] for p in sorted(jobs.glob("*.json"))] if jobs.is_dir() else []


def complete(key) -> bool:
    return (cache_root() / key / "complete").exists()


# --- Frames -------------------------------------------------------------------------------------

def _save(image: Image.Image, x: int, y: int, path: Path):
    info = PngImagePlugin.PngInfo()
    info.add_text("pos", f"{x},{y}")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    image.save(tmp, "PNG", pnginfo=info, compress_level=1)
    os.replace(tmp, path)


def load_frame(key, i):
    """((figure image, x, y), (shadow image, x, y)) of frame i, or None if not made yet."""
    folder = cache_root() / key
    try:
        with Image.open(folder / f"f{i:04d}.png") as figure_file, Image.open(folder / f"s{i:04d}.png") as shadow_file:
            fx, fy = map(int, figure_file.text["pos"].split(","))
            sx, sy = map(int, shadow_file.text["pos"].split(","))
            figure = figure_file.convert("RGBA")
            alpha = shadow_file.convert("L")
    except (OSError, KeyError, ValueError):
        return None
    shadow = Image.new("RGBA", alpha.size, (0, 0, 0, 0))
    shadow.putalpha(alpha)
    return (figure, fx, fy), (shadow, sx, sy)


class Painter:
    """Makes the frames of one job (in a worker), one transition at a time."""

    def __init__(self, spec, resource_dir: Path):
        import dance
        import dance_motion
        import desktop
        self.motion, self.desktop = dance_motion, desktop
        self.spec = spec
        self.stage = desktop.Stage(resource_dir / "web" / "desktop", spec["height"], spec["sizing"])
        self.plan = Plan(len(spec["entries"]), spec["loop"], spec["style"], spec["bpm"], spec["fps"])
        self.prepared, self.canvases, self.joints = [], [], []
        for entry in spec["entries"]:
            pose = entry["pose"]
            with Image.open(entry["path"]) as opened:
                image = opened.convert("RGBA")
            if entry["mirror"]:
                pose, image = dance.mirror_pose(pose), ImageOps.mirror(image)
            prepared = self.stage.prepare(pose, image)
            canvas = Image.new("RGBA", self.stage.size, (0, 0, 0, 0))
            canvas.alpha_composite(prepared["figure"], (prepared["left"], prepared["top"]))
            self.prepared.append(prepared)
            self.canvases.append(canvas)
            self.joints.append(desktop.display_joints(prepared))

    def step(self, step, folder: Path, keep_going=lambda: True):
        """Writes the frames of transition `step`; False if stopped early."""
        plan, motion, stage = self.plan, self.motion, self.stage
        a, b = plan.order[step], plan.order[step + 1]
        transition = motion.Transition(self.canvases[a], self.joints[a], self.canvases[b], self.joints[b],
                                       grid=self.spec["grid"])
        still = None
        for i in plan.frames_of(step):
            if not keep_going():
                return False
            _, _, _, u = plan.at(i)
            if u >= 1 and plan.style == "soft":
                if still is None:
                    p = self.prepared[b]
                    still = self.desktop.Sprite(p["figure"], p["left"], p["top"])
                figure = still
            else:
                target = transition.skeleton(u, plan.ease)
                if plan.style == "groove":
                    target = motion.groove(target, i / plan.fps, plan.beat, transition.height)
                layer = transition.layer(target, u, plan.ease)
                box = layer.getbbox() or (0, 0, 1, 1)
                figure = self.desktop.Sprite(layer.crop(box), box[0], box[1])
            shadow = stage.cast_shadow(figure)
            _save(figure.image, figure.x, figure.y, folder / f"f{i:04d}.png")
            _save(shadow.image.getchannel("A"), shadow.x, shadow.y, folder / f"s{i:04d}.png")
        return True


# --- Worker -------------------------------------------------------------------------------------

def _claim(folder: Path, step: int) -> bool:
    claim = folder / f"c{step}"
    try:
        fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        try:  # a worker that died leaves its claim behind
            if time.time() - claim.stat().st_mtime > CLAIM_STALE_S and not (folder / f"d{step}").exists():
                os.utime(claim)
                return True
        except OSError:
            pass
        return False


def _parent_alive(pid: int) -> bool:
    if not pid:
        return True
    import ctypes
    handle = ctypes.windll.kernel32.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: running
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def work_once(resource_dir: Path, painters: dict, keep_going=lambda: True) -> bool:
    """Makes one transition of the most urgent job; False when there is nothing to do."""
    root = cache_root()
    jobs = root / "jobs"
    for job in sorted(jobs.glob("*.json")) if jobs.is_dir() else ():
        key = job.name.rsplit("-", 1)[1][:-5]
        folder = root / key
        try:
            spec = json.loads((folder / "spec.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            job.unlink(missing_ok=True)
            continue
        plan = Plan(len(spec["entries"]), spec["loop"], spec["style"], spec["bpm"], spec["fps"])
        if all((folder / f"d{s}").exists() for s in range(plan.steps)):
            (folder / "complete").touch()
            job.unlink(missing_ok=True)
            continue
        for step in range(plan.steps):
            if (folder / f"d{step}").exists() or not _claim(folder, step):
                continue
            try:
                painter = painters.get(key)
                if painter is None:
                    painters.clear()  # one job's poses at a time in memory
                    painter = painters[key] = Painter(spec, resource_dir)
                if painter.step(step, folder, lambda: keep_going() and job.exists()):
                    (folder / f"d{step}").touch()
                else:
                    (folder / f"c{step}").unlink(missing_ok=True)  # cancelled: free the claim
            except Exception:
                logging.exception("Dance frames of %s, step %d failed", key, step)
                job.unlink(missing_ok=True)
                (folder / f"c{step}").unlink(missing_ok=True)
            return True
    return False


def prune(max_bytes=MAX_CACHE_BYTES, keep=()):
    """Drops the least recently used jobs' frames beyond `max_bytes` (never those in `keep`)."""
    root = cache_root()
    folders = []
    for folder in root.iterdir() if root.is_dir() else ():
        if folder.is_dir() and folder.name != "jobs":
            size = sum(f.stat().st_size for f in folder.iterdir() if f.is_file())
            folders.append((folder.stat().st_mtime, size, folder))
    total = sum(size for _, size, _ in folders)
    for _, size, folder in sorted(folders):
        if total <= max_bytes:
            break
        if folder.name in keep:
            continue
        shutil.rmtree(folder, ignore_errors=True)
        total -= size


def run_worker(resource_dir: Path, parent_pid: int) -> int:
    """`PoseStage --dance-worker <pid>`: makes queued frames until the podium closes or nothing
    has been queued for a while."""
    import ctypes
    try:
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)  # BELOW_NORMAL
    except (AttributeError, OSError):
        pass
    try:
        prune(keep=set(pending_jobs()))
    except OSError:
        logging.exception("Dance frame cache: prune failed")
    painters, idle_since = {}, time.monotonic()
    while _parent_alive(parent_pid):
        if work_once(resource_dir, painters, lambda: _parent_alive(parent_pid)):
            idle_since = time.monotonic()
        elif time.monotonic() - idle_since > WORKER_IDLE_EXIT_S:
            break
        else:
            time.sleep(.4)
    return 0
