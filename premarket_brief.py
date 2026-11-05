#!/usr/bin/env python3
# ============================================================
# Market Agent V4.5 — Pre-Market Brief
# premarket_brief.py
#
# Reads logs/premarket_brief.json (written by premarket_scanner.py)
# Formats and sends a Telegram message with:
#   🚨 URGENT     — confirmed stops, act at open
#   ⚡ OPPORTUNITY — new entries worth considering
#   ⚠️ REVISED     — adjusted limit prices
#   ❌ WITHDRAWN   — recs no longer valid
#   ✅ CONFIRMED   — yesterday's recs still valid
#
# Silent if market is closed (sends holiday notice instead)
# Silent if no meaningful pre-market activity
# ============================================================

import json
import os
import sys
from datetime import datetime
from dotenv import load_dotenv
from common import RED, YELLOW, BLUE, BOLD, RESET, load_json, send_telegram

load_dotenv()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


BRIEF_PATH = os.path.join(BASE_DIR, "logs", "premarket_brief.json")


def format_change(pct):
    """Format a % change with arrow and sign."""
    if pct is None:
        return ""
    arrow = "▲" if pct > 0 else "▼"
    return f"{arrow} {pct:+.1f}%"


def build_market_closed_message(reason):
    """Build a brief Telegram message for market closed days."""
    today = datetime.now().strftime("%A %d %b")
    return (
        f"🌅 <b>PRE-MARKET BRIEF</b> — {today}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📅 <b>No Market Data Available</b>\n"
        f"{reason}\n\n"
        f"<i>Pre-market scan skipped. Next run on the next trading day.</i>"
    )


def build_brief_message(brief):
    """Build the full pre-market Telegram message from brief data."""
    today      = datetime.now().strftime("%A %d %b")
    analysis   = brief.get("claude_analysis") or {}
    # The regime detector's own label (step 4), not Claude's echo of it.
    regime_lbl = ((load_json(os.path.join(BASE_DIR, "data", "market_regime.json")) or {}).get("regime")
                  or analysis.get("regime") or "UNKNOWN")

    # Regime badge
    regime_icons = {
        "BULL_TREND": "📈", "BULL": "📈",
        "CHOPPY": "↔️",
        "BEAR_TREND": "📉", "BEAR": "📉",
        "CRISIS": "🔴",
    }
    regime_icon = regime_icons.get(regime_lbl, "❓")

    lines = []
    lines.append(f"🌅 <b>PRE-MARKET BRIEF</b> — {today}")
    lines.append(f"━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"Regime: {regime_icon} <b>{regime_lbl}</b>  |  Market opens ~1hr")
    lines.append("")

    validated         = brief.get("validated", [])
    opportunity_cands = brief.get("opportunity_candidates", [])
    claude_ops        = analysis.get("opportunities", [])
    claude_urgent     = analysis.get("urgent", [])
    claude_withdrawn  = analysis.get("withdrawn", [])
    claude_revised    = analysis.get("revised", [])
    summary           = analysis.get("summary", "")

    has_content = False

    # ══════════════════════════════════════════════════════════
    # GROUP 1 — STATUS OF RECOMMENDATIONS
    # Everything here is tied to an actual position/recommendation
    # from yesterday (validate_recommendations' output). This ALWAYS
    # includes every active recommendation now, even quiet HOLD-type
    # ones — a stock sitting flat with no active signal used to be
    # silently omitted entirely, which is exactly the gap being fixed.
    # ══════════════════════════════════════════════════════════
    status_lines = []

    urgent_items = [x for x in validated if x["status"] == "URGENT"]
    if urgent_items:
        status_lines.append("🚨 <b>URGENT — Act at Open</b>")
        for item in urgent_items:
            chg_str = format_change(item.get("pm_change"))
            price   = item.get("pm_price", "")
            claude_u = next((c for c in claude_urgent if c.get("ticker") == item["ticker"]), None)
            limit_str = f"  └ Limit sell: <b>${claude_u['limit_price']}</b>" if claude_u and claude_u.get("limit_price") else ""
            rationale = claude_u.get("rationale", item.get("message", "")) if claude_u else item.get("message", "")
            status_lines.append(f"  <b>{item['ticker']}</b> {chg_str}  ${price}")
            if limit_str:
                status_lines.append(limit_str)
            status_lines.append(f"  └ {rationale}")
        status_lines.append("")

    # Only recommendations that gapped down from an EXISTING BUY signal
    # — genuinely new candidates (real_opps) now live under Big Movers
    # instead, since they were never actually recommended yesterday.
    opp_validated = [x for x in validated if x["status"] == "OPPORTUNITY"]
    if opp_validated:
        status_lines.append("⚡ <b>OPPORTUNITY</b>")
        for item in opp_validated:
            chg_str   = format_change(item.get("pm_change"))
            price     = item.get("pm_price", "")
            claude_rev = next((c for c in claude_revised if c.get("ticker") == item["ticker"]), None)
            limit     = claude_rev.get("revised_limit") if claude_rev else item.get("revised_limit")
            act_now   = claude_rev.get("act_now", False) if claude_rev else False
            act_label = "Act pre-mkt" if act_now else "Wait for open"
            limit_str = f"  └ Limit: <b>${limit}</b>  ({act_label})" if limit else ""
            status_lines.append(f"  <b>{item['ticker']}</b> {chg_str}  ${price}")
            if limit_str:
                status_lines.append(limit_str)
            if item.get("message"):
                status_lines.append(f"  └ {item['message']}")
        status_lines.append("")

    revised_items = [x for x in validated if x["status"] == "REVISED"]
    if revised_items:
        status_lines.append("⚠️ <b>REVISED</b>")
        for item in revised_items:
            chg_str   = format_change(item.get("pm_change"))
            price     = item.get("pm_price", "")
            claude_r  = next((c for c in claude_revised if c.get("ticker") == item["ticker"]), None)
            new_limit = claude_r.get("revised_limit") if claude_r else item.get("revised_limit")
            rationale = claude_r.get("rationale", item.get("message", "")) if claude_r else item.get("message", "")
            act_now   = claude_r.get("act_now", False) if claude_r else False
            timing    = "Act now" if act_now else "Wait for open"
            status_lines.append(f"  <b>{item['ticker']}</b> {chg_str}  ${price}")
            if new_limit:
                status_lines.append(f"  └ Adjusted limit: <b>${new_limit}</b>  ({timing})")
            status_lines.append(f"  └ {rationale}")
        status_lines.append("")

    withdrawn_items = [x for x in validated if x["status"] == "WITHDRAWN"]
    if withdrawn_items:
        status_lines.append("❌ <b>WITHDRAWN</b>")
        for item in withdrawn_items:
            chg_str   = format_change(item.get("pm_change"))
            price     = item.get("pm_price", "")
            claude_w  = next((c for c in claude_withdrawn if c.get("ticker") == item["ticker"]), None)
            watch_for = claude_w.get("watch_for", "") if claude_w else ""
            instruction = claude_w.get("instruction", item.get("message", "")) if claude_w else item.get("message", "")
            status_lines.append(f"  <b>{item['ticker']}</b> {chg_str}  ${price}")
            status_lines.append(f"  └ {instruction}")
            if watch_for:
                status_lines.append(f"  └ Watch: {watch_for}")
        status_lines.append("")

    confirmed_items = [x for x in validated if x["status"] == "CONFIRMED"]
    if confirmed_items:
        status_lines.append("✅ <b>CONFIRMED</b>")
        for item in confirmed_items:
            chg_str = format_change(item.get("pm_change"))
            price   = item.get("pm_price", "")
            status_lines.append(f"  <b>{item['ticker']}</b> {chg_str}  ${price}  — {item['action']}")
        status_lines.append("")

    # WATCH here is only ever the rec_map-tied kind (a HOLD-type
    # position with a notable move) — NOT the new-candidate kind,
    # which now lives entirely under Big Movers below.
    watch_items = [x for x in validated if x["status"] == "WATCH"]
    if watch_items:
        status_lines.append("👁 <b>WATCH</b>")
        for item in watch_items[:4]:
            chg_str = format_change(item.get("pm_change"))
            price   = item.get("pm_price", "")
            msg     = item.get("message", "")
            status_lines.append(f"  <b>{item['ticker']}</b> {chg_str}  ${price}" +
                                 (f"  — {msg}" if msg else ""))
        status_lines.append("")

    if status_lines:
        has_content = True
        lines.append("📋 <b>STATUS OF RECOMMENDATIONS</b>")
        lines.append("──────────────────────")
        lines.extend(status_lines)

    # ══════════════════════════════════════════════════════════
    # GROUP 2 — BIG MOVERS
    # Genuinely new candidates from the sector-leader scan — stocks
    # NOT tied to any existing recommendation, moving enough to be
    # worth a look (scan_opportunities' output, classified by Claude).
    # ══════════════════════════════════════════════════════════
    movers_lines = []

    real_opps = [c for c in claude_ops if c.get("classification") == "OPPORTUNITY"]
    if real_opps:
        movers_lines.append("⚡ <b>OPPORTUNITY</b>")
        for opp in real_opps[:4]:
            cand      = next((c for c in opportunity_cands if c["ticker"] == opp["ticker"]), {})
            chg_str   = format_change(cand.get("pm_change"))
            price     = cand.get("pm_price", "")
            limit     = opp.get("limit_price")
            act_now   = opp.get("action") == "BUY"
            act_label = "Act pre-mkt" if act_now else "Wait for open"
            limit_str = f"  └ Limit: <b>${limit}</b>  ({act_label})" if limit else ""
            movers_lines.append(f"  <b>{opp['ticker']}</b> {chg_str}  ${price}")
            if limit_str:
                movers_lines.append(limit_str)
            if opp.get("rationale"):
                movers_lines.append(f"  └ {opp['rationale']}")
        movers_lines.append("")

    watch_opps = [c for c in claude_ops if c.get("classification") == "WATCH"]
    if watch_opps:
        movers_lines.append("👁 <b>WATCH</b>")
        for wo in watch_opps[:4]:
            cand    = next((c for c in opportunity_cands if c["ticker"] == wo["ticker"]), {})
            chg_str = format_change(cand.get("pm_change"))
            price   = cand.get("pm_price", "")
            msg     = wo.get("rationale", "")
            movers_lines.append(f"  <b>{wo['ticker']}</b> {chg_str}  ${price}" +
                                 (f"  — {msg}" if msg else ""))
        movers_lines.append("")

    if movers_lines:
        has_content = True
        lines.append("📈 <b>BIG MOVERS</b>")
        lines.append("──────────────────────")
        lines.extend(movers_lines)

    # ── Summary ───────────────────────────────────────────────
    if summary:
        lines.append(f"<i>{summary}</i>")
        lines.append("")

    if not has_content:
        lines.append("📊 <b>No significant pre-market moves</b>")
        lines.append("<i>All holdings quiet. No action required before open.</i>")

    lines.append(f"<i>Market Agent V4.5 · {datetime.now().strftime('%H:%M')} GST</i>")

    message = "\n".join(lines)

    # Telegram's hard limit is 4096 chars. A typical brief (even with the
    # widened per-item limits above) runs well under this, so this is a
    # rare last-resort safety net, not the normal path — truncates at a
    # clean LINE boundary (never mid-sentence) and says so explicitly.
    TELEGRAM_LIMIT = 4096
    if len(message) > TELEGRAM_LIMIT:
        budget = TELEGRAM_LIMIT - 60  # leave room for the notice below
        cut_lines = []
        running_len = 0
        for line in lines:
            if running_len + len(line) + 1 > budget:
                break
            cut_lines.append(line)
            running_len += len(line) + 1
        cut_lines.append("")
        cut_lines.append("<i>⚠️ Brief truncated — too long for one Telegram message. See dashboard for the full picture.</i>")
        message = "\n".join(cut_lines)

    return message


def run():
    print(f"\n{BOLD}{BLUE}{'='*55}")
    print(f"  Pre-Market Brief")
    print(f"  {datetime.now().strftime('%A %d %B %Y  %H:%M')}")
    print(f"{'='*55}{RESET}\n")

    # Load brief data
    if not os.path.exists(BRIEF_PATH):
        print(f"{RED}✗ logs/premarket_brief.json not found — run premarket_scanner.py first{RESET}")
        sys.exit(1)

    try:
        with open(BRIEF_PATH) as f:
            brief = json.load(f)
    except Exception as e:
        print(f"{RED}✗ Could not read brief: {e}{RESET}")
        sys.exit(1)

    # Market closed path
    if not brief.get("market_open", True):
        reason = brief.get("market_closed_reason", "Market closed")
        print(f"{YELLOW}⏭  {reason} — sending closed notice{RESET}")
        message = build_market_closed_message(reason)
        send_telegram(message)
        return

    # Build and send message
    print(f"{BLUE}Building Telegram message...{RESET}")
    message = build_brief_message(brief)

    print(f"\n{BLUE}--- Message Preview ---{RESET}")
    # Strip HTML for preview
    preview = message.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")
    print(preview[:800])
    print(f"{BLUE}--- End Preview ---{RESET}\n")

    success = send_telegram(message)
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    run()
