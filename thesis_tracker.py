# ============================================================
# MARKET AGENT V4.5 — Thesis Drift Tracking
# thesis_tracker.py
#
# Maintains a persistent record of the ORIGINAL investment thesis
# for every holding, and re-evaluates it against current data on
# every full agent run — decoupling thesis health from raw price
# action. A stock can dip on macro noise while its thesis stays
# intact, or sit flat while its thesis quietly rots.
#
# First run for any ticker: no baseline exists yet, so the ticker's
# current 'reason' text (produced by run_agent()/enrich_holdings())
# is captured as the original_thesis baseline. INTACT by default.
#
# Every subsequent run: ALL holdings with an existing baseline are
# evaluated in a SINGLE batched Claude call (not one call per stock)
# and classified INTACT / WEAKENING / BROKEN with reasoning.
#
# status_history only grows when status actually CHANGES — not on
# every run — to keep the log lean and to double as the data source
# for a future "what changed" dashboard view.
#
# Reads:  analysis["holdings"]   — current holdings (with fresh
#                                   'reason' text from this run)
#         data["stocks"]         — market data incl. buy_price,
#                                   52w_high, days_to_earnings
#
# Writes: analysis["holdings"][i]["thesis_status"]     — per holding
#         analysis["holdings"][i]["thesis_reasoning"]  — per holding
#         logs/thesis_log.json                          — persisted store
# ============================================================

import json
import os
import time
from datetime import datetime
from dotenv import load_dotenv
import anthropic
from claude_json import ask_json, StructuredReplyError, keyed_schema
from common import GREEN, RED, YELLOW, BLUE, RESET, safe_float, save_json

load_dotenv()
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
THESIS_LOG_PATH = os.path.join(BASE_DIR, "logs", "thesis_log.json")
client     = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


VALID_STATUSES = {"INTACT", "WEAKENING", "BROKEN"}
THESIS_SCHEMA  = keyed_schema(
    {"status": {"type": "string", "enum": sorted(VALID_STATUSES)}, "reasoning": {"type": "string"}},
    ["status", "reasoning"],
)


def load_thesis_log():
    if not os.path.exists(THESIS_LOG_PATH):
        return {}
    try:
        with open(THESIS_LOG_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"{YELLOW}⚠ Could not load thesis_log.json ({e}) — starting fresh{RESET}")
        return {}


def save_thesis_log(thesis_log):
    save_json(THESIS_LOG_PATH, thesis_log)


# ── BATCHED DRIFT EVALUATION ──────────────────────────────────
def evaluate_drift(holdings_to_check, thesis_log, stock_map):
    """
    Single Claude call evaluating thesis health for every holding that
    already has a baseline. Returns {TICKER: {"status":..., "reasoning":...}}
    """
    blocks = []
    for h in holdings_to_check:
        ticker  = str(h.get("ticker", "")).upper()
        entry   = thesis_log.get(ticker, {})
        s_data  = stock_map.get(ticker, {})
        blocks.append(f"""
TICKER: {ticker}
Original thesis (established {entry.get('thesis_established_date','?')}): {entry.get('original_thesis','')}
Entry price: ${safe_float(entry.get('entry_price')):.2f}
Current price: ${safe_float(h.get('price') or s_data.get('price')):.2f}  (P&L: {h.get('pnl_pct','?')}%)
Current 52-week high: ${safe_float(s_data.get('52w_high')):.2f}
Sector: {h.get('sector','')}
Days to earnings: {s_data.get('days_to_earnings','N/A')}
This run's fresh assessment: {h.get('reason','')}
""")

    prompt = f"""You are reviewing whether the ORIGINAL investment thesis for each holding below still holds up against current data — not simply whether the stock price is up or down.

For each ticker, compare "Original thesis" against the current data and this run's fresh assessment. Classify as exactly one of:
- INTACT: the original reasoning still holds; nothing material has changed
- WEAKENING: some erosion in the original reasoning — worth monitoring closely, not yet a red flag
- BROKEN: the original thesis no longer holds — a specific claim it depended on has been invalidated

Price movement ALONE is NOT sufficient grounds for WEAKENING or BROKEN — a stock can fall on broad macro or sector weakness while its underlying thesis stays fully intact. Judge whether the SPECIFIC claims in the original thesis remain true today, not the stock's recent return.
{"".join(blocks)}
Return ONLY this JSON — one entry per ticker listed above, no other text, no markdown fences:
{{
  "TICKER": {{"status": "INTACT|WEAKENING|BROKEN", "reasoning": "1-2 sentences referencing the specific original claim(s)"}}
}}"""

    for attempt in range(2):
        try:
            result = ask_json(client, prompt, max_tokens=2000, label="thesis_tracker", schema=THESIS_SCHEMA)
            return {str(k).upper(): v for k, v in result.items()}
        except StructuredReplyError as err:
            print(f"{RED}✗ Thesis drift reply error: {err}{RESET}")
            return {}
        except Exception as e:
            if "overloaded" in str(e).lower() and attempt < 1:
                print(f"{YELLOW}⚠ API overloaded — retrying thesis drift check in 20s...{RESET}")
                time.sleep(20)
                continue
            print(f"{RED}✗ Thesis drift API error: {e}{RESET}")
            return {}
    return {}


# ── MAIN ENTRY POINT ──────────────────────────────────────────
def run(analysis, data):
    """
    Called from agent_brain.py's run() after enrich_holdings() has
    populated fresh 'reason' text on each holding. Mutates and
    returns analysis with thesis_status/thesis_reasoning attached
    to each holding, and persists logs/thesis_log.json.
    """
    holdings = analysis.get("holdings", [])
    if not holdings:
        return analysis

    thesis_log = load_thesis_log()
    stock_map  = {str(s.get("ticker", "")).upper(): s for s in data.get("stocks", [])}

    new_baselines = []
    to_evaluate   = []

    for h in holdings:
        ticker = str(h.get("ticker", "")).upper()
        if not ticker:
            continue
        if ticker not in thesis_log:
            entry_price = safe_float(stock_map.get(ticker, {}).get("buy_price"))
            thesis_log[ticker] = {
                "ticker":                   ticker,
                "thesis_established_date":  datetime.now().strftime("%Y-%m-%d"),
                "entry_price":              entry_price,
                "original_thesis":          h.get("reason", ""),
                "current_status":           "INTACT",
                "last_checked":             datetime.now().isoformat(),
                "status_history":           [],
            }
            new_baselines.append(ticker)
            h["thesis_status"]    = "INTACT"
            h["thesis_reasoning"] = "Baseline established this run — no prior thesis to compare against yet."
        else:
            to_evaluate.append(h)

    if new_baselines:
        print(f"{BLUE}  Thesis baseline established for: {', '.join(new_baselines)}{RESET}")

    if to_evaluate:
        results = evaluate_drift(to_evaluate, thesis_log, stock_map)
        for h in to_evaluate:
            ticker = str(h.get("ticker", "")).upper()
            entry  = thesis_log[ticker]
            r = results.get(ticker)
            if not r or r.get("status") not in VALID_STATUSES:
                # No usable result for this ticker this run — keep prior status, don't overwrite
                h["thesis_status"]    = entry.get("current_status", "INTACT")
                h["thesis_reasoning"] = entry.get("status_history", [{}])[-1].get("reasoning", "") \
                    if entry.get("status_history") else "No thesis update this run."
                continue

            old_status = entry.get("current_status", "INTACT")
            new_status = r.get("status")
            reasoning  = r.get("reasoning", "")
            entry["last_checked"] = datetime.now().isoformat()

            if new_status != old_status:
                entry.setdefault("status_history", []).append({
                    "date":      entry["last_checked"],
                    "from":      old_status,
                    "to":        new_status,
                    "reasoning": reasoning,
                })
                analysis.setdefault("thesis_changes_this_run", []).append({
                    "ticker":    ticker,
                    "from":      old_status,
                    "to":        new_status,
                    "reasoning": reasoning,
                })
                print(f"  {YELLOW}⚠ {ticker} thesis status changed: {old_status} → {new_status}{RESET}")
            else:
                print(f"  {GREEN}✓ {ticker} thesis {new_status}{RESET}")

            entry["current_status"]   = new_status
            h["thesis_status"]        = new_status
            h["thesis_reasoning"]     = reasoning

    save_thesis_log(thesis_log)
    analysis["holdings"] = holdings

    statuses = [h.get("thesis_status", "INTACT") for h in holdings]
    print(f"{GREEN}✓ Thesis drift check complete — "
          f"{statuses.count('INTACT')} intact, "
          f"{statuses.count('WEAKENING')} weakening, "
          f"{statuses.count('BROKEN')} broken{RESET}")

    return analysis
