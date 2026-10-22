"""Dance phrases (docs/DANCE.md): pose features, transitions, phrase search and names, outfit
matching and its cache, bone-driven transitions, the folder export.

Run with: python -m unittest discover -s tests -v
"""
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image, ImageChops, ImageDraw, ImageOps

import dance
import dance_motion
import poses

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import export_phrases  # noqa: E402

# A standing front figure on a 400×1000 canvas, figure's left on the image's right.
BODY = {"head": (200, 130), "chest": (200, 255), "navel": (200, 360), "pelvis": (200, 470),
        "shoulder_l": (250, 230), "shoulder_r": (150, 230), "hip_l": (230, 470), "hip_r": (170, 470),
        "knee_l": (235, 680), "knee_r": (165, 680), "foot_l": (240, 880), "foot_r": (160, 880)}
ARM = 95  # upper arm and forearm length


def skeleton(arm_angle=math.pi / 2, view="front", **changes):
    """Both arms at `arm_angle` from the shoulder (π/2 = hanging down, measured toward the outside)."""
    points = dict(BODY)
    for side, out in (("l", 1), ("r", -1)):
        sx, sy = BODY[f"shoulder_{side}"]
        dx, dy = math.cos(arm_angle) * out, math.sin(arm_angle)
        points[f"elbow_{side}"] = (sx + dx * ARM, sy + dy * ARM)
        points[f"wrist_{side}"] = (sx + 2 * dx * ARM, sy + 2 * dy * ARM)
    points.update(changes)
    return {"version": 2, "view": view,
            "points": {k: {"xy": list(v), "state": "visible"} for k, v in points.items()}}


def pose(file, joints=None, mode="feet"):
    return {"file": file, "w": 400, "h": 1000, "box": [40, 80, 360, 900],
            "joints": joints or skeleton(), "support": {"mode": mode, "profile": [[150, 900], [250, 900]]}}


def drawing(joints, outfit=(200, 200, 200), shoes=False):
    """A crude figure: light skin, top and shorts of the given tone, optional dark shoes."""
    image = Image.new("RGBA", (400, 1000), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    p = {k: tuple(v["xy"]) for k, v in joints["points"].items()}
    draw.ellipse((160, 80, 240, 180), fill=(225, 225, 225, 255), outline=(60, 60, 60, 255), width=3)
    draw.rectangle((150, 220, 250, 440), fill=(225, 225, 225, 255), outline=(60, 60, 60, 255), width=3)
    draw.rectangle((150, 220, 250, 320), fill=outfit + (255,))
    draw.rectangle((150, 440, 250, 540), fill=outfit + (255,))
    for a, b in (("shoulder_l", "elbow_l"), ("elbow_l", "wrist_l"), ("shoulder_r", "elbow_r"), ("elbow_r", "wrist_r"),
                 ("hip_l", "knee_l"), ("knee_l", "foot_l"), ("hip_r", "knee_r"), ("knee_r", "foot_r")):
        draw.line([p[a], p[b]], fill=(215, 215, 215, 255), width=34)
    if shoes:
        for side in ("l", "r"):
            x, y = p[f"foot_{side}"]
            draw.ellipse((x - 40, y - 25, x + 40, y + 25), fill=(30, 30, 30, 255))
    return image


class FeatureTests(unittest.TestCase):
    def test_mirror_features_match_a_mirrored_pose(self):
        original = pose("a.png", skeleton(math.pi / 2, wrist_l=(330, 200)))
        direct = dance.features(original, mirror=True)
        flipped = dance.features(dance.mirror_pose(original))
        for name, angle in direct["angles"].items():
            self.assertAlmostEqual(dance.wrap(angle - flipped["angles"][name]), 0, places=6, msg=name)
        for key, (xy, _) in direct["ends"].items():
            self.assertAlmostEqual(xy[0], flipped["ends"][key][0][0], places=6)
            self.assertAlmostEqual(xy[1], flipped["ends"][key][0][1], places=6)

    def test_mirror_pose_swaps_sides_and_keeps_states(self):
        joints = skeleton()
        joints["points"]["wrist_l"] = {"state": "absent"}
        original = dict(pose("a.png", joints), box=[40, 80, 300, 900])
        original["support"] = {"mode": "feet", "profile": [[150, 900], [200, 905]]}
        flipped = dance.mirror_pose(original)
        self.assertEqual(flipped["box"], [100, 80, 360, 900])
        self.assertEqual(flipped["joints"]["points"]["wrist_r"], {"state": "absent"})
        self.assertEqual(flipped["joints"]["points"]["shoulder_r"]["xy"], [400 - 250, 230])
        self.assertEqual(flipped["support"]["profile"], [[200, 905], [250, 900]])
        self.assertEqual(original["box"], [40, 80, 300, 900])  # the original is untouched

    def test_facing_of_turned_figures(self):
        side = skeleton(view="side")
        for key in ("shoulder_r", "elbow_r", "wrist_r", "hip_r", "knee_r", "foot_r"):
            side["points"][key]["state"] = "hidden"  # left side toward the viewer: looks right
        right = dance.features(pose("s.png", side))
        left = dance.features(pose("s.png", side), mirror=True)
        self.assertEqual((right["facing"], left["facing"]), (1, -1))
        self.assertEqual(dance.features(pose("f.png"))["facing"], 0)
        self.assertEqual(dance.features(dance.mirror_pose(pose("s.png", side)))["facing"], -1)
        self.assertGreaterEqual(dance.transition_cost(right, left), dance.FACING_PENALTY)

    def test_without_a_v2_skeleton_there_are_no_features(self):
        self.assertIsNone(dance.features({"file": "x.png", "joints": None}))

    def test_transition_costs(self):
        a = dance.features(pose("a.png"))
        self.assertLess(dance.transition_cost(a, dance.features(pose("b.png"))), dance.DUPLICATE)
        raised = dance.features(pose("c.png", skeleton(math.pi / 2 - .5)))
        self.assertTrue(dance.DUPLICATE <= dance.transition_cost(a, raised) <= dance.FLOW)
        back = dance.features(pose("d.png", skeleton(view="back")))
        self.assertEqual(dance.transition_cost(a, back), math.inf)
        kneeling = dance.features(pose("e.png", skeleton(math.pi / 2 - .5), mode="kneel"))
        self.assertAlmostEqual(dance.transition_cost(a, kneeling) - dance.transition_cost(a, raised),
                               dance.SUPPORT_PENALTY)

    def test_acceleration_is_zero_for_the_same_direction(self):
        self.assertAlmostEqual(dance.acceleration({"x": 1, "y": 2}, {"x": 2, "y": 4}), 0)
        self.assertAlmostEqual(dance.acceleration({"x": 1}, {"x": -1}), 1)


class PhraseTests(unittest.TestCase):
    def arms(self, angles, prefix="p"):
        return [pose(f"{prefix}{i}.png", skeleton(angle)) for i, angle in enumerate(angles)]

    def test_rising_arms_become_one_ordered_phrase(self):
        steps = [math.pi / 2 - .35 * i for i in range(8)]  # arms rise from hanging to over the head
        shuffled = self.arms(steps)
        shuffled = shuffled[::2] + shuffled[1::2]
        phrases = dance.find_phrases(shuffled)
        self.assertTrue(phrases)
        best = max(phrases, key=lambda p: len(p["poses"]))
        order = [int(Path(entry["file"]).stem[1:]) for entry in best["poses"]]
        self.assertGreaterEqual(len(order), dance.MIN_LENGTH)
        self.assertIn(order, (sorted(order), sorted(order, reverse=True)))  # by the movement, not by name
        self.assertTrue(all(abs(a - b) == 1 for a, b in zip(order, order[1:])))
        self.assertIn(best["name"].split(" · ")[0], ("Руки вверх", "Руки вниз", "Руки в стороны", "Руки к телу"))
        self.assertFalse(best["loop"])

    def test_a_pose_appears_in_one_phrase_only(self):
        phrases = dance.find_phrases(self.arms([math.pi / 2 - .35 * i for i in range(8)]))
        files = [entry["file"] for phrase in phrases for entry in phrase["poses"]]
        self.assertEqual(len(files), len(set(files)))

    def test_names_follow_the_whole_phrase(self):
        node = lambda angle, **changes: dance.features(pose("n.png", skeleton(angle, **changes)))
        up = [node(math.pi / 2 - .35 * i) for i in range(6)]
        self.assertEqual(dance.name_of(up).split(" · ")[0], "Руки вверх")
        self.assertEqual(dance.name_of(up[::-1]).split(" · ")[0], "Руки вниз")
        wave = [node(a) for a in (1.57, 1.0, .5, 1.0, 1.57)]  # up and back down: no net change
        self.assertEqual(dance.name_of(wave), "Руки волной")
        self.assertEqual(dance.name_of([node(1.57), node(1.5)]), "Волна")
        lean = [node(1.57, chest=(200 + 30 * i, 255), head=(200 + 45 * i, 130)) for i in range(4)]
        self.assertIn("Наклон вправо", dance.name_of(lean))  # chest toward the image's right


class StyleTests(unittest.TestCase):
    def setUp(self):
        self.joints = skeleton()
        self.base = dance.style_signature(pose("a.png", self.joints), drawing(self.joints))

    def test_same_outfit_is_near_and_a_new_one_is_far(self):
        same = dance.style_signature(pose("b.png", skeleton(1.2)), drawing(skeleton(1.2)))
        dark = dance.style_signature(pose("c.png", self.joints), drawing(self.joints, outfit=(25, 25, 25)))
        shoes = dance.style_signature(pose("d.png", self.joints), drawing(self.joints, shoes=True))
        self.assertLess(dance.style_distance(self.base, same), .05)
        self.assertGreater(dance.style_distance(self.base, dark), .15)  # two of five regions changed
        self.assertGreater(dance.style_distance(self.base, shoes), dance.style_distance(self.base, same) + .1)
        self.assertEqual(dance.style_distance(None, self.base), .5)

    def test_mirroring_swaps_the_feet(self):
        one_shoe = drawing(self.joints)
        draw = ImageDraw.Draw(one_shoe)
        draw.ellipse((200, 855, 280, 905), fill=(30, 30, 30, 255))  # left foot only
        a = dance.style_signature(pose("a.png", self.joints), one_shoe)
        mirrored = dance.style_signature(dance.mirror_pose(pose("a.png", self.joints)),
                                         one_shoe.transpose(Image.FLIP_LEFT_RIGHT))
        self.assertLess(dance.style_distance(a, mirrored, False, True), 1e-9)
        self.assertGreater(dance.style_distance(a, mirrored), .05)

    def test_outfit_limit_on_edges(self):
        a, b = pose("a.png", skeleton(1.57)), pose("b.png", skeleton(1.07))
        light = {p["file"]: dance.style_signature(p, drawing(p["joints"])) for p in (a, b)}
        dark = dict(light, **{"b.png": dance.style_signature(b, drawing(b["joints"], outfit=(25, 25, 25)))})
        edges = lambda styles: sum(len(v) for v in dance.build_graph([a, b], styles)[1].values())
        self.assertGreater(edges(light), 0)
        self.assertEqual(edges(dark), 0)
        self.assertGreater(edges({"a.png": light["a.png"]}), 0)  # unknown outfit: no hard limit

    def test_outfit_change_breaks_the_phrase(self):
        steps = [math.pi / 2 - .35 * i for i in range(8)]
        items = [pose(f"p{i}.png", skeleton(a)) for i, a in enumerate(steps)]
        tones = [(200, 200, 200)] * 4 + [(25, 25, 25)] * 4
        styles = {p["file"]: dance.style_signature(p, drawing(p["joints"], outfit=tone)) for p, tone in zip(items, tones)}
        for phrase in dance.find_phrases(items, styles=styles):
            dark = {tones[int(Path(e["file"]).stem[1:])] for e in phrase["poses"]}
            self.assertEqual(len(dark), 1)

    def test_signatures_are_cached_by_file_and_skeleton(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            items = []
            for i in range(3):
                drawing(skeleton()).save(tmp / f"p{i}.png")
                items.append(pose(f"p{i}.png"))
            resolve = lambda file: tmp / file if (tmp / file).exists() else None
            cache = tmp / "data" / "styles.json"
            calls = []
            real = dance.style_signature
            counting = lambda *args: calls.append(1) or real(*args)
            with mock.patch.object(dance, "style_signature", counting):
                first = dance.load_styles(items, resolve, cache)
                self.assertEqual((len(first), len(calls)), (3, 3))
                again = dance.load_styles(items, resolve, cache)
                self.assertEqual(len(calls), 3)  # all from the cache
                self.assertEqual(again, first)
                os.utime(tmp / "p0.png", ns=(1, 1))  # the file changed
                items[1] = pose("p1.png", skeleton(pelvis=(200, 480)))  # the skeleton was corrected
                items[2] = pose("p2.png", skeleton(1.0))  # arms only: the measured regions stay
                dance.load_styles(items + [pose("gone.png")], resolve, cache)
                self.assertEqual(len(calls), 5)
            cache.write_text("broken", encoding="utf-8")
            self.assertEqual(len(dance.load_styles(items, resolve, cache)), 3)


class MotionTests(unittest.TestCase):
    def joints(self, angle):
        return {k: tuple(v["xy"]) for k, v in skeleton(angle)["points"].items()}

    def test_in_between_ends_and_keeps_bone_lengths(self):
        ja, jb = self.joints(math.pi / 2), self.joints(math.pi / 2 - 1.2)
        jb.update(chest=(240, 250), head=(260, 135))  # the torso leans and stretches
        ease = dance_motion.curve("soft")
        start, end = dance_motion.in_between(ja, jb, 0, ease), dance_motion.in_between(ja, jb, 1, ease)
        for key in ja:
            self.assertAlmostEqual(math.dist(start[key], ja[key]), 0, places=6)
            self.assertAlmostEqual(math.dist(end[key], jb[key]), 0, places=6)
        mid = dance_motion.in_between(ja, jb, .5, ease)
        for child, parent in dance_motion.TREE:
            la, lb = math.dist(ja[child], ja[parent]), math.dist(jb[child], jb[parent])
            self.assertTrue(min(la, lb) - 1e-6 <= math.dist(mid[child], mid[parent]) <= max(la, lb) + 1e-6, child)
        self.assertGreater(abs(math.dist(ja["chest"], ja["navel"]) - math.dist(jb["chest"], jb["navel"])), 1)

        def progress(u, child, parent):  # share of the bone's own turn done at u
            angle = lambda j: math.atan2(j[child][1] - j[parent][1], j[child][0] - j[parent][0])
            j = dance_motion.in_between(ja, jb, u, ease)
            return dance.wrap(angle(j) - angle(ja)) / dance.wrap(angle(jb) - angle(ja))
        # The torso leads, the arm follows, the hand comes last.
        self.assertGreater(progress(.2, "chest", "navel"), progress(.2, "elbow_l", "shoulder_l"))
        self.assertGreater(progress(.2, "elbow_l", "shoulder_l"), progress(.2, "wrist_l", "elbow_l"))

    def test_inverse_warp_holds_for_large_rotations(self):
        rest = self.joints(math.pi / 2)
        skin = dance_motion.Skin(rest, 215)
        for degrees in (45, 90, 135, 170):
            a = math.radians(degrees)
            posed = dict(rest, wrist_l=(rest["elbow_l"][0] + 95 * math.sin(a), rest["elbow_l"][1] + 95 * math.cos(a)))
            warp = dance_motion.Warp(skin, rest, posed)
            for i in range(30):
                target = warp.forward(rest["elbow_l"][0] + (i % 5 - 2) * 6, rest["elbow_l"][1] + 5 + i * 4)
                self.assertLess(warp.inverse(*target)[2], 1.0, f"{degrees} degrees, point {i}")

    def test_curves(self):
        for name in dance_motion.CURVES:
            at = dance_motion.curve(name)
            self.assertEqual((at(-1), at(0), at(1), at(2)), (0, 0, 1, 1))
        hit = [dance_motion.curve("hit")(i / 100) for i in range(101)]
        self.assertLess(min(hit), 0)  # wind-up
        self.assertGreater(max(hit), 1)  # overshoot

    def test_identity_deform_keeps_the_drawing(self):
        joints = skeleton()
        image = drawing(joints)
        points = {k: tuple(v["xy"]) for k, v in joints["points"].items()}
        skin = dance_motion.Skin(points, 215)
        out = dance_motion.deform(image, (100, 50, 300, 950), skin, points, points)
        diff = ImageChops.difference(out.getchannel("A"), image.getchannel("A")).crop((100, 50, 300, 950))
        self.assertLess(sum(diff.getdata()) / (200 * 900 * 255), .01)

    def test_groove_keeps_the_feet_and_bounces_on_the_beat(self):
        points = self.joints(math.pi / 2)
        on, off = dance_motion.groove(points, 0, .5, 800), dance_motion.groove(points, .25, .5, 800)
        self.assertEqual((on["foot_l"], on["foot_r"]), (points["foot_l"], points["foot_r"]))
        self.assertGreater(on["pelvis"][1], off["pelvis"][1])  # lower on the count
        self.assertAlmostEqual(on["pelvis"][1] - points["pelvis"][1], dance_motion.GROOVE * 800)

    def test_dance_frames_count_and_size(self):
        canvases, joints = [], []
        for angle in (math.pi / 2, math.pi / 2 - .5, math.pi / 2 - 1):
            j = skeleton(angle)
            canvases.append(drawing(j))
            joints.append({k: tuple(v["xy"]) for k, v in j["points"].items()})
        render = lambda layer: layer.convert("RGB").resize((40, 100))
        frames = dance_motion.dance_frames(canvases, joints, True, render, bpm=120, fps=10, style="groove")
        self.assertEqual(len(frames), 3 * 5)  # three counts (looped) of half a second at 10 fps
        self.assertEqual({f.size for f in frames}, {(40, 100)})
        with self.assertRaises(ValueError):
            dance_motion.dance_frames(canvases, joints, True, render, rhythm=[(1, .6, "hit")])
        with self.assertRaises(ValueError):
            dance_motion.dance_frames(canvases, joints, False, render, rhythm=[(1, 0, "hit")] * 2)


class ExportTests(unittest.TestCase):
    def test_existing_folders_continue_numbers_and_are_recognised(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "02 Волна").mkdir()
            (out / "02 Волна" / "01__a.png").write_bytes(b"")
            (out / "02 Волна" / "02__b__зеркально.png").write_bytes(b"")
            (out / "02 Волна" / "Thumbs.db").write_bytes(b"")
            (out / "_просмотр").mkdir()
            (out / "_просмотр" / "05 Удалённая.png").write_bytes(b"")
            (out / "notes.txt").write_text("", encoding="utf-8")
            poses.write_json(out / export_phrases.MANIFEST, {"phrases": [{"number": 4, "poses": ["c", "d"]}]})
            highest, known = export_phrases.existing(out)
            self.assertEqual(highest, 5)
            self.assertEqual(known, {frozenset({"a", "b__зеркально"}), frozenset({"c", "d"})})
            self.assertEqual(export_phrases.existing(out / "missing"), (0, set()))

    def test_export_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            folder, out = tmp / "poses", tmp / "Позы — связки"
            folder.mkdir()
            items = []
            for i in range(8):
                p = pose(f"p{i}.png", skeleton(math.pi / 2 - .35 * i, wrist_r=(60, 300 - 20 * i)))
                image = drawing(p["joints"])
                ImageDraw.Draw(image).rectangle((240, 600, 280, 640), fill=(10, 10, 10, 255))  # asymmetric mark
                image.save(folder / p["file"])
                items.append(p)
            before = {f.name: f.read_bytes() for f in folder.iterdir()}

            class Catalog:
                def __init__(self, root, prefix):
                    self.root = root

                def scan(self):
                    pass

                def get(self):
                    return [dict(p) for p in items]

                def resolve(self, file):
                    return self.root / file

            def phrase_folders():
                return sorted(f for f in out.iterdir() if f.is_dir() and f.name != export_phrases.REVIEW)
            old = poses.DATA_DIR
            poses.DATA_DIR = tmp / "appdata"
            try:
                with mock.patch.object(export_phrases.poses, "PoseCatalog", Catalog), mock.patch("builtins.print"):
                    export_phrases.main(["--folder", str(folder), "--out", str(out)])
                    folders = phrase_folders()
                    self.assertTrue(folders)
                    copies = 0
                    for phrase in folders:
                        self.assertTrue((out / export_phrases.REVIEW / f"{phrase.name}.png").is_file())
                        for copy in phrase.iterdir():
                            copies += 1
                            name = re.sub(r"^\d+__", "", copy.stem).replace(export_phrases.MIRROR_TAG, "")
                            with Image.open(copy) as a, Image.open(folder / f"{name}.png") as b:
                                expected = b.convert("RGBA")
                                if export_phrases.MIRROR_TAG in copy.name:
                                    expected = ImageOps.mirror(expected)
                                self.assertIsNone(ImageChops.difference(a.convert("RGBA"), expected).getbbox(), copy.name)
                    corrections = poses.read_joint_overrides(poses.collection_id(str(out.resolve())))
                    self.assertEqual(len(corrections), copies)
                    for key, joints in corrections.items():
                        source = items[int(re.search(r"__p(\d+)", key).group(1))]["joints"]["points"]
                        if export_phrases.MIRROR_TAG in key:
                            self.assertEqual(joints["points"]["wrist_l"]["xy"][0], 400 - source["wrist_r"]["xy"][0])
                        else:
                            self.assertEqual(joints["points"]["wrist_r"]["xy"], source["wrist_r"]["xy"])
                    # Again: nothing new; a deleted folder stays rejected and nothing is overwritten.
                    sheet = out / export_phrases.REVIEW / f"{folders[0].name}.png"
                    stamp = sheet.stat().st_mtime_ns
                    shutil.rmtree(folders[0])
                    export_phrases.main(["--folder", str(folder), "--out", str(out)])
                    self.assertEqual(phrase_folders(), folders[1:])
                    self.assertEqual(sheet.stat().st_mtime_ns, stamp)
            finally:
                poses.DATA_DIR = old
            self.assertEqual({f.name: f.read_bytes() for f in folder.iterdir()}, before)  # pose folder untouched

    def test_refuses_to_write_into_the_pose_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
                export_phrases.main(["--folder", tmp, "--out", str(Path(tmp) / "Позы — связки")])


if __name__ == "__main__":
    unittest.main()
