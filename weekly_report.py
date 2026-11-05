# ============================================================
# Market Agent V4.5 — Weekly Sunday Summary
# Sends a full portfolio briefing every Sunday morning
# Triggered by run_agent.sh (Step 3b) when DAY=Sunday
# ============================================================

import os
from datetime import datetime, timedelta
from dotenv import load_dotenv
from common import GREEN, RED, BLUE, BOLD, RESET, safe_float, load_json, send_telegram

load_dotenv()

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
LATEST_FILE     = os.path.join(BASE_DIR, "logs",  "latest.json")
HISTORY_FILE    = os.path.join(BASE_DIR, "logs",  "performance_history.json")
STOP_CLOCK_PATH = os.path.join(BASE_DIR, "logs",  "stop_clock.json")
ALERT_STATE_PATH= os.path.join(BASE_DIR, "logs",  "price_alerts_fired.json")
PEAK_FILE       = os.path.join(BASE_DIR, "logs",  "portfolio_peak.json")


def get_spy_week_return():
    """Fetch SPY return over the past 7 days"""
    try:
        import yfinance as yf
        hist = yf.Ticker("SPY").history(period="10d")
        if len(hist) < 2:
            return None
        start = float(hist["Close"].iloc[0])
        end   = float(hist["Close"].iloc[-1])
        return round(((end - start) / start) * 100, 2)
    except Exception:
        return None


def week_change_from_history(history, curr_cap):
    """Calculate portfolio change vs 7 days ago from performance_history"""
    if not history:
        return None, None
    one_week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    snap = next((h for h in reversed(history) if h.get("date", "") <= one_week_ago), None)
    if not snap:
        return None, None
    prev_cap = safe_float(snap.get("total_capital", 0))
    if prev_cap <= 0:
        return None, None
    chg_usd = round(curr_cap - prev_cap, 2)
    chg_pct = round((chg_usd / prev_cap) * 100, 2)
    return chg_usd, chg_pct


def format_holding_row(h, clock):
    """Format a single holding line for the weekly summary"""
    ticker  = h.get("ticker", "—")
    price   = safe_float(h.get("price"))
    pnl_pct = safe_float(h.get("pnl_pct"))
    target  = safe_float(h.get("target"))
    stop    = safe_float(h.get("stop_loss"))
    qty     = safe_float(h.get("quantity"))
    val     = round(price * qty, 0) if price and qty else 0

    sign    = "+" if pnl_pct >= 0 else ""
    emoji   = "🟢" if pnl_pct >= 0 else "🔴"

    # Distance to target / stop
    to_tgt  = f"{(target-price)/price*100:+.1f}%" if target > 0 and price > 0 else "—"
    to_stop = f"{round(((stop-price)/price)*100,1)}%"    if stop  > 0 and price > 0 else "—"

    # Stop clock status
    ck      = clock.get(ticker, {})
    ck_stat = ck.get("status", "")
    clock_tag = ""
    if "L2" in ck_stat:
        clock_tag = " ⏰⏰"
    elif "L1" in ck_stat:
        clock_tag = " ⏰"
    elif "EARLY_WARNING" in ck_stat or "early_warning" in ck_stat.lower():
        clock_tag = " ⚠️"
    elif ck.get("trailing_active"):
        clock_tag = " 🔵"

    line = (
        f"{emoji} <b>{ticker}</b>{clock_tag}  {sign}{pnl_pct:.1f}%\n"
        f"   ${price:,.2f}  ·  ${val:,.0f}  ·  🎯{to_tgt}  ·  🛑{to_stop}"
    )
    return line


def get_active_alerts(alert_state):
    """Return list of currently fired price alerts"""
    if not alert_state:
        return []
    alerts = []
    for key, val in alert_state.items():
        if val.get("fired"):
            # key format: TICKER_above_LEVEL or TICKER_below_LEVEL
            parts = key.split("_")
            if len(parts) >= 3:
                ticker    = parts[0]
                direction = parts[1]
                level     = parts[2]
                fired_at  = safe_float(val.get("fired_at"))
                dir_emoji = "🔼" if direction == "above" else "🔽"
                alerts.append(
                    f"  {dir_emoji} <b>{ticker}</b> crossed ${level} → now ${fired_at:,.2f}"
                )
    return alerts


def get_earnings_this_week(latest):
    """Find holdings with earnings in the next 7 days"""
    earnings_list = latest.get("earnings", [])
    holdings_tickers = set(
        h.get("ticker", "").upper()
        for h in latest.get("holdings", [])
    )
    now      = datetime.now()
    week_end = now + timedelta(days=7)
    upcoming = []
    for e in earnings_list:
        ticker = str(e.get("ticker", "")).upper()
        if ticker not in holdings_tickers:
            continue
        date_str = e.get("earnings_date", "") or e.get("date", "")
        if not date_str:
            continue
        try:
            ed = datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
            if now <= ed <= week_end:
                upcoming.append(f"  📅 <b>{ticker}</b> — {ed.strftime('%a %d %b')}")
        except Exception:
            continue
    return upcoming


def run():
    print(f"\n{BOLD}{BLUE}{'='*55}")
    print(f"  MARKET AGENT — Weekly Sunday Summary")
    print(f"  {datetime.now().strftime('%A %d %B %Y')}")
    print(f"{'='*55}{RESET}\n")

    latest      = load_json(LATEST_FILE)  or {}
    history     = load_json(HISTORY_FILE) or []
    clock       = load_json(STOP_CLOCK_PATH)  or {}
    alert_state = load_json(ALERT_STATE_PATH) or {}
    peak        = load_json(PEAK_FILE)    or {}

    now      = datetime.now()
    week_str = now.strftime("%d %b %Y")

    # ── PORTFOLIO SUMMARY ─────────────────────────────────────
    ps       = latest.get("portfolio_summary", {})
    curr_cap = safe_float(ps.get("total_capital") or latest.get("total_capital", 0))
    curr_pnl = safe_float(ps.get("total_pnl_usd") or latest.get("total_pnl_usd", 0))
    curr_pnlp= safe_float(ps.get("total_pnl_pct") or latest.get("total_pnl_pct", 0))
    cash     = safe_float(ps.get("cash") or latest.get("cash", 0))
    cash_pct = round((cash / curr_cap * 100), 1) if curr_cap > 0 else 0

    # Week change
    wk_chg_usd, wk_chg_pct = week_change_from_history(history, curr_cap)
    spy_week = get_spy_week_return()

    # Drawdown
    drawdown = safe_float(peak.get("drawdown_pct", 0))
    dd_mode  = peak.get("mode", "NORMAL")

    sign  = lambda x: "+" if x >= 0 else ""
    arrow = lambda x: "📈" if x >= 0 else "📉"

    # ── SECTION 1: HEADER ─────────────────────────────────────
    msg = (
        f"📋 <b>Weekly Summary — {week_str}</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 <b>PORTFOLIO</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Total Capital:  <b>${curr_cap:,.2f}</b>\n"
        f"Total P&amp;L:      {sign(curr_pnl)}${abs(curr_pnl):,.2f}  ({sign(curr_pnlp)}{curr_pnlp:.2f}%)\n"
        f"Cash:           ${cash:,.2f}  ({cash_pct}%)\n"
        f"Drawdown:       {drawdown:.1f}%  {'🟢' if dd_mode=='NORMAL' else '🟡' if dd_mode=='DEFENSIVE' else '🔴'}\n"
    )

    # ── SECTION 2: WEEK PERFORMANCE ───────────────────────────
    msg += f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"{arrow(wk_chg_pct or 0)} <b>THIS WEEK</b>\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    if wk_chg_pct is not None:
        alpha = round((wk_chg_pct or 0) - (spy_week or 0), 2) if spy_week is not None else None
        msg += (
            f"Portfolio:  {sign(wk_chg_pct)}{wk_chg_pct:.2f}%  "
            f"({sign(wk_chg_usd)}${abs(wk_chg_usd):,.0f})\n"
        )
        msg += f"S&amp;P 500:    {(sign(spy_week)+str(spy_week)+'%') if spy_week is not None else '—'}\n"
        if alpha is not None:
            msg += f"Alpha:      {sign(alpha)}{alpha:.2f}%\n"
    else:
        msg += "No prior week data yet\n"

    # ── SECTION 3: ALL HOLDINGS ───────────────────────────────
    holdings = latest.get("holdings", [])
    # Target and stop live in the watchlist (carried in market_data.json),
    # not in Claude's holdings list — without this both always showed "—".
    md = {s.get("ticker"): s for s in (load_json(os.path.join(BASE_DIR, "data", "market_data.json")) or {}).get("stocks", [])}
    for h in holdings:
        src = md.get(h.get("ticker"), {})
        h.setdefault("target", src.get("target"))
        h.setdefault("stop_loss", src.get("stop_loss"))
    msg += f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"🏦 <b>HOLDINGS  ({len(holdings)})</b>\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += "<i>P&amp;L% · Price · Value · To target · To stop</i>\n\n"

    if holdings:
        sorted_h = sorted(holdings, key=lambda h: safe_float(h.get("pnl_pct", 0)), reverse=True)
        for h in sorted_h:
            msg += format_holding_row(h, clock) + "\n\n"
    else:
        msg += "No holdings found\n"

    # ── SECTION 4: STOP CLOCK ALERTS ─────────────────────────
    clocked = {t: c for t, c in clock.items()
               if any(x in c.get("status", "") for x in ("L1", "L2", "EARLY"))}
    if clocked:
        msg += f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"⏰ <b>STOP CLOCKS ACTIVE</b>\n"
        msg += f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        for ticker, c in clocked.items():
            stat    = c.get("status", "")
            drift   = safe_float(c.get("drift_pct", 0))
            days    = c.get("days_at_level", c.get("l1_days", c.get("l2_days", "?")))
            level   = "L2 🔴" if "L2" in stat else "L1 🟡" if "L1" in stat else "Early ⚠️"
            msg += f"  <b>{ticker}</b>  {level}  {drift:+.1f}%  ({days} days)\n"
        msg += "\n"

    # ── SECTION 5: ACTIVE PRICE ALERTS ────────────────────────
    active_alerts = get_active_alerts(alert_state)
    if active_alerts:
        msg += f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"🔔 <b>PRICE ALERTS ACTIVE</b>\n"
        msg += f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        msg += "\n".join(active_alerts) + "\n\n"

    # ── SECTION 6: TOP AGENT PICKS ────────────────────────────
    picks = latest.get("agent_picks", [])[:3]
    msg += f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"🎯 <b>TOP PICKS THIS WEEK  ({len(latest.get('agent_picks',[]))} qualified)</b>\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    if picks:
        for p in picks:
            ticker  = p.get("ticker", "—")
            score   = safe_float(p.get("strategy_score", p.get("score", 0)))
            entry   = p.get("suggested_entry", "—")
            target  = p.get("target_price", p.get("target", "—"))
            action  = p.get("action", "BUY")
            msg += f"  <b>{ticker}</b>  Score:{score:.0f}  Entry:{entry}  🎯{target}  [{action}]\n"
    else:
        msg += "  No picks qualified this week\n"

    # ── SECTION 7: EARNINGS THIS WEEK ─────────────────────────
    earnings_upcoming = get_earnings_this_week(latest)
    if earnings_upcoming:
        msg += f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"📅 <b>EARNINGS THIS WEEK</b>\n"
        msg += f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        msg += "\n".join(earnings_upcoming) + "\n"

    # ── FOOTER ────────────────────────────────────────────────
    msg += (
        f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ Not financial advice.\n"
        f"Market Agent V4.5  ·  {now.strftime('%H:%M')} GST"
    )

    print(msg)
    success = send_telegram(msg)
    if success:
        print(f"\n{GREEN}✓ Weekly summary sent to Telegram{RESET}")
    else:
        print(f"\n{RED}✗ Weekly summary failed to send{RESET}")


if __name__ == "__main__":
    run()
