"""Development harness: serve web/ in a normal browser with the real Python API over HTTP.

Usage:  python tools/dev_server.py [--folder PATH]
Open the printed URL, then run in the page console (or javascript_tool):
    await import(location.pathname.replace(/web\\/index\\.html$/, '__shim.js'))
The shim defines window.pywebview.api (each call is POST <prefix>/__api/<method>) and
dispatches `pywebviewready`. /poses/ and /thumbs/ work as in the app. Window-only methods
(fullscreen, on-top, compact, folder dialog) are harmless no-ops. Without --folder the active
collection from %APPDATA%\\PoseStage\\config.json is used; set APPDATA to a temp dir to keep the
real settings untouched. --folder is not saved as a collection. Never shipped: build.bat bundles only web/.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import poses  # noqa: E402

SHIM = """
const prefix = location.pathname.replace(/\\/web\\/index\\.html$/, '');
const call = name => async (...args) => {
  const response = await fetch(`${prefix}/__api/${name}`,
    {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(args)});
  if (!response.ok) throw new Error(`${name}: HTTP ${response.status} ${await response.text()}`);
  return response.json();
};
window.pywebview = {api: new Proxy({}, {get: (_, name) => typeof name === 'string' ? call(name) : undefined})};
window.dispatchEvent(new Event('pywebviewready'));
window.dispatchEvent(new Event('focus'));
"""


class DevHandler(app.Handler):
    def do_GET(self):
        if self.path == self.server.catalog.prefix + "/__shim.js" and self.host_allowed():
            body = SHIM.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def do_POST(self):
        prefix = self.server.catalog.prefix + "/__api/"
        name = self.path[len(prefix):] if self.path.startswith(prefix) else ""
        method = getattr(self.server.api, name, None)
        # Host check blocks DNS rebinding; the JSON content type forces a CORS preflight
        # (never answered) for cross-origin pages.
        if not self.host_allowed() or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self.send_error(403)
            return
        if not name or name.startswith("_") or not callable(method):
            self.send_error(404)
            return
        try:
            args = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"[]")
        except ValueError:
            args = None
        if not isinstance(args, list):  # the body is the JSON list of positional arguments
            self.send_error(400)
            return
        try:
            body = json.dumps(method(*args), ensure_ascii=False).encode()
            self.send_response(200)
        except Exception as error:  # surface backend errors to the page
            body = repr(error).encode()
            self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", type=Path, default=None)
    parser.add_argument("--bridge", action="store_true", help="start the agent bridge (agent_bridge.py)")
    options = parser.parse_args()
    app.setup_logging()
    # Like the app run from source: saved collections only, no EXE-folder fallback.
    folder, recursive = (options.folder.resolve(), True) if options.folder else poses.find_source(None)
    catalog = poses.PoseCatalog(folder, "/dev", recursive=recursive)
    threading.Thread(target=catalog.scan, daemon=True).start()
    api = app.Api(catalog, fullscreen=False)
    server = app.LocalServer(catalog, api._service())
    server.RequestHandlerClass = DevHandler
    server.api = api
    if options.bridge:  # the agent bridge too, for trying the MCP adapter against the dev page
        import agent_bridge
        agent_bridge.start(api._service(), poses.DATA_DIR)
    print(f"http://127.0.0.1:{server.server_port}/dev/web/index.html", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
