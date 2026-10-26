"""Short animation (timeline.py): keys, interpolation, loops, the pose at a time, contacts on
intervals, frame checks and the deterministic PNG export (also as a background job).

Run with: python -m unittest discover -s tests -v
"""
import math
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import figure_doc as fd  # noqa: E402
import parts_service  # noqa: E402
import timeline  # noqa: E402
from test_rig import JOINTS, make_project  # noqa: E402


class InterpolationTests(unittest.TestCase):
    def test_value_at(self):
        keys = [{"t": 0, "value": 0, "interp": "linear"}, {"t": 1, "value": 10, "interp": "ease"}, {"t": 2, "value": 20, "interp": "step"},
                {"t": 3, "value": 0}]
        self.assertEqual(timeline.value_at(keys, -1), 0)
        self.assertAlmostEqual(timeline.value_at(keys, .25), 2.5)  # linear
        self.assertAlmostEqual(timeline.value_at(keys, 1.25), 10 + 10 * (.25 * .25 * 2.5))  # smoothstep
        self.assertEqual(timeline.value_at(keys, 2.9), 20)  # step holds
        self.assertEqual(timeline.value_at(keys, 3), 0)
        self.assertEqual(timeline.value_at(keys, 9), 0)
        self.assertEqual(timeline.value_at([{"t": 0, "value": [0, 0], "interp": "linear"}, {"t": 2, "value": [4, 8]}], 1), [2, 4])

    def test_loop_and_frames(self):
        anim = {"duration": 2.0, "fps": 10, "loop": True, "tracks": {}}
        self.assertAlmostEqual(timeline.local_time(anim, 2.5), .5)
        self.assertEqual(len(timeline.frame_times(anim)), 20)  # a loop does not repeat its first frame
        anim["loop"] = False
        self.assertEqual(len(timeline.frame_times(anim)), 21)
        self.assertEqual(timeline.local_time(anim, 5), 2.0)


class AnimationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = make_project(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_keys_drive_the_pose_at_a_time(self):
        p = self.project
        p.execute("set_animation", {"duration": 1.0, "fps": 4, "loop": False})
        p.execute("set_keyframe", {"track_id": "rot:left_forearm", "time": 0, "value": 0, "interpolation": "linear"})
        p.execute("set_joint_rotation", {"joint_id": "elbow_l", "angle": -90, "time": 1.0})
        p.execute("set_root", {"offset": [0, 0], "time": 0})
        p.execute("set_root", {"offset": [20, 0], "time": 1})
        half = p.execute("get_pose", {"time": .5})
        self.assertAlmostEqual(half["rotations"]["left_forearm"], -45, places=3)
        elbow, wrist = half["joints"]["elbow_l"], half["joints"]["wrist_l"]
        self.assertAlmostEqual(math.degrees(math.atan2(wrist[1] - elbow[1], wrist[0] - elbow[0])), 45 + math.degrees(math.atan2(35, 0)) - 90, places=0)
        self.assertGreater(half["joints"]["pelvis"][0], 100)
        self.assertEqual(p.execute("get_pose")["rotations"], {})  # the static pose is untouched
        anim = p.execute("get_animation")
        self.assertEqual(sorted(anim["tracks"]), ["root", "rot:left_forearm"])
        self.assertEqual(anim["frames"], 5)
        p.execute("delete_keyframe", {"track_id": "rot:left_forearm", "time": 1.0})
        self.assertEqual(len(p.execute("get_animation")["tracks"]["rot:left_forearm"]), 1)
        p.execute("undo")
        self.assertEqual(len(p.execute("get_animation")["tracks"]["rot:left_forearm"]), 2)
        with self.assertRaises(fd.CommandError):
            p.execute("set_keyframe", {"track_id": "rot:tail", "time": 0, "value": 1})
        with self.assertRaises(fd.CommandError):
            p.execute("set_keyframe", {"track_id": "root", "time": -1, "value": [0, 0]})
        with self.assertRaises(fd.CommandError):
            p.execute("delete_keyframe", {"track_id": "rot:left_forearm", "time": .3})

    def test_key_pose_and_ik_tracks(self):
        p = self.project
        p.execute("set_joint_rotation", {"joint_id": "elbow_r", "angle": 30})
        p.execute("set_ik_target", {"chain_id": "left_arm", "position": [170, 110]})
        p.execute("key_pose", {"time": 0})
        p.execute("set_ik_target", {"chain_id": "left_arm", "position": None, "time": 1})  # no target from 1 s
        anim = p.execute("get_animation")
        self.assertIn("rot:right_forearm", anim["tracks"])
        self.assertEqual(anim["tracks"]["ik:left_arm"][-1]["value"], None)
        self.assertLess(math.dist(p.execute("get_pose", {"time": .2})["joints"]["wrist_l"], (170, 110)), .5)
        self.assertNotIn("left_arm", timeline.pose_at(p.instance()["rig"], 1.5)["ik"])

    def test_contacts_on_an_interval(self):
        p = self.project
        p.execute("set_animation", {"duration": 2.0, "fps": 4})
        p.execute("set_contact", {"entity_id": "foot_l", "interval": [0, 1]})
        p.execute("set_root", {"offset": [0, 0], "time": 0})
        p.execute("set_root", {"offset": [0, 30], "time": 2})
        at = lambda t: p.execute("get_pose", {"time": t})["joints"]["foot_l"]  # noqa: E731
        self.assertLess(math.dist(at(.9), JOINTS["foot_l"]), .5)  # held
        self.assertGreater(at(1.9)[1], JOINTS["foot_l"][1] + 20)  # released: moves with the body

    def test_check_and_export_are_deterministic(self):
        p = self.project
        p.execute("set_animation", {"duration": .5, "fps": 6, "loop": True})
        p.execute("set_joint_rotation", {"joint_id": "elbow_l", "angle": 0, "time": 0})
        p.execute("set_joint_rotation", {"joint_id": "elbow_l", "angle": -80, "time": .25})
        p.execute("set_ik_target", {"chain_id": "right_arm", "position": [380, 0], "time": .4})
        check = p.execute("check_animation")
        self.assertEqual(check["checked"], 3)
        self.assertTrue(any("ik right_arm" in x for f in check["frames_with_problems"] for x in f["problems"]))
        a = timeline.export_frames(p, self.root / "a", max_side=100)
        b = timeline.export_frames(p, self.root / "b", max_side=100)
        self.assertEqual(a["frames"], 3)
        for k in range(3):
            self.assertEqual((self.root / "a" / f"frame_{k:04d}.png").read_bytes(), (self.root / "b" / f"frame_{k:04d}.png").read_bytes())
        self.assertNotEqual((self.root / "a" / "frame_0000.png").read_bytes(), (self.root / "a" / "frame_0001.png").read_bytes())
        manifest = (self.root / "a" / "frames.json").read_text(encoding="utf-8")
        self.assertIn('"fps": 6', manifest)
        # the frame of the export equals the preview at the same time (one evaluator)
        import rig_doc
        from PIL import Image, ImageChops
        image, _ = rig_doc.render_pose(p, time=1 / 6, overlays=("pose",), background=(0, 0, 0, 0), max_side=100)
        exported = Image.open(self.root / "a" / "frame_0001.png")
        self.assertIsNone(ImageChops.difference(image, exported).getbbox())

    def test_export_job_with_progress_and_cancel(self):
        with patch.object(parts_service, "DATA_DIR", self.root / "appdata"):
            service = parts_service.PartsService(figures_dir=self.root / "figures")
            service._register(self.project)
            self.project.execute("set_animation", {"duration": 1.0, "fps": 8})
            job = service.export_frames(options={"max_side": 80})
            for _ in range(200):
                if service.job(job["job_id"])["status"] not in ("queued", "running"):
                    break
                time.sleep(.05)
            done = service.job(job["job_id"])
            self.assertEqual(done["status"], "done", done)
            self.assertEqual(done["result"]["frames"], 8)
            self.assertTrue(Path(done["result"]["folder"]).is_relative_to(self.project.folder / "exports"))
            with self.assertRaises(fd.CommandError):
                service.export_frames(options={"path": "C:/"})
            self.project.execute("set_animation", {"duration": 30.0, "fps": 30})
            job = service.export_frames(options={"max_side": 400})
            service.cancel(job["job_id"])
            for _ in range(200):
                if service.job(job["job_id"])["status"] not in ("queued", "running"):
                    break
                time.sleep(.05)
            self.assertEqual(service.job(job["job_id"])["status"], "cancelled")
            service.close_all()


if __name__ == "__main__":
    unittest.main()
