# ============================================================
# Market Agent V4.5 — Earnings Pre-Brief
# Checks each holding for earnings within 3 days.
# Calls Claude API for a HOLD/REDUCE/TIGHT_STOP recommendation.
# Sends Telegram brief the night before earnings.
# Called by: run_agent.sh Step 1e (daily)
# ============================================================

import os
import json
from datetime import datetime
from dotenv import load_dotenv
import anthropic
from claude_json import ask_json, StructuredReplyError
from common import GREEN, YELLOW, BLUE, BOLD, RESET, safe_float, load_json, send_telegram

load_dotenv()

BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "recommendation":   {"type": "string", "enum": ["HOLD", "REDUCE", "TIGHT_STOP"]},
        "confidence":       {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
        "reasoning":        {"type": "string"},
        "key_risk":         {"type": "string"},
        "suggested_action": {"type": "string"},
    },
    "required": ["recommendation", "confidence", "reasoning", "key_risk", "suggested_action"],
}

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
LATEST_FILE     = os.path.join(BASE_DIR, "logs", "latest.json")
BRIEFS_SENT     = os.path.join(BASE_DIR, "logs", "earnings_briefs_sent.json")


def load_briefs_sent():
    return load_json(BRIEFS_SENT) or {}


def save_briefs_sent(state):
    os.makedirs(os.path.dirname(BRIEFS_SENT), exist_ok=True)
    with open(BRIEFS_SENT, "w") as f:
        json.dump(state, f, indent=2)


def get_recent_price_action(ticker, days=10):
    """Get recent price action summary for the Claude prompt"""
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period=f"{days+5}d", auto_adjust=True)
        if hist.empty or len(hist) < 3:
            return None
        closes   = hist["Close"].dropna()
        price    = float(closes.iloc[-1])
        price_5d = float(closes.iloc[-5]) if len(closes) >= 5 else price
        price_10d= float(closes.iloc[-10]) if len(closes) >= 10 else price
        chg_5d   = round(((price - price_5d)  / price_5d)  * 100, 2)
        chg_10d  = round(((price - price_10d) / price_10d) * 100, 2)
        vol      = hist["Volume"].iloc[-1]
        avg_vol  = hist["Volume"].iloc[-10:].mean()
        vol_ratio= round(vol / avg_vol, 2) if avg_vol > 0 else 1.0
        return {
            "price":      round(price, 2),
            "chg_5d":     chg_5d,
            "chg_10d":    chg_10d,
            "vol_ratio":  vol_ratio,
        }
    except Exception:
        return None


def call_claude_for_brief(holding, price_action, days_to_earnings):
    """Call Claude API for earnings recommendation"""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print(f"{YELLOW}⚠ No ANTHROPIC_API_KEY{RESET}")
        return None

    ticker   = holding.get("ticker", "")
    name     = holding.get("name", ticker)
    qty      = safe_float(holding.get("quantity", 0))
    buy_price= safe_float(holding.get("buy_price", 0))
    target   = safe_float(holding.get("target", 0))
    stop     = safe_float(holding.get("stop_loss", 0))
    pnl_pct  = safe_float(holding.get("pnl_pct", 0))
    pnl_usd  = safe_float(holding.get("pnl_usd", 0))
    weight   = safe_float(holding.get("portfolio_weight", 0))
    reason   = holding.get("reason", "")

    pa = price_action or {}
    price    = pa.get("price", buy_price)
    chg_5d   = pa.get("chg_5d", 0)
    chg_10d  = pa.get("chg_10d", 0)
    vol_ratio= pa.get("vol_ratio", 1.0)

    sign = lambda x: "+" if x >= 0 else ""

    prompt = f"""You are a portfolio risk advisor. A holding is reporting earnings in {days_to_earnings} day(s).

POSITION DETAILS:
- Ticker: {ticker} ({name})
- Shares: {qty:.0f}  |  Portfolio weight: {weight:.1f}%
- Buy price: ${buy_price:.2f}  |  Current: ${price:.2f}
- P&L: {sign(pnl_pct)}{pnl_pct:.1f}% ({sign(pnl_usd)}${abs(pnl_usd):,.0f})
- Target: ${target:.2f}  |  Stop loss: ${stop:.2f}
- Investment thesis: {reason}

RECENT PRICE ACTION:
- 5-day change: {sign(chg_5d)}{chg_5d:.1f}%
- 10-day change: {sign(chg_10d)}{chg_10d:.1f}%
- Volume ratio vs avg: {vol_ratio:.1f}x {"(elevated — possible positioning)" if vol_ratio > 1.4 else ""}

TASK:
Based on this position and the upcoming earnings, provide a concise pre-earnings brief.
Respond ONLY with a JSON object — no markdown, no preamble:

{{
  "recommendation": "HOLD" | "REDUCE" | "TIGHT_STOP",
  "confidence": "HIGH" | "MEDIUM" | "LOW",
  "reasoning": "2-3 sentences explaining the recommendation",
  "key_risk": "The single biggest risk into earnings",
  "suggested_action": "One specific action if any (e.g. 'Set stop at $X', 'Trim 30% before close', 'Hold full position')"
}}

HOLD = maintain current position through earnings
REDUCE = trim position size before earnings to limit exposure
TIGHT_STOP = hold but set a tighter intraday stop for earnings day"""

    try:
        # Same structured-reply path (and spend tracking) as every other
        # Claude call — this used to be a raw HTTP request that bypassed both.
        client = anthropic.Anthropic(api_key=api_key)
        return ask_json(client, prompt, max_tokens=600, label=f"earnings_brief_{ticker}", schema=BRIEF_SCHEMA)
    except StructuredReplyError as e:
        print(f"{YELLOW}⚠ Claude brief reply error: {e}{RESET}")
        return None
    except Exception as e:
        print(f"{YELLOW}⚠ Claude brief error: {e}{RESET}")
        return None


def format_brief_message(holding, brief, days_to_earnings, price_action):
    ticker  = holding.get("ticker", "")
    name    = holding.get("name", ticker)
    qty     = safe_float(holding.get("quantity", 0))
    pnl_pct = safe_float(holding.get("pnl_pct", 0))
    weight  = safe_float(holding.get("portfolio_weight", 0))
    sign    = lambda x: "+" if x >= 0 else ""

    rec     = brief.get("recommendation", "HOLD")
    conf    = brief.get("confidence", "MEDIUM")
    reason  = brief.get("reasoning", "")
    risk    = brief.get("key_risk", "")
    action  = brief.get("suggested_action", "")

    rec_emoji = {"HOLD": "🟢", "REDUCE": "🟡", "TIGHT_STOP": "🔴"}.get(rec, "⚪")
    conf_color= {"HIGH": "strong", "MEDIUM": "moderate", "LOW": "low"}.get(conf, "moderate")

    timing = "tomorrow" if days_to_earnings == 1 else f"in {days_to_earnings} days"

    msg = (
        f"📅 <b>Earnings Brief — {ticker}</b>\n"
        f"Reports {timing}\n\n"
        f"{rec_emoji} <b>{rec}</b>  <i>({conf_color} conviction)</i>\n\n"
        f"<b>Position:</b> {qty:.0f} shares · {weight:.1f}% of portfolio · "
        f"{sign(pnl_pct)}{pnl_pct:.1f}% P&amp;L\n\n"
        f"<b>Analysis:</b>\n{reason}\n\n"
        f"<b>Key risk:</b> {risk}\n\n"
        f"<b>Action:</b> {action}\n\n"
        f"<i>{name} · Market Agent V4.5</i>"
    )
    return msg


def run():
    print(f"\n{BOLD}{BLUE}── Earnings Brief ──────────────────────────────{RESET}")

    latest   = load_json(LATEST_FILE)
    if not latest:
        print(f"{YELLOW}⚠ No latest.json — skipping earnings brief{RESET}")
        return

    holdings = latest.get("holdings", [])
    earnings = latest.get("earnings", [])
    if not holdings:
        print("  No holdings to check")
        return

    # Build earnings date map for holdings
    holding_tickers = {h.get("ticker", "").upper() for h in holdings}
    now             = datetime.now()
    briefs_sent     = load_briefs_sent()
    sent_any        = False

    # Check earnings list from market context
    for e in earnings:
        ticker = str(e.get("ticker", "")).upper()
        if ticker not in holding_tickers:
            continue

        date_str = e.get("date", "") or e.get("earnings_date", "")
        if not date_str:
            continue

        # latest.json stores "30 Jul 2026"; older data used "2026-07-30".
        # Parsing only the second format made every date fail silently, so
        # this brief could never fire.
        ed = None
        for fmt in ("%d %b %Y", "%Y-%m-%d"):
            try:
                ed = datetime.strptime(str(date_str).strip()[:11].strip(), fmt)
                break
            except ValueError:
                continue
        if ed is None:
            print(f"{YELLOW}⚠ Unrecognised earnings date for {ticker}: {date_str!r}{RESET}")
            continue

        days_away = (ed.date() - now.date()).days
        if days_away < 0 or days_away > 3:
            continue

        # Check if we already sent a brief for this ticker/date combo
        brief_key = f"{ticker}_{ed.strftime('%Y-%m-%d')}"
        if briefs_sent.get(brief_key):
            print(f"  {ticker}: brief already sent for {date_str[:10]} — skipping")
            continue

        print(f"  {ticker}: earnings in {days_away} day(s) — generating brief...")

        # Get holding details
        holding = next((h for h in holdings if h.get("ticker","").upper() == ticker), None)
        if not holding:
            continue

        # Get price action
        price_action = get_recent_price_action(ticker)

        # Call Claude
        brief = call_claude_for_brief(holding, price_action, days_away)
        if not brief:
            print(f"  {YELLOW}⚠ Could not generate brief for {ticker}{RESET}")
            continue

        # Send Telegram
        msg     = format_brief_message(holding, brief, days_away, price_action)
        success = send_telegram(msg)

        if success:
            briefs_sent[brief_key] = {
                "sent_at":     now.isoformat(),
                "days_away":   days_away,
                "rec":         brief.get("recommendation"),
            }
            sent_any = True
            print(f"  {GREEN}✓ {ticker}: brief sent — {brief.get('recommendation')}{RESET}")
        else:
            print(f"  {YELLOW}⚠ {ticker}: brief generated but Telegram failed{RESET}")

    if sent_any:
        save_briefs_sent(briefs_sent)
    else:
        print("  No earnings within 3 days for current holdings")


if __name__ == "__main__":
    run()
