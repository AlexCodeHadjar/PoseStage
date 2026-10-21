"""Pose animation: skeleton version 2 (docs/SKELETON.md), the user's corrections, the idle rig.

Run with: python -m unittest discover -s tests -v
"""
import copy
import math
from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageDraw

import app
import desktop
import poses

ROOT = Path(__file__).resolve().parent.parent
V1 = {"nose": [200, 130], "shoulders": [[240, 220], [160, 220]], "elbows": [[300, 300], [100, 300]],
      "wrists": [[360, 260], [40, 260]], "hips": [[230, 470], [170, 470]]}


def v2(**changes):
    """A standing front skeleton (version 2) with some points changed."""
    skeleton = poses.clean_joints(V1)
    for key, value in changes.items():
        skeleton["points"][key] = value
    return skeleton


def standing(file="a.png", joints=None):
    profile = [[x, 900] for x in range(150, 260, 10)]
    return {"file": file, "box": [30, 100, 370, 900], "w": 400, "h": 1000,
            "joints": v2() if joints is None else joints,
            "support": {"mode": "feet", "cut": .93, "profile": profile}}


class SkeletonFormatTests(unittest.TestCase):
    def test_v1_converts_to_v2(self):
        skeleton = poses.clean_joints(V1)
        self.assertEqual((skeleton["version"], skeleton["view"]), (2, "front"))
        self.assertEqual(set(skeleton["points"]), set(poses.JOINT_IDS))
        self.assertEqual(len(poses.JOINT_IDS), 16)
        points = skeleton["points"]
        self.assertEqual(points["head"]["xy"], [200.0, 130.0])
        self.assertEqual(points["pelvis"]["xy"], [200.0, 470.0])  # middle of the hips
        self.assertEqual(points["chest"]["xy"], [200.0, 257.5])  # shoulders, 15 % toward the pelvis
        self.assertEqual(points["shoulder_l"]["xy"], [240.0, 220.0])  # figure's left = MediaPipe left
        self.assertEqual(points["knee_l"]["state"], "hidden")  # unknown in v1: placed, never moves
        self.assertTrue(poses.valid_joints(V1))

    def test_validation(self):
        good = v2()
        self.assertTrue(poses.valid_joints(good))
        bad = [
            {**good, "view": "top"},
            v2(head={"state": "absent"}),  # the axis is required
            v2(chest={"xy": [1, 2], "state": "gone"}),
            v2(wrist_l={"state": "visible"}),  # visible needs a position
            v2(wrist_l={"xy": [float("nan"), 0], "state": "visible"}),
            v2(wrist_l={"xy": [1e9, 0], "state": "visible"}),
            v2(foot_l={"xy": [1, 2], "state": "visible", "support": "yes"}),
            v2(foot_l={"state": "absent", "support": True}),  # nothing to pin
            {**good, "points": {**good["points"], "tail": {"xy": [1, 2]}}},
            {"version": 3, "points": {}},
            "x",
        ]
        for skeleton in bad:
            self.assertFalse(poses.valid_joints(skeleton, 400, 1000), skeleton if isinstance(skeleton, str) else "")
        absent = poses.clean_joints(v2(knee_r={"state": "absent", "xy": [5, 5]}))
        self.assertEqual(absent["points"]["knee_r"], {"state": "absent"})  # no position kept

    def test_bundled_skeletons_are_v2(self):
        bundled = poses.bundled_joints()
        self.assertGreater(len(bundled), 150)
        sample = next(iter(bundled.values()))
        self.assertEqual(sample["version"], 2)
        self.assertIn(sample["view"], poses.JOINT_VIEWS)
        views = {s["view"] for s in bundled.values()}
        self.assertIn("side", views)


class JointsApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old = poses.DATA_DIR, app.DATA_DIR
        poses.DATA_DIR = app.DATA_DIR = self.root / "appdata"
        self.images = self.root / "poses"
        self.images.mkdir()
        for name in ("photo-000001__pose-01.png", "own.png"):
            im = Image.new("RGBA", (400, 1000))
            ImageDraw.Draw(im).rectangle((150, 100, 250, 900), fill=(200, 200, 200, 255))
            im.save(self.images / name)

    def tearDown(self):
        poses.DATA_DIR, app.DATA_DIR = self.old
        self.tmp.cleanup()

    def catalog(self):
        catalog = poses.PoseCatalog(self.images, "/t")
        catalog.scan()
        return catalog

    def test_priority_user_over_bundled_over_none(self):
        by_file = {p["file"]: p for p in self.catalog().get()}
        self.assertEqual(by_file["photo-000001__pose-01.png"]["joints_source"], "bundled")
        self.assertIsNone(by_file["own.png"]["joints"])
        api = app.Api(self.catalog())
        side = v2(shoulder_r={"xy": [200, 220], "state": "hidden"})
        side["view"] = "side"
        pose = api.set_joints("own.png", side)
        self.assertEqual((pose["joints_source"], pose["joints"]["view"]), ("user", "side"))
        self.assertEqual(pose["joints"]["points"]["shoulder_r"], {"state": "hidden", "xy": [200.0, 220.0]})
        self.assertEqual(api.set_joints("photo-000001__pose-01.png", V1)["joints"]["version"], 2)  # v1 is upgraded
        by_file = {p["file"]: p for p in self.catalog().get()}  # persisted across scans
        self.assertEqual(by_file["own.png"]["joints"]["view"], "side")
        self.assertEqual(api.set_joints("photo-000001__pose-01.png", None)["joints_source"], "bundled")
        self.assertIsNone(api.set_joints("own.png")["joints"])

    def test_old_corrections_get_the_detected_legs(self):
        cid = poses.collection_id(self.images.resolve())
        poses.write_json(poses.DATA_DIR / "joint_overrides.json", {cid: {"photo-000001__pose-01.png": V1}})
        pose = next(p for p in self.catalog().get() if p["file"] == "photo-000001__pose-01.png")
        bundled = poses.bundled_joints()["photo-000001__pose-01.png"]
        self.assertEqual(pose["joints_source"], "user")
        self.assertEqual(pose["joints"]["points"]["head"]["xy"], [200.0, 130.0])  # the user's points stay
        for key in ("knee_l", "knee_r", "foot_l", "foot_r"):
            self.assertEqual(pose["joints"]["points"][key], bundled["points"][key])

    def test_invalid_skeletons_are_rejected_or_ignored(self):
        api = app.Api(self.catalog())
        for bad in ({}, v2(head={"state": "absent"}), {"nose": [1]}, "x"):
            with self.assertRaises(ValueError):
                api.set_joints("own.png", bad)
        with self.assertRaises(ValueError):
            api.set_joints("missing.png", v2())
        cid = poses.collection_id(self.images.resolve())
        poses.write_json(poses.DATA_DIR / "joint_overrides.json", {cid: {"own.png": {"version": 2, "points": {}}}})
        self.assertIsNone(next(p for p in self.catalog().get() if p["file"] == "own.png")["joints"])

    def test_mode_and_size_changes_keep_the_skeleton(self):
        catalog = self.catalog()
        catalog.set_joints("own.png", v2())
        self.assertEqual(catalog.set_mode("own.png", "kneel")["joints_source"], "user")
        self.assertEqual(catalog.set_scale("own.png", 1.1)["joints_source"], "user")


def motion(rig, x, y):
    return max(math.hypot(*rig.displace(x, y, t / 10)) for t in range(60))


class IdleRigTests(unittest.TestCase):
    def test_loop_is_seamless_and_supports_stay(self):
        rig = desktop.IdleRig(standing())
        for x, y in ((200, 150), (300, 300), (200, 400)):
            for t in (0, .4, 2.2):
                a, b = rig.displace(x, y, t), rig.displace(x, y, t + desktop.IDLE_LOOP)
                self.assertAlmostEqual(a[0], b[0], places=9)
                self.assertAlmostEqual(a[1], b[1], places=9)
        for x in (160, 200, 250):
            self.assertEqual(rig.displace(x, 900, 1.7), (0.0, 0.0))
        self.assertGreater(motion(rig, 200, 130), 3)
        self.assertGreater(motion(rig, 200, 130), motion(rig, 200, 480) * 2)

    def test_hidden_or_supported_hands_do_not_swing(self):
        self.assertEqual(len(desktop.IdleRig(standing()).hands), 2)
        hidden = v2(wrist_l={"xy": [360, 260], "state": "hidden"})
        self.assertEqual(len(desktop.IdleRig(standing(joints=hidden)).hands), 1)
        pinned = v2(wrist_r={"xy": [40, 260], "state": "visible", "support": True})
        rig = desktop.IdleRig(standing(joints=pinned))
        self.assertEqual(len(rig.hands), 1)
        self.assertEqual(rig.displace(40, 260, 1.3), (0.0, 0.0))  # a support flag pins the point

    def test_support_false_releases_a_contour_contact(self):
        released = v2(foot_l={"xy": [160, 900], "state": "visible", "support": False})
        rig = desktop.IdleRig(standing(joints=released))
        self.assertFalse(any(math.hypot(x - 160, y - 900) < rig.pin * 1.5 for x, y in rig.pins))

    def test_views_change_the_motion(self):
        side = v2()
        side["view"] = "side"
        front_rig, side_rig = desktop.IdleRig(standing()), desktop.IdleRig(standing(joints=side))
        self.assertAlmostEqual(side_rig.sway, front_rig.sway * .6)
        self.assertNotEqual(side_rig.lift, front_rig.lift)  # breathing also goes forward
        lying = desktop.template_joints({"box": [0, 400, 1000, 700]}, "lie")
        lying_pose = {"file": "l.png", "box": [0, 400, 1000, 700], "w": 1000, "h": 1000, "joints": lying,
                      "support": {"mode": "ground", "cut": .55, "profile": [[x, 700] for x in range(0, 1000, 50)]}}
        rig = desktop.IdleRig(lying_pose)
        self.assertEqual(rig.sway, 0.0)
        self.assertEqual(rig.lift, (0.0, -1.0))

    def test_templates_and_estimate(self):
        pose = standing(joints=None)
        pose["joints"] = None
        for kind in ("front", "side", "back", "sit", "lie"):
            skeleton = desktop.template_joints(pose, kind)
            self.assertTrue(poses.valid_joints(skeleton, 400, 1000), kind)
        front, back = desktop.template_joints(pose, "front"), desktop.template_joints(pose, "back")
        self.assertGreater(front["points"]["shoulder_l"]["xy"][0], front["points"]["shoulder_r"]["xy"][0])
        self.assertLess(back["points"]["shoulder_l"]["xy"][0], back["points"]["shoulder_r"]["xy"][0])
        self.assertEqual(desktop.estimate_joints(pose)["view"], "front")
        self.assertTrue(desktop.template_joints(pose, "sit")["points"]["pelvis"]["support"])
        self.assertGreater(motion(desktop.IdleRig(pose), 200, 160), 0)


class AnimatedSpriteTests(unittest.TestCase):
    def test_animated_frames_move_the_figure_and_keep_the_feet(self):
        stage = desktop.Stage(ROOT / "web" / "desktop", 500)
        image = Image.new("RGBA", (400, 1000), (0, 0, 0, 0))
        ImageDraw.Draw(image).rectangle((150, 100, 250, 900), fill=(220, 220, 220, 255))
        prepared = stage.prepare(standing(), image)
        still, _ = stage.sprites(prepared)
        first, shadow = stage.sprites(prepared, .5)
        later, _ = stage.sprites(prepared, 2.0)
        self.assertGreater(first.image.width, still.image.width)  # padded for the motion
        self.assertNotEqual(first.image.tobytes(), later.image.tobytes())
        bottom = (0, first.image.height - 30, first.image.width, first.image.height - 22)
        self.assertEqual(first.image.crop(bottom).tobytes(), later.image.crop(bottom).tobytes())
        self.assertEqual(stage.frame([(first, shadow, 1)]).size, stage.size)

    def test_sprites_over_the_edge_are_clipped(self):
        stage = desktop.Stage(ROOT / "web" / "desktop", 300)
        sprite = desktop.Sprite(Image.new("RGBA", (50, 50), (255, 0, 0, 255)), -20, -10)
        frame = stage.frame([(sprite, desktop.Sprite(Image.new("RGBA", (1, 1)), 0, 0), 1)])
        self.assertEqual(frame.getpixel((0, 0)), (255, 0, 0, 255))
        self.assertEqual(frame.getpixel((29, 39)), (255, 0, 0, 255))
        self.assertNotEqual(frame.getpixel((31, 41))[:3], (255, 0, 0))


if __name__ == "__main__":
    unittest.main()
