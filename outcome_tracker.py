# ============================================================
# MARKET AGENT V4.5 — Outcome Tracking / Post-Mortem
# outcome_tracker.py
#
# Grades the original reasoning against what actually happened,
# 30/60/90 days later — by rules since L2 (see rule_grade).
# Covers TWO pools, tagged by
# "source" in the same log so they stay comparable but separable:
#
#   "holding"     — every current holding, reusing the baseline
#                    already captured in logs/thesis_log.json.
#                    Graded at 30/60/90 days AND at exit (if sold
#                    before a checkpoint comes due).
#
#   "agent_pick"  — every pick that passed Gates 1-6 (strategy
#                    score, risk, confidence, sector, entry
#                    conditions) REGARDLESS of Gate 7 (52W High
#                    Headroom) — this deliberately includes picks
#                    Salim considers too strict on G7 alone, so
#                    the model's raw call quality on the fuller
#                    set gets graded, not just what cleared every
#                    gate into agent_picks.
#
# Known limitation: exit grading for holdings uses the CURRENT
# market price at the run that first notices the position is gone
# from watchlist.csv — not the actual sale price, since no broker
# fill signal is available to this system. Flagged in the entry
# as "price_is_exit_proxy": true so this is visible on the dashboard
# later, not silently treated as the real fill price.
#
# Reads:  analysis["holdings"]      — current holdings this run
#         thesis_log.json           — original thesis + entry price
#                                      for each holding (source of
#                                      truth — NOT re-derived here)
#         data["stocks"]            — live market data
#
# Writes: logs/outcome_log.json     — persisted grading store
# ============================================================

import json
import os
from datetime import datetime
from dotenv import load_dotenv
import anthropic
from claude_json import ask_json, keyed_schema
from common import GREEN, RED, YELLOW, BLUE, RESET, safe_float, load_json, save_json

load_dotenv()
BASE_DIR         = os.path.dirname(os.path.abspath(__file__))
OUTCOME_LOG_PATH = os.path.join(BASE_DIR, "logs", "outcome_log.json")
THESIS_LOG_PATH  = os.path.join(BASE_DIR, "logs", "thesis_log.json")
client           = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


CHECKPOINTS   = [30, 60, 90]   # days
VALID_GRADES  = {"CORRECT", "PARTIALLY_CORRECT", "INCORRECT"}
OUTCOME_SCHEMA = keyed_schema(
    {"grade": {"type": "string", "enum": sorted(VALID_GRADES)}, "reasoning": {"type": "string"}},
    ["grade", "reasoning"],
)


def load_outcome_log():
    return load_json(OUTCOME_LOG_PATH, {})


def save_outcome_log(log):
    save_json(OUTCOME_LOG_PATH, log)


def _new_checkpoints():
    return {
        str(h): {"status": "pending", "graded_at": None, "price_at_grading": None,
                  "grade": None, "reasoning": None}
        for h in CHECKPOINTS
    }


def _days_since(date_str):
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d")
        return (datetime.now() - d).days
    except Exception:
        return 0


# ── LOGGING NEW ENTRIES ───────────────────────────────────────
def log_new_picks(qualifying_picks, data):
    """
    Called from apply_pick_quality_gates() for every pick that passed
    Gates 1-6 this run, regardless of Gate 7 outcome. One entry per
    ticker per day — re-running the agent same-day won't duplicate.
    """
    if not qualifying_picks:
        return
    log   = load_outcome_log()
    today = datetime.now().strftime("%Y-%m-%d")
    added = []

    for pick in qualifying_picks:
        ticker = str(pick.get("ticker", "")).upper()
        if not ticker:
            continue
        key = f"{ticker}::{today}"
        if key in log:
            continue  # already logged today — avoid dupes across same-day runs

        price = safe_float(pick.get("price") or pick.get("current_price"))
        log[key] = {
            "ticker":                ticker,
            "source":                "agent_pick",
            "pick_date":             today,
            "entry_price":           price,
            "target_price":          safe_float(pick.get("target_price") or pick.get("target")),
            "original_thesis":       pick.get("reason", ""),
            "sector":                pick.get("sector", ""),
            # What the model claimed at the time — needed to check later
            # whether "High" confidence actually beats "Medium".
            "confidence_at_pick":    pick.get("confidence"),
            "risk_level_at_pick":    pick.get("risk_level"),
            "strategy_score_at_pick": pick.get("strategy_score"),
            "bear_case_at_pick":     (pick.get("bear_case") or {}).get("strength"),
            "stop_price_at_pick":    pick.get("stop_price"),
            "status":                "active",
            "closed_date":           None,
            "closed_price":          None,
            "price_is_exit_proxy":   False,
            "exit_review":           None,
            "checkpoints":           _new_checkpoints(),
        }
        added.append(ticker)

    if added:
        save_outcome_log(log)
        print(f"{BLUE}  Outcome tracking started for {len(added)} G1-6 pick(s): {', '.join(added)}{RESET}")


def sync_holdings(holdings, data):
    """
    Called every run. Two jobs:
      1. Ensure every current holding with a thesis_log baseline has
         a matching "holding" entry in outcome_log (created once).
      2. Detect holdings that dropped out of watchlist.csv since last
         run (i.e. were sold) and close+grade them immediately using
         current market price as an exit proxy.
    """
    log        = load_outcome_log()
    thesis_log = load_json(THESIS_LOG_PATH, {})
    current_tickers = {str(h.get("ticker", "")).upper() for h in holdings}
    stock_map  = {str(s.get("ticker", "")).upper(): s for s in data.get("stocks", [])}

    # 1. Create holding entries from thesis_log baselines
    created = []
    for ticker, tentry in thesis_log.items():
        established = tentry.get("thesis_established_date", "")
        key = f"{ticker}::{established}"
        if key not in log and ticker in current_tickers:
            log[key] = {
                "ticker":               ticker,
                "source":               "holding",
                "pick_date":            established,
                "entry_price":          safe_float(tentry.get("entry_price")),
                "target_price":         None,
                "original_thesis":      tentry.get("original_thesis", ""),
                "sector":               "",
                "status":               "active",
                "closed_date":          None,
                "closed_price":         None,
                "price_is_exit_proxy":  False,
                "exit_review":          None,
                "checkpoints":          _new_checkpoints(),
            }
            created.append(ticker)

    # 2. Detect exits — active "holding" entries whose ticker is no longer held
    closed = []
    for key, entry in log.items():
        if entry.get("source") != "holding" or entry.get("status") != "active":
            continue
        ticker = entry.get("ticker", "")
        if ticker in current_tickers:
            continue
        # No longer held — close it using best-available current price as a proxy
        s_data = stock_map.get(ticker, {})
        exit_price = safe_float(s_data.get("price"))
        if exit_price <= 0:
            exit_price = _fetch_live_price(ticker)
        entry["status"]              = "closed"
        entry["closed_date"]         = datetime.now().strftime("%Y-%m-%d")
        entry["closed_price"]        = exit_price if exit_price > 0 else None
        entry["price_is_exit_proxy"] = True
        # Any 30/60/90 checkpoint still pending would otherwise wait for
        # elapsed time to pass and then grade against this now-stale price.
        # Instead: skip those, and queue a single immediate exit review that
        # grades the thesis against the ACTUAL hold period (pick_date -> now).
        for cp in entry.get("checkpoints", {}).values():
            if cp.get("status") == "pending":
                cp["status"] = "skipped_exit"
        entry["exit_review"] = {"status": "pending", "graded_at": None,
                                 "grade": None, "reasoning": None}
        closed.append(ticker)

    if created:
        print(f"{BLUE}  Outcome tracking started for {len(created)} holding(s): {', '.join(created)}{RESET}")
    if closed:
        print(f"{YELLOW}  Position(s) no longer held — closing outcome tracking: {', '.join(closed)}{RESET}")

    if created or closed:
        save_outcome_log(log)

    return log


def _fetch_live_price(ticker):
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        return safe_float(info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose"))
    except Exception:
        return 0.0


# ── CHECKPOINT GRADING ────────────────────────────────────────
def _collect_due(log):
    """
    Returns list of (key, entry, horizon) tuples due for grading this run.
    horizon is "30"/"60"/"90" for standard checkpoints, or "EXIT" for a
    position closed before its next checkpoint came due — exit reviews
    are always immediately due, no elapsed-time gating.
    """
    due = []
    for key, entry in log.items():
        if entry.get("status") == "active":
            elapsed = _days_since(entry.get("pick_date"))
            for horizon_str, cp in entry.get("checkpoints", {}).items():
                if cp.get("status") == "pending" and elapsed >= int(horizon_str):
                    due.append((key, entry, horizon_str))
        elif entry.get("status") == "closed":
            exit_review = entry.get("exit_review") or {}
            if exit_review.get("status") == "pending":
                due.append((key, entry, "EXIT"))
    return due


def _current_price_for(entry, stock_map):
    ticker = entry.get("ticker", "")
    if entry.get("status") == "closed" and entry.get("closed_price"):
        return safe_float(entry.get("closed_price"))
    s_data = stock_map.get(ticker, {})
    price = safe_float(s_data.get("price"))
    if price <= 0:
        price = _fetch_live_price(ticker)
    return price


# ── L2: INDEPENDENT (RULE-BASED) GRADING ─────────────────────
# The grade of record no longer comes from Claude marking its own work.
# Each checkpoint is graded from prices alone:
#   1. did the price reach the target or the stop first (daily highs/lows)?
#   2. if neither, did it beat SPY over the same days by more than band_pct?
# A Claude review can still be switched on (outcome_grading.claude_review in
# strategy.json); it is told the thesis came from another analyst, sees the
# rule result, and its view is stored beside the rule grade, never instead.
def load_grading_config():
    cfg = {"band_pct": 3.0, "benchmark": "SPY", "claude_review": False}
    try:
        with open(os.path.join(BASE_DIR, "strategy.json")) as f:
            cfg.update({k: v for k, v in (json.load(f).get("outcome_grading") or {}).items() if k != "note"})
    except Exception:
        pass
    return cfg


def _history(ticker, start, end=None):
    """Daily OHLC from start (inclusive) to end (inclusive), or None."""
    try:
        import yfinance as yf
        from datetime import timedelta
        kw = {"start": start, "auto_adjust": True}
        if end:
            kw["end"] = (datetime.strptime(end[:10], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        h = yf.Ticker(ticker).history(**kw)
        return h if h is not None and not h.empty else None
    except Exception:
        return None


def rule_grade(entry, current_price, end_date=None, cfg=None):
    """(grade, detail) from prices only — no model involved."""
    cfg = cfg or load_grading_config()
    entry_price = safe_float(entry.get("entry_price"))
    stop   = safe_float(entry.get("stop_price_at_pick"))
    target = safe_float(entry.get("target_price"))
    start  = (entry.get("pick_date") or "")[:10]
    detail = {"grader": "rules", "entry_price": entry_price, "exit_price": current_price,
              "stop": stop or None, "target": target or None}
    if entry_price <= 0 or current_price <= 0 or not start:
        return None, {**detail, "error": "missing entry or current price"}

    ret = (current_price / entry_price - 1) * 100
    detail["return_pct"] = round(ret, 2)

    # Path: which level was touched first? Same-day touch of both counts as
    # the stop (the conservative reading).
    hist = _history(entry.get("ticker", ""), start, end_date)
    hit = None
    if hist is not None and len(hist) > 1:
        rows = hist.iloc[1:]        # the pick-day bar happened before the pick
        detail["max_gain_pct"] = round((float(rows["High"].max()) / entry_price - 1) * 100, 2)
        detail["max_drawdown_pct"] = round((float(rows["Low"].min()) / entry_price - 1) * 100, 2)
        for _, r in rows.iterrows():
            if stop and 0 < stop < entry_price and float(r["Low"]) <= stop:
                hit = "STOP"; break
            if target and target > entry_price and float(r["High"]) >= target:
                hit = "TARGET"; break
    detail["first_hit"] = hit

    bench = _history(cfg["benchmark"], start, end_date)
    spy_ret = None
    if bench is not None and len(bench) > 1:
        spy_ret = (float(bench["Close"].iloc[-1]) / float(bench["Close"].iloc[0]) - 1) * 100
    detail["benchmark"] = cfg["benchmark"]
    detail["benchmark_return_pct"] = round(spy_ret, 2) if spy_ret is not None else None
    excess = ret - spy_ret if spy_ret is not None else None
    detail["excess_pct"] = round(excess, 2) if excess is not None else None

    band = float(cfg["band_pct"])
    if hit == "TARGET":
        grade, why = "CORRECT", f"reached the ${target:,.2f} target before the stop"
    elif hit == "STOP":
        grade, why = "INCORRECT", f"hit the ${stop:,.2f} stop before the target"
    elif excess is None:
        grade = "CORRECT" if ret >= band else "INCORRECT" if ret <= -band else "PARTIALLY_CORRECT"
        why = f"{ret:+.1f}% (benchmark unavailable, judged on absolute return)"
    else:
        grade = "CORRECT" if excess >= band else "INCORRECT" if excess <= -band else "PARTIALLY_CORRECT"
        why = f"{ret:+.1f}% vs {cfg['benchmark']} {spy_ret:+.1f}% ({excess:+.1f} pts; ±{band:g} band)"
    detail["reasoning"] = f"Rules: {why}."
    return grade, detail


def grade_due_checkpoints(log, data):
    stock_map = {str(s.get("ticker", "")).upper(): s for s in data.get("stocks", [])}
    due = _collect_due(log)
    if not due:
        return log

    cfg = load_grading_config()
    print(f"{BLUE}  {len(due)} checkpoint(s) due — grading by rules (price path + {cfg['benchmark']}){RESET}")
    graded = []
    for key, entry, horizon in due:
        current_price = _current_price_for(entry, stock_map)
        end_date = entry.get("closed_date") if horizon == "EXIT" else None
        grade, detail = rule_grade(entry, current_price, end_date, cfg)
        if grade is None:
            print(f"  {YELLOW}⚠ {entry.get('ticker')} @ {horizon}: can't grade yet — {detail.get('error')}{RESET}")
            continue
        target = entry["exit_review"] if horizon == "EXIT" else entry["checkpoints"][horizon]
        target.update({"status": "graded", "graded_at": datetime.now().isoformat(),
                       "grade": grade, "grader": "rules", "reasoning": detail["reasoning"],
                       "rule_detail": detail})
        if horizon != "EXIT":
            target["price_at_grading"] = current_price
        graded.append((key, entry, horizon, current_price, grade, detail))
        icon = {"CORRECT": GREEN, "PARTIALLY_CORRECT": YELLOW, "INCORRECT": RED}.get(grade, "")
        label = "exit" if horizon == "EXIT" else f"{horizon}d"
        print(f"  {icon}{entry['ticker']} @ {label}: {grade} — {detail['reasoning']}{RESET}")

    if graded and cfg.get("claude_review"):
        independent_review(log, graded)
    if graded:
        save_outcome_log(log)
    print(f"{GREEN}✓ Outcome grading complete — {len(graded)} checkpoint(s) graded by rules{RESET}")
    return log


def independent_review(log, graded):
    """Optional second opinion from Claude, framed as reviewing someone
    else's call and given the rule result. Stored as review_grade."""
    blocks, lookup = [], {}
    for key, entry, horizon, price, grade, d in graded:
        gk = f"{key}::{horizon}"
        lookup[gk] = (key, horizon)
        blocks.append(f"""
GRADE_KEY: {gk}
Ticker: {entry.get('ticker')} | thesis dated {entry.get('pick_date')} | checkpoint {horizon}
Thesis: {entry.get('original_thesis','')}
Facts: entry ${d['entry_price']:.2f} -> now ${price:.2f} ({d.get('return_pct'):+.1f}%); {d['benchmark']} {d.get('benchmark_return_pct')}%;
max gain {d.get('max_gain_pct')}%, max drawdown {d.get('max_drawdown_pct')}%; first level hit: {d.get('first_hit') or 'neither'}.
Rule grade: {grade}
""")
    prompt = f"""You are an independent reviewer checking another analyst's past theses against the facts below.
You did not write these theses. Judge whether the stated REASONING was borne out, not only the return.
If you disagree with the rule grade, say why in one sentence.
{"".join(blocks)}
Return one entry per GRADE_KEY: {{"GRADE_KEY": {{"grade": "CORRECT|PARTIALLY_CORRECT|INCORRECT", "reasoning": "1-2 sentences"}}}}"""
    try:
        results = ask_json(client, prompt, max_tokens=2000, label="outcome_review", schema=OUTCOME_SCHEMA)
    except Exception as e:
        print(f"{YELLOW}⚠ Independent review skipped: {e}{RESET}")
        return
    for gk, r in (results or {}).items():
        if gk in lookup and r.get("grade") in VALID_GRADES:
            key, horizon = lookup[gk]
            t = log[key]["exit_review"] if horizon == "EXIT" else log[key]["checkpoints"][horizon]
            t["review_grade"], t["review_reasoning"] = r["grade"], r.get("reasoning", "")


def outcome_summary(log=None):
    """Track record from rule grades, for the dashboard."""
    log = log if log is not None else load_outcome_log()
    rows = []
    for e in log.values():
        cps = list((e.get("checkpoints") or {}).items())
        if e.get("exit_review"):
            cps.append(("EXIT", e["exit_review"]))
        for h, cp in cps:
            if cp.get("status") == "graded" and cp.get("grader") == "rules":
                d = cp.get("rule_detail") or {}
                rows.append({"ticker": e.get("ticker"), "source": e.get("source"), "pick_date": e.get("pick_date"),
                             "horizon": h, "grade": cp.get("grade"), "confidence": e.get("confidence_at_pick"),
                             "return_pct": d.get("return_pct"), "excess_pct": d.get("excess_pct"),
                             "first_hit": d.get("first_hit"), "review_grade": cp.get("review_grade")})
    def agg(rs):
        n = len(rs)
        ex = [r["excess_pct"] for r in rs if r["excess_pct"] is not None]
        return {"n": n,
                "correct_pct": round(100 * sum(r["grade"] == "CORRECT" for r in rs) / n, 1) if n else None,
                "incorrect_pct": round(100 * sum(r["grade"] == "INCORRECT" for r in rs) / n, 1) if n else None,
                "avg_excess_pct": round(sum(ex) / len(ex), 2) if ex else None}
    by_h = {h: agg([r for r in rows if r["horizon"] == h]) for h in ["30", "60", "90", "EXIT"] if any(r["horizon"] == h for r in rows)}
    by_src = {s: agg([r for r in rows if r["source"] == s]) for s in {r["source"] for r in rows}}
    pending = sum(1 for e in log.values() for cp in (e.get("checkpoints") or {}).values() if cp.get("status") == "pending")
    return {"overall": agg(rows), "by_horizon": by_h, "by_source": by_src, "pending": pending,
            "recent": sorted(rows, key=lambda r: r["pick_date"] or "", reverse=True)[:15],
            "method": "Graded by rules: target or stop hit first, otherwise return vs SPY over the same days (±3 pts = partial)."}


# ── MAIN ENTRY POINT ──────────────────────────────────────────
def run(analysis, data):
    """
    Called from agent_brain.py's run() after thesis_tracker. Syncs
    holdings into the outcome log (create + exit detection), then
    grades any checkpoint that's come due — for both holdings and
    G1-6 agent picks (the latter logged earlier in the same run via
    apply_pick_quality_gates() -> log_new_picks()).
    """
    holdings = analysis.get("holdings", [])
    log = sync_holdings(holdings, data)
    log = grade_due_checkpoints(log, data)
    return analysis
