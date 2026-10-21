"""Pose animation: bundled joints, the user's skeleton corrections, the idle rig and its sprites.

Run with: python -m unittest discover -s tests -v
"""
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

import app
import desktop
import poses

ROOT = Path(__file__).resolve().parent.parent
SKELETON = {"nose": [200, 130], "shoulders": [[240, 220], [160, 220]], "elbows": [[300, 300], [100, 300]],
            "wrists": [[360, 260], [40, 260]], "hips": [[230, 470], [170, 470]]}


def standing(file="a.png", joints=SKELETON):
    profile = [[x, 900] for x in range(150, 260, 10)]
    return {"file": file, "box": [30, 100, 370, 900], "w": 400, "h": 1000, "joints": joints,
            "support": {"mode": "feet", "cut": .93, "profile": profile}}


class JointsTests(unittest.TestCase):
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

    def test_bundled_joints_are_valid_and_cover_the_set(self):
        bundled = poses.bundled_joints()
        self.assertGreater(len(bundled), 150)
        self.assertTrue(all(poses.valid_joints(j) for j in bundled.values()))

    def test_priority_user_over_bundled_over_none(self):
        by_file = {p["file"]: p for p in self.catalog().get()}
        self.assertEqual(by_file["photo-000001__pose-01.png"]["joints_source"], "bundled")
        self.assertIsNone(by_file["own.png"]["joints"])
        api = app.Api(self.catalog())
        pose = api.set_joints("own.png", SKELETON)
        self.assertEqual((pose["joints_source"], pose["joints"]["nose"]), ("user", [200.0, 130.0]))
        self.assertEqual(api.set_joints("photo-000001__pose-01.png", SKELETON)["joints_source"], "user")
        by_file = {p["file"]: p for p in self.catalog().get()}  # persisted across scans
        self.assertEqual(by_file["photo-000001__pose-01.png"]["joints_source"], "user")
        self.assertEqual(api.set_joints("photo-000001__pose-01.png", None)["joints_source"], "bundled")
        self.assertEqual(api.set_joints("own.png")["joints"], None)  # reset: no skeleton left

    def test_invalid_skeletons_are_rejected_or_ignored(self):
        api = app.Api(self.catalog())
        for bad in ({}, {**SKELETON, "nose": [1]}, {**SKELETON, "hips": [[0, 0]]},
                    {**SKELETON, "wrists": [[float("nan"), 0], [0, 0]]}, {**SKELETON, "nose": [1e9, 0]}, "x"):
            with self.assertRaises(ValueError):
                api.set_joints("own.png", bad)
        with self.assertRaises(ValueError):
            api.set_joints("missing.png", SKELETON)
        cid = poses.collection_id(self.images.resolve())
        poses.write_json(poses.DATA_DIR / "joint_overrides.json", {cid: {"own.png": {"nose": "bad"}}})
        self.assertIsNone(next(p for p in self.catalog().get() if p["file"] == "own.png")["joints"])

    def test_mode_and_size_changes_keep_the_skeleton(self):
        catalog = self.catalog()
        catalog.set_joints("own.png", SKELETON)
        self.assertEqual(catalog.set_mode("own.png", "kneel")["joints_source"], "user")
        self.assertEqual(catalog.set_scale("own.png", 1.1)["joints_source"], "user")


class IdleRigTests(unittest.TestCase):
    def test_loop_is_seamless_and_supports_stay(self):
        rig = desktop.IdleRig(standing())
        for x, y in ((200, 150), (300, 300), (200, 400)):
            for t in (0, .4, 2.2):
                a, b = rig.displace(x, y, t), rig.displace(x, y, t + desktop.IDLE_LOOP)
                self.assertAlmostEqual(a[0], b[0], places=9)
                self.assertAlmostEqual(a[1], b[1], places=9)
        for x in (160, 200, 250):  # the feet on the podium never move
            for t in (.3, 1.7, 4.1):
                self.assertEqual(rig.displace(x, 900, t), (0.0, 0.0))
        head = max(math.hypot(*rig.displace(200, 130, t / 10)) for t in range(60))
        hips = max(math.hypot(*rig.displace(200, 480, t / 10)) for t in range(60))
        self.assertGreater(head, 3)
        self.assertGreater(head, hips * 2)

    def test_free_hands_swing_only_when_away_from_the_body(self):
        self.assertEqual(len(desktop.IdleRig(standing()).hands), 2)
        tucked = {**SKELETON, "wrists": [[205, 300], [195, 300]], "elbows": [[220, 280], [180, 280]]}
        self.assertEqual(desktop.IdleRig(standing(joints=tucked)).hands, [])

    def test_estimated_skeleton_without_joints(self):
        pose = standing(joints=None)
        joints = desktop.estimate_joints(pose)
        self.assertLess(joints["nose"][1], joints["shoulders"][0][1])
        self.assertLess(joints["shoulders"][0][1], joints["hips"][0][1])
        self.assertGreater(math.hypot(*desktop.IdleRig(pose).displace(200, 150, 1.2)), 0)


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
        # The lowest rows (feet) are identical at any time: supports are pinned.
        bottom = (0, first.image.height - 30, first.image.width, first.image.height - 22)
        self.assertEqual(first.image.crop(bottom).tobytes(), later.image.crop(bottom).tobytes())
        frame = stage.frame([(first, shadow, 1)])
        self.assertEqual(frame.size, stage.size)

    def test_sprites_over_the_edge_are_clipped(self):
        stage = desktop.Stage(ROOT / "web" / "desktop", 300)
        sprite = desktop.Sprite(Image.new("RGBA", (50, 50), (255, 0, 0, 255)), -20, -10)
        frame = stage.frame([(sprite, desktop.Sprite(Image.new("RGBA", (1, 1)), 0, 0), 1)])
        self.assertEqual(frame.getpixel((0, 0)), (255, 0, 0, 255))
        self.assertEqual(frame.getpixel((29, 39)), (255, 0, 0, 255))  # the sprite's last visible pixel
        self.assertNotEqual(frame.getpixel((31, 41))[:3], (255, 0, 0))


if __name__ == "__main__":
    unittest.main()
