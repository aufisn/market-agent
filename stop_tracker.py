# ============================================================
# MARKET AGENT V4.5 — Intelligent Position Monitor
# stop_tracker.py
#
# Replaces fixed % stop loss with a contextual system:
#   Gate 0A  — single-day ATR breach detection
#   Gate 0B  — cumulative 10-day drift from entry
#   Filter 1 — SPY market context (suppress if market-wide)
#   Filter 2 — Sector ETF context (suppress if sector-driven)
#   Stop clock — days at threshold before alert fires
#   Early warning — -5% drift alert before threshold breached
#   Trailing stop — protects profit once target confirmed
#   Gain clock — confirms target before locking in profit
#
# Reads:  logs/performance_log.json  (entry prices)
#         data/context_prices.json   (SPY + sector ETFs)
#         data/market_data.json      (current prices + ATR)
#         logs/stop_clock.json       (persistent clock state)
#         strategy.json              (contextual stop rules)
# Writes: logs/stop_clock.json       (updated clock state)
#         logs/stop_status.json      (current status per ticker)
# ============================================================

import json
import os
from datetime import datetime
from dotenv import load_dotenv
import anthropic
from claude_json import ask_json
from common import GREEN, RED, YELLOW, BLUE, BOLD, RESET, safe_float, load_json, save_json, send_telegram, trail_width_pct

load_dotenv()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONTEXT_PATH      = os.path.join(BASE_DIR, "data",  "context_prices.json")
MARKET_DATA_PATH  = os.path.join(BASE_DIR, "data",  "market_data.json")
STOP_CLOCK_PATH   = os.path.join(BASE_DIR, "logs",  "stop_clock.json")
STOP_STATUS_PATH  = os.path.join(BASE_DIR, "logs",  "stop_status.json")
STRATEGY_PATH     = os.path.join(BASE_DIR,           "strategy.json")

GAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["TAKE_PROFIT", "TRIM", "LET_IT_RUN"]},
        "reason": {"type": "string"},
        "trail":  {"type": "boolean"},
    },
    "required": ["action", "reason", "trail"],
}
ALERT_STATE_PATH  = os.path.join(BASE_DIR, "logs",  "price_alerts_fired.json")
SENT_STATE_PATH   = os.path.join(BASE_DIR, "logs",  "stop_alert_state.json")
REMIND_AFTER_DAYS = 5     # repeat an unchanged alert at most this often
WORSEN_RESEND_PTS = 5.0   # ...or sooner if the loss deepened this much


# ── CLAUDE GAIN RECOMMENDATION ────────────────────────────────
def get_gain_recommendation(ticker, entry_price, current_price, target,
                             drift_pct, atr, sector, spy_chg, sector_chg,
                             sector_etf, days_held):
    """
    Calls Claude API to assess a position that has confirmed its target.
    Returns dict: {action: TAKE_PROFIT|TRIM|LET_IT_RUN, reason: str, trail: bool}
    """
    try:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            return {"action": "LET_IT_RUN", "reason": "Claude API not configured — defaulting to trail.", "trail": True}

        client = anthropic.Anthropic(api_key=api_key)

        upside_from_entry  = round(drift_pct, 2)
        pct_above_target   = round(((current_price - target) / target) * 100, 2)
        spy_str    = f"{'+' if spy_chg and spy_chg >= 0 else ''}{spy_chg:.1f}%" if spy_chg is not None else "N/A"
        sector_str = f"{sector_etf} {'+' if sector_chg and sector_chg >= 0 else ''}{sector_chg:.1f}%" if sector_chg is not None and sector_etf else "N/A"
        atr_str    = f"${atr:.2f}" if atr else "N/A"

        prompt = f"""You are a portfolio risk manager assessing a position that has hit its price target.

POSITION DATA:
  Ticker:           {ticker}
  Sector:           {sector or 'Unknown'}
  Entry price:      ${entry_price:.2f}
  Target price:     ${target:.2f}
  Current price:    ${current_price:.2f}
  Gain from entry:  {upside_from_entry:+.2f}%
  Above target by:  {pct_above_target:+.2f}%
  Days held:        {days_held}
  ATR (daily):      {atr_str}

MARKET CONTEXT TODAY:
  SPY (S&P 500):    {spy_str}
  Sector ETF:       {sector_str}

TASK:
Based on this data, recommend ONE of three actions:

TAKE_PROFIT — Target reached, risk/reward now unfavourable. Exit the full position cleanly. Use when: price has run well above target, momentum looks exhausted, or market context is weakening.

TRIM — Take partial profit now (approximately 50% of position). Let remainder run with a trailing stop. Use when: stock has more upside but prudent to lock in some gain.

LET_IT_RUN — Momentum is strong, stock still has room. Raise trailing stop and hold full position. Use when: stock is breaking out above target with strong volume/sector support.

Respond ONLY with valid JSON, no markdown:
{{"action": "TAKE_PROFIT" | "TRIM" | "LET_IT_RUN", "reason": "2-3 sentence explanation referencing the specific data above.", "trail": true | false}}

trail must be true for TRIM and LET_IT_RUN, false for TAKE_PROFIT."""

        result = ask_json(client, prompt, max_tokens=300, label=f"stop_tracker_{ticker}", schema=GAIN_SCHEMA)

        # Validate
        if result.get("action") not in ("TAKE_PROFIT", "TRIM", "LET_IT_RUN"):
            result["action"] = "LET_IT_RUN"
        if "trail" not in result:
            result["trail"] = result["action"] != "TAKE_PROFIT"

        print(f"  {GREEN}✓ Claude recommendation for {ticker}: {result['action']}{RESET}")
        return result

    except Exception as e:
        print(f"  {YELLOW}⚠ Claude API error for gain recommendation ({ticker}): {e}{RESET}")
        return {"action": "LET_IT_RUN", "reason": "Could not reach Claude API — defaulting to trail.", "trail": True}


# ── SECTOR ETF MAP ────────────────────────────────────────────
SECTOR_ETF_MAP = {
    "Technology":             "XLK",
    "Healthcare":             "XLV",
    "Financials":             "XLF",
    "Consumer Staples":       "XLP",
    "Energy":                 "XLE",
    "Industrials":            "XLI",
    "Consumer Discretionary": "XLY",
    "Utilities":              "XLU",
    "Real Estate":            "XLRE",
    "Commodities":            "GLD",
    "Communications":         "XLC",
    "Materials":              "XLB",
    "Index":                  "SPY",
}

# Yahoo's sector names → the names above (and strategy.json's)
YAHOO_SECTOR_NAMES = {
    "Financial Services":     "Financials",
    "Consumer Defensive":     "Consumer Staples",
    "Consumer Cyclical":      "Consumer Discretionary",
    "Communication Services": "Communications",
    "Basic Materials":        "Materials",
}


def normalize_sector(sector):
    sector = (sector or "").strip()
    return YAHOO_SECTOR_NAMES.get(sector, sector) or None


def fetch_context_prices(sectors):
    """Today's % change for SPY and the sector ETFs of current holdings,
    written to data/context_prices.json. Nothing ever wrote that file
    before, so the contextual-stop filters ("is the whole market or
    sector also down?") never ran."""
    tickers = ["SPY"] + sorted({SECTOR_ETF_MAP[s] for s in sectors if s in SECTOR_ETF_MAP and SECTOR_ETF_MAP[s] != "SPY"})
    prices = {}
    try:
        import yfinance as yf
        for t in tickers:
            try:
                hist = yf.Ticker(t).history(period="5d", auto_adjust=True)
                closes = [float(c) for c in hist["Close"].dropna()] if hist is not None and not hist.empty else []
                if len(closes) >= 2:
                    prices[t] = {"price": round(closes[-1], 2),
                                 "change_pct": round((closes[-1] / closes[-2] - 1) * 100, 2)}
            except Exception:
                pass
    except Exception:
        pass
    data = {"prices": prices, "fetched_at": datetime.now().isoformat()}
    save_json(CONTEXT_PATH, data)
    missing = [t for t in tickers if t not in prices]
    summary = ", ".join("%s %+.2f%%" % (t, v["change_pct"]) for t, v in prices.items())
    missing_txt = f" — missing {missing}" if missing else ""
    print(f"{BLUE}Context prices: {summary}{missing_txt}{RESET}")
    return data

# Infer sector from ticker note field or name
def infer_sector(note):
    note = (note or "").lower()
    # Fallback only (evaluate_position prefers the real sector). Word
    # boundaries: a bare "ai" substring used to make "Retail" Technology.
    import re
    words = set(re.findall(r"[a-z]+", note))
    if words & {"ai"} or any(w in note for w in ["tech", "software", "semiconductor", "chip", "cloud"]):
        return "Technology"
    if any(w in note for w in ["health", "pharma", "medical", "biotech"]):
        return "Healthcare"
    if any(w in note for w in ["financial", "bank", "insur", "payment"]):
        return "Financials"
    if any(w in note for w in ["staple", "consumer staple", "beverage", "food"]):
        return "Consumer Staples"
    if any(w in note for w in ["energy", "oil", "gas", "pipeline"]):
        return "Energy"
    if any(w in note for w in ["industrial", "defence", "aerospace", "logistics"]):
        return "Industrials"
    if any(w in note for w in ["discretionary", "retail", "restaurant", "travel", "auto"]):
        return "Consumer Discretionary"
    if any(w in note for w in ["utilit", "clean energy", "power"]):
        return "Utilities"
    if any(w in note for w in ["real estate", "reit", "property"]):
        return "Real Estate"
    if any(w in note for w in ["gold", "silver", "commodit", "metal", "precious"]):
        return "Commodities"
    if any(w in note for w in ["communication", "media", "telecom"]):
        return "Communications"
    return None


# ── ATR ESTIMATE ─────────────────────────────────────────────
def estimate_atr(ticker, market_data):
    """
    The 14-day ATR from market_data; failing that, today's true range,
    then 2% of price.
    """
    stocks = market_data.get("stocks", []) if market_data else []
    for s in stocks:
        if s.get("ticker", "").upper() == ticker.upper():
            if safe_float(s.get("atr_14")) > 0:
                return round(safe_float(s["atr_14"]), 4)
            hi  = safe_float(s.get("price_day_high"))
            lo  = safe_float(s.get("price_day_low"))
            pre = safe_float(s.get("price_prev_close"))
            curr= safe_float(s.get("price"))
            if hi > 0 and lo > 0 and pre > 0:
                true_range = max(hi - lo, abs(hi - pre), abs(lo - pre))
                return round(true_range, 4)
            if curr > 0:
                return round(curr * 0.02, 4)  # 2% fallback
    return None


# ── CONTEXT FILTERS ───────────────────────────────────────────
def check_context_filters(context_prices, sector, strategy):
    """
    Returns (spy_suppressed, sector_suppressed, spy_chg, sector_chg, sector_etf)
    spy_suppressed = True if SPY is also down enough to suggest market-driven decline
    sector_suppressed = True if sector ETF also down enough
    """
    if not context_prices:
        return False, False, None, None, None

    prices = context_prices.get("prices", {})

    # Filter 1 — SPY
    spy_threshold = -1.0
    if strategy:
        spy_threshold = strategy.get("stop_loss", {}).get(
            "contextual_stop", {}).get("context_filters", {}).get(
            "filter_1_market", {}).get("spy_down_threshold_pct", -1.0)

    spy_data    = prices.get("SPY", {})
    spy_chg     = safe_float(spy_data.get("change_pct"), default=None)
    spy_suppressed = spy_chg is not None and spy_chg <= spy_threshold

    # Filter 2 — Sector ETF
    sector_threshold = -1.0
    if strategy:
        sector_threshold = strategy.get("stop_loss", {}).get(
            "contextual_stop", {}).get("context_filters", {}).get(
            "filter_2_sector", {}).get("sector_down_threshold_pct", -1.0)

    sector_etf = SECTOR_ETF_MAP.get(sector, None)
    sector_chg = None
    sector_suppressed = False
    if sector_etf:
        etf_data  = prices.get(sector_etf, {})
        sector_chg = safe_float(etf_data.get("change_pct"), default=None)
        sector_suppressed = sector_chg is not None and sector_chg <= sector_threshold

    return spy_suppressed, sector_suppressed, spy_chg, sector_chg, sector_etf


# ── CLOCK STATE ───────────────────────────────────────────────
def load_clock():
    data = load_json(STOP_CLOCK_PATH)
    return data if isinstance(data, dict) else {}


def save_clock(clock):
    save_json(STOP_CLOCK_PATH, clock)


def get_ticker_clock(clock, ticker):
    return clock.get(ticker.upper(), {
        "ticker":               ticker.upper(),
        "level_1_days":         0,
        "level_2_days":         0,
        "level_1_start":        None,
        "level_2_start":        None,
        "early_warning_sent":   False,
        "level_1_alert_sent":   False,
        "level_2_alert_sent":   False,
        "trailing_active":      False,
        "trailing_high":        None,
        "trailing_stop_price":  None,
        "gain_days_above_target": 0,
        "gain_target_reached":  False,
        "gain_alert_sent":      False,
        "last_updated":         None,
    })


# ── MAIN POSITION EVALUATOR ───────────────────────────────────
def evaluate_position(entry, current_price, atr, context_prices, clock_state, strategy, today_str):
    """
    Core logic for a single open position.
    Returns updated clock_state, status_record, list of alerts to send.
    """
    ticker     = entry.get("ticker", "").upper()
    entry_price= safe_float(entry.get("entryPrice"))
    target     = safe_float(entry.get("target"))
    note       = entry.get("note", "")
    sector     = normalize_sector(entry.get("sector")) or infer_sector(note)

    if entry_price <= 0 or current_price <= 0:
        return clock_state, None, []

    # Cumulative drift from entry
    drift_pct = round(((current_price - entry_price) / entry_price) * 100, 2)

    # Today's single-day change (from market_data change_pct if available)
    # We use drift as a proxy for cumulative; daily change handled via ATR gate
    atr_threshold = atr if atr else (entry_price * 0.02)
    atr_pct = atr_threshold / current_price * 100   # trailing-stop width

    # Hard exit — no filters, no clock
    hard_exit_pct = -15.0
    if strategy:
        hard_exit_pct = safe_float(
            strategy.get("stop_loss", {}).get("hard_exit_pct", -15.0), -15.0)

    alerts = []
    status = "OK"
    action = ""

    cs = dict(clock_state)  # copy to modify

    # ── HARD EXIT (Gate bypassed — immediate) ─────────────────
    if drift_pct <= hard_exit_pct:
        status = "HARD_EXIT"
        action = f"EXIT IMMEDIATELY — position down {drift_pct:.1f}% from entry (hard exit at {hard_exit_pct}%)"
        alerts.append({
            "level":   "CRITICAL",
            "ticker":  ticker,
            "message": (
                f"🚨 <b>HARD EXIT REQUIRED — {ticker}</b>\n\n"
                f"Position has fallen <b>{drift_pct:.1f}%</b> from entry.\n"
                f"Hard exit threshold is {hard_exit_pct}%.\n\n"
                f"📌 Entry:   <b>${entry_price:.2f}</b>\n"
                f"📌 Current: <b>${current_price:.2f}</b>\n"
                f"📌 Loss:    <b>{drift_pct:+.2f}%</b>\n\n"
                f"⛔ <b>EXIT IMMEDIATELY — no context filters apply at this level.</b>"
            )
        })
        return cs, {
            "ticker":      ticker,
            "status":      status,
            "drift_pct":   drift_pct,
            "action":      action,
            "sector":      sector,
            "entry_price": entry_price,
            "current_price": current_price,
            "clock_days_l1": cs.get("level_1_days", 0),
            "clock_days_l2": cs.get("level_2_days", 0),
            "trailing_active": cs.get("trailing_active", False),
            "trailing_stop_price": cs.get("trailing_stop_price"),
            "evaluated_at": today_str,
        }, alerts

    # ── TRAILING STOP CHECK ───────────────────────────────────
    if cs.get("trailing_active") and cs.get("trailing_stop_price"):
        trail_stop = safe_float(cs["trailing_stop_price"])
        if trail_stop > 0 and current_price <= trail_stop:
            status = "TRAIL_EXIT"
            action = f"EXIT — trailing stop hit at ${trail_stop:.2f} (current ${current_price:.2f})"
            alerts.append({
                "level":   "HIGH",
                "ticker":  ticker,
                "message": (
                    f"🔔 <b>Trailing Stop Hit — {ticker}</b>\n\n"
                    f"Price has fallen below the trailing stop.\n\n"
                    f"📌 Entry:         ${entry_price:.2f}\n"
                    f"📌 Current:       ${current_price:.2f}\n"
                    f"📌 Trailing stop: ${trail_stop:.2f}\n"
                    f"📌 Total gain:    {drift_pct:+.2f}%\n\n"
                    f"✅ Consider exiting to protect your profit."
                )
            })
        else:
            # Update trailing high and stop
            trail_high  = max(safe_float(cs.get("trailing_high", current_price)), current_price)
            new_trail   = round(trail_high * (1 - trail_width_pct(atr_pct, strategy) / 100), 2)
            cs["trailing_high"]        = trail_high
            cs["trailing_stop_price"]  = new_trail
            status = "TRAILING"
            action = f"Trailing stop active at ${new_trail:.2f} (high ${trail_high:.2f})"

    # ── GAIN CLOCK / PROFIT TARGET ────────────────────────────
    if target > 0 and current_price >= target and status == "OK":
        cs["gain_target_reached"]    = True
        cs["gain_days_above_target"] = cs.get("gain_days_above_target", 0) + 1
        gain_days    = cs["gain_days_above_target"]
        confirm_days = 3

        if gain_days >= confirm_days and not cs.get("gain_alert_sent"):
            cs["gain_alert_sent"] = True

            # Get context for Claude recommendation
            ctx_prices = context_prices.get("prices", {}) if context_prices else {}
            spy_chg    = safe_float(ctx_prices.get("SPY", {}).get("change_pct"), default=None)
            sec_etf    = SECTOR_ETF_MAP.get(sector) if sector else None
            sec_chg    = safe_float(ctx_prices.get(sec_etf, {}).get("change_pct"), default=None) if sec_etf else None
            days_held  = gain_days  # minimum; actual days held not tracked here

            # Ask Claude what to do
            rec = get_gain_recommendation(
                ticker, entry_price, current_price, target,
                drift_pct, atr, sector, spy_chg, sec_chg,
                sec_etf, days_held
            )

            action_label = rec.get("action", "LET_IT_RUN")
            reason       = rec.get("reason", "")
            do_trail     = rec.get("trail", True)

            # Only activate trailing stop if Claude recommends it
            if do_trail and not cs.get("trailing_active"):
                cs["trailing_active"]     = True
                cs["trailing_high"]       = current_price
                cs["trailing_stop_price"] = round(current_price * (1 - trail_width_pct(atr_pct, strategy) / 100), 2)

            status = "TARGET_HIT"
            action = f"Target confirmed ({gain_days} days) — Claude: {action_label}"

            # Emoji and framing per recommendation
            if action_label == "TAKE_PROFIT":
                emoji     = "💰"
                rec_line  = "🔴 <b>Recommendation: TAKE PROFIT</b>\nExit the full position now."
                trail_note = ""
            elif action_label == "TRIM":
                emoji     = "✂️"
                rec_line  = "🟡 <b>Recommendation: TRIM</b>\nTake ~50% profit. Trailing stop active on remainder."
                trail_note = f"\n📌 Trailing stop set at ${cs.get('trailing_stop_price', 0):.2f}"
            else:  # LET_IT_RUN
                emoji     = "🚀"
                rec_line  = "🟢 <b>Recommendation: LET IT RUN</b>\nHold full position. Trailing stop raised."
                trail_note = f"\n📌 Trailing stop set at ${cs.get('trailing_stop_price', 0):.2f}"

            alerts.append({
                "level":   "POSITIVE",
                "ticker":  ticker,
                "message": (
                    f"{emoji} <b>Target Confirmed — {ticker}</b>\n\n"
                    f"Position above target for <b>{gain_days} days</b>.\n\n"
                    f"📌 Entry:          ${entry_price:.2f}\n"
                    f"📌 Target:         ${target:.2f}\n"
                    f"📌 Current:        ${current_price:.2f}\n"
                    f"📌 Gain:           {drift_pct:+.2f}%\n"
                    f"📌 Above target:   {round(((current_price-target)/target)*100,2):+.2f}%"
                    f"{trail_note}\n\n"
                    f"{rec_line}\n\n"
                    f"💬 <i>{reason}</i>"
                )
            })

        elif gain_days < confirm_days:
            status = "NEAR_TARGET"
            action = f"Price at/above target — confirming ({gain_days}/{confirm_days} days)"
    elif status not in ("TRAILING", "TRAIL_EXIT"):
        cs["gain_days_above_target"] = 0

    # ── STOP LOGIC — only if not already in gain/trail state ──
    if status not in ("TARGET_HIT", "NEAR_TARGET", "TRAILING", "TRAIL_EXIT", "HARD_EXIT"):

        # Check context filters
        spy_sup, sec_sup, spy_chg, sec_chg, sec_etf = check_context_filters(
            context_prices, sector, strategy)

        context_suppressed = spy_sup and sec_sup
        context_note = ""
        if spy_chg is not None:
            context_note += f"SPY {'+' if spy_chg>=0 else ''}{spy_chg:.1f}%"
        if sec_chg is not None and sec_etf:
            context_note += f"  |  {sec_etf} {'+' if sec_chg>=0 else ''}{sec_chg:.1f}%"

        # Level thresholds
        l1_pct = -8.0
        l2_pct = -12.0
        ew_pct = -5.0

        # ── EARLY WARNING ─────────────────────────────────────
        if drift_pct <= ew_pct and not cs.get("early_warning_sent"):
            cs["early_warning_sent"] = True
            note_txt = f"  Context: {context_note}" if context_note else ""
            if not context_suppressed:
                alerts.append({
                    "level":   "WARNING",
                    "ticker":  ticker,
                    "message": (
                        f"⚠️ <b>Early Warning — {ticker}</b>\n\n"
                        f"Position has drifted <b>{drift_pct:.1f}%</b> from entry.\n"
                        f"Approaching Level 1 stop threshold (-8%).\n\n"
                        f"📌 Entry:   ${entry_price:.2f}\n"
                        f"📌 Current: ${current_price:.2f}\n"
                        f"📌 Drift:   {drift_pct:+.2f}%\n"
                        f"{note_txt}\n\n"
                        f"No action required yet — monitoring."
                    )
                })
            status = "EARLY_WARNING"

        # ── LEVEL 1 — -8% ─────────────────────────────────────
        if drift_pct <= l1_pct:
            if context_suppressed:
                status = "L1_SUPPRESSED"
                action = (f"Level 1 ({drift_pct:.1f}%) suppressed — market/sector both down "
                          f"({context_note})")
                cs["level_1_days"] = 0  # reset — not stock-specific
            else:
                cs["level_1_days"] = cs.get("level_1_days", 0) + 1
                if cs.get("level_1_start") is None:
                    cs["level_1_start"] = today_str
                days    = cs["level_1_days"]
                allowed = 5
                status  = f"L1_CLOCK_{days}d"
                action  = f"Level 1 stop: {drift_pct:.1f}% — day {days}/{allowed} before alert"

                if days >= allowed and not cs.get("level_1_alert_sent"):
                    cs["level_1_alert_sent"] = True
                    status = "L1_ALERT"
                    action = f"Level 1 clock expired ({days} days at -8%) — exit recommended"
                    alerts.append({
                        "level":   "HIGH",
                        "ticker":  ticker,
                        "message": (
                            f"🔴 <b>Stop Alert — {ticker}</b>\n\n"
                            f"Position has been down <b>{drift_pct:.1f}%</b> for "
                            f"<b>{days} days</b> (Level 1 clock expired).\n\n"
                            f"📌 Entry:    ${entry_price:.2f}\n"
                            f"📌 Current:  ${current_price:.2f}\n"
                            f"📌 Drift:    {drift_pct:+.2f}%\n"
                            f"📌 Context:  {context_note or 'Stock-specific decline'}\n\n"
                            f"This is a <b>stock-specific decline</b>, not market/sector driven.\n"
                            f"⚠️ Consider exiting — thesis review recommended."
                        )
                    })
        else:
            # Recovery — reset L1
            if cs.get("level_1_days", 0) > 0:
                cs["level_1_days"]       = 0
                cs["level_1_start"]      = None
                cs["level_1_alert_sent"] = False

        # ── LEVEL 2 — -12% ────────────────────────────────────
        if drift_pct <= l2_pct:
            if context_suppressed:
                status = "L2_SUPPRESSED"
                action = (f"Level 2 ({drift_pct:.1f}%) suppressed — market/sector both down "
                          f"({context_note})")
                cs["level_2_days"] = 0
            else:
                cs["level_2_days"] = cs.get("level_2_days", 0) + 1
                if cs.get("level_2_start") is None:
                    cs["level_2_start"] = today_str
                days    = cs["level_2_days"]
                allowed = 3
                status  = f"L2_CLOCK_{days}d"
                action  = f"Level 2 stop: {drift_pct:.1f}% — day {days}/{allowed} before strong alert"

                if days >= allowed and not cs.get("level_2_alert_sent"):
                    cs["level_2_alert_sent"] = True
                    status = "L2_ALERT"
                    action = f"Level 2 clock expired ({days} days at -12%) — STRONGLY recommend exit"
                    alerts.append({
                        "level":   "CRITICAL",
                        "ticker":  ticker,
                        "message": (
                            f"🚨 <b>Strong Exit Alert — {ticker}</b>\n\n"
                            f"Position has been down <b>{drift_pct:.1f}%</b> for "
                            f"<b>{days} days</b> (Level 2 clock expired).\n\n"
                            f"📌 Entry:    ${entry_price:.2f}\n"
                            f"📌 Current:  ${current_price:.2f}\n"
                            f"📌 Drift:    {drift_pct:+.2f}%\n"
                            f"📌 Context:  {context_note or 'Stock-specific decline'}\n\n"
                            f"This is a confirmed <b>stock-specific decline</b>.\n"
                            f"⛔ <b>EXIT STRONGLY RECOMMENDED — protect remaining capital.</b>"
                        )
                    })
        else:
            if cs.get("level_2_days", 0) > 0:
                cs["level_2_days"]       = 0
                cs["level_2_start"]      = None
                cs["level_2_alert_sent"] = False

        # Reset early warning if recovered above -5%
        if drift_pct > ew_pct:
            cs["early_warning_sent"] = False

        if status == "OK" and drift_pct > 0:
            status = "GAIN"
            action = f"Position up {drift_pct:+.2f}% from entry"
        elif status == "OK":
            action = f"Position {drift_pct:+.2f}% from entry — within normal range"

    cs["last_updated"] = today_str

    status_record = {
        "ticker":             ticker,
        "status":             status,
        "drift_pct":          drift_pct,
        "action":             action,
        "sector":             sector,
        "entry_price":        entry_price,
        "current_price":      current_price,
        "target":             target,
        "clock_days_l1":      cs.get("level_1_days", 0),
        "clock_days_l2":      cs.get("level_2_days", 0),
        "early_warning_sent": cs.get("early_warning_sent", False),
        "trailing_active":    cs.get("trailing_active", False),
        "trailing_stop_price":cs.get("trailing_stop_price"),
        "trailing_high":      cs.get("trailing_high"),
        "gain_days":          cs.get("gain_days_above_target", 0),
        "spy_suppressed":     False,
        "sector_suppressed":  False,
        "evaluated_at":       today_str,
    }
    return cs, status_record, alerts


# ── PRICE ALERT MONITOR ───────────────────────────────────────
def load_alert_state():
    try:
        if os.path.exists(ALERT_STATE_PATH):
            with open(ALERT_STATE_PATH) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_alert_state(state):
    os.makedirs(os.path.dirname(ALERT_STATE_PATH), exist_ok=True)
    with open(ALERT_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def check_price_alerts(stocks, watchlist_map):
    """
    Fire Telegram alerts when price crosses alert_above or alert_below.
    Re-arms only after price retraces through the level by >1%.
    Runs on both 'holding' and 'interested' stocks.
    """
    state   = load_alert_state()
    updated = False

    for stock in stocks:
        ticker = stock.get("ticker", "").upper()
        ltype  = str(stock.get("list_type", "")).lower()
        if ltype not in ("holding", "interested"):
            continue

        price = safe_float(stock.get("price"))
        if price <= 0:
            continue

        # Alert levels come from watchlist (source of truth), fall back to stock data
        wl          = watchlist_map.get(ticker, {})
        alert_above = safe_float(wl.get("alert_above") or stock.get("alert_above"))
        alert_below = safe_float(wl.get("alert_below") or stock.get("alert_below"))
        name        = stock.get("name", ticker)

        # ── ALERT ABOVE ───────────────────────────────────────
        if alert_above > 0:
            key   = f"{ticker}_above_{alert_above}"
            fired = state.get(key, {}).get("fired", False)

            if not fired and price > alert_above:
                msg = (
                    f"🔔 <b>Price Alert — {ticker}</b>\n\n"
                    f"Broke above <b>${alert_above:,.2f}</b>\n"
                    f"Current price: <b>${price:,.2f}</b>\n"
                    f"<i>{name}</i>"
                )
                send_telegram(msg)
                state[key] = {"fired": True, "fired_at": price, "level": alert_above}
                updated = True
                print(f"  🔔 {ticker}: alert_above ${alert_above:.2f} fired (price ${price:.2f})")

            elif fired and price <= alert_above * 0.99:
                # Price retraced >1% back below level — re-arm
                state.pop(key, None)
                updated = True
                print(f"  ↩  {ticker}: alert_above re-armed (${price:.2f} back below ${alert_above:.2f})")

        # ── ALERT BELOW ───────────────────────────────────────
        if alert_below > 0:
            key   = f"{ticker}_below_{alert_below}"
            fired = state.get(key, {}).get("fired", False)

            if not fired and price < alert_below:
                msg = (
                    f"🔔 <b>Price Alert — {ticker}</b>\n\n"
                    f"Dropped below <b>${alert_below:,.2f}</b>\n"
                    f"Current price: <b>${price:,.2f}</b>\n"
                    f"<i>{name}</i>"
                )
                send_telegram(msg)
                state[key] = {"fired": True, "fired_at": price, "level": alert_below}
                updated = True
                print(f"  🔔 {ticker}: alert_below ${alert_below:.2f} fired (price ${price:.2f})")

            elif fired and price >= alert_below * 1.01:
                # Price recovered >1% back above level — re-arm
                state.pop(key, None)
                updated = True
                print(f"  ↩  {ticker}: alert_below re-armed (${price:.2f} back above ${alert_below:.2f})")

    if updated:
        save_alert_state(state)


# ── MAIN ──────────────────────────────────────────────────────
def run():
    print(f"\n{BOLD}{BLUE}{'='*55}")
    print(f"  MARKET AGENT V4.5 — Position Monitor")
    print(f"  {datetime.now().strftime('%A %d %B %Y  %H:%M')}")
    print(f"{'='*55}{RESET}\n")

    today_str = datetime.now().strftime("%Y-%m-%d")

    # Load all data sources
    context_data  = load_json(CONTEXT_PATH)       or {}
    market_data   = load_json(MARKET_DATA_PATH)   or {}
    strategy      = load_json(STRATEGY_PATH)      or {}
    clock         = load_clock()

    # Get current prices from market_data stocks list (where holdings prices live)
    stocks_price_map = {}
    for s in market_data.get("stocks", []):
        t = str(s.get("ticker", "")).upper()
        if t and s.get("price"):
            stocks_price_map[t] = s

    # ── WATCHLIST SYNC CHECK ────────────────────────────
    print(f"\n{BLUE}── Watchlist Sync Check ──────────────────────────{RESET}")
    holdings_in_market = [
        s for s in market_data.get("stocks", [])
        if str(s.get("list_type","")).lower() == "holding"
    ]
    for s in holdings_in_market:
        ticker = str(s.get("ticker","")).upper()
        price  = safe_float(s.get("price", 0))
        if price <= 0:
            msg = (
                f"⚠️ <b>Data Warning — {ticker}</b>\n\n"
                f"Holding <b>{ticker}</b> has no price data in this run.\n"
                f"Possible causes: ticker delisted, data feed issue, or symbol changed.\n"
                f"Please verify on your broker — stop tracker skipped this position."
            )
            send_telegram(msg)
            print(f"  {YELLOW}⚠ {ticker}: no price data — Telegram alert sent{RESET}")
        else:
            print(f"  ✓ {ticker}: ${price:.2f}")


    # ── PRICE ALERTS (holdings + interested) ──────────────────
    print(f"\n{BLUE}── Price Alert Check ─────────────────────────────{RESET}")
    all_stocks = market_data.get("stocks", [])
    watchlist_map = {
        str(s.get("ticker", "")).upper(): s
        for s in all_stocks
    }
    check_price_alerts(all_stocks, watchlist_map)

    # SOURCE OF TRUTH: holdings come from market_data.json list_type="holding"
    # All position data (cost basis, target, stop, quantity) comes from the watchlist
    # performance_log.json is NOT used by stop tracker — it tracks agent picks only
    open_positions = []
    for s in market_data.get("stocks", []):
        if str(s.get("list_type", "")).strip().lower() != "holding":
            continue
        ticker = s.get("ticker", "").strip().upper()
        if not ticker:
            continue

        buy_price = safe_float(s.get("buy_price") or s.get("purchase_price") or s.get("cost_basis"))
        if buy_price <= 0:
            print(f"  {YELLOW}⚠ {ticker} — no buy_price in watchlist, skipping{RESET}")
            continue

        open_positions.append({
            "ticker":     ticker,
            "name":       s.get("name", ticker),
            "entryPrice": buy_price,        # cost basis from watchlist buy_price
            "target":     safe_float(s.get("target")),
            "stop":       safe_float(s.get("stop_loss")),
            "quantity":   safe_float(s.get("quantity")),
            "note":       s.get("name", ""),
            "sector":     s.get("sector"),   # Yahoo sector from fetch_data
        })

    if not open_positions:
        print(f"{YELLOW}⚠ No holdings with buy_price found in market_data.json{RESET}")
        save_json(STOP_STATUS_PATH, {"statuses": [], "evaluated_at": today_str})
        return {}

    print(f"{BLUE}Holdings to monitor ({len(open_positions)}): {[p['ticker'] for p in open_positions]}{RESET}")
    context_data = fetch_context_prices({normalize_sector(p.get("sector")) for p in open_positions})

    print(f"{BLUE}Evaluating {len(open_positions)} open position(s)...{RESET}\n")

    all_statuses = []
    all_alerts   = []
    updated_clock = dict(clock)

    for entry in open_positions:
        ticker    = entry.get("ticker", "").upper()
        price_d   = stocks_price_map.get(ticker)

        if not price_d or not price_d.get("price"):
            print(f"  {YELLOW}⚠ {ticker} — no current price available, skipping{RESET}")
            continue

        current_price = safe_float(price_d.get("price"))
        atr           = estimate_atr(ticker, market_data)
        clock_state   = get_ticker_clock(clock, ticker)

        new_clock, status_record, ticker_alerts = evaluate_position(
            entry, current_price, atr, context_data,
            clock_state, strategy, today_str
        )

        if status_record:
            updated_clock[ticker] = new_clock
            all_statuses.append(status_record)
            all_alerts.extend(ticker_alerts)

            # Print summary
            s = status_record["status"]
            d = status_record["drift_pct"]
            color = GREEN if d >= 0 else (RED if d <= -8 else YELLOW)
            print(f"  {BOLD}{ticker}{RESET}  drift={color}{d:+.2f}%{RESET}  "
                  f"status={s}  "
                  f"L1={status_record['clock_days_l1']}d  "
                  f"L2={status_record['clock_days_l2']}d  "
                  f"trail={'✓' if status_record['trailing_active'] else '—'}")
            if status_record.get("action"):
                print(f"    → {status_record['action']}")

    # Save updated clock and status
    save_clock(updated_clock)
    result = {
        "statuses":    all_statuses,
        "evaluated_at": today_str,
        "positions_checked": len(all_statuses),
        "alerts_fired":      len(all_alerts),
    }
    save_json(STOP_STATUS_PATH, result)

    # Alert de-duplication: the same HARD EXIT used to be re-sent on every
    # run (65 identical messages in a 65-day simulation). Send when the
    # status is new/changed, when the drift worsened by WORSEN_RESEND_PTS,
    # or as a reminder every REMIND_AFTER_DAYS days.
    sent_state = load_json(SENT_STATE_PATH) or {}
    by_ticker  = {st.get("ticker"): st for st in all_statuses if st}
    for t in list(sent_state):
        if by_ticker.get(t, {}).get("status") != sent_state[t].get("status"):
            sent_state.pop(t)  # status changed or position gone — next alert is fresh
    to_send = []
    for a in all_alerts:
        t = a.get("ticker", "")
        st = by_ticker.get(t, {})
        prev = sent_state.get(t)
        drift = safe_float(st.get("drift_pct"))
        if prev:
            days = (datetime.strptime(today_str, "%Y-%m-%d") - datetime.strptime(prev["last_sent"], "%Y-%m-%d")).days
            worse = drift <= safe_float(prev.get("drift")) - WORSEN_RESEND_PTS
            if not worse and days < REMIND_AFTER_DAYS:
                print(f"  {BLUE}· {t}: {st.get('status')} already alerted {prev['last_sent']} — suppressed{RESET}")
                continue
            if not worse:
                a = dict(a, message="🔁 <b>Reminder</b> — still unresolved\n\n" + a["message"])
        to_send.append(a)
        sent_state[t] = {"status": st.get("status"), "drift": drift, "last_sent": today_str}
    save_json(SENT_STATE_PATH, sent_state)
    all_alerts = to_send

    # Send Telegram alerts
    if all_alerts:
        print(f"\n{YELLOW}📱 Sending {len(all_alerts)} alert(s) to Telegram...{RESET}")
        for a in all_alerts:
            ok = send_telegram(a["message"])
            level = a.get("level","INFO")
            ticker = a.get("ticker","")
            if ok:
                print(f"  {GREEN}✓ Alert sent: {level} — {ticker}{RESET}")
            else:
                print(f"  {RED}✗ Alert failed: {level} — {ticker}{RESET}")
    else:
        print(f"\n{GREEN}✓ No alerts required — all positions within healthy parameters{RESET}")

    # Summary
    print(f"\n{BOLD}{GREEN}{'─'*55}")
    print(f"  STOP TRACKER SUMMARY")
    print(f"{'─'*55}{RESET}")
    status_counts = {}
    for s in all_statuses:
        key = s["status"].split("_")[0] if "_" in s["status"] else s["status"]
        status_counts[key] = status_counts.get(key, 0) + 1
    for k, v in sorted(status_counts.items()):
        print(f"  {k}: {v} position(s)")
    print(f"  Alerts sent: {len(all_alerts)}")
    print()

    return result


if __name__ == "__main__":
    run()
