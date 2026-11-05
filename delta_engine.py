# ============================================================
# MARKET AGENT V4.5 — Delta Engine
# delta_engine.py
#
# Computes "what changed since the last run" — directly enabled by
# the V4.5 memory features (thesis tracking, gate widening). This is
# what powers the dashboard's Delta tab, so you don't have to mentally
# diff two runs yourself.
#
# MUST run BEFORE save_analysis() overwrites logs/latest.json, since
# it reads that file as the "previous run" snapshot to diff against.
#
# Three change types:
#   1. Thesis status changes — sourced directly from
#      analysis["thesis_changes_this_run"], populated by
#      thesis_tracker.py during THIS run (not re-derived after the
#      fact, which would be fragile).
#   2. Gate changes — tickers newly qualified into agent_picks, tickers
#      that dropped out of agent_picks, and tickers whose gate7_status
#      flipped (PASSED <-> FAILED) between runs.
#   3. Score movements — strategy_score changes >= SCORE_MOVEMENT_
#      THRESHOLD points for any ticker present in both runs' holdings
#      or agent_picks.
#
# Reads:  logs/latest.json (the OLD one, before this run overwrites it)
#         analysis["thesis_changes_this_run"], analysis["holdings"],
#         analysis["agent_picks"]
#
# Writes: analysis["delta"] — consumed by save_analysis() into the
#         NEW latest.json, and by the dashboard's Delta tab
# ============================================================

import json
import os
from datetime import datetime
from common import GREEN, YELLOW, RESET, safe_float

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
LATEST_PATH = os.path.join(BASE_DIR, "logs", "latest.json")


SCORE_MOVEMENT_THRESHOLD = 3.0  # minimum |delta| in strategy_score points to report


def load_previous_run():
    if not os.path.exists(LATEST_PATH):
        return None
    try:
        with open(LATEST_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"{YELLOW}⚠ Could not load previous latest.json for delta ({e}){RESET}")
        return None


def compute_gate_changes(new_analysis, old_analysis):
    if not old_analysis:
        return {"newly_qualified": [], "no_longer_qualified": [], "gate7_status_changed": []}

    new_picks = {str(p.get("ticker", "")).upper(): p for p in new_analysis.get("agent_picks", [])}
    old_picks = {str(p.get("ticker", "")).upper(): p for p in old_analysis.get("agent_picks", [])}

    newly_qualified = [
        {"ticker": t, "strategy_score": p.get("strategy_score"), "gate7_status": p.get("gate7_status")}
        for t, p in new_picks.items() if t not in old_picks
    ]
    no_longer_qualified = [
        {"ticker": t, "strategy_score": p.get("strategy_score")}
        for t, p in old_picks.items() if t not in new_picks
    ]
    gate7_status_changed = []
    for t, p in new_picks.items():
        if t in old_picks:
            new_g7, old_g7 = p.get("gate7_status"), old_picks[t].get("gate7_status")
            if new_g7 and old_g7 and new_g7 != old_g7:
                gate7_status_changed.append({"ticker": t, "from": old_g7, "to": new_g7})

    return {
        "newly_qualified":      newly_qualified,
        "no_longer_qualified":  no_longer_qualified,
        "gate7_status_changed": gate7_status_changed,
    }


def compute_score_movements(new_analysis, old_analysis):
    if not old_analysis:
        return []

    movements = []
    for section in ("holdings", "agent_picks"):
        new_items = {str(i.get("ticker", "")).upper(): i for i in new_analysis.get(section, [])}
        old_items = {str(i.get("ticker", "")).upper(): i for i in old_analysis.get(section, [])}
        for t, new_item in new_items.items():
            if t not in old_items:
                continue
            new_score = safe_float(new_item.get("strategy_score"))
            old_score = safe_float(old_items[t].get("strategy_score"))
            delta = new_score - old_score
            if abs(delta) >= SCORE_MOVEMENT_THRESHOLD:
                movements.append({
                    "ticker": t, "section": section,
                    "old_score": old_score, "new_score": new_score,
                    "delta": round(delta, 1),
                })
    movements.sort(key=lambda m: -abs(m["delta"]))
    return movements


def compute(analysis, data):
    old_analysis = load_previous_run()  # MUST read before save_analysis() overwrites latest.json

    thesis_changes  = analysis.get("thesis_changes_this_run", [])
    gate_changes    = compute_gate_changes(analysis, old_analysis)
    score_movements = compute_score_movements(analysis, old_analysis)

    has_changes = bool(
        thesis_changes or score_movements
        or gate_changes["newly_qualified"] or gate_changes["no_longer_qualified"]
        or gate_changes["gate7_status_changed"]
    )

    delta = {
        "computed_at":      datetime.now().isoformat(),
        "previous_run_at":  old_analysis.get("saved_at_display") if old_analysis else None,
        "has_previous_run": old_analysis is not None,
        "thesis_changes":   thesis_changes,
        "gate_changes":     gate_changes,
        "score_movements":  score_movements,
        "has_changes":      has_changes,
    }
    analysis["delta"] = delta

    if not old_analysis:
        print(f"{YELLOW}⚠ No previous run found — delta view will populate from next run{RESET}")
    elif has_changes:
        n = (len(thesis_changes) + len(score_movements) + len(gate_changes["newly_qualified"])
             + len(gate_changes["no_longer_qualified"]) + len(gate_changes["gate7_status_changed"]))
        print(f"{GREEN}✓ Delta computed — {n} change(s) since last run{RESET}")
    else:
        print(f"{GREEN}✓ Delta computed — no material changes since last run{RESET}")

    return analysis
