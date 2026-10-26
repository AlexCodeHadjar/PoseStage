"""Body / hand analysis of one figure (docs/PARTS.md) — runs in the separate analysis runtime
(MediaPipe, numpy, scipy), never inside the main program:

    python parts_analyzer.py <request.json> <out folder>

Request: {"image": path, "roi": [x1, y1, x2, y2], "mode": "full" | "masks",
          "joints": {...} (mode "masks": the document's joints), "hint": {...} (optional body joints)}
Out: result.json (joints, parts, analysis info), labels.png, uncertain.png. Progress is printed as
JSON lines {"progress": 0…1, "stage": …}; the program may be killed at any time (cancel).

The pipeline, chosen by the experiment in docs/PARTS.md § 3:
1. foreground: the picture's alpha (drawings); for opaque pictures MediaPipe's person mask;
2. body: MediaPipe Holistic (pose landmark model, heavy) over a few input variants (padding,
   background tone, scale, mirror) — drawings are not photos, the first try often finds nothing;
3. hands: Holistic's 21 landmarks per hand (cropped from the pose, no palm detector — the palm
   detector found no hand on the drawings); each hand is taken from the variant whose wrist agrees
   with the body's, checked for collapsed fingers and duplicates;
4. part masks: an *estimate* — every figure pixel goes to the nearest bone (distance scaled by
   the part's width), then each region enclosed by the drawing's outlines that is mostly one part
   becomes that part, so borders follow the drawn lines; fingers only where the hand's landmarks
   passed the checks, otherwise the hand stays "unsplit".
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import sys
import time

import os

os.environ.setdefault("GLOG_minloglevel", "2")  # MediaPipe's C++ log lines are not progress
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
import numpy as np
from PIL import Image, ImageOps
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).resolve().parent))
import figure_doc as fd  # noqa: E402

VARIANTS = [(.25, 232, 1.0), (.25, 255, 1.0), (.45, 200, 1.0), (.25, 232, 1.6), (.6, 160, 1.0)]  # pad share, bg, scale
MODEL = {"name": "mediapipe.holistic", "version": None, "model_complexity": 2, "static_image_mode": True}
POSE_IDS = {"shoulder_l": 11, "shoulder_r": 12, "elbow_l": 13, "elbow_r": 14, "wrist_l": 15, "wrist_r": 16,
            "hip_l": 23, "hip_r": 24, "knee_l": 25, "knee_r": 26, "foot_l": 27, "foot_r": 28,
            "heel_l": 29, "heel_r": 30, "toe_l": 31, "toe_r": 32}
MIRROR_PAIRS = [(1, 4), (2, 5), (3, 6), (7, 8), (9, 10), (11, 12), (13, 14), (15, 16), (17, 18), (19, 20), (21, 22),
                (23, 24), (25, 26), (27, 28), (29, 30), (31, 32)]
FINGER_CHAINS = {f: [f"{f}.{k}" for k in ks] for f, ks in (("thumb", ("cmc", "mcp", "ip", "tip")),
                                                           ("index", ("mcp", "pip", "dip", "tip")),
                                                           ("middle", ("mcp", "pip", "dip", "tip")),
                                                           ("ring", ("mcp", "pip", "dip", "tip")),
                                                           ("pinky", ("mcp", "pip", "dip", "tip")))}
LINE_TONE = 110  # darker than this is a drawn outline
SMALL_HAND_PX = 40  # a hand smaller than this (farthest landmark from the wrist) is too small to split into fingers
ISSUE_NAMES = {"fingers_collapsed": "пальцы сложились в линию", "knuckles_out_of_order": "костяшки не по порядку",
               "hand_size_implausible": "размер кисти не подходит к предплечью",
               "hand landmarks marked uncertain": "точки отмечены как сомнительные"}


def progress(value, stage):
    print(json.dumps({"progress": round(value, 3), "stage": stage}), flush=True)


# --- Landmarks ----------------------------------------------------------------------------------

def run_variant(model, image, roi, pad, bg, scale, mirrored):
    """Holistic on the ROI placed on a square canvas; landmarks back in source px."""
    x1, y1, x2, y2 = roi
    side = max(x2 - x1, y2 - y1) * (1 + 2 * pad)
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    ox, oy = cx - side / 2, cy - side / 2
    target = max(256, min(1600, round(side * scale)))
    k = target / side
    canvas = Image.new("RGBA", (target, target), (bg, bg, bg, 255))
    piece = image.crop((x1, y1, x2, y2))
    piece = piece.resize((max(1, round(piece.width * k)), max(1, round(piece.height * k))), Image.LANCZOS)
    canvas.alpha_composite(piece, (round((x1 - ox) * k), round((y1 - oy) * k)))
    rgb = canvas.convert("RGB")
    if mirrored:
        rgb = ImageOps.mirror(rgb)
    found = model.process(np.asarray(rgb))

    def back(landmarks, visibility=False):
        if landmarks is None:
            return None
        return [(((1 - q.x) if mirrored else q.x) * target / k + ox, q.y * target / k + oy,
                 getattr(q, "visibility", None) if visibility else None) for q in landmarks.landmark]
    pose = back(found.pose_landmarks, True)
    hands = [h for h in (back(found.left_hand_landmarks), back(found.right_hand_landmarks)) if h]
    if pose and mirrored:
        for a, b in MIRROR_PAIRS:
            pose[a], pose[b] = pose[b], pose[a]
    return pose, hands


def hand_check(points, forearm):
    """Diagnostics of 21 hand landmarks. Thresholds from the tune images (docs/PARTS.md § 4):
    fingers lying on one line (finger hull < 0.06 of size², knuckle spread < 0.12 of size) and a
    hand of impossible size for its forearm (size < 0.25 or > 1.6 of the forearm). Size = the
    farthest landmark from the wrist (a fist is short from wrist to middle tip)."""
    pts = np.array([p[:2] for p in points])
    size = float(np.linalg.norm(pts - pts[0], axis=1).max()) or 1.0
    issues = []
    try:
        from scipy.spatial import ConvexHull
        finger_area = ConvexHull(pts[5:]).volume
    except Exception:  # noqa: BLE001 — degenerate points
        finger_area = 0.0
    knuckles = float(np.linalg.norm(pts[17] - pts[5]))
    if size < .25 * forearm or size > 1.6 * forearm or knuckles > 1.0 * size:
        issues.append("hand_size_implausible")
    elif finger_area < .06 * size * size or knuckles < .12 * size:
        issues.append("fingers_collapsed")
    # knuckles index → pinky should run one way along the knuckle line
    line = pts[17] - pts[5]
    order = [float(np.dot(pts[i] - pts[5], line)) for i in (5, 9, 13, 17)]
    if not all(a < b for a, b in zip(order, order[1:])):
        issues.append("knuckles_out_of_order")
    return issues, size


def body_joints(pose, image_alpha=None):
    """33 pose landmarks → the document's body joints (sides are the figure's own)."""
    P = {k: pose[i] for k, i in POSE_IDS.items()}
    mid = lambda a, b: ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    lerp = lambda a, b, t: (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
    head = tuple(np.mean([pose[0][:2], pose[7][:2], pose[8][:2]], axis=0))
    shoulders = mid(P["shoulder_l"], P["shoulder_r"])
    pelvis = mid(P["hip_l"], P["hip_r"])
    chest = lerp(shoulders, pelvis, .15)
    out = {"head": head, "neck": lerp(shoulders, head, .35), "chest": chest, "navel": lerp(chest, pelvis, .55),
           "pelvis": pelvis}
    out.update({k: v[:2] for k, v in P.items()})
    joints = {}
    for jid, xy in out.items():
        score = P[jid][2] if jid in P else None
        on = True
        if image_alpha is not None:
            x, y = int(round(xy[0])), int(round(xy[1]))
            on = 0 <= x < image_alpha.shape[1] and 0 <= y < image_alpha.shape[0] and image_alpha[y, x]
        state = "visible" if (score is None or score >= .5) and on else "uncertain"
        joints[jid] = {"xy": [round(float(xy[0]), 2), round(float(xy[1]), 2)], "state": state,
                       "score": None if score is None else round(float(score), 4),
                       "score_kind": None if score is None else "mediapipe pose visibility",
                       "source": {"kind": "model", "method": "holistic pose", "model": MODEL["name"]}}
    return joints


def view_of(J):
    torso = math.dist(J["chest"]["xy"], J["pelvis"]["xy"]) or 1
    shoulders = math.dist(J["shoulder_l"]["xy"], J["shoulder_r"]["xy"])
    if shoulders < .25 * torso:
        return "side"
    if shoulders < .55 * torso:
        return "three_quarter"
    return "front" if J["shoulder_l"]["xy"][0] > J["shoulder_r"]["xy"][0] else "back"


class ModelMissing(RuntimeError):
    pass


def offline_mediapipe():
    """MediaPipe fetches a missing model file from the internet by itself; here it may not:
    the model comes only with tools/setup_analyzer.py."""
    import mediapipe as mp
    from mediapipe.python.solutions import download_utils

    def refuse(model_path):
        target = Path(mp.__file__).parent.parent / model_path
        if not target.is_file():
            raise ModelMissing(f"нет файла модели {target.name}: установите его через tools/setup_analyzer.py")
    download_utils.download_oss_model = refuse
    return mp


def landmarks(image, roi, alpha_np):
    mp = offline_mediapipe()
    MODEL["version"] = mp.__version__
    model = mp.solutions.holistic.Holistic(static_image_mode=True, model_complexity=2)
    tried = []
    for n, (pad, bg, scale) in enumerate(VARIANTS):
        for mirrored in (False, True):
            pose, hands = run_variant(model, image, roi, pad, bg, scale, mirrored)
            tried.append({"variant": [pad, bg, scale, mirrored], "pose": pose, "hands": hands})
            progress(.05 + .55 * (len(tried) / (2 * len(VARIANTS))), "landmarks")
    with_pose = [t for t in tried if t["pose"]]
    if not with_pose:
        return None, {}, {"variants": len(tried), "variants_with_pose": 0}
    # The body from the variant whose landmarks agree with most others (median-closest).
    def key(t):
        return np.array([t["pose"][i][:2] for i in (11, 12, 23, 24, 15, 16, 27, 28)])
    stack = np.stack([key(t) for t in with_pose])
    median = np.median(stack, axis=0)
    best = min(with_pose, key=lambda t: float(np.linalg.norm(key(t) - median, axis=1).mean()))
    J = body_joints(best["pose"], alpha_np)
    # Hands: every candidate from every variant, given to the nearer body wrist.
    forearm = {s: math.dist(J[f"elbow_{s}"]["xy"], J[f"wrist_{s}"]["xy"]) or 30 for s in "lr"}
    candidates = {"l": [], "r": []}
    for t in tried:
        for pts in t["hands"]:
            d = {s: math.dist(pts[0][:2], J[f"wrist_{s}"]["xy"]) for s in "lr"}
            s = min(d, key=d.get)
            if d[s] <= .7 * forearm[s]:
                issues, length = hand_check(pts, forearm[s])
                candidates[s].append({"points": pts, "issues": issues, "length": length, "dist": d[s], "variant": t["variant"]})
    hands, rejected = {}, {}
    for s, cands in candidates.items():
        # a hand of impossible size is not this figure's hand at all: not kept even as a guess
        rejected[s] = sum("hand_size_implausible" in c["issues"] for c in cands)
        cands = [c for c in cands if "hand_size_implausible" not in c["issues"]]
        if not cands:
            continue
        # prefer clean ones, then the most typical (closest to the candidates' median)
        clean = [c for c in cands if not c["issues"]] or cands
        med = np.median(np.stack([np.array([p[:2] for p in c["points"]]) for c in clean]), axis=0)
        pick = min(clean, key=lambda c: float(np.linalg.norm(np.array([p[:2] for p in c["points"]]) - med, axis=1).mean()))
        pick["votes"] = len(cands)
        hands[s] = pick
    if "l" in hands and "r" in hands:  # one hand found twice
        a = np.array([p[:2] for p in hands["l"]["points"]])
        b = np.array([p[:2] for p in hands["r"]["points"]])
        if float(np.linalg.norm(a - b, axis=1).mean()) < .3 * max(hands["l"]["length"], hands["r"]["length"]):
            drop = "l" if hands["l"]["dist"] > hands["r"]["dist"] else "r"
            hands.pop(drop)
    for s, side in (("l", "left"), ("r", "right")):
        hand = hands.get(s)
        if not hand:
            continue
        state = "uncertain" if hand["issues"] else "visible"
        for name, (x, y, _) in zip(fd.HAND_POINTS, hand["points"]):
            J[f"{side}_hand.{name}"] = {"xy": [round(float(x), 2), round(float(y), 2)], "state": state, "score": None,
                                        "score_kind": None,
                                        "source": {"kind": "model", "method": "holistic hand", "model": MODEL["name"]},
                                        "diagnostics": hand["issues"]}
    info = {"variants": len(tried), "variants_with_pose": len(with_pose), "body_variant": best["variant"],
            "hands": {("left" if s == "l" else "right"): {"votes": h["votes"], "issues": h["issues"], "variant": h["variant"],
                                                          "size_px": round(h["length"], 1)} for s, h in hands.items()},
            "hand_candidates_rejected": {("left" if s == "l" else "right"): n for s, n in rejected.items() if n},
            "view": view_of(J)}
    return J, hands, info


# --- Masks ----------------------------------------------------------------------------------------

def _segment_distance(xx, yy, a, b):
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    length2 = dx * dx + dy * dy or 1.0
    t = np.clip(((xx - ax) * dx + (yy - ay) * dy) / length2, 0, 1)
    return np.hypot(xx - ax - t * dx, yy - ay - t * dy)


def _polygon_mask(shape, points, box):
    from PIL import ImageDraw
    x0, y0 = box[0], box[1]
    image = Image.new("L", (shape[1], shape[0]), 0)
    if len(points) >= 3:
        ImageDraw.Draw(image).polygon([(x - x0, y - y0) for x, y in points], fill=1)
    return np.asarray(image, dtype=bool)


def build_masks(image, fg, J, roi, clean_hands=()):
    """Label map and 'uncertain' map (full picture size) from joints and the silhouette.
    clean_hands: sides ("left"/"right") whose 21 landmarks passed the checks: their hull is the
    hand even in front of the torso or the head."""
    H, W = fg.shape
    labels = np.zeros((H, W), np.uint8)
    unsure = np.zeros((H, W), np.uint8)
    x1, y1, x2, y2 = roi
    sub = fg[y1:y2, x1:x2]
    if not sub.any():
        return labels, unsure, {}
    yy, xx = np.mgrid[y1:y2, x1:x2].astype(np.float32)
    P = {k: v["xy"] for k, v in J.items() if v.get("xy") and v.get("state") != "absent"}
    need = ("chest", "pelvis", "shoulder_l", "shoulder_r", "hip_l", "hip_r")
    if not all(k in P for k in need):
        labels[y1:y2, x1:x2][sub] = fd.UNKNOWN
        return labels, unsure, {}
    L = math.dist(P["chest"], P["pelvis"]) or 50.0
    lerp = lambda a, b, t: (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
    segs = []  # (part, a, b, half width)
    if "head" in P and "neck" in P:
        segs.append(("head", lerp(P["neck"], P["head"], .5), lerp(P["neck"], P["head"], 1.6), .30 * L))
        segs.append(("neck", P["neck"], lerp(P["neck"], P["head"], .45), .13 * L))
    for side, s in (("left", "l"), ("right", "r")):
        chain = [("upper_arm", f"shoulder_{s}", f"elbow_{s}", .12), ("forearm", f"elbow_{s}", f"wrist_{s}", .10),
                 ("thigh", f"hip_{s}", f"knee_{s}", .18), ("shin", f"knee_{s}", f"foot_{s}", .12)]
        for part, a, b, w in chain:
            if a in P and b in P:
                segs.append((f"{side}_{part}", P[a], P[b], w * L))
        if f"foot_{s}" in P:
            tip = P.get(f"toe_{s}") or (lerp(P[f"knee_{s}"], P[f"foot_{s}"], 1.25) if f"knee_{s}" in P else None)
            if tip:
                segs.append((f"{side}_foot", P[f"foot_{s}"], tip, .08 * L))
            if f"heel_{s}" in P:
                segs.append((f"{side}_foot", P[f"foot_{s}"], P[f"heel_{s}"], .07 * L))
        if f"{side}_hand.middle.tip" in P and f"wrist_{s}" in P:
            segs.append((f"{side}_hand.unsplit", P[f"wrist_{s}"], P[f"{side}_hand.middle.tip"], .10 * L))
        elif f"wrist_{s}" in P and f"elbow_{s}" in P:
            segs.append((f"{side}_hand.unsplit", P[f"wrist_{s}"], lerp(P[f"elbow_{s}"], P[f"wrist_{s}"], 1.45), .09 * L))
    # Torso: inside the shoulder–hip quadrilateral a small constant distance (an arm drawn across
    # the torso stays an arm near its bone), else the distance to the spine.
    quad = _polygon_mask((y2 - y1, x2 - x1), [P["shoulder_l"], P["shoulder_r"], P["hip_r"], P["hip_l"]], roi)
    spine_a, spine_b = lerp(P["shoulder_l"], P["shoulder_r"], .5), P["pelvis"]
    names = ["torso"] + [s[0] for s in segs]
    dist = np.empty((len(names),) + sub.shape, np.float32)
    dist[0] = np.where(quad, .7, _segment_distance(xx, yy, spine_a, spine_b) / (.30 * L))
    for i, (_, a, b, w) in enumerate(segs, 1):
        dist[i] = _segment_distance(xx, yy, a, b) / w
    order = np.argsort(dist, axis=0)
    best, second = order[0], order[1]
    d_best = np.take_along_axis(dist, best[None], 0)[0]
    d_second = np.take_along_axis(dist, second[None], 0)[0]
    group = lambda name: name.split(".")[0].split("_")[0] if name != "torso" else "torso"
    groups = np.array([{"head": 0, "neck": 0, "torso": 1, "left": 2, "right": 3}.get(group(n), 4) for n in names])
    ambiguous = (d_second < 1.15 * d_best + .15) & (groups[best] != groups[second]) & sub
    far = (d_best > 3.0) & sub
    # Torso → chest / abdomen / pelvis by position along the spine.
    sx, sy = spine_b[0] - spine_a[0], spine_b[1] - spine_a[1]
    along = ((xx - spine_a[0]) * sx + (yy - spine_a[1]) * sy) / ((sx * sx + sy * sy) or 1)
    values = np.array([0] + [fd.PARTS[n]["value"] for n in names[1:]], np.uint8)
    lab = values[best]
    torso = best == 0
    lab[torso & (along < .45)] = fd.PARTS["chest"]["value"]
    lab[torso & (along >= .45) & (along < .78)] = fd.PARTS["abdomen"]["value"]
    lab[torso & (along >= .78)] = fd.PARTS["pelvis"]["value"]
    lab[far] = fd.UNKNOWN
    lab[~sub] = 0
    # Hands with good landmarks: their (slightly grown) hull, in front of everything else.
    hulls = {}
    for side in clean_hands:
        pts = [P[f"{side}_hand.{n}"] for n in fd.HAND_POINTS if f"{side}_hand.{n}" in P]
        if len(pts) == 21:
            from scipy.spatial import ConvexHull
            arr = np.array(pts)
            hull = _polygon_mask(sub.shape, [tuple(arr[i]) for i in ConvexHull(arr).vertices], roi)
            grow = max(1, round(.06 * float(np.linalg.norm(arr - arr[0], axis=1).max())))
            hulls[fd.PARTS[f"{side}_hand.unsplit"]["value"]] = ndimage.binary_dilation(hull, iterations=grow) & sub
    with_hands = lab.copy()
    for value, hull in hulls.items():
        with_hands[hull] = value
    # Snap to the drawing's outlines: a region enclosed by lines becomes a hand if a hand's hull
    # covers half of it, else the part covering most (72 %) of it; mixed regions keep the
    # geometric split. Big regions (photos, unoutlined drawings) keep the geometry with hands.
    gray = np.asarray(image.convert("L"), np.uint8)[y1:y2, x1:x2]
    lines = (gray < LINE_TONE) & sub
    regions, count = ndimage.label(sub & ~lines)
    area_all = int(sub.sum())
    lab_out = with_hands
    if count:
        pairs = np.bincount(regions.ravel() * 256 + lab.ravel(), minlength=(count + 1) * 256).reshape(count + 1, 256)
        sizes = pairs.sum(axis=1)
        tops = pairs.argmax(axis=1)
        shares = pairs.max(axis=1) / np.maximum(1, sizes)
        small = sizes < .35 * area_all
        region_label = np.where((shares >= .72) & (tops != fd.UNKNOWN), tops, -1)
        for value, hull in hulls.items():
            hand_share = np.bincount(regions[hull], minlength=count + 1) / np.maximum(1, sizes)
            region_label = np.where(hand_share >= .5, value, region_label)
        region_label[0] = -1
        mapped = np.where(small[regions], np.where(region_label[regions] >= 0, region_label[regions], lab), with_hands)
        lab_out = np.where(sub & ~lines, mapped, with_hands)
    lab = lab_out.astype(np.uint8)
    # Outline pixels take the label of the nearest figure pixel that is not a line.
    if lines.any():
        _, (iy, ix) = ndimage.distance_transform_edt(lines | ~sub, return_indices=True)
        lab = np.where(lines, lab[iy, ix], lab)
    unsure_sub = (ambiguous | far).astype(np.uint8) * 255
    labels[y1:y2, x1:x2] = lab
    unsure[y1:y2, x1:x2] = unsure_sub
    return labels, unsure, {"torso_px": int(L), "outline_regions": int(count)}


def split_fingers(image, labels, unsure, J, side, roi):
    """Inside one hand's pixels: palm (wrist–knuckle polygon) or the nearest finger chain;
    borders snapped to the drawn outlines like the body. Returns the number of finger pixels."""
    hand_value = fd.PARTS[f"{side}_hand.unsplit"]["value"]
    pts = {k.split(".", 1)[1]: v["xy"] for k, v in J.items() if k.startswith(f"{side}_hand.") and v.get("xy")}
    if len(pts) < 21:
        return 0
    length = max(math.dist(pts["wrist"], p) for p in pts.values())
    iy, ix = np.nonzero(labels == hand_value)  # the hand's pixels may reach past its landmarks
    xs = [p[0] for p in pts.values()] + ([int(ix.min()), int(ix.max())] if ix.size else [])
    ys = [p[1] for p in pts.values()] + ([int(iy.min()), int(iy.max())] if iy.size else [])
    x1, y1 = max(roi[0], int(min(xs)) - 1), max(roi[1], int(min(ys)) - 1)
    x2, y2 = min(roi[2], int(max(xs)) + 2), min(roi[3], int(max(ys)) + 2)
    if x2 <= x1 or y2 <= y1:
        return 0
    region = labels[y1:y2, x1:x2]
    mine = region == hand_value
    if not mine.any():
        return 0
    yy, xx = np.mgrid[y1:y2, x1:x2].astype(np.float32)
    palm_poly = [pts["wrist"], pts["thumb.cmc"], pts["index.mcp"], pts["middle.mcp"], pts["ring.mcp"], pts["pinky.mcp"]]
    palm = _polygon_mask(region.shape, palm_poly, (x1, y1))
    width = max(2.0, .11 * length)
    names, dist = ["palm"], [np.where(palm, 0, _segment_distance(xx, yy, pts["wrist"], pts["middle.mcp"]) / (2.2 * width))]
    for finger, chain in FINGER_CHAINS.items():
        d = np.full(region.shape, np.inf, np.float32)
        for a, b in zip(chain, chain[1:]):
            d = np.minimum(d, _segment_distance(xx, yy, pts[a], pts[b]))
        names.append(finger)
        dist.append(d / width)
    dist = np.stack(dist)
    best = dist.argmin(axis=0)
    values = np.array([fd.PARTS[f"{side}_hand.{n}"]["value"] for n in names], np.uint8)
    lab = values[best]
    gray = np.asarray(image.convert("L"), np.uint8)[y1:y2, x1:x2]
    lines = (gray < LINE_TONE) & mine
    regions, count = ndimage.label(mine & ~lines)
    if count:
        pairs = np.bincount(regions.ravel() * 256 + lab.ravel(), minlength=(count + 1) * 256).reshape(count + 1, 256)
        sizes, tops = pairs.sum(axis=1), pairs.argmax(axis=1)
        snap = (pairs.max(axis=1) / np.maximum(1, sizes) >= .72)
        snap[0] = False
        lab = np.where(snap[regions] & mine & ~lines, tops[regions], lab).astype(np.uint8)
    if lines.any():
        _, (iy, ix) = ndimage.distance_transform_edt(~(mine & ~lines), return_indices=True)
        lab = np.where(lines, lab[iy, ix], lab)
    region[mine] = lab[mine]
    labels[y1:y2, x1:x2] = region
    return int(mine.sum())


def part_info(labels, J, hands, view):
    """State, source and diagnostics of every terminal part."""
    counts = np.bincount(labels.ravel(), minlength=256)
    parts = {}
    for pid, part in fd.PARTS.items():
        px = int(counts[part["value"]])
        side = pid.split("_")[0] if pid.startswith(("left", "right")) else None
        diagnostics = []
        state = "visible" if px else "absent"
        if px and (view in ("side", "back") and side):
            state = "uncertain"
            diagnostics.append("вид со спины: левая и правая стороны могут быть перепутаны" if view == "back" else "вид сбоку: дальняя конечность перекрыта ближней")
        if ".unsplit" in pid and px:
            hand = hands.get(side[0])
            if hand is None:
                diagnostics.append("точки кисти не найдены: пальцы не разделены")
            elif hand["issues"]:
                diagnostics.append("точки кисти не прошли проверку (" + ", ".join(ISSUE_NAMES.get(i, i) for i in hand["issues"]) + "): пальцы не разделены")
            elif hand["length"] < SMALL_HAND_PX:
                diagnostics.append(f"кисть слишком мала для пальцев ({hand['length']:.0f} px)")
            state = "uncertain"
        if pid == "abdomen" and px:
            diagnostics.append("граница груди, живота и таза — условность (по положению на позвоночнике)")
        parts[pid] = {"state": state, "diagnostics": diagnostics,
                      "source": {"kind": "estimated", "method": "bones+outlines", "model": None}}
    return parts


def has_body(joints):
    return bool(joints) and all(joints.get(k, {}).get("xy") for k in ("chest", "pelvis", "shoulder_l", "shoulder_r", "hip_l", "hip_r"))


def given_joints(joints):
    """The document's joints as the analysis' own; hands whose 21 points are all there count as
    found, with an issue if any of them is marked uncertain."""
    J = {k: v for k, v in joints.items() if v.get("xy") and v.get("state") != "absent"}
    hands = {}
    for s, side in (("l", "left"), ("r", "right")):
        pts = {n: J[f"{side}_hand.{n}"] for n in fd.HAND_POINTS if f"{side}_hand.{n}" in J}
        if len(pts) == 21:
            wrist = pts["wrist"]["xy"]
            hands[s] = {"issues": ["hand landmarks marked uncertain"] if any(v.get("state") == "uncertain" for v in pts.values()) else [],
                        "length": max(math.dist(wrist, v["xy"]) for v in pts.values())}
    return J, hands


def analyze(request: dict, out: Path):
    t0 = time.perf_counter()
    path = Path(request["image"])
    with Image.open(path) as opened:
        opened.load()
        image = ImageOps.exif_transpose(opened).convert("RGBA")
    W, H = image.size
    roi = [int(v) for v in request.get("roi") or (0, 0, W, H)]
    alpha = np.asarray(image.getchannel("A")) > 8
    transparent = not alpha.all()
    progress(.02, "foreground")
    if transparent:
        fg, fg_source = alpha, "alpha"
    else:
        fg, fg_source = person_mask(image, roi), "mediapipe selfie segmentation"
    clip = np.zeros_like(fg)
    clip[roi[1]:roi[3], roi[0]:roi[2]] = True
    fg = fg & clip
    info = {"foreground": fg_source}
    J = None
    if request.get("mode") != "masks":
        J, hands, found = landmarks(image, roi, fg)
        info.update(found)
    if J is None and has_body(request.get("joints")):
        # masks from the document's own joints (set by hand, or the library's skeleton)
        J, hands = given_joints(request["joints"])
        info["body"] = "document joints"
        info["view"] = view_of(J)
    if J is None:
        labels = np.where(fg, fd.UNKNOWN, 0).astype(np.uint8)
        save(out, labels, np.zeros_like(labels), {}, {pid: {"state": "absent", "diagnostics": ["тело не найдено"],
                                                             "source": {"kind": "estimated", "method": "none"}} for pid in fd.PARTS},
             {**info, "error": "body not found", "seconds": round(time.perf_counter() - t0, 2)})
        return
    progress(.65, "masks")
    clean = [side for s, side in (("l", "left"), ("r", "right"))
             if s in hands and not hands[s]["issues"] and hands[s]["length"] >= SMALL_HAND_PX]
    labels, unsure, mask_info = build_masks(image, fg, J, roi, clean)
    info.update(mask_info)
    for side in clean:
        split_fingers(image, labels, unsure, J, side, roi)
    progress(.95, "checks")
    parts = part_info(labels, J, hands, info.get("view"))
    info["model"] = MODEL if request.get("mode") != "masks" else None
    info["seconds"] = round(time.perf_counter() - t0, 2)
    info["peak_mb"] = peak_mb()
    # joints that came from the document are not the analysis' to write back
    save(out, labels, unsure, {} if info.get("body") == "document joints" else J, parts, info)


def person_mask(image, roi):
    mp = offline_mediapipe()
    # on the chosen region only: a person small in a big picture (a sheet of frames) is lost otherwise
    seg = mp.solutions.selfie_segmentation.SelfieSegmentation(model_selection=0)
    x1, y1, x2, y2 = roi
    out = np.zeros((image.height, image.width), bool)
    piece = image.convert("RGB").crop((x1, y1, x2, y2))
    k = 512 / max(piece.size)
    work = piece.resize((max(1, round(piece.width * k)), max(1, round(piece.height * k))), Image.BILINEAR) if k > 1 else piece
    found = seg.process(np.asarray(work))
    if found.segmentation_mask is None:
        out[y1:y2, x1:x2] = True
        return out
    mask = Image.fromarray((found.segmentation_mask * 255).astype(np.uint8)).resize(piece.size, Image.BILINEAR)
    out[y1:y2, x1:x2] = np.asarray(mask) > 127
    return out


def peak_mb():
    """Peak working set of this process (Windows), None elsewhere."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    psapi = ctypes.WinDLL("psapi")
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    kernel = ctypes.WinDLL("kernel32")
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return None
    return round(counters.PeakWorkingSetSize / 2 ** 20)


def save(out: Path, labels, unsure, joints, parts, info):
    out.mkdir(parents=True, exist_ok=True)
    Image.fromarray(labels, "L").save(out / "labels.png")
    Image.fromarray(unsure, "L").save(out / "uncertain.png")
    (out / "result.json").write_text(json.dumps({"joints": joints, "parts": parts, "analysis": info}, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
    progress(1, "done")


if __name__ == "__main__":
    try:
        analyze(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")), Path(sys.argv[2]))
    except ModelMissing as error:
        print(f"PoseStage analyzer: {error}", file=sys.stderr)
        sys.exit(3)
