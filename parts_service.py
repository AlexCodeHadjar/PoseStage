"""Open figure projects, analysis jobs and the analysis runtime (docs/PARTS.md).

Shared by the window (app.Api), the agent bridge (agent_bridge.py) and the MCP adapter's
headless mode (tools/posestage_mcp.py): all of them change documents through
figure_doc.Project.execute, so the UI and an AI agent have the same commands, undo and journal.

The analysis itself (MediaPipe, numpy, scipy) is NOT part of PoseStage.exe. It runs as a separate
process from an analysis runtime found in this order:
    1. env POSESTAGE_ANALYZER = a python.exe (runs parts_analyzer.py) or an analyzer .exe;
    2. %APPDATA%/PoseStage/analyzer.json {"python": …} or {"exe": …} (tools/setup_analyzer.py writes it);
    3. PoseStageAnalyzer/PoseStageAnalyzer.exe next to PoseStage.exe (a separate model pack).
Without one, analysis reports "unavailable" and everything else (editing, export) still works.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import threading
import time

from PIL import Image

import figure_doc as fd

DATA_DIR = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / "PoseStage"
FIGURES_DIR = DATA_DIR / "figures"
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
JOB_TIMEOUT = 300  # s; one analysis takes 2–10 s


def find_analyzer(exe_dir: Path | None = None) -> dict:
    """{"available", "command" (argv before <request> <out>), "source", "reason"}."""
    script = RESOURCE_DIR / "parts_analyzer.py"

    def from_path(path, source):
        path = Path(path)
        if not path.is_file():
            return {"available": False, "command": None, "source": source, "reason": f"Файл не найден: {path}"}
        if path.name.lower().startswith("python"):
            if not script.is_file():
                return {"available": False, "command": None, "source": source, "reason": "Нет parts_analyzer.py"}
            return {"available": True, "command": [str(path), "-I", str(script)], "source": source, "reason": None}
        return {"available": True, "command": [str(path)], "source": source, "reason": None}

    if os.environ.get("POSESTAGE_ANALYZER"):
        return from_path(os.environ["POSESTAGE_ANALYZER"], "env")
    try:
        config = json.loads((DATA_DIR / "analyzer.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = None
    if isinstance(config, dict) and (config.get("python") or config.get("exe")):
        return from_path(config.get("python") or config.get("exe"), "config")
    if exe_dir is not None:
        bundled = Path(exe_dir) / "PoseStageAnalyzer" / "PoseStageAnalyzer.exe"
        if bundled.is_file():
            return from_path(bundled, "bundled")
    return {"available": False, "command": None, "source": None,
            "reason": "Модуль анализа не установлен (см. docs/PARTS.md, «Установка анализа»)"}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class Job:
    def __init__(self, project_id, instance_id, mode, source):
        self.id = secrets.token_hex(6)
        self.project_id, self.instance_id, self.mode, self.source = project_id, instance_id, mode, source
        self.status, self.progress, self.stage, self.error, self.result = "queued", 0.0, "", None, None
        self.started = time.time()
        self.process: subprocess.Popen | None = None
        self.cancelled = False

    def public(self):
        return {"job_id": self.id, "project_id": self.project_id, "instance_id": self.instance_id, "mode": self.mode,
                "status": self.status, "progress": round(self.progress, 3), "stage": self.stage, "error": self.error,
                "result": self.result, "seconds": round(time.time() - self.started, 1)}


class PartsService:
    def __init__(self, exe_dir: Path | None = None, figures_dir: Path = FIGURES_DIR):
        self.exe_dir = exe_dir
        self.figures_dir = Path(figures_dir)
        self.projects: dict[str, fd.Project] = {}
        self.jobs: dict[str, Job] = {}
        self.lock = threading.RLock()
        self.on_change = None  # callable(change dict) — the window refreshes; the bridge does not need it

    # --- projects ---
    def _register(self, project: fd.Project) -> dict:
        pid = project.doc["id"]
        with self.lock:
            if pid in self.projects and self.projects[pid] is not project:
                return self.summary(pid)
            lock = project.folder / "figure.lock"
            try:
                other = int(lock.read_text(encoding="ascii").strip() or 0)
            except (OSError, ValueError):
                other = 0
            if other and other != os.getpid() and _pid_alive(other):
                raise fd.CommandError("Проект уже открыт в другом процессе PoseStage", "busy")
            lock.write_text(str(os.getpid()), encoding="ascii")
            self.projects[pid] = project
            project.listeners.append(lambda change, pid=pid: self._changed({**change, "project_id": pid}))
            self._remember(project)
        return self.summary(pid)

    def _changed(self, change):
        if self.on_change:
            try:
                self.on_change(change)
            except Exception:  # noqa: BLE001
                logging.exception("Parts change listener failed")

    def _remember(self, project):
        recent_file = self.figures_dir / "recent.json"
        try:
            recent = json.loads(recent_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            recent = []
        entry = {"id": project.doc["id"], "name": project.doc.get("name"), "folder": str(project.folder)}
        recent = [entry] + [r for r in recent if isinstance(r, dict) and r.get("folder") != entry["folder"]][:29]
        self.figures_dir.mkdir(parents=True, exist_ok=True)
        recent_file.write_text(json.dumps(recent, ensure_ascii=False, indent=1), encoding="utf-8")

    def recent(self):
        try:
            recent = json.loads((self.figures_dir / "recent.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [r for r in recent if isinstance(r, dict) and (Path(r.get("folder", "")) / "figure.json").is_file()]

    def open_folder(self, folder) -> dict:
        return self._register(fd.Project.open(Path(folder)))

    def create(self, image: Path, name=None, joints=None, folder: Path | None = None) -> dict:
        """A project for a picture; the same picture (same sha256) reopens its project."""
        image = Path(image)
        digest = fd.sha256_file(image)
        folder = Path(folder) if folder else self.figures_dir / digest[:16]
        if (folder / "figure.json").is_file():
            return self.open_folder(folder)
        project = fd.Project.create(image, folder, name=name)
        if joints:  # e.g. the library's skeleton: a starting point, marked as estimated
            inst = project.instance()
            for jid, point in joints.items():
                if jid in fd.JOINT_IDS and point.get("xy") and point.get("state", "visible") != "absent":
                    inst["joints"][jid] = {"xy": [round(float(v), 2) for v in point["xy"]],
                                           "state": "uncertain" if point.get("state") == "uncertain" else "visible",
                                           "score": None, "locked": False, "review": "unreviewed",
                                           "source": {"kind": "estimated", "method": "library skeleton"}}
            project.save()
        return self._register(project)

    def close(self, project_id):
        with self.lock:
            project = self.projects.pop(project_id, None)
        if project is not None:
            (project.folder / "figure.lock").unlink(missing_ok=True)
        return {"closed": project is not None}

    def close_all(self):
        for pid in list(self.projects):
            self.close(pid)

    def project(self, project_id=None) -> fd.Project:
        with self.lock:
            if project_id is None and len(self.projects) == 1:
                return next(iter(self.projects.values()))
            if project_id not in self.projects:
                open_ids = ", ".join(self.projects) or "нет открытых"
                raise fd.CommandError(f"Проект {project_id!r} не открыт ({open_ids})", "not_found")
            return self.projects[project_id]

    def summary(self, project_id=None):
        project = self.project(project_id)
        job = self.active_job(project.doc["id"])
        return {**project.execute("get_document_summary"), "job": job.public() if job else None,
                "analyzer": self.analyzer_status()}

    def list_projects(self):
        with self.lock:
            items = list(self.projects.values())
        return [{"project_id": p.doc["id"], "name": p.doc.get("name"), "folder": str(p.folder), "revision": p.doc["revision"],
                 "instances": [i["id"] for i in p.doc["instances"]]} for p in items]

    def call(self, project_id, command, args=None, source="user", expected_revision=None):
        return self.project(project_id).execute(command, args or {}, source=source, expected_revision=expected_revision)

    # --- analysis ---
    def analyzer_status(self):
        found = find_analyzer(self.exe_dir)
        return {"available": found["available"], "source": found["source"], "reason": found["reason"]}

    def active_job(self, project_id):
        with self.lock:
            return next((j for j in self.jobs.values() if j.project_id == project_id and j.status in ("queued", "running")), None)

    def analyze(self, project_id=None, instance_id=None, mode="full", source="user"):
        """Starts a background analysis ("full": landmarks and masks; "masks": masks from the
        document's current joints). The result goes in directly while the figure is untouched,
        otherwise as a proposal (locked parts and joints are kept either way)."""
        if mode not in ("full", "masks"):
            raise fd.CommandError("mode: full | masks")
        project = self.project(project_id)
        inst = project.instance(instance_id)
        found = find_analyzer(self.exe_dir)
        if not found["available"]:
            raise fd.CommandError(found["reason"], "unavailable")
        with self.lock:
            running = self.active_job(project.doc["id"])
            if running:
                raise fd.CommandError("Анализ этой фигуры уже идёт", "busy")
            job = Job(project.doc["id"], inst["id"], mode, source)
            self.jobs[job.id] = job
            for old in [j for j in self.jobs.values() if j.status not in ("queued", "running")][:-20]:
                self.jobs.pop(old.id, None)
        threading.Thread(target=self._run, args=(job, project, found["command"]), daemon=True, name=f"analysis-{job.id}").start()
        return job.public()

    def job(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
        if job is None:
            raise fd.CommandError(f"Нет задачи {job_id!r}", "not_found")
        return job.public()

    def cancel(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
        if job is None:
            raise fd.CommandError(f"Нет задачи {job_id!r}", "not_found")
        job.cancelled = True
        if job.process and job.process.poll() is None:
            job.process.kill()
        return job.public()

    def _run(self, job: Job, project: fd.Project, command):
        work = project.folder / ".analysis" / job.id
        try:
            work.mkdir(parents=True, exist_ok=True)
            inst = project.instance(job.instance_id)
            request = {"image": str(project.folder / project.doc["source"]["file"]), "roi": inst["roi"], "mode": job.mode,
                       "joints": inst["joints"]}
            (work / "request.json").write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
            job.status = "running"
            self._changed({"project_id": job.project_id, "job": job.public()})
            flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
            with open(work / "stderr.txt", "wb") as errors:
                job.process = subprocess.Popen([*command, str(work / "request.json"), str(work / "out")],
                                               stdout=subprocess.PIPE, stderr=errors, stdin=subprocess.DEVNULL,
                                               creationflags=flags, cwd=str(work))
                timer = threading.Timer(JOB_TIMEOUT, lambda: job.process.poll() is None and job.process.kill())
                timer.start()
                for raw in job.process.stdout:
                    try:
                        line = json.loads(raw.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    if isinstance(line, dict) and "progress" in line:
                        job.progress, job.stage = float(line["progress"]), str(line.get("stage", ""))
                        self._changed({"project_id": job.project_id, "job": job.public()})
                code = job.process.wait()
                timer.cancel()
            if job.cancelled:
                job.status = "cancelled"
                return
            if code != 0:
                tail = (work / "stderr.txt").read_text(encoding="utf-8", errors="replace")[-1500:]
                raise RuntimeError(f"Анализатор завершился с кодом {code}: {tail.strip().splitlines()[-1] if tail.strip() else ''}")
            out = work / "out"
            result = json.loads((out / "result.json").read_text(encoding="utf-8"))
            info = result.get("analysis") or {}
            if info.get("error"):
                raise RuntimeError({"body not found": "Тело не найдено: выберите область фигуры или расставьте суставы и "
                                                      "пересчитайте маски"}.get(info["error"], info["error"]))
            stored = project.execute("store_analysis", {"instance_id": job.instance_id, "result": {
                "labels": Image.open(out / "labels.png").convert("L"), "uncertain": Image.open(out / "uncertain.png").convert("L"),
                "parts": result["parts"], "joints": result["joints"], "analysis": {**info, "mode": job.mode, "job": job.id,
                                                                                   "time": time.strftime("%Y-%m-%dT%H:%M:%S")}}},
                source="model")
            job.result = {"applied": stored.get("applied", False), "proposal": stored.get("proposal", False),
                          "revision": stored.get("revision"), "seconds": info.get("seconds"), "view": info.get("view"),
                          "hands": info.get("hands")}
            job.progress, job.status = 1.0, "done"
        except Exception as error:  # noqa: BLE001 — reported to the caller
            if job.cancelled:
                job.status = "cancelled"
            else:
                logging.exception("Analysis failed")
                job.status, job.error = "error", str(error)
        finally:
            shutil.rmtree(work, ignore_errors=True)
            self._changed({"project_id": job.project_id, "job": job.public()})

    # --- animation frames (docs/RIG.md § Анимация) ---
    def export_frames(self, project_id=None, folder=None, instance_id=None, options=None, source="user"):
        """A background job writing the animation's PNG frames into `folder` (default: the
        project's exports/); same Job interface as analysis (job, cancel)."""
        import timeline
        project = self.project(project_id)
        inst = project.instance(instance_id)
        options = dict(options or {})
        allowed = {"start", "end", "fps", "max_side", "background", "overlays"}
        if set(options) - allowed:
            raise fd.CommandError(f"options: {', '.join(sorted(allowed))}")
        folder = Path(folder) if folder else project.folder / "exports" / f"{time.strftime('%Y%m%d-%H%M%S')}-frames"
        with self.lock:
            if self.active_job(project.doc["id"]):
                raise fd.CommandError("Для этой фигуры уже идёт задача", "busy")
            job = Job(project.doc["id"], inst["id"], "frames", source)
            self.jobs[job.id] = job

        def work():
            job.status = "running"
            try:
                def progress(done, total):
                    job.progress, job.stage = done / total, f"кадр {done} из {total}"
                    self._changed({"project_id": job.project_id, "job": job.public()})
                job.result = timeline.export_frames(project, folder, inst["id"], progress=progress,
                                                    cancelled=lambda: job.cancelled, **options)
                job.status, job.progress = "done", 1.0
            except Exception as error:  # noqa: BLE001
                if job.cancelled:
                    job.status = "cancelled"
                else:
                    logging.exception("Frame export failed")
                    job.status, job.error = "error", str(error)
            finally:
                self._changed({"project_id": job.project_id, "job": job.public()})
        threading.Thread(target=work, daemon=True, name=f"frames-{job.id}").start()
        return job.public()

    # --- images for the window ---
    def layer_png(self, project_id, instance_id, kind) -> bytes:
        project = self.project(project_id)
        with project.lock:
            if kind == "source":
                image = project.source()
            elif kind == "posed":  # the current pose, full size, transparent (docs/RIG.md)
                import rig_doc
                image, _ = rig_doc.render_pose(project, instance_id, overlays=("pose",), background=(0, 0, 0, 0),
                                               max_side=max(project.doc["source"]["width"], project.doc["source"]["height"]))
            elif kind == "rig_labels":  # labels with the unknown pixels given to a part, as the mesh uses them
                import rig_doc
                image = rig_doc.rig_of(project, instance_id).mesh.labels
            elif kind.startswith("s_"):
                image = project.map(f"{instance_id}_{kind}")
                if image is None:
                    raise fd.CommandError("Нет дорисовки", "not_found")
            elif kind.startswith("w_"):
                import rig_doc
                image = rig_doc.weights_layer(project, instance_id, kind[2:])
            else:
                image = fd.layer(project, kind, instance_id)
            return fd._png(image)
