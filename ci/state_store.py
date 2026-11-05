#!/usr/bin/env python3
# ============================================================
# MARKET AGENT V4.5 — Encrypted state in a secret Gist
#
# The public repo holds code only. Everything personal (watchlist.csv,
# logs/, data/) lives in one secret Gist as two encrypted files:
#   state.enc      full archive the GitHub Actions run restores and saves
#   dashboard.enc  just the files the dashboard reads, as one JSON bundle
#   inbox.enc      edits made on the phone (watchlist, closed positions,
#                  signals log, recorded trades), applied by the next run
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
#   state_store.py open-input VAR OUT  decrypt an encrypted workflow input
#   state_store.py backup --to DIR     save the Gist's data as a plain dated
#                                      market-agent-backup-YYYY-MM-DD.tar.gz
#   state_store.py restore FILE        load a backup into the Gist (saves the
#                                      current data as a backup first)
# Backups are the same plain format as local/backup_local.sh, so a GitHub
# backup restores on the laptop and a laptop backup restores to GitHub.
#
# Environment: MA_PASSPHRASE, MA_GIST_ID, MA_GIST_TOKEN (classic token,
# gist scope). On the laptop the Gist ID is read from ~/market-agent/.gist_id
# when MA_GIST_ID is unset, so it never has to appear in a command or the repo. seed prompts for the passphrase and can use `gh auth token`.
# MA_STORE_DIR=<folder> stores the two files in a local folder instead of
# the Gist (for testing).
# ============================================================

import argparse
import base64
import datetime
import shutil
import tempfile
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
INBOX_FILE = "inbox.enc"
APPLIED_MARK = ".inbox_applied.json"   # app folder, outside the state: op ids applied by pull
EDITABLE = {"watchlist.csv", "logs/closed_positions.json", "logs/performance_log.json"}
INBOX_LOG = "logs/inbox_log.json"
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
    "logs/trade_history.json", "logs/simulator_state.json", "logs/inbox_log.json",
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


def _checked_members(tar):
    """Every entry must be a file or folder under watchlist.csv, logs/ or data/."""
    members = []
    for m in tar.getmembers():
        name = m.name[2:] if m.name.startswith("./") else m.name
        if name in ("", ".") or name.split("/")[-1].startswith("._"):
            continue                     # macOS tar's hidden metadata files
        if (not (m.isfile() or m.isdir()) or name.startswith("/") or ".." in name.split("/")
                or name.split("/")[0] not in STATE_PATHS):
            raise ValueError(f"unexpected path in state archive: {m.name}")
        members.append(m)
    return members


def unpack(blob, app_dir):
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        members = _checked_members(tar)
        tar.extractall(app_dir, members=members, filter="data")
    return len(members)


def check_backup(blob):
    """(file count) for a valid backup, else ValueError."""
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            members = _checked_members(tar)
    except tarfile.TarError:
        raise ValueError("not a Market Agent backup (.tar.gz)") from None
    names = {m.name[2:] if m.name.startswith("./") else m.name for m in members}
    if "watchlist.csv" not in names:
        raise ValueError("not a Market Agent backup: no watchlist.csv inside")
    return sum(1 for m in members if m.isfile())


def backup_path(folder, suffix=""):
    """<folder>/market-agent-backup-YYYY-MM-DD[suffix].tar.gz, with the time
    added when that name is taken (several backups a day)."""
    now = datetime.datetime.now()
    path = os.path.join(folder, f"market-agent-backup-{now:%Y-%m-%d}{suffix}.tar.gz")
    if os.path.exists(path):
        path = os.path.join(folder, f"market-agent-backup-{now:%Y-%m-%d_%H%M%S}{suffix}.tar.gz")
    return path


def write_backup(blob, folder, suffix=""):
    folder = os.path.abspath(os.path.expanduser(folder))
    os.makedirs(folder, exist_ok=True)
    path = backup_path(folder, suffix)
    with open(path, "wb") as f:
        f.write(blob)
    os.chmod(path, 0o600)          # plain data: readable by you only
    return path


def dashboard_bundle(app_dir):
    files = {}
    for rel in DASHBOARD_PATHS:
        p = os.path.join(app_dir, rel)
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                files[rel] = f.read()
    return json.dumps({"files": files}).encode()


# ── STORAGE (Gist, or a local folder for tests) ───────────────
GIST_ID_FILE = os.path.expanduser("~/market-agent/.gist_id")   # laptop only, never in the repo


class Store:
    def __init__(self, gist_id=None, token=None):
        self.dir = os.getenv("MA_STORE_DIR")
        if not gist_id and os.path.exists(GIST_ID_FILE):
            gist_id = open(GIST_ID_FILE).read().strip()
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
        r = requests.get(GIST_API + self.gist_id, headers=self._headers(), timeout=30)   # always fresh
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


# ── PHONE EDITS (inbox) ───────────────────────────────────────
def read_inbox(store, passphrase):
    text = store.read(INBOX_FILE)
    return json.loads(decrypt(text, passphrase)).get("ops", []) if text else []


def _check_put(name, content):
    if name not in EDITABLE:
        raise ValueError(f"{name} can't be edited from the phone")
    if not isinstance(content, str) or len(content) > 1_000_000:
        raise ValueError("content missing or over 1 MB")
    if name == "watchlist.csv":
        header = content.lstrip().split("\n", 1)[0]
        if not header.startswith("ticker,") or "list_type" not in header:
            raise ValueError("not a watchlist CSV")
    else:
        try:
            ok = isinstance(json.loads(content), (list, dict))
        except ValueError:
            ok = False
        if not ok:
            raise ValueError("not valid JSON")


def apply_inbox(app_dir, ops):
    """Apply phone edits in the order they were made, with the laptop's own
    checks. Returns (applied, rejected). Each outcome is kept in
    logs/inbox_log.json; the edits' content is never printed."""
    sys.path.insert(0, app_dir)
    from common import make_trade_entry
    log_path = os.path.join(app_dir, INBOX_LOG)
    try:
        log = json.load(open(log_path))
    except Exception:
        log = []
    applied = rejected = 0
    for op in sorted(ops, key=lambda o: o.get("at", "")):
        rec = {"id": op.get("id"), "at": op.get("at"), "type": op.get("type")}
        try:
            if op.get("type") == "put":
                name, content = op.get("name"), op.get("content")
                _check_put(name, content)
                with open(os.path.join(app_dir, name), "w", encoding="utf-8") as f:
                    f.write(content)
                rec["file"] = name
            elif op.get("type") == "trade":
                entry = make_trade_entry(op.get("trade") or {})
                entry["source"] = "phone"
                path = os.path.join(app_dir, "logs", "trade_history.json")
                try:
                    history = json.load(open(path))
                except Exception:
                    history = []
                history.append(entry)
                with open(path, "w") as f:
                    json.dump(history, f, indent=2)
                rec["ticker"], rec["action"] = entry["ticker"], entry["action"]
            else:
                raise ValueError("unknown edit type")
            rec["status"] = "applied"; applied += 1
        except Exception as e:
            rec["status"], rec["message"] = "rejected", str(e)[:200]; rejected += 1
        log.append(rec)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "w") as f:
        json.dump(log[-100:], f, indent=2)
    return applied, rejected


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
    passphrase = _passphrase(prompt=True, confirm=False)
    n = unpack(decrypt(text, passphrase), args.app)
    print(f"✓ State restored: {n} files")
    if args.apply_inbox:
        ops = read_inbox(store, passphrase)
        applied, rejected = apply_inbox(args.app, ops) if ops else (0, 0)
        with open(os.path.join(args.app, APPLIED_MARK), "w") as f:
            json.dump([o.get("id") for o in ops], f)
        print(f"✓ Phone edits: {applied} applied, {rejected} rejected")


def save(app_dir, store, passphrase):
    blob = pack(app_dir)
    files = {STATE_FILE: encrypt(blob, passphrase),
             DASHBOARD_FILE: encrypt(dashboard_bundle(app_dir), passphrase)}
    mark = os.path.join(app_dir, APPLIED_MARK)
    if os.path.exists(mark):
        # Drop only the edits this run applied: ones made on the phone
        # while it ran stay pending for the next run.
        done = set(json.load(open(mark)))
        remaining = [o for o in read_inbox(store, passphrase) if o.get("id") not in done]
        files[INBOX_FILE] = encrypt(json.dumps({"ops": remaining}).encode(), passphrase)
    store.write(files)
    if os.path.exists(mark):
        os.remove(mark)
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


def cmd_open_input(args):
    """Decrypt a workflow input (the dashboard encrypts the stock-insight
    ticker: run inputs are public on a public repo) into a file — never
    to the log."""
    text = os.getenv(args.var, "")
    if not text:
        sys.exit(f"✗ {args.var} is empty")
    value = decrypt(text, _passphrase()).decode().strip()
    try:
        parsed = json.loads(value)          # the browser encrypts JSON text ("BRK.B")
        if isinstance(parsed, str):
            value = parsed.strip()
    except ValueError:
        pass
    with open(args.out, "w") as f:
        f.write(value)


def cmd_backup(args):
    store = Store(os.getenv("MA_GIST_ID"), os.getenv("MA_GIST_TOKEN") or _gh_token())
    text = store.read(STATE_FILE)
    if text is None:
        sys.exit("✗ The Gist holds no data yet")
    blob = decrypt(text, _passphrase(prompt=True, confirm=False))
    n = check_backup(blob)
    print(f"✓ Backup saved: {write_backup(blob, args.to)} ({n} files, plain — keep it private)")


def cmd_restore(args):
    path = os.path.abspath(os.path.expanduser(args.file))
    blob = open(path, "rb").read()
    n = check_backup(blob)
    store = Store(os.getenv("MA_GIST_ID"), os.getenv("MA_GIST_TOKEN") or _gh_token())
    current = store.read(STATE_FILE)
    # With data already in the Gist, decrypting it proves the passphrase is
    # right before anything is replaced; with none, type it twice
    passphrase = _passphrase(prompt=True, confirm=current is None)
    keep = args.keep_dir or os.path.dirname(path)
    if current is not None:
        before = write_backup(decrypt(current, passphrase), keep, "-before-restore")
        print(f"✓ Current Gist data saved first: {before}")
    if not args.yes:
        answer = input(f"Replace the Gist's data with {os.path.basename(path)} ({n} files)? Type RESTORE: ")
        if answer.strip() != "RESTORE":
            sys.exit("✗ Not restored")
    pending = len(read_inbox(store, passphrase)) if current is not None else 0
    tmp = tempfile.mkdtemp(prefix="ma-restore-")
    try:
        unpack(blob, tmp)
        save(tmp, store, passphrase)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # Phone edits made against the old data would be applied on top of the
    # restored data on the next run, so they are dropped
    store.write({INBOX_FILE: encrypt(json.dumps({"ops": []}).encode(), passphrase)})
    print(f"✓ Restored {n} files to the Gist from {path}"
          + (f" ({pending} pending phone edit(s) discarded)" if pending else ""))


def main():
    ap = argparse.ArgumentParser(description="Encrypted Market Agent state in a secret Gist")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("pull", "push"):
        p = sub.add_parser(name)
        p.add_argument("--app", default=APP_DIR, help="app folder (default: the repo root)")
        if name == "pull":
            p.add_argument("--apply-inbox", action="store_true",
                           help="apply pending phone edits (GitHub runs); push then clears them")
    s = sub.add_parser("seed")
    s.add_argument("--from", dest="source", required=True, help="folder holding the current state")
    s.add_argument("--overwrite", action="store_true")
    o = sub.add_parser("open-input")
    o.add_argument("var", help="environment variable holding the encrypted input")
    o.add_argument("out", help="file to write the plain value to")
    b = sub.add_parser("backup")
    b.add_argument("--to", required=True, help="folder to save the dated backup in")
    r = sub.add_parser("restore")
    r.add_argument("file", help="a market-agent-backup-*.tar.gz")
    r.add_argument("--keep-dir", help="where to save the current data first (default: next to FILE)")
    r.add_argument("--yes", action="store_true", help="don't ask to confirm")
    args = ap.parse_args()
    try:
        {"pull": cmd_pull, "push": cmd_push, "seed": cmd_seed, "open-input": cmd_open_input,
         "backup": cmd_backup, "restore": cmd_restore}[args.cmd](args)
    except ValueError as e:
        sys.exit(f"✗ {e}")


if __name__ == "__main__":
    main()
