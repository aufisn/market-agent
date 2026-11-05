# ============================================================
# MARKET AGENT V4.5 — Alert System (Telegram)
# ============================================================

import hashlib
import html
import json
import os
from datetime import datetime
from dotenv import load_dotenv
from common import RED, YELLOW, BLUE, BOLD, RESET, safe_float, send_telegram

load_dotenv()

BASE_DIR         = os.path.dirname(os.path.abspath(__file__))


SIGNAL_EMOJI = {
    "STRONG BUY": "🔥",
    "BUY":        "🟢",
    "SELL":       "🔴",
    "WATCH":      "🟡",
    "HOLD":       "⚪",
    "ADD":        "🟢",
    "TOP-UP":     "➕",
    "TRIM":       "✂️",
}


# ── LOAD LATEST ANALYSIS ─────────────────────────────────────
def load_latest_analysis(path=None):
    if path is None:
        path = os.path.join(BASE_DIR, "logs", "latest.json")
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"{RED}✗ latest.json not found. Run agent_brain.py first.{RESET}")
        return None

# ── SEND TO TELEGRAM ─────────────────────────────────────────
# HTML parse mode everywhere (stop_tracker, briefs and reports already use
# it). This file used legacy Markdown, where a stray "_" or "*" in Claude's
# text makes Telegram reject the whole message.


def esc(x):
    return html.escape(str(x if x is not None else ""), quote=False)


# ── ONE DIGEST PER RUN ────────────────────────────────────────
# Previously: a briefing plus one message per BUY/SELL signal on every
# run (about 7 messages a run). Now one message, most important first.
TELEGRAM_LIMIT = 4096


def action_items(analysis):
    """What needs a decision: rule/model alerts (CRITICAL/WARNING) and SELL signals."""
    items = []
    cb = ((analysis.get("market_regime") or {}).get("exposure") or {}).get("circuit_breaker") or {}
    if cb.get("tripped"):
        items.append(f"🛑 <b>Circuit breaker</b>: {esc(cb.get('note'))}")
    for a in analysis.get("strategy_alerts", []) or []:
        if a.get("severity") in ("CRITICAL", "WARNING"):
            tag = "🚨" if a["severity"] == "CRITICAL" else "⚠️"
            items.append(f"{tag} {esc(a.get('message'))} → <b>{esc(a.get('action'))}</b>")
    flagged = " ".join(items)
    for h in analysis.get("holdings", []) or []:
        if h.get("signal") == "SELL" and str(h.get("ticker")) not in flagged:
            items.append(f"🔴 <b>{esc(h.get('ticker'))}</b> SELL — {esc(h.get('reason', ''))[:160]}")
    return items


def build_digest(analysis):
    mood   = analysis.get("market_mood", "NEUTRAL")
    ps     = analysis.get("portfolio_summary", {})
    cap    = safe_float(ps.get("total_capital", analysis.get("total_capital")))
    hold_v = safe_float(ps.get("total_value", analysis.get("total_value")))
    pnl    = safe_float(ps.get("total_pnl_pct", analysis.get("total_pnl_pct")))
    cash   = safe_float(ps.get("cash", analysis.get("cash")))
    regime = (analysis.get("market_regime") or {}).get("regime")
    mood_e = {"BULLISH": "📈", "BEARISH": "📉", "NEUTRAL": "➡️"}.get(mood, "➡️")
    lines = [f"📡 <b>Market Agent — {datetime.now().strftime('%d %b %Y %H:%M')}</b>",
             f"{mood_e} {esc(mood)}" + (f" · regime {esc(regime)}" if regime else ""),
             f"💼 Total capital <b>${cap:,.0f}</b> · holdings ${hold_v:,.0f} ({pnl:+.1f}% vs cost) · "
             f"cash ${cash:,.0f} ({(cash / cap * 100) if cap else 0:.0f}%)"]
    dq = analysis.get("data_quality") or {}
    if dq.get("stale_tickers"):
        lines.append(f"⚠️ Stale prices: {esc(', '.join(dq['stale_tickers']))}")
    rsi_skipped = [p.get("ticker", "") for p in analysis.get("agent_picks", []) or [] if p.get("rsi_check") == "SKIPPED"]
    if rsi_skipped:
        lines.append(f"⚠️ G5 RSI not checked (no Yahoo history): {esc(', '.join(rsi_skipped))}")

    acts = action_items(analysis)
    lines += ["", "<b>Action needed</b>"] + (acts or ["✅ Nothing urgent"])

    buys = [r for r in analysis.get("reallocation", []) or [] if r.get("scenario", "").startswith("BUY")]
    if buys:
        lines += ["", "<b>New buy ideas</b> (sized within your cash floor)"]
        for r in buys:
            p, d = r.get("pick", {}), r.get("detail", {})
            bc = (p.get("bear_case") or {}).get("strength")
            lines.append(f"🟢 <b>{esc(p.get('ticker'))}</b> {esc(d.get('shares'))} sh ≈ ${safe_float(d.get('position_value')):,.0f} "
                         f"({safe_float(d.get('position_pct')):.1f}%) · entry {esc(p.get('suggested_entry', ''))}"
                         + (f" · bear case {esc(bc)}" if bc else "")
                         + (" · ⚠️ RSI not checked" if p.get("rsi_check") == "SKIPPED" else ""))
    elif analysis.get("agent_picks"):
        lines += ["", f"<b>Picks</b>: {esc(', '.join(p.get('ticker', '') for p in analysis['agent_picks']))} — no buy sized (cash floor/regime)"]

    hl = analysis.get("holdings", []) or []
    if hl:
        lines += ["", "<b>Holdings</b>"]
        for h in hl:
            line = (f"{SIGNAL_EMOJI.get(h.get('signal'), '⚪')} {esc(h.get('ticker'))} {esc(h.get('signal'))} "
                    f"({safe_float(h.get('pnl_pct')):+.1f}%)")
            if h.get("signal") in ("TRIM", "TOP-UP") and h.get("action_detail"):
                line += f" — {esc(h['action_detail'])}"
            lines.append(line)
    au = analysis.get("api_usage") or {}
    if au.get("monthly_cap_usd"):
        lines += ["", f"💳 Claude spend: run ${au.get('run_usd', 0):.2f} · month ${au.get('month_usd', 0):.2f} of ${au['monthly_cap_usd']:.0f}"]
    lines += ["", "<i>Not financial advice</i>"]

    msg = "\n".join(lines)
    if len(msg) > TELEGRAM_LIMIT:
        cut, n = [], 0
        for line in lines:
            if n + len(line) + 1 > TELEGRAM_LIMIT - 80:
                break
            cut.append(line); n += len(line) + 1
        msg = "\n".join(cut + ["", "<i>Truncated — see the dashboard for the rest.</i>"])
    return msg


# ── PROCESS AND SEND ──────────────────────────────────────────
DIGEST_STATE = os.path.join(BASE_DIR, "logs", "alert_digest_state.json")


def process_and_send(analysis, mode="full"):
    if mode == "test":
        print(f"\n{BLUE}Sending test message...{RESET}")
        send_telegram("✅ <b>Market Agent connected!</b>\nTelegram alerts working correctly.")
        return
    if not analysis:
        return

    digest = build_digest(analysis)
    # "full" (the main daily run) always sends. "alerts" (any other run)
    # sends only when the action items changed since the last digest.
    key = hashlib.sha256("\n".join(action_items(analysis)).encode()).hexdigest()
    try:
        last = json.load(open(DIGEST_STATE)).get("action_key")
    except Exception:
        last = None
    if mode == "alerts" and key == last:
        print(f"{YELLOW}Action items unchanged since the last digest — not re-sending{RESET}")
        return
    print(f"\n{BLUE}Sending digest...{RESET}")
    if send_telegram(digest):
        os.makedirs(os.path.dirname(DIGEST_STATE), exist_ok=True)
        json.dump({"action_key": key, "sent_at": datetime.now().isoformat()}, open(DIGEST_STATE, "w"))


# ── MAIN ─────────────────────────────────────────────────────
def run(mode="full"):
    print(f"\n{BOLD}{BLUE}{'='*50}")
    print(f"  MARKET AGENT V4.5 — Alerts")
    print(f"  {datetime.now().strftime('%A %d %B %Y  %H:%M')}")
    print(f"{'='*50}{RESET}\n")

    if mode == "test":
        process_and_send(None, mode="test")
        return

    analysis = load_latest_analysis()
    process_and_send(analysis, mode=mode)

if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    run(mode=mode)
