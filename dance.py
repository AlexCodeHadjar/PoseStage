"""Dance phrases (docs/DANCE.md, stage 1): poses that flow into each other, ordered by how the
body moves, found from the pose skeletons (version 2, docs/SKELETON.md).

A pose becomes a feature set: 10 segment angles (torso, neck, upper arms, forearms, thighs,
shins), the wrists and feet relative to the pelvis in torso lengths, its support mode, view and
facing.
Each pose also exists mirrored (x flipped, sides swapped). The transition cost A → B weighs
the angle and end changes plus penalties for changing support mode, view or facing; near-identical poses
are duplicates, not motion. Phrases are 4-8 step paths with small, consistently directed
steps (low second derivative), found by beam search and picked so they barely overlap.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

SEGMENTS = (  # name, from, to, weight
    ("torso", "pelvis", "chest", 1.5), ("neck", "chest", "head", .6),
    ("upper_arm_l", "shoulder_l", "elbow_l", 1.0), ("upper_arm_r", "shoulder_r", "elbow_r", 1.0),
    ("forearm_l", "elbow_l", "wrist_l", .8), ("forearm_r", "elbow_r", "wrist_r", .8),
    ("thigh_l", "hip_l", "knee_l", 1.0), ("thigh_r", "hip_r", "knee_r", 1.0),
    ("shin_l", "knee_l", "foot_l", .8), ("shin_r", "knee_r", "foot_r", .8),
)
ENDS = ("wrist_l", "wrist_r", "foot_l", "foot_r")
END_WEIGHT = .5
STATE_WEIGHT = {"visible": 1.0, "hidden": .4}
SUPPORT_PENALTY = .6  # stand ↔ kneel ↔ sit: possible, but a big change
FACING_PENALTY = .5  # a turned figure suddenly looking the other way (often a mirror) jumps
VIEW_PENALTY = {frozenset({"front", "three_quarter"}): .2, frozenset({"three_quarter", "side"}): .3,
                frozenset({"front", "side"}): .8, frozenset({"back", "three_quarter"}): .5,
                frozenset({"back", "side"}): .6}
DUPLICATE = .12  # below this the poses are the same moment (e.g. dancers of one photo)
FLOW = .55  # above this the change is too big to look like one movement
NEIGHBOURS = 8
BEAM = 24
MIN_LENGTH, MAX_LENGTH = 4, 8
LENGTH_BONUS = .05  # per pose beyond MIN_LENGTH, against the mean step cost


def wrap(angle):
    return (angle + math.pi) % (2 * math.pi) - math.pi


def photo_of(file: str) -> str:
    """Poses cut from one photo share its id (photo-000045__pose-02 → photo-000045)."""
    return re.sub(r"__pose-\d+$", "", file.rsplit("/", 1)[-1].rsplit(".", 1)[0], flags=re.I)


def features(pose: dict, mirror: bool = False) -> dict | None:
    """Normalised skeleton features, or None without a usable skeleton."""
    joints = pose.get("joints")
    if not (isinstance(joints, dict) and joints.get("version") == 2):
        return None
    points = joints["points"]
    pelvis, chest = points["pelvis"]["xy"], points["chest"]["xy"]
    torso = math.hypot(chest[0] - pelvis[0], chest[1] - pelvis[1])
    if torso < 1:
        return None
    swap = (lambda key: key.replace("_l", "_R").replace("_r", "_l").replace("_R", "_r")) if mirror else (lambda key: key)

    def at(key):
        point = points.get(swap(key)) or {}
        if point.get("state", "absent") == "absent" or not point.get("xy"):
            return None, 0.0
        x, y = (point["xy"][0] - pelvis[0]) / torso, (point["xy"][1] - pelvis[1]) / torso
        return ((-x if mirror else x), y), STATE_WEIGHT.get(point.get("state"), 0.0)
    angles, weights = {}, {}
    for name, a, b, weight in SEGMENTS:
        (pa, wa), (pb, wb) = at(a), at(b)
        if pa is None or pb is None:
            continue
        angles[name] = math.atan2(pb[1] - pa[1], pb[0] - pa[0])
        weights[name] = weight * min(wa, wb)
    ends = {key: at(key) for key in ENDS}
    mode = (pose.get("support") or {}).get("mode", "feet")
    view = joints.get("view", "front")
    return {"file": pose["file"], "mirror": mirror, "angles": angles, "weights": weights,
            "ends": {k: v for k, v in ends.items() if v[0] is not None}, "mode": mode,
            "view": view, "facing": facing(points, view, mirror), "photo": photo_of(pose["file"])}


def facing(points: dict, view: str, mirror: bool = False) -> int:
    """Which way a turned figure looks: +1 toward the image's right, -1 left, 0 unknown or
    frontal. A figure turned to the right shows its left side, so that side's limbs are the
    visible ones; a mirror turns it the other way."""
    if view not in ("side", "three_quarter"):
        return 0
    seen = {side: sum(STATE_WEIGHT.get((points.get(f"{limb}_{side}") or {}).get("state"), 0.0)
                      for limb in ("shoulder", "elbow", "wrist", "hip", "knee", "foot")) for side in "lr"}
    if abs(seen["l"] - seen["r"]) < 1:
        return 0
    side = 1 if seen["l"] > seen["r"] else -1
    return -side if mirror else side


def motion(a: dict, b: dict) -> tuple[float, dict]:
    """(weighted body change A → B, per-feature velocity) — no penalties."""
    total, weight_sum, velocity = 0.0, 0.0, {}
    for name, _, _, _ in SEGMENTS:
        if name in a["angles"] and name in b["angles"]:
            weight = min(a["weights"][name], b["weights"][name])
            delta = wrap(b["angles"][name] - a["angles"][name])
            velocity[name] = delta
            total += weight * abs(delta)
            weight_sum += weight
    for key in ENDS:
        if key in a["ends"] and key in b["ends"]:
            (ax, ay), wa = a["ends"][key]
            (bx, by), wb = b["ends"][key]
            weight = END_WEIGHT * min(wa, wb)
            velocity[key + "_x"], velocity[key + "_y"] = bx - ax, by - ay
            total += weight * math.hypot(bx - ax, by - ay)
            weight_sum += weight
    return (total / weight_sum if weight_sum else math.inf), velocity


def transition_cost(a: dict, b: dict) -> float:
    """How far B is from A as one movement (∞ when it cannot flow, e.g. front → back)."""
    change, _ = motion(a, b)
    views = frozenset({a["view"], b["view"]})
    if views == frozenset({"front", "back"}):
        return math.inf
    penalty = VIEW_PENALTY.get(views, 0.0) if len(views) > 1 else 0.0
    if a["mode"] != b["mode"]:
        penalty += SUPPORT_PENALTY
    if a["facing"] * b["facing"] < 0:
        penalty += FACING_PENALTY
    return change + penalty


def acceleration(v1: dict, v2: dict) -> float:
    """How much the motion changes direction between two consecutive steps (0 = same)."""
    keys = set(v1) & set(v2)
    if not keys:
        return 1.0
    dot = sum(v1[k] * v2[k] for k in keys)
    n1 = math.sqrt(sum(v1[k] ** 2 for k in keys)) or 1e-9
    n2 = math.sqrt(sum(v2[k] ** 2 for k in keys)) or 1e-9
    return (1 - dot / (n1 * n2)) / 2  # 0 when the steps go the same way, 1 when opposite


# --- Drawing style: hair, top, shorts and shoes must match for a convincing phrase ---------------
STYLE_PATCHES = (  # skeleton point, patch radius in torso lengths
    ("head", .5), ("chest", .45), ("pelvis", .5), ("foot_l", .3), ("foot_r", .3))
STYLE_BINS = 8  # grey-level histogram bins of the drawing's own pixels in each patch
STYLE_WEIGHT = 1.4  # style distance (0..1) → transition cost
OUTFIT_MAX = .15  # neighbours in a phrase never differ more: a new outfit always looks like a jump


def style_signature(pose: dict, image) -> dict | None:
    """Per body region (head, chest, pelvis, feet): the tone histogram of the drawing's own
    pixels (alpha > 8), its share of dark pixels and how much of the patch is drawn. Histograms ignore where the limbs are,
    so they describe the outfit, hair and shoes rather than the pose."""
    joints = pose.get("joints")
    if not (isinstance(joints, dict) and joints.get("version") == 2):
        return None
    points = joints["points"]
    pelvis, chest = points["pelvis"]["xy"], points["chest"]["xy"]
    torso = math.hypot(chest[0] - pelvis[0], chest[1] - pelvis[1])
    if torso < 1:
        return None
    rgba = image.convert("RGBA")
    grey, alpha = rgba.convert("L"), rgba.getchannel("A")
    out = {}
    for key, radius in STYLE_PATCHES:
        point = points.get(key) or {}
        if not point.get("xy") or point.get("state") == "absent":
            continue
        x, y, r = point["xy"][0], point["xy"][1], radius * torso
        box = (round(x - r), round(y - r), round(x + r), round(y + r))
        patch_alpha = list(alpha.crop(box).getdata())
        values = [g for g, a in zip(grey.crop(box).getdata(), patch_alpha) if a > 8]
        if len(values) < 50:
            continue
        coverage = len(values) / max(1, len(patch_alpha))  # shoes, a ponytail: more drawing here
        histogram = [0] * STYLE_BINS
        for g in values:
            histogram[min(STYLE_BINS - 1, g * STYLE_BINS // 256)] += 1
        out[key] = [h / len(values) for h in histogram] + [sum(g < 110 for g in values) / len(values), coverage]
    return out


def style_distance(a: dict | None, b: dict | None, mirror_a=False, mirror_b=False) -> float:
    """0 (same outfit) … 1 (very different); 0.5 when unknown."""
    if not a or not b:
        return .5

    def get(signature, key, mirror):
        if mirror and key.startswith("foot_"):
            key = "foot_r" if key == "foot_l" else "foot_l"
        return signature.get(key)
    total, count = 0.0, 0
    for key, _ in STYLE_PATCHES:
        va, vb = get(a, key, mirror_a), get(b, key, mirror_b)
        if va is None or vb is None:
            continue
        histogram = sum(abs(p - q) for p, q in zip(va[:-2], vb[:-2])) / 2  # 0..1
        dark = min(1.0, abs(va[-2] - vb[-2]) * 3)
        # Feet and head: how much of the area is drawing tells shoes / bare feet and hair.
        cover = min(1.0, abs(va[-1] - vb[-1]) * (5 if key in ("head", "foot_l", "foot_r") else 2))
        total += .35 * histogram + .3 * dark + .35 * cover
        count += 1
    return total / count if count else .5


STYLE_VERSION = 3
STYLE_CACHE_SIZE = 3000


def load_styles(pose_list: list[dict], resolve, cache: Path | None = None) -> dict:
    """{file: style_signature} for a collection. Reading every drawing takes a while, so the
    signatures are kept in DATA_DIR/styles.json, keyed by the file (path, size, time) and the
    skeleton points they were measured at."""
    from PIL import Image, ImageOps
    import poses as store
    path = store.DATA_DIR / "styles.json" if cache is None else cache
    saved = store.read_dict(path)
    entries = saved.get("entries") if saved.get("version") == STYLE_VERSION else None
    entries = entries if isinstance(entries, dict) else {}
    out, fresh = {}, {}
    for pose in pose_list:
        source = resolve(pose["file"])
        try:
            stat = source.stat()
        except (AttributeError, OSError):
            continue
        points = (pose.get("joints") or {}).get("points") or {}
        anchors = [(points.get(k) or {}).get("xy") if (points.get(k) or {}).get("state") != "absent" else None
                   for k in (key for key, _ in STYLE_PATCHES)]
        key = f"{source}|{stat.st_size}|{stat.st_mtime_ns}|{json.dumps(anchors)}"
        if key in entries:
            signature = entries[key]
        else:
            try:
                with Image.open(source) as opened:
                    image = ImageOps.exif_transpose(opened)  # the box and skeleton are measured upright
                    signature = style_signature(pose, image)
            except Exception:  # unreadable, truncated or oversized (DecompressionBombError)
                signature = None
        fresh[key] = signature
        if signature:
            out[pose["file"]] = signature
    if fresh != entries:
        # Keep other collections' entries too (most recent first) so switching does not recompute.
        kept = dict(fresh)
        for key, value in entries.items():
            if len(kept) >= STYLE_CACHE_SIZE:
                break
            kept.setdefault(key, value)
        if kept != entries:
            store.write_json(path, {"version": STYLE_VERSION, "entries": kept})
    return out


def mirror_pose(pose: dict) -> dict:
    """The pose flipped left-right: box, support profile and skeleton (sides swapped)."""
    w = pose["w"]
    x1, y1, x2, y2 = pose["box"]
    flipped = dict(pose, box=[w - x2, y1, w - x1, y2])
    support = pose.get("support")
    if isinstance(support, dict) and isinstance(support.get("profile"), list):
        flipped["support"] = dict(support, profile=sorted([[w - x, y] for x, y in support["profile"]]))
    joints = pose.get("joints")
    if isinstance(joints, dict) and isinstance(joints.get("points"), dict):
        swap = lambda key: key.replace("_l", "_R").replace("_r", "_l").replace("_R", "_r")
        flipped["joints"] = dict(joints, points={
            swap(key): (dict(point, xy=[w - point["xy"][0], point["xy"][1]]) if point.get("xy") else dict(point))
            for key, point in joints["points"].items()})
    return flipped


def build_graph(poses: list[dict], styles: dict | None = None):
    """Nodes: every pose and its mirror; edges: the nearest flowing transitions. With
    `styles` ({file: style_signature}) a change of outfit, hair or shoes costs extra."""
    nodes = []
    for pose in poses:
        for mirror in (False, True):
            node = features(pose, mirror)
            if node:
                nodes.append(node)
    edges = {}
    for i, a in enumerate(nodes):
        candidates = []
        for j, b in enumerate(nodes):
            if a["file"] == b["file"]:
                continue
            cost = transition_cost(a, b)
            if not DUPLICATE <= cost <= FLOW:  # the body change itself must look like one move
                continue
            if styles is not None:  # then outfit, hair and shoes must match well enough
                known = styles.get(a["file"]) and styles.get(b["file"])
                style = style_distance(styles.get(a["file"]), styles.get(b["file"]), a["mirror"], b["mirror"])
                if known and style > OUTFIT_MAX:  # unknown outfit (unreadable drawing): only the cost
                    continue
                cost += STYLE_WEIGHT * style
            candidates.append((cost, j))
        candidates.sort()
        edges[i] = candidates[:NEIGHBOURS]
    return nodes, edges


def search(nodes, edges):
    """All good paths of MIN..MAX_LENGTH poses (beam search from every node)."""
    found = []
    for start in range(len(nodes)):
        beam = [(0.0, [start], None)]  # (score, path, last velocity)
        for _ in range(MAX_LENGTH - 1):
            grown = []
            for score, path, last in beam:
                used = {nodes[k]["file"] for k in path}
                for cost, nxt in edges[path[-1]]:
                    node = nodes[nxt]
                    if node["file"] in used:
                        continue
                    _, velocity = motion(nodes[path[-1]], node)
                    turn = acceleration(last, velocity) if last is not None else 0.0
                    grown.append((score + cost + .8 * turn, path + [nxt], velocity))
            if not grown:
                break
            grown.sort(key=lambda item: item[0] / len(item[1]))
            beam = grown[:BEAM]
            for score, path, _ in beam:
                if len(path) >= MIN_LENGTH:
                    found.append((score / (len(path) - 1), path))
    return found


def pick(found, nodes, limit=40):
    """Best phrases that barely overlap (a pose appears in at most one phrase)."""
    # A longer phrase reads more like dancing: each extra pose is worth a little cost.
    ranked = sorted(found, key=lambda item: item[0] - LENGTH_BONUS * (len(item[1]) - MIN_LENGTH))
    taken, phrases = set(), []
    for quality, path in ranked:
        files = [nodes[k]["file"] for k in path]
        if any(file in taken for file in files):
            continue
        taken.update(files)
        phrases.append((quality, path))
        if len(phrases) >= limit:
            break
    return phrases


# Movements a phrase can be named after: (name up, name down, threshold), measured per pose.
def _arms_height(node):  # wrists above the pelvis, torso lengths (y grows downward)
    values = [-node["ends"][k][0][1] for k in ("wrist_l", "wrist_r") if k in node["ends"]]
    return sum(values) / len(values) if values else None


def _arms_width(node):
    values = [abs(node["ends"][k][0][0]) for k in ("wrist_l", "wrist_r") if k in node["ends"]]
    return sum(values) / len(values) if values else None


def _tilt(node):  # torso lean, radians from upright; positive = chest toward the image's right
    angle = node["angles"].get("torso")
    return None if angle is None else wrap(angle + math.pi / 2)


def _step(node):
    ends = node["ends"]
    return abs(ends["foot_l"][0][0] - ends["foot_r"][0][0]) if "foot_l" in ends and "foot_r" in ends else None


def _weight(node):  # pelvis over the feet: positive = pelvis toward the image's right
    ends = node["ends"]
    return -(ends["foot_l"][0][0] + ends["foot_r"][0][0]) / 2 if "foot_l" in ends and "foot_r" in ends else None


MOVES = (  # measure, name when it grows, name when it falls, name when it goes back and forth, scale
    (_arms_height, "Руки вверх", "Руки вниз", "Руки волной", .5),
    (_arms_width, "Руки в стороны", "Руки к телу", "Руки волной", .5),
    (_tilt, "Наклон вправо", "Наклон влево", "Покачивание корпусом", .25),
    (_weight, "Перенос веса вправо", "Перенос веса влево", "Покачивание бёдрами", .35),
    (_step, "Шаг шире", "Шаг уже", "Переступание", .4),
)


def name_of(path: list[dict]) -> str:
    """A short Russian name for the phrase's main movement over all its poses. A measure that
    ends far from where it started names the direction; one that travels a lot but comes back
    names the back-and-forth («Руки волной»); nothing large → «Волна»."""
    scored = []
    if path[0]["mode"] != path[-1]["mode"]:
        order = {"feet": 0, "kneel": 1, "ground": 2}
        down = order.get(path[-1]["mode"], 0) > order.get(path[0]["mode"], 0)
        scored.append((9.0, "Опускание" if down else "Подъём"))
    for measure, grows, falls, back_and_forth, scale in MOVES:
        values = [v for v in map(measure, path) if v is not None]
        if len(values) < 2:
            continue
        net = (values[-1] - values[0]) / scale
        travel = sum(abs(b - a) for a, b in zip(values, values[1:])) / scale
        if abs(net) >= 1:
            scored.append((abs(net), grows if net > 0 else falls))
        elif travel >= 2.5:
            scored.append((travel / 2.5, back_and_forth))
    names = []
    for _, name in sorted(scored, reverse=True):
        if name not in names:
            names.append(name)
    return " · ".join(names[:2]) or "Волна"


def find_phrases(poses: list[dict], limit=40, styles: dict | None = None) -> list[dict]:
    """Phrases for a collection: [{id, name, poses: [{file, mirror}], loop, score}]."""
    nodes, edges = build_graph(poses, styles)
    phrases = []
    counts = {}
    for quality, path in pick(search(nodes, edges), nodes, limit):
        first, last = nodes[path[0]], nodes[path[-1]]
        name = name_of([nodes[k] for k in path])
        counts[name] = counts.get(name, 0) + 1
        loop = DUPLICATE <= transition_cost(last, first) <= FLOW and (
            styles is None or not (styles.get(last["file"]) and styles.get(first["file"]))
            or style_distance(styles[last["file"]], styles[first["file"]], last["mirror"], first["mirror"]) <= OUTFIT_MAX)
        phrases.append({"id": f"s{len(phrases) + 1:02d}",
                        "name": name if counts[name] == 1 else f"{name} {counts[name]}",
                        "poses": [{"file": nodes[k]["file"], "mirror": nodes[k]["mirror"]} for k in path],
                        "loop": loop, "score": round(quality, 4)})
    return phrases


# --- Storyboards: dances already ordered and timed by their author --------------------------------
# A frame of a storyboard dance is named dance_<NN>_<NAME>__<frame>__<seconds>s__pose-<k>.png
# (e.g. dance_04_TWICE_TT__003__01.000s__pose-01.png): its place and time in the dance are given,
# nothing has to be matched. Other dance_<NN>_* drawings (overview sheets) repeat those frames.
STORYBOARD_FRAME = re.compile(r"^dance_(\d+)_(.+?)__(\d{3})__(\d+(?:\.\d+)?)s__pose-\d+\.\w+$", re.I)
DANCE_DRAWING = re.compile(r"^dance_\d+_", re.I)


def is_dance_drawing(file: str) -> bool:
    """A drawing of a storyboard dance: never matched into found phrases."""
    return bool(DANCE_DRAWING.match(file.rsplit("/", 1)[-1]))


def storyboard_titles(root: Path | None) -> dict:
    """{"01_JISOO_FLOWER": "JISOO — FLOWER"} from the storyboards' manifest.json files (a folder
    of the collection, one level down), if there are any."""
    titles = {}
    for manifest in (root.glob("*/manifest.json") if root and root.is_dir() else ()):
        try:
            entries = json.loads(manifest.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        for entry in entries if isinstance(entries, list) else ():
            if isinstance(entry, dict) and isinstance(entry.get("folder"), str) and isinstance(entry.get("title"), str):
                titles[entry["folder"].upper()] = entry["title"]
    return titles


def between_skeleton(pose: dict, before: dict | None, after: dict | None) -> dict | None:
    """A skeleton for a storyboard frame the detector missed: the middle of its neighbours'
    skeletons (0.5 s apart), each taken relative to its own box and fitted to this frame's box."""
    def relative(neighbour):
        joints = (neighbour or {}).get("joints")
        if not (isinstance(joints, dict) and joints.get("version") == 2):
            return None
        x1, y1, x2, y2 = neighbour["box"]
        return joints, {k: ((p["xy"][0] - x1) / max(1, x2 - x1), (p["xy"][1] - y1) / max(1, y2 - y1))
                        for k, p in joints["points"].items() if p.get("xy") and p.get("state") != "absent"}
    sides = [r for r in (relative(before), relative(after)) if r]
    if not sides:
        return None
    x1, y1, x2, y2 = pose["box"]
    points = {}
    for key in sides[0][0]["points"]:
        found = [rel[key] for _, rel in sides if key in rel]
        if not found:
            points[key] = {"state": "absent"}
            continue
        u = sum(f[0] for f in found) / len(found)
        v = sum(f[1] for f in found) / len(found)
        points[key] = {"xy": [round(x1 + u * (x2 - x1), 1), round(y1 + v * (y2 - y1), 1)], "state": "visible"}
    return {"version": 2, "view": sides[0][0].get("view", "front"), "points": points}


def storyboards(poses: list[dict], titles: dict | None = None) -> list[dict]:
    """The storyboard dances of a collection, frames in their order:
    [{id, name, kind: "storyboard", poses: [{file, mirror, joints?}], times, bpm, loop, score}].
    One pose per count; `bpm` is the dance's own tempo (17 frames 0.5 s apart → 120). A frame
    without a skeleton gets one between its neighbours' (`joints`)."""
    groups = {}
    for pose in poses:
        found = STORYBOARD_FRAME.match(pose["file"].rsplit("/", 1)[-1])
        if found:
            number, name, frame, seconds = found.groups()
            groups.setdefault((number, name.upper()), {}).setdefault(int(frame), (float(seconds), pose))
    out = []
    for (number, name), frames in sorted(groups.items()):
        ordered = [frames[k] for k in sorted(frames)]
        if len(ordered) < 2:
            continue
        times = [t for t, _ in ordered]
        steps = sorted(b - a for a, b in zip(times, times[1:]) if b > a)
        beat = steps[len(steps) // 2] if steps else .5
        title = (titles or {}).get(f"{number}_{name}") or name.replace("_", " ")
        entries = []
        for i, (_, pose) in enumerate(ordered):
            entry = {"file": pose["file"], "mirror": False}
            if (pose.get("joints") or {}).get("version") != 2:
                filled = between_skeleton(pose, ordered[i - 1][1] if i else ordered[-1][1],
                                          ordered[(i + 1) % len(ordered)][1])
                if filled:
                    entry["joints"] = filled
            entries.append(entry)
        out.append({"id": f"board-{number}", "name": title, "kind": "storyboard", "loop": True,
                    "poses": entries, "times": times, "bpm": max(60, min(200, round(60 / beat))), "score": 0})
    return out
