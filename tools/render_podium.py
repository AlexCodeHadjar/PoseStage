"""Regenerates web/desktop/podium.png and podium.json (desktop mode) from the real 3D scene.

Usage: python tools/render_podium.py, then open the printed URL in a browser (Chrome/Edge).
The page renders the podium with Three.js and posts the result back; the server writes the
files and exits. Run it again whenever web/scene/stage.js or placement.js changes.
"""
from __future__ import annotations

import base64
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "web" / "desktop"


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        if self.path != "/save" or not self.headers.get("Host", "").startswith("127.0.0.1:"):
            self.send_error(404)
            return
        data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "podium.png").write_bytes(base64.b64decode(data["png"].split(",", 1)[1]))
        (OUT / "podium.json").write_text(json.dumps(data["meta"], indent=1), encoding="utf-8")
        self.send_response(204)
        self.end_headers()
        print(f"Saved {OUT / 'podium.png'} and podium.json", flush=True)
        threading.Thread(target=self.server.shutdown, daemon=True).start()


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(ROOT)))
    print(f"http://127.0.0.1:{server.server_port}/tools/render_podium.html?save", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
