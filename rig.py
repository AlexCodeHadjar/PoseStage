"""Posing a marked-up figure (docs/RIG.md): bones, a deformation mesh with editable weights,
FK / IK with limits and contacts, layer order and drawn surfaces. Pillow only, no numpy.

This is the one evaluator of a pose: the editor (it draws the vertex positions computed here),
the agent's render_preview and the export all go through `Rig.solve` + `Rig.deform`, and the
reference picture is `Rig.render`.

Coordinates are source pixels (y down). An angle is in degrees, positive = clockwise on the
picture; a bone's rotation is relative to its rest direction (the document's joints are the rest
skeleton; posing never moves them).

Deformation: a regular grid over the figure. Every vertex takes the part its cells mostly show;
it starts bound to the nearest bone of that part, then weights are smoothed across the borders
of anatomically adjacent parts only (upper arm ↔ forearm at the elbow, never forearm ↔ torso
where an arm lies over the body), so joints bend softly and overlapping limbs do not drag the
body along. User weight edits are stored as per-bone delta maps and added before normalising.
Every part is drawn as its own layer in the layer order, through the same vertex positions where
parts touch, rigidly with its own bones where it only overlaps another part.
"""
from __future__ import annotations

import math

from PIL import Image, ImageChops, ImageDraw

import figure_doc as fd

DELTA_SCALE = 4  # weight delta maps: one value per 4×4 source pixels
DELTA_ZERO = 128  # stored value meaning "no change"; ±127 = ±1 weight


def _bones():
    out = [("pelvis", None, "pelvis", "navel", None), ("spine", "pelvis", "navel", "chest", None),
           ("neck", "spine", "chest", "neck", None), ("head", "neck", "neck", "head", .7)]
    for s, side in (("l", "left"), ("r", "right")):
        out += [(f"{side}_clavicle", "spine", "chest", f"shoulder_{s}", None),
                (f"{side}_upper_arm", f"{side}_clavicle", f"shoulder_{s}", f"elbow_{s}", None),
                (f"{side}_forearm", f"{side}_upper_arm", f"elbow_{s}", f"wrist_{s}", None),
                (f"{side}_hand", f"{side}_forearm", f"wrist_{s}", f"{side}_hand.middle.mcp", .4)]
        for finger, joints in (("thumb", ("cmc", "mcp", "ip", "tip")), ("index", ("mcp", "pip", "dip", "tip")),
                               ("middle", ("mcp", "pip", "dip", "tip")), ("ring", ("mcp", "pip", "dip", "tip")),
                               ("pinky", ("mcp", "pip", "dip", "tip"))):
            parent = f"{side}_hand"
            for k, (a, b) in enumerate(zip(joints, joints[1:]), 1):
                bone = f"{side}_hand.{finger}.{k}"
                out.append((bone, parent, f"{side}_hand.{finger}.{a}", f"{side}_hand.{finger}.{b}", None))
                parent = bone
        out += [(f"{side}_hip", "pelvis", "pelvis", f"hip_{s}", None),
                (f"{side}_thigh", f"{side}_hip", f"hip_{s}", f"knee_{s}", None),
                (f"{side}_shin", f"{side}_thigh", f"knee_{s}", f"foot_{s}", None),
                (f"{side}_foot", f"{side}_shin", f"foot_{s}", f"toe_{s}", .3)]
    return out


# (bone id, parent, head joint, tail joint, extension): a bone without its tail joint points along
# its parent by `extension` × the parent's length (head, hand without landmarks, foot without toe).
BONES = _bones()
BONE_IDS = [b[0] for b in BONES]
PARENT = {b[0]: b[1] for b in BONES}


def _part_bones():
    out = {"head": ["head"], "neck": ["neck"], "chest": ["spine", "left_clavicle", "right_clavicle"],
           "abdomen": ["spine", "pelvis"], "pelvis": ["pelvis", "left_hip", "right_hip"]}
    for side in ("left", "right"):
        out.update({f"{side}_upper_arm": [f"{side}_upper_arm"], f"{side}_forearm": [f"{side}_forearm"],
                    f"{side}_hand.palm": [f"{side}_hand"], f"{side}_hand.unsplit": [f"{side}_hand"],
                    f"{side}_thigh": [f"{side}_thigh"], f"{side}_shin": [f"{side}_shin"], f"{side}_foot": [f"{side}_foot"]})
        for finger in ("thumb", "index", "middle", "ring", "pinky"):
            out[f"{side}_hand.{finger}"] = [f"{side}_hand.{finger}.{k}" for k in (1, 2, 3)]
    return out


DEFAULT_BINDINGS = _part_bones()


def _chains():
    out = {"head": (["neck", "head"], "head")}
    for s, side in (("l", "left"), ("r", "right")):
        out[f"{side}_arm"] = ([f"{side}_upper_arm", f"{side}_forearm"], f"wrist_{s}")
        out[f"{side}_leg"] = ([f"{side}_thigh", f"{side}_shin"], f"foot_{s}")
        for finger in ("thumb", "index", "middle", "ring", "pinky"):
            out[f"{side}_hand.{finger}"] = ([f"{side}_hand.{finger}.{k}" for k in (1, 2, 3)], f"{side}_hand.{finger}.tip")
    return out


CHAINS = _chains()  # chain id -> (bones root → end, end joint): IK targets
# Joints a contact can pin, and the chain that moves to keep it there.
CONTACT_CHAINS = {}
for _s, _side in (("l", "left"), ("r", "right")):
    CONTACT_CHAINS.update({f"wrist_{_s}": f"{_side}_arm", f"foot_{_s}": f"{_side}_leg", f"heel_{_s}": f"{_side}_leg",
                           f"toe_{_s}": f"{_side}_leg", f"elbow_{_s}": f"{_side}_upper_arm", f"knee_{_s}": f"{_side}_thigh"})
    for _f in ("thumb", "index", "middle", "ring", "pinky"):
        CONTACT_CHAINS[f"{_side}_hand.{_f}.tip"] = f"{_side}_hand.{_f}"
SINGLE = {f"{side}_upper_arm": ([f"{side}_upper_arm"], f"elbow_{s}") for s, side in (("l", "left"), ("r", "right"))}
SINGLE.update({f"{side}_thigh": ([f"{side}_thigh"], f"knee_{s}") for s, side in (("l", "left"), ("r", "right"))})
# The joint a rotation "at" names: rotating at the elbow turns the forearm.
JOINT_BONE = {"pelvis": "pelvis", "navel": "spine", "chest": "neck", "neck": "head"}
for _s, _side in (("l", "left"), ("r", "right")):
    JOINT_BONE.update({f"shoulder_{_s}": f"{_side}_upper_arm", f"elbow_{_s}": f"{_side}_forearm", f"wrist_{_s}": f"{_side}_hand",
                       f"hip_{_s}": f"{_side}_thigh", f"knee_{_s}": f"{_side}_shin", f"foot_{_s}": f"{_side}_foot"})
    for _f, _js in (("thumb", ("cmc", "mcp", "ip")), ("index", ("mcp", "pip", "dip")), ("middle", ("mcp", "pip", "dip")),
                    ("ring", ("mcp", "pip", "dip")), ("pinky", ("mcp", "pip", "dip"))):
        for _k, _j in enumerate(_js, 1):
            JOINT_BONE[f"{_side}_hand.{_f}.{_j}"] = f"{_side}_hand.{_f}.{_k}"


def default_layers():
    """Back to front. It matters only once parts move over each other: set_layer_order."""
    order = []
    for side in ("right", "left"):
        order += [f"{side}_foot", f"{side}_shin", f"{side}_thigh"]
    order += ["pelvis", "abdomen", "chest", "neck", "head"]
    for side in ("right", "left"):
        order += [f"{side}_upper_arm", f"{side}_forearm", f"{side}_hand.unsplit", f"{side}_hand.palm",
                  *(f"{side}_hand.{f}" for f in ("thumb", "index", "middle", "ring", "pinky"))]
    return order


def default_rig():
    return {"version": 1, "bindings": {}, "layers": default_layers(), "constraints": {}, "contacts": [],
            "surfaces": [], "weights": [], "settings": {"cell": None, "falloff": None},
            "pose": {"root": [0.0, 0.0], "rotations": {}, "ik": {}},
            "animation": {"duration": 2.0, "fps": 24, "loop": True, "tracks": {}}}


# --- Affine 2D: (a, b, c, d, e, f): x' = a x + b y + e, y' = c x + d y + f ----------------------

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def compose(t1, t2):
    """t1 ∘ t2: apply t2, then t1."""
    a1, b1, c1, d1, e1, f1 = t1
    a2, b2, c2, d2, e2, f2 = t2
    return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2, c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            a1 * e2 + b1 * f2 + e1, c1 * e2 + d1 * f2 + f1)


def rotation(angle, pivot):
    c, s = math.cos(angle), math.sin(angle)
    px, py = pivot
    return (c, -s, s, c, px - c * px + s * py, py - s * px - c * py)


def apply(t, p):
    return (t[0] * p[0] + t[1] * p[1] + t[4], t[2] * p[0] + t[3] * p[1] + t[5])


def _angle(v):
    return math.atan2(v[1], v[0])


def _wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def _segment_distance(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy or 1.0)))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


# --- Skeleton ---------------------------------------------------------------------------------

def rest_joints(joints):
    """Rest positions from the document's joints, with the derivable ones filled in (a library
    skeleton has no neck, heel or toe). Returns (positions, derived ids)."""
    P = {k: tuple(v["xy"]) for k, v in joints.items() if v.get("xy") and v.get("state") != "absent"}
    derived = []

    def lerp(a, b, t):
        return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
    if "chest" not in P and "shoulder_l" in P and "shoulder_r" in P and "pelvis" in P:
        P["chest"] = lerp(lerp(P["shoulder_l"], P["shoulder_r"], .5), P["pelvis"], .15)
        derived.append("chest")
    if "navel" not in P and "chest" in P and "pelvis" in P:
        P["navel"] = lerp(P["chest"], P["pelvis"], .55)
        derived.append("navel")
    if "neck" not in P and "chest" in P and "head" in P:
        P["neck"] = lerp(P["chest"], P["head"], .4)
        derived.append("neck")
    return P, derived


class Skeleton:
    """Bones placed on the rest joints; FK, IK, limits and contacts."""

    def __init__(self, joints, constraints=None):
        self.rest, self.derived = rest_joints(joints)
        self.constraints = constraints or {}
        # optional bend side of a two-bone chain, by its first bone: {"left_thigh": {"bend": 1 | -1}}
        self.bends = {b: c.get("bend") for b, c in self.constraints.items() if isinstance(c, dict)}
        self.bones = {}  # id -> {"parent", "head", "tail", "length", "tail_joint"}
        self.order = []
        for bid, parent, head, tail, ext in BONES:
            if parent is not None and parent not in self.bones:
                continue
            if head not in self.rest:
                continue
            h = self.rest[head]
            if tail in self.rest and math.dist(self.rest[tail], h) > .5:
                t = self.rest[tail]
            elif ext is not None and parent is not None:
                ph, pt = self.bones[parent]["head"], self.bones[parent]["tail"]
                t = (h[0] + (pt[0] - ph[0]) * ext, h[1] + (pt[1] - ph[1]) * ext)
                if math.dist(t, h) < .5:
                    continue
            else:
                continue
            self.bones[bid] = {"parent": parent, "head": h, "tail": t, "length": math.dist(h, t),
                               "tail_joint": tail if tail in self.rest else None, "head_joint": head}
            self.order.append(bid)
        self.children = {b: [c for c in self.order if self.bones[c]["parent"] == b] for b in self.order}
        # joints carried by each bone: its tail joint, plus heel/toe on the foot
        self.joint_bone = {}
        for bid in self.order:
            bone = self.bones[bid]
            if bone["tail_joint"]:
                self.joint_bone[bone["tail_joint"]] = bid
        if "pelvis" in self.bones:
            self.joint_bone.setdefault("pelvis", "pelvis")
        for jid in self.rest:  # anything else follows the bone that carries the nearest joint
            if jid not in self.joint_bone:
                for prefix, bone in (("heel_l", "left_foot"), ("heel_r", "right_foot"), ("toe_l", "left_foot"),
                                     ("toe_r", "right_foot"), ("left_hand.", "left_hand"), ("right_hand.", "right_hand")):
                    if jid.startswith(prefix) and bone in self.bones:
                        self.joint_bone[jid] = bone
                        break
                else:
                    near = min(self.order, key=lambda b: _segment_distance(self.rest[jid], self.bones[b]["head"],
                                                                         self.bones[b]["tail"]), default=None)
                    if near:
                        self.joint_bone[jid] = near

    def limit(self, bid, degrees):
        c = self.constraints.get(bid)
        if not c:
            return degrees
        lo, hi = c.get("min"), c.get("max")
        if lo is not None:
            degrees = max(lo, degrees)
        if hi is not None:
            degrees = min(hi, degrees)
        return degrees

    def fk(self, rotations, root=(0.0, 0.0)):
        """World transform of every bone for local rotations {bone: degrees}."""
        world = {}
        for bid in self.order:
            bone = self.bones[bid]
            local = rotation(math.radians(self.limit(bid, rotations.get(bid, 0.0))), bone["head"])
            if bone["parent"] is None:
                world[bid] = compose((1.0, 0.0, 0.0, 1.0, root[0], root[1]), local)
            else:
                world[bid] = compose(world[bone["parent"]], local)
        return world

    def posed_joints(self, world):
        out = {}
        for jid, p in self.rest.items():
            bid = self.joint_bone.get(jid)
            out[jid] = apply(world[bid], p) if bid in world else p
        return out

    def _end(self, world, bid, joint):
        if joint in self.rest:
            return apply(world[self.joint_bone.get(joint, bid)], self.rest[joint])
        return apply(world[bid], self.bones[bid]["tail"])

    def solve(self, pose, contacts=()):
        """Rotations after IK targets and contacts. Returns (rotations, world, report)."""
        rot = {k: float(v) for k, v in (pose.get("rotations") or {}).items() if k in self.bones}
        root = tuple(pose.get("root") or (0.0, 0.0))
        report = {"ik": {}, "contacts": {}}
        goals = [(cid, tuple(t), None) for cid, t in (pose.get("ik") or {}).items() if cid in CHAINS or cid in SINGLE]
        for contact in contacts:
            chain = CONTACT_CHAINS.get(contact["joint"])
            if contact.get("enabled", True) and chain and contact.get("point"):
                goals.append((chain, tuple(contact["point"]), contact))
        for chain_id, target, contact in goals:
            bones, end = (CHAINS.get(chain_id) or SINGLE[chain_id])
            bones = [b for b in bones if b in self.bones]
            if not bones:
                continue
            aim = target
            if contact is not None and contact["joint"] != end and contact["joint"] in self.rest:
                # pin the heel / toe: aim the chain's end so that joint lands on the point
                world = self.fk(rot, root)
                e = self._end(world, bones[-1], end)
                j = self._end(world, bones[-1], contact["joint"])
                aim = (target[0] + e[0] - j[0], target[1] + e[1] - j[1])
            if len(bones) == 2 and all(b in self.bones for b in bones):
                self._two_bone(rot, root, bones, end, aim)
            else:
                self._ccd(rot, root, bones, end, aim)
            world = self.fk(rot, root)
            got = self._end(world, bones[-1], contact["joint"] if contact else end) if (contact and contact["joint"] in self.rest) \
                else self._end(world, bones[-1], end)
            miss = round(math.dist(got, target), 2)
            (report["contacts"] if contact else report["ik"])[contact["id"] if contact else chain_id] = {"miss_px": miss}
        return {b: self.limit(b, v) for b, v in rot.items()}, self.fk(rot, root), report

    def _two_bone(self, rot, root, bones, end, target):
        b1, b2 = bones
        world = self.fk(rot, root)
        a = apply(world[b1], self.bones[b1]["head"])
        b = apply(world[b1], self.bones[b1]["tail"])
        c = self._end(world, b2, end)
        l1 = self.bones[b1]["length"]
        l2 = math.dist(self.bones[b2]["head"], self.rest.get(end, self.bones[b2]["tail"]))
        d = math.dist(a, target)
        d = max(abs(l1 - l2) + 1e-3, min(l1 + l2 - 1e-3, d))
        alpha = math.acos(max(-1.0, min(1.0, (l1 * l1 + d * d - l2 * l2) / (2 * l1 * d))))
        base = _angle((target[0] - a[0], target[1] - a[1]))
        bend = self.bends.get(b1)
        if bend in (1, -1):
            sign = float(bend)
        elif abs(_wrap(_angle((c[0] - b[0], c[1] - b[1])) - _angle((b[0] - a[0], b[1] - a[1])))) > math.radians(8):
            # keep the side the joint already bends to
            cross = (c[0] - a[0]) * (b[1] - a[1]) - (c[1] - a[1]) * (b[0] - a[0])
            sign = 1.0 if cross >= 0 else -1.0
        else:
            # nearly straight: bend away from the body (knees and elbows go outwards)
            centre = apply(world.get("spine", IDENTITY), self.rest["navel"]) if "navel" in self.rest else a
            sign = max((1.0, -1.0), key=lambda k: math.dist(centre, (a[0] + l1 * math.cos(base + k * alpha),
                                                                     a[1] + l1 * math.sin(base + k * alpha))))
        want1 = base + sign * alpha
        rot[b1] = rot.get(b1, 0.0) + math.degrees(_wrap(want1 - _angle((b[0] - a[0], b[1] - a[1]))))
        rot[b1] = self.limit(b1, rot[b1])
        world = self.fk(rot, root)
        b = apply(world[b1], self.bones[b1]["tail"])
        c = self._end(world, b2, end)
        want2 = _angle((target[0] - b[0], target[1] - b[1]))
        rot[b2] = self.limit(b2, rot.get(b2, 0.0) + math.degrees(_wrap(want2 - _angle((c[0] - b[0], c[1] - b[1])))))

    def _ccd(self, rot, root, bones, end, target, rounds=12):
        for _ in range(rounds):
            for bid in reversed(bones):
                world = self.fk(rot, root)
                pivot = apply(world[bid], self.bones[bid]["head"])
                e = self._end(world, bones[-1], end)
                turn = _wrap(_angle((target[0] - pivot[0], target[1] - pivot[1])) - _angle((e[0] - pivot[0], e[1] - pivot[1])))
                rot[bid] = self.limit(bid, rot.get(bid, 0.0) + math.degrees(turn))
            if math.dist(self._end(self.fk(rot, root), bones[-1], end), target) < .5:
                break


# --- Mesh and weights ---------------------------------------------------------------------------

class Mesh:
    """Grid mesh over the figure with per-vertex bone weights (see the module docstring)."""

    def __init__(self, labels, skeleton, bindings=None, deltas=None, cell=None, falloff=None):
        self.skeleton = skeleton
        box = labels.point(lambda v: 255 if v else 0).getbbox()
        self.empty = box is None
        torso = math.dist(skeleton.rest["chest"], skeleton.rest["pelvis"]) if {"chest", "pelvis"} <= skeleton.rest.keys() else 100.0
        height = (box[3] - box[1]) if box else 100
        self.cell = int(cell or max(4, round(height / 110)))
        self.falloff = float(falloff or .08 * torso)
        self.bindings = self._bindings(bindings or {})
        if self.empty:
            self.labels = labels
            self.vertices, self.parts, self.weights = [], {}, []
            return
        g = self.cell
        self.x0, self.y0 = max(0, box[0] - g), max(0, box[1] - g)
        self.nx = (min(labels.width, box[2] + g) - self.x0) // g + 2
        self.ny = (min(labels.height, box[3] + g) - self.y0) // g + 2
        self.labels, cells = self._effective_labels(labels)
        self.vertices = [(self.x0 + i * g, self.y0 + j * g) for j in range(self.ny) for i in range(self.nx)]
        self.cell_parts = cells  # cell index -> {part value: px}
        self._vertex_parts()
        self._weights(deltas or {})
        self._layers()

    # parts → bones
    def _bindings(self, user):
        sk = self.skeleton
        out = {}
        for pid in fd.PARTS:
            wanted = (user.get(pid) or {}).get("bones") or DEFAULT_BINDINGS.get(pid, [])
            bones = [b for b in wanted if b in sk.bones]
            if not bones:  # e.g. fingers without hand landmarks: the nearest existing ancestor
                for b in wanted:
                    while b is not None and b not in sk.bones:
                        b = PARENT.get(b)
                    if b is not None:
                        bones = [b]
                        break
            if not bones and sk.order:
                bones = [sk.order[0]]
            out[fd.PARTS[pid]["value"]] = bones
        return out

    def adjacent(self, p, q):
        if p == q:
            return True
        key = (p, q) if p < q else (q, p)
        cache = self.__dict__.setdefault("_adj", {})
        if key not in cache:
            bp, bq = self.bindings.get(p, []), self.bindings.get(q, [])
            cache[key] = any(a == b or PARENT.get(a) == b or PARENT.get(b) == a for a in bp for b in bq)
        return cache[key]

    def _effective_labels(self, labels):
        """Unknown pixels (254) take their cell's main part, or the nearest cell's."""
        g = self.cell
        unknown = labels.point(lambda v: 255 if v == fd.UNKNOWN else 0)
        cells = {}
        pending = []
        main = {}
        for j in range(self.ny - 1):
            for i in range(self.nx - 1):
                x, y = self.x0 + i * g, self.y0 + j * g
                if x >= labels.width or y >= labels.height:
                    continue
                h = labels.crop((x, y, min(labels.width, x + g), min(labels.height, y + g))).histogram()
                known = {v: n for v, n in enumerate(h) if n and v not in (fd.BACKGROUND, fd.UNKNOWN)}
                c = j * (self.nx - 1) + i
                if known:
                    main[c] = max(known, key=known.get)
                if known or h[fd.UNKNOWN]:
                    cells[c] = dict(known)
                    if h[fd.UNKNOWN]:
                        pending.append((c, h[fd.UNKNOWN]))
        if not pending:
            return labels, cells
        # nearest cell with a main part for the cells of only unknown pixels (grid BFS)
        from collections import deque
        frontier = deque(main)
        seen = dict(main)
        w = self.nx - 1
        while frontier:
            c = frontier.popleft()
            i, j = c % w, c // w
            for ni, nj in ((i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)):
                n = nj * w + ni
                if 0 <= ni < w and 0 <= nj < self.ny - 1 and n not in seen and n in cells:
                    seen[n] = seen[c]
                    frontier.append(n)
        out = labels.copy()
        for c, count in pending:
            value = main.get(c, seen.get(c))
            if value is None:
                continue
            i, j = c % w, c // w
            x, y = self.x0 + i * g, self.y0 + j * g
            box = (x, y, min(labels.width, x + g), min(labels.height, y + g))
            out.paste(value, box, unknown.crop(box))
            cells[c][value] = cells[c].get(value, 0) + count
        return out, cells

    def _vertex_parts(self):
        w = self.nx - 1
        self.vpart = [None] * len(self.vertices)
        for v in range(len(self.vertices)):
            i, j = v % self.nx, v // self.nx
            total = {}
            for ci, cj in ((i - 1, j - 1), (i, j - 1), (i - 1, j), (i, j)):
                if 0 <= ci < w and 0 <= cj < self.ny - 1:
                    for p, n in self.cell_parts.get(cj * w + ci, {}).items():
                        total[p] = total.get(p, 0) + n
            if total:
                self.vpart[v] = max(total, key=total.get)

    def nearest_bone(self, part, p):
        bones = self.bindings.get(part) or self.skeleton.order[:1]
        sk = self.skeleton.bones
        return min(bones, key=lambda b: _segment_distance(p, sk[b]["head"], sk[b]["tail"]))

    def _weights(self, deltas):
        verts = self.vertices
        self.weights = [None] * len(verts)
        active = [v for v in range(len(verts)) if self.vpart[v] is not None]
        for v in active:
            self.weights[v] = {self.nearest_bone(self.vpart[v], verts[v]): 1.0}
        rounds = max(1, round(self.falloff / self.cell))
        nx = self.nx
        neighbours = {}
        for v in active:
            i, j = v % nx, v // nx
            near = [u for u in ((v + 1) if i + 1 < nx else None, (v - 1) if i > 0 else None,
                                v + nx if j + 1 < self.ny else None, v - nx if j > 0 else None)
                    if u is not None and self.vpart[u] is not None and self.adjacent(self.vpart[u], self.vpart[v])]
            neighbours[v] = near
        for _ in range(rounds):
            nxt = {}
            for v in active:
                acc = dict(self.weights[v])
                for u in neighbours[v]:
                    for b, w in self.weights[u].items():
                        acc[b] = acc.get(b, 0.0) + w
                n = 1 + len(neighbours[v])
                nxt[v] = {b: w / n for b, w in acc.items()}
            for v, w in nxt.items():
                self.weights[v] = w
        for v in active:
            w = self.weights[v]
            for bone, image in deltas.items():
                if bone not in self.skeleton.bones:
                    continue
                x, y = verts[v]
                d = image.getpixel((min(image.width - 1, x // DELTA_SCALE), min(image.height - 1, y // DELTA_SCALE)))
                if d != DELTA_ZERO:
                    w[bone] = max(0.0, w.get(bone, 0.0) + (d - DELTA_ZERO) / 127)
            top = sorted(((x, b) for b, x in w.items() if x > 1e-4), reverse=True)[:4]
            total = sum(x for x, _ in top)
            self.weights[v] = {b: x / total for x, b in top} if total > 0 else {self.nearest_bone(self.vpart[v], verts[v]): 1.0}

    def _layers(self):
        """Per part: its cells, and the weights each of their vertices has *for that part* —
        a vertex of a part that only overlaps it (not anatomically adjacent) moves rigidly
        with this part's nearest bone instead."""
        w = self.nx - 1
        self.parts = {}
        for c, present in self.cell_parts.items():
            i, j = c % w, c // w
            corners = (j * self.nx + i, j * self.nx + i + 1, (j + 1) * self.nx + i + 1, (j + 1) * self.nx + i)
            for p in present:
                entry = self.parts.setdefault(p, {"cells": [], "vertex_weights": {}})
                entry["cells"].append((c, corners))
                for v in corners:
                    if v in entry["vertex_weights"]:
                        continue
                    q = self.vpart[v]
                    if q is not None and self.adjacent(p, q):
                        entry["vertex_weights"][v] = self.weights[v]
                    else:
                        entry["vertex_weights"][v] = {self.nearest_bone(p, self.vertices[v]): 1.0}

    def deform(self, world):
        """Posed position of every vertex of every part: {part value: {vertex: (x, y)}}."""
        out = {}
        cache = {}
        for p, entry in self.parts.items():
            posed = {}
            for v, weights in entry["vertex_weights"].items():
                key = (v, id(weights))
                if key not in cache:
                    x = y = 0.0
                    for b, w in weights.items():
                        t = world.get(b, IDENTITY)
                        px, py = apply(t, self.vertices[v])
                        x += w * px
                        y += w * py
                    cache[key] = (x, y)
                posed[v] = cache[key]
            out[p] = posed
        return out


# --- Rendering -------------------------------------------------------------------------------------

def _affine_from(dst, src):
    """Coefficients for Image.transform(AFFINE): output pixel (x, y) in dst triangle → src."""
    (x0, y0), (x1, y1), (x2, y2) = dst
    (u0, v0), (u1, v1), (u2, v2) = src
    det = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
    if abs(det) < 1e-9:
        return None
    a = ((u1 - u0) * (y2 - y0) - (u2 - u0) * (y1 - y0)) / det
    b = ((u2 - u0) * (x1 - x0) - (u1 - u0) * (x2 - x0)) / det
    c = u0 - a * x0 - b * y0
    d = ((v1 - v0) * (y2 - y0) - (v2 - v0) * (y1 - y0)) / det
    e = ((v2 - v0) * (x1 - x0) - (v1 - v0) * (x2 - x0)) / det
    f = v0 - d * x0 - e * y0
    return a, b, c, d, e, f


def _warp_piece(out, image, dst, src, offset):
    """Draws the `src` polygon of `image` mapped affinely onto the `dst` polygon (3 or 4 points,
    the same affine map for all of them) into `out` (shifted by `offset`)."""
    ox, oy = offset
    xs = [p[0] - ox for p in dst]
    ys = [p[1] - oy for p in dst]
    bx0, by0 = math.floor(min(xs)) - 1, math.floor(min(ys)) - 1
    bx1, by1 = math.ceil(max(xs)) + 1, math.ceil(max(ys)) + 1
    bx0c, by0c, bx1c, by1c = max(0, bx0), max(0, by0), min(out.width, bx1), min(out.height, by1)
    if bx1c <= bx0c or by1c <= by0c:
        return
    coeffs = _affine_from([(x - ox, y - oy) for x, y in dst[:3]], src[:3])
    if coeffs is None:
        return
    a, b, c, d, e, f = coeffs
    w, h = bx1c - bx0c, by1c - by0c
    # `image` is premultiplied (RGBa): Pillow would otherwise convert the whole picture per call
    tile = image.transform((w, h), Image.AFFINE, (a, b, c + a * bx0c + b * by0c, d, e, f + d * bx0c + e * by0c),
                           Image.BILINEAR).convert("RGBA")
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).polygon([(x - bx0c, y - by0c) for x, y in zip(xs, ys)], fill=255, outline=255)
    tile.putalpha(ImageChops.multiply(tile.getchannel("A"), mask))
    out.alpha_composite(tile, (bx0c, by0c))


class Rig:
    """A rigged figure: skeleton + mesh + layers from one instance of a figure document."""

    def __init__(self, source, labels, joints, rig, deltas=None):
        self.source = source
        self.data = rig
        self.skeleton = Skeleton(joints, rig.get("constraints"))
        settings = rig.get("settings") or {}
        self.mesh = Mesh(labels, self.skeleton, rig.get("bindings"), deltas, settings.get("cell"), settings.get("falloff"))
        self._part_images = {}

    def solve(self, pose=None, time=None):
        """Rotations, bone transforms and the IK / contact report of a pose; contacts with an
        interval hold only inside it (at `time` on the timeline)."""
        contacts = [c for c in self.data.get("contacts") or [] if c.get("enabled", True)
                    and (c.get("interval") is None or time is None or c["interval"][0] <= time <= c["interval"][1])]
        return self.skeleton.solve(pose if pose is not None else self.data.get("pose") or {}, contacts)

    def layer_order(self):
        order = [fd.PARTS[p]["value"] for p in (self.data.get("layers") or []) if p in fd.PARTS]
        rest = [v for v in self.mesh.parts if v not in order]
        return [v for v in order if v in self.mesh.parts] + rest

    def part_image(self, value):
        if value not in self._part_images:
            image = self.source.copy()
            mask = self.mesh.labels.point(lambda v, value=value: 255 if v == value else 0)
            image.putalpha(ImageChops.multiply(image.getchannel("A"), mask))
            self._part_images[value] = image.convert("RGBa")
        return self._part_images[value]

    def render(self, world, size=None, offset=(0, 0), surfaces=()):
        """The posed figure on a transparent canvas of `size` (default: the source size) whose
        top-left is source point `offset`. Parts back to front; a part's cells moved rigidly
        by one bone go in one piece, the rest triangle by triangle."""
        size = size or self.source.size
        out = Image.new("RGBA", size, (0, 0, 0, 0))
        if self.mesh.empty:
            return out
        posed = self.mesh.deform(world)
        verts = self.mesh.vertices
        by_part_surfaces = {}
        for surface in surfaces:
            by_part_surfaces.setdefault(surface["part_value"], []).append(surface)
        for value in self.layer_order():
            for surface in by_part_surfaces.get(value, []):
                if not surface.get("above"):
                    self._render_surface(out, surface, world, offset)
            self._render_part(out, value, posed[value], world, offset)
            for surface in by_part_surfaces.get(value, []):
                if surface.get("above"):
                    self._render_surface(out, surface, world, offset)
        return out

    def _render_part(self, out, value, posed, world, offset):
        entry = self.mesh.parts[value]
        image = self.part_image(value)
        verts = self.mesh.vertices
        weights = entry["vertex_weights"]
        rigid = {}
        loose = []
        for c, corners in entry["cells"]:
            ws = [weights[v] for v in corners]
            if all(len(w) == 1 for w in ws):
                bones = {next(iter(w)) for w in ws}
                if len(bones) == 1:
                    rigid.setdefault(bones.pop(), []).append(corners)
                    continue
            loose.append(corners)
        ox, oy = offset
        for bone, cells in rigid.items():
            t = world.get(bone, IDENTITY)
            xs = [verts[v][0] for cs in cells for v in cs]
            ys = [verts[v][1] for cs in cells for v in cs]
            box = (min(xs), min(ys), max(xs), max(ys))
            mask = Image.new("L", (box[2] - box[0], box[3] - box[1]), 0)
            draw = ImageDraw.Draw(mask)
            for cs in cells:
                x, y = verts[cs[0]]
                draw.rectangle((x - box[0], y - box[1], x - box[0] + self.mesh.cell - 1, y - box[1] + self.mesh.cell - 1), fill=255)
            piece = image.crop(box).convert("RGBA")
            piece.putalpha(ImageChops.multiply(piece.getchannel("A"), mask))
            piece = piece.convert("RGBa")
            # the inverse of the bone's map, for Image.transform
            a, b, c, d, e, f = t
            det = a * d - b * c
            ia, ib, ic, id_ = d / det, -b / det, -c / det, a / det
            corners = [apply(t, p) for p in ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3]))]
            xs2 = [p[0] - ox for p in corners]
            ys2 = [p[1] - oy for p in corners]
            bx0, by0 = max(0, math.floor(min(xs2)) - 1), max(0, math.floor(min(ys2)) - 1)
            bx1, by1 = min(out.width, math.ceil(max(xs2)) + 1), min(out.height, math.ceil(max(ys2)) + 1)
            if bx1 <= bx0 or by1 <= by0:
                continue
            # output (x, y) → source: inverse(t)(x + bx0 + ox, y + by0 + oy) − box origin
            sx = (bx0 + ox) - e
            sy = (by0 + oy) - f
            coeffs = (ia, ib, ia * sx + ib * sy - box[0], ic, id_, ic * sx + id_ * sy - box[1])
            tile = piece.transform((bx1 - bx0, by1 - by0), Image.AFFINE, coeffs, Image.BILINEAR).convert("RGBA")
            out.alpha_composite(tile, (bx0, by0))
        for corners in loose:
            src = [verts[v] for v in corners]
            dst = [posed[v] for v in corners]
            for tri in ((0, 1, 2), (0, 2, 3)):
                _warp_piece(out, image, [dst[k] for k in tri], [src[k] for k in tri], offset)

    def _render_surface(self, out, surface, world, offset):
        """A drawn surface follows its bone rigidly (it is not observed material)."""
        t = world.get(surface["bone"], IDENTITY)
        image = surface["image"]
        box = image.getbbox()
        image = image.convert("RGBa")
        if not box:
            return
        corners = [(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])]
        _warp_piece(out, image, [apply(t, p) for p in corners], corners, offset)


def draw_skeleton(image, skeleton, world, offset=(0, 0), scale=1.0, highlight=None):
    draw = ImageDraw.Draw(image)
    ox, oy = offset
    joints = skeleton.posed_joints(world)

    def screen(p):
        return ((p[0] - ox) * scale, (p[1] - oy) * scale)
    for bid in skeleton.order:
        bone = skeleton.bones[bid]
        a = screen(apply(world[bid], bone["head"]))
        b = screen(apply(world[bid], bone["tail"]))
        fine = "_hand." in bid
        draw.line([a, b], fill=(255, 200, 0) if bid == highlight else (25, 25, 25) if not fine else (245, 245, 245),
                  width=2 if fine else 4)
    for jid, p in joints.items():
        x, y = screen(p)
        r = 2.5 if "_hand." in jid else 4.5
        draw.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255), outline=(20, 20, 20))
    return image
