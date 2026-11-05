# ============================================================
# MARKET AGENT V4.5 — Technicals from price history
# technicals.py
#
# RSI(14), SMA50 and SMA200 computed from yfinance daily closes.
# Replaces the Alpha Vantage free tier (25 requests/day, 3 per
# ticker), which in practice returned nothing: every RSI/SMA in
# market_data.json was None, and the RSI entry gate silently
# passed on the missing value.
# ============================================================

import math


def _clean(closes):
    return [float(c) for c in closes if c is not None and not (isinstance(c, float) and math.isnan(c))]


def rsi_wilder(closes, period=14):
    """Wilder-smoothed RSI. Needs at least period+1 closes."""
    c = _clean(closes)
    if len(c) < period + 1:
        return None
    gains = [max(0.0, b - a) for a, b in zip(c, c[1:])]
    losses = [max(0.0, a - b) for a, b in zip(c, c[1:])]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_gain / avg_loss), 2)


def sma(closes, period):
    c = _clean(closes)
    if len(c) < period:
        return None
    return round(sum(c[-period:]) / period, 2)


def atr_wilder(highs, lows, closes, period=14):
    """Wilder-smoothed Average True Range — a stock's typical daily move in
    dollars. Used by L4 sizing so a stop is never tighter than normal noise."""
    rows = [(float(h), float(l), float(c)) for h, l, c in zip(highs, lows, closes)
            if None not in (h, l, c) and not any(isinstance(x, float) and math.isnan(x) for x in (h, l, c))]
    if len(rows) < period + 1:
        return None
    trs = [max(h - l, abs(h - pc), abs(l - pc)) for (h, l, _), (_, _, pc) in zip(rows[1:], rows)]
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return round(atr, 4)


def compute_technicals(ticker, closes=None):
    """{"rsi", "sma_50", "sma_200", "atr_14", "atr_pct"} for a ticker; any
    value is None when there isn't enough history. Pass closes to skip the
    download (ATR then stays None — it needs highs and lows)."""
    highs = lows = None
    if closes is None:
        try:
            import yfinance as yf
            hist = yf.Ticker(ticker).history(period="1y", auto_adjust=True)
            if hist is not None and not hist.empty:
                closes = list(hist["Close"])
                if "High" in hist and "Low" in hist:
                    highs, lows = list(hist["High"]), list(hist["Low"])
            else:
                closes = []
        except Exception:
            closes = []
    atr = atr_wilder(highs, lows, closes) if highs and lows else None
    last = _clean(closes)[-1] if _clean(closes) else None
    return {"rsi": rsi_wilder(closes), "sma_50": sma(closes, 50), "sma_200": sma(closes, 200),
            "atr_14": atr, "atr_pct": round(atr / last * 100, 2) if atr and last else None}
