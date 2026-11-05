# ============================================================
# MARKET AGENT V4.5 — Layer 3: Agent Brain
# Pick Quality Gates: strategy≥90, risk=Low, confidence=High,
# sector limits, entry conditions, no portfolio exposure added
# ============================================================

import json
import os
import sys
from datetime import datetime, timezone
from dotenv import load_dotenv
import anthropic
from claude_json import ask_json, StructuredReplyError
from write_progress import substep
from common import GREEN, RED, YELLOW, BLUE, BOLD, RESET, safe_float, load_json

load_dotenv()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
client   = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Sub-stages shown on the dashboard during run_agent.sh step 7
# (see write_progress.substep) — keep the count in sync with run().
BRAIN_STAGES = 8

def stage(n, label):
    substep(f"{n}/{BRAIN_STAGES} · {label}")

TICKER_LIST_SCHEMA = {
    "type": "object",
    "properties": {"tickers": {"type": "array", "items": {"type": "string"}}},
    "required": ["tickers"],
}


# ── LOAD DATA ─────────────────────────────────────────────────
def load_market_data(path=None):
    if path is None:
        path = os.path.join(BASE_DIR, "data", "market_data.json")
    try:
        with open(path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"{RED}✗ market_data.json not found. Run fetch_data.py first.{RESET}")
        return None


# ── LOAD STRATEGY ────────────────────────────────────────────
def load_strategy(path=None):
    if path is None:
        path = os.path.join(BASE_DIR, "strategy.json")
    try:
        with open(path) as f:
            strategy = json.load(f)
        print(f"{GREEN}✓ Strategy loaded: {strategy.get('profile','')}{RESET}")
        return strategy
    except FileNotFoundError:
        print(f"{YELLOW}⚠ No strategy.json found — using defaults{RESET}")
        return {}

def build_strategy_rules(strategy, holdings, data):
    """Build strategy compliance check to inject into prompt"""
    if not strategy:
        return ""

    ps        = strategy.get("position_sizing", {})
    sl        = strategy.get("stop_loss", {})
    entry     = strategy.get("entry_rules", {})
    total_cap = safe_float(data.get("total_capital", 0))
    cash      = safe_float(data.get("cash", 0))

    max_stock    = ps.get("max_single_stock_pct", 10)
    max_existing = ps.get("max_existing_holding_pct", 20)
    max_sector   = ps.get("max_sector_pct", 30)
    min_cash     = ps.get("min_cash_pct", 20)
    priority_exits = strategy.get("rebalancing",{}).get("priority_exits",[])
    sl_pct     = sl.get("default_pct", 8)

    lines = []
    lines.append("INVESTOR STRATEGY RULES")
    lines.append(f"Profile: {strategy.get('profile','')} | Horizon: {strategy.get('horizon','')} | Risk: {strategy.get('risk_tolerance','')}")
    lines.append(f"Position Rules: Max new position={max_stock}%, Max existing holding={max_existing}%, Max sector={max_sector}%, Min cash={min_cash}%")
    lines.append(f"Priority exits: {', '.join(priority_exits) if priority_exits else 'None'}")
    lines.append(f"Stop Loss Rule: Exit at {sl_pct}% below buy price - no exceptions")
    lines.append(f"Entry Rules: Only buy when RSI < {entry.get('rsi_max',60)}, ideal RSI < {entry.get('rsi_ideal',35)}")

    violations = []

    cash_pct = (cash / total_cap * 100) if total_cap > 0 else 0
    if cash_pct < min_cash:
        violations.append(f"CASH TOO LOW: {cash_pct:.1f}% vs required {min_cash}% minimum (need ${total_cap*(min_cash/100)-cash:,.0f} more) — NO NEW BUYS")

    # Cash deployment tiers
    cash_deploy = strategy.get("cash_deployment", {})
    excess_pct  = cash_deploy.get("excess_pct", 30)
    if cash_pct > 40:
        violations.append(f"CAPITAL INEFFICIENCY: Cash at {cash_pct:.1f}% — above 40% threshold. Actively seek qualifying picks to deploy excess capital.")
    elif cash_pct > excess_pct:
        lines.append(f"Cash Deployment: Cash at {cash_pct:.1f}% (above {excess_pct}% threshold) — actively seek qualifying picks for deployment")

    for h in holdings:
        ticker = h.get("ticker", "")
        val    = safe_float(h.get("current_value", 0))
        buy    = safe_float(h.get("buy_price", 0))
        curr   = safe_float(h.get("price", 0))
        stop   = safe_float(h.get("stop_loss", 0))
        wt     = (val / total_cap * 100) if total_cap > 0 else 0

        effective_max = max_stock if ticker in priority_exits else max_existing
        if wt > effective_max:
            violations.append(f"OVERWEIGHT: {ticker} is {wt:.1f}% of total capital (max {effective_max}% — cash counts as a position) - trim ${val-(total_cap*effective_max/100):,.0f}")

        if stop > 0 and curr > 0 and curr < stop:
            pct_below = round(((stop - curr) / stop) * 100, 1)
            violations.append(f"STOP LOSS BREACH: {ticker} at ${curr} is {pct_below}% BELOW stop loss ${stop} - exit required")

        if buy > 0 and curr > 0 and stop == 0:
            loss_pct = ((curr - buy) / buy) * 100
            if loss_pct < -sl_pct:
                violations.append(f"LOSS LIMIT: {ticker} down {loss_pct:.1f}% exceeds {sl_pct}% stop loss rule")

    if violations:
        lines.append(f"STRATEGY VIOLATIONS ({len(violations)}) - flag these urgently:")
        for v in violations:
            lines.append(f"  VIOLATION: {v}")
    else:
        lines.append("No strategy violations detected")

    lines.append("IMPORTANT: Check each position against these rules. Flag violations with specific dollar amounts.")
    return "\n".join(lines)


# ── SPLIT STOCKS ──────────────────────────────────────────────
def split_stocks(data):
    stocks     = data.get("stocks", [])
    holdings   = [s for s in stocks if s.get("list_type") == "holding"]
    interested = [s for s in stocks if s.get("list_type") == "interested"]
    return holdings, interested

# ── FORMAT STOCK FOR PROMPT ───────────────────────────────────
def fmt(s, include_position=True):
    curr = safe_float(s.get("price"))
    buy  = safe_float(s.get("buy_price"))
    qty  = safe_float(s.get("quantity"))

    parts = [
        f"{s.get('ticker')} ({s.get('name')})",
        f"price=${curr if curr>0 else 'N/A'}",
        f"chg={s.get('change_pct','N/A')}%",
        f"vol={s.get('volume_ratio','N/A')}x",
        f"RSI={s.get('rsi','N/A')}",
        f"SMA50={s.get('sma_50','N/A')}",
        f"SMA200={s.get('sma_200','N/A')}",
        f"52wH=${s.get('52w_high','N/A')}",
        f"52wL=${s.get('52w_low','N/A')}",
    ]

    # Candidate/fundamental fields — only emitted when present, so holdings
    # lines stay as before. The DATA FIELDS LEGEND in the prompt promises
    # these; previously they were never sent, so Claude's valuation claims
    # about candidates came from memory rather than data.
    for key, label, suffix in (("sector", "sector", ""), ("market_cap_class", "mktcap", ""),
                               ("pct_from_52w_high", "from52wH", "%"), ("beta", "beta", ""),
                               ("pe_forward", "pe_fwd", ""), ("peg_ratio", "peg", ""),
                               ("ev_to_ebitda", "ev_ebitda", ""), ("fcf_yield", "fcf_yield", "%"),
                               ("revenue_growth_yoy", "rev_growth", "%"), ("operating_margin", "op_mgn", "%"),
                               ("roe", "roe", "%"), ("debt_to_equity", "debt_eq", ""),
                               ("days_to_earnings", "days_to_earnings", "")):
        if not include_position and s.get(key) not in (None, "", 0):
            parts.append(f"{label}={s.get(key)}{suffix}")

    if include_position and buy > 0:
        parts += [
            f"buy=${buy}",
            f"qty={int(qty)}shares",
            f"cost_basis=${s.get('cost_basis','N/A')}",
            f"current_value=${s.get('current_value','N/A')}",
            f"pnl=${s.get('pnl_usd','N/A')}({s.get('pnl_pct','N/A')}%)",
            f"target=${s.get('target','N/A')}",
            f"stop=${s.get('stop_loss','N/A')}",
        ]

    return ", ".join(parts)

# ── SECTOR MAP ────────────────────────────────────────────────

def get_sector(ticker, name="", sector_from_data=""):
    """
    Returns sector for a stock.
    Priority: 1) yfinance sector field (fetched by fetch_data.py — works for any stock)
              2) Name-based heuristic fallback (ETFs, edge cases)
    No hardcoded ticker list — fully dynamic for any stock ever bought.
    """
    # Primary: use sector fetched directly from Yahoo Finance
    if sector_from_data and str(sector_from_data).strip() not in ("", "nan", "None", "N/A"):
        return str(sector_from_data).strip()
    # Fallback: name/ticker heuristics for ETFs and edge cases
    t = ticker.upper()
    n = name.lower()
    if t in ("SPY", "RSP", "IVV", "VOO"):       return "Index ETF"
    if t in ("QQQ",):                            return "Tech ETF"
    if t in ("GLD", "SLV", "IAU"):              return "Commodities"
    if t in ("XLE", "XOP"):                     return "Energy ETF"
    if any(w in n for w in ["tech","software","semi","chip","cloud","cyber"]):
        return "Technology"
    if any(w in n for w in ["oil","energy","petroleum","gas"]):
        return "Energy"
    if any(w in n for w in ["gold","silver","metal","commodity"]):
        return "Commodities"
    if any(w in n for w in ["health","pharma","medical","biotech"]):
        return "Healthcare"
    if any(w in n for w in ["bank","financial","insurance","invest","capital"]):
        return "Financials"
    if any(w in n for w in ["utility","electric","water","power","grid"]):
        return "Utilities"
    if any(w in n for w in ["reit","realty","estate","tower","properties"]):
        return "Real Estate"
    if any(w in n for w in ["industrial","aerospace","defense","machinery"]):
        return "Industrials"
    return "Other"
def build_sector_map(holdings, total_value):
    sectors = {}
    for s in holdings:
        sector = get_sector(
            s.get("ticker", ""),
            s.get("name", "") or s.get("company_name", ""),
            s.get("sector", "")
        )
        val = safe_float(s.get("current_value"))
        sectors[sector] = sectors.get(sector, 0) + val
    if total_value > 0:
        return {k: round((v/total_value)*100) for k,v in sorted(sectors.items(), key=lambda x:-x[1])}
    return {}

# ── HEALTH SCORE ──────────────────────────────────────────────
def calculate_health_score(analysis, data):
    holdings = analysis.get("holdings", [])
    if not holdings:
        return 50, "No holdings data"

    score  = 70  # start neutral
    issues = []
    boosts = []

    signal_scores = {
        "STRONG BUY":+8,"BUY":+5,"ADD":+3,"TOP-UP":+3,
        "HOLD":0,"WATCH":-5,"TRIM":-3,"SELL":-15
    }
    risk_scores = {"Low":+3,"Medium":0,"High":-5}
    conf_scores = {"High":+3,"Medium":0,"Low":-5}

    for s in holdings:
        signal = s.get("signal","HOLD")
        risk   = s.get("risk_level","Medium")
        conf   = s.get("confidence","Medium")
        pnl    = safe_float(s.get("pnl_pct"))
        ticker = s.get("ticker","")
        weight = safe_float(s.get("portfolio_weight"))

        score += signal_scores.get(signal, 0)
        score += risk_scores.get(risk, 0)
        score += conf_scores.get(conf, 0)

        if pnl < -20:
            score -= 10
            issues.append(f"{ticker} deeply underwater ({pnl:+.1f}%)")
        elif pnl < -10:
            score -= 5
            issues.append(f"{ticker} at loss ({pnl:+.1f}%)")
        elif pnl > 20:
            score += 5
            boosts.append(f"{ticker} strong gain ({pnl:+.1f}%)")
        elif pnl > 10:
            score += 3
            boosts.append(f"{ticker} in profit ({pnl:+.1f}%)")

        # Concentration penalty
        if weight > 50:
            score -= 8
            issues.append(f"{ticker} overweight at {weight:.0f}% of portfolio")
        elif weight > 35:
            score -= 3

        if signal == "SELL":
            issues.append(f"{ticker} — SELL signal")

    # Cash ratio bonus
    cash         = safe_float(data.get("cash"))
    total_capital= safe_float(data.get("total_capital"))
    if total_capital > 0:
        cash_ratio = (cash / total_capital) * 100
        if cash_ratio > 20:
            score += 3
            boosts.append(f"Good cash buffer ({cash_ratio:.0f}%)")
        elif cash_ratio < 5:
            score -= 3
            issues.append("Low cash buffer")

    score = max(0, min(100, score))

    if score >= 75:   label = "Strong"
    elif score >= 60: label = "Healthy"
    elif score >= 45: label = "Caution"
    elif score >= 30: label = "Weak"
    else:             label = "Critical"

    summary = f"{label} — "
    if issues:
        summary += "; ".join(issues[:2])
    elif boosts:
        summary += "; ".join(boosts[:2])
    else:
        summary += "Portfolio balanced"

    return score, summary

# ── SIGNAL HISTORY ────────────────────────────────────────────
def update_signal_history(analysis):
    path = os.path.join(BASE_DIR, "logs", "signal_history.json")
    history = []
    if os.path.exists(path):
        try:
            with open(path) as f:
                history = json.load(f)
        except Exception:
            history = []

    today    = datetime.now().strftime("%Y-%m-%d")
    snapshot = {"date": today, "time": datetime.now().strftime("%H:%M"), "signals": {}}

    for section in ["holdings","interested","agent_picks"]:
        for s in analysis.get(section, []):
            t = s.get("ticker","")
            if t:
                snapshot["signals"][t] = {
                    "signal":     s.get("signal",""),
                    "confidence": s.get("confidence",""),
                    "risk_level": s.get("risk_level",""),
                    "pnl_pct":    s.get("pnl_pct"),
                }

    history = [h for h in history if h.get("date") != today]
    history.append(snapshot)
    history = sorted(history, key=lambda x: x["date"])[-7:]

    with open(path, "w") as f:
        json.dump(history, f, indent=2)
    print(f"{GREEN}✓ Signal history updated ({len(history)} days){RESET}")
    return history

# ── STEP 1: GET PICK TICKERS ──────────────────────────────────
def get_pick_tickers(holdings, interested, strategy=None):
    """
    Reads the stable master candidate list built by refresh_universe.py
    (refreshed manually via the dashboard's "Refresh Stock List" button)
    — replacing the old approach of asking Claude to invent ~52 tickers
    from memory every single run. That approach meant a stock passing
    every quality gate today could simply vanish from consideration
    tomorrow for no reason other than "Claude didn't happen to think
    of it that day." This fixes that structurally: the master list only
    changes when you click refresh, not based on what a single small
    Claude call happens to recall on any given run.

    ETFs & Commodities are handled separately via a small dedicated
    Claude call, unchanged in spirit from the original design — ETFs
    aren't S&P 500 constituents, so they can't come from the master
    list, and this preserves the "no stock names hardcoded" approach
    for that one category.

    Falls back to the FULL original Claude-generated approach only if
    no master list exists yet at all (i.e. refresh has never been run)
    — so a fresh install still works before the first manual refresh.

    Returns (all_tickers, sector_map)
    """
    exclude_set = set(s.get("ticker","").upper() for s in holdings + interested)
    master_path = os.path.join(BASE_DIR, "data", "universe_master.json")

    if not os.path.exists(master_path):
        print(f"{YELLOW}⚠ No master list found — using one-time Claude-generated universe. "
              f"Run a Stock List refresh from the dashboard's Run tab to switch to the stable list.{RESET}")
        return _build_claude_universe(holdings, interested, strategy)

    try:
        with open(master_path) as f:
            master = json.load(f)
        stocks = master.get("stocks", [])
        all_tickers, sector_map = [], {}
        for s in stocks:
            t = s.get("ticker", "").upper()
            if t in exclude_set:
                continue
            sector = s.get("sector", "Other")
            sector_map.setdefault(sector, []).append(t)
            all_tickers.append(t)

        refreshed_at = master.get("refreshed_at", "unknown")
        print(f"\n{BLUE}Step 1 — Using stable master list "
              f"({len(all_tickers)} candidates, refreshed {refreshed_at}){RESET}")
        for sector, tickers in sorted(sector_map.items(), key=lambda x: -len(x[1])):
            print(f"  {GREEN}✓ {sector} ({len(tickers)}){RESET}")

        etf_tickers, etf_sector_name = _build_etf_universe(exclude_set, strategy)
        if etf_tickers:
            sector_map[etf_sector_name] = etf_tickers
            all_tickers.extend(etf_tickers)

        print(f"\n{GREEN}✓ Universe: {len(all_tickers)} candidates across {len(sector_map)} sectors{RESET}")
        return all_tickers, sector_map

    except Exception as e:
        print(f"{YELLOW}⚠ Could not read master list ({e}) — falling back to Claude-generated universe{RESET}")
        return _build_claude_universe(holdings, interested, strategy)


def _build_etf_universe(exclude_set, strategy=None):
    """
    Small dedicated Claude call for JUST the ETFs & Commodities slots —
    ETFs aren't S&P 500 constituents so can't come from the master list,
    kept Claude-driven (not hardcoded) to match this system's existing
    "no stock names hardcoded" philosophy for this one category. Much
    cheaper than the old 10-sector call since it's now scoped to 1.
    """
    universe_cfg = (strategy or {}).get("candidate_universe", {})
    etf_cfg = universe_cfg.get("sectors", {}).get(
        "ETFs & Commodities",
        {"slots": 3, "bias": "broad market ETFs, gold, sector ETFs for diversification"}
    )
    slots = etf_cfg.get("slots", 3)
    exclude_str = ", ".join(sorted(exclude_set)) if exclude_set else "None"

    prompt = f"""Suggest exactly {slots} quality US-listed ETFs or commodities for a
diversified portfolio, matching this bias: {etf_cfg.get('bias','broad market ETFs')}.
DO NOT suggest any of these (already held or watched): {exclude_str}
Respond ONLY with JSON listing the ticker strings, e.g. {{"tickers": ["SPY","GLD","XLV"]}}. No explanation."""

    try:
        result  = ask_json(client, prompt, max_tokens=150, label="agent_brain_etf_universe", schema=TICKER_LIST_SCHEMA)
        tickers = [t.upper().strip() for t in result["tickers"] if t.upper().strip() not in exclude_set][:slots]
        return tickers, "ETFs & Commodities"
    except Exception as e:
        print(f"{YELLOW}⚠ Could not build ETF universe: {e}{RESET}")
        return [], "ETFs & Commodities"


def _build_claude_universe(holdings, interested, strategy=None):
    """
    Original Claude-generated universe builder — fallback path only,
    used before the first manual "Refresh Stock List" run has ever
    happened. Once data/universe_master.json exists, get_pick_tickers()
    uses that instead and this function is no longer called.
    """
    exclude_set = set(s.get("ticker","").upper() for s in holdings + interested)
    exclude_str = ", ".join(sorted(exclude_set)) if exclude_set else "None"
    today       = datetime.now().strftime("%A %d %B %Y")

    universe_cfg = (strategy or {}).get("candidate_universe", {})
    sectors      = universe_cfg.get("sectors", {
        "Technology":             {"slots": 7,  "bias": "profitable large-cap, positive FCF"},
        "Healthcare":             {"slots": 7,  "bias": "pharma, biotech, devices, insurers"},
        "Financials":             {"slots": 6,  "bias": "banks, payments, insurance, asset managers"},
        "Consumer Staples":       {"slots": 6,  "bias": "defensive, dividend-paying, global brands"},
        "Energy":                 {"slots": 6,  "bias": "oil, gas, pipelines, renewables"},
        "Industrials":            {"slots": 5,  "bias": "defence, aerospace, logistics, infrastructure"},
        "Consumer Discretionary": {"slots": 5,  "bias": "retail, restaurants, travel"},
        "Utilities":              {"slots": 4,  "bias": "regulated utilities, clean energy"},
        "Real Estate":            {"slots": 3,  "bias": "diversified REITs, data centres"},
        "ETFs & Commodities":     {"slots": 3,  "bias": "broad ETFs, gold, sector ETFs"}
    })

    total        = sum(cfg["slots"] for cfg in sectors.values())
    sector_lines = "\n".join([
        f"  {sector} ({cfg['slots']} stocks): {cfg.get('bias','quality names')}"
        for sector, cfg in sectors.items()
    ])

    prompt = f"""You are a US stock market analyst. Today: {today}.

DO NOT suggest any of these tickers (already held or watched): {exclude_str}

Build a diversified candidate universe of exactly {total} quality US stocks and ETFs
spread across these sectors:
{sector_lines}

Rules for ALL candidates:
- Established companies only — positive revenue, no pre-revenue speculation
- Prefer beta below 1.5 — avoid highly volatile names
- Mix of growth and value within each sector
- No penny stocks or micro-caps
- Choose names genuinely worth considering for a quality balanced portfolio today

Respond ONLY with a valid JSON object mapping each sector name exactly as shown above to an array of tickers.
Example format:
{{
  "Technology": ["AAPL","GOOGL","ORCL","CRM","IBM","INTC","TXN"],
  "Healthcare": ["JNJ","UNH","LLY","ABT","TMO","PFE","MDT"]
}}
No explanation. Only the JSON object."""

    print(f"\n{BLUE}Step 1 — Building candidate universe ({total} stocks, {len(sectors)} sectors)...{RESET}")

    all_tickers = []
    sector_map  = {}

    try:
        data_j = ask_json(client, prompt, max_tokens=500, label="agent_brain_claude_universe")

        for sector, tickers in data_j.items():
            cfg   = sectors.get(sector, {})
            slots = cfg.get("slots", 5)
            valid = [t.upper().strip() for t in tickers
                     if t.upper().strip() not in exclude_set][:slots]
            sector_map[sector] = valid
            all_tickers.extend(valid)
            print(f"  {GREEN}✓ {sector} ({len(valid)}): {', '.join(valid)}{RESET}")

        print(f"\n{GREEN}✓ Universe: {len(all_tickers)} candidates across {len(sector_map)} sectors{RESET}")
        return all_tickers, sector_map

    except Exception as e:
        print(f"{RED}✗ Could not build universe: {e}{RESET}")
        return [], {}


DAILY_NARROW_SIZE = 50  # keeps Step 3's Claude call cost flat vs. the old ~48-candidate universe


# Gate 7 thresholds, set from strategy.json by fetch_pick_prices() before
# narrowing so the shortlist favours the same headroom band Gate 7 accepts.
G7_HARD_HEADROOM = 15.0
G7_SOFT_HEADROOM = 10.0


def compute_daily_strength(pick):
    """
    Same-day ranking signal for narrowing the 300-stock master pool down
    to DAILY_NARROW_SIZE before Step 3's Claude call — distinct from
    refresh_universe.py's quality score (revenue/margin/beta/FCF/debt,
    computed once per manual refresh). This reuses fields ALREADY
    fetched by the pre-filter loop above, so narrowing adds zero extra
    yfinance calls: how far below its 52-week high (more room to run),
    healthy-but-not-extreme volume, and a sane forward P/E.
    """
    score = 0.0
    pct_from_high = pick.get("pct_from_52w_high")
    if pct_from_high is not None:
        # Headroom = % below the 52-week high. Previously this rewarded
        # stocks NEAR their high (-2% scored best), which Gate 7 then
        # rejected for lacking headroom — the shortlist was built to fail
        # its own last gate. Now: full marks inside Gate 7's hard band
        # (>= hard headroom, up to 40% — deeper than that suggests a broken
        # chart), partial marks in the soft band, none near the high.
        headroom = -pct_from_high
        if headroom >= G7_HARD_HEADROOM:
            score += 50 if headroom <= 40 else max(0, 50 - (headroom - 40) * 2.5)
        elif headroom >= G7_SOFT_HEADROOM:
            score += 30
        else:
            score += max(0, headroom / G7_SOFT_HEADROOM) * 10
    vol_ratio = pick.get("volume_ratio") or 1.0
    score += max(0, min(1, 1 - abs(vol_ratio - 1.2) / 2)) * 25  # near-normal-to-elevated volume
    pe = pick.get("pe_forward")
    if pe and pe > 0:
        score += max(0, min(1, 1 - abs(pe - 20) / 30)) * 25  # reasonable valuation, ~20x sweet spot
    else:
        score += 12.5  # neutral if PE unavailable — don't penalise for missing data
    return round(score, 2)


def compute_ttm_revenue_growth(ticker):
    """
    Computes a longer-window revenue growth comparison, distinct from
    yfinance's revenueGrowth field, which only compares the single
    most recent quarter to the same quarter a year ago. A stock can
    show a sharply negative single-quarter figure — e.g. from a
    one-time divestiture, like Occidental selling OxyChem to Berkshire
    Hathaway — while its underlying trend is genuinely healthy, or
    vice versa. Showing both figures side by side lets a real
    discrepancy speak for itself, without needing to identify or
    explain the underlying cause (divestiture, one-time write-down,
    etc.) — that's exactly the kind of nuanced judgment call that
    would otherwise require a Claude call per candidate, defeating the
    purpose of a cheap, pure-Python pre-filter stage.

    Tries a full 12-month comparison first (last 4 reported quarters
    vs. the prior 4). CONFIRMED via real production logs that most
    tickers' quarterly_financials via yfinance only go back ~5
    quarters, well short of the 8 needed for that — so this falls back
    to a 6-month comparison (last 2 quarters vs. the prior 2, needing
    only 4 total) when the fuller comparison isn't available, rather
    than the feature almost never actually firing in practice.

    Only called for candidates that already failed the cheap
    single-quarter check — not for every candidate — to avoid a second
    yfinance call across the full ~300-candidate pre-filter pass.

    Returns (growth_pct, window_label) where window_label is
    "12-month" or "6-month" — callers should describe the window
    accurately rather than always saying "trailing 12 months" even
    when a shorter comparison was actually used. Returns (None, None)
    if not even the shorter comparison can be computed — callers
    should treat this as "couldn't be determined", not "zero growth".
    """
    import yfinance as yf
    try:
        qf = yf.Ticker(ticker).quarterly_financials
        if qf is None or qf.empty:
            print(f"  {YELLOW}⚠ TTM check for {ticker}: quarterly_financials returned empty/None{RESET}")
            return None, None
        if "Total Revenue" not in qf.index:
            print(f"  {YELLOW}⚠ TTM check for {ticker}: 'Total Revenue' not in available fields "
                  f"({list(qf.index)[:5]}...){RESET}")
            return None, None
        # yfinance's quarterly_financials columns are most-recent-first
        revenue_row = qf.loc["Total Revenue"].dropna()
        n = len(revenue_row)

        if n >= 8:
            last_period, prior_period = revenue_row.iloc[0:4].sum(), revenue_row.iloc[4:8].sum()
            window_label = "12-month"
        elif n >= 4:
            last_period, prior_period = revenue_row.iloc[0:2].sum(), revenue_row.iloc[2:4].sum()
            window_label = "6-month"
        else:
            print(f"  {YELLOW}⚠ TTM check for {ticker}: only {n} quarters of revenue reported, "
                  f"need at least 4{RESET}")
            return None, None

        if prior_period <= 0:
            print(f"  {YELLOW}⚠ TTM check for {ticker}: prior {window_label} revenue is zero/negative "
                  f"(${prior_period:,.0f}), can't compute a growth rate{RESET}")
            return None, None

        result = round(((last_period - prior_period) / prior_period) * 100, 1)
        print(f"  {GREEN}✓ TTM check for {ticker}: {result:+.1f}% over trailing {window_label} "
              f"(${last_period:,.0f} vs ${prior_period:,.0f}){RESET}")
        return result, window_label
    except Exception as e:
        print(f"  {YELLOW}⚠ TTM check for {ticker} failed: {e}{RESET}")
        return None, None


def fetch_pick_prices(tickers, strategy=None, etf_tickers=None):
    """
    Fetches live data for all candidates then Python pre-filters
    using beta, RSI, and revenue growth before sending to Claude.
    Returns (picks, pre_excluded) — pre_excluded are stocks dropped before Claude sees them.
    """
    import yfinance as yf
    picks        = []
    pre_excluded = []
    pre_cfg  = (strategy or {}).get("candidate_universe", {}).get("pre_filter", {})
    max_beta = pre_cfg.get("max_beta", 1.5)
    min_rev  = pre_cfg.get("min_revenue_growth", -5)

    print(f"\n{BLUE}Step 2 — Fetching data for {len(tickers)} candidates + pre-filtering...{RESET}")

    for i, ticker in enumerate(tickers, 1):
        stage(2, f"Screening candidates {i}/{len(tickers)}")
        try:
            stock = yf.Ticker(ticker)
            info  = stock.info

            price = safe_float(
                info.get("regularMarketPrice") or
                info.get("currentPrice") or
                info.get("previousClose") or 0
            )
            if price == 0:
                continue

            beta       = safe_float(info.get("beta") or info.get("beta3Year") or 0)
            rev_growth = safe_float(info.get("revenueGrowth") or 0) * 100
            name       = info.get("longName") or info.get("shortName") or ticker
            sector     = info.get("sector") or info.get("category") or "Other"

            # 52-week-high position — computed here at ZERO extra API cost,
            # since fiftyTwoWeekHigh is already sitting in the SAME info
            # dict fetched above for beta/revenue checks. Pre-filter
            # rejects never reach Claude analysis, so this was previously
            # only available for candidates that made it further — but
            # headroom-to-52W-high is independent of why a stock was
            # excluded (volatility, declining revenue, etc.) and is
            # genuinely useful decision context regardless. Reuses the
            # exact same field name (gate7_headroom_pct) the dashboard's
            # existing badge already knows how to display — no dashboard
            # change needed to surface this.
            w52_high_pf = safe_float(info.get("fiftyTwoWeekHigh"))
            headroom_pct_pf = round(((w52_high_pf - price) / w52_high_pf) * 100, 1) if w52_high_pf > 0 else None

            # Pre-filter: drop high beta and severely declining revenue
            failures = []
            if beta > max_beta and beta > 0:
                failures.append({
                    "gate":    "Volatility (Pre-filter)",
                    "score":   f"Beta {beta:.2f}",
                    "needed":  f"Below {max_beta}",
                    "message": f"Beta of {beta:.2f} exceeds the {max_beta} volatility limit — this stock moves too aggressively relative to the market for the current strategy, creating outsized downside risk in adverse conditions."
                })
            if rev_growth < min_rev and rev_growth != 0:
                ttm_growth, ttm_window = compute_ttm_revenue_growth(ticker)
                ttm_note = ""
                if ttm_growth is not None:
                    divergence = abs(ttm_growth - rev_growth)
                    ttm_note = f" Trailing {ttm_window}: {ttm_growth:+.1f}% YoY."
                    if divergence >= 15:
                        # A quarterly figure this far off the longer-window trend
                        # usually means something one-time happened (a divestiture,
                        # a write-down, a comp-period distortion) rather than a
                        # genuine change in the ongoing business — flagged
                        # explicitly rather than left for you to notice only
                        # if you happen to already know the company.
                        ttm_note += " This is a notably different picture than the single quarter alone — worth a second look before assuming this reflects the ongoing business."
                failures.append({
                    "gate":    "Revenue Growth (Pre-filter)",
                    "score":   f"{rev_growth:.1f}% YoY" + (f" | {ttm_window} {ttm_growth:+.1f}%" if ttm_growth is not None else ""),
                    "needed":  f"Above {min_rev}%",
                    "message": f"Revenue declined {abs(rev_growth):.1f}% year-over-year — below the {min_rev}% minimum threshold. A shrinking top line suggests the business is losing customers or pricing power, raising the risk of further deterioration.{ttm_note}"
                })

            if failures:
                pre_excluded.append({
                    "ticker":        ticker,
                    "name":          name,
                    "sector":        sector,
                    "gates_failed":  failures,
                    "strategy_score": 0,
                    "stage":         "pre-filter",
                    "reason":        "; ".join([f["gate"] for f in failures]),
                    "price":                price,
                    "gate7_headroom_pct":   headroom_pct_pf,
                })
                print(f"  {YELLOW}✗ {ticker} pre-filtered: {', '.join([f['gate'] for f in failures])}{RESET}")
                continue

            mc = safe_float(info.get("marketCap") or 0)
            picks.append({
                "ticker":            ticker,
                "name":              name,
                "sector":            sector,
                "price":             round(price, 4),
                "current_price":     round(price, 4),
                "change_pct":        round(safe_float(info.get("regularMarketChangePercent") or 0) * 100, 2),
                "52w_high":          round(safe_float(info.get("fiftyTwoWeekHigh")), 4),
                "52w_low":           round(safe_float(info.get("fiftyTwoWeekLow")), 4),
                "pct_from_52w_high": round(((price - safe_float(info.get("fiftyTwoWeekHigh"))) / safe_float(info.get("fiftyTwoWeekHigh"))) * 100, 2) if info.get("fiftyTwoWeekHigh") else None,
                "volume_ratio":      round(safe_float(info.get("regularMarketVolume") or 0) / max(safe_float(info.get("averageVolume") or 1), 1), 2),
                "market_cap":        mc,
                "market_cap_class":  "Mega-cap" if mc >= 200e9 else "Large-cap" if mc >= 10e9 else "Mid-cap",
                "beta":              round(beta, 2) if beta else None,
                "pe_forward":        round(safe_float(info.get("forwardPE")), 2) if info.get("forwardPE") else None,
                "pe_trailing":       round(safe_float(info.get("trailingPE")), 2) if info.get("trailingPE") else None,
                "peg_ratio":         round(safe_float(info.get("pegRatio")), 2) if info.get("pegRatio") else None,
                "ev_to_ebitda":      round(safe_float(info.get("enterpriseToEbitda")), 2) if info.get("enterpriseToEbitda") else None,
                "price_to_sales":    round(safe_float(info.get("priceToSalesTrailingTwelveMonths")), 2) if info.get("priceToSalesTrailingTwelveMonths") else None,
                "dividend_yield":    round(safe_float(info.get("dividendYield") or 0) * 100, 2),
                "fcf_yield":         round((safe_float(info.get("freeCashflow") or 0) / max(mc, 1)) * 100, 2) if info.get("freeCashflow") and mc > 0 else None,
                "revenue_growth_yoy":round(rev_growth, 2) if rev_growth != 0 else None,
                "earnings_growth_yoy":round(safe_float(info.get("earningsGrowth") or 0) * 100, 2) if info.get("earningsGrowth") else None,
                "gross_margin":      round(safe_float(info.get("grossMargins") or 0) * 100, 2) if info.get("grossMargins") else None,
                "operating_margin":  round(safe_float(info.get("operatingMargins") or 0) * 100, 2) if info.get("operatingMargins") else None,
                "net_margin":        round(safe_float(info.get("profitMargins") or 0) * 100, 2) if info.get("profitMargins") else None,
                "roe":               round(safe_float(info.get("returnOnEquity") or 0) * 100, 2) if info.get("returnOnEquity") else None,
                "debt_to_equity":    round(safe_float(info.get("debtToEquity") or 0), 2) if info.get("debtToEquity") else None,
                "free_cash_flow":    safe_float(info.get("freeCashflow") or 0),
                "days_to_earnings":  None,
                "next_earnings_date":None,
                "rsi":    None,
                "sma_50": None,
                "sma_200":None,
                "list_type": "pick_candidate",
            })
            print(f"  {GREEN}✓ {ticker} @ ${price}  beta={beta:.2f}  rev={rev_growth:.1f}%{RESET}")

        except Exception as e:
            print(f"  {YELLOW}⚠ {ticker}: {e}{RESET}")

    print(f"\n{GREEN}✓ {len(picks)} passed pre-filter, {len(pre_excluded)} removed{RESET}")

    # Daily narrowing — only kicks in when the pool is meaningfully
    # larger than DAILY_NARROW_SIZE (i.e. the new 300-stock master list
    # path). With the old ~52-candidate Claude-generated universe this
    # is a no-op, since that's already smaller than the target.
    if len(picks) > DAILY_NARROW_SIZE:
        global G7_HARD_HEADROOM, G7_SOFT_HEADROOM
        g7 = (strategy or {}).get("pick_quality_gates", {}).get("gate7_headroom", {})
        G7_HARD_HEADROOM = g7.get("hard_headroom_pct", G7_HARD_HEADROOM)
        G7_SOFT_HEADROOM = g7.get("soft_headroom_pct", G7_SOFT_HEADROOM)
        etf_set = set((etf_tickers or []))
        etfs    = [p for p in picks if p["ticker"] in etf_set]
        stocks  = [p for p in picks if p["ticker"] not in etf_set]
        for p in stocks:
            p["daily_strength"] = compute_daily_strength(p)
        stocks_sorted = sorted(stocks, key=lambda p: -p["daily_strength"])
        narrowed = stocks_sorted[:DAILY_NARROW_SIZE] + etfs
        print(f"{BLUE}  Narrowed {len(stocks)} stocks -> top {min(len(stocks),DAILY_NARROW_SIZE)} by today's strength "
              f"(+{len(etfs)} ETF/commodity slot(s) exempt from narrowing) -> sending {len(narrowed)} to Claude{RESET}")
        picks = narrowed
    else:
        print(f"{BLUE}  Sending all {len(picks)} to Claude{RESET}")

    return picks, pre_excluded


def compute_calibration(min_n=30):
    """X6: does the model's stated confidence predict outcomes? Groups every
    graded outcome-tracker checkpoint by the confidence recorded at pick
    time. Buckets under min_n are reported but marked as not meaningful."""
    try:
        with open(os.path.join(BASE_DIR, "logs", "outcome_log.json")) as f:
            log = json.load(f)
    except Exception:
        return {}
    buckets = {}
    for e in log.values():
        conf = e.get("confidence_at_pick")
        if not conf:
            continue
        for cp in (e.get("checkpoints") or {}).values():
            if cp.get("status") == "graded" and cp.get("grader") == "rules" and cp.get("price_at_grading") and e.get("entry_price"):
                b = buckets.setdefault(conf, {"n": 0, "correct": 0, "ret_sum": 0.0})
                b["n"] += 1
                b["correct"] += cp.get("grade") == "CORRECT"
                b["ret_sum"] += (cp["price_at_grading"] / e["entry_price"] - 1) * 100
    out = {}
    for conf, b in buckets.items():
        out[conf] = {"graded": b["n"], "hit_rate_pct": round(100 * b["correct"] / b["n"], 1),
                     "avg_return_pct": round(b["ret_sum"] / b["n"], 2), "meaningful": b["n"] >= min_n}
    return {"by_confidence": out, "min_n": min_n,
            "note": "Confidence/score at pick time are recorded from 2026-11-02; earlier picks can't be calibrated."}


def add_decision_fields(analysis, strategy):
    """X5: every pick carries what a decision needs — a stop (Claude's, or
    the strategy's level-1 stop as a fallback), and the risk/reward that
    stop implies against the target."""
    l1 = abs((((strategy or {}).get("stop_loss", {}).get("contextual_stop", {}) or {}).get("gates", {})
              .get("gate_0b_cumulative_drift", {}).get("thresholds", {}).get("level_1_pct", -8)))
    for p in analysis.get("agent_picks", []):
        price  = safe_float(p.get("price") or p.get("current_price"))
        target = safe_float(p.get("analyst_target_price")) or safe_float(p.get("target_price"))
        stop   = safe_float(p.get("stop_price"))
        if price <= 0:
            continue
        if not (0 < stop < price):
            stop = round(price * (1 - l1 / 100), 2)
            p["stop_source"] = f"strategy level-1 stop (-{l1:g}%)"
        else:
            p["stop_source"] = "Claude"
        p["stop_price"] = stop
        p["risk_pct"]   = round((price - stop) / price * 100, 1)
        if target > price:
            p["reward_pct"]  = round((target - price) / price * 100, 1)
            p["risk_reward"] = round((target - price) / (price - stop), 2)


PICK_BOOK_PATH = os.path.join(BASE_DIR, "logs", "pick_book.json")


def update_pick_book(analysis):
    """X10: picks persist across runs instead of churning daily. Each pick
    is NEW, STILL VALID (with days listed), or RETIRED with a reason —
    the Delta tab and pick cards show this."""
    try:
        with open(PICK_BOOK_PATH) as f:
            book = json.load(f)
    except Exception:
        book = {}
    today = datetime.now().strftime("%Y-%m-%d")
    current = {str(p.get("ticker", "")).upper(): p for p in analysis.get("agent_picks", [])}
    excluded = {str(e.get("ticker", "")).upper(): e for e in analysis.get("picks_excluded", [])}
    for t, p in current.items():
        e = book.get(t)
        if not e or e.get("status") == "RETIRED":
            book[t] = {"ticker": t, "first_seen": today, "last_seen": today, "status": "NEW",
                       "entry_price_at_first": p.get("price") or p.get("current_price"), "runs_listed": 1}
        else:
            e.update(status="STILL_VALID", last_seen=today, runs_listed=e.get("runs_listed", 1) + 1)
        p["pick_status"] = book[t]["status"]
        p["listed_since"] = book[t]["first_seen"]
    retired_today = []
    for t, e in book.items():
        if e.get("status") in ("NEW", "STILL_VALID") and t not in current:
            ex = excluded.get(t)
            e.update(status="RETIRED", retired_on=today,
                     retired_reason=(f"failed gates: {ex.get('reason')}" if ex else
                                     "not on today's shortlist (outranked by other candidates or dropped by the pre-filter)"))
            retired_today.append(e)
    with open(PICK_BOOK_PATH, "w") as f:
        json.dump(book, f, indent=2)
    analysis["pick_book"] = {"active": [e for e in book.values() if e["status"] != "RETIRED"],
                             "retired_today": retired_today}


def build_gate_rules_text(strategy, max_sector_pct):
    """The PICK QUALITY GATES section of the Step 3 prompt, generated from
    the same strategy.json values apply_pick_quality_gates() enforces —
    one rulebook. Previously the prompt hardcoded its own thresholds
    (score >= 90, Low risk only, High confidence only, "not more than 5%
    above the 52w high", a Gate 6 with no code behind it) that disagreed
    with the Python gates, and Claude cited rules that don't exist."""
    st   = strategy or {}
    pqg  = st.get("pick_quality_gates", {})
    er   = st.get("entry_rules", {})
    g7   = pqg.get("gate7_headroom", {})
    min_score  = pqg.get("gate1_strategy_score", {}).get("min_score", 81)
    risks      = pqg.get("gate2_risk_level", {}).get("acceptable_levels", ["Low", "Medium"])
    confs      = pqg.get("gate3_confidence", {}).get("acceptable_levels", ["Medium", "High"])
    buffer_pct = pqg.get("gate4_sector_exposure", {}).get("buffer_pct", 5)
    earn_days  = pqg.get("gate5_entry_conditions", {}).get("earnings_risk_days", 7)
    hard, soft, up = g7.get("hard_headroom_pct", 15), g7.get("soft_headroom_pct", 10), g7.get("soft_min_upside_pct", 20)
    text = f"""════ PICK QUALITY GATES (enforced in code after your reply) ════
The system re-checks every agent pick against these exact rules using live data.
Picks that fail are moved to an excluded list automatically, so only return picks
you expect to pass. Returning 0 picks is acceptable — never pad the list.

GATE 1 — strategy_score >= {min_score} (your 0-100 fit with this strategy and portfolio).
GATE 2 — risk_level is one of: {", ".join(risks)}.
GATE 3 — confidence is one of: {", ".join(confs)}.
GATE 4 — Sector: the pick's sector must be below {max_sector_pct - buffer_pct}% of total capital
         today (the {max_sector_pct}% cap minus a {buffer_pct}-point buffer). Sectors in the
         CAPPED SECTORS list above fail.
GATE 5 — Entry conditions: RSI(14) <= {er.get("rsi_max", 60)} (if RSI is N/A, treat this part as unchecked — code flags it), the market's
         Fear & Greed <= {er.get("fear_greed_max", 65)}, and no earnings report due in the next
         {earn_days} days (a report that already happened is fine).
GATE 7 — 52-week-high headroom: price at least {hard:g}% below its 52-week high, OR at least
         {soft:g}% below with at least {up:g}% upside to target. Use from52wH in the data.
         (There is no Gate 6.)

For each pick include gate_results with your own PASS/FAIL view of each gate."""
    return text, "|".join(reversed(confs)), "|".join(risks)


# ── STEP 3: FULL ANALYSIS ─────────────────────────────────────
def run_agent(holdings, interested, picks_data, data, context=None, strategy=None):
    total_value   = safe_float(data.get("total_value"))
    total_capital = safe_float(data.get("total_capital"))
    cash          = safe_float(data.get("cash"))
    total_pnl_usd = safe_float(data.get("total_pnl_usd"))
    total_pnl_pct = safe_float(data.get("total_pnl_pct"))

    # Add portfolio weight to each holding — against TOTAL CAPITAL (including cash)
    for s in holdings:
        val = safe_float(s.get("current_value"))
        s["portfolio_weight"] = round((val/total_capital)*100, 1) if total_capital > 0 else 0

    h_lines = "\n".join([fmt(s, include_position=True)  for s in holdings])   or "None"
    i_lines = "\n".join([fmt(s, include_position=False) for s in interested]) or "None"
    p_lines = "\n".join([fmt(s, include_position=False) for s in picks_data]) or "None"
    exclude = ", ".join([s.get("ticker","") for s in holdings+interested])
    today   = datetime.now().strftime("%A %d %B %Y")

    context_summary  = context.get("summary","No market context available.") if context else "No market context available."
    strategy_rules   = build_strategy_rules(strategy, holdings, data) if strategy else ""

    # Build current sector exposure for gate checks
    sector_map   = {}
    total_capital = safe_float(data.get("total_capital", 0))
    for h in holdings:
        sec = get_sector(h.get("ticker",""), h.get("name","") or h.get("company_name",""), h.get("sector",""))
        val = safe_float(h.get("current_value", 0))
        sector_map[sec] = sector_map.get(sec, 0) + val
    sector_pcts = {k: round((v/total_capital)*100, 1) for k,v in sector_map.items()} if total_capital > 0 else {}
    max_sector_pct = strategy.get("position_sizing", {}).get("max_sector_pct", 30) if strategy else 30

    # Sectors already at or near cap — picks in these sectors must be excluded
    capped_sectors = [s for s,p in sector_pcts.items() if p >= max_sector_pct - 5]  # within 5% of cap

    sector_exposure_str = "\n".join([f"  {s}: {p}%" for s,p in sorted(sector_pcts.items(), key=lambda x:-x[1])]) or "  No holdings"
    gate_rules, pick_conf_str, pick_risk_str = build_gate_rules_text(strategy, max_sector_pct)
    capped_str = ", ".join(capped_sectors) if capped_sectors else "None"

    prompt = f"""You are a personal portfolio analyst and investment strategy advisor. Today: {today}.
Analyse the portfolio below against the investor's personal strategy rules and return ONLY valid JSON — no markdown, no extra text.

════ MARKET CONTEXT ════
{context_summary}

{strategy_rules}

════ PORTFOLIO SUMMARY ════
Total Holdings Value: ${total_value:,.2f}
Cash Available:       ${cash:,.2f}
Total Capital:        ${total_capital:,.2f}
Overall P&L:          ${total_pnl_usd:+,.2f} ({total_pnl_pct:+.2f}%)

════ DATA FIELDS LEGEND ════
price=current live price | chg=daily % change | vol=volume ratio vs 30d avg
RSI=momentum 0-100 (>70 overbought, <30 oversold) | SMA50/200=moving averages
from52wH=% below 52-week high | mktcap=market cap | beta=volatility vs S&P500
pe_fwd=forward P/E | peg=P/E÷growth | ev_ebitda=enterprise value multiple
fcf_yield=free cash flow÷market cap | rev_growth=revenue growth YoY
op_mgn=operating margin | roe=return on equity | debt_eq=debt÷equity
days_to_earnings=days until next earnings (CRITICAL if <7 days)
buy=investor's buy price | target=profit exit | stop=loss exit price

════ CURRENT SECTOR EXPOSURE ════
{sector_exposure_str}
Max sector allowed: {max_sector_pct}%
CAPPED SECTORS (at or near {max_sector_pct}% limit — DO NOT add picks from these): {capped_str}

════ HOLDINGS (with full position data) ════
{h_lines}

════ INTERESTED (watching, not owned) ════
{i_lines}

════ AGENT PICKS CANDIDATES (live prices — use ONLY these prices) ════
{p_lines}
Do NOT suggest any of these: {exclude}

CRITICAL PORTFOLIO WEIGHT RULE:
All weights are against TOTAL CAPITAL including cash. Cash is a position.
Example: $37,000 MSFT in $107,000 total = 34.6%. Never calculate against holdings value alone.

{gate_rules}

Return this exact JSON:
{{
  "analysis_date": "{datetime.now().strftime('%Y-%m-%d %H:%M')}",
  "market_mood": "BULLISH|BEARISH|NEUTRAL",
  "portfolio_health": "one sentence on overall portfolio condition",
  "top_action_today": "the single most important action to take today",
  "holdings": [
    {{
      "ticker":"X","name":"X",
      "signal":"STRONG BUY|BUY|ADD|HOLD|WATCH|SELL",
      "confidence":"High|Medium|Low",
      "reason":"2 sentences using position data AND valuation/fundamental data",
      "pnl_pct":0.0,"pnl_usd":0.0,"pnl_status":"profit|loss|breakeven",
      "portfolio_weight":0.0,
      "price_vs_target":"e.g. 5% below target",
      "price_vs_stop":"e.g. 12% above stop",
      "risk_level":"Low|Medium|High",
      "risk_note":"1 sentence — mention earnings risk if days_to_earnings < 14",
      "strategy_score":0,"technical_score":0,"fundamental_score":0,"valuation_score":0,
      "recommended_alert_above":"suggested price","recommended_alert_below":"suggested price",
      "alert_reasoning":"1 sentence"
    }}
  ],
  "interested": [
    {{
      "ticker":"X","name":"X",
      "signal":"STRONG BUY|BUY|WATCH",
      "confidence":"High|Medium|Low",
      "reason":"2 sentences including valuation and fundamental quality",
      "suggested_entry":"$X-Y price range only",
      "entry_condition":"1 sentence — include RSI and earnings timing",
      "upside_potential":"e.g. ~18% upside",
      "risk_level":"Low|Medium|High","risk_note":"1 sentence",
      "strategy_score":0,"technical_score":0,"fundamental_score":0,"valuation_score":0,
      "recommended_alert_above":"breakout level","recommended_alert_below":"entry dip level",
      "alert_reasoning":"1 sentence"
    }}
  ],
  "agent_picks": [
    {{
      "ticker":"X","name":"X","sector":"X",
      "signal":"STRONG BUY|BUY",
      "confidence":"{pick_conf_str}",
      "reason":"2 sentences — must reference valuation, fundamentals, and technical setup",
      "current_price":0.0,
      "target_price":0.0,
      "stop_price":0.0,
      "time_horizon":"e.g. 1-3 months",
      "catalyst":"1 sentence — what should move the price in that window",
      "invalidation":"1 sentence — the specific observation that would prove this pick wrong",
      "suggested_entry":"$X-Y at or near live price",
      "upside_potential":"e.g. ~22% upside",
      "risk_level":"{pick_risk_str}",
      "risk_note":"1 sentence",
      "strategy_score":0,"technical_score":0,"fundamental_score":0,"valuation_score":0,
      "pe_forward":0.0,"revenue_growth":0.0,"operating_margin":0.0,"days_to_earnings":null,
      "gate_results":{{
        "gate1_strategy_score":"PASS|FAIL — score: X",
        "gate2_risk":"PASS|FAIL",
        "gate3_confidence":"PASS|FAIL",
        "gate4_sector_ok":"PASS|FAIL — sector X at Y%",
        "gate5_entry_conditions":"PASS|FAIL — RSI: X, F&G: Y, earnings: Z",
        "gate7_52w_headroom":"PASS|FAIL — X% below 52W high"
      }}
    }}
  ],
  "picks_excluded": [],
  "portfolio_insights": {{
    "largest_position":"ticker and % of portfolio",
    "cash_deployment":"advice on deploying the ${cash:,.0f} cash",
    "concentration_risk":"any sector or position concentration warning",
    "rebalancing_suggestion":"1 specific rebalancing action if needed"
  }},
  "strategy_alerts": [
    {{
      "severity":"CRITICAL|WARNING|INFO",
      "rule":"which strategy rule is affected",
      "message":"specific actionable message with dollar amounts",
      "action":"exact action to take"
    }}
  ]
}}

SIGNAL RULES:
- STRONG BUY: exceptional setup, max 1-2 across entire analysis
- BUY: clear bullish opportunity with full gate compliance
- holdings: the signal is your view of the stock (ADD = you would own more). The system turns it into TRIM / TOP-UP / HOLD from the position-size, cash and entry rules, so judge the business, not the position size. Never SELL only because a position is overweight; SELL means you would exit the whole position
- agent_picks: STRONG BUY or BUY only, and only picks you expect to pass the PICK QUALITY GATES above
- suggested_entry: price range ONLY e.g. "$185-195" — no words
- pnl_pct and pnl_usd: use the values from the data above
- stop loss is BELOW buy price | target is ABOVE buy price — never confuse these
- portfolio_weight: against TOTAL CAPITAL including cash — never holdings value alone
- recommended_alert_above: for holdings = 2-3% below target; for interested = breakout level
- recommended_alert_below: for holdings = stop + small buffer; for interested = entry dip
- ALL text fields: 2 sentences MAX
- picks_excluded: leave as empty array [] — exclusions are handled by the system automatically
- Return ONLY the JSON"""

    print(f"\n{BLUE}Step 3 — Full Claude analysis with portfolio context...{RESET}")
    import time
    for attempt in range(3):
        try:
            analysis = ask_json(client, prompt, max_tokens=16000, label="agent_brain_analysis")

            # ── SAFETY NET: Python-enforced exclusion of holdings + interested ──
            # Claude is instructed to exclude these in the prompt, but we enforce
            # it here in Python regardless — never trust Claude to self-exclude
            excluded_tickers = set(
                str(h.get("ticker","")).upper()
                for h in holdings + interested
            )
            raw_picks = analysis.get("agent_picks", [])
            clean_picks = []
            for p in raw_picks:
                pt = str(p.get("ticker","")).upper()
                if pt in excluded_tickers:
                    print(f"{YELLOW}⚠ Safety net: removed {pt} from picks — already held/watched{RESET}")
                else:
                    clean_picks.append(p)
            analysis["agent_picks"] = clean_picks

            print(f"{GREEN}✓ Analysis complete{RESET}")
            return analysis
        except StructuredReplyError as err:
            print(f"{RED}✗ Claude reply error: {err}{RESET}")
            return None
        except Exception as e:
            if "overloaded" in str(e).lower() and attempt < 2:
                wait = (attempt + 1) * 30
                print(f"{YELLOW}⚠ API overloaded — retrying in {wait}s...{RESET}")
                time.sleep(wait)
            else:
                print(f"{RED}✗ Claude error: {e}{RESET}")
                return None
    return None

# ── RANK PICKS ────────────────────────────────────────────────
def enrich_holdings(analysis, data):
    """Merge live pnl_usd, pnl_pct, current_value, portfolio_weight from data into analysis holdings"""
    stocks         = data.get("stocks", [])
    stock_map      = {str(s.get("ticker","")).upper(): s for s in stocks}
    total_capital  = safe_float(data.get("total_capital"))  # includes cash — correct denominator
    holdings       = analysis.get("holdings", [])

    for h in holdings:
        t      = str(h.get("ticker","")).upper()
        s_data = stock_map.get(t, {})
        if safe_float(s_data.get("pnl_pct")) != 0:
            h["pnl_pct"] = s_data.get("pnl_pct")
        if safe_float(s_data.get("pnl_usd")) != 0:
            h["pnl_usd"] = s_data.get("pnl_usd")
        h["current_value"]    = s_data.get("current_value", 0)
        h["quantity"]         = s_data.get("quantity", 0)
        h["sector"]           = get_sector(t, h.get("name","") or h.get("company_name",""), s_data.get("sector",""))
        h["company_name"]     = s_data.get("company_name") or h.get("name","")
        # Weight against total capital (cash is a position — counts in denominator)
        h["portfolio_weight"] = round((safe_float(s_data.get("current_value")) / total_capital) * 100, 1) if total_capital > 0 else 0

    analysis["holdings"] = holdings
    return analysis


def apply_pick_quality_gates(analysis, data, strategy, sector_pcts, capped_sectors, universe_size=0, prefiltered_size=0, pre_excluded=None):
    """
    Hard Python enforcement of pick quality gates.
    Drops any pick that fails any gate, regardless of what Claude said.
    Moves failed picks to picks_excluded.
    """
    picks    = analysis.get("agent_picks", [])
    excluded = list(pre_excluded) if pre_excluded else []  # Start with pre-filter exclusions
    passed   = []
    g1_6_qualifying = []  # picks passing Gates 1-6 regardless of Gate 7 — feeds outcome_tracker

    fg_raw     = data.get("fear_greed", {}).get("score") if isinstance(data.get("fear_greed"), dict) else None
    fg_score   = safe_float(fg_raw) if fg_raw is not None else None  # None = CNN feed unavailable today
    max_sector = strategy.get("position_sizing", {}).get("max_sector_pct", 30) if strategy else 30
    rsi_max    = strategy.get("entry_rules", {}).get("rsi_max", 60) if strategy else 60
    fg_max     = strategy.get("entry_rules", {}).get("fear_greed_max", 65) if strategy else 65

    # Gate thresholds — previously hardcoded directly in this function's
    # logic below; now read from strategy.json's pick_quality_gates
    # section so every gate can be tuned without a code change, same as
    # rsi_max/fg_max above already were. Defaults match the original
    # hardcoded values exactly, so behavior is unchanged for anyone not
    # yet using this config section.
    pqg = (strategy or {}).get("pick_quality_gates", {})
    min_strategy_score    = pqg.get("gate1_strategy_score", {}).get("min_score", 90)
    acceptable_risk       = pqg.get("gate2_risk_level", {}).get("acceptable_levels", ["Low"])
    acceptable_confidence = pqg.get("gate3_confidence", {}).get("acceptable_levels", ["High"])
    sector_buffer_pct     = pqg.get("gate4_sector_exposure", {}).get("buffer_pct", 5)
    earnings_risk_days    = pqg.get("gate5_entry_conditions", {}).get("earnings_risk_days", 7)
    gate7_cfg             = pqg.get("gate7_headroom", {})
    hard_headroom         = gate7_cfg.get("hard_headroom_pct", 15.0)
    soft_headroom         = gate7_cfg.get("soft_headroom_pct", 10.0)
    min_upside            = gate7_cfg.get("soft_min_upside_pct", 20.0)

    # Build stock_map for enriching picks with live market data
    stocks_list = data.get("stocks", [])
    pick_stock_map = {str(s.get("ticker","")).upper(): s for s in stocks_list}

    for pick in picks:
        ticker     = pick.get("ticker","")
        t_upper    = ticker.upper()
        failures   = []

        # Enrich pick with live market data fields — always use live values, never Claude's
        s_data = pick_stock_map.get(t_upper, {})
        if s_data.get("52w_high"):
            pick["52w_high"] = s_data["52w_high"]         # always overwrite — live yfinance value
        if s_data.get("price"):
            pick["price"] = s_data["price"]               # always overwrite — live price
        if s_data:
            pick["rsi"] = s_data.get("rsi")               # always overwrite — live RSI (None if history failed)
            pick["atr_pct"] = s_data.get("atr_pct")       # L4 sizing: typical daily range
        if s_data.get("sector"):
            pick["sector"] = s_data["sector"]             # always overwrite — live sector (G4)
        if s_data.get("days_to_earnings") is not None:
            pick["days_to_earnings"] = s_data["days_to_earnings"]  # always overwrite — live earnings calendar (G5)

        # Gate 1 — strategy_score >= min_strategy_score
        ss = safe_float(pick.get("strategy_score", 0))
        if ss < min_strategy_score:
            gap = round(min_strategy_score - ss)
            failures.append({
                "gate": "Strategy Score",
                "score": f"{int(ss)}/100",
                "needed": f"{min_strategy_score}/100 minimum",
                "message": f"Overall strategy fit score is {int(ss)}/100 — needs {gap} more points to qualify. Stock doesn't strongly align with current portfolio rules and conditions."
            })

        # Gate 2 — risk_level must be one of acceptable_risk
        rl = pick.get("risk_level","")
        if rl not in acceptable_risk:
            failures.append({
                "gate": "Risk Level",
                "score": rl or "Unknown",
                "needed": " or ".join(acceptable_risk),
                "message": f"Risk rated as '{rl}' — only {'/'.join(acceptable_risk)} risk picks are accepted. This stock carries too much volatility or uncertainty for the current strategy."
            })

        # Gate 3 — confidence must be one of acceptable_confidence
        conf = pick.get("confidence","")
        if conf not in acceptable_confidence:
            failures.append({
                "gate": "Confidence",
                "score": conf or "Unknown",
                "needed": " or ".join(acceptable_confidence),
                "message": f"Confidence rated as '{conf}' — only {'/'.join(acceptable_confidence)} confidence picks are accepted. Signals are not strong or clear enough to justify entry."
            })

        # Gate 4 — sector not capped
        # sector field already overwritten with live yfinance value in enrichment above
        sector = pick.get("sector","")
        mapped_sector   = get_sector(ticker, pick.get("name",""), sector)
        check_sector    = sector if sector else mapped_sector
        current_sec_pct = sector_pcts.get(check_sector, 0)
        if check_sector in capped_sectors or current_sec_pct >= max_sector - sector_buffer_pct:
            room = max_sector - current_sec_pct
            failures.append({
                "gate": "Sector Exposure",
                "score": f"{current_sec_pct}% in {check_sector}",
                "needed": f"Below {max_sector}% cap",
                "message": f"Portfolio already has {current_sec_pct}% exposure to {check_sector} — only {room:.1f}% room remaining before the {max_sector}% sector cap. Adding this pick would over-concentrate the portfolio."
            })

        # Gate 5 — entry conditions
        # RSI is computed from Yahoo price history (technicals.py). When
        # Yahoo returns no history the RSI part of Gate 5 is skipped, not
        # failed, and the pick carries a visible flag saying so (it used
        # to pass silently with no flag).
        pick_rsi = safe_float(pick.get("rsi", 0))
        pick["rsi_check"] = "PASSED"
        if pick_rsi <= 0:
            pick["rsi_check"] = "SKIPPED"
            pick["rsi_check_note"] = (f"G5 RSI check skipped — Yahoo price history unavailable, so RSI "
                                      f"(limit {rsi_max}) could not be computed. Check RSI yourself before buying.")
            print(f"  {YELLOW}⚠ {ticker}: G5 RSI check skipped — no price history{RESET}")
        elif pick_rsi > rsi_max:
            pick["rsi_check"] = "FAILED"
            failures.append({
                "gate": "Entry — RSI",
                "score": f"RSI {pick_rsi}",
                "needed": f"RSI below {rsi_max}",
                "message": f"RSI is {pick_rsi} — above the {rsi_max} entry limit. Stock is overbought or has too much momentum; waiting for a better entry point reduces risk."
            })
        if fg_score is not None and fg_score > fg_max:
            failures.append({
                "gate": "Entry — Market Sentiment",
                "score": f"Fear & Greed {fg_score}",
                "needed": f"Below {fg_max}",
                "message": f"Market Fear & Greed index is {fg_score} — above the {fg_max} threshold. Market sentiment is too greedy/euphoric for new entries right now."
            })

        # Core gates (1-4, RSI, Fear&Greed) checkpoint — captured
        # BEFORE Earnings Risk and Gate 7, since both of those are now
        # caveat-tier: a pick can fail either or both and still be
        # surfaced as a flagged pick, rather than excluded outright,
        # as long as every CORE gate passed.
        core_gates_passed = (len(failures) == 0)

        # Entry — Earnings Risk — tracked separately from the main
        # failures list (same treatment as Gate 7 below) rather than
        # a hard exclusion. A flat "≤N days" cutoff treats a stock
        # reporting today identically to one that reported yesterday
        # (already-resolved risk) — surfacing it as a caveat instead
        # lets you make that call yourself rather than losing an
        # otherwise-qualified pick purely over report timing.
        earnings_risk_failure = None
        dte = pick.get("days_to_earnings")
        if dte is not None and 0 <= safe_float(dte) <= earnings_risk_days:
            dte_val = safe_float(dte)
            timing_note = "already reported" if dte_val < 0 else f"in {dte} days"
            earnings_risk_failure = {
                "gate": "Entry — Earnings Risk",
                "score": f"{dte} days to earnings",
                "needed": f"More than {earnings_risk_days} days",
                "message": f"Earnings report due {timing_note} — too close to a report to treat as a clean new-entry timing. Earnings create binary risk; this is flagged as a caveat rather than excluded outright."
            }

        # g1_6_passed preserves the ORIGINAL "Gates 1-6" definition
        # (including Earnings Risk) — outcome_tracker already depends
        # on this exact meaning to grade raw pick quality regardless
        # of Gate 7, so it's left untouched rather than redefined.
        g1_6_passed = core_gates_passed and (earnings_risk_failure is None)
        if g1_6_passed:
            g1_6_qualifying.append(dict(pick))

        # Gate 7 — 52W High headroom
        # PASS if: headroom ≥ hard_headroom (default 15%)
        # OR PASS if: headroom ≥ soft_headroom (default 10%) AND upside_to_target ≥ min_upside (default 20%)
        # This avoids rejecting high-quality momentum stocks with strong target upside
        # (hard_headroom / soft_headroom / min_upside loaded from strategy.json at
        # the top of this function — see pick_quality_gates.gate7_headroom)
        w52_high   = safe_float(pick.get("52w_high", 0))
        pick_price = safe_float(pick.get("price") or pick.get("current_price", 0))

        gate7_headroom_pct  = None
        gate7_upside_pct    = None
        gate7_upside_source = None
        gate7_passed_flag   = None  # None = couldn't be computed (missing price data)

        if w52_high > 0 and pick_price > 0:
            headroom_pct = ((w52_high - pick_price) / w52_high) * 100

            # Calculate upside to target.
            # Priority 1: analyst consensus (targetMeanPrice, fetched live
            # from yfinance above) — a genuinely external, market-aggregated
            # figure, not something Claude generated itself.
            # Priority 2: Claude's own target_price field — kept as a
            # fallback for tickers with no analyst coverage, but this is
            # Claude's own opinion, generated in the same breath as the
            # pick itself, so it's flagged with a distinct source label.
            # Priority 3: parse upside_potential string, same as before.
            upside_source = None
            analyst_target = safe_float(pick.get("analyst_target_price"))
            claude_target  = safe_float(pick.get("target_price") or pick.get("target") or pick.get("price_target", 0))

            if analyst_target > pick_price > 0:
                target_price  = analyst_target
                upside_pct    = ((target_price - pick_price) / pick_price) * 100
                upside_source = "analyst_consensus"
            elif claude_target > pick_price > 0:
                target_price  = claude_target
                upside_pct    = ((target_price - pick_price) / pick_price) * 100
                upside_source = "claude_estimate"
            else:
                upside_pct    = 0.0
                # Parse upside_potential string as fallback
                import re as _re
                up_str = str(pick.get("upside_potential", "") or "")
                pct_match = _re.search(r"(\d+(?:\.\d+)?)\s*%", up_str)
                if pct_match:
                    upside_pct    = float(pct_match.group(1))
                    upside_source = "claude_estimate"
                elif pick_price > 0:
                    # Try to find a dollar target in the string e.g. "~$310 target"
                    dollar_match = _re.search(r"\$(\d+(?:\.\d+)?)", up_str)
                    if dollar_match:
                        implied_target = float(dollar_match.group(1))
                        if implied_target > pick_price:
                            upside_pct    = ((implied_target - pick_price) / pick_price) * 100
                            upside_source = "claude_estimate"

            passes_hard = headroom_pct >= hard_headroom
            passes_soft = headroom_pct >= soft_headroom and upside_pct >= min_upside

            # Recorded regardless of pass/fail — this used to only be
            # captured on failure, which meant a pick that passed Gate 7
            # while failing other gates left zero trace of its actual
            # margin, making it impossible to rank such picks by how
            # strongly they cleared Gate 7's own bar.
            gate7_headroom_pct = round(headroom_pct, 1)
            gate7_upside_pct   = round(upside_pct, 1)
            gate7_upside_source = upside_source  # "analyst_consensus" | "claude_estimate" | None
            gate7_passed_flag  = bool(passes_hard or passes_soft)

            source_label = ("analyst consensus" if upside_source == "analyst_consensus"
                             else "Claude's own estimate — no analyst coverage found" if upside_source == "claude_estimate"
                             else "no target data available")

            if not passes_hard and not passes_soft:
                if headroom_pct >= soft_headroom:
                    # Has 10-15% headroom but not enough target upside
                    fail_msg = (f"Stock is {headroom_pct:.1f}% below its 52-week high of ${w52_high:.2f} "
                                f"— headroom qualifies for soft path (≥{soft_headroom:.0f}%) but upside to target "
                                f"is only {upside_pct:.1f}% ({source_label}), need ≥{min_upside:.0f}% to compensate. "
                                f"Either wait for a deeper pullback or a higher target.")
                else:
                    # Below 10% — hard fail regardless of upside
                    fail_msg = (f"Stock is only {headroom_pct:.1f}% below its 52-week high of ${w52_high:.2f} "
                                f"— insufficient headroom. Need ≥{hard_headroom:.0f}%, or ≥{soft_headroom:.0f}% "
                                f"with ≥{min_upside:.0f}% upside to target (currently {upside_pct:.1f}%).")
                failures.append({
                    "gate":    "Entry — 52W High Headroom",
                    "score":   f"{headroom_pct:.1f}% below 52W high | {upside_pct:.1f}% upside",
                    "needed":  f"≥{hard_headroom:.0f}% headroom, OR ≥{soft_headroom:.0f}% + ≥{min_upside:.0f}% upside",
                    "message": fail_msg,
                })

        # A pick is now only fully EXCLUDED if a CORE gate failed (Gates
        # 1-4, RSI, Fear & Greed). Earnings Risk and Gate 7 are both
        # caveat-tier: failing either or both no longer excludes a pick
        # outright, as long as every core gate passed — generalizing the
        # existing Gate-7-only treatment to also cover Earnings Risk,
        # since both represent timing/momentum judgment calls rather
        # than fundamental quality problems with the pick itself.
        if not core_gates_passed:
            # Core gate failure — combine everything that actually
            # failed (core failures already in `failures`, plus
            # earnings/gate7 if those also happened to fail) into one
            # full exclusion record, same shape as before.
            all_failures = list(failures)
            if earnings_risk_failure:
                all_failures.append(earnings_risk_failure)
            failed_gates = [f["gate"] for f in all_failures]
            excluded.append({
                "ticker":       ticker,
                "name":         pick.get("name",""),
                "sector":       check_sector,
                "gates_failed": all_failures,
                "strategy_score": int(ss),
                "reason":       "; ".join(failed_gates),
                # Recorded even when Gate 7 itself passed — lets the
                # dashboard surface "failed elsewhere, but genuinely
                # cleared Gate 7" picks and rank them by margin, not
                # just flag pass/fail.
                "gate7_passed":        gate7_passed_flag,
                "gate7_headroom_pct":  gate7_headroom_pct,
                "gate7_upside_pct":    gate7_upside_pct,
                "gate7_upside_source": gate7_upside_source,
                # confidence/risk_level were already computed above for
                # Gates 2-3 — previously dropped here even though the
                # data existed, which meant the Adaptive Simulator could
                # never apply its confidence/risk avoid-rules to
                # excluded-pool candidates. Now carried through.
                "confidence":   conf,
                "risk_level":   rl,
                "rsi_check":    pick.get("rsi_check"),
            })
            print(f"  {YELLOW}✗ {ticker} excluded — {', '.join(failed_gates)}{RESET}")
        else:
            # Every core gate passed — include as a pick regardless of
            # Earnings Risk / Gate 7 outcome, tagging each independently
            # so the dashboard can show exactly which caveat(s) apply.
            pick["gate7_status"] = "FAILED" if any(f["gate"] == "Entry — 52W High Headroom" for f in failures) else "PASSED"
            pick["gate7_detail"] = next((f for f in failures if f["gate"] == "Entry — 52W High Headroom"), None)
            pick["gate7_headroom_pct"]  = gate7_headroom_pct
            pick["gate7_upside_pct"]    = gate7_upside_pct
            pick["gate7_upside_source"] = gate7_upside_source

            pick["earnings_status"] = "FAILED" if earnings_risk_failure else "PASSED"
            pick["earnings_detail"] = earnings_risk_failure

            passed.append(pick)
            if pick["gate7_status"] == "PASSED" and pick["earnings_status"] == "PASSED":
                print(f"  {GREEN}✓ {ticker} passed all 7 gates"
                      + (" (G5 RSI not checked)" if pick.get("rsi_check") == "SKIPPED" else "") + f"{RESET}")
            else:
                caveats = [c for c, ok in [("Gate 7", pick["gate7_status"]=="PASSED"), ("Earnings Risk", pick["earnings_status"]=="PASSED")] if not ok]
                print(f"  {BLUE}◆ {ticker} passed core gates — {', '.join(caveats)} failed, "
                      f"included as secondary pick{RESET}")

    analysis["agent_picks"]    = passed
    analysis["picks_excluded"] = excluded
    analysis["universe_size"]  = universe_size
    analysis["prefiltered_size"] = prefiltered_size

    # ── OUTCOME TRACKING — log G1-6 qualifiers regardless of G7 ────
    if g1_6_qualifying:
        try:
            import outcome_tracker
            outcome_tracker.log_new_picks(g1_6_qualifying, data)
        except Exception as e:
            print(f"{YELLOW}⚠ Outcome tracker logging error: {e} — continuing{RESET}")

    # Full pipeline summary
    full_pass_count = sum(1 for p in passed if p.get("gate7_status") == "PASSED")
    g7_flagged_count = sum(1 for p in passed if p.get("gate7_status") == "FAILED")
    deep_analysed = len(passed) + len(excluded)
    pre_removed   = universe_size - prefiltered_size if universe_size > 0 else 0

    if len(passed) == 0:
        analysis["picks_summary"] = (
            f"No picks qualify today. "
            f"Pipeline: {universe_size} stocks in universe → "
            f"{prefiltered_size} passed pre-filter (removed {pre_removed} high-beta/declining) → "
            f"{deep_analysed} deep-analysed by Claude → "
            f"0 passed Gates 1-6. "
            f"Common gate failures: {_summarise_failures(excluded)}"
        )
        print(f"  {YELLOW}⚠ No picks passed Gates 1-6{RESET}")
    else:
        analysis["picks_summary"] = (
            f"{full_pass_count} of {universe_size} stocks passed all 7 quality gates"
            + (f", plus {g7_flagged_count} more passed Gates 1-6 with a Gate 7 (52W Headroom) caveat"
               if g7_flagged_count else "") + ". "
            f"Pipeline: {universe_size} universe → {prefiltered_size} pre-filtered → "
            f"{deep_analysed} deep-analysed → {len(passed)} qualified."
        )
    return analysis

def _summarise_failures(excluded):
    """Summarise the most common gate failures across excluded picks"""
    counts = {}
    for e in excluded:
        for f in (e.get("gates_failed") or []):
            key = f["gate"] if isinstance(f, dict) else f.split(":")[0].strip()
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return "see excluded picks for details"
    top = sorted(counts.items(), key=lambda x: -x[1])[:3]
    return ", ".join([f"{k} ({v}x)" for k,v in top])

def rank_picks(analysis):
    """
    Sort picks: all-7-gates-passed first (Green section), then
    Gates-1-6-passed-but-Gate-7-failed (Blue section) — within each
    tier, highest strategy_score first.
    """
    picks = analysis.get("agent_picks", [])
    if not picks:
        return analysis
    gate7_tier = {"PASSED": 0, "FAILED": 1}
    picks.sort(key=lambda s: (
        gate7_tier.get(s.get("gate7_status", "PASSED"), 0),
        -safe_float(s.get("strategy_score", 0)),
    ))
    analysis["agent_picks"] = picks
    print(f"{BLUE}Agent Picks ranked:{RESET}")
    for i, s in enumerate(picks, 1):
        tier_label = "Gate 7 ✓" if s.get("gate7_status") == "PASSED" else "Gate 7 ✗ (G1-6 only)"
        print(f"  #{i} {s.get('ticker')} — score {s.get('strategy_score')} — {tier_label}")
    return analysis

# ── SAVE ──────────────────────────────────────────────────────
def load_regime():
    """data/market_regime.json from regime_detector.py (step 4), or {}."""
    try:
        with open(os.path.join(BASE_DIR, "data", "market_regime.json")) as f:
            return json.load(f)
    except Exception:
        return {}


def effective_cash_floor(strategy, regime=None):
    """The strategy's min_cash_pct, raised by the regime detector's own
    cash_floor_override (e.g. 25% in a bear trend) when it's higher."""
    base = (strategy or {}).get("position_sizing", {}).get("min_cash_pct", 15)
    override = ((regime or {}).get("adjustments") or {}).get("cash_floor_override")
    return max(base, override) if isinstance(override, (int, float)) else base


def _recent_trade_days(ticker, today):
    """Trading days since the last trade in ticker (trade_history.json), or None."""
    from market_calendar import trading_days_between
    trades = load_json(os.path.join(BASE_DIR, "logs", "trade_history.json"), []) or []
    dates = [t.get("trade_date") for t in trades
             if str(t.get("ticker", "")).upper() == ticker and t.get("trade_date")]
    if not dates:
        return None
    try:
        return trading_days_between(datetime.strptime(max(dates), "%Y-%m-%d").date(), today)
    except Exception:
        return None


def assign_position_actions(analysis, data, strategy, regime=None):
    """The action on each holding comes from the strategy's rules, not
    Claude's wording. Claude's label is kept as claude_signal; signal
    becomes, in priority order:
      SELL    the hard exit or the stop is hit, or Claude says SELL and the
              thesis tracker rates the thesis BROKEN
      TRIM    weight above the holding cap, or the stop tracker advises
              partial profit (a Claude SELL over the cap with the thesis
              intact is a sizing problem, so TRIM, not SELL)
      TOP-UP  in profit, below the top-up target weight, Claude's view
              intact, and every buy rule passes
      HOLD / WATCH otherwise
    action_detail says why, with a dollar amount for TRIM / TOP-UP."""
    st = strategy or {}
    ps = st.get("position_sizing", {})
    pa = st.get("position_actions", {})
    cap = safe_float(data.get("total_capital"))
    cash = safe_float(data.get("cash"))
    if cap <= 0:
        return
    max_hold = ps.get("max_existing_holding_pct", 20)
    max_new = ps.get("max_single_stock_pct", 10)
    max_sector = ps.get("max_sector_pct", 30)
    priority_exits = st.get("rebalancing", {}).get("priority_exits", [])
    target = safe_float(pa.get("top_up_target_pct"), max_new)
    cooldown = int(pa.get("top_up_cooldown_days", 10))
    min_order = safe_float(ps.get("min_order_usd"), 0)
    rsi_max = st.get("entry_rules", {}).get("rsi_max", 60)
    earn_days = st.get("pick_quality_gates", {}).get("gate5_entry_conditions", {}).get("earnings_risk_days", 7)
    hard_exit = st.get("stop_loss", {}).get("hard_exit_pct", -15)
    floor = effective_cash_floor(st, regime)
    cash_room = cash - cap * floor / 100
    today = datetime.now().date()

    stock_map = {str(s.get("ticker", "")).upper(): s for s in data.get("stocks", [])}
    sector_val = {}
    for s in data.get("stocks", []):
        if str(s.get("list_type", "")).lower() == "holding":
            sec = get_sector(s.get("ticker", ""), s.get("company_name", ""), s.get("sector", ""))
            sector_val[sec] = sector_val.get(sec, 0) + safe_float(s.get("current_value"))
    tracker = {str(s.get("ticker", "")).upper(): s for s in
               (load_json(os.path.join(BASE_DIR, "logs", "stop_status.json"), {}) or {}).get("statuses", [])}

    from reallocation_engine import risk_based_cap
    for h in analysis.get("holdings", []):
        t = str(h.get("ticker", "")).upper()
        s = stock_map.get(t, {})
        view = h.get("claude_signal") or h.get("signal") or "HOLD"
        h["claude_signal"] = view
        w = safe_float(h.get("portfolio_weight"))
        val = safe_float(h.get("current_value"))
        pnl = safe_float(h.get("pnl_pct"))
        price = safe_float(h.get("price") or s.get("price"))
        stop = safe_float(s.get("stop_loss") or h.get("stop"))
        hold_cap = max_new if t in priority_exits else max_hold
        tr_action = str((tracker.get(t) or {}).get("action", ""))

        thesis = str(h.get("thesis_status") or "").upper()
        action, detail = (view if view in ("HOLD", "WATCH") else "HOLD"), ""
        if view == "SELL" and thesis == "BROKEN":
            action, detail = "SELL", "Claude says sell and the thesis is broken"
        elif pnl <= hard_exit:
            action, detail = "SELL", f"Hard exit: {pnl:.1f}% (limit {hard_exit}%)"
        elif stop > 0 and 0 < price < stop:
            action, detail = "SELL", f"Price ${price:,.2f} below stop ${stop:,.2f}"
        elif w > hold_cap:
            action = "TRIM"
            detail = f"Trim about ${val - cap * hold_cap / 100:,.0f} to bring {w:.1f}% down to {hold_cap}%"
        elif "TRIM" in tr_action:
            action, detail = "TRIM", "Target reached: take partial profit (stop tracker)"
        elif view == "SELL":
            action = "WATCH"
            detail = f"Claude suggests selling, but the thesis is {thesis.lower() or 'not rated'}: review, not a rule exit"
        elif thesis == "BROKEN":
            action, detail = "WATCH", "Thesis rated broken: review whether to exit"
        elif view in ("STRONG BUY", "BUY", "ADD", "HOLD") and h.get("confidence") != "Low" and pnl > 0 and w < target:
            sec = h.get("sector") or get_sector(t, "", s.get("sector", ""))
            sec_room = cap * max_sector / 100 - sector_val.get(sec, 0)
            pick = {"price": price, "stop_price": stop, "atr_pct": s.get("atr_pct"),
                    "confidence": h.get("confidence")}
            risk_cap, rs = risk_based_cap(pick, cap, ps.get("risk_per_trade_pct", 1.0))
            limits = {f"the {target:g}% target": cap * target / 100 - val,
                      "cash above the floor": cash_room,
                      "sector room": sec_room,
                      f"the risk budget (stop {rs['sizing_stop_pct']:.0f}% away)": risk_cap - val}
            bind = min(limits, key=limits.get)
            amount = max(limits[bind], 0)
            rsi = s.get("rsi")
            dte = s.get("days_to_earnings")
            since = _recent_trade_days(t, today)
            blocks = []
            if cash_room <= 0:
                blocks.append(f"cash below the {floor:g}% floor")
            if rsi is not None and safe_float(rsi) > rsi_max:
                blocks.append(f"RSI {safe_float(rsi):.0f} above {rsi_max}")
            if dte is not None and 0 <= safe_float(dte) <= earn_days:
                blocks.append(f"earnings in {int(safe_float(dte))} days")
            if sec_room <= 0:
                blocks.append(f"{sec} at the {max_sector}% sector cap")
            if since is not None and since < cooldown:
                blocks.append(f"traded {since} trading days ago (wait {cooldown})")
            if not blocks and amount < max(min_order, 1):
                blocks.append(f"{bind} leaves room for only ${amount:,.0f} (minimum order ${min_order:,.0f})")
            if blocks:
                detail = "Top-up not allowed: " + "; ".join(blocks)
            else:
                action = "TOP-UP"
                detail = f"Add about ${amount:,.0f} (from {w:.1f}%; limited by {bind})"
                if rsi is None:
                    detail += "; RSI unavailable, check skipped"
        if action == "HOLD" and not detail and view in ("STRONG BUY", "BUY", "ADD"):
            why = [r for c, r in ((pnl <= 0, "position not in profit"),
                                  (w >= target, f"already {w:.1f}% (top-up target {target:g}%)"),
                                  (h.get("confidence") == "Low", "Low confidence")) if c]
            detail = f"Claude suggested {view}; top-up not allowed: " + "; ".join(why)
        if action == "TRIM" and view == "SELL":
            detail += " (Claude would sell; thesis still " + (thesis.lower() or "not rated") + ")"
        h["signal"], h["action_detail"] = action, detail


def compute_rule_alerts(data, strategy, sector_map, regime=None):
    """Strategy compliance computed from strategy.json — not left to the
    model. Previously the dashboard's Strategy Compliance panel showed only
    whatever Claude chose to list, so a 40% sector against a 30% cap could
    go unflagged for weeks."""
    st = strategy or {}
    ps = st.get("position_sizing", {})
    max_hold   = ps.get("max_existing_holding_pct", 20)
    max_sector = ps.get("max_sector_pct", 30)
    min_pos    = ps.get("min_positions", 6)
    hard_exit  = st.get("stop_loss", {}).get("hard_exit_pct", -15)
    floor      = effective_cash_floor(st, regime)
    cap        = safe_float(data.get("total_capital"))
    cash       = safe_float(data.get("cash"))
    holdings   = [x for x in data.get("stocks", []) if str(x.get("list_type", "")).lower() == "holding"
                  and safe_float(x.get("quantity")) > 0]
    alerts = []

    def add(sev, rule, msg, action, ticker=None):
        alerts.append({"severity": sev, "rule": rule, "message": msg, "action": action,
                       "ticker": ticker, "source": "rule"})

    for h in holdings:
        t = h.get("ticker")
        pnl = safe_float(h.get("pnl_pct"))
        w = safe_float(h.get("current_value")) / cap * 100 if cap > 0 else 0
        if pnl <= hard_exit:
            add("CRITICAL", f"Hard exit {hard_exit}%",
                f"{t} is {pnl:.1f}% below cost (hard exit at {hard_exit}%).", f"Exit {t}", t)
        if w > max_hold + 5:
            add("CRITICAL", f"Position > {max_hold + 5}% of capital",
                f"{t} is {w:.1f}% of total capital (max {max_hold}%).",
                f"Trim {t} by about ${(w - max_hold) / 100 * cap:,.0f} to reach {max_hold}%", t)
        elif w > max_hold:
            add("WARNING", f"Position > {max_hold}% of capital",
                f"{t} is {w:.1f}% of total capital (max {max_hold}%).",
                f"Trim {t} by about ${(w - max_hold) / 100 * cap:,.0f}", t)
    for sec, pct in (sector_map or {}).items():
        pct = safe_float(pct.get("pct") if isinstance(pct, dict) else pct)
        if pct > max_sector:
            add("WARNING", f"Sector > {max_sector}% of capital",
                f"{sec} is {pct:.1f}% of total capital (cap {max_sector}%).",
                f"No new {sec} buys; trim the largest {sec} holding")
    cash_pct = cash / cap * 100 if cap > 0 else 0
    if cash_pct < floor:
        why = f" (raised from {ps.get('min_cash_pct', 15)}% by the {regime.get('regime')} regime)" \
            if floor > ps.get("min_cash_pct", 15) and regime else ""
        add("WARNING", f"Cash below {floor}% floor",
            f"Cash is {cash_pct:.1f}% of total capital; floor is {floor}%{why}.", "No new buys until cash is above the floor")
    if 0 < len(holdings) < min_pos:
        add("INFO", f"Fewer than {min_pos} positions", f"{len(holdings)} positions held (target {min_pos}+).",
            "Consider qualifying picks for diversification")
    stale = data.get("stale_tickers") or []
    if stale:
        add("WARNING", "Stale prices", f"No fresh price for {', '.join(stale)} — values may be out of date.",
            "Check these tickers before acting")
    return alerts


def merge_alerts(rule_alerts, model_alerts):
    """Rule alerts first; the model's alerts kept unless they duplicate a rule alert for the same ticker."""
    seen = {(a.get("ticker"), a["severity"]) for a in rule_alerts if a.get("ticker")}
    out = list(rule_alerts)
    for a in model_alerts or []:
        txt = f"{a.get('message', '')} {a.get('action', '')}"
        dup = any(t and t in txt and sev == a.get("severity") for t, sev in seen)
        if not dup:
            out.append(dict(a, source=a.get("source", "model")))
    return out


def save_analysis(analysis, data, context=None, strategy=None):
    if not analysis:
        return

    now = datetime.now()
    # saved_at feeds the dashboard's getAge() "X minutes/hours ago" display,
    # which compares it against the BROWSER's clock — needs to be an
    # unambiguous UTC-aware timestamp for that comparison to be correct
    # across timezones (same bug class as the stock_insight.py / premarket
    # "stuck" issues). The display strings below are unaffected — same
    # wall-clock values either way, since the server runs in UTC.
    analysis["saved_at"]         = datetime.now(timezone.utc).isoformat()
    analysis["saved_at_display"] = now.strftime("%A %d %B %Y  %H:%M")
    analysis["saved_at_short"]   = now.strftime("%d %b %Y %H:%M")

    # fetch_data.py's fetch_performance_prices() already fetches a live price
    # for every OPEN signal in the Performance tab's log and saves it into
    # market_data.json — but that never made it into latest.json (the only
    # file the dashboard actually reads), so the Performance tab's RETURN
    # column always showed "—" regardless of how correctly the backend had
    # fetched the data.
    analysis["performance_prices"] = data.get("performance_prices", {})

    # Attach strategy alerts
    if strategy:
        analysis["strategy_profile"] = strategy.get("profile","")
        analysis["strategy_rules"]   = {
            "max_stock_pct":  strategy.get("position_sizing",{}).get("max_single_stock_pct",10),
            "max_sector_pct": strategy.get("position_sizing",{}).get("max_sector_pct",30),
            "min_cash_pct":   strategy.get("position_sizing",{}).get("min_cash_pct",20),
            "stop_loss_pct":  strategy.get("stop_loss",{}).get("default_pct",8),
        }

    # Attach portfolio summary — both nested and top-level for dashboard compatibility
    ps = {
        "total_value":   data.get("total_value", 0),
        "total_cost":    data.get("total_cost", 0),
        "total_pnl_usd": data.get("total_pnl_usd", 0),
        "total_pnl_pct": data.get("total_pnl_pct", 0),
        "cash":          data.get("cash", 0),
        "total_capital": data.get("total_capital", 0),
    }
    analysis["portfolio_summary"] = ps
    # Attach market context
    if context:
        fg = context.get("fear_greed", {})
        analysis["fear_greed"]   = fg
        analysis["earnings"]     = context.get("earnings", [])
        analysis["macro_events"] = context.get("macro_events", [])

    # Attach drawdown data so dashboard can show banner
    peak_path = os.path.join(BASE_DIR, "logs", "portfolio_peak.json")
    if os.path.exists(peak_path):
        try:
            with open(peak_path) as _pf:
                analysis["drawdown"] = json.load(_pf)
        except Exception:
            pass

    # Also at top level so dashboard can read either way
    analysis["total_value"]   = ps["total_value"]
    analysis["total_cost"]    = ps["total_cost"]
    analysis["total_pnl_usd"] = ps["total_pnl_usd"]
    analysis["total_pnl_pct"] = ps["total_pnl_pct"]
    analysis["cash"]          = ps["cash"]
    analysis["total_capital"] = ps["total_capital"]

    # Actions on holdings from the rules (TRIM / TOP-UP), before anything reads signal
    assign_position_actions(analysis, data, strategy, load_regime())

    # Health score
    health_score, health_summary = calculate_health_score(analysis, data)
    analysis["health_score"]   = health_score
    analysis["health_summary"] = health_summary
    print(f"{GREEN}✓ Health score: {health_score}/100 — {health_summary}{RESET}")

    # Sector map — use holdings from analysis + current_value from data stocks
    holdings    = analysis.get("holdings", [])
    total_value = safe_float(data.get("total_value"))
    stocks      = data.get("stocks", [])
    stock_map   = {str(s.get("ticker","")).upper(): s for s in stocks}

    enriched = []
    for h in holdings:
        t      = str(h.get("ticker","")).upper()
        s_data = stock_map.get(t, {})
        # Always use live values from fetch_data — never rely on watchlist or Claude
        h["current_value"] = safe_float(s_data.get("current_value"))
        h["sector"]        = get_sector(t, h.get("name","") or h.get("company_name",""), s_data.get("sector",""))
        h["company_name"]  = s_data.get("company_name") or h.get("name","")
        enriched.append(h)
    analysis["sector_map"] = build_sector_map(enriched, total_value)
    print(f"{GREEN}✓ Sector map: {analysis['sector_map']}{RESET}")

    # Prices on holdings directly — the simulator and dashboard used to get
    # them only via portfolio_analysis.py, a step allowed to fail.
    for h in analysis.get("holdings", []):
        s_data = stock_map.get(str(h.get("ticker", "")).upper(), {})
        if s_data.get("price"):
            h["price"]       = s_data["price"]
            h["price_as_of"] = s_data.get("price_as_of")
            h["price_stale"] = bool(s_data.get("price_stale"))
    try:
        from claude_json import usage_summary
        analysis["api_usage"] = usage_summary()
    except Exception:
        pass
    analysis["calibration"] = compute_calibration()
    try:
        from outcome_tracker import outcome_summary
        analysis["track_record"] = outcome_summary()
    except Exception as e:
        print(f"{YELLOW}⚠ Track record summary skipped: {e}{RESET}")
    analysis["data_quality"] = {
        "stale_tickers": data.get("stale_tickers", []),
        "holdings_live_coverage": data.get("holdings_live_coverage"),
        "market_data_fetched_at": data.get("fetched_at"),
    }

    # Regime (step 4) and stop tracker (step 3) — the dashboard already has
    # a Regime tile and a Position Monitor that read these keys, but nothing
    # ever put them into latest.json, so both were always blank.
    regime = load_regime()
    analysis["market_regime"] = regime
    try:
        with open(os.path.join(BASE_DIR, "logs", "stop_status.json")) as _sf:
            analysis["stop_tracker"] = json.load(_sf).get("statuses", [])
    except Exception:
        analysis["stop_tracker"] = []

    # Compliance from rules, then the model's own alerts
    # Sector weights against TOTAL CAPITAL (the strategy's definition, and
    # what the pick gates use) — sector_map above is % of holdings value.
    cap = safe_float(data.get("total_capital"))
    sector_pct_map = {}
    for h in enriched:
        sector_pct_map[h["sector"]] = sector_pct_map.get(h["sector"], 0) + safe_float(h.get("current_value"))
    sector_pct_map = {k: round(v / cap * 100, 1) for k, v in sector_pct_map.items()} if cap > 0 else {}
    analysis["sector_pct_of_capital"] = sector_pct_map
    rule_alerts = compute_rule_alerts(data, strategy, sector_pct_map, regime)
    analysis["strategy_alerts"] = merge_alerts(rule_alerts, analysis.get("strategy_alerts", []))
    if rule_alerts:
        print(f"{YELLOW}⚠ Rule-based compliance: {len(rule_alerts)} alert(s) — "
              f"{', '.join(a['rule'] for a in rule_alerts[:5])}{RESET}")

    # Signal history
    analysis["signal_history"] = update_signal_history(analysis)

    os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
    ts   = now.strftime("%Y%m%d_%H%M")
    path = os.path.join(BASE_DIR, "logs", f"analysis_{ts}.json")
    with open(path,"w") as f:
        json.dump(analysis, f, indent=2)
    print(f"{GREEN}✓ Saved: {path}{RESET}")

    latest = os.path.join(BASE_DIR, "logs", "latest.json")
    with open(latest + ".tmp", "w") as f:
        json.dump(analysis, f, indent=2)
    os.replace(latest + ".tmp", latest)  # atomic — the dashboard never reads a half-written file
    print(f"{GREEN}✓ latest.json — {analysis['saved_at_display']}{RESET}")

# ── PRINT RESULTS ─────────────────────────────────────────────
def print_analysis(analysis):
    if not analysis:
        return
    mc = {"BULLISH":GREEN,"BEARISH":RED,"NEUTRAL":YELLOW}
    mood = analysis.get("market_mood","NEUTRAL")
    print(f"\n{BOLD}{BLUE}{'='*55}")
    print(f"  AGENT ANALYSIS — {analysis.get('analysis_date')}")
    print(f"{'='*55}{RESET}")
    print(f"Mood:      {mc.get(mood,YELLOW)}{mood}{RESET}")
    print(f"Portfolio: {analysis.get('portfolio_health')}")
    print(f"Action:    {YELLOW}{analysis.get('top_action_today')}{RESET}")

    ps = analysis.get("portfolio_summary",{})
    print(f"\n{BOLD}Portfolio:{RESET}")
    print(f"  Value:  ${safe_float(ps.get('total_value')):,.2f}  |  "
          f"P&L: ${safe_float(ps.get('total_pnl_usd')):+,.2f} "
          f"({safe_float(ps.get('total_pnl_pct')):+.2f}%)")
    print(f"  Cash:   ${safe_float(ps.get('cash')):,.2f}  |  "
          f"Total Capital: ${safe_float(ps.get('total_capital')):,.2f}")

    for section, label in [("holdings","HOLDINGS"),("interested","INTERESTED"),("agent_picks","PICKS")]:
        items = analysis.get(section,[])
        if not items:
            continue
        print(f"\n{BOLD}{'─'*40}\n  {label}\n{'─'*40}{RESET}")
        for s in items:
            pnl = safe_float(s.get("pnl_pct"))
            pnl_usd = safe_float(s.get("pnl_usd"))
            wt  = s.get("portfolio_weight","")
            pnl_str = f" | P&L: {pnl:+.2f}% (${pnl_usd:+,.2f})" if pnl != 0 else ""
            wt_str  = f" | Weight: {wt}%" if wt else ""
            curr    = s.get("current_price","")
            cp_str  = f" | Live: ${curr}" if curr else ""
            gate7_tag = ""
            if section == "agent_picks":
                gate7_tag = "" if s.get("gate7_status") == "PASSED" else f" {YELLOW}[G1-6 only — G7 failed]{RESET}"
            print(f"\n  {BOLD}{s.get('ticker')}{RESET} — {s.get('signal')} ({s.get('confidence')}){pnl_str}{wt_str}{cp_str}{gate7_tag}")
            print(f"  {s.get('reason','')}")
            if s.get("suggested_entry"):
                print(f"  {GREEN}Entry: {s.get('suggested_entry')} | {s.get('upside_potential','')}{RESET}")
    print(f"\n{BOLD}{BLUE}{'='*55}{RESET}\n")

# ── MAIN ──────────────────────────────────────────────────────
def run():
    print(f"\n{BOLD}{BLUE}{'='*55}")
    print(f"  MARKET AGENT V4.5 — Brain")
    print(f"  {datetime.now().strftime('%A %d %B %Y  %H:%M')}")
    print(f"{'='*55}{RESET}\n")

    data = load_market_data()
    if not data:
        return

    holdings, interested = split_stocks(data)
    print(f"{BLUE}Holdings: {len(holdings)} | Interested: {len(interested)} | "
          f"Cash: ${safe_float(data.get('cash')):,.0f}{RESET}")

    # Step 2.5: Load market context and strategy first (needed for universe build)
    stage(1, "Loading market context + candidate universe")
    from market_context import fetch_all_context
    all_tickers_ctx = [s.get("ticker","") for s in data.get("stocks",[])]
    # Reuse step 6's market_context.json when it's under an hour old and
    # covers these tickers — previously this always fetched everything again.
    context = None
    try:
        with open(os.path.join(BASE_DIR, "data", "market_context.json")) as f:
            cached = json.load(f)
        age_min = (datetime.now() - datetime.fromisoformat(cached.get("fetched_at", "2000-01-01"))).total_seconds() / 60
        if age_min < 60 and cached.get("fear_greed"):
            context = cached
            print(f"{BLUE}Using market context from step 6 ({age_min:.0f} min old){RESET}")
    except Exception:
        pass
    if context is None:
        context = fetch_all_context(all_tickers_ctx)
    strategy = load_strategy()

    # Step 1: Build sector-based candidate universe
    pick_tickers, candidate_sector_map = get_pick_tickers(holdings, interested, strategy)
    universe_size = len(pick_tickers)

    # Step 2: Fetch live data + pre-filter candidates
    etf_tickers = candidate_sector_map.get("ETFs & Commodities", [])
    if pick_tickers:
        picks_data, pre_excluded = fetch_pick_prices(pick_tickers, strategy, etf_tickers=etf_tickers)
    else:
        picks_data, pre_excluded = [], []
    prefiltered_size = len(picks_data)

    # Step 3: Full analysis
    stage(3, f"Claude main analysis ({prefiltered_size} candidates) — usually a few minutes")
    analysis = run_agent(holdings, interested, picks_data, data, context, strategy)

    if analysis:
        analysis = enrich_holdings(analysis, data)

        # Apply pick quality gates — hard enforcement layer
        total_capital = safe_float(data.get("total_capital", 0))
        sector_map_raw = {}
        for h in holdings:
            sec = get_sector(h.get("ticker",""), h.get("name","") or h.get("company_name",""), h.get("sector",""))
            val = safe_float(h.get("current_value", 0))
            sector_map_raw[sec] = sector_map_raw.get(sec, 0) + val
        sector_pcts   = {k: round((v/total_capital)*100, 1) for k,v in sector_map_raw.items()} if total_capital > 0 else {}
        max_sector    = strategy.get("position_sizing", {}).get("max_sector_pct", 30) if strategy else 30
        capped_sectors= [s for s,p in sector_pcts.items() if p >= max_sector - 5]
        print(f"\n{BLUE}Applying Pick Quality Gates...{RESET}")
        stage(4, "Applying pick quality gates")

        # ── LIVE ENRICH: fetch market data for any pick not already in stock_map ──
        # Claude analyses stocks from a universe that may not all be in market_data.json
        # For picks missing from stock_map, fetch their key gate fields live from yfinance
        existing_stocks = {str(s.get("ticker","")).upper() for s in data.get("stocks", [])}
        picks_needing_data = [
            p.get("ticker","").upper()
            for p in analysis.get("agent_picks", [])
            if p.get("ticker","").upper() not in existing_stocks
        ]
        if picks_needing_data:
            print(f"{BLUE}  Fetching live gate data for {len(picks_needing_data)} picks not in market_data: {picks_needing_data}{RESET}")
            import yfinance as yf
            for t in picks_needing_data:
                try:
                    info = yf.Ticker(t).info
                    price = safe_float(info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose"))
                    w52_hi = safe_float(info.get("fiftyTwoWeekHigh"))
                    from technicals import compute_technicals
                    tech = compute_technicals(t)
                    dte_val = None
                    ne = info.get("earningsTimestamp") or info.get("earningsTimestampStart")
                    if ne:
                        from datetime import datetime as _dt
                        dte_val = (_dt.fromtimestamp(ne) - _dt.now()).days
                        if dte_val < 0:
                            # Yahoo keeps the LAST report's timestamp until the next
                            # date is announced — a past report is not upcoming risk.
                            dte_val = None
                    sector_val = info.get("sector") or info.get("industry") or ""
                    live_entry = {
                        "ticker":           t,
                        "price":            round(price, 4) if price else None,
                        "52w_high":         round(w52_hi, 4) if w52_hi else None,
                        "days_to_earnings": dte_val,
                        "sector":           sector_val,
                        "rsi":              tech["rsi"],
                        "sma_50":           tech["sma_50"],
                        "sma_200":          tech["sma_200"],
                        "atr_14":           tech.get("atr_14"),
                        "atr_pct":          tech.get("atr_pct"),
                    }
                    data.setdefault("stocks", []).append(live_entry)
                    print(f"  {GREEN}✓ {t}: price=${price:.2f} 52wH=${w52_hi:.2f} sector={sector_val}{RESET}")
                except Exception as e:
                    print(f"  {YELLOW}⚠ Could not fetch live data for {t}: {e}{RESET}")

        # ── Analyst consensus target price — for Gate 7's upside check ──
        # Claude's own target_price is generated in the same breath as the
        # recommendation itself — there's no independent validation of it,
        # unlike the 52-week high (a real, externally-recorded market fact).
        # Fetching the actual analyst consensus (yfinance's targetMeanPrice
        # — an aggregate of real Wall Street price targets) gives Gate 7's
        # soft path a genuinely external anchor instead of leaning on a
        # number the same model that picked the stock also invented.
        # Small, cheap loop — agent_picks is always tiny (a handful of
        # stocks), never the full universe.
        pick_tickers_for_analyst = [p.get("ticker","").upper() for p in analysis.get("agent_picks", []) if p.get("ticker")]
        if pick_tickers_for_analyst:
            import yfinance as yf
            for p in analysis["agent_picks"]:
                t = p.get("ticker","").upper()
                if not t:
                    continue
                try:
                    info = yf.Ticker(t).info
                    analyst_target = safe_float(info.get("targetMeanPrice"))
                    analyst_count  = info.get("numberOfAnalystOpinions")
                    if analyst_target > 0:
                        p["analyst_target_price"] = round(analyst_target, 2)
                        p["analyst_target_count"] = analyst_count
                        print(f"  {GREEN}✓ {t}: analyst consensus target ${analyst_target:.2f}"
                              f"{f' ({analyst_count} analysts)' if analyst_count else ''}{RESET}")
                    else:
                        print(f"  {YELLOW}⚠ {t}: no analyst consensus target available — "
                              f"Gate 7 will fall back to Claude's own target{RESET}")
                except Exception as e:
                    print(f"  {YELLOW}⚠ Could not fetch analyst target for {t}: {e}{RESET}")

        # Fear & Greed lives in the market context, not market_data.json —
        # previously the gate read data["fear_greed"], which never existed,
        # so it always saw the default 50.
        if context and isinstance(context.get("fear_greed"), dict):
            data["fear_greed"] = context["fear_greed"]
        analysis = apply_pick_quality_gates(analysis, data, strategy, sector_pcts, capped_sectors, universe_size, prefiltered_size, pre_excluded)
        passed  = len(analysis.get("agent_picks", []))
        excluded = len(analysis.get("picks_excluded", []))
        print(f"{GREEN}✓ Gates complete: {passed} picks passed, {excluded} excluded from {universe_size} universe{RESET}")

        analysis = rank_picks(analysis)
        add_decision_fields(analysis, strategy)
        update_pick_book(analysis)

        # ── ADVERSARIAL SELF-CRITIQUE ───────────────────────────
        # Separate API call (fresh context, not a continuation of the
        # bull-case call above) argues the strongest bear case against
        # every pick that passed all 7 gates. Attaches bear_case metadata
        # — does not filter or remove picks.
        print(f"\n{BLUE}Running Adversarial Self-Critique...{RESET}")
        stage(5, "Claude bear-case critique")
        try:
            import adversarial_critique
            analysis = adversarial_critique.run(analysis, data)
        except Exception as e:
            print(f"{YELLOW}⚠ Adversarial critique error: {e} — continuing without bear-case data{RESET}")

        # ── CORRELATION-AWARE SIZING ────────────────────────────
        # Attaches pick["correlation"] (daily-return correlation vs current
        # holdings + suggested size multiplier) BEFORE the reallocation
        # engine runs, so it can read the multiplier when computing
        # suggested position sizes.
        print(f"\n{BLUE}Running Correlation Analysis...{RESET}")
        stage(6, "Correlation + reallocation")
        try:
            import correlation_engine
            analysis = correlation_engine.run(analysis, data)
        except Exception as e:
            print(f"{YELLOW}⚠ Correlation engine error: {e} — continuing without correlation data{RESET}")

        # ── REALLOCATION ENGINE ───────────────────────────────
        print(f"\n{BLUE}Running Capital Reallocation Engine...{RESET}")
        try:
            import reallocation_engine
            analysis = reallocation_engine.run(analysis, data)
        except Exception as e:
            print(f"{YELLOW}⚠ Reallocation engine error: {e} — continuing without reallocation{RESET}")
            analysis["reallocation"] = []

        # ── THESIS DRIFT TRACKING ──────────────────────────────
        print(f"\n{BLUE}Running Thesis Drift Tracking...{RESET}")
        stage(7, "Claude thesis + outcome review")
        try:
            import thesis_tracker
            analysis = thesis_tracker.run(analysis, data)
        except Exception as e:
            print(f"{YELLOW}⚠ Thesis tracker error: {e} — continuing without thesis drift data{RESET}")

        # ── OUTCOME TRACKING / POST-MORTEM ─────────────────────
        # Syncs holdings into outcome_log (create + exit detection),
        # then grades any 30/60/90-day checkpoint that's come due —
        # covers both current holdings and G1-6 agent picks logged
        # earlier this run inside apply_pick_quality_gates().
        print(f"\n{BLUE}Running Outcome Tracking...{RESET}")
        try:
            import outcome_tracker
            analysis = outcome_tracker.run(analysis, data)
        except Exception as e:
            print(f"{YELLOW}⚠ Outcome tracker error: {e} — continuing without outcome data{RESET}")

        # ── DELTA ENGINE ────────────────────────────────────────
        # MUST run before save_analysis() — it reads the CURRENT
        # logs/latest.json as the "previous run" snapshot, which
        # save_analysis() is about to overwrite.
        print(f"\n{BLUE}Computing Delta vs Previous Run...{RESET}")
        stage(8, "Saving results")
        try:
            import delta_engine
            analysis = delta_engine.compute(analysis, data)
        except Exception as e:
            print(f"{YELLOW}⚠ Delta engine error: {e} — continuing without delta data{RESET}")

        # NOTE: save_analysis() populates analysis["portfolio_summary"] (and the
        # top-level total_value/cash/total_capital mirrors) — it must run BEFORE
        # print_analysis(), which reads those same fields for the console summary.
        # Previously this was reversed, so the console always showed $0.00 for
        # Value/Cash/Total Capital even though the saved latest.json was correct.
        save_analysis(analysis, data, context, strategy)
        print_analysis(analysis)
        return analysis
    else:
        print(f"{RED}✗ Analysis failed — Claude API error or empty response{RESET}")
        sys.exit(1)

if __name__ == "__main__":
    run()
