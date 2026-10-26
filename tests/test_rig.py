"""Posing (docs/RIG.md): skeleton, FK / IK, limits, contacts, deformation, layers, weights,
drawn surfaces, the commands' undo and locks, migration of version 1 documents.

Run with: python -m unittest discover -s tests -v
"""
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from PIL import Image, ImageChops, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import figure_doc as fd  # noqa: E402
import rig  # noqa: E402

V = {pid: p["value"] for pid, p in fd.PARTS.items()}
JOINTS = {"head": [100, 30], "neck": [100, 52], "chest": [100, 70], "navel": [100, 110], "pelvis": [100, 145],
          "shoulder_r": [60, 62], "shoulder_l": [140, 62], "elbow_r": [58, 100], "elbow_l": [142, 100],
          "wrist_r": [58, 135], "wrist_l": [142, 135], "hip_r": [88, 150], "hip_l": [112, 150],
          "knee_r": [88, 215], "knee_l": [112, 215], "foot_r": [88, 280], "foot_l": [112, 280]}
ARM = (200, 40, 40, 255)
BODY = (225, 225, 225, 255)


def picture(path):
    """A figure whose arms are red (to see them over the body) and its label map."""
    image = Image.new("RGBA", (200, 300), (0, 0, 0, 0))
    labels = Image.new("L", (200, 300), 0)
    d, l = ImageDraw.Draw(image), ImageDraw.Draw(labels)
    shapes = [("ellipse", (80, 10, 120, 50), BODY, "head"), ("rect", (92, 50, 108, 56), BODY, "neck"),
              ("rect", (75, 56, 125, 95), BODY, "chest"), ("rect", (75, 95, 125, 125), BODY, "abdomen"),
              ("rect", (75, 125, 125, 152), BODY, "pelvis"),
              ("rect", (48, 60, 70, 100), ARM, "right_upper_arm"), ("rect", (48, 100, 70, 140), ARM, "right_forearm"),
              ("rect", (130, 60, 152, 100), ARM, "left_upper_arm"), ("rect", (130, 100, 152, 140), ARM, "left_forearm"),
              ("rect", (79, 152, 98, 215), BODY, "right_thigh"), ("rect", (79, 215, 98, 285), BODY, "right_shin"),
              ("rect", (102, 152, 121, 215), BODY, "left_thigh"), ("rect", (102, 215, 121, 285), BODY, "left_shin")]
    for kind, box, colour, part in shapes:
        (d.ellipse if kind == "ellipse" else d.rectangle)(box, fill=colour)
        (l.ellipse if kind == "ellipse" else l.rectangle)(box, fill=V[part])
    image.save(path)
    return labels


def make_project(root: Path):
    labels = picture(root / "figure.png")
    project = fd.Project.create(root / "figure.png", root / "project")
    parts = {pid: {"state": "visible" if labels.histogram()[p["value"]] else "absent", "source": {"kind": "estimated"},
                   "diagnostics": []} for pid, p in fd.PARTS.items()}
    joints = {j: {"xy": xy, "state": "visible", "score": None, "source": {"kind": "model"}} for j, xy in JOINTS.items()}
    project.execute("store_analysis", {"result": {"labels": labels, "uncertain": Image.new("L", labels.size, 0),
                                                  "parts": parts, "joints": joints, "analysis": {}}}, source="model")
    return project


class SkeletonTests(unittest.TestCase):
    def setUp(self):
        self.joints = {j: {"xy": xy, "state": "visible"} for j, xy in JOINTS.items()}
        self.sk = rig.Skeleton(self.joints)

    def test_bones_from_joints(self):
        self.assertIn("left_forearm", self.sk.bones)
        self.assertEqual(self.sk.bones["left_forearm"]["head"], (142, 100))
        self.assertIn("left_hand", self.sk.bones)  # no hand landmarks: points along the forearm
        self.assertNotIn("left_hand.index.1", self.sk.bones)
        self.assertEqual(self.sk.bones["left_upper_arm"]["parent"], "left_clavicle")
        self.assertEqual(self.sk.derived, [])
        library = {k: v for k, v in self.joints.items() if k != "neck"}  # skeleton v2 has no neck
        self.assertEqual(rig.Skeleton(library).derived, ["neck"])

    def test_fk_turns_clockwise_about_the_joint(self):
        world = self.sk.fk({"left_forearm": 90})
        posed = self.sk.posed_joints(world)
        self.assertAlmostEqual(posed["elbow_l"][0], 142)
        self.assertAlmostEqual(posed["elbow_l"][1], 100)
        # the forearm pointed down (+y); 90° clockwise on the picture (y down) points it to −x
        self.assertAlmostEqual(posed["wrist_l"][0], 142 - 35, places=6)
        self.assertAlmostEqual(posed["wrist_l"][1], 100, places=6)
        world = self.sk.fk({"left_upper_arm": 90})  # the child follows its parent
        posed = self.sk.posed_joints(world)
        self.assertAlmostEqual(math.dist(posed["elbow_l"], posed["wrist_l"]), 35, places=6)
        world = self.sk.fk({}, root=(10, -5))
        self.assertAlmostEqual(self.sk.posed_joints(world)["head"][0], 110)

    def test_ik_reaches_and_reports(self):
        target = (170.0, 110.0)
        rot, world, report = self.sk.solve({"ik": {"left_arm": list(target)}})
        self.assertLess(math.dist(self.sk.posed_joints(world)["wrist_l"], target), .5)
        self.assertLess(report["ik"]["left_arm"]["miss_px"], .5)
        _, _, report = self.sk.solve({"ik": {"left_arm": [400, 100]}})  # out of reach
        self.assertGreater(report["ik"]["left_arm"]["miss_px"], 100)
        for bend in (1, -1):
            sk = rig.Skeleton(self.joints, {"left_upper_arm": {"bend": bend}})
            _, world, _ = sk.solve({"ik": {"left_arm": [142, 150]}})
            elbow = sk.posed_joints(world)["elbow_l"]
            cross = (150 - 62) * (elbow[0] - 140) - (142 - 140) * (elbow[1] - 62)
            self.assertEqual(math.copysign(1, cross), -bend if bend else 1)

    def test_limits_and_contacts(self):
        sk = rig.Skeleton(self.joints, {"left_forearm": {"min": -10, "max": 30}})
        self.assertEqual(sk.limit("left_forearm", 80), 30)
        self.assertEqual(sk.limit("left_forearm", -80), -10)
        feet = [{"id": f"foot_{s}", "joint": f"foot_{s}", "point": JOINTS[f"foot_{s}"]} for s in "lr"]
        rot, world, report = self.sk.solve({"root": [0, 25]}, feet)
        posed = self.sk.posed_joints(world)
        for s in "lr":
            self.assertLess(math.dist(posed[f"foot_{s}"], JOINTS[f"foot_{s}"]), .5)
            self.assertNotAlmostEqual(posed[f"knee_{s}"][0], JOINTS[f"knee_{s}"][0], places=1)  # the knees bend
        self.assertAlmostEqual(posed["pelvis"][1], 170)
        # nearly straight legs bend outwards (away from the body)
        self.assertLess(posed["knee_r"][0], JOINTS["knee_r"][0])
        self.assertGreater(posed["knee_l"][0], JOINTS["knee_l"][0])


class DeformationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = make_project(Path(self.tmp.name))
        import rig_doc
        self.rig_doc = rig_doc

    def tearDown(self):
        self.tmp.cleanup()

    def built(self):
        return self.rig_doc.rig_of(self.project)

    def test_rest_pose_is_the_picture(self):
        built = self.built()
        _, world, _ = built.solve({})
        out = built.render(world)
        source = self.project.source()
        alpha_diff = ImageChops.difference(out.getchannel("A"), source.getchannel("A")).point(lambda v: 255 if v > 40 else 0)
        self.assertEqual(alpha_diff.getbbox(), None)
        rgb = ImageChops.difference(out.convert("RGB"), source.convert("RGB")).convert("L")
        opaque = source.getchannel("A")
        self.assertEqual(ImageChops.multiply(rgb.point(lambda v: 255 if v > 30 else 0), opaque).getbbox(), None)

    def test_rotation_moves_the_part_and_keeps_the_body(self):
        built = self.built()
        _, world, _ = built.solve({"rotations": {"left_forearm": -90}})  # forearm swings out to +x
        out = built.render(world)
        self.assertEqual(out.getpixel((141, 130))[3], 0)  # the forearm left its place
        self.assertEqual(out.getpixel((170, 110))[:3], ARM[:3])  # and is out to the side now
        self.assertEqual(out.getpixel((100, 110)), BODY)  # the abdomen did not move
        self.assertEqual(out.getpixel((140, 80))[:3], ARM[:3])  # the upper arm did not move

    def test_layer_order_decides_what_is_in_front(self):
        built = self.built()
        pose = {"rotations": {"left_upper_arm": 50}}  # the arm swings in over the chest
        _, world, _ = built.solve(pose)
        posed = built.skeleton.posed_joints(world)
        probe = tuple(round(140 + .85 * (e - s)) if i == 0 else round(62 + .85 * (e - s))
                      for i, (e, s) in enumerate(zip(posed["elbow_l"], (140, 62))))  # over the chest
        self.assertEqual(built.render(world).getpixel(probe)[:3], ARM[:3])  # arms are in front by default
        self.project.execute("set_layer_order", {"part_ids": ["left_upper_arm", "left_forearm"], "position": "back"})
        built = self.built()
        _, world, _ = built.solve(pose)
        self.assertEqual(built.render(world).getpixel(probe)[:3], BODY[:3])

    def test_weights_blend_at_joints_only_between_neighbours(self):
        mesh = self.built().mesh
        g = mesh.cell
        near_elbow = min(range(len(mesh.vertices)), key=lambda v: math.dist(mesh.vertices[v], (141, 100)))
        self.assertGreater(len(mesh.weights[near_elbow]), 1)  # upper arm and forearm blend there
        inside = min(range(len(mesh.vertices)), key=lambda v: math.dist(mesh.vertices[v], (141, 128)))
        self.assertEqual(set(mesh.weights[inside]), {"left_forearm"})
        self.assertTrue(mesh.adjacent(V["left_upper_arm"], V["left_forearm"]))
        self.assertFalse(mesh.adjacent(V["left_forearm"], V["chest"]))
        self.assertGreaterEqual(g, 4)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = make_project(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pose_commands_undo_and_revision(self):
        p = self.project
        rev = p.doc["revision"]
        reply = p.execute("set_joint_rotation", {"joint_id": "elbow_l", "angle": -45}, expected_revision=rev)
        self.assertEqual((reply["bone"], reply["angle"]), ("left_forearm", -45.0))
        p.execute("set_joint_rotation", {"bone_id": "left_forearm", "angle": -10, "relative": True})
        self.assertEqual(p.instance()["rig"]["pose"]["rotations"]["left_forearm"], -55.0)
        reply = p.execute("set_ik_target", {"chain_id": "right_arm", "position": [30, 120]})
        self.assertLess(reply["miss_px"], .5)
        posed = p.execute("get_pose")
        self.assertLess(math.dist(posed["joints"]["wrist_r"], (30, 120)), .5)
        p.execute("undo")
        p.execute("undo")
        self.assertEqual(p.instance()["rig"]["pose"]["rotations"]["left_forearm"], -45.0)
        self.assertEqual(p.instance()["rig"]["pose"]["ik"], {})
        p.execute("redo")
        with self.assertRaises(fd.CommandError) as caught:
            p.execute("set_root", {"offset": [5, 5]}, expected_revision=rev)
        self.assertEqual(caught.exception.code, "conflict")
        with self.assertRaises(fd.CommandError):
            p.execute("set_joint_rotation", {"joint_id": "nose", "angle": 3})
        with self.assertRaises(fd.CommandError):
            p.execute("set_joint_rotation", {"bone_id": "left_hand.index.1", "angle": 3})  # no hand landmarks
        reply = p.execute("set_joint_rotation", {"joint_id": "elbow_l", "angle": 3, "time": 1.0})  # a key, not the pose
        self.assertEqual((reply["track"], reply["time"]), ("rot:left_forearm", 1.0))
        self.assertEqual(p.instance()["rig"]["pose"]["rotations"]["left_forearm"], -55.0)  # after the redo
        p.execute("reset_pose")
        self.assertEqual(p.instance()["rig"]["pose"], {"root": [0.0, 0.0], "rotations": {}, "ik": {}})

    def test_pose_lock_against_agent(self):
        p = self.project
        p.execute("set_pose_locked", {"locked": True})
        with self.assertRaises(fd.CommandError) as caught:
            p.execute("set_joint_rotation", {"joint_id": "elbow_l", "angle": 10}, source="ai")
        self.assertEqual(caught.exception.code, "locked")
        with self.assertRaises(fd.CommandError):
            p.execute("set_pose_locked", {"locked": False}, source="ai")
        p.execute("set_joint_rotation", {"joint_id": "elbow_l", "angle": 10, "override_lock": True}, source="ai")
        p.execute("set_joint_rotation", {"joint_id": "elbow_l", "angle": 20})  # the user may

    def test_constraints_contacts_and_validation(self):
        p = self.project
        p.execute("set_constraint", {"bone_id": "left_forearm", "min": -20, "max": 20})
        reply = p.execute("set_joint_rotation", {"joint_id": "elbow_l", "angle": 80})
        self.assertEqual(reply["limited_to"], 20)
        self.assertEqual(p.execute("get_pose")["rotations"]["left_forearm"], 20)
        p.execute("set_contact", {"entity_id": "foot_l"})
        p.execute("set_contact", {"entity_id": "foot_r"})
        p.execute("set_root", {"offset": [0, 20]})
        posed = p.execute("get_pose")
        self.assertLess(math.dist(posed["joints"]["foot_l"], JOINTS["foot_l"]), .5)
        p.execute("set_ik_target", {"chain_id": "left_arm", "position": [380, 100]})
        codes = {i["code"] for i in p.execute("validate_rig")["issues"]}
        self.assertIn("rotation_limited", codes)
        self.assertIn("ik_unreached", codes)
        self.assertIn("fingers_not_rigged", codes)
        p.execute("set_contact", {"entity_id": "foot_l", "options": {"remove": True}})
        self.assertEqual([c["joint"] for c in p.instance()["rig"]["contacts"]], ["foot_r"])
        with self.assertRaises(fd.CommandError):
            p.execute("set_contact", {"entity_id": "navel"})

    def test_weights_edit_and_undo(self):
        p = self.project
        name = "fig1_w_left_forearm"
        p.execute("edit_weights", {"bone_id": "left_forearm", "strokes": [{"points": [[140, 80]], "radius": 10}], "amount": 1})
        self.assertTrue((self.root / "project" / "masks" / f"{name}.png").exists())
        import rig_doc
        mesh = rig_doc.rig_of(p).mesh
        v = min(range(len(mesh.vertices)), key=lambda k: math.dist(mesh.vertices[k], (140, 80)))
        self.assertGreater(mesh.weights[v].get("left_forearm", 0), .3)  # the upper arm now follows the forearm too
        p.execute("undo")
        self.assertFalse((self.root / "project" / "masks" / f"{name}.png").exists())
        self.assertEqual(p.instance()["rig"]["weights"], [])
        p.execute("redo")
        p.execute("edit_weights", {"bone_id": "left_forearm", "strokes": [{"points": [[140, 80]], "radius": 30}], "operation": "reset"})
        self.assertEqual(p.instance()["rig"]["weights"], [])  # nothing left: the map is dropped
        with self.assertRaises(fd.CommandError):
            p.execute("edit_weights", {"bone_id": "tail", "strokes": [{"points": [[1, 1]]}]})

    def test_surfaces_are_separate_layers(self):
        p = self.project
        p.execute("set_joint_rotation", {"joint_id": "shoulder_l", "angle": -100})  # the arm leaves the body's side
        reply = p.execute("add_surface", {"part_id": "chest", "name": "кожа"})
        sid = reply["surface"]["id"]
        self.assertFalse(reply["surface"]["observed"])
        p.execute("paint_surface", {"surface_id": sid, "strokes": [{"points": [[110, 5]], "radius": 4}], "color": [10, 200, 10]})
        image, _ = __import__("rig_doc").render_pose(p, overlays=("pose",), background=(0, 0, 0, 0), max_side=300)
        self.assertEqual(image.getpixel((110, 5))[:3], (10, 200, 10))
        p.execute("set_surface", {"surface_id": sid, "enabled": False})
        image, _ = __import__("rig_doc").render_pose(p, overlays=("pose",), background=(0, 0, 0, 0), max_side=300)
        self.assertEqual(image.getpixel((110, 5))[3], 0)
        p.execute("set_surface", {"surface_id": sid, "remove": True})
        self.assertFalse((self.root / "project" / "masks" / f"fig1_s_{sid}.png").exists())
        p.execute("undo")
        self.assertTrue((self.root / "project" / "masks" / f"fig1_s_{sid}.png").exists())
        with self.assertRaises(fd.CommandError):  # the agent cannot pass a file
            p.execute("add_surface", {"part_id": "chest", "image": "C:/Windows/win.ini"}, source="ai")

    def test_preview_and_reads(self):
        p = self.project
        info = p.execute("get_rig")
        self.assertIn("left_arm", info["chains"])
        self.assertEqual(info["rotate_at"]["elbow_l"], "left_forearm")
        self.assertIn("degrees", info["units"]["angle"])
        p.execute("set_joint_rotation", {"joint_id": "elbow_l", "angle": -60})
        preview = p.execute("render_preview", {"overlays": ["pose", "skeleton", "holes"], "max_side": 150})
        self.assertEqual(preview["size"], [100, 150])
        self.assertEqual(Path(preview["file"]).parent, self.root / "project" / "previews")
        self.assertEqual(preview["transform"]["scale"], .5)
        mesh = p.execute("pose_mesh")
        evaluated = p.execute("pose_eval")
        for part in mesh["parts"]:
            self.assertEqual(len(evaluated["parts"][str(part["value"])]), 2 * len(part["verts"]))
        with self.assertRaises(fd.CommandError):
            p.execute("render_preview", {"overlays": ["x-ray"]})

    def test_version_1_documents_are_migrated(self):
        doc = json.loads((self.root / "project" / "figure.json").read_text(encoding="utf-8"))
        doc["version"] = 1
        for inst in doc["instances"]:
            del inst["rig"]
        (self.root / "project" / "figure.json").write_text(json.dumps(doc), encoding="utf-8")
        project = fd.Project.open(self.root / "project")
        self.assertEqual((project.doc["version"], project.doc["migrated_from"]), (2, 1))
        self.assertEqual(project.instance()["rig"]["layers"], rig.default_layers())
        project.execute("set_joint_rotation", {"joint_id": "elbow_l", "angle": 5})
        self.assertEqual(json.loads((self.root / "project" / "figure.json").read_text(encoding="utf-8"))["version"], 2)


class AgentToolsTests(unittest.TestCase):
    def test_mcp_posing_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_project(root)
            env = {**os.environ, "APPDATA": str(root / "appdata"), "POSESTAGE_PROJECTS": str(root / "project")}
            messages = [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "set_joint_rotation",
                                                                              "arguments": {"joint_id": "elbow_r", "angle": 40}}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "render_preview",
                                                                              "arguments": {"max_side": 120}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "validate_rig", "arguments": {}}},
            ]
            run = subprocess.run([sys.executable, str(ROOT / "tools" / "posestage_mcp.py")], input="".join(json.dumps(m) + "\n" for m in messages).encode(),
                                 capture_output=True, env=env, timeout=60)
            replies = {r["id"]: r for r in map(json.loads, run.stdout.decode().splitlines())}
            names = {t["name"] for t in replies[1]["result"]["tools"]}
            self.assertTrue({"get_rig", "set_joint_rotation", "set_ik_target", "set_contact", "set_layer_order", "set_binding",
                             "edit_weights", "render_preview", "validate_rig", "add_surface"} <= names)
            self.assertFalse(replies[2]["result"]["isError"])
            self.assertEqual(replies[3]["result"]["content"][0]["type"], "image")
            self.assertFalse(replies[4]["result"]["isError"])
            project = fd.Project.open(root / "project")
            self.assertEqual(project.instance()["rig"]["pose"]["rotations"]["right_forearm"], 40.0)
            self.assertEqual(project.execute("get_journal")["entries"][-1]["source"], "ai")


if __name__ == "__main__":
    unittest.main()
