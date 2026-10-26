"""Figure documents: a pose picture broken into named, editable body parts (docs/PARTS.md).

A project is a folder that can be moved anywhere:
    figure.json            the document (schema "posestage.figure", version 1)
    source.<ext>           the picture, never changed (sha256 in the document)
    masks/<inst>_labels.png    label map of the instance: one byte per pixel = one terminal part
    masks/<inst>_uncertain.png pixels whose part is a guess (255) — for the "unsure" overlay
    journal.jsonl          every accepted change: revision, time, source (user/ai/model), command
    previews/, exports/    rendered crops / previews and exports (can be deleted)

Coordinates are pixels of the source picture after its EXIF orientation, origin top-left, y down.
Every change goes through Project.execute(command, args, source=…, expected_revision=…): one
command is one undo step; a list of commands in "transaction" is one step too, applied whole or
not at all. The UI (app.py), the agent bridge and the MCP adapter (tools/posestage_mcp.py) call
the same commands.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import threading
import time

from PIL import Image, ImageChops, ImageDraw, ImageOps

SCHEMA, VERSION = "posestage.figure", 1
BACKGROUND, UNKNOWN = 0, 254  # label values: not the figure / the figure, part not known

# --- Taxonomy -------------------------------------------------------------------------------------
# Terminal parts (one label value each, stable across pictures; the colour is only for display).
# Sides are the figure's own. "*.unsplit" is a hand whose fingers could not be separated: it is
# not split into invented fingers.
_FINGERS = ("thumb", "index", "middle", "ring", "pinky")
_HAND_COLORS = {"palm": (255, 214, 170), "thumb": (255, 196, 0), "index": (255, 120, 40), "middle": (230, 60, 90),
                "ring": (170, 70, 200), "pinky": (80, 110, 255), "unsplit": (255, 170, 120)}


def _lighter(rgb, k=.45):
    return tuple(round(c + (255 - c) * k) for c in rgb)


def _parts():
    out = [("head", 1, "head_neck", "Голова", (80, 190, 235)), ("neck", 2, "head_neck", "Шея", (40, 140, 200)),
           ("chest", 3, "torso", "Грудь", (255, 143, 200)), ("abdomen", 4, "torso", "Живот", (240, 110, 175)),
           ("pelvis", 5, "torso", "Таз", (215, 80, 150))]
    for side, base, darker in (("left", 10, False), ("right", 20, True)):
        tone = (lambda c: c) if darker else _lighter
        name = "Левое" if side == "left" else "Правое"
        out += [(f"{side}_upper_arm", base, f"{side}_arm", f"{name} плечо (верх руки)", tone((232, 58, 58))),
                (f"{side}_forearm", base + 1, f"{side}_arm", f"{name} предплечье", tone((200, 40, 40))),
                (f"{side}_hand.palm", base + 2, f"{side}_hand", f"{'Левая' if side == 'left' else 'Правая'} ладонь", tone(_HAND_COLORS["palm"]))]
        for i, finger in enumerate(_FINGERS):
            out.append((f"{side}_hand.{finger}", base + 3 + i, f"{side}_hand",
                        f"{'Левый' if side == 'left' else 'Правый'} {('большой', 'указательный', 'средний', 'безымянный', 'мизинец')[i]}"
                        + ("" if finger == "pinky" else " палец"), tone(_HAND_COLORS[finger])))
        out.append((f"{side}_hand.unsplit", base + 8, f"{side}_hand",
                    f"{'Левая' if side == 'left' else 'Правая'} кисть (пальцы не разделены)", tone(_HAND_COLORS["unsplit"])))
    for side, base, darker in (("left", 30, False), ("right", 40, True)):
        tone = (lambda c: c) if darker else _lighter
        name = "Левое" if side == "left" else "Правое"
        out += [(f"{side}_thigh", base, f"{side}_leg", f"{name} бедро", tone((47, 191, 107))),
                (f"{side}_shin", base + 1, f"{side}_leg", f"{'Левая' if side == 'left' else 'Правая'} голень", tone((30, 150, 80))),
                (f"{side}_foot", base + 2, f"{side}_leg", f"{'Левая' if side == 'left' else 'Правая'} стопа", tone((20, 110, 60)))]
    return {pid: {"value": v, "parent": parent, "name": name, "color": color} for pid, v, parent, name, color in out}


PARTS = _parts()
VALUE_TO_PART = {p["value"]: pid for pid, p in PARTS.items()}
GROUPS = {"body": None, "head_neck": "body", "torso": "body", "left_arm": "body", "right_arm": "body",
          "left_hand": "left_arm", "right_hand": "right_arm", "left_leg": "body", "right_leg": "body"}
UNKNOWN_COLOR = (255, 0, 255)

# Joints: the body keeps skeleton version 2's ids (docs/SKELETON.md) so the old rig reads them;
# each hand has MediaPipe's 21 points as <side>_hand.<finger>.<joint>.
BODY_JOINTS = ("head", "neck", "chest", "navel", "pelvis",
               *(f"{n}_{s}" for n in ("shoulder", "elbow", "wrist", "hip", "knee", "foot", "heel", "toe") for s in "lr"))
HAND_POINTS = ("wrist", "thumb.cmc", "thumb.mcp", "thumb.ip", "thumb.tip", "index.mcp", "index.pip", "index.dip", "index.tip",
               "middle.mcp", "middle.pip", "middle.dip", "middle.tip", "ring.mcp", "ring.pip", "ring.dip", "ring.tip",
               "pinky.mcp", "pinky.pip", "pinky.dip", "pinky.tip")  # MediaPipe Hand landmark order
HAND_JOINTS = tuple(f"{side}_hand.{p}" for side in ("left", "right") for p in HAND_POINTS)
JOINT_IDS = BODY_JOINTS + HAND_JOINTS
BODY_BONES = (("head", "neck"), ("neck", "chest"), ("chest", "navel"), ("navel", "pelvis"),
              ("chest", "shoulder_l"), ("chest", "shoulder_r"), ("shoulder_l", "elbow_l"), ("shoulder_r", "elbow_r"),
              ("elbow_l", "wrist_l"), ("elbow_r", "wrist_r"), ("pelvis", "hip_l"), ("pelvis", "hip_r"),
              ("hip_l", "knee_l"), ("hip_r", "knee_r"), ("knee_l", "foot_l"), ("knee_r", "foot_r"),
              ("foot_l", "heel_l"), ("foot_r", "heel_r"), ("foot_l", "toe_l"), ("foot_r", "toe_r"))
HAND_CHAINS = (("wrist", "thumb.cmc", "thumb.mcp", "thumb.ip", "thumb.tip"),
               ("wrist", "index.mcp", "index.pip", "index.dip", "index.tip"),
               ("index.mcp", "middle.mcp", "middle.pip", "middle.dip", "middle.tip"),
               ("middle.mcp", "ring.mcp", "ring.pip", "ring.dip", "ring.tip"),
               ("ring.mcp", "pinky.mcp", "pinky.pip", "pinky.dip", "pinky.tip"), ("wrist", "pinky.mcp"))
STATES = ("visible", "uncertain", "hidden", "absent")  # absent: not in the picture at all
SOURCES = ("model", "estimated", "user", "ai")
OVERLAYS = ("original", "parts", "outline", "skeleton", "uncertain")


class CommandError(ValueError):
    """A refused command: bad arguments, a locked entity, an out-of-date revision."""

    def __init__(self, message, code="invalid"):
        super().__init__(message)
        self.code = code


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def oriented(path: Path) -> Image.Image:
    with Image.open(path) as opened:
        opened.load()
        return ImageOps.exif_transpose(opened).convert("RGBA")


def _write_atomic(path: Path, data: bytes):
    tmp = path.with_name(f".{os.getpid()}.{threading.get_ident() % 100000}.tmp")  # short: Windows MAX_PATH
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG", compress_level=3)
    return buffer.getvalue()


def _part_mask(labels: Image.Image, values) -> Image.Image:
    values = set(values)
    return labels.point(lambda v: 255 if v in values else 0)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def empty_instance(iid, roi, foreground_source):
    return {"id": iid, "roi": [int(v) for v in roi], "foreground": {"source": foreground_source},
            "parts": {pid: {"state": "absent", "source": None, "locked": False, "review": "unreviewed", "diagnostics": []}
                      for pid in PARTS},
            "joints": {}, "analysis": None, "proposal": None}


# --- Project ------------------------------------------------------------------------------------

class Project:
    """One figure document in its folder. Thread-safe: commands run under a lock."""

    def __init__(self, folder: Path, doc: dict):
        self.folder = Path(folder)
        self.doc = doc
        self.lock = threading.RLock()
        self._labels: dict[str, Image.Image] = {}
        self._uncertain: dict[str, Image.Image] = {}
        self._source: Image.Image | None = None
        self.undo_stack: list[dict] = []
        self.redo_stack: list[dict] = []
        self.listeners = []  # callables(change dict) — the UI is told about every change

    # --- files ---
    @classmethod
    def create(cls, image_path: Path, folder: Path, name: str | None = None) -> "Project":
        """A new project for a picture (copied in; the original is not touched)."""
        image_path, folder = Path(image_path), Path(folder)
        if folder.exists() and any(folder.iterdir()):
            raise CommandError(f"Папка проекта не пуста: {folder}", "exists")
        image = oriented(image_path)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "masks").mkdir(exist_ok=True)
        source = folder / f"source{image_path.suffix.lower() or '.png'}"
        shutil.copy2(image_path, source)
        digest = sha256_file(source)
        alpha = image.getchannel("A")
        transparent = alpha.getextrema()[0] < 255
        roi = (alpha.point(lambda a: 255 if a > 8 else 0).getbbox() if transparent else None) or (0, 0, *image.size)
        doc = {"schema": SCHEMA, "version": VERSION, "id": digest[:16], "revision": 0, "created": _now(),
               "name": name or image_path.stem,
               "source": {"file": source.name, "sha256": digest, "width": image.width, "height": image.height,
                          "original_name": image_path.name, "orientation": "exif applied"},
               "coordinates": {"units": "px", "origin": "top-left of the oriented source picture", "y": "down"},
               "taxonomy": {"background": BACKGROUND, "unknown": UNKNOWN,
                            "parts": {pid: p["value"] for pid, p in PARTS.items()}},
               "instances": [empty_instance("fig1", roi, "alpha" if transparent else "whole roi")]}
        project = cls(folder, doc)
        labels = Image.new("L", image.size, BACKGROUND)
        if transparent:
            labels.paste(UNKNOWN, mask=alpha.point(lambda a: 255 if a > 8 else 0))
        else:
            labels.paste(UNKNOWN, roi)
        project._labels["fig1"] = labels
        project._uncertain["fig1"] = Image.new("L", image.size, 0)
        project._source = image
        project.save()
        project._journal({"revision": 0, "time": _now(), "source": "user", "command": "create_project",
                          "summary": f"проект из {image_path.name}"})
        return project

    @classmethod
    def open(cls, folder: Path) -> "Project":
        folder = Path(folder)
        try:
            doc = json.loads((folder / "figure.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise CommandError(f"Не удалось открыть проект: {error}", "not_found") from error
        if doc.get("schema") != SCHEMA or doc.get("version") != VERSION:
            raise CommandError("Неизвестная версия документа фигуры", "version")
        project = cls(folder, doc)
        source = folder / doc["source"]["file"]
        if not source.exists() or sha256_file(source) != doc["source"]["sha256"]:
            raise CommandError("Исходная картинка проекта отсутствует или изменена", "source")
        return project

    def save(self):
        with self.lock:
            for iid, image in self._labels.items():
                _write_atomic(self.folder / "masks" / f"{iid}_labels.png", _png(image))
            for iid, image in self._uncertain.items():
                _write_atomic(self.folder / "masks" / f"{iid}_uncertain.png", _png(image))
            _write_atomic(self.folder / "figure.json", json.dumps(self.doc, ensure_ascii=False, indent=1).encode("utf-8"))
            self._prune_proposals()

    def _prune_proposals(self):
        """Proposal images no longer referenced by the document or its undo / redo history."""
        docs = [self.doc] + [step[k] for step in self.undo_stack + self.redo_stack for k in ("doc_before", "doc_after")]
        keep = {name for doc in docs for inst in doc["instances"] if inst.get("proposal")
                for name in (inst["proposal"]["labels"], inst["proposal"]["uncertain"])}
        for path in (self.folder / "masks").glob("*_prop*_[lu].png"):
            if path.name not in keep:
                path.unlink(missing_ok=True)

    def _journal(self, entry):
        with open(self.folder / "journal.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def source(self) -> Image.Image:
        if self._source is None:
            self._source = oriented(self.folder / self.doc["source"]["file"])
        return self._source

    def labels(self, iid) -> Image.Image:
        if iid not in self._labels:
            self.instance(iid)
            self._labels[iid] = Image.open(self.folder / "masks" / f"{iid}_labels.png").convert("L")
        return self._labels[iid]

    def uncertain(self, iid) -> Image.Image:
        if iid not in self._uncertain:
            path = self.folder / "masks" / f"{iid}_uncertain.png"
            self._uncertain[iid] = Image.open(path).convert("L") if path.exists() else Image.new("L", self.labels(iid).size, 0)
        return self._uncertain[iid]

    def instance(self, iid=None) -> dict:
        instances = self.doc["instances"]
        if iid is None:
            return instances[0]
        for inst in instances:
            if inst["id"] == iid:
                return inst
        raise CommandError(f"Нет фигуры {iid!r}", "not_found")

    # --- execution ---
    def execute(self, command: str, args: dict | None = None, source="user", expected_revision=None):
        """Runs a command (or a "transaction" of commands) as one undoable step."""
        args = dict(args or {})
        if source not in ("user", "ai", "model"):
            raise CommandError(f"Неизвестный источник {source!r}")
        if command not in COMMANDS and command not in READS:
            raise CommandError(f"Неизвестная команда {command!r}", "unknown_command")
        with self.lock:
            if command in READS:
                return READS[command](self, **args)
            if expected_revision is not None and expected_revision != self.doc["revision"]:
                raise CommandError(f"Документ уже изменён: ревизия {self.doc['revision']}, ожидалась {expected_revision}",
                                   "conflict")
            before = self._snapshot()
            try:
                if command == "transaction":
                    results = []
                    for step in args.get("commands") or []:
                        name = step.get("command")
                        if name not in COMMANDS or name in ("transaction", "undo", "redo", "store_analysis"):
                            raise CommandError(f"В транзакции недопустима команда {name!r}")
                        results.append(COMMANDS[name](self, source=source, **(step.get("args") or {})))
                    result = {"results": results}
                elif command in ("undo", "redo"):
                    return COMMANDS[command](self, source=source)
                else:
                    result = COMMANDS[command](self, source=source, **args)
            except TypeError as error:
                self._restore(before)
                raise CommandError(f"Неверные аргументы команды {command}: {error}") from error
            except Exception:
                self._restore(before)
                raise
            step = self._diff(before, command, source, args)
            if step is None:
                return {"revision": self.doc["revision"], "changed": False, **(result or {})}
            self.doc["revision"] += 1
            step["revision"] = self.doc["revision"]
            self.undo_stack.append(step)
            del self.undo_stack[:-100]
            self.redo_stack.clear()
            self.save()
            self._journal({"revision": self.doc["revision"], "time": _now(), "source": source, "command": command,
                           "summary": step["summary"]})
            self._notify({"revision": self.doc["revision"], "command": command, "source": source})
            return {"revision": self.doc["revision"], "changed": True, **(result or {})}

    def _notify(self, change):
        for listener in list(self.listeners):
            try:
                listener(change)
            except Exception:  # noqa: BLE001 — a broken listener must not break editing
                pass

    def _snapshot(self):
        return {"doc": copy.deepcopy(self.doc),
                "labels": {iid: im.copy() for iid, im in self._labels.items()},
                "uncertain": {iid: im.copy() for iid, im in self._uncertain.items()}}

    def _restore(self, snapshot):
        self.doc = snapshot["doc"]
        self._labels = snapshot["labels"]
        self._uncertain = snapshot["uncertain"]

    def _diff(self, before, command, source, args):
        """The undo step of a finished command (None if nothing changed): the document before and
        after, and only the changed rectangle of each mask."""
        images = []
        for kind, now, then in (("labels", self._labels, before["labels"]), ("uncertain", self._uncertain, before["uncertain"])):
            for iid, image in now.items():
                old = then.get(iid)
                box = ImageChops.difference(image, old).getbbox() if old is not None and old.size == image.size else (0, 0, *image.size)
                if box:
                    images.append({"kind": kind, "iid": iid, "box": box,
                                   "before": _png(old.crop(box)) if old is not None else None, "after": _png(image.crop(box))})
        doc_before = {k: v for k, v in before["doc"].items() if k != "revision"}
        doc_after = {k: v for k, v in self.doc.items() if k != "revision"}
        if not images and doc_before == doc_after:
            return None
        return {"command": command, "source": source, "summary": _summary(command, args), "doc_before": before["doc"],
                "doc_after": copy.deepcopy(self.doc), "images": images}

    def _apply_images(self, images, which):
        for item in images:
            store = self._labels if item["kind"] == "labels" else self._uncertain
            target = store.get(item["iid"]) if item["iid"] in store else (
                self.labels(item["iid"]) if item["kind"] == "labels" else self.uncertain(item["iid"]))
            data = item[which]
            if data is None:
                continue
            store[item["iid"]] = target
            target.paste(Image.open(io.BytesIO(data)), item["box"][:2])


# --- Commands ---------------------------------------------------------------------------------------
# Each takes the project, keyword arguments and `source`; returns a dict (merged into the reply).

def _summary(command, args):
    keys = [k for k in ("part_id", "joint_id", "entity_id", "part_ids", "target", "state", "locked") if k in args]
    return command + ("(" + ", ".join(f"{k}={args[k]}" for k in keys) + ")" if keys else "")


def _check_part(pid):
    if pid not in PARTS:
        raise CommandError(f"Нет такой части: {pid!r}. Части: {', '.join(PARTS)}")


def _check_joint(jid):
    if jid not in JOINT_IDS:
        raise CommandError(f"Нет такого сустава: {jid!r}")


def _guard(entity, source, override_lock, what):
    """Locked entities are protected from automatic replacement; an agent must ask explicitly."""
    if entity.get("locked") and (source == "model" or (source == "ai" and not override_lock)):
        raise CommandError(f"{what} зафиксирован(а) вручную; снимите фиксацию или передайте override_lock", "locked")


def _mark(entity, source, method):
    entity["source"] = {"kind": "user" if source == "user" else "ai" if source == "ai" else entity.get("source", {}).get("kind"),
                        "method": method, "time": _now()}
    entity["review"] = "edited"


def _xy(value, project):
    if not (isinstance(value, (list, tuple)) and len(value) == 2 and all(isinstance(v, (int, float)) and math.isfinite(v) for v in value)):
        raise CommandError(f"Координаты должны быть [x, y] в пикселях исходника: {value!r}")
    w, h = project.doc["source"]["width"], project.doc["source"]["height"]
    if not (-w <= value[0] <= 2 * w and -h <= value[1] <= 2 * h):
        raise CommandError(f"Точка {value} далеко за пределами картинки {w}×{h}")
    return [round(float(value[0]), 2), round(float(value[1]), 2)]


def cmd_set_joint(project, joint_id, xy, instance_id=None, state=None, override_lock=False, source="user"):
    _check_joint(joint_id)
    inst = project.instance(instance_id)
    joint = inst["joints"].setdefault(joint_id, {"state": "absent", "locked": False})
    _guard(joint, source, override_lock, f"Сустав {joint_id}")
    joint["xy"] = _xy(xy, project)
    joint["state"] = state if state in STATES else ("visible" if joint.get("state") in (None, "absent") else joint["state"])
    joint["score"] = None  # a hand-placed point has no model score
    _mark(joint, source, "set_joint")
    return {"joint": {joint_id: joint}}


def cmd_set_visibility(project, entity_id, state, instance_id=None, override_lock=False, source="user"):
    if state not in STATES:
        raise CommandError(f"Состояние должно быть одним из {STATES}")
    inst = project.instance(instance_id)
    if entity_id in PARTS:
        entity, what = inst["parts"][entity_id], f"Часть {entity_id}"
    elif entity_id in JOINT_IDS:
        entity, what = inst["joints"].setdefault(entity_id, {"state": "absent", "locked": False}), f"Сустав {entity_id}"
        if state != "absent" and "xy" not in entity:
            raise CommandError(f"У сустава {entity_id} нет координат: сначала set_joint")
    else:
        raise CommandError(f"Нет части или сустава {entity_id!r}")
    _guard(entity, source, override_lock, what)
    entity["state"] = state
    _mark(entity, source, "set_visibility")
    return {}


def cmd_set_locked(project, entity_id, locked, instance_id=None, override_lock=False, source="user"):
    """Locks protect an entity from re-analysis and from an agent. An agent may lock anything,
    but unlocks what the user locked only with override_lock (asked explicitly)."""
    inst = project.instance(instance_id)
    if entity_id in PARTS:
        entity = inst["parts"][entity_id]
    elif entity_id in JOINT_IDS and entity_id in inst["joints"]:
        entity = inst["joints"][entity_id]
    else:
        raise CommandError(f"Нет части или сустава {entity_id!r}")
    if not locked and entity.get("locked") and entity.get("locked_by", "user") == "user":
        _guard(entity, source, override_lock, entity_id)
    entity["locked"] = bool(locked)
    entity["locked_by"] = source if locked else None
    return {}


def cmd_set_review(project, entity_id, review, instance_id=None, source="user"):
    if review not in ("unreviewed", "accepted", "rejected", "edited"):
        raise CommandError("review: unreviewed | accepted | rejected | edited")
    inst = project.instance(instance_id)
    entity = inst["parts"].get(entity_id) or inst["joints"].get(entity_id)
    if entity is None:
        raise CommandError(f"Нет части или сустава {entity_id!r}")
    entity["review"] = review
    return {}


def _refresh_part_states(project, inst, pids):
    """A part with pixels is at least visible; one that lost all its pixels becomes hidden."""
    histogram = project.labels(inst["id"]).histogram()
    for pid in pids:
        part = inst["parts"][pid]
        count = histogram[PARTS[pid]["value"]]
        if count and part["state"] in ("absent", "hidden"):
            part["state"] = "visible"
        elif not count and part["state"] in ("visible", "uncertain"):
            part["state"] = "hidden"


def cmd_edit_mask(project, part_id, strokes, instance_id=None, override_lock=False, figure_only=True, source="user"):
    """Paints (mode add) or erases (mode erase → unknown, mode background → not the figure) round
    brush strokes: [{"points": [[x, y], …], "radius": px, "mode": "add"|"erase"|"background"}].
    With figure_only, painting stays inside the figure's silhouette."""
    _check_part(part_id)
    inst = project.instance(instance_id)
    _guard(inst["parts"][part_id], source, override_lock, f"Часть {part_id}")
    labels = project.labels(inst["id"])
    if not isinstance(strokes, list) or not strokes:
        raise CommandError("strokes: непустой список мазков")
    value = PARTS[part_id]["value"]
    stroke_mask = Image.new("L", labels.size, 0)
    erase_mask = Image.new("L", labels.size, 0)
    background_mask = Image.new("L", labels.size, 0)
    for stroke in strokes:
        points = [_xy(p, project) for p in stroke.get("points", [])]
        radius = float(stroke.get("radius", 6))
        if not points or not (0.5 <= radius <= 400):
            raise CommandError("У мазка нужны points и radius 0.5…400 px")
        target = {"add": stroke_mask, "erase": erase_mask, "background": background_mask}.get(stroke.get("mode", "add"))
        if target is None:
            raise CommandError("mode: add | erase | background")
        draw = ImageDraw.Draw(target)
        if len(points) > 1:
            draw.line([tuple(p) for p in points], fill=255, width=max(1, round(radius * 2)), joint="curve")
        for x, y in points:
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=255)
    figure = labels.point(lambda v: 255 if v != BACKGROUND else 0)
    if figure_only:
        stroke_mask = ImageChops.multiply(stroke_mask, figure)
    locked_values = [PARTS[p]["value"] for p, part in inst["parts"].items() if part["locked"] and p != part_id]
    if locked_values:  # never paint over another locked part
        keep = _part_mask(labels, locked_values)
        stroke_mask = ImageChops.subtract(stroke_mask, keep)
        erase_mask = ImageChops.subtract(erase_mask, keep)
        background_mask = ImageChops.subtract(background_mask, keep)
    own = _part_mask(labels, [value])
    labels.paste(value, mask=stroke_mask)
    labels.paste(UNKNOWN, mask=ImageChops.multiply(erase_mask, own))  # erase removes only this part's pixels
    labels.paste(BACKGROUND, mask=background_mask)
    project.uncertain(inst["id"]).paste(0, mask=stroke_mask)
    _mark(inst["parts"][part_id], source, "edit_mask")
    _refresh_part_states(project, inst, PARTS)
    return {}


def _flood(project, inst, point, edges):
    """Pixels reachable from `point` inside the figure without crossing drawn lines."""
    x, y = (round(v) for v in point)
    work = edges.copy()
    if not (0 <= x < work.width and 0 <= y < work.height) or work.getpixel((x, y)) != 128:
        return Image.new("L", work.size, 0)
    ImageDraw.floodfill(work, (x, y), 255)
    return work.point(lambda v: 255 if v == 255 else 0)


def _fill_space(project, inst):
    """128 = inside the figure and not on a drawn outline; 0 elsewhere (for region filling)."""
    image = project.source()
    labels = project.labels(inst["id"])
    figure = labels.point(lambda v: 255 if v != BACKGROUND else 0)
    lines = image.convert("L").point(lambda v: 255 if v < 110 else 0)  # the sketch's dark outlines
    space = ImageChops.subtract(figure, lines)
    return space.point(lambda v: 128 if v else 0)


def cmd_refine_mask(project, part_id, positive_points, negative_points=(), instance_id=None, override_lock=False, source="user"):
    """Fills the drawing's regions (bounded by its outlines) that contain the positive points with
    the part; regions of the negative points are taken from it (to unknown). Works on line
    drawings; on photos regions are not bounded and the command refuses large fills."""
    _check_part(part_id)
    inst = project.instance(instance_id)
    _guard(inst["parts"][part_id], source, override_lock, f"Часть {part_id}")
    labels = project.labels(inst["id"])
    space = _fill_space(project, inst)
    figure_area = labels.point(lambda v: 255 if v != BACKGROUND else 0).histogram()[255] or 1
    value = PARTS[part_id]["value"]
    locked_values = [PARTS[p]["value"] for p, part in inst["parts"].items() if part["locked"] and p != part_id]
    keep = _part_mask(labels, locked_values) if locked_values else None
    filled = 0
    for points, new in ((positive_points, value), (negative_points, UNKNOWN)):
        for point in points or ():
            region = _flood(project, inst, _xy(point, project), space)
            area = region.histogram()[255]
            if area > .6 * figure_area:
                raise CommandError("Область не замкнута контуром (заливка ушла на всю фигуру); используйте edit_mask",
                                   "unbounded")
            if keep is not None:
                region = ImageChops.subtract(region, keep)
            if new == UNKNOWN:
                region = ImageChops.multiply(region, _part_mask(labels, [value]))
            labels.paste(new, mask=region)
            filled += area
    _mark(inst["parts"][part_id], source, "refine_mask")
    _refresh_part_states(project, inst, PARTS)
    return {"filled_px": filled}


def cmd_relabel(project, part_id, target, instance_id=None, override_lock=False, source="user"):
    """Moves all pixels of part_id to target (a wrong name, e.g. left/right swapped)."""
    _check_part(part_id)
    _check_part(target)
    inst = project.instance(instance_id)
    for pid in (part_id, target):
        _guard(inst["parts"][pid], source, override_lock, f"Часть {pid}")
    labels = project.labels(inst["id"])
    labels.paste(PARTS[target]["value"], mask=_part_mask(labels, [PARTS[part_id]["value"]]))
    for pid in (part_id, target):
        _mark(inst["parts"][pid], source, "relabel")
    _refresh_part_states(project, inst, PARTS)
    return {}


def cmd_merge_parts(project, part_ids, target, instance_id=None, override_lock=False, source="user"):
    if not isinstance(part_ids, list) or not part_ids:
        raise CommandError("part_ids: список частей")
    for pid in part_ids:
        cmd_relabel(project, pid, target, instance_id, override_lock, source) if pid != target else None
    return {}


def cmd_split_part(project, part_id, line, target, instance_id=None, override_lock=False, source="user"):
    """Pixels of part_id on the left of the directed line [[x1, y1], [x2, y2]] (as drawn on the
    picture, y down) go to target — e.g. cut the thumb off a hand."""
    _check_part(part_id)
    _check_part(target)
    inst = project.instance(instance_id)
    for pid in (part_id, target):
        _guard(inst["parts"][pid], source, override_lock, f"Часть {pid}")
    if not (isinstance(line, list) and len(line) == 2):
        raise CommandError("line: [[x1, y1], [x2, y2]]")
    (x1, y1), (x2, y2) = (_xy(p, project) for p in line)
    if math.hypot(x2 - x1, y2 - y1) < 1:
        raise CommandError("Линия разреза слишком короткая")
    labels = project.labels(inst["id"])
    w, h = labels.size
    # Half-plane on the left of the line, as a big polygon.
    dx, dy = (x2 - x1), (y2 - y1)
    k = 4 * (w + h) / math.hypot(dx, dy)
    nx, ny = dy, -dx  # left normal (y down)
    a, b = (x1 - dx * k, y1 - dy * k), (x2 + dx * k, y2 + dy * k)
    half = Image.new("L", labels.size, 0)
    ImageDraw.Draw(half).polygon([a, b, (b[0] + nx * k, b[1] + ny * k), (a[0] + nx * k, a[1] + ny * k)], fill=255)
    moved = ImageChops.multiply(half, _part_mask(labels, [PARTS[part_id]["value"]]))
    labels.paste(PARTS[target]["value"], mask=moved)
    for pid in (part_id, target):
        _mark(inst["parts"][pid], source, "split_part")
    _refresh_part_states(project, inst, PARTS)
    return {"moved_px": moved.histogram()[255]}


def cmd_set_roi(project, roi, instance_id=None, source="user"):
    """Chooses the figure to analyse on a picture with several (an [x1, y1, x2, y2] rectangle)."""
    inst = project.instance(instance_id)
    if not (isinstance(roi, list) and len(roi) == 4):
        raise CommandError("roi: [x1, y1, x2, y2]")
    w, h = project.doc["source"]["width"], project.doc["source"]["height"]
    x1, y1, x2, y2 = (int(round(v)) for v in roi)
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
    if x2 - x1 < 16 or y2 - y1 < 16:
        raise CommandError("Область слишком мала")
    inst["roi"] = [x1, y1, x2, y2]
    if inst["foreground"]["source"] != "alpha":
        labels = project.labels(inst["id"])
        labels.paste(BACKGROUND, (0, 0, w, h))
        labels.paste(UNKNOWN, (x1, y1, x2, y2))
    return {}


def apply_analysis(project, inst, result, source="model", only=None):
    """Writes an analysis result (labels image, uncertain image, joints, part info) into the
    instance, keeping every locked part's pixels and every locked joint. `only` limits it to
    these part / joint ids."""
    labels = project.labels(inst["id"])
    new_labels, new_uncertain = result["labels"], result["uncertain"]
    selected_parts = [p for p in PARTS if only is None or p in only]
    locked = [PARTS[p]["value"] for p, part in inst["parts"].items() if part["locked"]]
    take = _part_mask(new_labels, [PARTS[p]["value"] for p in selected_parts]) if only is not None else \
        new_labels.point(lambda v: 255)
    if only is not None:  # pixels that were a selected part may become another part
        take = ImageChops.lighter(take, _part_mask(labels, [PARTS[p]["value"] for p in selected_parts]))
    if locked:
        take = ImageChops.subtract(take, _part_mask(labels, locked))
    proposed = new_labels.copy()
    if locked:  # a locked part's value may not appear outside its accepted pixels
        proposed.paste(UNKNOWN, mask=_part_mask(new_labels, locked))
    labels.paste(proposed, mask=take)
    project.uncertain(inst["id"]).paste(new_uncertain, mask=take)
    for pid, info in result["parts"].items():
        part = inst["parts"][pid]
        if part["locked"] or pid not in selected_parts:
            continue
        part.update(state=info["state"], source=info["source"], diagnostics=info.get("diagnostics", []), review="unreviewed")
    for jid, joint in result["joints"].items():
        if only is not None and jid not in only:
            continue
        current = inst["joints"].get(jid)
        if current and current.get("locked"):
            continue
        inst["joints"][jid] = {**joint, "locked": False, "review": "unreviewed"}
    _refresh_part_states(project, inst, PARTS)


def cmd_apply_proposal(project, instance_id=None, only=None, source="user"):
    """Accepts the pending analysis proposal (whole, or only these part / joint ids)."""
    inst = project.instance(instance_id)
    proposal = inst.get("proposal")
    if not proposal:
        raise CommandError("Нет предложения анализа", "no_proposal")
    folder = project.folder / "masks"
    result = {"labels": Image.open(folder / proposal["labels"]).convert("L"),
              "uncertain": Image.open(folder / proposal["uncertain"]).convert("L"),
              "parts": proposal["parts"], "joints": proposal["joints"]}
    apply_analysis(project, inst, result, source="model", only=set(only) if only else None)
    inst["analysis"] = proposal["analysis"]
    inst["proposal"] = None
    return {}


def cmd_discard_proposal(project, instance_id=None, source="user"):
    inst = project.instance(instance_id)
    inst["proposal"] = None
    return {}


def cmd_store_analysis(project, result, instance_id=None, as_proposal=None, source="model"):
    """Internal (analysis jobs): applies a result directly to an untouched instance, otherwise
    keeps it as a proposal to compare and accept."""
    inst = project.instance(instance_id)
    untouched = all(p["source"] is None or p["source"].get("kind") in ("model", "estimated") for p in inst["parts"].values()) and \
        not any(p["locked"] for p in inst["parts"].values()) and \
        all(j.get("source", {}).get("kind") in (None, "model", "estimated") for j in inst["joints"].values())
    if as_proposal is None:
        as_proposal = not untouched
    if not as_proposal:
        apply_analysis(project, inst, result)
        inst["analysis"] = result["analysis"]
        inst["proposal"] = None
        return {"applied": True}
    folder = project.folder / "masks"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    names = (f"{inst['id']}_prop{stamp}_l.png", f"{inst['id']}_prop{stamp}_u.png")
    _write_atomic(folder / names[0], _png(result["labels"]))
    _write_atomic(folder / names[1], _png(result["uncertain"]))
    inst["proposal"] = {"labels": names[0], "uncertain": names[1], "parts": result["parts"], "joints": result["joints"],
                        "analysis": result["analysis"]}
    return {"applied": False, "proposal": True}


def cmd_undo(project, source="user"):
    if not project.undo_stack:
        raise CommandError("Нечего отменять", "empty")
    step = project.undo_stack.pop()
    revision = project.doc["revision"]
    project.doc = copy.deepcopy(step["doc_before"])
    project._apply_images(step["images"], "before")
    project.doc["revision"] = revision + 1
    project.redo_stack.append(step)
    project.save()
    project._journal({"revision": project.doc["revision"], "time": _now(), "source": source, "command": "undo",
                      "summary": f"отмена: {step['summary']}"})
    project._notify({"revision": project.doc["revision"], "command": "undo", "source": source})
    return {"revision": project.doc["revision"], "undone": step["summary"]}


def cmd_redo(project, source="user"):
    if not project.redo_stack:
        raise CommandError("Нечего повторять", "empty")
    step = project.redo_stack.pop()
    revision = project.doc["revision"]
    project.doc = copy.deepcopy(step["doc_after"])
    project._apply_images(step["images"], "after")
    project.doc["revision"] = revision + 1
    project.undo_stack.append(step)
    project.save()
    project._journal({"revision": project.doc["revision"], "time": _now(), "source": source, "command": "redo",
                      "summary": f"повтор: {step['summary']}"})
    project._notify({"revision": project.doc["revision"], "command": "redo", "source": source})
    return {"revision": project.doc["revision"], "redone": step["summary"]}


COMMANDS = {"set_joint": cmd_set_joint, "set_visibility": cmd_set_visibility, "set_locked": cmd_set_locked,
            "set_review": cmd_set_review, "edit_mask": cmd_edit_mask, "refine_mask": cmd_refine_mask,
            "relabel": cmd_relabel, "merge_parts": cmd_merge_parts, "split_part": cmd_split_part, "set_roi": cmd_set_roi,
            "apply_proposal": cmd_apply_proposal, "discard_proposal": cmd_discard_proposal,
            "store_analysis": cmd_store_analysis, "transaction": None, "undo": cmd_undo, "redo": cmd_redo}


# --- Reading ----------------------------------------------------------------------------------------

def read_capabilities(project=None):
    return {"schema": SCHEMA, "version": VERSION,
            "commands": sorted(c for c in COMMANDS if c != "store_analysis"), "reads": sorted(READS),
            "overlays": list(OVERLAYS), "states": list(STATES), "sources": list(SOURCES),
            "parts": {pid: {"value": p["value"], "parent": p["parent"], "name": p["name"], "color": p["color"]} for pid, p in PARTS.items()},
            "joints": {"body": list(BODY_JOINTS), "hand": list(HAND_POINTS)},
            "not_yet": ["set_binding", "edit_weights", "set_layer_order", "set_joint_rotation", "set_ik_target",
                        "set_keyframe", "export_frames"],
            "coordinates": "px of the oriented source picture, origin top-left, y down"}


def read_summary(project):
    inst_out = []
    for inst in project.doc["instances"]:
        histogram = project.labels(inst["id"]).histogram()
        parts = {pid: {"state": p["state"], "px": histogram[PARTS[pid]["value"]], "locked": p["locked"],
                       "source": (p["source"] or {}).get("kind")} for pid, p in inst["parts"].items() if p["state"] != "absent" or histogram[PARTS[pid]["value"]]}
        inst_out.append({"id": inst["id"], "roi": inst["roi"], "foreground": inst["foreground"], "parts": parts,
                         "unknown_px": histogram[UNKNOWN], "joints": len([j for j in inst["joints"].values() if j.get("xy")]),
                         "analysis": inst["analysis"], "proposal": bool(inst.get("proposal"))})
    return {"id": project.doc["id"], "name": project.doc.get("name"), "revision": project.doc["revision"],
            "source": project.doc["source"], "coordinates": project.doc["coordinates"], "folder": str(project.folder),
            "instances": inst_out, "can_undo": bool(project.undo_stack), "can_redo": bool(project.redo_stack)}


def read_parts(project, instance_id=None):
    inst = project.instance(instance_id)
    labels = project.labels(inst["id"])
    histogram = labels.histogram()
    out = {}
    for pid, part in inst["parts"].items():
        value = PARTS[pid]["value"]
        box = _part_mask(labels, [value]).getbbox() if histogram[value] else None
        out[pid] = {**part, "name": PARTS[pid]["name"], "parent": PARTS[pid]["parent"], "value": value, "color": PARTS[pid]["color"],
                    "px": histogram[value], "bbox": list(box) if box else None}
    return {"revision": project.doc["revision"], "instance": inst["id"], "parts": out, "unknown_px": histogram[UNKNOWN]}


def read_joints(project, instance_id=None):
    inst = project.instance(instance_id)
    return {"revision": project.doc["revision"], "instance": inst["id"], "joints": inst["joints"],
            "bones": [list(b) for b in BODY_BONES], "hand_chains": [list(c) for c in HAND_CHAINS]}


def read_part(project, part_id, instance_id=None):
    _check_part(part_id)
    data = read_parts(project, instance_id)
    return {"revision": data["revision"], "part_id": part_id, **data["parts"][part_id]}


def read_mask(project, part_id, instance_id=None):
    """Writes the part's mask (255 = the part) as a PNG; returns its path and bbox."""
    _check_part(part_id)
    inst = project.instance(instance_id)
    mask = _part_mask(project.labels(inst["id"]), [PARTS[part_id]["value"]])
    folder = project.folder / "previews"
    folder.mkdir(exist_ok=True)
    path = folder / f"mask_{inst['id']}_{part_id}.png"
    _write_atomic(path, _png(mask))
    box = mask.getbbox()
    return {"revision": project.doc["revision"], "part_id": part_id, "file": str(path), "bbox": list(box) if box else None,
            "px": mask.histogram()[255], "size": list(mask.size)}


def render(project, overlays=("original", "parts", "skeleton"), instance_id=None, region=None, max_side=1024, opacity=.55):
    """The picture with the chosen overlays (original, parts, outline, skeleton, uncertain), cropped
    to `region` and scaled to fit `max_side`. Returns (image, transform) with transform mapping
    preview px back to source px: source = preview / scale + offset."""
    inst = project.instance(instance_id)
    source = project.source()
    labels = project.labels(inst["id"])
    w, h = source.size
    x1, y1, x2, y2 = region or (0, 0, w, h)
    x1, y1, x2, y2 = max(0, int(x1)), max(0, int(y1)), min(w, int(math.ceil(x2))), min(h, int(math.ceil(y2)))
    if x2 - x1 < 2 or y2 - y1 < 2:
        raise CommandError("Пустая область")
    overlays = [o for o in overlays if o in OVERLAYS]
    base = Image.new("RGBA", (x2 - x1, y2 - y1), (236, 236, 236, 255))
    crop_source = source.crop((x1, y1, x2, y2))
    if "original" in overlays or not overlays:
        base.alpha_composite(crop_source)
    crop_labels = labels.crop((x1, y1, x2, y2))
    if "parts" in overlays:
        palette = [0, 0, 0] * 256
        for p in PARTS.values():
            palette[p["value"] * 3: p["value"] * 3 + 3] = list(p["color"])
        palette[UNKNOWN * 3: UNKNOWN * 3 + 3] = list(UNKNOWN_COLOR)
        colored = crop_labels.copy()
        colored.putpalette(palette)
        colored = colored.convert("RGBA")
        alpha = crop_labels.point(lambda v: 0 if v == BACKGROUND else round(255 * (opacity if "original" in overlays else 1)))
        colored.putalpha(alpha)
        base.alpha_composite(colored)
    if "outline" in overlays:  # borders between parts
        edges = ImageChops.difference(crop_labels, ImageChops.offset(crop_labels, 1, 0))
        edges = ImageChops.lighter(edges, ImageChops.difference(crop_labels, ImageChops.offset(crop_labels, 0, 1)))
        line = Image.new("RGBA", base.size, (20, 20, 20, 255))
        line.putalpha(edges.point(lambda v: 220 if v else 0))
        base.alpha_composite(line)
    if "uncertain" in overlays:
        unsure = ImageChops.lighter(project.uncertain(inst["id"]).crop((x1, y1, x2, y2)),
                                    crop_labels.point(lambda v: 255 if v == UNKNOWN else 0))
        mark = Image.new("RGBA", base.size, UNKNOWN_COLOR + (255,))
        stripes = Image.new("L", base.size, 0)
        draw = ImageDraw.Draw(stripes)
        for k in range(-base.height, base.width, 8):
            draw.line((k, 0, k + base.height, base.height), fill=200, width=3)
        mark.putalpha(ImageChops.multiply(unsure, stripes))
        base.alpha_composite(mark)
    scale = min(1.0 * max_side / max(base.size), 8.0)
    if abs(scale - 1) > 1e-6:
        base = base.resize((max(1, round(base.width * scale)), max(1, round(base.height * scale))), Image.LANCZOS)
    if "skeleton" in overlays:
        draw = ImageDraw.Draw(base)
        P = {jid: ((j["xy"][0] - x1) * scale, (j["xy"][1] - y1) * scale) for jid, j in inst["joints"].items()
             if j.get("xy") and j.get("state") != "absent"}
        r = max(2.5, min(7, 3 * scale))
        for a, b in BODY_BONES:
            if a in P and b in P:
                draw.line([P[a], P[b]], fill=(30, 30, 30), width=max(2, round(r * .7)))
        for side in ("left", "right"):
            for chain in HAND_CHAINS:
                pts = [P.get(f"{side}_hand.{c}") for c in chain]
                pts = [p for p in pts if p]
                if len(pts) > 1:
                    draw.line(pts, fill=(250, 250, 250), width=max(1, round(r * .45)))
        for jid, (x, y) in P.items():
            state = inst["joints"][jid].get("state")
            rr = r * (.6 if "_hand." in jid else 1)
            fill = (255, 255, 255) if state == "visible" else (255, 220, 0) if state == "uncertain" else (160, 160, 160)
            draw.ellipse((x - rr, y - rr, x + rr, y + rr), fill=fill, outline=(20, 20, 20))
    return base, {"scale": scale, "offset": [x1, y1], "source_size": [w, h], "region": [x1, y1, x2, y2]}


def layer(project, kind, instance_id=None) -> Image.Image:
    """Full-size transparent layers for the editor: "parts" (colour of each part, unknown in
    magenta), "uncertain" (stripes over guessed pixels), "outline" (borders between parts)."""
    inst = project.instance(instance_id)
    labels = project.labels(inst["id"])
    if kind == "parts":
        palette = [0, 0, 0] * 256
        for p in PARTS.values():
            palette[p["value"] * 3: p["value"] * 3 + 3] = list(p["color"])
        palette[UNKNOWN * 3: UNKNOWN * 3 + 3] = list(UNKNOWN_COLOR)
        colored = labels.copy()
        colored.putpalette(palette)
        colored = colored.convert("RGBA")
        colored.putalpha(labels.point(lambda v: 0 if v == BACKGROUND else 255))
        return colored
    if kind == "uncertain":
        unsure = ImageChops.lighter(project.uncertain(inst["id"]), labels.point(lambda v: 255 if v == UNKNOWN else 0))
        stripes = Image.new("L", labels.size, 0)
        draw = ImageDraw.Draw(stripes)
        for k in range(-labels.height, labels.width, 10):
            draw.line((k, 0, k + labels.height, labels.height), fill=255, width=3)
        mark = Image.new("RGBA", labels.size, UNKNOWN_COLOR + (255,))
        mark.putalpha(ImageChops.multiply(unsure, stripes))
        return mark
    if kind == "outline":
        edges = ImageChops.difference(labels, ImageChops.offset(labels, 1, 0))
        edges = ImageChops.lighter(edges, ImageChops.difference(labels, ImageChops.offset(labels, 0, 1)))
        line = Image.new("RGBA", labels.size, (15, 15, 15, 255))
        line.putalpha(edges.point(lambda v: 255 if v else 0))
        return line
    raise CommandError(f"Нет такого слоя: {kind!r}")


def read_crop(project, region=None, overlays=("original", "parts", "skeleton"), instance_id=None, max_side=768):
    if region is not None and not (isinstance(region, (list, tuple)) and len(region) == 4
                                   and all(isinstance(v, (int, float)) for v in region)):
        raise CommandError("region: [x1, y1, x2, y2] в пикселях исходника")
    if isinstance(overlays, str):
        overlays = [overlays]
    image, transform = render(project, overlays, instance_id, region, max(64, min(2048, int(max_side))))
    folder = project.folder / "previews"
    folder.mkdir(exist_ok=True)
    path = folder / f"crop_{int(time.time() * 1000)}.png"
    _write_atomic(path, _png(image.convert("RGB")))
    for old in sorted(folder.glob("crop_*.png"))[:-30]:
        old.unlink(missing_ok=True)
    return {"revision": project.doc["revision"], "file": str(path), "size": list(image.size), "transform": transform,
            "overlays": list(overlays)}


def read_validate(project, instance_id=None):
    """Diagnostics (not a proof of accuracy): unassigned pixels, empty / scattered parts, joints
    outside their parts, side swaps, hand landmark problems, small hands."""
    inst = project.instance(instance_id)
    labels = project.labels(inst["id"])
    histogram = labels.histogram()
    issues = []
    figure_px = sum(histogram) - histogram[BACKGROUND]
    if histogram[UNKNOWN]:
        share = histogram[UNKNOWN] / max(1, figure_px)
        issues.append({"severity": "warning" if share > .02 else "info", "entity": None, "code": "unknown_pixels",
                       "message": f"Пиксели фигуры без части: {histogram[UNKNOWN]} ({share:.1%})"})
    for pid, part in inst["parts"].items():
        px = histogram[PARTS[pid]["value"]]
        if part["state"] in ("visible", "uncertain") and not px:
            issues.append({"severity": "warning", "entity": pid, "code": "visible_without_pixels",
                           "message": f"{PARTS[pid]['name']}: отмечена видимой, но маски нет"})
        if part["state"] == "uncertain":
            issues.append({"severity": "info", "entity": pid, "code": "uncertain_part",
                           "message": f"{PARTS[pid]['name']}: граница или принадлежность — предположение"})
        for d in part.get("diagnostics", []):
            issues.append({"severity": "info", "entity": pid, "code": d, "message": f"{PARTS[pid]['name']}: {d}"})
    J = {jid: j for jid, j in inst["joints"].items() if j.get("xy") and j.get("state") == "visible"}
    for side, s in (("left", "l"), ("right", "r")):
        for jid, pid in ((f"elbow_{s}", f"{side}_forearm"), (f"knee_{s}", f"{side}_shin")):
            if jid in J and histogram[PARTS[pid]["value"]]:
                x, y = (round(v) for v in J[jid]["xy"])
                if 0 <= x < labels.width and 0 <= y < labels.height and labels.getpixel((x, y)) == BACKGROUND:
                    issues.append({"severity": "warning", "entity": jid, "code": "joint_off_figure",
                                   "message": f"Сустав {jid} стоит вне фигуры"})
        wrist = J.get(f"wrist_{s}")
        hand_wrist = J.get(f"{side}_hand.wrist")
        if wrist and hand_wrist and math.dist(wrist["xy"], hand_wrist["xy"]) > 60:
            issues.append({"severity": "warning", "entity": f"{side}_hand.wrist", "code": "hand_far_from_wrist",
                           "message": f"Запястье кисти ({side}) далеко от запястья тела: возможно, найдена не та кисть"})
    if all(f"shoulder_{s}" in J for s in "lr") and all(f"hip_{s}" in J for s in "lr"):
        view = inst.get("analysis", {}) and (inst["analysis"] or {}).get("view")
        if view == "front" and J["shoulder_l"]["xy"][0] < J["shoulder_r"]["xy"][0]:
            issues.append({"severity": "warning", "entity": "shoulder_l", "code": "side_swap",
                           "message": "Анфас, но левое плечо фигуры слева на картинке: стороны могут быть перепутаны"})
    return {"revision": project.doc["revision"], "instance": inst["id"], "issues": issues}


def read_journal(project, limit=50):
    path = project.folder / "journal.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return {"entries": [json.loads(line) for line in lines[-limit:]]}


def export(project, folder: Path, instance_id=None):
    """Source picture, label map (lossless, one value per part), colour preview, one mask per
    part and the JSON markup, all the same size and coordinates."""
    inst = project.instance(instance_id)
    folder = Path(folder)
    (folder / "masks").mkdir(parents=True, exist_ok=True)
    source_name = f"source{Path(project.doc['source']['file']).suffix}"
    shutil.copy2(project.folder / project.doc["source"]["file"], folder / source_name)
    labels = project.labels(inst["id"])
    labels.save(folder / "labels.png")
    preview, _ = render(project, ("parts",), inst["id"], max_side=max(labels.size))
    preview.save(folder / "labels_color.png")
    files = {}
    for pid in PARTS:
        mask = _part_mask(labels, [PARTS[pid]["value"]])
        if mask.getbbox():
            name = f"masks/{pid}.png"
            mask.save(folder / name)
            files[pid] = name
    markup = {"schema": SCHEMA + ".export", "version": VERSION, "source": {**project.doc["source"], "file": source_name},
              "coordinates": project.doc["coordinates"], "revision": project.doc["revision"],
              "label_map": {"file": "labels.png", "background": BACKGROUND, "unknown": UNKNOWN,
                            "values": {pid: p["value"] for pid, p in PARTS.items()}},
              "instance": {k: v for k, v in inst.items() if k not in ("proposal",)}, "masks": files}
    (folder / "markup.json").write_text(json.dumps(markup, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"folder": str(folder), "masks": len(files), "size": list(labels.size)}


def read_export(project, folder, instance_id=None):
    return export(project, Path(folder), instance_id)


READS = {"get_capabilities": lambda project: read_capabilities(project), "get_document_summary": read_summary,
         "get_parts": read_parts, "get_joints": read_joints, "get_part": read_part, "get_mask": read_mask,
         "get_crop": read_crop, "validate": read_validate, "get_journal": read_journal, "export": read_export}
