#!/usr/bin/env python3
# ============================================================
# MARKET AGENT V4.5 — Recommendation Simulator
# simulator.py
#
# Runs automatically as the final step of run_agent.sh, immediately
# after a real Run Agent execution saves logs/latest.json. Makes ZERO
# new API calls, ZERO market data fetches, and NEVER triggers its own
# Market Run — it only reads what the real pipeline already computed
# and saved, then mechanically applies those signals to THREE
# independent, parallel paper portfolios.
#
# THREE LEDGERS:
#   sim_7gate    — "Strict": only buys picks with zero caveats at all
#                  (both Gate 7 and Earnings Risk must have passed).
#   sim_6gate    — "Relaxed": buys any pick that cleared the core
#                  gates (1-4, RSI, Fear&Greed), regardless of Gate 7
#                  or Earnings Risk caveats.
#   sim_adaptive — "Adaptive": starts identical to Strict (same
#                  portfolio, same rules), then evolves its own
#                  strategy every single run based on evidence from
#                  its own closed-trade history — see ADAPTIVE LEARNING
#                  CYCLE below. Its only permanent hard rule is
#                  cash >= 0; everything else, including its own
#                  target cash percentage and which candidates it's
#                  willing to consider, is something it can learn to
#                  change. It can even buy stocks from picks_excluded
#                  (candidates the real gates rejected) if its own
#                  evolved criteria say the evidence supports it.
#
# Three ledgers running in parallel from the same starting point is
# the only clean comparison — a single ledger with switchable rules
# would produce a meaningless hybrid history depending only on when
# you happened to flip a switch.
#
# All three are cloned from your REAL current holdings + cash the
# FIRST time this script ever runs, then diverge independently from
# that identical starting point. This snapshot is never re-triggered
# automatically again.
#
# COMMISSION: every buy and sell across all three ledgers now incurs
# a trading cost (strategy.json's trading_costs section), matching
# real-world brokerage fees — this was previously zero-commission,
# which understated the real cost of frequent trading, especially for
# smaller positions where a flat fee is a larger fraction of the trade.
#
# Manual Watchlist alerts are deliberately excluded — the simulator
# only acts on signals the AGENT itself generated (agent_picks,
# reallocation swaps, stop-loss, and for Adaptive, picks_excluded),
# not on your own personal price targets.
#
# KNOWN SIMPLIFICATION: a newly-bought simulator-only position (one
# your real portfolio doesn't hold) has no stop-loss field available
# from agent_picks itself. This script uses a simple, clearly-labeled
# 10%-below-entry default for simulator-only buys rather than
# reverse-engineering the real system's stop-clock logic.
# ============================================================

import json
import os
import sys
from collections import defaultdict, deque
from datetime import datetime, timezone

import anthropic
from dotenv import load_dotenv
from claude_json import ask_json
from common import GREEN, RED, YELLOW, BLUE, BOLD, RESET, safe_float, trailing_stop_cfg, trail_width_pct, TRAIL_ACTIVATE_PCT

load_dotenv()  # matches agent_brain.py/stop_tracker.py/premarket_scanner.py —
                # ANTHROPIC_API_KEY lives in .env, not the shell environment.
                # Without this, os.getenv() below always returns None, even
                # with a perfectly valid key, which is exactly what happened
                # in production: Step 3 (agent_brain.py) succeeded because
                # IT calls load_dotenv(); Step 4 (this file) silently
                # couldn't see the same key seconds later.

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
LATEST_PATH   = os.path.join(BASE_DIR, "logs", "latest.json")
STATE_PATH    = os.path.join(BASE_DIR, "logs", "simulator_state.json")
STRATEGY_PATH = os.path.join(BASE_DIR, "strategy.json")
TRADE_HISTORY_PATH = os.path.join(BASE_DIR, "logs", "trade_history.json")

ADAPTIVE_EXIT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["TAKE_PROFIT", "TRIM", "LET_IT_RUN", "EXIT_STAGNANT", "CUT_LOSS", "HOLD"]},
        "reason": {"type": "string"},
    },
    "required": ["action", "reason"],
}


LEDGER_NAMES = ["sim_7gate", "sim_6gate", "sim_adaptive"]
NEW_POSITION_DEFAULT_STOP_PCT = 0.10  # see KNOWN SIMPLIFICATION above

# Adaptive learning cycle tuning — deliberately conservative defaults,
# both documented here and visible in the dashboard's rule history, so
# the reasoning behind any evolved rule is always traceable.
MIN_SAMPLE_SIZE = 30           # minimum closed trades in a category before Identify/Plan will act on a pattern
                               # (was 5 — rules fired on noise and the ledger "learned" itself into 100% cash)
RULE_EXPIRY_DAYS = 60          # an active rule is reverted after this long, so a rule that stopped all
                               # trading can't deadlock forever waiting for new closed trades
TRIM_COOLDOWN_DAYS = 10        # no new exit review for this long after a TRIM (it used to re-trim daily)
REVIEW_LOOKBACK_TRADES = 3     # minimum NEW closed trades since a rule was applied before Review will re-evaluate it
REVIEW_UNDERPERFORM_PCT = 5.0  # if Adaptive has lagged the Strict benchmark by this many percentage points since a rule was applied, the rule is reverted

# Adaptive exit assessment — Claude-judged TAKE_PROFIT/TRIM/LET_IT_RUN/
# EXIT_STAGNANT/HOLD verdict per Adaptive holding, per cycle. Mirrors
# stop_tracker.py's TAKE_PROFIT/TRIM/LET_IT_RUN pattern for real
# holdings, extended with a stagnation verdict since the Adaptive
# ledger has no analyst/thesis re-check to lean on. Only applies to
# sim_adaptive — sim_7gate/sim_6gate stay clean gate-only benchmarks.
# trim_fraction and min_hold_days_before_exit_eval are STARTING
# points stored in strategy_state (see default_adaptive_strategy_state),
# not hardcoded constants — they are fair game for the learning cycle
# to evolve exactly like category_multipliers already does.
SECTOR_ETF_MAP = {
    "Technology":             "XLK",
    "Healthcare":              "XLV",
    "Financials":              "XLF",
    "Financial Services":      "XLF",
    "Consumer Staples":        "XLP",
    "Energy":                  "XLE",
    "Industrials":             "XLI",
    "Consumer Discretionary":  "XLY",
    "Utilities":               "XLU",
    "Real Estate":             "XLRE",
    "Materials":               "XLB",
    "Communications":          "XLC",
    "Communication Services":  "XLC",
}


def load_latest():
    with open(LATEST_PATH) as f:
        return json.load(f)


def load_strategy():
    """Falls back to sensible defaults if the file is missing or
    unreadable — this should never actually block the simulator from
    running."""
    try:
        with open(STRATEGY_PATH) as f:
            return json.load(f)
    except Exception as e:
        print(f"{YELLOW}⚠ Could not read strategy.json ({e}) — using defaults{RESET}")
        return {"position_sizing": {"min_cash_pct": 15}, "trading_costs": {"commission_type": "fixed", "commission_fixed_usd": 25}}


def get_commission(strategy, trade_value):
    """
    Applied to every buy and every sell across all three ledgers,
    matching your real brokerage's per-transaction fee. commission_type
    picks which of the two numbers actually applies — "fixed" uses a
    flat dollar amount regardless of trade size, "percentage" scales
    with trade value instead. Previously this was zero for every
    simulator trade, which understated real trading costs — a flat fee
    is a much larger fraction of a small position than a large one.
    """
    tc = (strategy or {}).get("trading_costs", {})
    ctype = tc.get("commission_type", "fixed")
    if ctype == "percentage":
        return round(trade_value * (safe_float(tc.get("commission_pct", 0)) / 100.0), 2)
    return round(safe_float(tc.get("commission_fixed_usd", 0)), 2)


def clone_real_holdings(real_holdings, now_iso):
    cloned = []
    for h in real_holdings:
        cloned.append({
            "ticker":     h.get("ticker"),
            "name":       h.get("name") or h.get("company_name") or h.get("ticker"),
            "sector":     h.get("sector", "Other"),
            "quantity":   safe_float(h.get("quantity")),
            "buy_price":  safe_float(h.get("buy_price")),
            "stop":       safe_float(h.get("stop")),
            "entry_date": now_iso,
            "source":     "initial_snapshot",
            "category":   "clean",
            # No entry-time market snapshot available for the initial
            # clone (real holdings, bought before the simulator
            # existed) — exit assessment gracefully treats these as
            # N/A for relative-performance context, not a crash.
            "target_price_at_entry":     None,
            "spy_price_at_entry":        None,
            "sector_etf_at_entry":       None,
            "sector_etf_price_at_entry": None,
            # Real holdings ARE re-scored every run, so this is the best
            # available proxy for "entry" quality (today's assessment,
            # not the true original buy-time one, which was never
            # recorded) — still meaningfully better than leaving it None.
            "strategy_score_at_entry":   safe_float(h.get("strategy_score")) or None,
            "confidence_at_entry":       h.get("confidence"),
            "risk_level_at_entry":       h.get("risk_level"),
        })
    return cloned


def default_adaptive_strategy_state(min_cash_pct):
    """
    The Adaptive ledger's evolvable rule set. Starts as an exact copy
    of the Strict ledger's real starting rules (per your instruction —
    it must start from somewhere), then every field here is fair game
    for the learning cycle to change based on evidence. The only rule
    that's NEVER touched by the learning cycle is enforced directly in
    process_buys_adaptive: cash can never go negative.
    """
    return {
        "target_cash_pct":           min_cash_pct,
        "candidate_scope":           "gate_evaluated_only",
        "excluded_score_threshold":  70,
        "excluded_position_pct":     5,
        "category_multipliers": {
            "clean":          1.0,
            "gate7_only":     1.0,
            "earnings_only":  1.0,
            "both_caveats":   1.0,
            "excluded_pick":  1.0,
        },
        "avoid_categories":  [],
        "rule_history":      [],
        "last_cycle_closed_count": 0,
        # Exit-assessment starting points — evolvable, see run_adaptive_cycle.
        "trim_fraction":                   0.5,  # fraction of shares sold on a Claude TRIM verdict
        "min_hold_days_before_exit_eval":  7,    # position must be held this long before Claude reviews it for STAGNATION specifically
        "min_days_before_any_eval":        2,    # absolute floor — even a large early move isn't reviewed before this, to avoid day-0/1 noise
        "early_eval_move_threshold_pct":   8.0,  # abs(unrealized P&L%) at/above this bypasses the 7-day wait — a big gain or loss gets judged early
        # Signal-quality evolution — makes use of the rich per-pick scoring
        # Claude already generates every run (strategy/technical/fundamental/
        # valuation scores, confidence, risk level) instead of discarding it
        # after the buy decision. All evolvable, all start permissive.
        "avoid_confidence_levels":  [],  # e.g. ["Low"] once evidence supports it
        "avoid_risk_levels":        [],  # e.g. ["High"] once evidence supports it
        "min_strategy_score_floor": 0,   # 0 = no floor; can rise to 70/85 on evidence
    }


def load_or_init_state(latest, min_cash_pct):
    if os.path.exists(STATE_PATH):
        state = json.load(open(STATE_PATH))
        if "sim_adaptive" not in state:
            now = datetime.now(timezone.utc).isoformat()
            real_holdings = latest.get("holdings", [])
            real_cash = safe_float(latest.get("cash"))
            state["sim_adaptive"] = {
                "cash": real_cash,
                "holdings": clone_real_holdings(real_holdings, now),
                "closed_positions": [],
                "trade_log": [],
                "strategy_state": default_adaptive_strategy_state(min_cash_pct),
            }
            print(f"{BLUE}◆ Adaptive ledger added — initialized from current real portfolio, "
                  f"starting with Strict's rules{RESET}")
        return state

    now = datetime.now(timezone.utc).isoformat()
    real_holdings = latest.get("holdings", [])
    real_cash = safe_float(latest.get("cash"))
    real_total_capital = safe_float(latest.get("total_capital"))

    state = {
        "initialized_at": now,
        "initial_total_value": real_total_capital,
        "sim_7gate": {
            "cash": real_cash,
            "holdings": clone_real_holdings(real_holdings, now),
            "closed_positions": [],
            "trade_log": [],
        },
        "sim_6gate": {
            "cash": real_cash,
            "holdings": clone_real_holdings(real_holdings, now),
            "closed_positions": [],
            "trade_log": [],
        },
        "sim_adaptive": {
            "cash": real_cash,
            "holdings": clone_real_holdings(real_holdings, now),
            "closed_positions": [],
            "trade_log": [],
            "strategy_state": default_adaptive_strategy_state(min_cash_pct),
        },
    }
    print(f"{BLUE}◆ First run — initialized all three simulator ledgers from your current "
          f"real portfolio ({len(real_holdings)} holdings, ${real_cash:,.2f} cash){RESET}")
    return state


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH + ".tmp", "w") as f:  # write-then-rename: the dashboard never reads half a file
        json.dump(state, f, indent=2)
    os.replace(STATE_PATH + ".tmp", STATE_PATH)


def categorize_pick(pick):
    """Tags a pick by its caveat status at the moment it's bought, so
    closed-trade history can later be grouped by category for the
    Adaptive ledger's Identify phase."""
    g7_failed = pick.get("gate7_status") == "FAILED"
    er_failed = pick.get("earnings_status") == "FAILED"
    if g7_failed and er_failed:
        return "both_caveats"
    if g7_failed:
        return "gate7_only"
    if er_failed:
        return "earnings_only"
    return "clean"


def get_actionable_picks(agent_picks, ledger_name):
    """
    sim_7gate: only picks with ZERO caveats at all.
    sim_6gate / sim_adaptive (base pool): every entry in agent_picks.
    Adaptive's ADDITIONAL excluded-pool candidates are handled
    separately in process_buys_adaptive.
    """
    if ledger_name == "sim_7gate":
        return [
            p for p in agent_picks
            if p.get("gate7_status") == "PASSED" and p.get("earnings_status", "PASSED") == "PASSED"
        ]
    return list(agent_picks)


def find_reallocation_entry(reallocation, ticker):
    for r in (reallocation or []):
        if r.get("pick", {}).get("ticker") == ticker:
            return r
    return None


def build_price_map(latest):
    """Best-available current price for every ticker we might need,
    pulled from whatever section of latest.json already has it."""
    prices = {}
    for h in latest.get("holdings", []):
        if h.get("ticker") and h.get("price"):
            prices[h["ticker"]] = safe_float(h["price"])
    for p in latest.get("agent_picks", []):
        if p.get("ticker") and p.get("price"):
            prices[p["ticker"]] = safe_float(p["price"])
    for e in latest.get("picks_excluded", []):
        if e.get("ticker") and e.get("price"):
            prices[e["ticker"]] = safe_float(e["price"])
    perf_prices = latest.get("performance_prices", {})
    if isinstance(perf_prices, dict):
        for ticker, val in perf_prices.items():
            price = val.get("price") if isinstance(val, dict) else val
            if price:
                prices.setdefault(ticker, safe_float(price))
    return prices


def complete_price_map(price_map, state, latest):
    """Make sure every ticker any ledger holds has a real current price.

    latest.json only carries prices for today's holdings/picks/excluded,
    so a simulator-bought position that dropped out of today's picks used
    to have no price at all: it was valued at cost, could never hit its
    stop, looked "stagnant" to the exit review, and was sold at its stale
    entry price. Holding prices also only reached latest.json via
    portfolio_analysis.py — a step run_agent.sh allows to fail.

    Order: latest.json → data/market_data.json → a direct yfinance quote.
    Anything still missing is returned so callers can flag it and skip
    price-dependent actions instead of acting on a fake price."""
    md_path = os.path.join(BASE_DIR, "data", "market_data.json")
    try:
        with open(md_path) as f:
            for st in json.load(f).get("stocks", []):
                if st.get("ticker") and st.get("price") and not st.get("price_carried_forward"):
                    price_map.setdefault(st["ticker"], safe_float(st["price"]))
    except Exception:
        pass
    wanted = {h["ticker"] for name in LEDGER_NAMES for h in state[name]["holdings"]}
    wanted.add("SPY")
    for name in LEDGER_NAMES:
        for h in state[name]["holdings"]:
            etf = SECTOR_ETF_MAP.get(h.get("sector") or "")
            if etf:
                wanted.add(etf)
    missing = sorted(t for t in wanted if not price_map.get(t))
    if missing:
        try:
            import yfinance as yf
            for t in missing:
                try:
                    info = yf.Ticker(t).info
                    p = safe_float(info.get("regularMarketPrice") or info.get("currentPrice") or info.get("previousClose"))
                    if p > 0:
                        price_map[t] = p
                except Exception:
                    pass
        except Exception:
            pass
    unavailable = sorted(t for t in wanted if not price_map.get(t))
    for name in LEDGER_NAMES:
        for h in state[name]["holdings"]:
            h["price_unavailable"] = h["ticker"] in unavailable
    if unavailable:
        print(f"{YELLOW}⚠ No current price for {', '.join(unavailable)} — valued at cost and skipped for stops/exits this run{RESET}")
    return unavailable


def compute_total_capital(ledger, price_map):
    holdings_value = sum(
        h["quantity"] * price_map.get(h["ticker"], h["buy_price"])
        for h in ledger["holdings"]
    )
    return ledger["cash"] + holdings_value


def log_trade(ledger, action, ticker, quantity, price, commission=0.0, note=""):
    ledger["trade_log"].append({
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "action":     action,
        "ticker":     ticker,
        "quantity":   quantity,
        "price":      price,
        "commission": commission,
        "note":       note,
    })


def sizing_stop_fraction(pick):
    """L4: the stop the position was sized against (pick stop, widened to
    2x ATR), so a stop-out loses what the sizing assumed."""
    rs = pick.get("risk_sizing") or {}
    pct = safe_float(rs.get("sizing_stop_pct"))
    return pct / 100 if 0 < pct < 50 else NEW_POSITION_DEFAULT_STOP_PCT


def process_buys(ledger, picks, reallocation, price_map, min_cash_pct, strategy):
    """min_cash_pct is a HARD floor for Strict and Relaxed. The
    Adaptive ledger does NOT use this function — see
    process_buys_adaptive, which has no hard floor beyond cash >= 0."""
    held_tickers = {h["ticker"] for h in ledger["holdings"]}

    for pick in picks:
        ticker = pick.get("ticker")
        if not ticker or pick.get("signal") not in ("BUY", "STRONG BUY"):
            continue
        if ticker in held_tickers:
            continue

        realloc = find_reallocation_entry(reallocation, ticker)
        if not realloc:
            continue
        detail = realloc.get("detail", {})
        pct = safe_float(detail.get("correlation_adjusted_pct") or detail.get("suggested_pct"))
        if pct <= 0:
            continue

        price = safe_float(pick.get("current_price")) or price_map.get(ticker, 0)
        if price <= 0:
            continue

        total_capital   = compute_total_capital(ledger, price_map)
        cash_floor      = total_capital * (min_cash_pct / 100.0)
        deployable_cash = ledger["cash"] - cash_floor
        if deployable_cash <= 0:
            break

        target_dollar_size = min(total_capital * (pct / 100.0), deployable_cash)
        est_commission = get_commission(strategy, target_dollar_size)
        affordable = target_dollar_size - est_commission
        if affordable <= 0:
            continue
        shares = int(affordable // price)
        if shares <= 0:
            continue

        cost       = shares * price
        commission = get_commission(strategy, cost)
        total_cost = cost + commission
        if total_cost > ledger["cash"]:
            continue
        if cost < strategy.get("position_sizing", {}).get("min_order_usd", 0):
            continue  # scrap order — commission would be a large % of it

        ledger["cash"] -= total_cost
        ledger["holdings"].append({
            "ticker":     ticker,
            "name":       pick.get("name", ticker),
            "sector":     pick.get("sector", "Other"),
            "quantity":   shares,
            "buy_price":  price,
            "stop":       round(price * (1 - sizing_stop_fraction(pick)), 2),
            "atr_pct":    pick.get("atr_pct"),   # sets the trailing-stop width
            "entry_date": datetime.now(timezone.utc).isoformat(),
            "source":     "agent_pick",
            "category":   categorize_pick(pick),
        })
        log_trade(ledger, "BUY", ticker, shares, price, commission,
                  note=f"{pct:.1f}% target allocation, ${commission:.2f} commission")
        held_tickers.add(ticker)


def process_buys_adaptive(ledger, agent_picks, picks_excluded, reallocation, price_map, strategy):
    """
    CANDIDATE POOL: every entry in agent_picks, PLUS — if
    strategy_state.candidate_scope has evolved to
    "all_including_excluded" — any picks_excluded entry that reached
    full Claude analysis (stage != "pre-filter") and whose
    strategy_score clears the current excluded_score_threshold. Any
    category in avoid_categories is skipped entirely.

    SIZING: agent_picks use the same reallocation percentage as the
    other ledgers, scaled by the current category multiplier.
    Excluded-pool buys use a separate, smaller starting percentage.

    THE ONLY HARD RULE IS CASH >= 0. target_cash_pct is a soft
    preference only, never a hard block.
    """
    ss = ledger["strategy_state"]
    held_tickers = {h["ticker"] for h in ledger["holdings"]}
    avoid = set(ss.get("avoid_categories", []))
    avoid_confidence = set(ss.get("avoid_confidence_levels", []))
    avoid_risk = set(ss.get("avoid_risk_levels", []))
    score_floor = ss.get("min_strategy_score_floor", 0)
    multipliers = ss.get("category_multipliers", {})

    candidates = []
    for pick in agent_picks:
        if pick.get("signal") not in ("BUY", "STRONG BUY"):
            continue
        cat = categorize_pick(pick)
        if cat in avoid:
            continue
        confidence = pick.get("confidence")
        if confidence and confidence in avoid_confidence:
            continue
        risk_level = pick.get("risk_level")
        if risk_level and risk_level in avoid_risk:
            continue
        strategy_score = safe_float(pick.get("strategy_score"))
        if strategy_score < score_floor:
            continue
        realloc = find_reallocation_entry(reallocation, pick.get("ticker"))
        if not realloc:
            continue
        detail = realloc.get("detail", {})
        base_pct = safe_float(detail.get("correlation_adjusted_pct") or detail.get("suggested_pct"))
        if base_pct <= 0:
            continue
        candidates.append({
            "ticker":         pick.get("ticker"),
            "name":           pick.get("name", pick.get("ticker")),
            "sector":         pick.get("sector", "Other"),
            "category":       cat,
            "pct":            base_pct * multipliers.get(cat, 1.0),
            "source":         "agent_pick",
            "price":          safe_float(pick.get("current_price")),  # checked directly, same as process_buys, before falling back to price_map below
            "target_price":   safe_float(pick.get("target_price")) or None,
            "strategy_score": strategy_score or None,
            "confidence":     confidence,
            "risk_level":     risk_level,
        })

    if ss.get("candidate_scope") == "all_including_excluded" and "excluded_pick" not in avoid:
        threshold = ss.get("excluded_score_threshold", 70)
        base_pct  = ss.get("excluded_position_pct", 5)
        for excl in (picks_excluded or []):
            if excl.get("stage") == "pre-filter":
                continue
            score = safe_float(excl.get("strategy_score"))
            if score < threshold or score < score_floor:
                continue
            excl_confidence = excl.get("confidence")
            if excl_confidence and excl_confidence in avoid_confidence:
                continue
            excl_risk_level = excl.get("risk_level")
            if excl_risk_level and excl_risk_level in avoid_risk:
                continue
            candidates.append({
                "ticker":         excl.get("ticker"),
                "name":           excl.get("name", excl.get("ticker")),
                "sector":         excl.get("sector", "Other"),
                "category":       "excluded_pick",
                "pct":            base_pct * multipliers.get("excluded_pick", 1.0),
                "source":         "excluded_pick",
                "price":          safe_float(excl.get("price")),
                "target_price":   None,  # excluded picks don't carry a target_price in latest.json
                "strategy_score": score or None,
                # agent_brain.py now includes these on excluded picks too
                # (previously dropped even though computed) — still
                # gracefully None for any older latest.json snapshot
                # saved before that fix.
                "confidence":     excl_confidence,
                "risk_level":     excl_risk_level,
            })

    category_priority = {"clean": 0, "gate7_only": 1, "earnings_only": 1, "both_caveats": 2, "excluded_pick": 3}
    candidates.sort(key=lambda c: category_priority.get(c["category"], 9))

    for cand in candidates:
        ticker = cand["ticker"]
        if not ticker or ticker in held_tickers:
            continue
        price = cand.get("price") or price_map.get(ticker, 0)
        if price <= 0:
            continue

        total_capital = compute_total_capital(ledger, price_map)
        target_dollar_size = total_capital * (cand["pct"] / 100.0)
        spend_target = target_dollar_size if target_dollar_size <= ledger["cash"] else ledger["cash"]
        est_commission = get_commission(strategy, spend_target)
        affordable = spend_target - est_commission
        if affordable <= 0:
            continue
        shares = int(affordable // price)
        if shares <= 0:
            continue

        cost       = shares * price
        commission = get_commission(strategy, cost)
        total_cost = cost + commission
        if total_cost > ledger["cash"]:
            continue
        if cost < strategy.get("position_sizing", {}).get("min_order_usd", 0):
            continue  # scrap order — commission would be a large % of it  # the one true hard rule

        sector_etf = SECTOR_ETF_MAP.get(cand["sector"])
        ledger["cash"] -= total_cost
        ledger["holdings"].append({
            "ticker":     ticker,
            "name":       cand["name"],
            "sector":     cand["sector"],
            "quantity":   shares,
            "buy_price":  price,
            "stop":       round(price * (1 - NEW_POSITION_DEFAULT_STOP_PCT), 2),
            "entry_date": datetime.now(timezone.utc).isoformat(),
            "source":     cand["source"],
            "category":   cand["category"],
            # Snapshot fields for the exit assessment — captured once at
            # entry so the Claude-judged review never needs a fresh
            # market fetch, only what's already in latest.json.
            "target_price_at_entry":      cand.get("target_price"),
            "spy_price_at_entry":         price_map.get("SPY") or None,
            "sector_etf_at_entry":        sector_etf,
            "sector_etf_price_at_entry":  price_map.get(sector_etf) if sector_etf else None,
            # Signal-quality snapshot — this is the "use what I already
            # paid for" data: captured once at buy time so Identify can
            # later learn from score/confidence/risk patterns, not just
            # which gates a pick cleared.
            "strategy_score_at_entry":    cand.get("strategy_score"),
            "confidence_at_entry":        cand.get("confidence"),
            "risk_level_at_entry":        cand.get("risk_level"),
        })
        log_trade(ledger, "BUY", ticker, shares, price, commission,
                  note=f"{cand['category']}, {cand['pct']:.1f}% target, ${commission:.2f} commission")
        held_tickers.add(ticker)


def process_sells(ledger, reallocation, price_map, strategy):
    """Applies SELL/swap suggestions from the reallocation engine."""
    held_by_ticker = {h["ticker"]: h for h in ledger["holdings"]}

    for r in (reallocation or []):
        swap = r.get("swap")
        if not swap:
            continue
        sell_ticker = swap.get("ticker")
        if not sell_ticker or sell_ticker not in held_by_ticker:
            continue

        holding = held_by_ticker[sell_ticker]
        if not price_map.get(sell_ticker):
            continue  # never sell at a made-up price — no current quote this run
        price = price_map[sell_ticker]
        gross_proceeds = holding["quantity"] * price
        commission = get_commission(strategy, gross_proceeds)
        net_proceeds = gross_proceeds - commission
        pnl_usd = net_proceeds - (holding["quantity"] * holding["buy_price"])
        pnl_pct = ((price / holding["buy_price"]) - 1) * 100 if holding["buy_price"] > 0 else 0

        ledger["cash"] += net_proceeds
        ledger["closed_positions"].append({
            "ticker":     sell_ticker,
            "name":       holding.get("name", sell_ticker),
            "quantity":   holding["quantity"],
            "buyPrice":   holding["buy_price"],
            "sellPrice":  price,
            "sellDate":   datetime.now(timezone.utc).isoformat(),
            "proceeds":   net_proceeds,
            "commission": commission,
            "pnlUsd":     round(pnl_usd, 2),
            "pnlPct":     round(pnl_pct, 2),
            "reason":     swap.get("reason", "Reallocation swap"),
            "category":   holding.get("category", "clean"),
            "exit_type":  "SWAP",
            "strategy_score_at_entry": holding.get("strategy_score_at_entry"),
            "confidence_at_entry":     holding.get("confidence_at_entry"),
            "risk_level_at_entry":     holding.get("risk_level_at_entry"),
        })
        log_trade(ledger, "SELL", sell_ticker, holding["quantity"], price, commission,
                  note=swap.get("reason", "Reallocation swap"))
        ledger["holdings"] = [h for h in ledger["holdings"] if h["ticker"] != sell_ticker]
        held_by_ticker.pop(sell_ticker, None)


def apply_trailing_stops(ledger, price_map, strategy):
    """Strict/Relaxed had no profit-taking at all (a +60% winner rode back
    down untouched). Mirror the strategy's trailing_stop: once a position
    is activate_at_pct up, keep its stop trail_width_pct() below the
    highest price seen. process_stop_losses() then exits on that stop."""
    activate = safe_float(trailing_stop_cfg(strategy).get("activate_at_pct"), TRAIL_ACTIVATE_PCT)
    for h in ledger["holdings"]:
        trail = trail_width_pct(h.get("atr_pct"), strategy)
        price = price_map.get(h["ticker"])
        if not price or not h.get("buy_price"):
            continue
        h["high_water"] = max(h.get("high_water") or 0, price)
        if (h["high_water"] / h["buy_price"] - 1) * 100 >= activate:
            trail_stop = round(h["high_water"] * (1 - trail / 100), 2)
            if trail_stop > (h.get("stop") or 0):
                h["stop"] = trail_stop
            h["trailing_active"] = True


def process_stop_losses(ledger, price_map, strategy):
    """Immediate stop-loss check: current price at/below the stored
    stop level triggers a sell."""
    still_held = []
    for h in ledger["holdings"]:
        if not price_map.get(h["ticker"]):
            still_held.append(h)  # no quote — can't judge the stop; flagged by complete_price_map
            continue
        price = price_map[h["ticker"]]
        if h.get("stop") and price <= h["stop"]:
            gross_proceeds = h["quantity"] * price
            commission = get_commission(strategy, gross_proceeds)
            net_proceeds = gross_proceeds - commission
            pnl_usd = net_proceeds - (h["quantity"] * h["buy_price"])
            pnl_pct = ((price / h["buy_price"]) - 1) * 100 if h["buy_price"] > 0 else 0
            ledger["cash"] += net_proceeds
            ledger["closed_positions"].append({
                "ticker":     h["ticker"],
                "name":       h.get("name", h["ticker"]),
                "quantity":   h["quantity"],
                "buyPrice":   h["buy_price"],
                "sellPrice":  price,
                "sellDate":   datetime.now(timezone.utc).isoformat(),
                "proceeds":   net_proceeds,
                "commission": commission,
                "pnlUsd":     round(pnl_usd, 2),
                "pnlPct":     round(pnl_pct, 2),
                "reason":     "Trailing stop — profit protected" if h.get("trailing_active") and price > h["buy_price"] else "Stop-loss triggered",
                "category":   h.get("category", "clean"),
                "exit_type":  "TRAILING_STOP" if h.get("trailing_active") and price > h["buy_price"] else "STOP_LOSS",
                "strategy_score_at_entry": h.get("strategy_score_at_entry"),
                "confidence_at_entry":     h.get("confidence_at_entry"),
                "risk_level_at_entry":     h.get("risk_level_at_entry"),
            })
            log_trade(ledger, "SELL", h["ticker"], h["quantity"], price, commission,
                      note="Trailing stop — profit protected" if h.get("trailing_active") and price > h["buy_price"] else "Stop-loss triggered")
        else:
            still_held.append(h)
    ledger["holdings"] = still_held


# ============================================================
# ADAPTIVE EXIT ASSESSMENT — Claude-judged TAKE_PROFIT / TRIM /
# LET_IT_RUN / EXIT_STAGNANT / HOLD, evaluated once per eligible
# Adaptive holding per cycle. This is the ONLY new API call this
# script makes (see file header — everything else stays a pure
# mechanical pass over what run_agent.sh already computed). It only
# runs from within a real Run Agent execution (Step 4 of
# run_agent.sh) or the dashboard's /reset-simulator button — never
# on its own schedule.
#
# Fixes the deadlock where the Adaptive ledger had no way to ever
# close a position except a hard stop-loss or a reallocation-engine
# swap targeting that exact ticker — meaning trades could sit open
# indefinitely, closed_positions could stay empty, and the learning
# cycle below could never reach MIN_SAMPLE_SIZE to evolve anything.
# ============================================================

def get_adaptive_exit_verdict(ticker, sector, entry_price, current_price,
                               days_held, target_price, spy_return_pct,
                               sector_return_pct, sector_etf,
                               days_to_earnings=None, upcoming_macro_events=None,
                               fear_greed=None, market_mood=None,
                               early_review_reason=None):
    """
    Calls Claude to assess an Adaptive-ledger holding. Returns dict:
    {action: TAKE_PROFIT|TRIM|LET_IT_RUN|EXIT_STAGNANT|CUT_LOSS|HOLD, reason: str}
    Defaults to HOLD (take no action) on any failure — the safest
    default, since it leaves the position exactly as-is rather than
    forcing an exit or a trim on missing/bad data.

    early_review_reason, if set, means this holding hasn't cleared the
    normal min_hold_days_before_exit_eval window yet — it's being
    reviewed early because its unrealized P&L crossed
    early_eval_move_threshold_pct. A big loss shouldn't have to sit
    and drift for days waiting on an arbitrary hold period before
    anyone asks whether to cut it, and a big gain shouldn't wait
    either. EXIT_STAGNANT is never the right call in this case by
    definition (a large move is the opposite of stagnant) — the prompt
    below makes that explicit rather than leaving Claude to infer it.

    days_to_earnings, upcoming_macro_events, fear_greed, and
    market_mood are all context the real Run Agent pipeline ALREADY
    computes every run (agent_brain.py's earnings/macro_events/
    fear_greed sections) — this reuses it rather than fetching
    anything new, so the "makes zero new market data fetches" header
    claim still holds for everything except the Claude call itself.
    """
    try:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            print(f"  {YELLOW}⚠ Adaptive exit verdict for {ticker}: Claude API not configured — no action taken.{RESET}")
            return {"action": "HOLD", "reason": "Claude API not configured — no action taken."}

        client = anthropic.Anthropic(api_key=api_key)

        pnl_pct = ((current_price / entry_price) - 1) * 100 if entry_price > 0 else 0

        if target_price and target_price > 0:
            target_str = f"${target_price:.2f} ({((current_price / target_price) - 1) * 100:+.1f}% vs target)"
        else:
            target_str = "N/A (position sourced outside a fresh agent pick with no stored target)"

        spy_str    = f"{spy_return_pct:+.1f}%" if spy_return_pct is not None else "N/A"
        sector_str = f"{sector_etf} {sector_return_pct:+.1f}%" if sector_return_pct is not None and sector_etf else "N/A"
        relative_str = (f"{pnl_pct - spy_return_pct:+.1f} pts vs SPY"
                         if spy_return_pct is not None else "N/A")

        if days_to_earnings is not None:
            earnings_str = f"{days_to_earnings} days away" + (" — binary risk, treat any move here cautiously" if days_to_earnings <= 7 else "")
        else:
            earnings_str = "None scheduled / not tracked for this ticker"

        if upcoming_macro_events:
            macro_lines = "\n".join(
                f"    - {e.get('event','Unknown event')} in {e.get('days_away','?')} days (impact: {e.get('impact','?')})"
                for e in upcoming_macro_events
            )
            macro_str = f"\n{macro_lines}"
        else:
            macro_str = " None in the near term"

        fg_str = (f"{fear_greed.get('score','?')} ({fear_greed.get('rating','?')}), trend {fear_greed.get('direction','?')}"
                   if fear_greed else "N/A")
        mood_str = market_mood or "N/A"

        if early_review_reason:
            timing_note = (f"\nEARLY REVIEW: this position has only been held {days_held} days — normally it wouldn't "
                            f"be reviewed yet — but it's being reviewed now specifically because {early_review_reason}. "
                            f"Given that, EXIT_STAGNANT is NOT a valid conclusion here (a large move is the opposite "
                            f"of stagnant); weigh TAKE_PROFIT/TRIM/CUT_LOSS/LET_IT_RUN/HOLD on their merits instead.")
        else:
            timing_note = ""

        prompt = f"""You are assessing one position in an experimental, self-learning paper-trading portfolio (the "Adaptive Simulator"). This portfolio evolves its own rules from evidence — your job here is only to judge THIS position's exit status, not to apply any personal strategy preference.

POSITION DATA:
  Ticker:              {ticker}
  Sector:              {sector or 'Unknown'}
  Entry price:         ${entry_price:.2f}
  Current price:       ${current_price:.2f}
  Unrealized P&L:      {pnl_pct:+.2f}%
  Days held:           {days_held}
  Analyst target:      {target_str}
  Days to earnings:    {earnings_str}
{timing_note}

RELATIVE PERFORMANCE SINCE ENTRY:
  SPY (S&P 500):       {spy_str}
  Sector ETF:          {sector_str}
  This position vs SPY: {relative_str}

MARKET CONTEXT TODAY:
  Fear & Greed:        {fg_str}
  Market mood:         {mood_str}
  Upcoming high-impact macro events:{macro_str}

TASK:
Recommend ONE of six actions:

TAKE_PROFIT — Meaningful gain achieved, risk/reward now unfavourable. Exit the full position.

TRIM — Solid gain, but position still has plausible upside. Take partial profit now, let the remainder run.

LET_IT_RUN — Strong, still-intact momentum with real room left. No gain worth locking in yet, or thesis clearly still developing favourably. Keep the full position.

EXIT_STAGNANT — No meaningful price movement for the hold period AND underperforming its benchmark (SPY/sector) by a real margin, with NO clear near-term catalyst (no earnings imminent, no major macro event that plausibly explains the pause). This is dead capital that could be redeployed. Exit the full position. If a real catalyst is imminent (earnings within ~7 days, or a high-impact macro event that plausibly explains the stall), prefer HOLD instead — the stagnation may be temporary and pre-catalyst, not genuinely dead capital.

CUT_LOSS — A real, meaningful unrealized LOSS (not yet at the hard stop-loss level, but a genuine loss) where momentum, relative performance, and/or the thesis have clearly deteriorated with no near-term catalyst to justify riding it out further. Exit the full position now rather than waiting for the hard stop to trigger. Do not use this for a small/normal drawdown that's still within reasonable noise — only for a loss that looks like it's genuinely going the wrong way.

HOLD — None of the above clearly applies yet (e.g. modest move either direction, roughly tracking its benchmark, or a stall/loss explained by a pending catalyst worth waiting for).

Respond ONLY with valid JSON, no markdown:
{{"action": "TAKE_PROFIT" | "TRIM" | "LET_IT_RUN" | "EXIT_STAGNANT" | "CUT_LOSS" | "HOLD", "reason": "2-3 sentence explanation referencing the specific data above."}}"""

        result = ask_json(client, prompt, max_tokens=350, label=f"simulator_{ticker}", schema=ADAPTIVE_EXIT_SCHEMA)

        if result.get("action") not in ("TAKE_PROFIT", "TRIM", "LET_IT_RUN", "EXIT_STAGNANT", "CUT_LOSS", "HOLD"):
            result["action"] = "HOLD"
        if "reason" not in result:
            result["reason"] = ""

        print(f"  {GREEN}✓ Adaptive exit verdict for {ticker}: {result['action']}{RESET}")
        if result["reason"]:
            print(f"      {result['reason']}")
        return result

    except Exception as e:
        print(f"  {YELLOW}⚠ Claude API error for adaptive exit assessment ({ticker}): {e}{RESET}")
        return {"action": "HOLD", "reason": f"Could not reach Claude API — no action taken ({e})."}


def _close_adaptive_holding(ledger, holding, price, strategy, reason, exit_type):
    """Fully closes one Adaptive holding — shared by TAKE_PROFIT and
    EXIT_STAGNANT verdicts. Mirrors the closed_positions shape used by
    process_sells / process_stop_losses, tagged with exit_type so
    adaptive_identify can group by outcome type."""
    gross_proceeds = holding["quantity"] * price
    commission = get_commission(strategy, gross_proceeds)
    net_proceeds = gross_proceeds - commission
    pnl_usd = net_proceeds - (holding["quantity"] * holding["buy_price"])
    pnl_pct = ((price / holding["buy_price"]) - 1) * 100 if holding["buy_price"] > 0 else 0

    ledger["cash"] += net_proceeds
    ledger["closed_positions"].append({
        "ticker":     holding["ticker"],
        "name":       holding.get("name", holding["ticker"]),
        "quantity":   holding["quantity"],
        "buyPrice":   holding["buy_price"],
        "sellPrice":  price,
        "sellDate":   datetime.now(timezone.utc).isoformat(),
        "proceeds":   net_proceeds,
        "commission": commission,
        "pnlUsd":     round(pnl_usd, 2),
        "pnlPct":     round(pnl_pct, 2),
        "reason":     reason,
        "category":   holding.get("category", "clean"),
        "exit_type":  exit_type,
        "strategy_score_at_entry": holding.get("strategy_score_at_entry"),
        "confidence_at_entry":     holding.get("confidence_at_entry"),
        "risk_level_at_entry":     holding.get("risk_level_at_entry"),
    })
    log_trade(ledger, "SELL", holding["ticker"], holding["quantity"], price, commission, note=reason)


def run_adaptive_exit_assessment(ledger, price_map, strategy, latest):
    """Runs the Claude-judged exit review on every eligible Adaptive
    holding (past its min_hold_days_before_exit_eval), applying
    TAKE_PROFIT / TRIM / EXIT_STAGNANT immediately. LET_IT_RUN and
    HOLD make no change to the position.

    latest is the full run_agent.sh output — used here ONLY to reuse
    macro_events/earnings/fear_greed/market_mood that the real
    pipeline already computed this run. No new fetch of any kind."""
    ss = ledger["strategy_state"]
    trim_fraction = ss.get("trim_fraction", 0.5)
    min_hold_days = ss.get("min_hold_days_before_exit_eval", 7)
    min_days_any  = ss.get("min_days_before_any_eval", 2)
    early_threshold = ss.get("early_eval_move_threshold_pct", 8.0)
    now = datetime.now(timezone.utc)

    earnings_map = {e.get("ticker"): e for e in (latest.get("earnings") or [])}
    all_macro = latest.get("macro_events") or []
    # Only high-impact events within a 10-day window are relevant to an
    # exit call — a low-impact event three weeks out isn't a catalyst
    # worth weighing against a stagnation verdict today.
    upcoming_macro = [
        e for e in all_macro
        if e.get("impact") == "HIGH" and isinstance(e.get("days_away"), (int, float)) and 0 <= e["days_away"] <= 10
    ]
    fear_greed = latest.get("fear_greed") or {}
    market_mood = latest.get("market_mood")

    still_held = []
    calls_made = 0
    not_yet_eligible = 0
    for h in ledger["holdings"]:
        ticker = h["ticker"]
        if not price_map.get(ticker):
            still_held.append(h)  # no quote — a frozen price would look "stagnant"
            continue
        if h.get("last_trimmed_at"):
            try:
                since = (now - datetime.fromisoformat(h["last_trimmed_at"])).days
            except (ValueError, TypeError):
                since = TRIM_COOLDOWN_DAYS
            if since < TRIM_COOLDOWN_DAYS:
                still_held.append(h)  # just trimmed — don't halve it again tomorrow
                not_yet_eligible += 1
                continue
        price = price_map[ticker]

        try:
            entry_dt = datetime.fromisoformat(h["entry_date"])
            if entry_dt.tzinfo is None:
                entry_dt = entry_dt.replace(tzinfo=timezone.utc)
            days_held = (now.date() - entry_dt.date()).days  # calendar days — not sensitive to the time of day of the run
        except (ValueError, TypeError, KeyError):
            days_held = 0

        buy_price = h.get("buy_price") or 0
        pnl_pct_now = ((price / buy_price) - 1) * 100 if buy_price > 0 else 0

        # Eligibility: the 7-day window (min_hold_days) exists to avoid
        # calling a position "stagnant" before enough time has passed to
        # actually judge that — it was never meant to block judgment on
        # a position that has already moved a lot. So: always require
        # min_days_before_any_eval (pure noise floor), but a big enough
        # move gets reviewed immediately after that, before the full
        # 7-day window, via early_eval_move_threshold_pct.
        early_review_reason = None
        if days_held < min_days_any:
            print(f"  {YELLOW}⏳ {ticker}: not yet eligible for exit review ({days_held}/{min_days_any} min days held){RESET}")
            not_yet_eligible += 1
            still_held.append(h)
            continue
        elif days_held < min_hold_days:
            if abs(pnl_pct_now) >= early_threshold:
                direction = "gain" if pnl_pct_now > 0 else "loss"
                early_review_reason = (f"its unrealized {direction} of {pnl_pct_now:+.1f}% has already crossed "
                                        f"the {early_threshold}% early-review threshold")
            else:
                print(f"  {YELLOW}⏳ {ticker}: not yet eligible for exit review "
                      f"({days_held}/{min_hold_days} days held, {pnl_pct_now:+.1f}% — below the "
                      f"{early_threshold}% early-review threshold){RESET}")
                not_yet_eligible += 1
                still_held.append(h)
                continue

        spy_now  = price_map.get("SPY")
        spy_then = h.get("spy_price_at_entry")
        spy_return_pct = ((spy_now / spy_then) - 1) * 100 if spy_now and spy_then else None

        sector_etf = h.get("sector_etf_at_entry")
        sector_now  = price_map.get(sector_etf) if sector_etf else None
        sector_then = h.get("sector_etf_price_at_entry")
        sector_return_pct = ((sector_now / sector_then) - 1) * 100 if sector_now and sector_then else None

        earnings_entry = earnings_map.get(ticker)
        days_to_earnings = earnings_entry.get("days_away") if earnings_entry else None

        calls_made += 1
        verdict = get_adaptive_exit_verdict(
            ticker=ticker,
            sector=h.get("sector"),
            entry_price=h["buy_price"],
            current_price=price,
            days_held=days_held,
            target_price=h.get("target_price_at_entry"),
            spy_return_pct=spy_return_pct,
            sector_return_pct=sector_return_pct,
            sector_etf=sector_etf,
            days_to_earnings=days_to_earnings,
            upcoming_macro_events=upcoming_macro,
            fear_greed=fear_greed,
            market_mood=market_mood,
            early_review_reason=early_review_reason,
        )
        action = verdict["action"]
        reason = f"Claude-judged {action}: {verdict.get('reason', '')}".strip()

        # Persisted on the holding itself so the dashboard can show WHY,
        # not just what — this is the one thing Salim couldn't verify
        # without SSH-ing in and grepping scheduler.log. Set here, before
        # branching, so it applies whether the position closes, trims, or
        # stays untouched (HOLD/LET_IT_RUN still get recorded — a verdict
        # that took no action is still a real judgment worth seeing).
        h["last_exit_verdict"]    = action
        h["last_exit_reason"]     = verdict.get("reason", "")
        h["last_exit_verdict_at"] = datetime.now(timezone.utc).isoformat()

        if action == "TAKE_PROFIT":
            _close_adaptive_holding(ledger, h, price, strategy, reason, "TAKE_PROFIT")
            # position closed — not carried into still_held
        elif action == "EXIT_STAGNANT":
            _close_adaptive_holding(ledger, h, price, strategy, reason, "EXIT_STAGNANT")
        elif action == "CUT_LOSS":
            _close_adaptive_holding(ledger, h, price, strategy, reason, "CUT_LOSS")
        elif action == "TRIM":
            shares_to_trim = max(1, int(h["quantity"] * trim_fraction))
            shares_to_trim = min(shares_to_trim, h["quantity"])
            if shares_to_trim >= h["quantity"]:
                # trim_fraction has evolved to effectively 100% — treat as a full close
                _close_adaptive_holding(ledger, h, price, strategy, reason, "TRIM")
            else:
                gross_proceeds = shares_to_trim * price
                commission = get_commission(strategy, gross_proceeds)
                net_proceeds = gross_proceeds - commission
                ledger["cash"] += net_proceeds
                h["quantity"] -= shares_to_trim
                h["last_trimmed_at"] = datetime.now(timezone.utc).isoformat()
                log_trade(ledger, "SELL", ticker, shares_to_trim, price, commission, note=reason)
                still_held.append(h)
        else:  # LET_IT_RUN or HOLD — no change
            still_held.append(h)

    ledger["holdings"] = still_held
    ledger["last_exit_assessment_calls"] = calls_made
    ledger["last_exit_assessment_not_yet_eligible"] = not_yet_eligible
    if calls_made or not_yet_eligible:
        print(f"{BLUE}◆ Adaptive exit assessment — {calls_made} Claude call(s) made, "
              f"{not_yet_eligible} position(s) not yet eligible this cycle{RESET}")


# ============================================================
# CROSS-TRADER EVIDENCE — observe outcomes from Strict, Relaxed, and
# your own real trades as ADDITIONAL evidence for the Adaptive
# ledger's own Plan step. This never copies anyone's actions — it
# only lets Adaptive compare its own outcomes against theirs, the
# same way Review already compares Adaptive's total return against
# Strict's. Salim's principle, stated directly: "one need to learn
# from everybody and not only his own actions" — observing another
# trader's outcome to inform your own judgment isn't being
# influenced, it's being smart.
# ============================================================

def load_trade_history():
    """Reads your real trade_history.json if it exists. Returns []
    if the file is missing or unreadable — this is optional evidence,
    never a hard dependency."""
    if not os.path.exists(TRADE_HISTORY_PATH):
        return []
    try:
        with open(TRADE_HISTORY_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def compute_real_trade_round_trips(trade_history):
    """FIFO-matches your real BUY/SELL trade_history.json entries per
    ticker into closed round-trips. This is observational only — nothing
    here changes what you actually do, it only lets Adaptive compare
    outcomes. Sells that predate any recorded buy for that ticker (e.g.
    a position you already held before this feature existed) are
    silently skipped rather than guessed at."""
    lots = defaultdict(deque)  # ticker -> deque of [remaining_qty, cost_per_share_incl_commission]
    round_trips = []

    def sort_key(t):
        return t.get("trade_date") or t.get("recorded_at") or ""

    for t in sorted(trade_history, key=sort_key):
        ticker = t.get("ticker")
        action = t.get("action")
        qty = safe_float(t.get("quantity"))
        price = safe_float(t.get("price"))
        commission = safe_float(t.get("commission"))
        if not ticker or qty <= 0 or price <= 0:
            continue

        if action == "BUY":
            cost_per_share = price + (commission / qty)
            lots[ticker].append([qty, cost_per_share])
        elif action == "SELL":
            remaining = qty
            sell_commission_per_share = commission / qty
            while remaining > 1e-9 and lots[ticker]:
                lot = lots[ticker][0]
                matched_qty = min(lot[0], remaining)
                buy_price = lot[1]
                sell_price_net = price - sell_commission_per_share
                pnl_pct = ((sell_price_net / buy_price) - 1) * 100 if buy_price > 0 else 0
                pnl_usd = (sell_price_net - buy_price) * matched_qty
                round_trips.append({"ticker": ticker, "pnlUsd": pnl_usd, "pnlPct": pnl_pct})
                lot[0] -= matched_qty
                remaining -= matched_qty
                if lot[0] <= 1e-9:
                    lots[ticker].popleft()
            # remaining > 0 with no lot left = a sell with no recorded
            # buy behind it — silently skipped, not fabricated.

    return round_trips


def compute_real_trade_stats(trade_history):
    """Your own real-trade win rate / avg return, gated at
    MIN_SAMPLE_SIZE same as every other evidence source. Purely
    observational — printed for visibility, not yet wired to any
    Plan rule, since real trades aren't tagged with a gate category or
    confidence/risk level the way simulator/agent picks are, so there's
    no clean lever for Adaptive to act on from this alone yet."""
    round_trips = compute_real_trade_round_trips(trade_history)
    if len(round_trips) < MIN_SAMPLE_SIZE:
        return None
    wins = [r for r in round_trips if r["pnlUsd"] > 0]
    return {
        "count":       len(round_trips),
        "win_rate":    round(len(wins) / len(round_trips) * 100, 1),
        "avg_pnl_pct": round(sum(r["pnlPct"] for r in round_trips) / len(round_trips), 2),
    }


def compute_cross_ledger_clean_stats(state):
    """Strict and Relaxed only ever buy 'clean' picks and only ever
    close via stop-loss or reallocation swap — no trim/stagnant/
    take-profit logic. Comparing Adaptive's OWN clean-category outcomes
    against theirs, on the same buy universe, isolates whether
    Adaptive's active exit management is actually helping — a sharper
    signal than the portfolio-level benchmark Review already uses."""
    out = {}
    for ledger_name, label in (("sim_7gate", "Strict"), ("sim_6gate", "Relaxed")):
        ledger = state.get(ledger_name, {})
        closed = ledger.get("closed_positions", [])
        clean = [c for c in closed if c.get("category", "clean") == "clean"]
        if len(clean) >= MIN_SAMPLE_SIZE:
            wins = [c for c in clean if c.get("pnlUsd", 0) > 0]
            out[label] = {
                "count":       len(clean),
                "win_rate":    round(len(wins) / len(clean) * 100, 1),
                "avg_pnl_pct": round(sum(c.get("pnlPct", 0) for c in clean) / len(clean), 2),
            }
    return out


# ============================================================
# ADAPTIVE LEARNING CYCLE — Identify, Plan, Execute, Review
# Runs every single time this script runs. At most ONE new rule
# change per cycle, so each change stays individually traceable in
# the rule history.
# ============================================================

def _score_band(score):
    """Buckets a strategy_score into a band for evidence grouping. Fixed
    bands (not evolvable) — only the FLOOR the ledger buys above is
    evolvable (min_strategy_score_floor); the bands themselves are just
    a stable way to group evidence, matching the 70-point threshold
    already used elsewhere (excluded_score_threshold)."""
    if score is None:
        return None
    if score >= 85:
        return "high_score_85plus"
    if score >= 70:
        return "mid_score_70to84"
    return "low_score_under70"


def adaptive_identify(closed_positions):
    """Groups the Adaptive ledger's own closed trades along FOUR
    evidence dimensions, each independently gated at MIN_SAMPLE_SIZE:
      - category    (gate-caveat combination at buy time — existing)
      - exit_type   (TAKE_PROFIT/TRIM/EXIT_STAGNANT/STOP_LOSS/SWAP)
      - confidence  (Claude's confidence at buy time, if recorded)
      - risk_level  (Claude's risk assessment at buy time, if recorded)
      - score_band  (strategy_score bucketed — see _score_band)
    confidence/risk_level/score_band make use of the rich per-pick
    scoring Claude already generates every run, which previously was
    read once for display and then discarded — see adaptive_plan for
    how this expands the rule space beyond gate category alone.
    Returns a dict of dimension -> {group -> stats}."""
    dims = {
        "category":    defaultdict(list),
        "exit_type":   defaultdict(list),
        "confidence":  defaultdict(list),
        "risk_level":  defaultdict(list),
        "score_band":  defaultdict(list),
    }
    for cp in closed_positions:
        dims["category"][cp.get("category", "clean")].append(cp)
        dims["exit_type"][cp.get("exit_type", "unknown")].append(cp)
        conf = cp.get("confidence_at_entry")
        if conf:
            dims["confidence"][conf].append(cp)
        risk = cp.get("risk_level_at_entry")
        if risk:
            dims["risk_level"][risk].append(cp)
        band = _score_band(cp.get("strategy_score_at_entry"))
        if band:
            dims["score_band"][band].append(cp)

    def summarize(trades):
        wins = [t for t in trades if t.get("pnlUsd", 0) > 0]
        return {
            "count":       len(trades),
            "win_rate":    round(len(wins) / len(trades) * 100, 1),
            "avg_pnl_pct": round(sum(t.get("pnlPct", 0) for t in trades) / len(trades), 2),
        }

    return {
        dim: {group: summarize(trades) for group, trades in groups.items() if len(trades) >= MIN_SAMPLE_SIZE}
        for dim, groups in dims.items()
    }


def adaptive_plan(strategy_state, identify, cross_ledger_clean=None):
    """Proposes at most one new rule change per cycle. Returns None if
    nothing in the current evidence clearly warrants a change.

    identify is the full dict returned by adaptive_identify.
    cross_ledger_clean is Strict/Relaxed's clean-category outcomes (see
    compute_cross_ledger_clean_stats) — observational evidence from
    other traders, used only to inform Adaptive's OWN rule evolution,
    never to copy their actions. Checked in a fixed priority order,
    first genuine signal wins — each dimension below is checked in
    turn rather than all at once, so a cycle never proposes two
    conflicting changes:

      1. EXIT_STAGNANT evidence      -> extend min_hold_days_before_exit_eval
      2. cross-ledger underperformance -> extend min_hold_days_before_exit_eval
      3. worst gate category         -> avoid_category
      4. worst confidence level      -> avoid_confidence_level
      5. worst risk level            -> avoid_risk_level
      6. weak low-score band         -> raise min_strategy_score_floor
      7. best gate category          -> category_multiplier boost
    """
    category_stats   = identify.get("category", {})
    exit_stats       = identify.get("exit_type", {})
    confidence_stats = identify.get("confidence", {})
    risk_stats       = identify.get("risk_level", {})
    score_stats      = identify.get("score_band", {})
    cross_ledger_clean = cross_ledger_clean or {}

    # 1. Exit-window evidence (existing)
    stagnant = exit_stats.get("EXIT_STAGNANT")
    if stagnant and stagnant["avg_pnl_pct"] > 3.0:
        old_days = strategy_state.get("min_hold_days_before_exit_eval", 7)
        new_days = min(30, old_days + 3)
        if new_days != old_days:
            return {
                "rule":   "min_hold_days_before_exit_eval",
                "target": "min_hold_days_before_exit_eval",
                "new_value": new_days,
                "old_value": old_days,
                "rationale": (f"EXIT_STAGNANT closes ({stagnant['count']} trades) averaged "
                              f"{stagnant['avg_pnl_pct']:+.1f}% at exit — positions tagged stagnant were "
                              f"still gaining, suggesting the eval window is too short. "
                              f"Extending from {old_days} to {new_days} days."),
                "evidence": stagnant,
            }

    # 2. Cross-ledger clean-category comparison — Strict/Relaxed buy the
    # same "clean" picks Adaptive does, but close ONLY via stop-loss/
    # swap (no trim/stagnant/take-profit logic at all). If Adaptive's
    # own clean-category win rate is clearly worse than theirs on a
    # real sample both sides, that's evidence the active exit
    # management itself is net-negative — the response is to intervene
    # less eagerly (longer eval window), not to guess which specific
    # exit type is at fault from this signal alone.
    adaptive_clean = category_stats.get("clean")
    if adaptive_clean and cross_ledger_clean:
        benchmark_rates = [s["win_rate"] for s in cross_ledger_clean.values()]
        benchmark_avg = sum(benchmark_rates) / len(benchmark_rates)
        gap = benchmark_avg - adaptive_clean["win_rate"]
        if gap > 15.0:
            old_days = strategy_state.get("min_hold_days_before_exit_eval", 7)
            new_days = min(30, old_days + 3)
            if new_days != old_days:
                benchmark_desc = ", ".join(f"{name} {s['win_rate']}%" for name, s in cross_ledger_clean.items())
                return {
                    "rule":   "min_hold_days_before_exit_eval",
                    "target": "min_hold_days_before_exit_eval",
                    "new_value": new_days,
                    "old_value": old_days,
                    "rationale": (f"Adaptive's own 'clean' category ({adaptive_clean['count']} closed trades): "
                                  f"{adaptive_clean['win_rate']}% win rate — trailing {benchmark_desc}, which buy "
                                  f"the same picks but only ever close via stop-loss/swap. Active exit management "
                                  f"looks net-negative on identical entries. Extending the eval window from "
                                  f"{old_days} to {new_days} days so it intervenes less eagerly."),
                    "evidence": {"adaptive_clean": adaptive_clean, "cross_ledger_clean": cross_ledger_clean},
                }

    avoid = set(strategy_state.get("avoid_categories", []))

    # 3. Worst gate category (existing)
    worst_cat, worst_stats = None, None
    for cat in ("both_caveats", "earnings_only", "gate7_only", "excluded_pick"):
        if cat in avoid or cat not in category_stats:
            continue
        s = category_stats[cat]
        if len(avoid | {cat}) >= 5:
            continue  # never avoid every category — that's paralysis, not learning
        if s["win_rate"] < 40 and s["avg_pnl_pct"] < 0 and (worst_stats is None or s["win_rate"] < worst_stats["win_rate"]):
            worst_cat, worst_stats = cat, s
    if worst_cat:
        return {
            "rule":   "avoid_category",
            "target": worst_cat,
            "rationale": (f"{worst_cat.replace('_', ' ')} picks ({worst_stats['count']} closed trades): "
                          f"{worst_stats['win_rate']}% win rate, {worst_stats['avg_pnl_pct']:+.1f}% avg return — "
                          f"underperforming on a real sample. Avoiding this category going forward."),
            "evidence": worst_stats,
        }

    # 4. Worst confidence level
    avoid_confidence = set(strategy_state.get("avoid_confidence_levels", []))
    worst_conf, worst_conf_stats = None, None
    for conf, s in confidence_stats.items():
        if conf in avoid_confidence:
            continue
        if {"High", "Medium"} <= (avoid_confidence | {conf}):
            continue  # the gates only admit High/Medium — avoiding both would stop all buying
        if s["win_rate"] < 40 and s["avg_pnl_pct"] < 0 and (worst_conf_stats is None or s["win_rate"] < worst_conf_stats["win_rate"]):
            worst_conf, worst_conf_stats = conf, s
    if worst_conf:
        return {
            "rule":   "avoid_confidence_level",
            "target": worst_conf,
            "rationale": (f"'{worst_conf}' confidence picks ({worst_conf_stats['count']} closed trades): "
                          f"{worst_conf_stats['win_rate']}% win rate, {worst_conf_stats['avg_pnl_pct']:+.1f}% avg return — "
                          f"Claude's own confidence rating at buy time is underperforming on a real sample. "
                          f"Avoiding this confidence level going forward."),
            "evidence": worst_conf_stats,
        }

    # 5. Worst risk level
    avoid_risk = set(strategy_state.get("avoid_risk_levels", []))
    worst_risk, worst_risk_stats = None, None
    for risk, s in risk_stats.items():
        if risk in avoid_risk:
            continue
        if {"Low", "Medium"} <= (avoid_risk | {risk}):
            continue  # the gates only admit Low/Medium — avoiding both would stop all buying
        if s["win_rate"] < 40 and s["avg_pnl_pct"] < 0 and (worst_risk_stats is None or s["win_rate"] < worst_risk_stats["win_rate"]):
            worst_risk, worst_risk_stats = risk, s
    if worst_risk:
        return {
            "rule":   "avoid_risk_level",
            "target": worst_risk,
            "rationale": (f"'{worst_risk}' risk-level picks ({worst_risk_stats['count']} closed trades): "
                          f"{worst_risk_stats['win_rate']}% win rate, {worst_risk_stats['avg_pnl_pct']:+.1f}% avg return — "
                          f"underperforming on a real sample. Avoiding this risk level going forward."),
            "evidence": worst_risk_stats,
        }

    # 6. Weak low-score band -> raise the floor
    low_band = score_stats.get("low_score_under70")
    current_floor = strategy_state.get("min_strategy_score_floor", 0)
    if low_band and low_band["win_rate"] < 40 and low_band["avg_pnl_pct"] < 0 and current_floor < 70:
        return {
            "rule":   "min_strategy_score_floor",
            "target": "min_strategy_score_floor",
            "new_value": 70,
            "old_value": current_floor,
            "rationale": (f"Picks scored below 70 ({low_band['count']} closed trades): "
                          f"{low_band['win_rate']}% win rate, {low_band['avg_pnl_pct']:+.1f}% avg return — "
                          f"underperforming on a real sample. Raising the minimum strategy_score floor to 70."),
            "evidence": low_band,
        }

    # 7. Best gate category (existing)
    best_cat, best_stats = None, None
    for cat, s in category_stats.items():
        if cat == "clean":
            continue
        current_mult = strategy_state.get("category_multipliers", {}).get(cat, 1.0)
        if s["win_rate"] > 65 and current_mult < 1.5 and (best_stats is None or s["win_rate"] > best_stats["win_rate"]):
            best_cat, best_stats = cat, s
    if best_cat:
        old_mult = strategy_state.get("category_multipliers", {}).get(best_cat, 1.0)
        new_mult = round(min(1.5, old_mult + 0.2), 2)
        return {
            "rule":   "category_multiplier",
            "target": best_cat,
            "new_value": new_mult,
            "old_value": old_mult,
            "rationale": (f"{best_cat.replace('_', ' ')} picks ({best_stats['count']} closed trades): "
                          f"{best_stats['win_rate']}% win rate, {best_stats['avg_pnl_pct']:+.1f}% avg return — "
                          f"clearly working. Sizing multiplier increased from {old_mult}x to {new_mult}x."),
            "evidence": best_stats,
        }

    return None


def adaptive_execute(ledger, proposal, total_capital_now):
    """Applies the proposed rule change and logs it, with a capital
    snapshot so Review can later measure whether it helped."""
    ss = ledger["strategy_state"]
    entry = {
        "applied_at":       datetime.now(timezone.utc).isoformat(),
        "rule":              proposal["rule"],
        "target":            proposal["target"],
        "rationale":         proposal["rationale"],
        "evidence":          proposal["evidence"],
        "status":            "active",
        "reverted_at":       None,
        "reverted_reason":   None,
        "adaptive_value_at_application": total_capital_now,
    }

    if proposal["rule"] == "avoid_category":
        ss.setdefault("avoid_categories", []).append(proposal["target"])
        entry["old_value"], entry["new_value"] = "allowed", "avoided"
    elif proposal["rule"] == "category_multiplier":
        ss.setdefault("category_multipliers", {})[proposal["target"]] = proposal["new_value"]
        entry["old_value"], entry["new_value"] = proposal["old_value"], proposal["new_value"]
    elif proposal["rule"] == "min_hold_days_before_exit_eval":
        ss["min_hold_days_before_exit_eval"] = proposal["new_value"]
        entry["old_value"], entry["new_value"] = proposal["old_value"], proposal["new_value"]
    elif proposal["rule"] == "avoid_confidence_level":
        ss.setdefault("avoid_confidence_levels", []).append(proposal["target"])
        entry["old_value"], entry["new_value"] = "allowed", "avoided"
    elif proposal["rule"] == "avoid_risk_level":
        ss.setdefault("avoid_risk_levels", []).append(proposal["target"])
        entry["old_value"], entry["new_value"] = "allowed", "avoided"
    elif proposal["rule"] == "min_strategy_score_floor":
        ss["min_strategy_score_floor"] = proposal["new_value"]
        entry["old_value"], entry["new_value"] = proposal["old_value"], proposal["new_value"]

    ss.setdefault("rule_history", []).append(entry)
    print(f"{BLUE}◆ Adaptive Plan/Execute: {proposal['rationale']}{RESET}")


def run_adaptive_cycle(state, price_map):
    """Runs Identify -> Plan -> Execute -> Review for the Adaptive
    ledger every single time this script runs."""
    ledger = state["sim_adaptive"]
    ss = ledger["strategy_state"]
    closed = ledger["closed_positions"]

    identify = adaptive_identify(closed)
    for dim, groups in identify.items():
        if not groups:
            continue
        print(f"{BLUE}◆ Adaptive Identify — {dim} performance this cycle:{RESET}")
        for group, s in groups.items():
            print(f"    {group}: {s['count']} trades, {s['win_rate']}% win rate, {s['avg_pnl_pct']:+.1f}% avg return")

    # Cross-trader evidence — observational for real trades (no clean
    # lever yet, see compute_real_trade_stats), actionable for the
    # Strict/Relaxed clean-category comparison (see adaptive_plan #2).
    real_trade_stats = compute_real_trade_stats(load_trade_history())
    if real_trade_stats:
        print(f"{BLUE}◆ Cross-trader Identify — your real trades this cycle:{RESET}")
        print(f"    {real_trade_stats['count']} closed round-trips, {real_trade_stats['win_rate']}% win rate, "
              f"{real_trade_stats['avg_pnl_pct']:+.1f}% avg return")

    cross_ledger_clean = compute_cross_ledger_clean_stats(state)
    if cross_ledger_clean:
        print(f"{BLUE}◆ Cross-trader Identify — Strict/Relaxed 'clean' category (same picks, passive exits only):{RESET}")
        for label, s in cross_ledger_clean.items():
            print(f"    {label}: {s['count']} trades, {s['win_rate']}% win rate, {s['avg_pnl_pct']:+.1f}% avg return")

    proposal = adaptive_plan(ss, identify, cross_ledger_clean)
    if proposal:
        total_capital_now = compute_total_capital(ledger, price_map)
        adaptive_execute(ledger, proposal, total_capital_now)
    else:
        print(f"{BLUE}◆ Adaptive Plan — no new rule change this cycle{RESET}")

    strict_value_now   = compute_total_capital(state["sim_7gate"], price_map)
    adaptive_value_now = compute_total_capital(ledger, price_map)
    for entry in ss.get("rule_history", []):
        if entry["status"] != "active":
            continue
        try:
            age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(entry["applied_at"])).days
        except (ValueError, TypeError, KeyError):
            age_days = 0
        expired = age_days >= RULE_EXPIRY_DAYS
        gap = 0.0
        if not expired:
            trades_since = len(closed) - ss.get("last_cycle_closed_count", 0)
            if trades_since < REVIEW_LOOKBACK_TRADES:
                continue
            adaptive_then = entry.get("adaptive_value_at_application", 0)
            if adaptive_then <= 0:
                continue
            adaptive_return_since = (adaptive_value_now / adaptive_then - 1) * 100
            initial_value = state.get("initial_total_value", adaptive_then) or adaptive_then
            strict_return_since = (strict_value_now / initial_value - 1) * 100 if initial_value > 0 else 0
            gap = strict_return_since - adaptive_return_since
        if expired or gap > REVIEW_UNDERPERFORM_PCT:
            entry["status"] = "reverted"
            entry["reverted_at"] = datetime.now(timezone.utc).isoformat()
            entry["reverted_reason"] = (f"Rule expired after {age_days} days — re-testing without it." if expired else
                                        f"Adaptive has lagged the Strict benchmark by {gap:.1f} points "
                                        f"since this rule was applied — reverting.")
            if entry["rule"] == "avoid_category":
                ss["avoid_categories"] = [c for c in ss.get("avoid_categories", []) if c != entry["target"]]
            elif entry["rule"] == "category_multiplier":
                ss.setdefault("category_multipliers", {})[entry["target"]] = entry.get("old_value", 1.0)
            elif entry["rule"] == "min_hold_days_before_exit_eval":
                ss["min_hold_days_before_exit_eval"] = entry.get("old_value", 7)
            elif entry["rule"] == "avoid_confidence_level":
                ss["avoid_confidence_levels"] = [c for c in ss.get("avoid_confidence_levels", []) if c != entry["target"]]
            elif entry["rule"] == "avoid_risk_level":
                ss["avoid_risk_levels"] = [r for r in ss.get("avoid_risk_levels", []) if r != entry["target"]]
            elif entry["rule"] == "min_strategy_score_floor":
                ss["min_strategy_score_floor"] = entry.get("old_value", 0)
            print(f"{YELLOW}◆ Adaptive Review — reverted: {entry['reverted_reason']}{RESET}")

    ss["last_cycle_closed_count"] = len(closed)


LEDGER_LABELS = {"sim_7gate": "Strict", "sim_6gate": "Relaxed", "sim_adaptive": "Adaptive"}
TRADES_FOR_SIGNIFICANCE = 245  # ≈ (1.96 × 8% monthly SD / 1% edge)² — trades needed to tell a 1% edge from luck


def record_history(state, latest, price_map):
    """One value point per ledger per day (latest run of the day wins),
    plus the real portfolio and SPY, for equity curves and the scorecard."""
    day = datetime.now().strftime("%Y-%m-%d")
    point = {"date": day, "spy": price_map.get("SPY"),
             "real": safe_float((latest.get("portfolio_summary") or {}).get("total_capital") or latest.get("total_capital"))}
    for name in LEDGER_NAMES:
        L = state[name]
        point[name] = round(compute_total_capital(L, price_map), 2)
        invested = point[name] - L["cash"]
        point[name + "_exposure"] = round(invested / point[name] * 100, 1) if point[name] > 0 else 0
    hist = [h for h in state.get("history", []) if h.get("date") != day]
    hist.append(point)
    state["history"] = hist[-750:]  # ~3 years of daily points


def _series_stats(values):
    vals = [v for v in values if v]
    if len(vals) < 2:
        return {}
    rets = [b / a - 1 for a, b in zip(vals, vals[1:]) if a]
    peak, mdd = vals[0], 0.0
    for v in vals:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    out = {"return_pct": round((vals[-1] / vals[0] - 1) * 100, 2), "max_drawdown_pct": round(mdd * 100, 2), "points": len(vals)}
    if len(rets) >= 10:
        mu = sum(rets) / len(rets)
        sd = (sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5
        dn = (sum(min(0, r) ** 2 for r in rets) / len(rets)) ** 0.5
        out["sharpe_ann"] = round(mu / sd * 252 ** 0.5, 2) if sd else None
        out["sortino_ann"] = round(mu / dn * 252 ** 0.5, 2) if dn else None
    return out


def compute_scorecard(state):
    """X1: the numbers a portfolio manager needs before trusting a ledger."""
    hist = state.get("history", [])
    spy = _series_stats([h.get("spy") for h in hist])
    card = {"since": hist[0]["date"] if hist else None, "days": len(hist), "spy": spy,
            "real": _series_stats([h.get("real") for h in hist]), "ledgers": {},
            "trades_for_significance": TRADES_FOR_SIGNIFICANCE}
    for name in LEDGER_NAMES:
        L = state[name]
        st = _series_stats([h.get(name) for h in hist])
        pnl = [safe_float(c.get("pnlPct")) for c in L.get("closed_positions", [])]
        wins, losses = [x for x in pnl if x > 0], [x for x in pnl if x <= 0]
        st.update({
            "label": LEDGER_LABELS[name],
            "excess_vs_spy_pct": round(st["return_pct"] - spy["return_pct"], 2) if st.get("return_pct") is not None and spy.get("return_pct") is not None else None,
            "closed_trades": len(pnl),
            "win_rate_pct": round(100 * len(wins) / len(pnl), 1) if pnl else None,
            "avg_win_pct": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss_pct": round(sum(losses) / len(losses), 2) if losses else None,
            "payoff_ratio": round((sum(wins) / len(wins)) / abs(sum(losses) / len(losses)), 2) if wins and losses and sum(losses) else None,
            "expectancy_pct": round(sum(pnl) / len(pnl), 2) if pnl else None,
            "avg_exposure_pct": round(sum(h.get(name + "_exposure", 0) for h in hist) / len(hist), 1) if hist else None,
            "commissions_usd": round(sum(t.get("commission", 0) or 0 for t in L.get("trade_log", [])), 2),
            "trades": len(L.get("trade_log", [])),
            "significant": len(pnl) >= TRADES_FOR_SIGNIFICANCE,
        })
        card["ledgers"][name] = st
    return card


def run():
    print(f"\n{BOLD}{BLUE}{'='*55}")
    print(f"  RECOMMENDATION SIMULATOR")
    print(f"  {datetime.now().strftime('%A %d %B %Y  %H:%M')}")
    print(f"{'='*55}{RESET}\n")

    try:
        latest = load_latest()
    except Exception as e:
        print(f"{RED}✗ Could not read latest.json: {e}{RESET}")
        sys.exit(1)

    strategy     = load_strategy()
    # Same sizing policy as the reallocation engine: strategy floor raised by
    # the regime override, and no orders below min_order_usd.
    from reallocation_engine import load_sizing_policy
    policy       = load_sizing_policy()
    min_cash_pct = policy["floor_pct"]
    strategy.setdefault("position_sizing", {})["min_order_usd"] = policy["min_order_usd"]

    state = load_or_init_state(latest, min_cash_pct)
    agent_picks    = latest.get("agent_picks", [])
    picks_excluded = latest.get("picks_excluded", [])
    reallocation   = latest.get("reallocation", [])
    price_map      = build_price_map(latest)
    state["price_unavailable"] = complete_price_map(price_map, state, latest)

    for ledger_name in LEDGER_NAMES:
        ledger = state[ledger_name]
        before_value = compute_total_capital(ledger, price_map)

        if ledger_name == "sim_adaptive":
            process_stop_losses(ledger, price_map, strategy)
            process_sells(ledger, reallocation, price_map, strategy)
            run_adaptive_exit_assessment(ledger, price_map, strategy, latest)
            process_buys_adaptive(ledger, agent_picks, picks_excluded, reallocation, price_map, strategy)
            run_adaptive_cycle(state, price_map)
        else:
            picks = get_actionable_picks(agent_picks, ledger_name)
            apply_trailing_stops(ledger, price_map, strategy)
            process_stop_losses(ledger, price_map, strategy)
            process_sells(ledger, reallocation, price_map, strategy)
            process_buys(ledger, picks, reallocation, price_map, min_cash_pct, strategy)

        after_value = compute_total_capital(ledger, price_map)
        cash_pct = (ledger["cash"] / after_value * 100) if after_value > 0 else 0
        label = {"sim_7gate": "Strict 7-gate", "sim_6gate": "Relaxed 6-gate", "sim_adaptive": "Adaptive"}[ledger_name]
        floor_note = f", floor {min_cash_pct}%" if ledger_name != "sim_adaptive" else " (no hard floor)"
        print(f"{GREEN}✓ {label}: {len(ledger['holdings'])} holdings, "
              f"${ledger['cash']:,.2f} cash ({cash_pct:.1f}%{floor_note}), total ${after_value:,.2f} "
              f"(was ${before_value:,.2f}){RESET}")

    record_history(state, latest, price_map)
    state["scorecard"] = compute_scorecard(state)
    held = {h["ticker"] for name in LEDGER_NAMES for h in state[name]["holdings"]} | {"SPY"}
    state["last_prices"] = {t: price_map[t] for t in sorted(held) if price_map.get(t)}  # what the dashboard should value with
    state["last_run_at"]  = datetime.now(timezone.utc).isoformat()
    state["min_cash_pct"] = min_cash_pct
    save_state(state)
    print(f"\n{GREEN}✓ Saved {STATE_PATH}{RESET}")


if __name__ == "__main__":
    run()
