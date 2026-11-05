# ============================================================
# MARKET AGENT V4.5 — Shared helpers
# Terminal colours, safe_float, JSON files and Telegram, used by
# every pipeline script (each used to carry its own copy).
# ============================================================

import html
import json
import os
import re

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BLUE   = "\033[94m"
BOLD   = "\033[1m"
RESET  = "\033[0m"


def safe_float(val, default=0.0):
    """float(val), or default for None, blanks, "N/A" and NaN."""
    try:
        if val is None or str(val).strip() in ("", "nan", "NaN", "None", "N/A"):
            return default
        return float(str(val).strip())
    except Exception:
        return default


def load_json(path, default=None):
    """Parsed JSON file, or default when it is missing or unreadable."""
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    """Write JSON atomically (temp file + rename), creating the folder."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


TRAIL_ACTIVATE_PCT = 20.0   # defaults for strategy.json stop_loss.contextual_stop.trailing_stop
TRAIL_MIN_PCT = 15.0
TRAIL_ATR_MULTIPLE = 3.0


def trailing_stop_cfg(strategy):
    """The trailing-stop settings from strategy.json, with defaults."""
    return ((strategy or {}).get("stop_loss", {}).get("contextual_stop", {}) or {}).get("trailing_stop", {}) or {}


def trail_width_pct(atr_pct, strategy):
    """How far below the high the trailing stop sits, in %: the wider of
    the floor and N x the 14-day ATR%, so volatile stocks aren't sold on
    an ordinary week's swing."""
    ts = trailing_stop_cfg(strategy)
    floor = safe_float(ts.get("min_trail_pct"), TRAIL_MIN_PCT)
    return max(floor, safe_float(ts.get("atr_multiple"), TRAIL_ATR_MULTIPLE) * safe_float(atr_pct))


def make_trade_entry(body):
    """A validated trade_history.json entry from a Record Trade request
    (the laptop's /record-trade, or a phone edit applied on GitHub).
    Raises ValueError with a message the user can act on."""
    import uuid
    from datetime import datetime
    ticker = str(body.get("ticker", "")).strip().upper()
    action = str(body.get("action", "")).strip().upper()
    if not ticker or len(ticker) > 10 or not all(c.isalnum() or c in ".-" for c in ticker):
        raise ValueError("Invalid ticker")
    if action not in ("BUY", "SELL"):
        raise ValueError("action must be BUY or SELL")
    try:
        quantity = float(body.get("quantity"))
        price = float(body.get("price"))
        commission = body.get("commission", 0)
        commission = float(commission) if commission not in (None, "") else 0.0
    except (TypeError, ValueError):
        raise ValueError("quantity, price, and commission must be numbers")
    if quantity <= 0 or price <= 0 or commission < 0:
        raise ValueError("quantity and price must be > 0, commission must be >= 0")
    return {
        "id":          uuid.uuid4().hex[:12],
        "recorded_at": datetime.now().isoformat(),
        "trade_date":  body.get("trade_date") or datetime.now().isoformat(),
        "ticker":      ticker,
        "action":      action,
        "quantity":    quantity,
        "price":       price,
        "commission":  commission,
        "sector":      body.get("sector") or "",
        "note":        str(body.get("note", ""))[:300],
    }


TELEGRAM_LIMIT = 4096


def send_telegram(message, parse_mode="HTML", label="Telegram message"):
    """Send one message to the configured chat. If Telegram rejects the
    HTML (a stray "<" in Claude's text, say), resend it as plain text.
    Returns True when a copy was delivered."""
    import requests
    token, chat_id = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(f"{YELLOW}⚠ Telegram not configured (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID in .env){RESET}")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    text = message[:TELEGRAM_LIMIT]
    for mode in (parse_mode, None):
        payload = {"chat_id": chat_id, "text": text}
        if mode:
            payload["parse_mode"] = mode
        try:
            r = requests.post(url, json=payload, timeout=15)
            if r.status_code == 200:
                print(f"{GREEN}✓ {label} sent ({len(text)} chars{'' if mode else ', plain text'}){RESET}")
                return True
            print(f"{RED}✗ {label} failed ({mode or 'plain'}): {r.status_code} — {r.text[:200]}{RESET}")
        except Exception as e:
            print(f"{RED}✗ {label} failed: {e}{RESET}")
            return False
        if not mode:
            break
        text = re.sub(r"<[^>]+>", "", html.unescape(text))[:TELEGRAM_LIMIT]
    return False
