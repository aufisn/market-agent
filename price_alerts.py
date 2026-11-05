# ============================================================
# MARKET AGENT V4.5 — Phase 3: Price Threshold Alerts
# Runs every 15 minutes during market hours
# Fires Telegram instantly when price crosses a threshold
# ============================================================

import json
import html
import os
import yfinance as yf
from datetime import datetime
from dotenv import load_dotenv
from common import GREEN, YELLOW, BLUE, BOLD, RESET, safe_float, send_telegram

load_dotenv()
BASE_DIR         = os.path.dirname(os.path.abspath(__file__))


FIRED_LOG = os.path.join(BASE_DIR, "logs", "alerts_fired.json")


# ── IS MARKET OPEN? ───────────────────────────────────────────
def is_market_hours():
    """Check if US market is open (Mon-Fri 9:30am-4:00pm ET = 13:30-20:00 UTC)"""
    now  = datetime.utcnow()
    hour = now.hour + now.minute / 60
    # Monday=0, Friday=4
    if now.weekday() > 4:
        return False
    return 13.5 <= hour <= 20.0

# ── LOAD WATCHLIST WITH THRESHOLDS ───────────────────────────
def load_thresholds():
    """Load price alert thresholds from watchlist or thresholds file"""
    path = os.path.join(BASE_DIR, "data", "price_thresholds.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return []

# ── LOAD FIRED LOG ────────────────────────────────────────────
def load_fired_log():
    """Track which alerts already fired today to avoid repeats"""
    if not os.path.exists(FIRED_LOG):
        return {}
    try:
        with open(FIRED_LOG) as f:
            return json.load(f)
    except Exception:
        return {}

def save_fired_log(log):
    os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
    with open(FIRED_LOG, "w") as f:
        json.dump(log, f, indent=2)

def already_fired_today(log, key):
    today = datetime.now().strftime("%Y-%m-%d")
    return log.get(key, {}).get("date") == today

def mark_fired(log, key, price, threshold):
    log[key] = {
        "date":      datetime.now().strftime("%Y-%m-%d"),
        "time":      datetime.now().strftime("%H:%M"),
        "price":     price,
        "threshold": threshold,
    }

# ── FETCH CURRENT PRICE ───────────────────────────────────────
def get_current_price(ticker):
    try:
        stock = yf.Ticker(ticker)
        info  = stock.info
        price = safe_float(
            info.get("regularMarketPrice") or
            info.get("currentPrice") or
            info.get("previousClose") or 0
        )
        return price
    except Exception:
        return 0.0


# ── BUILD ALERT MESSAGE ───────────────────────────────────────
def build_threshold_alert(ticker, name, alert_type, threshold, current_price):
    if alert_type == "above":
        emoji = "🚀"
        desc  = f"risen above ${threshold:.2f}"
        action= "Consider taking profits or reviewing target"
    else:
        emoji = "🔻"
        desc  = f"dropped below ${threshold:.2f}"
        action= "Possible entry or stop-loss review needed"

    change_from = round(((current_price - threshold) / threshold) * 100, 2)

    msg  = f"{emoji} <b>PRICE ALERT — {ticker}</b>\n"
    msg += f"{'─'*24}\n"
    msg += f"<b>{html.escape(str(name))}</b> has {desc}\n\n"
    msg += f"Current Price: <b>${current_price:.2f}</b>\n"
    msg += f"Your Threshold: ${threshold:.2f}\n"
    msg += f"Distance: {'+' if change_from>=0 else ''}{change_from:.1f}%\n\n"
    msg += f"💡 {action}\n"
    msg += f"\n<i>Market Agent V4.5 • Not financial advice</i>"
    return msg

# ── MAIN CHECK ────────────────────────────────────────────────
def run_price_checks():
    print(f"\n{BOLD}{BLUE}{'='*50}")
    print(f"  PRICE ALERT CHECK — {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*50}{RESET}")

    if not is_market_hours():
        print(f"{YELLOW}Market closed — skipping price checks{RESET}")
        return

    thresholds = load_thresholds()
    if not thresholds:
        print(f"{YELLOW}No price thresholds set{RESET}")
        return

    fired_log = load_fired_log()
    alerts_sent = 0

    for t in thresholds:
        ticker    = t.get("ticker","")
        name      = t.get("name", ticker)
        alert_above = safe_float(t.get("alert_above", 0))
        alert_below = safe_float(t.get("alert_below", 0))

        if not ticker or (alert_above == 0 and alert_below == 0):
            continue

        price = get_current_price(ticker)
        if price == 0:
            print(f"  {YELLOW}⚠ Could not fetch {ticker}{RESET}")
            continue

        print(f"  {ticker}: ${price:.2f}", end="")

        # Check ABOVE threshold
        if alert_above > 0 and price >= alert_above:
            key = f"{ticker}_above_{alert_above}"
            if not already_fired_today(fired_log, key):
                msg = build_threshold_alert(ticker, name, "above", alert_above, price)
                if send_telegram(msg):
                    mark_fired(fired_log, key, price, alert_above)
                    alerts_sent += 1
                    print(f" 🚀 ABOVE ${alert_above} — alert sent!", end="")

        # Check BELOW threshold
        if alert_below > 0 and price <= alert_below:
            key = f"{ticker}_below_{alert_below}"
            if not already_fired_today(fired_log, key):
                msg = build_threshold_alert(ticker, name, "below", alert_below, price)
                if send_telegram(msg):
                    mark_fired(fired_log, key, price, alert_below)
                    alerts_sent += 1
                    print(f" 🔻 BELOW ${alert_below} — alert sent!", end="")

        print()

    save_fired_log(fired_log)
    print(f"\n{GREEN}✓ Check complete — {alerts_sent} alerts sent{RESET}\n")

if __name__ == "__main__":
    run_price_checks()
