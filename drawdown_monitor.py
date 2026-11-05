# ============================================================
# MARKET AGENT V4.5 — Drawdown Monitor
# Tracks portfolio peak value and fires Telegram alerts when
# drawdown exceeds thresholds. Runs after every fetch_data.py.
# ============================================================

import json
import os
from datetime import datetime
from dotenv import load_dotenv
from common import GREEN, RED, YELLOW, BLUE, BOLD, RESET, safe_float, send_telegram

load_dotenv()
BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
PEAK_FILE = os.path.join(BASE_DIR, "logs", "portfolio_peak.json")
DATA_FILE = os.path.join(BASE_DIR, "data", "market_data.json")


# Drawdown thresholds
WARN_PCT     = 10.0   # Warning alert
CRITICAL_PCT = 15.0   # Critical alert — triggers defensive mode flag
RECOVER_PCT  =  5.0   # If drawdown recovers to within this, clear alert


def load_peak():
    """Load the stored portfolio peak record"""
    if not os.path.exists(PEAK_FILE):
        return None
    try:
        with open(PEAK_FILE) as f:
            return json.load(f)
    except Exception:
        return None

def save_peak(peak_val, peak_date, current_val, drawdown_pct,
              alert_sent_warn=False, alert_sent_critical=False, mode="NORMAL"):
    os.makedirs(os.path.dirname(PEAK_FILE), exist_ok=True)
    data = {
        "peak_value":           round(peak_val, 2),
        "peak_date":            peak_date,
        "last_value":           round(current_val, 2),
        "last_checked":         datetime.now().isoformat(),
        "drawdown_pct":         round(drawdown_pct, 2),
        "alert_sent_warn":      alert_sent_warn,
        "alert_sent_critical":  alert_sent_critical,
        "mode":                 mode   # NORMAL | DEFENSIVE | CRITICAL
    }
    with open(PEAK_FILE, "w") as f:
        json.dump(data, f, indent=2)
    return data

def run():
    print(f"\n{BOLD}{BLUE}{'='*55}")
    print(f"  DRAWDOWN MONITOR")
    print(f"  {datetime.now().strftime('%A %d %B %Y  %H:%M')}")
    print(f"{'='*55}{RESET}\n")

    # Load current portfolio value
    if not os.path.exists(DATA_FILE):
        print(f"{YELLOW}⚠ market_data.json not found — skipping{RESET}")
        return {"mode": "NORMAL", "drawdown_pct": 0}

    try:
        with open(DATA_FILE) as f:
            market_data = json.load(f)
    except Exception as e:
        print(f"{YELLOW}⚠ Could not read market_data.json: {e}{RESET}")
        return {"mode": "NORMAL", "drawdown_pct": 0}

    current_val = safe_float(market_data.get("total_capital") or
                             market_data.get("total_value") or 0)
    if current_val <= 0:
        print(f"{YELLOW}⚠ No valid portfolio value found — skipping{RESET}")
        return {"mode": "NORMAL", "drawdown_pct": 0}

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Load or initialise peak
    peak = load_peak()
    if peak is None:
        # First run — set peak and exit
        save_peak(current_val, now_str, current_val, 0.0)
        print(f"{GREEN}✓ Peak initialised: ${current_val:,.2f}{RESET}")
        return {"mode": "NORMAL", "drawdown_pct": 0}

    peak_val   = safe_float(peak.get("peak_value", current_val))
    peak_date  = peak.get("peak_date", now_str)
    alert_warn = peak.get("alert_sent_warn", False)
    alert_crit = peak.get("alert_sent_critical", False)
    prev_mode  = peak.get("mode", "NORMAL")

    # Update peak if new high
    if current_val > peak_val:
        print(f"{GREEN}✓ New portfolio high: ${current_val:,.2f} (prev: ${peak_val:,.2f}){RESET}")
        save_peak(current_val, now_str, current_val, 0.0,
                  False, False, "NORMAL")
        # Send recovery message if previously in drawdown
        if prev_mode in ("DEFENSIVE", "CRITICAL"):
            send_telegram(
                f"✅ <b>Portfolio Recovery — New High</b>\n\n"
                f"Portfolio has recovered to a new peak of <b>${current_val:,.2f}</b>\n"
                f"Previous peak was ${peak_val:,.2f} on {peak_date}\n\n"
                f"Defensive mode lifted — normal strategy rules resume."
            )
        return {"mode": "NORMAL", "drawdown_pct": 0, "peak_value": current_val}

    # Calculate drawdown
    drawdown_pct = ((peak_val - current_val) / peak_val) * 100
    print(f"  Current:  ${current_val:,.2f}")
    print(f"  Peak:     ${peak_val:,.2f}  ({peak_date})")
    print(f"  Drawdown: {drawdown_pct:.1f}%")

    # Determine mode and alerts
    mode = "NORMAL"

    if drawdown_pct >= CRITICAL_PCT:
        mode = "CRITICAL"
        if not alert_crit:
            # Send critical alert
            loss_usd = peak_val - current_val
            send_telegram(
                f"🚨 <b>CRITICAL DRAWDOWN ALERT</b>\n\n"
                f"Portfolio has fallen <b>{drawdown_pct:.1f}%</b> from its peak\n\n"
                f"📉 Peak value:    <b>${peak_val:,.2f}</b>  ({peak_date})\n"
                f"📉 Current value: <b>${current_val:,.2f}</b>\n"
                f"📉 Loss from peak: <b>${loss_usd:,.2f}</b>\n\n"
                f"⚠️ <b>DEFENSIVE MODE ACTIVE</b>\n"
                f"• No new BUY positions until drawdown recovers below {RECOVER_PCT}%\n"
                f"• Review all stop-loss levels immediately\n"
                f"• Consider raising cash above 20%\n\n"
                f"This alert will not repeat until portfolio recovers."
            )
            alert_crit = True
            alert_warn = True  # warn already implicit
            print(f"{RED}🚨 CRITICAL drawdown alert sent{RESET}")
        else:
            print(f"{RED}🚨 CRITICAL drawdown: {drawdown_pct:.1f}% (alert already sent){RESET}")

    elif drawdown_pct >= WARN_PCT:
        mode = "DEFENSIVE"
        if not alert_warn:
            loss_usd = peak_val - current_val
            send_telegram(
                f"⚠️ <b>Drawdown Warning</b>\n\n"
                f"Portfolio has fallen <b>{drawdown_pct:.1f}%</b> from its peak\n\n"
                f"📉 Peak value:    <b>${peak_val:,.2f}</b>  ({peak_date})\n"
                f"📉 Current value: <b>${current_val:,.2f}</b>\n"
                f"📉 Loss from peak: <b>${loss_usd:,.2f}</b>\n\n"
                f"Monitoring closely. A further drop to {CRITICAL_PCT}% will trigger "
                f"defensive mode and a critical alert.\n\n"
                f"Review holdings and stop-loss levels."
            )
            alert_warn = True
            print(f"{YELLOW}⚠ WARNING drawdown alert sent{RESET}")
        else:
            print(f"{YELLOW}⚠ Warning drawdown: {drawdown_pct:.1f}% (alert already sent){RESET}")

    else:
        mode = "NORMAL"
        print(f"{GREEN}✓ Drawdown within normal range: {drawdown_pct:.1f}%{RESET}")

    # Save updated peak record
    save_peak(peak_val, peak_date, current_val, drawdown_pct,
              alert_warn, alert_crit, mode)

    print(f"\n  Mode: {mode}  |  Drawdown: {drawdown_pct:.1f}%  |  Peak: ${peak_val:,.2f}\n")
    return {
        "mode":          mode,
        "drawdown_pct":  round(drawdown_pct, 2),
        "peak_value":    peak_val,
        "peak_date":     peak_date,
        "current_value": current_val,
        "loss_usd":      round(peak_val - current_val, 2)
    }

if __name__ == "__main__":
    result = run()
    print(f"Result: {result}")
