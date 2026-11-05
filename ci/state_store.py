#!/usr/bin/env python3
# ============================================================
# MARKET AGENT V4.5 — Encrypted state in a secret Gist
#
# The public repo holds code only. Everything personal (watchlist.csv,
# logs/, data/) lives in one secret Gist as two encrypted files:
#   state.enc      full archive the GitHub Actions run restores and saves
#   dashboard.enc  just the files the dashboard reads, as one JSON bundle
# A secret Gist is only unlisted, so both are AES-256-GCM encrypted with a
# key derived (PBKDF2-SHA256) from your passphrase. The envelope matches
# the browser's WebCrypto, so the dashboard decrypts it with the same
# passphrase (same scheme as Mandoos).
#
#   state_store.py pull [--app DIR]    restore state into the app folder
#                                      (laptop: copies GitHub's latest results
#                                      into ~/market-agent)
#   state_store.py push                save state + dashboard bundle
#   state_store.py seed --from DIR     first upload from the laptop install
#
# Environment: MA_PASSPHRASE, MA_GIST_ID, MA_GIST_TOKEN (classic token,
# gist scope). seed prompts for the passphrase and can use `gh auth token`.
# MA_STORE_DIR=<folder> stores the two files in a local folder instead of
# the Gist (for testing).
# ============================================================

import argparse
import base64
import getpass
import io
import json
import os
import re
import subprocess
import sys
import tarfile

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = "state.enc"
DASHBOARD_FILE = "dashboard.enc"
PBKDF2_ITER = 310000
GIST_API = "https://api.github.com/gists/"

# What makes up the state, relative to the app folder
STATE_PATHS = ["watchlist.csv", "logs", "data"]
SKIP_RE = re.compile(r"(^|/)(run\.lock|__pycache__)(/|$)|\.log$|\.tmp$|\.DS_Store$")
KEEP_LATEST = {r"^logs/analysis_\d{8}_\d{4}\.json$": 10,
               r"^logs/simulator_state_archive_\d{8}_\d{6}\.json$": 5,
               r"^logs/runs/.+\.txt$": 10}   # GitHub run output (kept out of the public Actions log)

# Files the dashboard reads (trigger.py's ALLOWED_FILES + READ_ONLY_FILES,
# plus latest.json)
DASHBOARD_PATHS = [
    "logs/latest.json", "watchlist.csv",
    "logs/performance_log.json", "logs/closed_positions.json", "logs/performance_history.json",
    "logs/premarket_brief.json", "logs/thesis_log.json", "logs/outcome_log.json",
    "logs/correlation_matrix.json", "logs/stock_insight_status.json", "logs/agent_run_status.json",
    "logs/premarket_status.json", "logs/universe_refresh_status.json",
    "logs/trade_history.json", "logs/simulator_state.json",
]


# ── ENCRYPTION ────────────────────────────────────────────────
def _b64(b):
    return base64.b64encode(b).decode()


def _key(passphrase, salt, iterations):
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return kdf.derive(passphrase.encode())


def encrypt(data, passphrase):
    """bytes -> JSON envelope text {v, kdf, iter, salt, iv, ct}."""
    salt, iv = os.urandom(16), os.urandom(12)
    ct = AESGCM(_key(passphrase, salt, PBKDF2_ITER)).encrypt(iv, data, None)
    return json.dumps({"v": 1, "kdf": "PBKDF2-SHA256", "iter": PBKDF2_ITER,
                       "salt": _b64(salt), "iv": _b64(iv), "ct": _b64(ct)})


def decrypt(envelope_text, passphrase):
    env = json.loads(envelope_text)
    if env.get("v") != 1:
        raise ValueError("unknown envelope version")
    key = _key(passphrase, base64.b64decode(env["salt"]), int(env["iter"]))
    try:
        return AESGCM(key).decrypt(base64.b64decode(env["iv"]), base64.b64decode(env["ct"]), None)
    except Exception:
        raise ValueError("wrong passphrase, or the stored state is damaged") from None


# ── ARCHIVE ───────────────────────────────────────────────────
def _state_files(app_dir):
    files = []
    for top in STATE_PATHS:
        full = os.path.join(app_dir, top)
        if os.path.isfile(full):
            files.append(top)
        for root, dirs, names in os.walk(full):
            for n in names:
                rel = os.path.relpath(os.path.join(root, n), app_dir).replace(os.sep, "/")
                if not SKIP_RE.search(rel):
                    files.append(rel)
    for pattern, keep in KEEP_LATEST.items():
        matched = sorted(f for f in files if re.match(pattern, f))
        files = [f for f in files if f not in matched[:-keep]]
    return sorted(files)


def pack(app_dir):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in _state_files(app_dir):
            tar.add(os.path.join(app_dir, rel), arcname=rel, recursive=False)
    return buf.getvalue()


def unpack(blob, app_dir):
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        members = []
        for m in tar.getmembers():
            name = m.name
            if (not (m.isfile() or m.isdir()) or name.startswith("/") or ".." in name.split("/")
                    or name.split("/")[0] not in STATE_PATHS):
                raise ValueError(f"unexpected path in state archive: {name}")
            members.append(m)
        tar.extractall(app_dir, members=members, filter="data")
    return len(members)


def dashboard_bundle(app_dir):
    files = {}
    for rel in DASHBOARD_PATHS:
        p = os.path.join(app_dir, rel)
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                files[rel] = f.read()
    return json.dumps({"files": files}).encode()


# ── STORAGE (Gist, or a local folder for tests) ───────────────
class Store:
    def __init__(self, gist_id=None, token=None):
        self.dir = os.getenv("MA_STORE_DIR")
        self.gist_id, self.token = gist_id, token
        if not self.dir and not (gist_id and token):
            sys.exit("✗ MA_GIST_ID and MA_GIST_TOKEN are not set")

    def _headers(self):
        return {"Authorization": f"token {self.token}", "Accept": "application/vnd.github+json"}

    def read(self, name):
        if self.dir:
            p = os.path.join(self.dir, name)
            return open(p).read() if os.path.exists(p) else None
        import requests
        r = requests.get(GIST_API + self.gist_id, headers=self._headers(), timeout=30)
        if r.status_code != 200:
            sys.exit(f"✗ Could not read the Gist (HTTP {r.status_code})")
        f = r.json().get("files", {}).get(name)
        if not f:
            return None
        if f.get("truncated"):
            raw = requests.get(f["raw_url"], headers=self._headers(), timeout=60)
            raw.raise_for_status()
            return raw.text
        return f["content"]

    def write(self, files):
        if self.dir:
            os.makedirs(self.dir, exist_ok=True)
            for name, text in files.items():
                with open(os.path.join(self.dir, name), "w") as f:
                    f.write(text)
            return
        import requests
        body = {"files": {n: {"content": t} for n, t in files.items()}}
        r = requests.patch(GIST_API + self.gist_id, headers=self._headers(), json=body, timeout=60)
        if r.status_code != 200:
            sys.exit(f"✗ Could not save to the Gist (HTTP {r.status_code})")


# ── COMMANDS ──────────────────────────────────────────────────
def _passphrase(prompt=False, confirm=True):
    p = os.getenv("MA_PASSPHRASE")
    if not p and prompt:
        p = getpass.getpass("Encryption passphrase: ")
        if confirm and p != getpass.getpass("Same passphrase again: "):
            sys.exit("✗ The two passphrases differ")
    if not p or len(p) < 12:
        sys.exit("✗ Passphrase missing or shorter than 12 characters")
    return p


def _gh_token():
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return None


def cmd_pull(args):
    # On the laptop: asks for the passphrase and uses the gh sign-in
    store = Store(os.getenv("MA_GIST_ID"), os.getenv("MA_GIST_TOKEN") or _gh_token())
    text = store.read(STATE_FILE)
    if text is None:
        sys.exit("✗ No state in the Gist yet — run `state_store.py seed` from the laptop first")
    n = unpack(decrypt(text, _passphrase(prompt=True, confirm=False)), args.app)
    print(f"✓ State restored: {n} files")


def save(app_dir, store, passphrase):
    blob = pack(app_dir)
    store.write({STATE_FILE: encrypt(blob, passphrase),
                 DASHBOARD_FILE: encrypt(dashboard_bundle(app_dir), passphrase)})
    return len(_state_files(app_dir)), len(blob)


def cmd_push(args):
    store = Store(os.getenv("MA_GIST_ID"), os.getenv("MA_GIST_TOKEN"))
    n, size = save(args.app, store, _passphrase())
    print(f"✓ State saved: {n} files, {size / 1024:.0f} KB compressed")


def cmd_seed(args):
    src = os.path.abspath(os.path.expanduser(args.source))
    if not os.path.isfile(os.path.join(src, "watchlist.csv")):
        sys.exit(f"✗ {src} has no watchlist.csv")
    store = Store(os.getenv("MA_GIST_ID"), os.getenv("MA_GIST_TOKEN") or _gh_token())
    if store.read(STATE_FILE) is not None and not args.overwrite:
        sys.exit("✗ The Gist already holds state — add --overwrite to replace it")
    n, size = save(src, store, _passphrase(prompt=True))
    print(f"✓ Seeded the Gist from {src}: {n} files, {size / 1024:.0f} KB compressed")


def main():
    ap = argparse.ArgumentParser(description="Encrypted Market Agent state in a secret Gist")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("pull", "push"):
        p = sub.add_parser(name)
        p.add_argument("--app", default=APP_DIR, help="app folder (default: the repo root)")
    s = sub.add_parser("seed")
    s.add_argument("--from", dest="source", required=True, help="folder holding the current state")
    s.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()
    try:
        {"pull": cmd_pull, "push": cmd_push, "seed": cmd_seed}[args.cmd](args)
    except ValueError as e:
        sys.exit(f"✗ {e}")


if __name__ == "__main__":
    main()
