# ============================================================
# MARKET AGENT V4.5 — Correlation-Aware Sizing
# correlation_engine.py
#
# Sector labels are a blunt instrument — MSFT/AVGO/NVDA can all say
# "Technology" while moving together far more than that implies during
# a drawdown. This computes actual DAILY-RETURN correlation (not price
# level — two stocks in a bull market both trend up regardless of any
# real relationship, so price-level correlation is close to meaningless
# for risk purposes) over a rolling 90-day window.
#
# Two tiers, matching the agreed design:
#   1. HOLDINGS x HOLDINGS matrix — cached once per day (logs/
#      correlation_matrix.json). Correlation between existing positions
#      doesn't meaningfully shift within a day, so this is computed
#      once and reused for the rest of the day's runs. Surfaces
#      standing concentration risk independent of any new pick.
#   2. NEW PICK x HOLDINGS — computed fresh each time a pick needs
#      evaluating (reuses the day's cached holdings return series where
#      possible, only fetches the new ticker's own history).
#
# Output is informational, not a filter — attaches a suggested,
# correlation-adjusted position size alongside whatever
# reallocation_engine.py already computed. Salim makes the call.
#
# Sizing taper: no reduction below 0.7 correlation to the most-
# correlated holding; linearly tapers to a 50% size cut as correlation
# approaches 1.0. This threshold/floor is a starting point, not
# empirically tuned — worth revisiting once there's real usage data.
#
# Reads:  analysis["holdings"]      — current holdings
#         analysis["agent_picks"]   — picks that passed all 7 gates
#
# Writes: analysis["correlation_matrix"]        — holdings x holdings
#         analysis["agent_picks"][i]["correlation"] — per-pick:
#           {"max_correlation": float, "most_correlated_ticker": str,
#            "size_multiplier": float, "note": str}
#         logs/correlation_matrix.json — daily cache
# ============================================================

import json
import os
from datetime import datetime
from common import GREEN, YELLOW, BLUE, RESET, save_json

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH    = os.path.join(BASE_DIR, "logs", "correlation_matrix.json")


LOOKBACK_DAYS       = 90
CORR_FLOOR          = 0.7   # below this, no size reduction
CORR_CEILING        = 1.0   # at this correlation, apply the max reduction
MAX_SIZE_REDUCTION  = 0.5   # multiplier at CORR_CEILING (i.e. 50% size cut)


def load_cache():
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"{YELLOW}⚠ Could not load correlation_matrix.json ({e}) — starting fresh{RESET}")
        return {}


def _fetch_returns(ticker):
    """Fetch ~90 trading days of daily closes and return a pandas Series of pct-change returns."""
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period=f"{LOOKBACK_DAYS}d")
        if hist is None or hist.empty or "Close" not in hist.columns:
            return None
        returns = hist["Close"].pct_change().dropna()
        if len(returns) < 20:  # too little data to trust a correlation
            return None
        return returns
    except Exception as e:
        print(f"  {YELLOW}⚠ Could not fetch history for {ticker}: {e}{RESET}")
        return None


def _compute_pairwise_corr(series_map):
    """series_map: {ticker: pandas Series of returns}. Returns nested dict of pairwise correlations."""
    import pandas as pd
    tickers = list(series_map.keys())
    if len(tickers) < 2:
        return {}
    df = pd.DataFrame(series_map)
    corr_df = df.corr(min_periods=20)
    matrix = {}
    for t1 in tickers:
        matrix[t1] = {}
        for t2 in tickers:
            if t1 == t2:
                continue
            val = corr_df.loc[t1, t2]
            matrix[t1][t2] = round(float(val), 3) if val == val else None  # NaN check
    return matrix


def _size_multiplier(max_corr):
    if max_corr is None or max_corr <= CORR_FLOOR:
        return 1.0
    span = CORR_CEILING - CORR_FLOOR
    reduction_frac = min((max_corr - CORR_FLOOR) / span, 1.0)
    return round(1.0 - reduction_frac * MAX_SIZE_REDUCTION, 3)


# ── TIER 1: HOLDINGS x HOLDINGS (daily cached) ─────────────────
def get_holdings_matrix(holdings):
    tickers = sorted({str(h.get("ticker", "")).upper() for h in holdings if h.get("ticker")})
    if len(tickers) < 2:
        return {}, {}

    cache = load_cache()
    today = datetime.now().strftime("%Y-%m-%d")
    cached_tickers = set(cache.get("holdings_returns", {}).keys())

    if cache.get("computed_date") == today and set(tickers).issubset(cached_tickers):
        print(f"{BLUE}  Using today's cached holdings correlation matrix{RESET}")
        matrix = cache.get("matrix", {})
        returns_raw = cache.get("holdings_returns", {})
        # Reconstruct pandas Series from cached lists for reuse in pick-vs-holdings step
        import pandas as pd
        series_map = {t: pd.Series(returns_raw[t]) for t in tickers if t in returns_raw}
        return matrix, series_map

    print(f"{BLUE}  Fetching {LOOKBACK_DAYS}-day return history for {len(tickers)} holding(s)...{RESET}")
    series_map = {}
    for t in tickers:
        r = _fetch_returns(t)
        if r is not None:
            series_map[t] = r

    matrix = _compute_pairwise_corr(series_map)

    cache_out = {
        "computed_date":    today,
        "matrix":           matrix,
        "holdings_returns": {t: s.tolist() for t, s in series_map.items()},
    }
    save_json(CACHE_PATH, cache_out)
    print(f"{GREEN}✓ Holdings correlation matrix computed and cached{RESET}")
    return matrix, series_map


# ── TIER 2: NEW PICK x HOLDINGS ─────────────────────────────────
def get_pick_correlation(ticker, holdings_series_map):
    ticker = ticker.upper()
    if not holdings_series_map:
        return None

    pick_returns = _fetch_returns(ticker)
    if pick_returns is None:
        return None

    import pandas as pd
    correlations = {}
    for h_ticker, h_returns in holdings_series_map.items():
        if h_ticker == ticker:
            continue
        aligned = pd.concat([pick_returns, h_returns], axis=1, join="inner")
        if len(aligned) < 20:
            continue
        c = aligned.iloc[:, 0].corr(aligned.iloc[:, 1])
        if c == c:  # not NaN
            correlations[h_ticker] = round(float(c), 3)

    if not correlations:
        return None

    most_correlated = max(correlations, key=correlations.get)
    max_corr = correlations[most_correlated]
    multiplier = _size_multiplier(max_corr)

    if multiplier >= 1.0:
        note = f"Low correlation to current holdings (max {max_corr:+.2f} vs {most_correlated}) — no size adjustment"
    else:
        cut_pct = round((1 - multiplier) * 100)
        note = (f"Correlates {max_corr:+.2f} with {most_correlated} — reduces real diversification "
                f"benefit despite any sector difference; suggested size cut {cut_pct}%")

    return {
        "max_correlation":        max_corr,
        "most_correlated_ticker": most_correlated,
        "all_correlations":       correlations,
        "size_multiplier":        multiplier,
        "note":                   note,
    }


# ── MAIN ENTRY POINT ──────────────────────────────────────────
def run(analysis, data):
    """
    Called from agent_brain.py's run(), after adversarial_critique and
    before the reallocation engine (so reallocation can read
    pick["correlation"]["size_multiplier"] when computing suggested sizing).
    """
    holdings = analysis.get("holdings", [])
    picks    = analysis.get("agent_picks", [])

    if len(holdings) < 2:
        print(f"{YELLOW}⚠ Fewer than 2 holdings — skipping correlation analysis{RESET}")
        return analysis

    matrix, holdings_series_map = get_holdings_matrix(holdings)
    analysis["correlation_matrix"] = matrix

    # Print any notably high holdings-vs-holdings correlations
    seen = set()
    high_pairs = []
    for t1, row in matrix.items():
        for t2, c in row.items():
            pair = tuple(sorted([t1, t2]))
            if pair in seen or c is None:
                continue
            seen.add(pair)
            if c >= CORR_FLOOR:
                high_pairs.append((t1, t2, c))
    if high_pairs:
        print(f"{YELLOW}  ⚠ High correlation between current holdings:{RESET}")
        for t1, t2, c in high_pairs:
            print(f"    {t1} <-> {t2}: {c:+.2f}")

    if not picks:
        return analysis

    print(f"{BLUE}  Checking pick correlation against holdings...{RESET}")
    for pick in picks:
        ticker = str(pick.get("ticker", "")).upper()
        corr_data = get_pick_correlation(ticker, holdings_series_map)
        if corr_data:
            pick["correlation"] = corr_data
            if corr_data["size_multiplier"] < 1.0:
                print(f"  {YELLOW}⚠ {ticker}: {corr_data['note']}{RESET}")
            else:
                print(f"  {GREEN}✓ {ticker}: max correlation {corr_data['max_correlation']:+.2f} — no size adjustment{RESET}")

    analysis["agent_picks"] = picks
    print(f"{GREEN}✓ Correlation analysis complete{RESET}")
    return analysis
