"""Short animation of a posed figure (docs/RIG.md § Анимация): keys on tracks, the pose at any
time, deterministic frame export.

The animation lives in the rig: `rig["animation"] = {"duration": s, "fps": n, "loop": bool,
"tracks": {track: [{"t": s, "value": …, "interp": "ease" | "linear" | "step"}]}}`.
Tracks: "rot:<bone>" (degrees, as set_joint_rotation), "root" ([dx, dy]), "ik:<chain>"
([x, y], or null: no target from that key on). A bone or chain without keys keeps its value of
the static pose. Contacts with an `interval` hold only inside it.

`pose_at` is the one evaluator: the editor's scrubbing and playback ask the backend for the
pose at a time (pose_eval), previews and the export render the same pose with rig.Rig.render.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import figure_doc as fd

INTERPOLATIONS = ("ease", "linear", "step")
MAX_DURATION = 120.0
MAX_FRAMES = 3000


def default_animation():
    return {"duration": 2.0, "fps": 24, "loop": True, "tracks": {}}


def animation_of(rig_data, create=False):
    """The animation of a rig; reads get a default without touching the document."""
    anim = rig_data.get("animation")
    if anim is None:
        anim = default_animation()
        if create:
            rig_data["animation"] = anim
    return anim


def _ease(u):
    return u * u * (3 - 2 * u)


def _mix(a, b, u):
    if isinstance(a, (list, tuple)):
        return [x + (y - x) * u for x, y in zip(a, b)]
    return a + (b - a) * u


def value_at(keys, t):
    """Interpolated value of a track at time t (keys sorted by time)."""
    if not keys:
        return None
    if t <= keys[0]["t"]:
        return keys[0]["value"]
    for k0, k1 in zip(keys, keys[1:]):
        if k0["t"] <= t <= k1["t"]:
            if k0["value"] is None or k1["value"] is None or k0.get("interp", "ease") == "step" or k1["t"] == k0["t"]:
                return k0["value"] if t < k1["t"] else k1["value"]
            u = (t - k0["t"]) / (k1["t"] - k0["t"])
            if k0.get("interp", "ease") == "ease":
                u = _ease(u)
            return _mix(k0["value"], k1["value"], u)
    return keys[-1]["value"]


def local_time(anim, t):
    duration = max(1e-6, float(anim.get("duration", 2.0)))
    if anim.get("loop", True):
        return t % duration if t != duration else duration
    return min(max(0.0, t), duration)


def pose_at(rig_data, t):
    """The pose at time t: keyed tracks over the static pose."""
    base = rig_data.get("pose") or {}
    pose = {"root": list(base.get("root") or [0.0, 0.0]), "rotations": dict(base.get("rotations") or {}),
            "ik": dict(base.get("ik") or {}), "time": t}
    anim = animation_of(rig_data)
    tl = local_time(anim, t)
    for track, keys in (anim.get("tracks") or {}).items():
        if not keys:
            continue
        value = value_at(keys, tl)
        kind, _, name = track.partition(":")
        if kind == "rot":
            pose["rotations"][name] = value
        elif kind == "root":
            pose["root"] = value
        elif kind == "ik":
            if value is None:
                pose["ik"].pop(name, None)
            else:
                pose["ik"][name] = value
    return pose


def contact_active(contact, t):
    interval = contact.get("interval")
    return contact.get("enabled", True) and (interval is None or t is None or interval[0] <= t <= interval[1])


def _check_track(track):
    import rig
    kind, _, name = track.partition(":")
    if kind == "rot" and name in rig.BONE_IDS:
        return
    if kind == "ik" and (name in rig.CHAINS or name in rig.SINGLE):
        return
    if track == "root":
        return
    raise fd.CommandError(f"Нет такой дорожки: {track!r} (rot:<кость>, ik:<цепочка>, root)")


def _check_value(track, value, project):
    if track.startswith("rot:"):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or abs(value) > 720:
            raise fd.CommandError("Значение поворота: градусы, −720…720")
        return round(float(value), 3)
    if track == "root":
        if not (isinstance(value, list) and len(value) == 2 and all(isinstance(v, (int, float)) and math.isfinite(v) for v in value)):
            raise fd.CommandError("root: [dx, dy]")
        return [round(float(v), 2) for v in value]
    return None if value is None else fd._xy(value, project)


def _check_time(t, anim):
    if not isinstance(t, (int, float)) or isinstance(t, bool) or not math.isfinite(t) or not 0 <= t <= MAX_DURATION:
        raise fd.CommandError(f"time: секунды, 0…{MAX_DURATION}")
    return round(float(t), 4)


def set_key(project, inst, track, t, value, relative=False, source="user", interp=None):
    """A key on a track (replaces a key at the same time). The animation grows to hold it."""
    from rig_doc import _rig_data
    data = _rig_data(inst)
    anim = animation_of(data, create=True)
    _check_track(track)
    t = _check_time(t, anim)
    keys = anim.setdefault("tracks", {}).setdefault(track, [])
    if relative and track.startswith("rot:"):
        before = value_at(keys, t)
        if before is None:
            before = (data.get("pose") or {}).get("rotations", {}).get(track[4:], 0.0)
        value = before + value
    value = _check_value(track, value, project)
    interp = interp or next((k.get("interp") for k in keys if k["t"] == t), None) or "ease"
    if interp not in INTERPOLATIONS:
        raise fd.CommandError(f"interpolation: {', '.join(INTERPOLATIONS)}")
    keys[:] = [k for k in keys if abs(k["t"] - t) > 1e-6] + [{"t": t, "value": value, "interp": interp}]
    keys.sort(key=lambda k: k["t"])
    if t > anim["duration"]:
        anim["duration"] = t
    return {"track": track, "time": t, "value": value, "keys": len(keys)}


# --- Commands ------------------------------------------------------------------------------------

def cmd_set_keyframe(project, track_id, time, value, interpolation="ease", instance_id=None, override_lock=False, source="user"):
    """A key: track rot:<bone> (degrees) | root ([dx, dy]) | ik:<chain> ([x, y] or null)."""
    from rig_doc import _lock_pose, _rig_data
    inst = project.instance(instance_id)
    _lock_pose(_rig_data(inst), source, override_lock)
    return set_key(project, inst, track_id, time, value, False, source, interpolation)


def cmd_delete_keyframe(project, track_id, time=None, instance_id=None, override_lock=False, source="user"):
    """Removes the key at `time` (± 1 ms), or the whole track without `time`."""
    from rig_doc import _lock_pose, _rig_data
    inst = project.instance(instance_id)
    data = _rig_data(inst)
    _lock_pose(data, source, override_lock)
    tracks = animation_of(data, create=True).setdefault("tracks", {})
    if track_id not in tracks:
        raise fd.CommandError(f"Нет дорожки {track_id!r}", "not_found")
    if time is None:
        del tracks[track_id]
        return {}
    keep = [k for k in tracks[track_id] if abs(k["t"] - float(time)) > 1e-3]
    if len(keep) == len(tracks[track_id]):
        raise fd.CommandError(f"На {track_id} нет ключа в {time} с", "not_found")
    tracks[track_id] = keep
    if not keep:
        del tracks[track_id]
    return {}


def cmd_set_animation(project, duration=None, fps=None, loop=None, instance_id=None, source="user"):
    from rig_doc import _rig_data
    inst = project.instance(instance_id)
    anim = animation_of(_rig_data(inst), create=True)
    if duration is not None:
        if not isinstance(duration, (int, float)) or not .05 <= duration <= MAX_DURATION:
            raise fd.CommandError(f"duration: 0,05…{MAX_DURATION} с")
        anim["duration"] = round(float(duration), 4)
    if fps is not None:
        if not isinstance(fps, int) or not 1 <= fps <= 60:
            raise fd.CommandError("fps: 1…60")
        anim["fps"] = fps
    if loop is not None:
        anim["loop"] = bool(loop)
    return {"animation": {k: v for k, v in anim.items() if k != "tracks"}}


def cmd_key_pose(project, time, bones=None, instance_id=None, override_lock=False, source="user"):
    """Keys the whole current pose at `time`: every rotated bone (or the given ones), root and
    IK targets — "remember this pose here"."""
    from rig_doc import _lock_pose, _rig_data
    inst = project.instance(instance_id)
    data = _rig_data(inst)
    _lock_pose(data, source, override_lock)
    pose = pose_at(data, float(time)) if animation_of(data).get("tracks") else (data.get("pose") or {})
    rotations = pose.get("rotations") or {}
    names = bones if bones is not None else sorted(set(rotations) | {t[4:] for t in animation_of(data).get("tracks", {}) if t.startswith("rot:")})
    for bone in names:
        set_key(project, inst, f"rot:{bone}", time, rotations.get(bone, 0.0), False, source)
    set_key(project, inst, "root", time, list(pose.get("root") or [0.0, 0.0]), False, source)
    for chain, target in (pose.get("ik") or {}).items():
        set_key(project, inst, f"ik:{chain}", time, target, False, source)
    return {"time": time, "tracks": len(animation_of(data)["tracks"])}


def read_animation(project, instance_id=None):
    from rig_doc import _rig_data
    inst = project.instance(instance_id)
    anim = animation_of(_rig_data(inst))
    return {"revision": project.doc["revision"], **json.loads(json.dumps(anim)),
            "frames": frame_count(anim), "units": {"time": "s", "rot": "degrees from rest, clockwise", "root": "px", "ik": "source px"}}


def frame_count(anim, start=0.0, end=None):
    end = anim["duration"] if end is None else end
    return max(1, min(MAX_FRAMES, int(math.floor((end - start) * anim["fps"] + 1e-9)) + (0 if anim.get("loop", True) else 1)))


def frame_times(anim, start=0.0, end=None):
    """Frame times of [start, end): a loop does not repeat its first frame at the end."""
    end = anim["duration"] if end is None else end
    return [round(start + i / anim["fps"], 6) for i in range(frame_count(anim, start, end))]


def read_check_animation(project, instance_id=None, start=0.0, end=None, fps=None):
    """Frame by frame: IK misses, slipping contacts, limited rotations — the frames to look at."""
    from rig_doc import _rig_data, rig_of
    inst = project.instance(instance_id)
    data = _rig_data(inst)
    anim = dict(animation_of(data))
    if fps is not None:
        anim["fps"] = int(fps)
    built = rig_of(project, inst["id"])
    frames = []
    for t in frame_times(anim, float(start), end):
        pose = pose_at(data, t)
        _, _, report = built.solve(pose, time=t)
        problems = [f"ik {c}: {r['miss_px']} px" for c, r in report["ik"].items() if r["miss_px"] > 2]
        problems += [f"опора {c}: {r['miss_px']} px" for c, r in report["contacts"].items() if r["miss_px"] > 2]
        problems += [f"{b} ограничен" for b, v in pose["rotations"].items()
                     if b in built.skeleton.bones and abs(built.skeleton.limit(b, v) - v) > 1e-6]
        if problems:
            frames.append({"time": t, "problems": problems})
    return {"revision": project.doc["revision"], "checked": len(frame_times(anim, float(start), end)), "frames_with_problems": frames}


def _long_path(path: Path) -> Path:
    """Windows paths over MAX_PATH (deep folders, long names) need the extended-length prefix."""
    import sys
    prefix = "\\\\?\\"
    text = str(path.resolve())
    if sys.platform == "win32" and len(text) > 200 and not text.startswith(prefix):
        return Path(prefix + text)
    return path


def export_frames(project, folder: Path, instance_id=None, start=0.0, end=None, fps=None, max_side=None,
                  background=None, overlays=("pose",), progress=lambda done, total: None, cancelled=lambda: False):
    """PNG frames of the animation, same pixels for the same document: frame_0000.png … and
    frames.json (times, size, revision, settings). Background None = transparent."""
    from rig_doc import _rig_data, render_pose
    inst = project.instance(instance_id)
    data = _rig_data(inst)
    anim = dict(animation_of(data))
    if fps is not None:
        anim["fps"] = int(fps)
    times = frame_times(anim, float(start), end)
    shown = Path(folder)
    folder = _long_path(shown)
    folder.mkdir(parents=True, exist_ok=True)
    w, h = project.doc["source"]["width"], project.doc["source"]["height"]
    side = int(max_side or max(w, h))
    files = []
    for i, t in enumerate(times):
        if cancelled():
            raise fd.CommandError("Экспорт отменён", "cancelled")
        with project.lock:
            image, _ = render_pose(project, inst["id"], pose=pose_at(data, t), time=t, overlays=tuple(overlays),
                                   max_side=side, background=tuple(background) if background else (0, 0, 0, 0))
        name = f"frame_{i:04d}.png"
        image.save(folder / name, "PNG", compress_level=6)
        files.append({"file": name, "time": t})
        progress(i + 1, len(times))
    manifest = {"schema": "posestage.frames", "version": 1, "revision": project.doc["revision"], "fps": anim["fps"],
                "loop": anim.get("loop", True), "duration": anim["duration"], "size": list(image.size) if times else [w, h],
                "coordinates": "source px scaled by size / source size", "background": background, "frames": files}
    (folder / "frames.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"folder": str(shown), "frames": len(files), "size": manifest["size"], "fps": anim["fps"]}


COMMANDS = {"set_keyframe": cmd_set_keyframe, "delete_keyframe": cmd_delete_keyframe, "set_animation": cmd_set_animation,
            "key_pose": cmd_key_pose}
READS = {"get_animation": read_animation, "check_animation": read_check_animation}
fd.COMMANDS.update(COMMANDS)
fd.READS.update(READS)
