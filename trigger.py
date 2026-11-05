# ============================================================
# Market Agent V4.5 — Remote Trigger + File API
# Port 8080
# POST /run           — trigger agent run
# GET  /file?name=X   — read file from server
# POST /file?name=X   — write file to server
# ============================================================

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import subprocess
import threading
import json
import os
import sys
import uuid
import hmac
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN    = open(os.path.join(BASE_DIR, ".trigger_token")).read().strip()
# Optional view-only token: can read files (dashboard view) but cannot start
# runs, record trades, reset the simulator or overwrite files.
_read_path = os.path.join(BASE_DIR, ".trigger_token_read")
TOKEN_READ = open(_read_path).read().strip() if os.path.exists(_read_path) else None
RUN_COOLDOWN_SECONDS = 120   # minimum gap between accepted paid runs (on top of the run lock)
_last_run_started = {"t": 0.0}


def token_ok(supplied, allow_read=False):
    """Constant-time comparison (a plain == leaks timing)."""
    supplied = str(supplied or "")
    if hmac.compare_digest(supplied, TOKEN):
        return True
    return bool(allow_read and TOKEN_READ and hmac.compare_digest(supplied, TOKEN_READ))

TRADE_HISTORY_PATH = os.path.join(BASE_DIR, "logs", "trade_history.json")
_trade_history_lock = threading.Lock()  # atomic append — several traders/tabs could hit this concurrently

# Readable via GET /file?name=X, but deliberately NOT in ALLOWED_FILES below —
# writes must go through /record-trade so every entry is validated and
# appended atomically, never overwritten wholesale from the client.
READ_ONLY_FILES = {
    "logs/trade_history.json":   TRADE_HISTORY_PATH,
    "logs/simulator_state.json": os.path.join(BASE_DIR, "logs", "simulator_state.json"),
}

ALLOWED_FILES = {
    "watchlist.csv":                os.path.join(BASE_DIR, "watchlist.csv"),
    "performance_log.json":         os.path.join(BASE_DIR, "logs", "performance_log.json"),
    "closed_positions.json":        os.path.join(BASE_DIR, "logs", "closed_positions.json"),
    "performance_history.json":     os.path.join(BASE_DIR, "logs", "performance_history.json"),
    "logs/premarket_brief.json":    os.path.join(BASE_DIR, "logs", "premarket_brief.json"),
    "logs/thesis_log.json":         os.path.join(BASE_DIR, "logs", "thesis_log.json"),
    "logs/outcome_log.json":        os.path.join(BASE_DIR, "logs", "outcome_log.json"),
    "logs/correlation_matrix.json": os.path.join(BASE_DIR, "logs", "correlation_matrix.json"),
    "logs/stock_insight_status.json": os.path.join(BASE_DIR, "logs", "stock_insight_status.json"),
    "logs/agent_run_status.json":     os.path.join(BASE_DIR, "logs", "agent_run_status.json"),
    "logs/premarket_status.json":     os.path.join(BASE_DIR, "logs", "premarket_status.json"),
    "logs/universe_refresh_status.json": os.path.join(BASE_DIR, "logs", "universe_refresh_status.json"),
    "data/universe_master.json":     os.path.join(BASE_DIR, "data", "universe_master.json"),
}

RUN_LOCK_DIR = os.path.join(BASE_DIR, "logs", "run.lock")


def run_in_progress():
    """(pid, what) of the run holding run_agent.sh / premarket_run.sh's lock, or None."""
    try:
        pid = int(open(os.path.join(RUN_LOCK_DIR, "pid")).read().strip())
        os.kill(pid, 0)  # raises if the process is gone (stale lock)
        what = open(os.path.join(RUN_LOCK_DIR, "what")).read().strip()
        return pid, what
    except Exception:
        return None


class TriggerHandler(BaseHTTPRequestHandler):

    def _cooling_down(self):
        """429 if a paid run was started less than RUN_COOLDOWN_SECONDS ago."""
        wait = RUN_COOLDOWN_SECONDS - (time.time() - _last_run_started["t"])
        if wait <= 0:
            _last_run_started["t"] = time.time()
            return False
        self.send_response(429)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(json.dumps({"status": "cooldown",
                                     "message": f"A run was started moments ago — try again in {int(wait)}s"}).encode())
        return True

    def _busy(self):
        """409 if a paid run is already going — no second pipeline, no double spend."""
        busy = run_in_progress()
        if not busy:
            return False
        self.send_response(409)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(json.dumps({"status": "busy",
                                     "message": f"A {busy[1]} run is already in progress"}).encode())
        return True


    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Token")

    def _auth(self, allow_read=False):
        return token_ok(self.headers.get("X-Token", ""), allow_read=allow_read)

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    # ── GET /file?name=X ──────────────────────────────────────
    def do_GET(self):
        path = self.path.split("?")[0]
        qs   = self.path.split("?")[1] if "?" in self.path else ""
        params = dict(p.split("=") for p in qs.split("&") if "=" in p)

        if path != "/file":
            self.send_response(404); self.end_headers(); return

        if not self._auth(allow_read=True):
            self.send_response(401); self.end_headers()
            self.wfile.write(b"Unauthorized"); return

        name = params.get("name","")
        if name not in ALLOWED_FILES and name not in READ_ONLY_FILES:
            self.send_response(400); self.end_headers()
            self.wfile.write(b"Unknown file"); return

        fpath = ALLOWED_FILES.get(name) or READ_ONLY_FILES.get(name)
        if not os.path.exists(fpath):
            self.send_response(404); self.end_headers()
            self.wfile.write(b"File not found"); return

        with open(fpath, "rb") as f:
            content = f.read()

        ctype = "application/json" if name.endswith(".json") else "text/csv"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(content))
        self._cors()
        self.end_headers()
        self.wfile.write(content)

    # ── POST /run or /file?name=X ─────────────────────────────
    def do_POST(self):
        path = self.path.split("?")[0]
        qs   = self.path.split("?")[1] if "?" in self.path else ""
        params = dict(p.split("=") for p in qs.split("&") if "=" in p)

        length  = int(self.headers.get("Content-Length", 0))
        raw     = self.rfile.read(length) if length else b""

        # ── /reset-simulator — archive current comparison, start fresh ──
        if path == "/reset-simulator":
            try:
                body = json.loads(raw) if raw else {}
            except Exception:
                body = {}
            if not token_ok(body.get("token")):
                self.send_response(401); self.end_headers()
                self.wfile.write(b"Unauthorized"); return

            state_path = os.path.join(BASE_DIR, "logs", "simulator_state.json")
            archived = False
            if os.path.exists(state_path):
                archive_name = f"simulator_state_archive_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                os.rename(state_path, os.path.join(BASE_DIR, "logs", archive_name))
                archived = True

            # Re-running simulator.py right here (not backgrounded — this
            # completes almost instantly, since it only reads the CURRENT
            # latest.json already on disk, no new market run or API call).
            # simulator.py itself detects the missing state file and
            # re-initializes fresh from today's real portfolio automatically
            # — no separate reset logic needed, this just reuses that path.
            try:
                subprocess.run(
                    [sys.executable, os.path.join(BASE_DIR, "simulator.py")],
                    cwd=BASE_DIR, timeout=30
                )
                ok = True
            except Exception:
                ok = False

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "done",
                "success": ok,
                "archived": archived,
            }).encode())
            return

        # ── /run — trigger agent ──────────────────────────────
        if path == "/run":
            try:
                body = json.loads(raw) if raw else {}
            except Exception:
                body = {}
            if not token_ok(body.get("token")):
                self.send_response(401); self.end_headers()
                self.wfile.write(b"Unauthorized"); return
            if self._busy() or self._cooling_down():
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(json.dumps({
                "status":"started",
                "message":"Agent run triggered"
            }).encode())

            def run():
                subprocess.run(
                    ["bash", os.path.join(BASE_DIR, "run_agent.sh")],
                    cwd=BASE_DIR
                )
            threading.Thread(target=run, daemon=True).start()
            return

        # ── /premarket-run — trigger pre-market scan ──────────────
        if path == "/premarket-run":
            try:
                body = json.loads(raw) if raw else {}
            except Exception:
                body = {}
            if not token_ok(body.get("token")):
                self.send_response(401); self.end_headers()
                self.wfile.write(b"Unauthorized"); return
            if self._busy() or self._cooling_down():
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(json.dumps({"status":"started","message":"Pre-market scan triggered"}).encode())
            def run_premarket():
                subprocess.run(["bash", os.path.join(BASE_DIR, "premarket_run.sh")], cwd=BASE_DIR)
            threading.Thread(target=run_premarket, daemon=True).start()
            return

        # ── /stock-insight — on-demand intraday stock/sector/market insight ──
        if path == "/stock-insight":
            try:
                body = json.loads(raw) if raw else {}
            except Exception:
                body = {}
            if not token_ok(body.get("token")):
                self.send_response(401); self.end_headers()
                self.wfile.write(b"Unauthorized"); return

            ticker = str(body.get("ticker", "")).strip().upper()
            # Basic sanity check — letters/digits/dot/hyphen only (covers tickers like BRK.B), reasonable length
            if not ticker or len(ticker) > 10 or not all(c.isalnum() or c in ".-" for c in ticker):
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self._cors()
                self.end_headers()
                self.wfile.write(json.dumps({"status":"error","message":"Invalid ticker"}).encode())
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(json.dumps({"status":"started","message":f"Stock insight for {ticker} triggered"}).encode())
            def run_insight():
                subprocess.run([sys.executable, os.path.join(BASE_DIR, "stock_insight.py"), ticker], cwd=BASE_DIR)
            threading.Thread(target=run_insight, daemon=True).start()
            return

        # ── /refresh-universe — rebuild the 300-stock master candidate list ──
        if path == "/refresh-universe":
            try:
                body = json.loads(raw) if raw else {}
            except Exception:
                body = {}
            if not token_ok(body.get("token")):
                self.send_response(401); self.end_headers()
                self.wfile.write(b"Unauthorized"); return

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(json.dumps({"status":"started","message":"Universe refresh triggered"}).encode())
            def run_refresh():
                subprocess.run([sys.executable, os.path.join(BASE_DIR, "refresh_universe.py")], cwd=BASE_DIR)
            threading.Thread(target=run_refresh, daemon=True).start()
            return

        # ── /record-trade — append one real-portfolio trade to the ──
        # permanent, immutable trade_history.json ledger. This is the
        # single source of truth for "Salim's actions" that
        # simulator.py's Adaptive ledger reads as an additional
        # learning input — separate from watchlist.csv (current state,
        # mutable) and closed_positions.json (sells only, no
        # commission). Every BUY and SELL from the dashboard's Record
        # Trade modal is appended here, atomically, and never edited
        # or overwritten by any other endpoint.
        if path == "/record-trade":
            try:
                body = json.loads(raw) if raw else {}
            except Exception:
                body = {}
            if not token_ok(body.get("token")):
                self.send_response(401); self.end_headers()
                self.wfile.write(b"Unauthorized"); return

            ticker     = str(body.get("ticker", "")).strip().upper()
            action     = str(body.get("action", "")).strip().upper()
            quantity   = body.get("quantity")
            price      = body.get("price")
            commission = body.get("commission", 0)
            trade_date = body.get("trade_date") or datetime.now().isoformat()
            sector     = body.get("sector") or ""
            note       = str(body.get("note", ""))[:300]

            def bad(msg):
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self._cors()
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": msg}).encode())

            if not ticker or len(ticker) > 10 or not all(c.isalnum() or c in ".-" for c in ticker):
                bad("Invalid ticker"); return
            if action not in ("BUY", "SELL"):
                bad("action must be BUY or SELL"); return
            try:
                quantity = float(quantity)
                price = float(price)
                commission = float(commission) if commission not in (None, "") else 0.0
            except (TypeError, ValueError):
                bad("quantity, price, and commission must be numbers"); return
            if quantity <= 0 or price <= 0 or commission < 0:
                bad("quantity and price must be > 0, commission must be >= 0"); return

            entry = {
                "id":          uuid.uuid4().hex[:12],
                "recorded_at": datetime.now().isoformat(),
                "trade_date":  trade_date,
                "ticker":      ticker,
                "action":      action,
                "quantity":    quantity,
                "price":       price,
                "commission":  commission,
                "sector":      sector,
                "note":        note,
            }

            with _trade_history_lock:
                history = []
                if os.path.exists(TRADE_HISTORY_PATH):
                    try:
                        with open(TRADE_HISTORY_PATH, "r") as f:
                            history = json.load(f)
                    except Exception:
                        history = []
                history.append(entry)
                os.makedirs(os.path.dirname(TRADE_HISTORY_PATH), exist_ok=True)
                with open(TRADE_HISTORY_PATH, "w") as f:
                    json.dump(history, f, indent=2)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(json.dumps({"status": "recorded", "entry": entry}).encode())
            return

        # ── /file — write file ────────────────────────────────
        if path == "/file":
            if not self._auth():
                self.send_response(401); self.end_headers()
                self.wfile.write(b"Unauthorized"); return

            name = params.get("name","")
            if name not in ALLOWED_FILES:
                self.send_response(400); self.end_headers()
                self.wfile.write(b"Unknown file"); return

            fpath = ALLOWED_FILES[name]
            os.makedirs(os.path.dirname(fpath), exist_ok=True)
            with open(fpath, "wb") as f:
                f.write(raw)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(json.dumps({
                "status":"saved",
                "file": name,
                "bytes": len(raw)
            }).encode())
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress access logs

if __name__ == "__main__":
    # Localhost only — local/local_server.py proxies /api/* here. Set TRIGGER_HOST=0.0.0.0
    # only if you deliberately want the API reachable from other machines.
    host = os.getenv("TRIGGER_HOST", "127.0.0.1")
    server = ThreadingHTTPServer((host, 8080), TriggerHandler)
    print(f"Market Agent API running on {host}:8080")
    server.serve_forever()
