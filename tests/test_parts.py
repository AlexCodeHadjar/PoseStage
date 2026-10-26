"""Figure documents, commands, analysis jobs, agent bridge and MCP adapter (docs/PARTS.md).

Run with: python -m unittest discover -s tests -v
The analyzer test needs the analysis runtime: set POSESTAGE_ANALYZER to its python.exe.
"""
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import agent_bridge  # noqa: E402
import figure_doc as fd  # noqa: E402
import parts_service  # noqa: E402

V = {pid: p["value"] for pid, p in fd.PARTS.items()}


def figure_picture(path: Path):
    """A transparent drawing of a simple figure with dark outlines (regions closed by lines)."""
    image = Image.new("RGBA", (200, 300), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    fill, line = (225, 225, 225, 255), (30, 30, 30, 255)
    draw.ellipse((80, 10, 120, 50), fill=fill, outline=line, width=2)  # head
    draw.rectangle((75, 55, 125, 150), fill=fill, outline=line, width=2)  # torso
    draw.rectangle((45, 60, 72, 140), fill=fill, outline=line, width=2)  # left arm (picture left)
    draw.rectangle((128, 60, 155, 140), fill=fill, outline=line, width=2)
    draw.rectangle((78, 153, 98, 285), fill=fill, outline=line, width=2)  # legs
    draw.rectangle((102, 153, 122, 285), fill=fill, outline=line, width=2)
    image.save(path)
    return path


JOINTS = {"head": [100, 30], "neck": [100, 52], "chest": [100, 70], "navel": [100, 110], "pelvis": [100, 145],
          "shoulder_r": [78, 62], "shoulder_l": [122, 62], "elbow_r": [58, 100], "elbow_l": [142, 100],
          "wrist_r": [58, 135], "wrist_l": [142, 135], "hip_r": [88, 150], "hip_l": [112, 150],
          "knee_r": [88, 215], "knee_l": [112, 215], "foot_r": [88, 280], "foot_l": [112, 280]}


def fake_result(project, value=None, state="visible"):
    """An analysis result as parts_service hands it to store_analysis: the whole figure one part."""
    labels = project.labels("fig1").point(lambda v: 0 if v == fd.BACKGROUND else (value or V["chest"]))
    parts = {pid: {"state": "absent", "source": {"kind": "estimated", "method": "test"}, "diagnostics": []} for pid in fd.PARTS}
    parts["chest" if value is None else fd.VALUE_TO_PART[value]]["state"] = state
    joints = {jid: {"xy": xy, "state": "visible", "score": .9, "source": {"kind": "model", "method": "test"}} for jid, xy in JOINTS.items()}
    return {"labels": labels, "uncertain": Image.new("L", labels.size, 0), "parts": parts, "joints": joints,
            "analysis": {"model": {"name": "test"}}}


class FigureDocTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.picture = figure_picture(self.root / "figure.png")
        self.project = fd.Project.create(self.picture, self.root / "project")

    def tearDown(self):
        self.tmp.cleanup()

    def labels(self):
        return self.project.labels("fig1")

    def test_create_document(self):
        doc = self.project.doc
        self.assertEqual((doc["schema"], doc["version"], doc["revision"]), (fd.SCHEMA, fd.VERSION, 0))
        self.assertEqual(doc["source"]["sha256"], fd.sha256_file(self.root / "project" / doc["source"]["file"]))
        self.assertEqual(doc["id"], doc["source"]["sha256"][:16])
        inst = self.project.instance()
        self.assertEqual(inst["roi"], [45, 10, 156, 286])
        self.assertEqual(inst["foreground"]["source"], "alpha")
        histogram = self.labels().histogram()
        self.assertGreater(histogram[fd.UNKNOWN], 10000)  # the figure, not yet split
        self.assertEqual(histogram[fd.BACKGROUND] + histogram[fd.UNKNOWN], 200 * 300)
        self.assertTrue(all(p["state"] == "absent" for p in inst["parts"].values()))
        with self.assertRaises(fd.CommandError):
            fd.Project.create(self.picture, self.root / "project")  # not an empty folder

    def test_stable_ids_and_colours_are_separate(self):
        values = [p["value"] for p in fd.PARTS.values()]
        self.assertEqual(len(values), len(set(values)))
        self.assertNotIn(fd.UNKNOWN, values)
        self.assertEqual(V["head"], 1)
        self.assertEqual(V["left_hand.index"], 14)
        self.assertEqual(len(fd.HAND_POINTS), 21)
        caps = fd.read_capabilities()
        self.assertIn("left_hand.unsplit", caps["parts"])
        self.assertNotIn("set_binding", caps["not_yet"])  # posing exists since stage 2
        self.assertIn("set_binding", caps["commands"])

    def test_edit_mask_undo_redo(self):
        before = self.labels().copy()
        reply = self.project.execute("edit_mask", {"part_id": "head", "strokes": [{"points": [[100, 30]], "radius": 12}]})
        self.assertEqual(reply["revision"], 1)
        self.assertEqual(self.labels().getpixel((100, 30)), V["head"])
        self.assertEqual(self.labels().getpixel((5, 5)), fd.BACKGROUND)  # figure_only: not outside the silhouette
        part = self.project.instance()["parts"]["head"]
        self.assertEqual((part["state"], part["source"]["kind"], part["review"]), ("visible", "user", "edited"))
        self.project.execute("edit_mask", {"part_id": "head", "strokes": [{"points": [[100, 30]], "radius": 4, "mode": "erase"}]})
        self.assertEqual(self.labels().getpixel((100, 30)), fd.UNKNOWN)
        self.project.execute("undo")
        self.assertEqual(self.labels().getpixel((100, 30)), V["head"])
        self.project.execute("undo")
        self.assertEqual(self.labels().tobytes(), before.tobytes())
        self.assertEqual(self.project.instance()["parts"]["head"]["state"], "absent")
        self.project.execute("redo")
        self.assertEqual(self.labels().getpixel((100, 30)), V["head"])
        self.assertEqual(self.project.doc["revision"], 5)  # every step, undo and redo too, is a revision
        with self.assertRaises(fd.CommandError):
            self.project.execute("edit_mask", {"part_id": "tail", "strokes": [{"points": [[1, 1]]}]})
        journal = self.project.execute("get_journal")["entries"]
        self.assertEqual([e["command"] for e in journal][-3:], ["undo", "undo", "redo"])

    def test_background_and_erase_only_own_pixels(self):
        self.project.execute("edit_mask", {"part_id": "chest", "strokes": [{"points": [[100, 100]], "radius": 10}]})
        self.project.execute("edit_mask", {"part_id": "head", "strokes": [{"points": [[100, 100]], "radius": 10, "mode": "erase"}]})
        self.assertEqual(self.labels().getpixel((100, 100)), V["chest"])
        self.project.execute("edit_mask", {"part_id": "chest", "strokes": [{"points": [[100, 100]], "radius": 3, "mode": "background"}]})
        self.assertEqual(self.labels().getpixel((100, 100)), fd.BACKGROUND)

    def test_transaction_is_atomic_and_revision_checked(self):
        with self.assertRaises(fd.CommandError):
            self.project.execute("transaction", {"commands": [
                {"command": "edit_mask", "args": {"part_id": "head", "strokes": [{"points": [[100, 30]], "radius": 8}]}},
                {"command": "set_joint", "args": {"joint_id": "nose_tip", "xy": [1, 1]}}]})
        self.assertEqual(self.project.doc["revision"], 0)
        self.assertEqual(self.labels().getpixel((100, 30)), fd.UNKNOWN)
        with self.assertRaises(fd.CommandError):
            self.project.execute("transaction", {"commands": [{"command": "store_analysis", "args": {}}]})
        reply = self.project.execute("transaction", {"commands": [
            {"command": "edit_mask", "args": {"part_id": "head", "strokes": [{"points": [[100, 30]], "radius": 8}]}},
            {"command": "set_joint", "args": {"joint_id": "head", "xy": [100, 30]}}]}, expected_revision=0)
        self.assertEqual(reply["revision"], 1)
        self.project.execute("undo")
        self.assertNotIn("xy", self.project.instance()["joints"].get("head", {}))
        self.assertEqual(self.labels().getpixel((100, 30)), fd.UNKNOWN)
        with self.assertRaises(fd.CommandError) as caught:
            self.project.execute("set_joint", {"joint_id": "head", "xy": [1, 1]}, expected_revision=0)
        self.assertEqual(caught.exception.code, "conflict")

    def test_locks_against_agent_and_model(self):
        self.project.execute("edit_mask", {"part_id": "head", "strokes": [{"points": [[100, 30]], "radius": 12}]})
        self.project.execute("set_locked", {"entity_id": "head", "locked": True})
        with self.assertRaises(fd.CommandError) as caught:
            self.project.execute("edit_mask", {"part_id": "head", "strokes": [{"points": [[100, 30]], "radius": 3, "mode": "erase"}]},
                                 source="ai")
        self.assertEqual(caught.exception.code, "locked")
        with self.assertRaises(fd.CommandError):  # an agent may not lift the user's lock on its own
            self.project.execute("set_locked", {"entity_id": "head", "locked": False}, source="ai")
        # painting another part never covers a locked one
        self.project.execute("edit_mask", {"part_id": "neck", "strokes": [{"points": [[100, 30]], "radius": 30}]}, source="ai")
        self.assertEqual(self.labels().getpixel((100, 30)), V["head"])
        self.project.execute("edit_mask", {"part_id": "head", "strokes": [{"points": [[100, 30]], "radius": 3, "mode": "erase"}],
                                           "override_lock": True}, source="ai")
        self.assertEqual(self.labels().getpixel((100, 30)), fd.UNKNOWN)
        self.project.execute("set_locked", {"entity_id": "chest", "locked": True}, source="ai")
        self.project.execute("set_locked", {"entity_id": "chest", "locked": False}, source="ai")  # its own lock
        self.assertFalse(self.project.instance()["parts"]["chest"]["locked"])

    def test_analysis_applies_then_proposes_and_keeps_locks(self):
        reply = self.project.execute("store_analysis", {"result": fake_result(self.project)}, source="model")
        self.assertTrue(reply["applied"])
        self.assertEqual(self.labels().getpixel((100, 100)), V["chest"])
        self.assertEqual(self.project.instance()["joints"]["head"]["source"]["kind"], "model")
        # the user corrects the head and locks it; a new analysis is now only a proposal
        self.project.execute("edit_mask", {"part_id": "head", "strokes": [{"points": [[100, 30]], "radius": 12}]})
        self.project.execute("set_locked", {"entity_id": "head", "locked": True})
        self.project.execute("set_joint", {"joint_id": "knee_l", "xy": [115, 220]})
        self.project.execute("set_locked", {"entity_id": "knee_l", "locked": True})
        reply = self.project.execute("store_analysis", {"result": fake_result(self.project, V["abdomen"])}, source="model")
        self.assertFalse(reply["applied"])
        self.assertTrue(self.project.instance()["proposal"])
        self.assertEqual(self.labels().getpixel((100, 100)), V["chest"])  # nothing changed yet
        self.project.execute("apply_proposal")
        self.assertEqual(self.labels().getpixel((100, 100)), V["abdomen"])
        self.assertEqual(self.labels().getpixel((100, 30)), V["head"])  # locked pixels kept
        self.assertEqual(self.project.instance()["joints"]["knee_l"]["xy"], [115.0, 220.0])  # locked joint kept
        self.assertIsNone(self.project.instance()["proposal"])
        self.project.execute("undo")  # back to the proposal, its images still there
        self.assertTrue(self.project.instance()["proposal"])
        self.project.execute("discard_proposal")
        self.assertIsNone(self.project.instance()["proposal"])
        with self.assertRaises(fd.CommandError):
            self.project.execute("apply_proposal")

    def test_apply_proposal_only_some_parts(self):
        self.project.execute("store_analysis", {"result": fake_result(self.project)}, source="model")
        self.project.execute("set_joint", {"joint_id": "head", "xy": [99, 29]})
        self.project.execute("store_analysis", {"result": fake_result(self.project, V["abdomen"])}, source="model")
        self.project.execute("apply_proposal", {"only": ["chest", "abdomen"]})
        self.assertEqual(self.labels().getpixel((100, 100)), V["abdomen"])
        self.assertEqual(self.project.instance()["joints"]["head"]["xy"], [99.0, 29.0])  # not in `only`

    def test_split_relabel_merge_refine(self):
        self.project.execute("store_analysis", {"result": fake_result(self.project)}, source="model")
        reply = self.project.execute("split_part", {"part_id": "chest", "target": "pelvis", "line": [[200, 150], [0, 150]]})
        self.assertGreater(reply["moved_px"], 0)  # left of a line drawn leftwards on the picture = below it
        self.assertEqual(self.labels().getpixel((90, 200)), V["pelvis"])
        self.assertEqual(self.labels().getpixel((100, 100)), V["chest"])
        self.project.execute("relabel", {"part_id": "pelvis", "target": "left_thigh"})
        self.assertEqual(self.labels().getpixel((90, 200)), V["left_thigh"])
        self.assertEqual(self.project.instance()["parts"]["pelvis"]["state"], "hidden")  # lost its pixels
        self.project.execute("merge_parts", {"part_ids": ["left_thigh"], "target": "chest"})
        self.assertEqual(self.labels().getpixel((90, 200)), V["chest"])
        # the head is a region closed by the drawn outline: one click fills exactly it
        reply = self.project.execute("refine_mask", {"part_id": "head", "positive_points": [[100, 30]]})
        self.assertEqual(self.labels().getpixel((100, 20)), V["head"])
        self.assertEqual(self.labels().getpixel((100, 100)), V["chest"])
        self.assertLess(reply["filled_px"], 1500)
        self.project.execute("refine_mask", {"part_id": "head", "positive_points": [], "negative_points": [[100, 30]]})
        self.assertEqual(self.labels().getpixel((100, 20)), fd.UNKNOWN)

    def test_joints_and_visibility(self):
        self.project.execute("set_joint", {"joint_id": "right_hand.index.tip", "xy": [60, 140]})
        joint = self.project.instance()["joints"]["right_hand.index.tip"]
        self.assertEqual((joint["state"], joint["score"], joint["source"]["kind"]), ("visible", None, "user"))
        self.project.execute("set_visibility", {"entity_id": "right_hand.index.tip", "state": "hidden"})
        self.assertEqual(self.project.instance()["joints"]["right_hand.index.tip"]["state"], "hidden")
        with self.assertRaises(fd.CommandError):
            self.project.execute("set_visibility", {"entity_id": "left_hand.index.tip", "state": "visible"})  # no xy yet
        with self.assertRaises(fd.CommandError):
            self.project.execute("set_joint", {"joint_id": "head", "xy": [1e9, 0]})
        with self.assertRaises(fd.CommandError):
            self.project.execute("set_joint", {"joint_id": "head", "xy": ["1", 0]})

    def test_save_reopen_and_move(self):
        self.project.execute("store_analysis", {"result": fake_result(self.project)}, source="model")
        self.project.execute("edit_mask", {"part_id": "head", "strokes": [{"points": [[100, 30]], "radius": 12}]})
        moved = shutil.move(str(self.root / "project"), str(self.root / "elsewhere" / "копия проекта"))
        project = fd.Project.open(Path(moved))
        self.assertEqual(project.doc["revision"], 2)
        self.assertEqual(project.labels("fig1").getpixel((100, 30)), V["head"])
        self.assertEqual(project.instance()["joints"]["head"]["xy"], [100, 30])
        source = Path(moved) / project.doc["source"]["file"]
        source.write_bytes(source.read_bytes() + b"x")
        with self.assertRaises(fd.CommandError) as caught:
            fd.Project.open(Path(moved))
        self.assertEqual(caught.exception.code, "source")

    def test_export_is_consistent(self):
        self.project.execute("store_analysis", {"result": fake_result(self.project)}, source="model")
        self.project.execute("edit_mask", {"part_id": "head", "strokes": [{"points": [[100, 30]], "radius": 12}]})
        out = self.root / "export"
        result = fd.export(self.project, out)
        markup = json.loads((out / "markup.json").read_text(encoding="utf-8"))
        labels = Image.open(out / "labels.png")
        self.assertEqual(labels.mode, "L")
        self.assertEqual(labels.size, Image.open(out / markup["source"]["file"]).size)
        self.assertEqual(labels.tobytes(), self.labels().tobytes())
        self.assertEqual(set(markup["masks"]), {"head", "chest"})
        self.assertEqual(result["masks"], 2)
        for pid, name in markup["masks"].items():
            mask = Image.open(out / name)
            value = markup["label_map"]["values"][pid]
            self.assertEqual(mask.histogram()[255], labels.histogram()[value])
        self.assertEqual(markup["instance"]["joints"]["head"]["xy"], [100, 30])
        self.assertEqual(markup["label_map"]["unknown"], fd.UNKNOWN)

    def test_reads_validate_and_crop(self):
        self.project.execute("store_analysis", {"result": fake_result(self.project)}, source="model")
        self.project.instance()["parts"]["head"]["state"] = "visible"  # claims visible, has no pixels
        codes = {i["code"] for i in self.project.execute("validate")["issues"]}
        self.assertIn("visible_without_pixels", codes)
        crop = self.project.execute("get_crop", {"region": [50, 50, 150, 150], "overlays": ["original", "parts", "skeleton"],
                                                 "max_side": 200})
        self.assertEqual(crop["size"], [200, 200])
        self.assertEqual(crop["transform"], {"scale": 2.0, "offset": [50, 50], "source_size": [200, 300], "region": [50, 50, 150, 150]})
        self.assertTrue(Path(crop["file"]).is_file())
        self.assertEqual(Path(crop["file"]).parent, self.root / "project" / "previews")
        mask = self.project.execute("get_mask", {"part_id": "chest"})
        self.assertEqual(Image.open(mask["file"]).histogram()[255], mask["px"])
        for kind in ("parts", "uncertain", "outline"):
            self.assertEqual(fd.layer(self.project, kind).size, (200, 300))
        with self.assertRaises(fd.CommandError):
            self.project.execute("get_crop", {"region": "all"})


class ServiceAndBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.picture = figure_picture(self.root / "figure.png")
        self.patch = patch.object(parts_service, "DATA_DIR", self.root / "appdata")
        self.patch.start()
        self.env = patch.dict(os.environ, {"POSESTAGE_ANALYZER": ""})
        self.env.start()
        self.service = parts_service.PartsService(figures_dir=self.root / "figures")

    def tearDown(self):
        self.service.close_all()
        self.env.stop()
        self.patch.stop()
        self.tmp.cleanup()

    def test_project_by_image_hash_and_seeded_joints(self):
        first = self.service.create(self.picture, joints={k: {"xy": v} for k, v in JOINTS.items()})
        copy = self.root / "same picture.png"
        shutil.copy(self.picture, copy)
        second = self.service.create(copy)
        self.assertEqual(first["id"], second["id"])
        joint = self.service.project(first["id"]).instance()["joints"]["head"]
        self.assertEqual(joint["source"], {"kind": "estimated", "method": "library skeleton"})
        self.assertEqual(self.service.recent()[0]["id"], first["id"])
        self.assertFalse(first["analyzer"]["available"])
        with self.assertRaises(fd.CommandError) as caught:
            self.service.analyze(first["id"])
        self.assertEqual(caught.exception.code, "unavailable")

    def test_busy_project_lock(self):
        summary = self.service.create(self.picture)
        folder = self.service.project(summary["id"]).folder
        self.service.close(summary["id"])
        (folder / "figure.lock").write_text(str(os.getppid()), encoding="ascii")  # a live other process
        with self.assertRaises(fd.CommandError) as caught:
            self.service.open_folder(folder)
        self.assertEqual(caught.exception.code, "busy")
        (folder / "figure.lock").write_text("999999", encoding="ascii")  # a dead one
        self.assertEqual(self.service.open_folder(folder)["id"], summary["id"])

    def test_analyzer_discovery(self):
        self.assertFalse(parts_service.find_analyzer()["available"])
        config = self.root / "appdata" / "analyzer.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"python": sys.executable}), encoding="utf-8")
        found = parts_service.find_analyzer()
        self.assertTrue(found["available"])
        self.assertEqual(found["command"][:2], [sys.executable, "-I"])
        bundled = self.root / "exe" / "PoseStageAnalyzer"
        bundled.mkdir(parents=True)
        (bundled / "PoseStageAnalyzer.exe").write_bytes(b"")
        config.unlink()
        self.assertEqual(parts_service.find_analyzer(self.root / "exe")["source"], "bundled")

    def test_bridge_http(self):
        summary = self.service.create(self.picture)
        stop = agent_bridge.start(self.service, self.root / "appdata")
        try:
            info = json.loads((self.root / "appdata" / "agent_bridge.json").read_text(encoding="utf-8"))

            def post(body, token=info["token"], headers=None):
                connection = http.client.HTTPConnection("127.0.0.1", info["port"], timeout=10)
                connection.request("POST", "/v1/call", json.dumps(body).encode(),
                                   {"Authorization": f"Bearer {token}", "Content-Type": "application/json", **(headers or {})})
                response = connection.getresponse()
                return response.status, json.loads(response.read())

            self.assertEqual(post({"tool": "list_projects"}, token="wrong")[0], 401)
            self.assertEqual(post({"tool": "list_projects"}, headers={"Origin": "http://evil"})[0], 403)
            status, reply = post({"tool": "list_projects", "args": {}})
            self.assertEqual((status, reply["result"]["projects"][0]["project_id"]), (200, summary["id"]))
            status, reply = post({"tool": "set_joint", "args": {"joint_id": "head", "xy": [100, 30], "expected_revision": 0}})
            self.assertTrue(reply["ok"])
            self.assertEqual(self.service.project().instance()["joints"]["head"]["source"]["kind"], "ai")
            status, reply = post({"tool": "set_joint", "args": {"joint_id": "head", "xy": [100, 30], "expected_revision": 0}})
            self.assertEqual(reply["code"], "conflict")
            self.assertEqual(post({"tool": "store_analysis", "args": {}})[1]["code"], "unknown_command")
            self.assertEqual(post({"tool": "get_part", "args": {"project_id": "0" * 16, "part_id": "head"}})[1]["code"], "not_found")
            status, reply = post({"tool": "export", "args": {}})
            self.assertTrue(Path(reply["result"]["folder"]).is_relative_to(self.service.project().folder / "exports"))
        finally:
            stop()
        self.assertFalse((self.root / "appdata" / "agent_bridge.json").exists())


class AppApiTests(unittest.TestCase):
    def test_editor_api_and_layers(self):
        import threading
        import app
        import poses
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "poses").mkdir()
            figure_picture(root / "poses" / "photo-000001__pose-01.png")
            with patch.object(parts_service, "DATA_DIR", root / "appdata"), patch.object(poses, "DATA_DIR", root / "appdata"),                     patch.dict(os.environ, {"POSESTAGE_ANALYZER": ""}):
                catalog = poses.PoseCatalog(root / "poses", "/t")
                catalog.scan()
                api = app.Api(catalog, fullscreen=False)
                api._parts = parts_service.PartsService(figures_dir=root / "figures")
                summary = api.parts_open_pose("photo-000001__pose-01.png")
                self.assertEqual(summary["name"], "photo-000001__pose-01")
                self.assertEqual(api.parts_status()["open"][0]["project_id"], summary["id"])
                reply = api.parts_call(summary["id"], "edit_mask", {"part_id": "head", "strokes": [{"points": [[100, 30]], "radius": 9}]}, 0)
                self.assertEqual(reply["revision"], 1)
                self.assertEqual(api.parts_call(summary["id"], "set_joint", {"joint_id": "x", "xy": [0, 0]})["code"], "invalid")
                with self.assertRaises(ValueError):
                    api.parts_call(summary["id"], "store_analysis", {})
                self.assertEqual(api.parts_analyze(summary["id"])["code"], "unavailable")
                self.assertIn("left_hand.thumb", api.parts_capabilities()["parts"])
                server = app.LocalServer(catalog, api._parts)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                try:
                    for name, status in ((f"{summary['id']}/fig1/parts.png", 200), (f"{summary['id']}/fig1/source.png", 200),
                                         (f"{summary['id']}/fig9/parts.png", 404), ("0123456789abcdef/fig1/parts.png", 404),
                                         (f"{summary['id']}/fig1/../figure.json", 404)):
                        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                        connection.request("GET", f"/t/figures/{name}")
                        response = connection.getresponse()
                        body = response.read()
                        self.assertEqual(response.status, status, name)
                        if status == 200:
                            self.assertEqual(Image.open(__import__("io").BytesIO(body)).size, (200, 300))
                finally:
                    server.shutdown()
                    server.server_close()
                    api._parts.close_all()


class McpAdapterTests(unittest.TestCase):
    def test_headless_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = fd.Project.create(figure_picture(root / "figure.png"), root / "project")
            env = {**os.environ, "APPDATA": str(root / "appdata"), "POSESTAGE_PROJECTS": str(root / "project")}
            process = subprocess.Popen([sys.executable, str(ROOT / "tools" / "posestage_mcp.py")], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, env=env)
            messages = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                                                              "clientInfo": {"name": "test", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "edit_mask", "arguments": {
                    "part_id": "head", "strokes": [{"points": [[100, 30]], "radius": 10}], "expected_revision": 0}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "get_crop", "arguments": {"max_side": 100}}},
                {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "shell", "arguments": {}}},
                {"jsonrpc": "2.0", "id": 6, "method": "nope"},
            ]
            out, _ = process.communicate("".join(json.dumps(m) + "\n" for m in messages).encode(), timeout=60)
            replies = {r["id"]: r for r in map(json.loads, out.decode().splitlines())}
            self.assertEqual(sorted(replies), [1, 2, 3, 4, 5, 6])  # no reply to the notification
            self.assertEqual(replies[1]["result"]["serverInfo"]["name"], "posestage")
            names = {t["name"] for t in replies[2]["result"]["tools"]}
            self.assertTrue({"get_capabilities", "get_document_summary", "get_parts", "get_joints", "get_part", "get_mask",
                             "get_crop", "validate", "refine_mask", "edit_mask", "split_part", "merge_parts", "set_joint",
                             "set_visibility", "label", "lock", "analyze"} <= names)
            self.assertFalse(replies[3]["result"]["isError"])
            self.assertEqual(replies[4]["result"]["content"][0]["type"], "image")
            self.assertTrue(replies[5]["result"]["isError"])
            self.assertEqual(replies[6]["error"]["code"], -32601)
            reopened = fd.Project.open(root / "project")
            self.assertEqual(reopened.labels("fig1").getpixel((100, 30)), V["head"])
            self.assertEqual(reopened.instance()["parts"]["head"]["source"]["kind"], "ai")
            del project


@unittest.skipUnless(os.environ.get("POSESTAGE_ANALYZER"), "needs the analysis runtime (POSESTAGE_ANALYZER)")
class AnalyzerTests(unittest.TestCase):
    def test_masks_from_joints(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            picture = figure_picture(root / "figure.png")
            joints = {k: {"xy": v, "state": "visible"} for k, v in JOINTS.items()}
            (root / "request.json").write_text(json.dumps({"image": str(picture), "roi": [0, 0, 200, 300], "mode": "masks",
                                                           "joints": joints}), encoding="utf-8")
            run = subprocess.run([os.environ["POSESTAGE_ANALYZER"], "-I", str(ROOT / "parts_analyzer.py"), str(root / "request.json"),
                                  str(root / "out")], capture_output=True, text=True, timeout=120)
            self.assertEqual(run.returncode, 0, run.stderr[-2000:])
            self.assertIn('"progress": 1', run.stdout)
            labels = Image.open(root / "out" / "labels.png")
            result = json.loads((root / "out" / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["joints"], {})  # the document's own joints are not written back
            self.assertEqual(labels.getpixel((100, 30)), V["head"])
            self.assertEqual(labels.getpixel((100, 80)), V["chest"])
            self.assertEqual(labels.getpixel((142, 90)), V["left_upper_arm"])  # the figure's left is the picture's right
            self.assertEqual(labels.getpixel((88, 250)), V["right_shin"])
            self.assertEqual(labels.getpixel((5, 5)), fd.BACKGROUND)
            self.assertEqual(result["parts"]["left_hand.thumb"]["state"], "absent")  # no hand landmarks: no fingers
            self.assertIn("пальцы не разделены", " ".join(result["parts"]["left_hand.unsplit"]["diagnostics"]))


if __name__ == "__main__":
    unittest.main()
