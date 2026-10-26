"""Agent bridge: the running PoseStage's figure commands for a local AI agent (docs/PARTS.md § AI).

A loopback HTTP endpoint (127.0.0.1, random port) that takes
    POST /v1/call  {"tool": name, "args": {...}}   with  Authorization: Bearer <token>
and answers {"ok": true, "result": …} or {"ok": false, "error": text, "code": code}.
Port and token are in %APPDATA%/PoseStage/agent_bridge.json while the program runs (the file is
readable only by the user's own account like the rest of %APPDATA%). The MCP adapter
(tools/posestage_mcp.py) reads it; any other local client may as well.

Only projects open in PoseStage are reachable, files are written only inside their folders
(previews/, exports/), and the tools are a fixed list: no paths to open, no code, no shell.
Every change is made with source "ai" (locked parts and joints need override_lock) and lands in
the same undo history and journal as the user's own edits.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import threading
import time

import figure_doc as fd

MAX_BODY = 4 * 1024 * 1024
# tool → (kind, figure_doc command / read or service method, description) — the MCP adapter lists these.
WRITE = {"edit_mask": "edit_mask", "refine_mask": "refine_mask", "split_part": "split_part", "merge_parts": "merge_parts",
         "label": "relabel", "set_joint": "set_joint", "set_visibility": "set_visibility", "lock": "set_locked",
         "set_review": "set_review", "set_roi": "set_roi", "apply_proposal": "apply_proposal",
         "discard_proposal": "discard_proposal", "transaction": "transaction", "undo": "undo", "redo": "redo"}
READ = {"get_capabilities": "get_capabilities", "get_document_summary": "get_document_summary", "get_parts": "get_parts",
        "get_joints": "get_joints", "get_part": "get_part", "get_mask": "get_mask", "get_crop": "get_crop",
        "validate": "validate", "get_journal": "get_journal"}
SERVICE = ("list_projects", "analyze", "analysis_status", "cancel_analysis", "export")


def call_tool(service, tool, args):
    """One tool call against the service (also used by the MCP adapter's headless mode)."""
    args = dict(args or {})
    if not isinstance(tool, str):
        raise fd.CommandError("tool: строка")
    if tool == "list_projects":
        return {"projects": service.list_projects()}
    if tool == "get_capabilities" and not service.projects:
        return {**fd.read_capabilities(), "tools": sorted([*WRITE, *READ, *SERVICE])}
    project_id = args.pop("project_id", None)
    expected = args.pop("expected_revision", None)
    if tool in READ:
        result = service.call(project_id, READ[tool], args, source="ai")
        if tool == "get_capabilities":
            result = {**result, "tools": sorted([*WRITE, *READ, *SERVICE])}
        return result
    if tool in WRITE:
        command = WRITE[tool]
        if command == "set_locked":
            args.setdefault("locked", True)
        return service.call(project_id, command, args, source="ai", expected_revision=expected)
    if tool == "analyze":
        return service.analyze(project_id, args.get("instance_id"), args.get("mode", "full"), source="ai")
    if tool == "analysis_status":
        return service.job(args.get("job_id"))
    if tool == "cancel_analysis":
        return service.cancel(args.get("job_id"))
    if tool == "export":  # only into the project's own exports/ folder
        project = service.project(project_id)
        folder = project.folder / "exports" / time.strftime("%Y%m%d-%H%M%S")
        return fd.export(project, folder, args.get("instance_id"))
    raise fd.CommandError(f"Неизвестный инструмент {tool!r}", "unknown_command")


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, service, token):
        self.service, self.token = service, token
        super().__init__(("127.0.0.1", 0), BridgeHandler)


class BridgeHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _reply(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply(405, {"ok": False, "error": "POST /v1/call", "code": "method"})

    def do_POST(self):
        if self.headers.get("Host", "") != f"127.0.0.1:{self.server.server_port}" or self.headers.get("Origin"):
            return self._reply(403, {"ok": False, "error": "forbidden", "code": "forbidden"})  # no browser pages
        token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(token.encode(), self.server.token.encode()):
            return self._reply(401, {"ok": False, "error": "bad token", "code": "unauthorized"})
        if self.path != "/v1/call":
            return self._reply(404, {"ok": False, "error": "POST /v1/call", "code": "not_found"})
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            return self._reply(413, {"ok": False, "error": "body size", "code": "invalid"})
        try:
            request = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(request, dict) or not isinstance(request.get("args", {}), dict):
                raise ValueError("{tool, args}")
            result = call_tool(self.server.service, request.get("tool"), request.get("args"))
            self._reply(200, {"ok": True, "result": result})
        except fd.CommandError as error:
            self._reply(200, {"ok": False, "error": str(error), "code": error.code})
        except (ValueError, TypeError) as error:
            self._reply(200, {"ok": False, "error": str(error), "code": "invalid"})
        except Exception as error:  # noqa: BLE001
            logging.exception("Agent bridge call failed")
            self._reply(500, {"ok": False, "error": str(error), "code": "internal"})


def start(service, data_dir: Path):
    """Starts the bridge and writes its address file; returns a stop() function."""
    token = secrets.token_urlsafe(32)
    server = BridgeServer(service, token)
    threading.Thread(target=server.serve_forever, name="agent-bridge", daemon=True).start()
    info_file = Path(data_dir) / "agent_bridge.json"
    info_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = info_file.with_suffix(".tmp")
    tmp.write_text(json.dumps({"url": f"http://127.0.0.1:{server.server_port}/v1/call", "port": server.server_port,
                               "token": token, "pid": os.getpid(), "version": fd.VERSION}), encoding="utf-8")
    os.replace(tmp, info_file)
    logging.info("Agent bridge on 127.0.0.1:%s", server.server_port)

    def stop():
        server.shutdown()
        server.server_close()
        try:
            if json.loads(info_file.read_text(encoding="utf-8")).get("pid") == os.getpid():
                info_file.unlink()
        except (OSError, ValueError):
            pass
    return stop
