#!/usr/bin/env python3
# ============================================================
# Market Agent V4.5 — Pre-Market Scanner
# premarket_scanner.py
#
# 1. Market holiday / weekend guard (--check-only exits 2)
# 2. Fetches pre-market price data for all tickers
# 3. Validates existing agent recommendations vs pre-market
# 4. Scans sector leader universe for new opportunities
# 5. Calls Claude API to classify meaningful movers
# 6. Saves results to logs/premarket_brief.json
#
# Reads:  data/market_data.json       (watchlist prices)
#         data/latest.json            (yesterday's recommendations)
#         watchlist.csv               (holdings + interested)
#         strategy.json               (thresholds)
# Writes: logs/premarket_brief.json   (brief data for premarket_brief.py)
# ============================================================

import sys
import json
import os
from datetime import datetime, date, timezone
import time

import yfinance as yf
import pandas as pd
from dotenv import load_dotenv
import anthropic
from claude_json import ask_json, StructuredReplyError

load_dotenv()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ── SECTOR LEADER UNIVERSE ─────────────────────────────────────
# Top 2-3 liquid large-caps per sector — stable reference list
# Broad-market benchmarks — NOT individual stock picks, kept as an
# explicit, deliberate exception. SPY/QQQ/GLD track the S&P 500,
# Nasdaq 100, and gold respectively; they exist to gauge overall market
# mood, not to recommend a specific company. Every OTHER sector below
# used to be a hardcoded list of individual stocks — that's now loaded
# dynamically instead, see get_sector_leaders() below.
BENCHMARK_TICKERS = {"ETFs & Commodities": ["SPY", "QQQ", "GLD"]}

SECTOR_LEADERS_TOP_N = 3  # top-scoring stocks pulled per sector

def load_dynamic_sector_leaders(top_n=SECTOR_LEADERS_TOP_N):
    """
    Replaces what used to be a hardcoded list of 2-5 individual stocks
    per sector. Reads data/universe_master.json — the same live
    NYSE/NASDAQ universe refresh_universe.py already builds with zero
    hardcoded tickers — groups by sector, and takes the top-scoring
    stocks in each. Updates automatically every time the universe
    refreshes, with no code change ever needed to reflect a new
    "leader" as market conditions shift.

    Returns {} (not a crash) if the universe file is missing or
    unreadable — the scanner still runs, just without dynamic sector
    coverage for that one run, falling back to only the benchmark
    tickers rather than any hardcoded stock list.
    """
    path = os.path.join(BASE_DIR, "data", "universe_master.json")
    try:
        with open(path) as f:
            universe = json.load(f)
    except Exception as e:
        print(f"{YELLOW}⚠ Could not load universe_master.json for sector leaders ({e}) — "
              f"scanning benchmarks only this run{RESET}")
        return {}

    by_sector = {}
    for stock in universe.get("stocks", []):
        sector = stock.get("sector")
        ticker = stock.get("ticker")
        score  = stock.get("quality_score", 0)
        if not sector or not ticker:
            continue
        by_sector.setdefault(sector, []).append((ticker, score))

    leaders = {}
    for sector, entries in by_sector.items():
        entries.sort(key=lambda x: x[1], reverse=True)
        leaders[sector] = [t for t, _ in entries[:top_n]]
    return leaders


def get_sector_leaders():
    """Combines the dynamically-loaded sector leaders with the small,
    explicit benchmark exception — this is the dict actually passed
    around the rest of the scanner, matching the exact shape the old
    hardcoded SECTOR_LEADERS constant used to have."""
    leaders = load_dynamic_sector_leaders()
    leaders.update(BENCHMARK_TICKERS)
    return leaders

# Thresholds
VALIDATE_THRESHOLD  = 2.0   # % move that triggers recommendation validation
OPPORTUNITY_THRESHOLD = 3.0  # % move that triggers opportunity scan

# US market holidays — computed from NYSE's rules (L7), never runs out
from market_calendar import holiday_list as _holiday_list
from common import GREEN, RED, YELLOW, BLUE, BOLD, RESET, safe_float
MARKET_HOLIDAYS = set(_holiday_list(date.today().year - 1, date.today().year + 2))


# ── MARKET CALENDAR CHECK ──────────────────────────────────────
def is_market_open_today():
    """Returns (is_open, reason_string)"""
    today = date.today()
    weekday = today.weekday()  # 0=Mon, 6=Sun
    today_str = today.strftime("%Y-%m-%d")

    if weekday >= 5:
        day_name = "Saturday" if weekday == 5 else "Sunday"
        return False, f"{day_name} — US markets closed on weekends"

    if today_str in MARKET_HOLIDAYS:
        return False, f"{today_str} is a US market holiday"

    return True, ""


# ── LOAD WATCHLIST ─────────────────────────────────────────────
def load_watchlist():
    path = os.path.join(BASE_DIR, "watchlist.csv")
    try:
        df = pd.read_csv(path)
        df["list_type"] = df["list_type"].fillna("interested").str.lower().str.strip()
        for col in ["quantity", "buy_price", "target", "stop_loss"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        holdings  = df[df["list_type"] == "holding"].to_dict("records")
        interested = df[df["list_type"] == "interested"].to_dict("records")
        return holdings, interested
    except Exception as e:
        print(f"{RED}✗ Could not load watchlist: {e}{RESET}")
        return [], []


# ── LOAD LATEST RECOMMENDATIONS ───────────────────────────────
def load_latest_recs():
    path = os.path.join(BASE_DIR, "logs", "latest.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        # Build ticker → rec map from holdings and picks
        rec_map = {}
        # Stop/target live in the watchlist (market_data.json), not in
        # Claude's holdings output. The signal key is "signal" — reading a
        # non-existent "action" key made every holding show as HOLD in the
        # pre-market brief, even ones the main run had flagged SELL.
        try:
            with open(os.path.join(BASE_DIR, "data", "market_data.json")) as mf:
                wl = {str(x.get("ticker", "")).upper(): x for x in json.load(mf).get("stocks", [])}
        except Exception:
            wl = {}
        for h in data.get("holdings", []):
            tkr = str(h.get("ticker", "")).upper()
            if tkr:
                w = wl.get(tkr, {})
                rec_map[tkr] = {
                    "action":     h.get("signal") or h.get("action") or "HOLD",
                    "stop_loss":  safe_float(w.get("stop_loss") or h.get("stop_loss")),
                    "target":     safe_float(w.get("target") or h.get("target")),
                    "thesis":     h.get("reason") or h.get("thesis", ""),
                    "health":     h.get("health_score", 0),
                    "type":       "holding",
                }
        for p in data.get("agent_picks", []):
            tkr = str(p.get("ticker", "")).upper()
            if tkr:
                rec_map[tkr] = {
                    "action":      p.get("signal", "WATCH"),
                    "limit_price": safe_float(p.get("limit_price")),
                    "target":      safe_float(p.get("target_price") or p.get("target")),
                    "thesis":      p.get("reason") or p.get("thesis", ""),
                    "type":        "pick",
                }
        return rec_map
    except Exception as e:
        print(f"{YELLOW}⚠ Could not load latest.json: {e}{RESET}")
        return {}


# ── LOAD STRATEGY ──────────────────────────────────────────────
def load_strategy():
    path = os.path.join(BASE_DIR, "strategy.json")
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


# ── FETCH PRE-MARKET PRICE ─────────────────────────────────────
def fetch_premarket(ticker):
    """Fetch pre-market price and % change vs prior close."""
    try:
        stock = yf.Ticker(ticker)
        info  = stock.info

        prev_close = safe_float(
            info.get("regularMarketPreviousClose") or
            info.get("previousClose") or 0
        )

        # Pre-market fields
        pm_price  = safe_float(info.get("preMarketPrice") or 0)
        pm_change = safe_float(info.get("preMarketChangePercent") or 0)
        pm_time   = info.get("preMarketTime")

        # Prefer deriving pm_change directly from pm_price and prev_close
        # — unambiguous and always correct, unlike preMarketChangePercent,
        # which yfinance sometimes returns as a fraction (0.0119) and
        # sometimes already as a percent (0.742), with no reliable way to
        # tell which from the raw number alone. The old approach guessed
        # "if abs < 1.0, assume a fraction and multiply by 100" — this
        # broke for genuine small moves already reported in percent form
        # (a real 0.74% move became a fabricated 74%), which is exactly
        # what happened to GLD and SPY in a real pre-market brief.
        if pm_price > 0 and prev_close > 0:
            pm_change = round(((pm_price - prev_close) / prev_close) * 100, 2)
        else:
            # No reliable price data to derive from — fall back to the
            # API's own field, still applying the old heuristic as a
            # last resort, since a guessed number beats nothing at all
            # when real prices aren't available.
            if pm_change != 0 and abs(pm_change) < 1.0:
                pm_change = round(pm_change * 100, 2)
            else:
                pm_change = round(pm_change, 2)

        # Volume proxy — pre-market volume often in regularMarketVolume before open
        volume     = int(safe_float(info.get("preMarketVolume") or
                                    info.get("regularMarketVolume") or 0))
        avg_vol    = int(safe_float(info.get("averageVolume") or 1))
        vol_ratio  = round(volume / avg_vol, 2) if avg_vol > 0 and volume > 0 else None

        # Timestamp
        if pm_time:
            try:
                pm_time_str = datetime.fromtimestamp(pm_time).strftime("%H:%M ET")
            except Exception:
                pm_time_str = None
        else:
            pm_time_str = None

        return {
            "ticker":        ticker,
            "prev_close":    round(prev_close, 2),
            "pm_price":      round(pm_price, 2) if pm_price > 0 else None,
            "pm_change_pct": pm_change,
            "pm_time":       pm_time_str,
            "vol_ratio":     vol_ratio,
            "available":     pm_price > 0,
        }

    except Exception as e:
        print(f"{RED}  ✗ {ticker}: {e}{RESET}")
        return {
            "ticker":        ticker,
            "prev_close":    0,
            "pm_price":      None,
            "pm_change_pct": 0,
            "pm_time":       None,
            "vol_ratio":     None,
            "available":     False,
        }


# ── BUILD FULL TICKER UNIVERSE ─────────────────────────────────
def build_universe(holdings, interested, sector_leaders):
    """Merge holdings + interested + sector leaders, deduplicated."""
    tickers = set()

    for h in holdings:
        t = str(h.get("ticker", "")).upper().strip()
        if t:
            tickers.add(t)

    for s in interested:
        t = str(s.get("ticker", "")).upper().strip()
        if t:
            tickers.add(t)

    for sector, leaders in sector_leaders.items():
        for t in leaders:
            tickers.add(t.upper())

    return sorted(tickers)


# ── VALIDATE EXISTING RECOMMENDATIONS ─────────────────────────
def validate_recommendations(rec_map, pm_data_map):
    """
    Compare pre-market moves against yesterday's recommendations.
    Returns list of validated/revised/withdrawn/urgent items.
    """
    results = []

    for ticker, rec in rec_map.items():
        pm = pm_data_map.get(ticker)
        if not pm or not pm["available"]:
            continue

        chg  = pm["pm_change_pct"]
        action = rec.get("action", "HOLD").upper()

        # ── BUY / STRONG BUY recommendation validation
        if action in ("BUY", "STRONG BUY", "ADD"):
            if chg >= VALIDATE_THRESHOLD:
                # Gapped UP — chasing risk, revise or defer
                new_limit = round(pm["pm_price"] * 1.005, 2) if pm["pm_price"] else None
                results.append({
                    "ticker":    ticker,
                    "status":    "REVISED",
                    "action":    action,
                    "pm_change": chg,
                    "pm_price":  pm["pm_price"],
                    "message":   f"Gapped up {chg:+.1f}% pre-market. Original buy thesis intact but entry elevated. Adjust limit to ${new_limit} or wait for intraday pullback.",
                    "revised_limit": new_limit,
                    "icon":      "⚠️",
                })
            elif chg <= -VALIDATE_THRESHOLD:
                # Gapped DOWN — better entry opportunity
                new_limit = round(pm["pm_price"] * 1.002, 2) if pm["pm_price"] else None
                results.append({
                    "ticker":    ticker,
                    "status":    "OPPORTUNITY",
                    "action":    action,
                    "pm_change": chg,
                    "pm_price":  pm["pm_price"],
                    "message":   f"Gapped down {chg:+.1f}% pre-market — improved entry vs yesterday's recommendation. Revised limit ${new_limit}.",
                    "revised_limit": new_limit,
                    "icon":      "⚡",
                })
            else:
                # Move within noise — confirm original recommendation
                results.append({
                    "ticker":    ticker,
                    "status":    "CONFIRMED",
                    "action":    action,
                    "pm_change": chg,
                    "pm_price":  pm["pm_price"],
                    "message":   f"Pre-market flat ({chg:+.1f}%). Yesterday's recommendation stands. Original limit price unchanged.",
                    "icon":      "✅",
                })

        # ── STOP LOSS / SELL recommendation validation
        elif action in ("SELL", "STOP", "EXIT", "DIVEST"):
            if chg >= VALIDATE_THRESHOLD:
                # Recovered overnight — stop may no longer be valid
                results.append({
                    "ticker":    ticker,
                    "status":    "WITHDRAWN",
                    "action":    action,
                    "pm_change": chg,
                    "pm_price":  pm["pm_price"],
                    "message":   f"Stop loss recommendation WITHDRAWN. Stock recovered {chg:+.1f}% overnight. Re-evaluate at market open before acting.",
                    "icon":      "⚠️",
                })
            elif chg <= -VALIDATE_THRESHOLD:
                # Worsened — confirms exit urgency
                results.append({
                    "ticker":    ticker,
                    "status":    "URGENT",
                    "action":    action,
                    "pm_change": chg,
                    "pm_price":  pm["pm_price"],
                    "message":   f"Stop loss CONFIRMED URGENT. Gap down {chg:+.1f}% pre-market worsens position. Exit at open. Suggested limit sell ${pm['pm_price']}.",
                    "icon":      "🚨",
                })
            else:
                # Within noise — confirm stop recommendation
                results.append({
                    "ticker":    ticker,
                    "status":    "CONFIRMED",
                    "action":    action,
                    "pm_change": chg,
                    "pm_price":  pm["pm_price"],
                    "message":   f"Stop loss recommendation confirmed. Pre-market move {chg:+.1f}% — no recovery. Proceed with planned exit at open.",
                    "icon":      "✅",
                })

        # ── HOLD / WATCH — always report status; flag prominently
        # only if the move is significant. Previously this branch only
        # fired when abs(chg) >= VALIDATE_THRESHOLD, meaning a HOLD-type
        # recommendation sitting quiet got NO status entry at all —
        # completely invisible in the brief, unlike BUY/SELL actions,
        # which always get at least a quiet "CONFIRMED" line above.
        else:
            if abs(chg) >= VALIDATE_THRESHOLD:
                direction = "up" if chg > 0 else "down"
                results.append({
                    "ticker":    ticker,
                    "status":    "WATCH",
                    "action":    action,
                    "pm_change": chg,
                    "pm_price":  pm["pm_price"],
                    "message":   f"Notable pre-market move {chg:+.1f}% ({direction}). No active recommendation — monitor at open for entry/exit opportunity.",
                    "icon":      "👁",
                })
            else:
                results.append({
                    "ticker":    ticker,
                    "status":    "CONFIRMED",
                    "action":    action,
                    "pm_change": chg,
                    "pm_price":  pm["pm_price"],
                    "message":   f"Pre-market flat ({chg:+.1f}%). No action needed — holding steady.",
                    "icon":      "✅",
                })

    return results


# ── SCAN FOR NEW OPPORTUNITIES ─────────────────────────────────
def scan_opportunities(pm_data_map, rec_map, holdings, interested, sector_leaders):
    """
    Find sector leader movers with no existing recommendation.
    Returns tickers worth sending to Claude for classification.
    """
    # Build set of tickers already in rec_map
    known = set(rec_map.keys())

    # Build set of holding tickers (different handling)
    holding_tickers = {str(h.get("ticker","")).upper() for h in holdings}
    interested_tickers = {str(s.get("ticker","")).upper() for s in interested}

    candidates = []

    for ticker, pm in pm_data_map.items():
        if not pm["available"]:
            continue
        if ticker in known:
            continue  # Already handled in validation

        chg = pm["pm_change_pct"]
        if abs(chg) < OPPORTUNITY_THRESHOLD:
            continue  # Below opportunity threshold

        # Determine which sector this leader belongs to
        sector = "Unknown"
        for s, leaders in sector_leaders.items():
            if ticker in leaders:
                sector = s
                break

        vol_signal = "HIGH" if (pm["vol_ratio"] or 0) > 1.5 else \
                     "NORMAL" if (pm["vol_ratio"] or 0) > 0.5 else "LOW"

        candidates.append({
            "ticker":     ticker,
            "sector":     sector,
            "pm_change":  chg,
            "pm_price":   pm["pm_price"],
            "prev_close": pm["prev_close"],
            "pm_time":    pm["pm_time"],
            "vol_signal": vol_signal,
            "vol_ratio":  pm["vol_ratio"],
            "in_interested": ticker in interested_tickers,
            "is_holding":    ticker in holding_tickers,
        })

    # Sort by absolute move size, largest first
    candidates.sort(key=lambda x: abs(x["pm_change"]), reverse=True)

    return candidates


# ── CLAUDE ANALYSIS OF PRE-MARKET MOVERS ──────────────────────
def analyse_with_claude(validated_items, opportunity_candidates, regime=None):
    """
    Send meaningful movers to Claude for classification and action recommendation.
    Returns enriched results dict.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print(f"{RED}✗ ANTHROPIC_API_KEY not set{RESET}")
        return None

    client = anthropic.Anthropic(api_key=api_key)

    # Load regime from latest analysis if available and not passed
    if not regime:
        try:
            latest_path = os.path.join(BASE_DIR, "logs", "latest.json")
            with open(latest_path) as f:
                latest = json.load(f)
            regime = (latest.get("market_regime") or {}).get("regime") or "UNKNOWN"
        except Exception:
            regime = "UNKNOWN"

    # Build context for Claude
    urgent_items    = [x for x in validated_items if x["status"] == "URGENT"]
    withdrawn_items = [x for x in validated_items if x["status"] == "WITHDRAWN"]
    revised_items   = [x for x in validated_items if x["status"] == "REVISED"]
    watch_items     = [x for x in validated_items if x["status"] == "WATCH"]

    prompt = f"""You are the pre-market intelligence engine for a personal US equity portfolio.
Market regime today: {regime}
Current time: Pre-market (~1 hour before NYSE open)

=== VALIDATED RECOMMENDATIONS ===
These are yesterday's agent recommendations checked against today's pre-market prices.

URGENT (stop loss confirmed by gap down):
{json.dumps(urgent_items, indent=2) if urgent_items else "None"}

WITHDRAWN (stop loss invalidated by recovery):
{json.dumps(withdrawn_items, indent=2) if withdrawn_items else "None"}

REVISED (buy rec affected by gap):
{json.dumps(revised_items, indent=2) if revised_items else "None"}

NOTABLE HOLDS/WATCHES with significant moves:
{json.dumps(watch_items, indent=2) if watch_items else "None"}

=== NEW OPPORTUNITY CANDIDATES ===
Sector leaders moving >{OPPORTUNITY_THRESHOLD}% pre-market with no existing recommendation:
{json.dumps(opportunity_candidates[:8], indent=2) if opportunity_candidates else "None"}

=== YOUR TASK ===
1. For each URGENT item: confirm or refine the exit instruction. Give a specific limit sell price.
2. For each WITHDRAWN item: confirm withdrawal and say what to watch for at open.
3. For each REVISED item: give the adjusted limit buy price and whether to act pre-market or wait for open.
4. For each opportunity candidate: classify as OPPORTUNITY, NOISE, or WATCH.
   - OPPORTUNITY: move is meaningful, thesis exists, action is clear
   - WATCH: move is notable but wait for more data at open
   - NOISE: sector drag, pre-market thin liquidity, no real signal
   For OPPORTUNITY only: provide suggested action, limit price, and 1-sentence rationale.

Respond ONLY with this JSON structure, no preamble:
{{
  "regime": "{regime}",
  "analysis_time": "{datetime.now().strftime('%Y-%m-%d %H:%M')}",
  "urgent": [
    {{"ticker": "X", "instruction": "...", "limit_price": 0.00, "rationale": "1 sentence"}}
  ],
  "withdrawn": [
    {{"ticker": "X", "instruction": "...", "watch_for": "..."}}
  ],
  "revised": [
    {{"ticker": "X", "revised_limit": 0.00, "act_now": true/false, "rationale": "1 sentence"}}
  ],
  "opportunities": [
    {{"ticker": "X", "classification": "OPPORTUNITY/WATCH/NOISE", "action": "BUY/WAIT/IGNORE", "limit_price": 0.00, "rationale": "1 sentence"}}
  ],
  "summary": "2-sentence overall pre-market summary"
}}"""

    print(f"{BLUE}  Calling Claude API for pre-market analysis...{RESET}")

    for attempt in range(3):
        try:
            result = ask_json(client, prompt, max_tokens=2000, label="premarket_scanner")
            print(f"{GREEN}  ✓ Claude analysis complete{RESET}")
            return result
        except StructuredReplyError as err:
            print(f"{RED}  ✗ Claude reply error: {err}{RESET}")
            return None
        except Exception as e:
            if "overloaded" in str(e).lower() and attempt < 2:
                wait = (attempt + 1) * 20
                print(f"{YELLOW}  ⚠ API overloaded — retrying in {wait}s...{RESET}")
                time.sleep(wait)
            else:
                print(f"{RED}  ✗ Claude error: {e}{RESET}")
                return None
    return None


# ── SAVE BRIEF DATA ────────────────────────────────────────────
def save_brief(validated_items, opportunity_candidates, claude_analysis, pm_data_map, market_open, market_reason):
    """Save all pre-market data to logs/premarket_brief.json for premarket_brief.py"""
    os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
    path = os.path.join(BASE_DIR, "logs", "premarket_brief.json")

    output = {
        # Timezone-aware UTC timestamp — a naive one here gets misread by the
        # browser's Date parser as LOCAL time, which is wrong when server
        # (UTC) and browser (Oman, UTC+4) differ. Same root cause as the
        # stock_insight.py "stuck at Analyzing" bug — the dashboard's
        # freshness check compares this against the browser's clock, so it
        # must be an unambiguous, timezone-aware value.
        "generated_at":           datetime.now(timezone.utc).isoformat(),
        "market_open":            market_open,
        "market_closed_reason":   market_reason if not market_open else None,
        "validated":              validated_items,
        "opportunity_candidates": opportunity_candidates,
        "claude_analysis":        claude_analysis,
        "pm_snapshot": {
            t: {k: v for k, v in d.items() if k != "ticker"}
            for t, d in pm_data_map.items()
            if d.get("available")
        },
    }

    with open(path, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"{GREEN}✓ Brief data saved to logs/premarket_brief.json{RESET}")
    return path


# ── MAIN ───────────────────────────────────────────────────────
def run(check_only=False):
    print(f"\n{BOLD}{BLUE}{'='*55}")
    print(f"  Pre-Market Scanner")
    print(f"  {datetime.now().strftime('%A %d %B %Y  %H:%M')}")
    print(f"{'='*55}{RESET}\n")

    # Step 1 — Market calendar check
    market_open, market_reason = is_market_open_today()

    if check_only:
        if not market_open:
            print(f"{YELLOW}⏭  {market_reason}{RESET}")
            sys.exit(2)
        print(f"{GREEN}✓ Market open today — proceeding{RESET}")
        sys.exit(0)

    if not market_open:
        print(f"{YELLOW}⏭  {market_reason} — saving closed notice{RESET}")
        save_brief([], [], None, {}, False, market_reason)
        sys.exit(0)

    # Step 2 — Load watchlist + existing recs
    print(f"{BLUE}Loading watchlist and existing recommendations...{RESET}")
    holdings, interested = load_watchlist()
    rec_map = load_latest_recs()

    print(f"  Holdings:   {len(holdings)}")
    print(f"  Interested: {len(interested)}")
    print(f"  Existing recs in latest.json: {len(rec_map)}")

    # Step 3 — Build full universe
    sector_leaders = get_sector_leaders()
    universe = build_universe(holdings, interested, sector_leaders)
    print(f"\n{BLUE}Universe: {len(universe)} tickers{RESET}")

    # Step 4 — Fetch pre-market prices
    print(f"\n{BLUE}Fetching pre-market prices...{RESET}")
    pm_data_map = {}
    available_count = 0

    for ticker in universe:
        print(f"  {BLUE}{ticker}...{RESET}", end=" ", flush=True)
        pm = fetch_premarket(ticker)
        pm_data_map[ticker] = pm
        if pm["available"]:
            available_count += 1
            arrow = "▲" if pm["pm_change_pct"] > 0 else "▼"
            color = GREEN if pm["pm_change_pct"] > 0 else RED
            print(f"{color}{arrow} {pm['pm_change_pct']:+.1f}%  ${pm['pm_price']}{RESET}")
        else:
            print(f"{YELLOW}no pre-market data{RESET}")
        time.sleep(0.3)  # Rate limiting

    print(f"\n{GREEN}✓ Pre-market data: {available_count}/{len(universe)} tickers{RESET}")

    # Step 5 — Validate existing recommendations
    print(f"\n{BLUE}Validating existing recommendations...{RESET}")
    validated_items = validate_recommendations(rec_map, pm_data_map)

    urgent_count    = len([x for x in validated_items if x["status"] == "URGENT"])
    withdrawn_count = len([x for x in validated_items if x["status"] == "WITHDRAWN"])
    revised_count   = len([x for x in validated_items if x["status"] in ("REVISED", "OPPORTUNITY")])
    confirmed_count = len([x for x in validated_items if x["status"] == "CONFIRMED"])

    print(f"  🚨 Urgent:    {urgent_count}")
    print(f"  ⚠️  Withdrawn: {withdrawn_count}")
    print(f"  ✏️  Revised:   {revised_count}")
    print(f"  ✅ Confirmed:  {confirmed_count}")

    # Step 6 — Scan for new opportunities
    print(f"\n{BLUE}Scanning for new opportunities (>{OPPORTUNITY_THRESHOLD}% movers)...{RESET}")
    opportunity_candidates = scan_opportunities(pm_data_map, rec_map, holdings, interested, sector_leaders)
    print(f"  Found {len(opportunity_candidates)} opportunity candidates")
    for c in opportunity_candidates[:5]:
        arrow = "▲" if c["pm_change"] > 0 else "▼"
        print(f"  {arrow} {c['ticker']} {c['pm_change']:+.1f}% — {c['sector']}")

    # Step 7 — Claude analysis (only if there's something worth analysing)
    meaningful = (urgent_count + withdrawn_count + revised_count +
                  len([x for x in validated_items if x["status"] == "WATCH"]) +
                  len(opportunity_candidates))

    claude_analysis = None
    if meaningful > 0:
        print(f"\n{BLUE}Calling Claude for pre-market analysis ({meaningful} items)...{RESET}")
        claude_analysis = analyse_with_claude(validated_items, opportunity_candidates)
    else:
        print(f"\n{YELLOW}No significant pre-market moves — skipping Claude call{RESET}")

    # Step 8 — Save brief data
    print(f"\n{BLUE}Saving brief data...{RESET}")
    save_brief(validated_items, opportunity_candidates, claude_analysis, pm_data_map, True, "")

    print(f"\n{BOLD}{GREEN}✓ Pre-market scan complete{RESET}")


if __name__ == "__main__":
    check_only = "--check-only" in sys.argv
    run(check_only=check_only)
