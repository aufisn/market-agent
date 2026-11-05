# ============================================================
# Market Agent V4.5 — Regime Detector
# Classifies market as: BULL_TREND / BEAR_TREND / CHOPPY / CRISIS
# Writes: data/market_regime.json
# Called by: run_agent.sh Step 1d
# ============================================================

import os
import json
from datetime import datetime
from dotenv import load_dotenv
from common import GREEN, YELLOW, BLUE, BOLD, RESET, safe_float

load_dotenv()

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
OUTPUT_PATH = os.path.join(BASE_DIR, "data", "market_regime.json")


def get_spy_regime():
    """
    Determine SPY trend:
    - price vs 50-day SMA slope
    - 50d SMA vs 200d SMA (golden/death cross)
    Returns: slope_pct, above_50d, above_200d, golden_cross
    """
    try:
        import yfinance as yf
        hist = yf.Ticker("SPY").history(period="1y", auto_adjust=True)
        if len(hist) < 55:
            return None, None, None, None

        closes   = hist["Close"].dropna()
        price    = float(closes.iloc[-1])
        sma50    = float(closes.iloc[-50:].mean())
        sma200   = float(closes.iloc[-200:].mean()) if len(closes) >= 200 else None

        # 50d slope: compare current SMA50 vs SMA50 20 days ago
        sma50_20d_ago = float(closes.iloc[-70:-20].mean()) if len(closes) >= 70 else sma50
        slope_pct     = round(((sma50 - sma50_20d_ago) / sma50_20d_ago) * 100, 2)

        above_50d    = price > sma50
        above_200d   = price > sma200 if sma200 else None
        golden_cross = sma50 > sma200  if sma200 else None

        global SPY_DRAWDOWN_PCT
        peak = float(closes.max())
        SPY_DRAWDOWN_PCT = round((price / peak - 1) * 100, 2) if peak > 0 else None
        return slope_pct, above_50d, above_200d, golden_cross
    except Exception as e:
        print(f"{YELLOW}⚠ SPY regime error: {e}{RESET}")
        return None, None, None, None


def get_vix():
    """Fetch VIX level as crisis/fear indicator"""
    try:
        import yfinance as yf
        vix  = yf.Ticker("^VIX")
        hist = vix.history(period="5d")
        if hist.empty:
            return None
        return round(float(hist["Close"].iloc[-1]), 2)
    except Exception as e:
        print(f"{YELLOW}⚠ VIX fetch error: {e}{RESET}")
        return None


def get_breadth():
    """
    Market breadth via RSP (equal-weight S&P ETF) vs SPY ratio.
    When RSP/SPY ratio is trending down = narrow leadership = weakening breadth.
    Returns ratio and 20d change in ratio.
    """
    try:
        import yfinance as yf
        data   = yf.download(["RSP", "SPY"], period="30d",
                             auto_adjust=True, progress=False)
        closes = data["Close"].dropna()
        if len(closes) < 5:
            return None, None
        ratio_now  = float(closes["RSP"].iloc[-1]) / float(closes["SPY"].iloc[-1])
        ratio_20d  = float(closes["RSP"].iloc[0])  / float(closes["SPY"].iloc[0])
        ratio_chg  = round(((ratio_now - ratio_20d) / ratio_20d) * 100, 2)
        return round(ratio_now, 4), ratio_chg
    except Exception as e:
        print(f"{YELLOW}⚠ Breadth error: {e}{RESET}")
        return None, None


def classify_regime(slope_pct, above_50d, above_200d, golden_cross,
                    vix, breadth_chg):
    """
    Classify regime based on all signals:
    CRISIS:     VIX > 35 OR (below 200d AND slope < -3%)
    BEAR_TREND: slope < -1.5% OR (below 50d AND below 200d)
    BULL_TREND: slope > 1% AND above 50d AND (golden cross OR above 200d)
    CHOPPY:     everything else
    """
    if vix and vix > 35:
        return "CRISIS", "VIX extreme fear — defensive posture"

    if slope_pct is not None and slope_pct < -3 and above_200d is False:
        return "CRISIS", f"SPY below 200d SMA with sharp slope ({slope_pct:+.1f}%) — crisis conditions"

    if slope_pct is not None and slope_pct < -1.5:
        return "BEAR_TREND", f"SPY 50d SMA declining ({slope_pct:+.1f}%) — downtrend in force"

    if above_50d is False and above_200d is False:
        return "BEAR_TREND", "SPY below both 50d and 200d SMA — bear market structure"

    if (slope_pct is not None and slope_pct > 1.0
            and above_50d is True
            and (golden_cross is True or above_200d is True)):
        label = "Golden cross in effect" if golden_cross else "SPY above 200d SMA"
        breadth_note = ""
        if breadth_chg is not None and breadth_chg < -1.5:
            breadth_note = " — breadth narrowing, monitor"
        return "BULL_TREND", f"{label}, 50d slope {slope_pct:+.1f}%{breadth_note}"

    if slope_pct is None:  # SPY history unavailable (feed outage) — don't crash, say so
        return "CHOPPY", "SPY trend data unavailable — regime unknown, treating as mixed"
    return "CHOPPY", f"Mixed signals — slope {slope_pct:+.1f}% — consolidation or transition"


SPY_DRAWDOWN_PCT = None   # set by get_spy_regime(): SPY vs its 52-week closing high

# L3 defaults — overridable in strategy.json "regime_exposure".
EXPOSURE_DEFAULTS = {
    "max_exposure_pct": {"BULL_TREND": 85, "CHOPPY": 80, "BEAR_TREND": 75, "CRISIS": 60},
    "vix_elevated": 25, "vix_elevated_cut": 5,
    "vix_high": 30,     "vix_high_cut": 10,
    "breadth_weak_pct": -2.0, "breadth_weak_cut": 5,
    "min_exposure_pct": 50,
    "circuit_breaker_drawdown_pct": 10,
    "severe_drawdown_pct": 20,
    "allow_buying_drawdowns": False,
}


def load_exposure_config():
    cfg = dict(EXPOSURE_DEFAULTS)
    try:
        with open(os.path.join(BASE_DIR, "strategy.json")) as f:
            user = json.load(f).get("regime_exposure") or {}
        cfg.update({k: v for k, v in user.items() if not k.startswith("note")})
    except Exception:
        pass
    return cfg


def compute_exposure(regime, vix, breadth_chg, spy_drawdown_pct, cfg=None):
    """L3: how much of capital may be in stocks, and whether the index
    drawdown circuit breaker is on. Returns a dict the sizing code reads."""
    cfg = cfg or load_exposure_config()
    base = safe_float((cfg.get("max_exposure_pct") or {}).get(regime), 80)
    cap, reasons = base, [f"{regime} base {base:g}%"]
    if vix is not None:
        if vix >= cfg["vix_high"]:
            cap -= cfg["vix_high_cut"]; reasons.append(f"VIX {vix:g} ≥ {cfg['vix_high']:g}: −{cfg['vix_high_cut']:g}")
        elif vix >= cfg["vix_elevated"]:
            cap -= cfg["vix_elevated_cut"]; reasons.append(f"VIX {vix:g} ≥ {cfg['vix_elevated']:g}: −{cfg['vix_elevated_cut']:g}")
    if breadth_chg is not None and breadth_chg <= cfg["breadth_weak_pct"]:
        cap -= cfg["breadth_weak_cut"]; reasons.append(f"breadth {breadth_chg:+.1f}% (narrow leadership): −{cfg['breadth_weak_cut']:g}")
    cap = max(cfg["min_exposure_pct"], cap)

    dd = spy_drawdown_pct
    tripped = dd is not None and dd <= -abs(cfg["circuit_breaker_drawdown_pct"])
    severe  = dd is not None and dd <= -abs(cfg["severe_drawdown_pct"])
    breaker = {
        "spy_drawdown_pct": dd,
        "threshold_pct":    -abs(cfg["circuit_breaker_drawdown_pct"]),
        "tripped":          tripped,
        "severe":           severe,
        "halts_buys":       tripped and not cfg.get("allow_buying_drawdowns"),
        "note": (None if not tripped else
                 (f"SPY is {dd:.1f}% below its 52-week high. "
                  + ("New buys are allowed because strategy.json sets allow_buying_drawdowns — a deliberate choice to buy the dip."
                     if cfg.get("allow_buying_drawdowns") else
                     "New buys are paused. To buy into drawdowns deliberately, set regime_exposure.allow_buying_drawdowns to true.")
                  + (" Severe drawdown — review every holding against its stop." if severe else ""))),
    }
    return {"max_exposure_pct": round(cap, 1), "cash_floor_pct": round(100 - cap, 1),
            "reasons": reasons, "circuit_breaker": breaker}


def regime_to_adjustments(regime):
    """
    Return strategy adjustments for each regime.
    These are injected into agent_brain.py's Claude prompt.
    """
    adjustments = {
        "BULL_TREND": {
            "pick_aggressiveness":  "normal",
            "stop_sensitivity":     "relaxed",
            "cash_floor_override":  None,
            "note": "Bull trend — run normal strategy. Allow wider stops on quality names. Full position sizing permitted."
        },
        "CHOPPY": {
            "pick_aggressiveness":  "selective",
            "stop_sensitivity":     "normal",
            "cash_floor_override":  None,
            "note": "Choppy market — be selective. Prefer high-conviction setups. Tighter entries, wait for confirmation."
        },
        "BEAR_TREND": {
            "pick_aggressiveness":  "defensive",
            "stop_sensitivity":     "tight",
            "cash_floor_override":  25,
            "note": "Bear trend — limit new buys to highest conviction only. Raise cash floor to 25%. Tighten stops on existing holdings."
        },
        "CRISIS": {
            "pick_aggressiveness":  "halt",
            "stop_sensitivity":     "immediate",
            "cash_floor_override":  40,
            "note": "Crisis conditions — NO new buy positions. Protect capital. Review all holdings for hard exit triggers. Cash floor 40%."
        }
    }
    return adjustments.get(regime, adjustments["CHOPPY"])


def run():
    print(f"\n{BOLD}{BLUE}── Regime Detector ─────────────────────────────{RESET}")

    slope_pct, above_50d, above_200d, golden_cross = get_spy_regime()
    vix                                             = get_vix()
    breadth_ratio, breadth_chg                      = get_breadth()

    regime, description = classify_regime(
        slope_pct, above_50d, above_200d, golden_cross, vix, breadth_chg
    )

    adjustments = dict(regime_to_adjustments(regime))
    exposure = compute_exposure(regime, vix, breadth_chg, SPY_DRAWDOWN_PCT)
    # The exposure cap becomes the cash floor the sizing code already reads;
    # the circuit breaker halts new buys unless the user opted in.
    adjustments["cash_floor_override"] = max(adjustments.get("cash_floor_override") or 0, exposure["cash_floor_pct"])
    if exposure["circuit_breaker"]["halts_buys"]:
        adjustments["pick_aggressiveness"] = "halt"
        adjustments["note"] = adjustments["note"] + " CIRCUIT BREAKER: " + exposure["circuit_breaker"]["note"]

    result = {
        "regime":        regime,
        "description":   description,
        "generated_at":  datetime.now().isoformat(),
        "signals": {
            "spy_slope_pct":   slope_pct,
            "above_50d_sma":   above_50d,
            "above_200d_sma":  above_200d,
            "golden_cross":    golden_cross,
            "vix":             vix,
            "breadth_ratio":   breadth_ratio,
            "breadth_20d_chg": breadth_chg,
            "spy_drawdown_pct": SPY_DRAWDOWN_PCT,
        },
        "adjustments": adjustments,
        "exposure":    exposure,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH + ".tmp", "w") as f:
        json.dump(result, f, indent=2)
    os.replace(OUTPUT_PATH + ".tmp", OUTPUT_PATH)

    regime_colors = {
        "BULL_TREND": GREEN, "CHOPPY": YELLOW,
        "BEAR_TREND": "\033[91m", "CRISIS": "\033[91m"
    }
    c = regime_colors.get(regime, YELLOW)
    print(f"  Regime:  {c}{regime}{RESET}")
    print(f"  VIX:     {vix or '—'}")
    print(f"  Slope:   {slope_pct:+.2f}%" if slope_pct is not None else "  Slope:   —")
    print(f"  Breadth: {breadth_chg:+.2f}% (20d)" if breadth_chg is not None else "  Breadth: —")
    print(f"  Exposure: max {exposure['max_exposure_pct']:g}% in stocks (cash floor {exposure['cash_floor_pct']:g}%) — {'; '.join(exposure['reasons'])}")
    if exposure["circuit_breaker"]["tripped"]:
        print(f"  \033[91mCircuit breaker: {exposure['circuit_breaker']['note']}{RESET}")
    print(f"  Note:    {adjustments['note']}")

    return result


if __name__ == "__main__":
    run()
