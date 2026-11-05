# ============================================================
# Market Agent — Laptop dashboard server
#
#   /             → Market_Agent.html
#   /latest.json  → logs/latest.json
#   /api/*        → trigger.py on 127.0.0.1:8080, with /api stripped
#   anything else → 404
#
# Listens on 127.0.0.1:8000 only — reachable from this Mac, not the network.
# Started by local/start.sh; run directly with: venv/bin/python local/local_server.py
# ============================================================

import os
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD   = os.path.join(BASE_DIR, "Market_Agent.html")
LATEST      = os.path.join(BASE_DIR, "logs", "latest.json")
TRIGGER_API = "http://127.0.0.1:8080"
PORT        = int(os.getenv("DASHBOARD_PORT", "8000"))

# Headers passed through to trigger.py — the same ones the dashboard sends
FORWARD_HEADERS = ("x-token", "content-type")


class LocalHandler(BaseHTTPRequestHandler):

    def _send(self, code, body, ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, ctype):
        if not os.path.exists(path):
            return self._send(404, b"File not found")
        with open(path, "rb") as f:
            self._send(200, f.read(), ctype)

    def _proxy(self, method):
        length = int(self.headers.get("Content-Length", 0))
        data   = self.rfile.read(length) if length else None
        req = urllib.request.Request(
            TRIGGER_API + self.path[len("/api"):],
            data=data,
            method=method,
            headers={k: v for k, v in self.headers.items() if k.lower() in FORWARD_HEADERS},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                self._send(r.status, r.read(), r.headers.get("Content-Type", "application/json"))
        except urllib.error.HTTPError as e:
            self._send(e.code, e.read())
        except urllib.error.URLError:
            self._send(502, b"trigger.py is not running on port 8080")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/api/"):
            return self._proxy("GET")
        if path in ("/", "/Market_Agent.html"):
            return self._send_file(DASHBOARD, "text/html; charset=utf-8")
        if path == "/latest.json":
            return self._send_file(LATEST, "application/json")
        self._send(404, b"Not found")

    def do_POST(self):
        if self.path.startswith("/api/"):
            return self._proxy("POST")
        self._send(404, b"Not found")

    def log_message(self, format, *args):
        pass  # quiet, like trigger.py


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), LocalHandler)
    print(f"Market Agent dashboard on http://localhost:{PORT}")
    server.serve_forever()
