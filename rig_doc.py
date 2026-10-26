"""Posing commands and reads of a figure document (docs/RIG.md) — registered into
figure_doc.COMMANDS / READS, so they share execute(): revision check, transactions, undo/redo,
journal, locks of the source (user / ai).

The rig of an instance (`inst["rig"]`):
    bindings     {part id: {"bones": [...]}} — user overrides of rig.DEFAULT_BINDINGS
    layers       part ids back → front
    constraints  {bone id: {"min", "max" (degrees, relative to rest), "bend": 1 | -1}}
    contacts     [{"id", "joint", "point": [x, y], "enabled", "interval": [t0, t1] | null}]
    weights      bone ids that have weight edits (images masks/<inst>_w_<bone>.png)
    surfaces     [{"id", "name", "part_id", "bone_id", "above", "enabled", "source"}]
                 (images masks/<inst>_s_<id>.png: drawn material, never observed pixels)
    settings     {"cell": px | null, "falloff": px | null}
    pose         {"root": [dx, dy], "rotations": {bone: degrees}, "ik": {chain: [x, y]}}
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import secrets
import time

from PIL import Image, ImageChops, ImageDraw, ImageFilter

import figure_doc as fd
import rig

CommandError = fd.CommandError


# --- The rig object of an instance (cached) -------------------------------------------------------

def _rig_data(inst):
    data = inst.get("rig")
    if data is None:
        data = inst["rig"] = rig.default_rig()
    return data


def _weight_map_name(inst, bone):
    return f"{inst['id']}_w_{bone}"


def _surface_map_name(inst, sid):
    return f"{inst['id']}_s_{sid}"


def rig_of(project, instance_id=None) -> rig.Rig:
    """The rig built from the instance's labels, joints and rig data; rebuilt only when one of
    those that shape the mesh changed (not for a pose change)."""
    inst = project.instance(instance_id)
    data = _rig_data(inst)
    labels = project.labels(inst["id"])
    deltas = {b: project.map(_weight_map_name(inst, b)) for b in data.get("weights", [])}
    deltas = {b: m for b, m in deltas.items() if m is not None}
    digest = hashlib.md5(labels.tobytes())
    digest.update(json.dumps([inst["joints"], data.get("bindings"), data.get("constraints"), data.get("settings")],
                             sort_keys=True, default=str).encode())
    for b in sorted(deltas):
        digest.update(b.encode())
        digest.update(deltas[b].tobytes())
    key = (inst["id"], digest.hexdigest())
    cache = project.__dict__.setdefault("_rigs", {})
    if key not in cache:
        cache.clear()
        cache[key] = rig.Rig(project.source(), labels, inst["joints"], data, deltas)
        cache[key].key = key[1]
    built = cache[key]
    built.data = data  # layers, contacts, pose: read live
    return built


def pose_at(inst, time_s=None):
    """The pose to evaluate. Stage 2: the instance's static pose (time is the timeline's)."""
    data = _rig_data(inst)
    if time_s is not None:
        import timeline
        return timeline.pose_at(data, float(time_s))
    return data.get("pose") or {}


def surfaces_of(project, inst, built):
    out = []
    for s in _rig_data(inst).get("surfaces", []):
        if not s.get("enabled", True):
            continue
        image = project.map(_surface_map_name(inst, s["id"]))
        if image is None:
            continue
        part = fd.PARTS.get(s["part_id"])
        bone = s.get("bone_id") or (built.mesh.bindings.get(part["value"], [None])[0] if part else None)
        out.append({"image": image, "part_value": part["value"] if part else None, "bone": bone, "above": s.get("above", False)})
    return out


# --- Validation helpers --------------------------------------------------------------------------

def _bone(project, inst, bone_id):
    built = rig_of(project, inst["id"])
    if bone_id not in rig.BONE_IDS:
        raise CommandError(f"Нет такой кости: {bone_id!r}. Кости: {', '.join(rig.BONE_IDS)}")
    if bone_id not in built.skeleton.bones:
        raise CommandError(f"Кость {bone_id} не построена: нет её суставов в документе", "not_found")
    return built


def _number(value, name, lo=None, hi=None):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise CommandError(f"{name}: число")
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise CommandError(f"{name}: от {lo} до {hi}")
    return float(value)


def _lock_pose(data, source, override_lock):
    """A pose the user locked (`pose_locked`) is kept from the agent unless it asks explicitly."""
    if data.get("pose_locked") and source == "ai" and not override_lock:
        raise CommandError("Поза зафиксирована пользователем; передайте override_lock по его просьбе", "locked")


def _bone_for(joint_id=None, bone_id=None):
    if bone_id:
        return bone_id
    if joint_id in rig.JOINT_BONE:
        return rig.JOINT_BONE[joint_id]
    if joint_id in rig.BONE_IDS:
        return joint_id
    raise CommandError(f"Не знаю, какую кость вращает {joint_id!r}: передайте bone_id")


# --- Pose commands -----------------------------------------------------------------------------------

def cmd_set_joint_rotation(project, angle, joint_id=None, bone_id=None, relative=False, time=None, instance_id=None,
                           override_lock=False, source="user"):
    """Turns a bone (named by the joint it turns at, e.g. elbow_l → left_forearm, or by bone_id)
    to `angle` degrees from its rest direction (clockwise on the picture, y down), or by `angle`
    with relative=true. With `time`, a key on the timeline instead."""
    inst = project.instance(instance_id)
    data = _rig_data(inst)
    bone = _bone_for(joint_id, bone_id)
    built = _bone(project, inst, bone)
    angle = _number(angle, "angle", -720, 720)
    if time is not None:
        import timeline
        return timeline.set_key(project, inst, f"rot:{bone}", float(time), angle, relative, source)
    _lock_pose(data, source, override_lock)
    rotations = data["pose"].setdefault("rotations", {})
    value = rotations.get(bone, 0.0) + angle if relative else angle
    limited = built.skeleton.limit(bone, value)
    rotations[bone] = round(value, 3)
    return {"bone": bone, "angle": rotations[bone], "limited_to": round(limited, 3) if limited != value else None}


def cmd_set_root(project, offset, time=None, instance_id=None, override_lock=False, source="user"):
    """Moves the whole figure by [dx, dy] px (the root of the skeleton)."""
    inst = project.instance(instance_id)
    data = _rig_data(inst)
    if not (isinstance(offset, list) and len(offset) == 2):
        raise CommandError("offset: [dx, dy]")
    offset = [_number(v, "offset", -10000, 10000) for v in offset]
    if time is not None:
        import timeline
        return timeline.set_key(project, inst, "root", float(time), offset, False, source)
    _lock_pose(data, source, override_lock)
    data["pose"]["root"] = [round(v, 2) for v in offset]
    return {}


def cmd_set_ik_target(project, chain_id, position, time=None, instance_id=None, override_lock=False, source="user"):
    """Reach for `position` [x, y] with a chain (left_arm, right_leg, left_hand.index, head…);
    null clears the target. Limits apply; the miss is reported by get_pose / validate_rig."""
    inst = project.instance(instance_id)
    data = _rig_data(inst)
    if chain_id not in rig.CHAINS and chain_id not in rig.SINGLE:
        raise CommandError(f"Нет цепочки {chain_id!r}. Цепочки: {', '.join([*rig.CHAINS, *rig.SINGLE])}")
    if position is not None:
        position = fd._xy(position, project)
    if time is not None:
        import timeline
        return timeline.set_key(project, inst, f"ik:{chain_id}", float(time), position, False, source)
    _lock_pose(data, source, override_lock)
    ik = data["pose"].setdefault("ik", {})
    if position is None:
        ik.pop(chain_id, None)
    else:
        ik[chain_id] = position
    built = rig_of(project, inst["id"])
    _, _, report = built.solve(pose_at(inst))
    return {"miss_px": report["ik"].get(chain_id, {}).get("miss_px")}


def cmd_reset_pose(project, bones=None, instance_id=None, override_lock=False, source="user"):
    """Back to the rest pose (or only these bones / chains)."""
    inst = project.instance(instance_id)
    data = _rig_data(inst)
    _lock_pose(data, source, override_lock)
    if bones is None:
        data["pose"] = {"root": [0.0, 0.0], "rotations": {}, "ik": {}}
    else:
        if not isinstance(bones, list):
            raise CommandError("bones: список костей или цепочек")
        for b in bones:
            data["pose"].get("rotations", {}).pop(b, None)
            data["pose"].get("ik", {}).pop(b, None)
    return {}


def cmd_set_pose_locked(project, locked, instance_id=None, source="user"):
    inst = project.instance(instance_id)
    data = _rig_data(inst)
    if not locked and data.get("pose_locked") and source == "ai":
        raise CommandError("Снять фиксацию позы может только пользователь", "locked")
    data["pose_locked"] = bool(locked)
    return {}


def cmd_set_contact(project, entity_id, interval=None, options=None, instance_id=None, source="user"):
    """Keeps a joint (foot, heel, toe, wrist, knee, elbow, fingertip) at a point while the rest
    moves: options {point: [x, y] (default: where the joint is now), enabled, remove}.
    `interval` [t0, t1] limits it to that time on the timeline."""
    inst = project.instance(instance_id)
    data = _rig_data(inst)
    options = dict(options or {})
    if entity_id not in rig.CONTACT_CHAINS:
        raise CommandError(f"Опорой может быть: {', '.join(rig.CONTACT_CHAINS)}")
    contacts = data.setdefault("contacts", [])
    existing = next((c for c in contacts if c["joint"] == entity_id), None)
    if options.get("remove"):
        if existing:
            contacts.remove(existing)
        return {}
    if interval is not None:
        if not (isinstance(interval, list) and len(interval) == 2):
            raise CommandError("interval: [t0, t1] в секундах")
        interval = sorted(_number(v, "interval", 0, 36000) for v in interval)
    if "point" in options:
        point = fd._xy(options["point"], project)
    else:
        built = rig_of(project, inst["id"])
        _, world, _ = built.solve(pose_at(inst))
        posed = built.skeleton.posed_joints(world)
        if entity_id not in posed:
            raise CommandError(f"Сустава {entity_id} нет в скелете", "not_found")
        point = [round(posed[entity_id][0], 2), round(posed[entity_id][1], 2)]
    entry = existing or {"id": entity_id, "joint": entity_id}
    entry.update(point=point, enabled=bool(options.get("enabled", True)), interval=interval)
    if existing is None:
        contacts.append(entry)
    return {"contact": entry}


def cmd_set_constraint(project, bone_id, min=None, max=None, bend=None, instance_id=None, source="user"):  # noqa: A002
    """Rotation limits of a bone in degrees from rest (null = free) and, for the first bone of a
    two-bone chain, the side IK bends it to (1 / -1, null = automatic)."""
    inst = project.instance(instance_id)
    data = _rig_data(inst)
    _bone(project, inst, bone_id)
    lo = None if min is None else _number(min, "min", -360, 360)
    hi = None if max is None else _number(max, "max", -360, 360)
    if lo is not None and hi is not None and lo > hi:
        raise CommandError("min больше max")
    if bend not in (None, 1, -1):
        raise CommandError("bend: 1, -1 или null")
    entry = {k: v for k, v in (("min", lo), ("max", hi), ("bend", bend)) if v is not None}
    if entry:
        data.setdefault("constraints", {})[bone_id] = entry
    else:
        data.setdefault("constraints", {}).pop(bone_id, None)
    return {}


# --- Rig set-up commands ---------------------------------------------------------------------------

def cmd_set_layer_order(project, part_ids, position=None, instance_id=None, source="user"):
    """Draw order: the listed parts keep their places in the order but take them in the given
    sequence (back → front); position "front" / "back" moves them to the top / bottom."""
    inst = project.instance(instance_id)
    data = _rig_data(inst)
    if not isinstance(part_ids, list) or not part_ids or len(set(part_ids)) != len(part_ids):
        raise CommandError("part_ids: список разных частей")
    for pid in part_ids:
        fd._check_part(pid)
    order = [p for p in (data.get("layers") or rig.default_layers()) if p in fd.PARTS]
    order += [p for p in fd.PARTS if p not in order]
    if position in ("front", "back"):
        rest = [p for p in order if p not in part_ids]
        order = rest + part_ids if position == "front" else part_ids + rest
    elif position is None:
        slots = sorted(order.index(p) for p in part_ids)
        for slot, pid in zip(slots, part_ids):
            order[slot] = pid
    else:
        raise CommandError("position: front, back или null")
    data["layers"] = order
    return {"layers": order}


def cmd_set_binding(project, part_id, bone_ids=None, options=None, instance_id=None, source="user"):
    """Which bones move a part (its pixels start bound to the nearest of them; weights blend
    across joints). options {"reset": true} restores the default."""
    fd._check_part(part_id)
    inst = project.instance(instance_id)
    data = _rig_data(inst)
    options = dict(options or {})
    if options.get("reset"):
        data.setdefault("bindings", {}).pop(part_id, None)
        return {}
    if not isinstance(bone_ids, list) or not bone_ids:
        raise CommandError("bone_ids: список костей")
    for b in bone_ids:
        if b not in rig.BONE_IDS:
            raise CommandError(f"Нет такой кости: {b!r}")
    data.setdefault("bindings", {})[part_id] = {"bones": list(dict.fromkeys(bone_ids))}
    return {}


def cmd_edit_weights(project, bone_id, strokes, operation="add", amount=.5, instance_id=None, source="user"):
    """Paints the influence of a bone: strokes [{points: [[x, y]…], radius}] with operation add
    / subtract (by `amount` 0…1), smooth (blur the edits) or reset (remove the edits there).
    Weights are normalised over all bones afterwards."""
    inst = project.instance(instance_id)
    data = _rig_data(inst)
    _bone(project, inst, bone_id)
    if operation not in ("add", "subtract", "smooth", "reset"):
        raise CommandError("operation: add | subtract | smooth | reset")
    amount = _number(amount, "amount", 0, 1)
    if not isinstance(strokes, list) or not strokes:
        raise CommandError("strokes: непустой список")
    w, h = project.doc["source"]["width"], project.doc["source"]["height"]
    size = (math.ceil(w / rig.DELTA_SCALE), math.ceil(h / rig.DELTA_SCALE))
    region = Image.new("L", size, 0)
    draw = ImageDraw.Draw(region)
    k = rig.DELTA_SCALE
    for stroke in strokes:
        points = [fd._xy(p, project) for p in stroke.get("points", [])]
        radius = _number(stroke.get("radius", 12), "radius", .5, 400)
        if not points:
            raise CommandError("У мазка нужны points")
        pts = [(x / k, y / k) for x, y in points]
        if len(pts) > 1:
            draw.line(pts, fill=255, width=max(1, round(2 * radius / k)), joint="curve")
        for x, y in pts:
            draw.ellipse((x - radius / k, y - radius / k, x + radius / k, y + radius / k), fill=255)
    name = _weight_map_name(inst, bone_id)
    current = project.map(name, create=lambda: Image.new("L", size, rig.DELTA_ZERO))
    if operation in ("add", "subtract"):
        step = round(127 * amount)
        changed = current.point(lambda v: min(255, v + step)) if operation == "add" else current.point(lambda v: max(1, v - step))
        current.paste(changed, mask=region)
    elif operation == "smooth":
        current.paste(current.filter(ImageFilter.GaussianBlur(3)), mask=region)
    else:
        current.paste(rig.DELTA_ZERO, mask=region)
    weights = data.setdefault("weights", [])
    if bone_id not in weights:
        weights.append(bone_id)
    if current.getextrema() == (rig.DELTA_ZERO, rig.DELTA_ZERO):  # nothing left: drop the map
        project.drop_map(name)
        weights.remove(bone_id)
    return {}


def cmd_set_rig_settings(project, cell=None, falloff=None, instance_id=None, source="user"):
    """Mesh cell (px, 3…40) and the blend distance of weights across joints (px)."""
    inst = project.instance(instance_id)
    settings = _rig_data(inst).setdefault("settings", {})
    if cell is not None:
        settings["cell"] = int(_number(cell, "cell", 3, 40))
    if falloff is not None:
        settings["falloff"] = _number(falloff, "falloff", 1, 400)
    return {}


# --- Drawn surfaces -------------------------------------------------------------------------------

def cmd_add_surface(project, part_id, name=None, image=None, above=False, instance_id=None, source="user"):
    """A new drawn layer bound to a part (it moves with the part's bone), empty or from an image
    the application loaded (never a path: the agent paints it with paint_surface)."""
    fd._check_part(part_id)
    inst = project.instance(instance_id)
    data = _rig_data(inst)
    size = (project.doc["source"]["width"], project.doc["source"]["height"])
    if image is not None and not isinstance(image, Image.Image):
        raise CommandError("image передаёт только приложение (импорт из файла)")
    sid = secrets.token_hex(4)
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    if image is not None:
        picture = image.convert("RGBA")
        if picture.size != size:
            picture.thumbnail(size)
        layer.paste(picture, (0, 0))
    project.map(_surface_map_name(inst, sid), create=lambda: layer)
    entry = {"id": sid, "name": str(name or "Дорисовка")[:60], "part_id": part_id, "bone_id": None, "above": bool(above),
             "enabled": True, "source": {"kind": source, "method": "import" if image is not None else "paint", "time": fd._now()},
             "observed": False}
    data.setdefault("surfaces", []).append(entry)
    return {"surface": entry}


def _surface(inst, sid):
    found = next((s for s in _rig_data(inst).get("surfaces", []) if s["id"] == sid), None)
    if found is None:
        raise CommandError(f"Нет дорисовки {sid!r}", "not_found")
    return found


def cmd_paint_surface(project, surface_id, strokes, color=(200, 200, 200), mode="paint", instance_id=None, source="user"):
    """Paints (colour [r, g, b] or [r, g, b, a]) or erases round strokes on a drawn surface."""
    inst = project.instance(instance_id)
    entry = _surface(inst, surface_id)
    if mode not in ("paint", "erase"):
        raise CommandError("mode: paint | erase")
    if not (isinstance(color, (list, tuple)) and len(color) in (3, 4) and all(isinstance(c, int) and 0 <= c <= 255 for c in color)):
        raise CommandError("color: [r, g, b] или [r, g, b, a], 0…255")
    image = project.map(_surface_map_name(inst, surface_id))
    mask = Image.new("L", image.size, 0)
    draw = ImageDraw.Draw(mask)
    for stroke in strokes or []:
        points = [fd._xy(p, project) for p in stroke.get("points", [])]
        radius = _number(stroke.get("radius", 6), "radius", .5, 400)
        if len(points) > 1:
            draw.line([tuple(p) for p in points], fill=255, width=max(1, round(radius * 2)), joint="curve")
        for x, y in points:
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=255)
    if mode == "paint":
        image.paste(tuple(color) + ((255,) if len(color) == 3 else ()), mask=mask)
    else:
        image.paste((0, 0, 0, 0), mask=mask)
    entry["source"] = {"kind": source, "method": "paint", "time": fd._now()}
    return {}


def cmd_set_surface(project, surface_id, enabled=None, above=None, part_id=None, bone_id=None, name=None, remove=False,
                    instance_id=None, source="user"):
    inst = project.instance(instance_id)
    entry = _surface(inst, surface_id)
    if remove:
        _rig_data(inst)["surfaces"].remove(entry)
        project.drop_map(_surface_map_name(inst, surface_id))
        return {}
    if part_id is not None:
        fd._check_part(part_id)
        entry["part_id"] = part_id
    if bone_id is not None:
        if bone_id not in rig.BONE_IDS:
            raise CommandError(f"Нет такой кости: {bone_id!r}")
        entry["bone_id"] = bone_id
    if enabled is not None:
        entry["enabled"] = bool(enabled)
    if above is not None:
        entry["above"] = bool(above)
    if name is not None:
        entry["name"] = str(name)[:60]
    return {}


# --- Reads --------------------------------------------------------------------------------------------

def read_rig(project, instance_id=None):
    inst = project.instance(instance_id)
    built = rig_of(project, inst["id"])
    sk = built.skeleton
    data = _rig_data(inst)
    bones = {b: {"parent": sk.bones[b]["parent"], "head": [round(v, 2) for v in sk.bones[b]["head"]],
                 "tail": [round(v, 2) for v in sk.bones[b]["tail"]], "length": round(sk.bones[b]["length"], 2),
                 "head_joint": sk.bones[b]["head_joint"], "tail_joint": sk.bones[b]["tail_joint"]} for b in sk.order}
    bindings = {fd.VALUE_TO_PART[v]: bs for v, bs in built.mesh.bindings.items()}
    return {"revision": project.doc["revision"], "instance": inst["id"],
            "units": {"angle": "degrees from the rest direction, clockwise on the picture (y down)", "position": "source px"},
            "bones": bones, "missing_bones": [b for b in rig.BONE_IDS if b not in sk.bones],
            "chains": {c: {"bones": [b for b in bs if b in sk.bones], "end": e} for c, (bs, e) in {**rig.CHAINS, **rig.SINGLE}.items()},
            "rotate_at": {j: b for j, b in rig.JOINT_BONE.items() if b in sk.bones},
            "contact_joints": list(rig.CONTACT_CHAINS), "bindings": bindings, "layers": data.get("layers"),
            "constraints": data.get("constraints", {}), "contacts": data.get("contacts", []), "pose": data.get("pose"),
            "pose_locked": bool(data.get("pose_locked")), "weights_edited": data.get("weights", []),
            "surfaces": data.get("surfaces", []), "settings": data.get("settings"),
            "mesh_key": built.key, "mesh": {"cell": built.mesh.cell, "falloff": round(built.mesh.falloff, 1), "vertices": len(built.mesh.vertices),
                     "parts": len(built.mesh.parts)}, "derived_joints": sk.derived}


def read_pose(project, instance_id=None, pose=None, time=None):
    """The solved pose: rotations after IK and contacts, posed joint positions, misses."""
    inst = project.instance(instance_id)
    built = rig_of(project, inst["id"])
    rotations, world, report = built.solve(pose if pose is not None else pose_at(inst, time), time=time)
    joints = built.skeleton.posed_joints(world)
    return {"revision": project.doc["revision"], "time": time, "rotations": {b: round(v, 3) for b, v in rotations.items()},
            "joints": {j: [round(p[0], 2), round(p[1], 2)] for j, p in joints.items()}, "report": report}


def render_pose(project, instance_id=None, pose=None, time=None, overlays=("pose",), region=None, max_side=1024,
                background=(236, 236, 236, 255)):
    """The posed figure with overlays: pose, skeleton, rest (the original, faint), contacts,
    holes (where the rest figure was and nothing is drawn now). Returns (image, transform)."""
    inst = project.instance(instance_id)
    built = rig_of(project, inst["id"])
    rotations, world, report = built.solve(pose if pose is not None else pose_at(inst, time), time=time)
    w, h = project.doc["source"]["width"], project.doc["source"]["height"]
    x1, y1, x2, y2 = [int(round(v)) for v in (region or (0, 0, w, h))]
    if x2 - x1 < 2 or y2 - y1 < 2 or x2 - x1 > 8 * w or y2 - y1 > 8 * h:
        raise CommandError("Неверная область")
    posed = built.render(world, size=(x2 - x1, y2 - y1), offset=(x1, y1), surfaces=surfaces_of(project, inst, built))
    base = Image.new("RGBA", posed.size, tuple(background))
    if "rest" in overlays:
        ghost = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ghost.paste(project.source(), (0, 0))
        ghost.putalpha(ghost.getchannel("A").point(lambda a: a * 35 // 100))
        base.alpha_composite(ghost.crop((x1, y1, x2, y2)) if (x1, y1) != (0, 0) or (x2, y2) != (w, h) else ghost)
    if "pose" in overlays or not overlays:
        base.alpha_composite(posed)
    if "holes" in overlays:
        rest_alpha = Image.new("L", (w, h), 0)
        rest_alpha.paste(project.source().getchannel("A"), (0, 0))
        rest_alpha = rest_alpha.crop((x1, y1, x2, y2)).point(lambda a: 255 if a > 8 else 0)
        empty = posed.getchannel("A").point(lambda a: 255 if a <= 8 else 0)
        hole = ImageChops.multiply(rest_alpha, empty)
        mark = Image.new("RGBA", base.size, fd.UNKNOWN_COLOR + (150,))
        mark.putalpha(hole.point(lambda v: 150 if v else 0))
        base.alpha_composite(mark)
    scale = min(1.0 * max_side / max(base.size), 8.0)
    if abs(scale - 1) > 1e-6:
        base = base.resize((max(1, round(base.width * scale)), max(1, round(base.height * scale))), Image.LANCZOS)
    if "skeleton" in overlays:
        rig.draw_skeleton(base, built.skeleton, world, offset=(x1, y1), scale=scale)
    if "contacts" in overlays:
        draw = ImageDraw.Draw(base)
        for c in _rig_data(inst).get("contacts", []):
            if c.get("enabled", True):
                x, y = (c["point"][0] - x1) * scale, (c["point"][1] - y1) * scale
                draw.rectangle((x - 5, y - 5, x + 5, y + 5), outline=(40, 120, 255), width=2)
    return base, {"scale": scale, "offset": [x1, y1], "source_size": [w, h], "region": [x1, y1, x2, y2],
                  "report": report, "rotations": {b: round(v, 3) for b, v in rotations.items()}}


def read_render_preview(project, time=None, overlays=("pose", "skeleton"), region=None, max_side=768, instance_id=None,
                        pose=None):
    if isinstance(overlays, str):
        overlays = [overlays]
    allowed = {"pose", "skeleton", "rest", "contacts", "holes"}
    if any(o not in allowed for o in overlays):
        raise CommandError(f"overlays: {', '.join(sorted(allowed))}")
    if region is not None and not (isinstance(region, (list, tuple)) and len(region) == 4):
        raise CommandError("region: [x1, y1, x2, y2]")
    image, transform = render_pose(project, instance_id, pose, time, tuple(overlays), region, max(64, min(2048, int(max_side))))
    folder = project.folder / "previews"
    folder.mkdir(exist_ok=True)
    path = folder / f"pose_{int(time_module() * 1000)}.png"
    fd._write_atomic(path, fd._png(image.convert("RGB")))
    for old in sorted(folder.glob("pose_*.png"))[:-30]:
        old.unlink(missing_ok=True)
    return {"revision": project.doc["revision"], "file": str(path), "size": list(image.size), "time": time,
            "overlays": list(overlays), "transform": {k: v for k, v in transform.items() if k not in ("report", "rotations")},
            "report": transform["report"]}


def time_module():
    return time.time()


def read_validate_rig(project, instance_id=None, time=None):
    """Diagnostics of the rig and the current pose (hints, not proof)."""
    inst = project.instance(instance_id)
    built = rig_of(project, inst["id"])
    sk = built.skeleton
    data = _rig_data(inst)
    issues = []

    def add(severity, entity, code, message):
        issues.append({"severity": severity, "entity": entity, "code": code, "message": message})
    body = [b for b in rig.BONE_IDS if "_hand." not in b]
    for b in body:
        if b not in sk.bones:
            add("warning", b, "bone_missing", f"Кость {b} не построена: нет её суставов")
    for side in ("left", "right"):
        if f"{side}_hand" in sk.bones and not any(b.startswith(f"{side}_hand.") for b in sk.bones):
            add("info", f"{side}_hand", "fingers_not_rigged", "Нет точек кисти: пальцы двигаются только вместе с кистью")
    for jid in sk.derived:
        add("info", jid, "joint_derived", f"Сустав {jid} достроен по соседним (его нет в разметке)")
    histogram = project.labels(inst["id"]).histogram()
    if histogram[fd.UNKNOWN]:
        add("info", None, "unknown_follow_neighbours",
            f"{histogram[fd.UNKNOWN]} px без части движутся с соседней частью своей ячейки сетки")
    for v, bones in built.mesh.bindings.items():
        pid = fd.VALUE_TO_PART[v]
        wanted = ((data.get("bindings") or {}).get(pid) or {}).get("bones") or rig.DEFAULT_BINDINGS.get(pid, [])
        if histogram[v] and wanted and not any(b in sk.bones for b in wanted):
            add("info", pid, "binding_fallback", f"{fd.PARTS[pid]['name']}: своих костей нет, движется с {', '.join(bones)}")
    rotations, world, report = built.solve(pose_at(inst, time), time=time)
    for chain, r in report["ik"].items():
        if r["miss_px"] > 2:
            add("warning", chain, "ik_unreached", f"Цель цепочки {chain} не достигнута: промах {r['miss_px']} px")
    for cid, r in report["contacts"].items():
        if r["miss_px"] > 2:
            add("warning", cid, "contact_slipped", f"Опора {cid} не удержана: промах {r['miss_px']} px")
    for b, v in (data.get("pose") or {}).get("rotations", {}).items():
        if b in sk.bones and abs(sk.limit(b, v) - v) > 1e-6:
            add("info", b, "rotation_limited", f"Поворот {b} {v}° ограничен до {round(sk.limit(b, v), 1)}°")
    posed = built.render(world, surfaces=surfaces_of(project, inst, built))
    rest_alpha = project.source().getchannel("A").point(lambda a: 255 if a > 8 else 0)
    hole = ImageChops.multiply(rest_alpha, posed.getchannel("A").point(lambda a: 255 if a <= 8 else 0))
    holes = hole.histogram()[255]
    figure = rest_alpha.histogram()[255] or 1
    if holes > .01 * figure and any(abs(v) > 1e-6 for v in (data.get("pose") or {}).get("rotations", {}).values()):
        add("info", None, "revealed_area", f"Открылось {holes} px, которых на рисунке нет (за сдвинутыми частями); "
                                         f"при необходимости — дорисовка (add_surface)")
    return {"revision": project.doc["revision"], "instance": inst["id"], "issues": issues}


def read_pose_mesh(project, instance_id=None):
    """For the editor: mesh topology per part (rest vertex positions, cells)."""
    inst = project.instance(instance_id)
    built = rig_of(project, inst["id"])
    mesh = built.mesh
    parts = []
    for value in built.layer_order():
        entry = mesh.parts[value]
        verts = list(entry["vertex_weights"])
        index = {v: k for k, v in enumerate(verts)}
        parts.append({"part": fd.VALUE_TO_PART.get(value), "value": value, "verts": verts,
                      "cells": [[index[v] for v in corners] for _, corners in entry["cells"]]})
    return {"revision": project.doc["revision"], "cell": mesh.cell,
            "vertices": [c for v in mesh.vertices for c in v], "parts": parts,
            "size": [project.doc["source"]["width"], project.doc["source"]["height"]]}


def read_pose_eval(project, instance_id=None, pose=None, time=None):
    """For the editor: posed positions of every part's vertices (in pose_mesh order) and joints."""
    inst = project.instance(instance_id)
    built = rig_of(project, inst["id"])
    used = pose if pose is not None else pose_at(inst, time)
    rotations, world, report = built.solve(used, time=time)
    posed = built.mesh.deform(world)
    parts = {}
    for value in built.layer_order():
        positions = posed[value]
        parts[str(value)] = [round(c, 1) for v in built.mesh.parts[value]["vertex_weights"] for c in positions[v]]
    joints = built.skeleton.posed_joints(world)
    bones = {b: [*map(lambda c: round(c, 1), rig.apply(world[b], built.skeleton.bones[b]["head"])),
                 *map(lambda c: round(c, 1), rig.apply(world[b], built.skeleton.bones[b]["tail"]))]
             for b in built.skeleton.order}
    transforms = {b: [round(c, 6) for c in t] for b, t in world.items()}
    return {"parts": parts, "joints": {j: [round(p[0], 1), round(p[1], 1)] for j, p in joints.items()}, "bones": bones,
            "transforms": transforms, "pose": used, "time": time,
            "rotations": {b: round(v, 3) for b, v in rotations.items()}, "report": report}


def weights_layer(project, instance_id, bone):
    """Full-size heat of one bone's weight over the rest figure (for the editor)."""
    inst = project.instance(instance_id)
    built = _bone(project, inst, bone)
    mesh = built.mesh
    size = (project.doc["source"]["width"], project.doc["source"]["height"])
    heat = Image.new("L", size, 0)
    draw = ImageDraw.Draw(heat)
    g = mesh.cell
    for value, entry in mesh.parts.items():
        for _, corners in entry["cells"]:
            w = sum(entry["vertex_weights"][v].get(bone, 0.0) for v in corners) / 4
            if w > .01:
                x, y = mesh.vertices[corners[0]]
                draw.rectangle((x, y, x + g - 1, y + g - 1), fill=round(255 * w))
    heat = heat.filter(ImageFilter.BoxBlur(g / 2))
    figure = mesh.labels.point(lambda v: 255 if v else 0)
    out = Image.new("RGBA", size, (255, 70, 30, 255))
    out.putalpha(ImageChops.multiply(heat, figure).point(lambda v: v * 200 // 255))
    return out


COMMANDS = {"set_joint_rotation": cmd_set_joint_rotation, "set_root": cmd_set_root, "set_ik_target": cmd_set_ik_target,
            "reset_pose": cmd_reset_pose, "set_pose_locked": cmd_set_pose_locked, "set_contact": cmd_set_contact,
            "set_constraint": cmd_set_constraint, "set_layer_order": cmd_set_layer_order, "set_binding": cmd_set_binding,
            "edit_weights": cmd_edit_weights, "set_rig_settings": cmd_set_rig_settings, "add_surface": cmd_add_surface,
            "paint_surface": cmd_paint_surface, "set_surface": cmd_set_surface}
READS = {"get_rig": read_rig, "get_pose": read_pose, "render_preview": read_render_preview, "validate_rig": read_validate_rig,
         "pose_mesh": read_pose_mesh, "pose_eval": read_pose_eval}
fd.COMMANDS.update(COMMANDS)
fd.READS.update(READS)
