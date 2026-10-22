"""Bone-driven transitions between the poses of a dance phrase (docs/DANCE.md § 5).

In-between skeleton: forward kinematics from the pelvis — every bone turns from its angle in
pose A to its angle in pose B (the short way) on its own timing (torso leads, limbs follow), bone
lengths blend. Both drawings are skinned to that skeleton (each pixel follows its three nearest
bones with smooth weights), warped by an inverse mesh, and cross-faded where they still differ.

The "groove" style adds what makes it read as dancing: a bounce on every beat, a wind-up before
the move and an overshoot after it, and motion blur on fast frames.
"""
from __future__ import annotations

import math

from PIL import Image

TREE = (("navel", "pelvis"), ("chest", "navel"), ("head", "chest"),
        ("shoulder_l", "chest"), ("shoulder_r", "chest"), ("elbow_l", "shoulder_l"), ("elbow_r", "shoulder_r"),
        ("wrist_l", "elbow_l"), ("wrist_r", "elbow_r"),
        ("hip_l", "pelvis"), ("hip_r", "pelvis"), ("knee_l", "hip_l"), ("knee_r", "hip_r"),
        ("foot_l", "knee_l"), ("foot_r", "knee_r"))
DELAY = {"navel": 0, "chest": .02, "head": .1, "shoulder_l": .05, "shoulder_r": .05, "elbow_l": .12,
         "elbow_r": .12, "wrist_l": .2, "wrist_r": .2, "hip_l": 0, "hip_r": 0, "knee_l": .04, "knee_r": .04,
         "foot_l": .08, "foot_r": .08}  # share of the move a bone waits before it starts
EXTEND = {"head": .7, "wrist_l": .35, "wrist_r": .35, "foot_l": .2, "foot_r": .2}  # skull, hands, feet
GRID = 10  # mesh cell, px
# Progress curves: keys are turning points, smoothstep between them.
CURVES = {"soft": ((0, 0), (1, 1)),
          "hit": ((0, 0), (.16, -.07), (.55, 1.09), (.74, .97), (.88, 1.01), (1, 1)),  # wind-up, snap, overshoot
          "fast": ((0, 0), (.12, -.04), (.6, 1.06), (.82, .985), (1, 1))}
GROOVE = .02  # bounce depth, share of the figure's height
SWAY = .012   # side sway over two beats
SMEAR_PX = 9  # joint travel per frame above which a frame gets motion blur


def smooth(a, b, x):
    t = min(1.0, max(0.0, (x - a) / (b - a)))
    return t * t * (3 - 2 * t)


def curve(name):
    keys = CURVES[name]

    def at(x):
        x = min(1.0, max(0.0, x))
        for (x0, y0), (x1, y1) in zip(keys, keys[1:]):
            if x <= x1:
                return y0 + (y1 - y0) * smooth(x0, x1, x)
        return keys[-1][1]
    return at


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def complete(joints, other):
    """Fill a joint missing in one pose from the other pose's bone, relative to its parent."""
    out = dict(joints)
    for child, parent in TREE:
        if child not in out and parent in out and child in other and parent in other:
            out[child] = (out[parent][0] + other[child][0] - other[parent][0],
                          out[parent][1] + other[child][1] - other[parent][1])
    return out


def in_between(ja, jb, u, ease):
    """Skeleton at progress u (0..1): the pelvis moves, every bone rotates on its own timing."""
    pa, pb = ja["pelvis"], jb["pelvis"]
    k = ease(u)
    out = {"pelvis": (pa[0] + (pb[0] - pa[0]) * k, pa[1] + (pb[1] - pa[1]) * k)}
    for child, parent in TREE:
        if child not in ja or child not in jb or parent not in out:
            continue
        va = (ja[child][0] - ja[parent][0], ja[child][1] - ja[parent][1])
        vb = (jb[child][0] - jb[parent][0], jb[child][1] - jb[parent][1])
        t = ease((u - DELAY[child]) / (1 - DELAY[child]))
        angle = math.atan2(va[1], va[0]) + wrap(math.atan2(vb[1], vb[0]) - math.atan2(va[1], va[0])) * t
        length = math.hypot(*va) + (math.hypot(*vb) - math.hypot(*va)) * t
        out[child] = (out[parent][0] + math.cos(angle) * length, out[parent][1] + math.sin(angle) * length)
    return out


def groove(joints, time, beat, height):
    """Bounce down on every beat (the head a little later), knees open, the chest sways over two."""
    def dip(delay):
        phase = (time / beat - delay) % 1
        return GROOVE * height * ((1 + math.cos(2 * math.pi * phase)) / 2) ** 2
    body, head = dip(0), dip(.07) * 1.2
    sway = SWAY * height * math.sin(math.pi * time / beat)
    pelvis_x = joints["pelvis"][0]
    out = {}
    for key, (x, y) in joints.items():
        if key.startswith("foot"):
            out[key] = (x, y)
        elif key.startswith("knee"):
            out[key] = (x + math.copysign(.35 * body, x - pelvis_x), y + .5 * body)
        elif key in ("pelvis", "hip_l", "hip_r"):
            out[key] = (x, y + body)
        elif key == "navel":
            out[key] = (x + .5 * sway, y + body)
        elif key == "head":
            out[key] = (x + 1.2 * sway, y + head)
        else:  # chest, shoulders, arms
            out[key] = (x + sway, y + body)
    return out


class Skin:
    """Linear blend skinning of one drawing: pixels follow their three nearest bones."""

    def __init__(self, joints, torso):
        self.bones = []
        for child, parent in TREE:
            if child in joints and parent in joints:
                a, b = joints[parent], joints[child]
                ext = EXTEND.get(child, 0)
                self.bones.append((child, (a[0], a[1]), (b[0] + (b[0] - a[0]) * ext, b[1] + (b[1] - a[1]) * ext)))
        self.soft = .08 * torso
        self.cache = {}

    def weights(self, x, y):
        key = (round(x), round(y))
        if key in self.cache:
            return self.cache[key]
        out = []
        for child, a, b in self.bones:
            dx, dy = b[0] - a[0], b[1] - a[1]
            t = max(0.0, min(1.0, ((x - a[0]) * dx + (y - a[1]) * dy) / (dx * dx + dy * dy or 1.0)))
            out.append((1.0 / (math.hypot(x - a[0] - t * dx, y - a[1] - t * dy) + self.soft) ** 4, child))
        out = sorted(out, reverse=True)[:3]
        total = sum(w for w, _ in out)
        self.cache[key] = result = [(w / total, child) for w, child in out]
        return result


def transforms(rest, pose):
    """Per bone: (cos·scale, sin·scale, rest start, posed start)."""
    out = {}
    for child, parent in TREE:
        if child in rest and parent in rest and child in pose and parent in pose:
            ra = (rest[child][0] - rest[parent][0], rest[child][1] - rest[parent][1])
            pa = (pose[child][0] - pose[parent][0], pose[child][1] - pose[parent][1])
            rotation = wrap(math.atan2(pa[1], pa[0]) - math.atan2(ra[1], ra[0]))
            scale = max(.7, min(1.4, (math.hypot(*pa) or 1) / (math.hypot(*ra) or 1)))
            out[child] = (math.cos(rotation) * scale, math.sin(rotation) * scale, rest[parent], pose[parent])
    return out


def _segment_distance(x, y, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = max(0.0, min(1.0, ((x - a[0]) * dx + (y - a[1]) * dy) / (dx * dx + dy * dy or 1.0)))
    return math.hypot(x - a[0] - t * dx, y - a[1] - t * dy)


class Warp:
    """The skinning map F (rest pixel → posed pixel) and its inverse."""

    def __init__(self, skin, rest, pose):
        self.skin, self.moves = skin, transforms(rest, pose)
        self.posed = []  # posed bones (with skull/hand/foot extension) to start the inverse search
        for child, parent in TREE:
            if child in self.moves:
                a, b = pose[parent], pose[child]
                ext = EXTEND.get(child, 0)
                self.posed.append((child, a, (b[0] + (b[0] - a[0]) * ext, b[1] + (b[1] - a[1]) * ext)))

    def forward(self, x, y):
        nx = ny = 0.0
        for w, child in self.skin.weights(x, y):
            if child not in self.moves:
                nx, ny = nx + w * x, ny + w * y
                continue
            c, s, r0, p0 = self.moves[child]
            dx, dy = x - r0[0], y - r0[1]
            nx += w * (p0[0] + c * dx - s * dy)
            ny += w * (p0[1] + s * dx + c * dy)
        return nx, ny

    def _newton(self, px, py, x, y, steps):
        for _ in range(steps):
            fx, fy = self.forward(px, py)
            ex, ey = x - fx, y - fy
            if ex * ex + ey * ey < .01:
                break
            ax, ay = self.forward(px + 1, py)
            bx, by = self.forward(px, py + 1)
            j11, j21, j12, j22 = ax - fx, ay - fy, bx - fx, by - fy  # Jacobian, 1 px differences
            det = j11 * j22 - j12 * j21
            if abs(det) < 1e-3:
                px, py = px + .5 * ex, py + .5 * ey
                continue
            sx, sy = (j22 * ex - j12 * ey) / det, (j11 * ey - j21 * ex) / det
            length = math.hypot(sx, sy)
            if length > 40:  # keep steps local: the map folds where bones overlap
                sx, sy = sx * 40 / length, sy * 40 / length
            px, py = px + sx, py + sy
        fx, fy = self.forward(px, py)
        return px, py, math.hypot(x - fx, y - fy)

    def inverse(self, x, y):
        """Rest point p with F(p) = (x, y). Newton's method started from the inverse of the
        nearest posed bones' own motion — exact for large rotations, where a plain fixed-point
        iteration diverges. Nearest bones are tried first; the rest only if those fail.
        Returns (px, py, residual px)."""
        starts = []
        for child, a, b in sorted(self.posed, key=lambda bone: _segment_distance(x, y, bone[1], bone[2])):
            c, s, r0, p0 = self.moves[child]
            dx, dy = x - p0[0], y - p0[1]
            k = c * c + s * s
            starts.append((r0[0] + (c * dx + s * dy) / k, r0[1] + (c * dy - s * dx) / k))
        starts.append((x, y))
        best = None
        for sx, sy in starts:
            found = self._newton(sx, sy, x, y, 8)
            if best is None or found[2] < best[2]:
                best = found
            if best[2] < .5:
                break
        return best


def deform(canvas, box, skin, rest, pose):
    """`canvas` warped so its skeleton `rest` moves to `pose` (only inside `box`): each mesh
    vertex q of the output samples the source point p with F(p) = q."""
    warp = Warp(skin, rest, pose)
    x0, y0, x1, y1 = box
    xs = list(range(x0, x1, GRID)) + [x1]
    ys = list(range(y0, y1, GRID)) + [y1]
    moved = {}
    for x in xs:
        for y in ys:
            px, py, _ = warp.inverse(x, y)
            moved[(x, y)] = (px - x0, py - y0)
    mesh = [((l - x0, t - y0, r - x0, b - y0), [*moved[(l, t)], *moved[(l, b)], *moved[(r, b)], *moved[(r, t)]])
            for l, r in zip(xs, xs[1:]) for t, b in zip(ys, ys[1:])]
    out = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    out.paste(canvas.crop(box).transform((x1 - x0, y1 - y0), Image.MESH, mesh, Image.BICUBIC), (x0, y0))
    return out


class Transition:
    """Pose A → B on one canvas: skeletons {joint: (x, y)} in canvas pixels."""

    def __init__(self, canvas_a, joints_a, canvas_b, joints_b, pad=60):
        self.ja, self.jb = complete(joints_a, joints_b), complete(joints_b, joints_a)
        torso = math.hypot(self.ja["chest"][0] - self.ja["pelvis"][0], self.ja["chest"][1] - self.ja["pelvis"][1])
        self.skin_a, self.skin_b = Skin(self.ja, torso), Skin(self.jb, torso)
        self.ca, self.cb = canvas_a, canvas_b
        w, h = canvas_a.size
        ba, bb = canvas_a.getbbox() or (0, 0, w, h), canvas_b.getbbox() or (0, 0, w, h)
        self.box = (max(0, min(ba[0], bb[0]) - pad), max(0, min(ba[1], bb[1]) - pad),
                    min(w, max(ba[2], bb[2]) + pad), min(h, max(ba[3], bb[3]) + pad))
        ys = [y for _, y in self.ja.values()]
        self.height = max(ys) - min(ys)

    def skeleton(self, u, ease):
        return in_between(self.ja, self.jb, u, ease) if u < 1 else dict(self.jb)

    def layer(self, target, u, ease):
        """The drawing for skeleton `target` at progress u."""
        if u >= 1:
            return deform(self.cb, self.box, self.skin_b, self.jb, target)
        mix = smooth(.2, .8, min(1.0, max(0.0, ease(u))))
        if mix <= 0:
            return deform(self.ca, self.box, self.skin_a, self.ja, target)
        return Image.blend(deform(self.ca, self.box, self.skin_a, self.ja, target),
                           deform(self.cb, self.box, self.skin_b, self.jb, target), mix)


def dance_frames(canvases, joints, loop, render, bpm=120, fps=50, style="soft", rhythm=None):
    """Frames of a phrase, one pose per count. `render(layer)` turns a figure layer into an
    RGB frame (podium, shadow, background).

    style "soft": smooth ease-in-out, 78 % of each count moving, no bounce.
    style "groove": bounce, wind-up and overshoot, motion blur; `rhythm` is a list of
    (counts, share moving, curve name) per transition, by default a hit on every count."""
    beat = 60 / bpm
    order = list(range(len(canvases))) + ([0] if loop else [])
    pairs = list(zip(order, order[1:]))
    if rhythm is None:
        rhythm = [(1, .78, "soft") if style == "soft" else (1, .6, "hit")] * len(pairs)
    if len(rhythm) < len(pairs) or any(counts <= 0 or not 0 < share <= 1 for counts, share, _ in rhythm):
        raise ValueError("rhythm: one (counts > 0, 0 < share ≤ 1, curve) per transition")
    frames, clock = [], 0.0
    for (a, b), (counts, share, kind) in zip(pairs, rhythm):
        step = Transition(canvases[a], joints[a], canvases[b], joints[b])
        ease = curve(kind)
        duration, move = counts * beat, counts * beat * share

        def target(t):
            mid = step.skeleton(t / move, ease)
            return groove(mid, clock + t, beat, step.height) if style == "groove" else mid
        for f in range(round(duration * fps)):
            t = f / fps
            frame = render(step.layer(target(t), t / move, ease))
            if style == "groove":
                now, before = target(t), target(max(0.0, t - 1 / fps))
                travel = max((math.hypot(now[k][0] - before[k][0], now[k][1] - before[k][1])
                              for k in ("wrist_l", "wrist_r", "elbow_l", "elbow_r", "head", "foot_l", "foot_r")
                              if k in now and k in before), default=0.0)
                samples = 1 if travel < SMEAR_PX else min(5, 2 + int(travel // SMEAR_PX))
                for i in range(1, samples):  # motion blur: a short shutter behind the moment
                    back = t - .6 / fps * i / (samples - 1)
                    frame = Image.blend(frame, render(step.layer(target(back), back / move, ease)), 1 / (i + 1))
            frames.append(frame)
        clock += duration
    return frames
