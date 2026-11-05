#!/usr/bin/env python3
# ============================================================
# MARKET AGENT V4.5 — Shared step-progress writer
# write_progress.py
#
# Used by run_agent.sh and premarket_run.sh to report real
# step-by-step progress to a status file the dashboard polls —
# needed because each pipeline is a SHELL SCRIPT orchestrating
# several separate Python subprocesses, so progress can't live
# in any one process's memory. Each script calls this once per
# step with a shared started_at value (captured once at the top
# of the shell script) so the dashboard can show both "step 4 of
# 10" and how long the whole run has been going.
#
# Usage: python3 write_progress.py <status_file> <step> <total> "<label>" <started_at> [status]
#
# Sub-steps: a long step (agent_brain.py, step 7) can also report
# finer progress from inside Python with substep("..."). run_agent.sh
# exports PROGRESS_FILE / PROGRESS_STEP / PROGRESS_TOTAL /
# PROGRESS_STARTED / PROGRESS_LABEL before that step; substep() keeps
# the same step number and rewrites only the label, e.g.
# "Analysis 2/8 · Screening candidates 120/301". Without those env
# vars (script run by hand) substep() does nothing.
# ============================================================
import json
import os
import sys
from datetime import datetime, timezone


def write_status(status_file, step, total, label, started_at=None, status="running"):
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "status":      status,
        "step":        int(step),
        "total_steps": int(total),
        "step_label":  label,
        "started_at":  started_at or now,
        "updated_at":  now,
    }
    if status == "done":
        payload["finished_at"] = now

    os.makedirs(os.path.dirname(status_file) or ".", exist_ok=True)
    # Write-then-rename so the dashboard never polls a half-written file
    # (sub-steps rewrite this every few seconds during step 7).
    tmp = status_file + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, status_file)


def substep(label):
    status_file = os.getenv("PROGRESS_FILE")
    if not status_file:
        return
    base = os.getenv("PROGRESS_LABEL", "")
    try:
        write_status(
            status_file,
            os.getenv("PROGRESS_STEP", "0"),
            os.getenv("PROGRESS_TOTAL", "0"),
            f"{base} {label}".strip(),
            os.getenv("PROGRESS_STARTED") or None,
        )
    except Exception:
        pass  # progress is cosmetic — never let it break a run


def main():
    if len(sys.argv) < 5:
        print("Usage: write_progress.py <status_file> <step> <total> <label> <started_at> [status]", file=sys.stderr)
        sys.exit(1)

    write_status(
        sys.argv[1],
        sys.argv[2],
        sys.argv[3],
        sys.argv[4],
        sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] else None,
        sys.argv[6] if len(sys.argv) > 6 else "running",
    )


if __name__ == "__main__":
    main()
