# ============================================================
# MARKET AGENT V4.5 — Phase 2: Market Context
# Fetches:
#   1. Fear & Greed Index (CNN)
#   2. Earnings Calendar (for watchlist stocks)
#   3. Macro Events (Fed, CPI, Jobs — from known schedule)
# ============================================================

import requests
import json
import os
from datetime import datetime, timedelta
from common import GREEN, RED, YELLOW, BLUE, BOLD, RESET

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ── 1. FEAR & GREED INDEX ─────────────────────────────────────
def fetch_fear_greed():
    """
    Fetch CNN Fear & Greed Index via their API
    Returns score 0-100 and label
    """
    print(f"{BLUE}Fetching Fear & Greed Index...{RESET}", end=" ", flush=True)
    try:
        url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer":    "https://www.cnn.com/markets/fear-and-greed",
        }
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()

        score = round(float(data["fear_and_greed"]["score"]), 1)
        rating = data["fear_and_greed"]["rating"].replace("_", " ").title()
        prev_close = round(float(data["fear_and_greed"]["previous_close"]), 1)
        prev_week  = round(float(data["fear_and_greed"].get("previous_1_week", score)), 1)

        # Direction
        if score > prev_close:
            direction = "↑ rising"
        elif score < prev_close:
            direction = "↓ falling"
        else:
            direction = "→ stable"

        result = {
            "score":       score,
            "rating":      rating,
            "direction":   direction,
            "prev_close":  prev_close,
            "prev_week":   prev_week,
            "interpretation": interpret_fg(score),
        }
        print(f"{GREEN}✓ {score} — {rating}{RESET}")
        return result

    except Exception as e:
        print(f"{YELLOW}⚠ Could not fetch Fear & Greed: {e}{RESET}")
        return {"score": None, "rating": "Unavailable", "direction": "", "interpretation": ""}

def interpret_fg(score):
    """Actionable interpretation of Fear & Greed score"""
    if score is None:
        return ""
    if score <= 20:
        return "Extreme Fear — historically a strong buying opportunity"
    elif score <= 40:
        return "Fear — market oversold, look for entry points"
    elif score <= 60:
        return "Neutral — no strong directional signal"
    elif score <= 80:
        return "Greed — markets elevated, be selective with new entries"
    else:
        return "Extreme Greed — markets overheated, consider taking profits"

# ── 2. EARNINGS CALENDAR ──────────────────────────────────────
# ETFs and funds that never have earnings — skip silently
ETF_TICKERS = {
    "SLV","GLD","SPY","QQQ","XLE","XLF","XLK","XLV","XLU","XLI",
    "XLP","XLY","XLB","XLRE","VTI","VOO","IWM","EFA","EEM","TLT",
    "HYG","LQD","GDX","GDXJ","USO","UNG","ARKK","ARKG","ARKW"
}

def fetch_earnings(tickers, days_ahead=14):
    """
    Check upcoming earnings for watchlist stock tickers
    Silently skips ETFs and funds
    """
    print(f"{BLUE}Fetching earnings calendar...{RESET}", end=" ", flush=True)
    import yfinance as yf
    import warnings
    warnings.filterwarnings("ignore")

    earnings = []
    today    = datetime.now().date()
    cutoff   = today + timedelta(days=days_ahead)

    # Filter out known ETFs
    stock_tickers = [t for t in tickers if t.upper() not in ETF_TICKERS and t.upper() != "CASH"]

    for ticker in stock_tickers:
        try:
            stock = yf.Ticker(ticker)

            # Try fast_info first — less likely to throw 404
            try:
                quote_type = stock.fast_info.get("quoteType","") or ""
                if quote_type.upper() in ("ETF","MUTUALFUND","INDEX","FUTURE","CURRENCY"):
                    continue
            except Exception:
                pass

            # Suppress stderr for 404 errors from yfinance
            import sys, io
            old_stderr = sys.stderr
            sys.stderr  = io.StringIO()

            try:
                cal = stock.calendar
            finally:
                sys.stderr = old_stderr

            if cal is None:
                continue

            # Handle both dict and DataFrame formats
            if isinstance(cal, dict):
                ed_raw = cal.get("Earnings Date") or cal.get("earningsDate")
                if ed_raw:
                    dates = ed_raw if isinstance(ed_raw, list) else [ed_raw]
                    for d in dates:
                        try:
                            import pandas as pd
                            ed = pd.to_datetime(d).date()
                            if today <= ed <= cutoff:
                                days_to = (ed - today).days
                                earnings.append({
                                    "ticker":    ticker,
                                    "date":      ed.strftime("%d %b %Y"),
                                    "days_away": days_to,
                                    "urgency":   "⚠️ SOON" if days_to <= 3 else "📅 Upcoming",
                                })
                        except Exception:
                            pass
            elif hasattr(cal, "empty") and not cal.empty:
                if "Earnings Date" in cal.columns:
                    import pandas as pd
                    for val in cal["Earnings Date"]:
                        try:
                            ed = pd.to_datetime(val).date()
                            if today <= ed <= cutoff:
                                days_to = (ed - today).days
                                earnings.append({
                                    "ticker":    ticker,
                                    "date":      ed.strftime("%d %b %Y"),
                                    "days_away": days_to,
                                    "urgency":   "⚠️ SOON" if days_to <= 3 else "📅 Upcoming",
                                })
                        except Exception:
                            pass

        except Exception:
            pass  # Silently skip any ticker that errors

    earnings.sort(key=lambda x: x["days_away"])
    print(f"{GREEN}✓ {len(earnings)} upcoming earnings found{RESET}")
    return earnings

# ── 3. MACRO EVENTS ───────────────────────────────────────────
def get_macro_events():
    """
    Key macro events — Fed meetings, CPI, Jobs report
    Hand-maintained calendar — see the source note on the list below
    """
    print(f"{BLUE}Loading macro events calendar...{RESET}", end=" ", flush=True)

    # Official dates, checked 2026-11-02 against federalreserve.gov (FOMC,
    # decision = 2nd day), bls.gov (CPI, Employment Situation) and bea.gov (GDP).
    # BEA had not published its 2027 schedule yet — add 2027 GDP dates when it does.
    events = [
        # Fed FOMC decisions
        {"date": "2026-07-29", "event": "Fed FOMC Decision",       "impact": "HIGH",   "emoji": "🏦"},
        {"date": "2026-09-16", "event": "Fed FOMC Decision",       "impact": "HIGH",   "emoji": "🏦"},
        {"date": "2026-10-28", "event": "Fed FOMC Decision",       "impact": "HIGH",   "emoji": "🏦"},
        {"date": "2026-12-09", "event": "Fed FOMC Decision",       "impact": "HIGH",   "emoji": "🏦"},
        {"date": "2027-01-27", "event": "Fed FOMC Decision",       "impact": "HIGH",   "emoji": "🏦"},
        {"date": "2027-03-17", "event": "Fed FOMC Decision",       "impact": "HIGH",   "emoji": "🏦"},
        {"date": "2027-04-28", "event": "Fed FOMC Decision",       "impact": "HIGH",   "emoji": "🏦"},
        {"date": "2027-06-09", "event": "Fed FOMC Decision",       "impact": "HIGH",   "emoji": "🏦"},
        {"date": "2027-07-28", "event": "Fed FOMC Decision",       "impact": "HIGH",   "emoji": "🏦"},
        {"date": "2027-09-15", "event": "Fed FOMC Decision",       "impact": "HIGH",   "emoji": "🏦"},
        {"date": "2027-10-27", "event": "Fed FOMC Decision",       "impact": "HIGH",   "emoji": "🏦"},
        {"date": "2027-12-08", "event": "Fed FOMC Decision",       "impact": "HIGH",   "emoji": "🏦"},
        # CPI reports
        {"date": "2026-07-14", "event": "US CPI Inflation Report", "impact": "HIGH",   "emoji": "📊"},
        {"date": "2026-08-11", "event": "US CPI Inflation Report", "impact": "HIGH",   "emoji": "📊"},
        {"date": "2026-09-10", "event": "US CPI Inflation Report", "impact": "HIGH",   "emoji": "📊"},
        {"date": "2026-10-13", "event": "US CPI Inflation Report", "impact": "HIGH",   "emoji": "📊"},
        {"date": "2026-11-10", "event": "US CPI Inflation Report", "impact": "HIGH",   "emoji": "📊"},
        {"date": "2026-12-10", "event": "US CPI Inflation Report", "impact": "HIGH",   "emoji": "📊"},
        {"date": "2027-01-13", "event": "US CPI Inflation Report", "impact": "HIGH",   "emoji": "📊"},
        {"date": "2027-02-11", "event": "US CPI Inflation Report", "impact": "HIGH",   "emoji": "📊"},
        {"date": "2027-03-10", "event": "US CPI Inflation Report", "impact": "HIGH",   "emoji": "📊"},
        {"date": "2027-04-13", "event": "US CPI Inflation Report", "impact": "HIGH",   "emoji": "📊"},
        {"date": "2027-05-12", "event": "US CPI Inflation Report", "impact": "HIGH",   "emoji": "📊"},
        {"date": "2027-06-10", "event": "US CPI Inflation Report", "impact": "HIGH",   "emoji": "📊"},
        {"date": "2027-07-14", "event": "US CPI Inflation Report", "impact": "HIGH",   "emoji": "📊"},
        {"date": "2027-08-11", "event": "US CPI Inflation Report", "impact": "HIGH",   "emoji": "📊"},
        {"date": "2027-09-14", "event": "US CPI Inflation Report", "impact": "HIGH",   "emoji": "📊"},
        {"date": "2027-10-14", "event": "US CPI Inflation Report", "impact": "HIGH",   "emoji": "📊"},
        {"date": "2027-11-10", "event": "US CPI Inflation Report", "impact": "HIGH",   "emoji": "📊"},
        {"date": "2027-12-10", "event": "US CPI Inflation Report", "impact": "HIGH",   "emoji": "📊"},
        # Jobs reports
        {"date": "2026-07-02", "event": "US Jobs Report (NFP)",    "impact": "HIGH",   "emoji": "💼"},
        {"date": "2026-08-07", "event": "US Jobs Report (NFP)",    "impact": "HIGH",   "emoji": "💼"},
        {"date": "2026-09-04", "event": "US Jobs Report (NFP)",    "impact": "HIGH",   "emoji": "💼"},
        {"date": "2026-10-02", "event": "US Jobs Report (NFP)",    "impact": "HIGH",   "emoji": "💼"},
        {"date": "2026-11-06", "event": "US Jobs Report (NFP)",    "impact": "HIGH",   "emoji": "💼"},
        {"date": "2026-12-04", "event": "US Jobs Report (NFP)",    "impact": "HIGH",   "emoji": "💼"},
        {"date": "2027-01-08", "event": "US Jobs Report (NFP)",    "impact": "HIGH",   "emoji": "💼"},
        {"date": "2027-02-05", "event": "US Jobs Report (NFP)",    "impact": "HIGH",   "emoji": "💼"},
        {"date": "2027-03-05", "event": "US Jobs Report (NFP)",    "impact": "HIGH",   "emoji": "💼"},
        {"date": "2027-04-02", "event": "US Jobs Report (NFP)",    "impact": "HIGH",   "emoji": "💼"},
        {"date": "2027-05-07", "event": "US Jobs Report (NFP)",    "impact": "HIGH",   "emoji": "💼"},
        {"date": "2027-06-04", "event": "US Jobs Report (NFP)",    "impact": "HIGH",   "emoji": "💼"},
        {"date": "2027-07-02", "event": "US Jobs Report (NFP)",    "impact": "HIGH",   "emoji": "💼"},
        {"date": "2027-08-06", "event": "US Jobs Report (NFP)",    "impact": "HIGH",   "emoji": "💼"},
        {"date": "2027-09-03", "event": "US Jobs Report (NFP)",    "impact": "HIGH",   "emoji": "💼"},
        {"date": "2027-10-08", "event": "US Jobs Report (NFP)",    "impact": "HIGH",   "emoji": "💼"},
        {"date": "2027-11-05", "event": "US Jobs Report (NFP)",    "impact": "HIGH",   "emoji": "💼"},
        {"date": "2027-12-03", "event": "US Jobs Report (NFP)",    "impact": "HIGH",   "emoji": "💼"},
        # GDP advance estimates
        {"date": "2026-07-30", "event": "US GDP Q2 Advance",       "impact": "MEDIUM", "emoji": "📈"},
        {"date": "2026-10-29", "event": "US GDP Q3 Advance",       "impact": "MEDIUM", "emoji": "📈"},
    ]

    # L7: FOMC dates from the Fed's own page (cached weekly) replace the
    # hand-kept ones for every year the page covers.
    source = "hand list"
    try:
        import market_calendar
        fed = market_calendar.fomc_dates()
        if fed:
            fed_years = {d[:4] for d in fed}
            events = [e for e in events if not (e["event"] == "Fed FOMC Decision" and e["date"][:4] in fed_years)]
            events += [{"date": d, "event": "Fed FOMC Decision", "impact": "HIGH", "emoji": "🏦"} for d in fed]
            source = "federalreserve.gov"
    except Exception as e:
        print(f"{YELLOW}⚠ FOMC feed unavailable ({e}) — using hand list{RESET}", end=" ")

    today   = datetime.now().date()
    cutoff  = today + timedelta(days=30)
    upcoming = []

    for e in events:
        try:
            ed = datetime.strptime(e["date"], "%Y-%m-%d").date()
            if today <= ed <= cutoff:
                days_away = (ed - today).days
                upcoming.append({
                    **e,
                    "date_display": ed.strftime("%d %b %Y"),
                    "days_away":    days_away,
                    "urgency":      "⚠️" if days_away <= 5 else "📅",
                })
        except Exception:
            pass

    upcoming.sort(key=lambda x: x["days_away"])
    print(f"{GREEN}✓ {len(upcoming)} macro events in next 30 days (FOMC: {source}){RESET}")

    # CPI/NFP are maintained by hand (bls.gov blocks automated downloads);
    # FOMC usually comes from the Fed feed. Check each HIGH-impact series
    # separately so one running out is never hidden by another.
    for kind, where in [("Fed FOMC Decision", "federalreserve.gov"),
                        ("US CPI Inflation Report", "bls.gov/schedule/news_release/cpi.htm"),
                        ("US Jobs Report (NFP)", "bls.gov/schedule/news_release/empsit.htm")]:
        dates = [datetime.strptime(e["date"], "%Y-%m-%d").date() for e in events if e["event"] == kind]
        last = max(dates) if dates else today
        if last < cutoff:
            upcoming.append({
                "date": last.isoformat(), "event": f"MACRO CALENDAR NEEDS UPDATING — {kind}",
                "impact": "HIGH", "emoji": "🗓️", "date_display": f"list ends {last.strftime('%d %b %Y')}",
                "days_away": max(0, (last - today).days), "urgency": "⚠️",
                "note": f"Add the next {kind} dates from {where} to market_context.py",
            })
            print(f"{YELLOW}⚠ {kind} dates end {last} — add the next ones to market_context.py{RESET}")
    return upcoming

# ── 4. CROSS-ASSET MACRO CONTEXT ───────────────────────
# VIX, 10Y yield, DXY, HYG/LQD ratio — injected as standing text
# context into every Claude prompt via build_context_summary().
# Deliberately simple: no dedicated module, just 4 more data points
# alongside the existing Fear & Greed / earnings / macro events.
MACRO_TICKERS = {
    "vix": "^VIX",
    "tnx": "^TNX",         # 10Y yield, quoted in tenths of a percent (44.2 = 4.42%)
    "dxy": "DX-Y.NYB",     # US Dollar Index — flaky on yfinance, has a fallback below
    "hyg": "HYG",
    "lqd": "LQD",
}

def _fetch_quote(ticker):
    """Returns (price, prev_close) or (None, None) on failure."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
        prev  = info.get("previousClose")
        if price is None:
            return None, None
        return round(float(price), 4), round(float(prev), 4) if prev else None
    except Exception:
        return None, None

def _pct_change(price, prev):
    if price is None or prev is None or prev == 0:
        return None
    return round(((price - prev) / prev) * 100, 2)

def fetch_macro_indicators():
    """
    Fetch VIX, 10Y yield, DXY, and HYG/LQD ratio.
    Returns a dict with current values, day-over-day change, and a
    short interpretation for each — same shape/spirit as fetch_fear_greed().
    """
    print(f"{BLUE}Fetching cross-asset macro indicators...{RESET}", end=" ", flush=True)
    result = {}

    # VIX
    vix, vix_prev = _fetch_quote(MACRO_TICKERS["vix"])
    if vix is not None:
        if vix < 15:
            vix_interp = "Low volatility — market complacent"
        elif vix < 25:
            vix_interp = "Normal range"
        elif vix < 35:
            vix_interp = "Elevated — market stress building"
        else:
            vix_interp = "Extreme — panic/crisis-level volatility"
        result["vix"] = {"value": vix, "change_pct": _pct_change(vix, vix_prev), "interpretation": vix_interp}

    # 10Y Treasury yield (^TNX is yield * 10)
    # 10Y Treasury yield (^TNX) — Yahoo's LEGACY convention quotes this as
    # yield x10 (e.g. 44.2 = 4.42%), but this was observed returning the
    # actual yield directly (e.g. 4.42) instead, which /10'd down to a
    # nonsensical 0.44%. Rather than hard-code one convention, detect it:
    # actual 10Y yields realistically run ~1-9%, so any raw value >= 15
    # must be the x10-encoded form and needs dividing; anything smaller
    # is already the real yield.
    tnx, tnx_prev = _fetch_quote(MACRO_TICKERS["tnx"])
    if tnx is not None:
        yield_pct = round(tnx / 10, 2) if tnx >= 15 else round(tnx, 2)
        yield_prev_pct = None
        if tnx_prev:
            yield_prev_pct = round(tnx_prev / 10, 2) if tnx_prev >= 15 else round(tnx_prev, 2)
        result["yield_10y"] = {
            "value": yield_pct,
            "change_pct": _pct_change(yield_pct, yield_prev_pct),
            "interpretation": "Rising yields tighten financial conditions and pressure high-multiple growth stocks"
                               if (yield_prev_pct and yield_pct > yield_prev_pct)
                               else "Falling/stable yields — supportive for duration-sensitive sectors",
        }

    # Dollar Index — try primary ticker, fall back to futures ticker if it fails
    dxy, dxy_prev = _fetch_quote(MACRO_TICKERS["dxy"])
    if dxy is None:
        dxy, dxy_prev = _fetch_quote("DX=F")
    if dxy is not None:
        result["dxy"] = {
            "value": dxy,
            "change_pct": _pct_change(dxy, dxy_prev),
            "interpretation": "Dollar strength — headwind for multinational earnings and commodities"
                               if (dxy_prev and dxy > dxy_prev)
                               else "Dollar weakness/stable — tailwind for multinationals and commodities",
        }

    # HYG/LQD ratio — high-yield vs investment-grade credit, a risk-appetite proxy
    hyg, hyg_prev = _fetch_quote(MACRO_TICKERS["hyg"])
    lqd, lqd_prev = _fetch_quote(MACRO_TICKERS["lqd"])
    if hyg is not None and lqd is not None and lqd > 0:
        ratio = round(hyg / lqd, 4)
        ratio_prev = round(hyg_prev / lqd_prev, 4) if (hyg_prev and lqd_prev and lqd_prev > 0) else None
        result["credit_risk_appetite"] = {
            "value": ratio,
            "change_pct": _pct_change(ratio, ratio_prev),
            "interpretation": "Credit spreads tightening — risk-on in credit markets"
                               if (ratio_prev and ratio > ratio_prev)
                               else "Credit spreads widening/stable — watch for risk-off credit stress",
        }

    if result:
        print(f"{GREEN}✓ {len(result)}/4 indicators fetched{RESET}")
    else:
        print(f"{YELLOW}⚠ No macro indicators could be fetched{RESET}")
    return result

# ── MAIN: FETCH ALL CONTEXT ───────────────────────────────────
def fetch_all_context(tickers=None):
    """
    Fetch all market context data and return as dict
    Also saves to data/market_context.json
    """
    print(f"\n{BOLD}{BLUE}{'='*55}")
    print(f"  MARKET AGENT — Phase 2: Market Context")
    print(f"  {datetime.now().strftime('%A %d %B %Y  %H:%M')}")
    print(f"{'='*55}{RESET}\n")

    # Import pandas here for earnings
    try:
        import pandas as pd
        globals()['pd'] = pd
    except ImportError:
        pass

    context = {}

    # 1. Fear & Greed
    context["fear_greed"] = fetch_fear_greed()

    # 2. Earnings
    if tickers:
        context["earnings"] = fetch_earnings(tickers)
    else:
        context["earnings"] = []

    # 3. Macro events
    context["macro_events"] = get_macro_events()

    # 4. Cross-asset macro indicators
    context["macro_indicators"] = fetch_macro_indicators()

    # Summary for Claude
    context["summary"] = build_context_summary(context)
    context["fetched_at"] = datetime.now().isoformat()

    # Save
    os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)
    path = os.path.join(BASE_DIR, "data", "market_context.json")
    with open(path, "w") as f:
        json.dump(context, f, indent=2, default=str)

    print(f"\n{GREEN}✓ Market context saved to data/market_context.json{RESET}")
    print_context_summary(context)
    return context

def build_context_summary(context):
    """Build a text summary to inject into Claude's prompt"""
    lines = []

    # Fear & Greed
    fg = context.get("fear_greed", {})
    if fg.get("score") is not None:
        lines.append(f"Fear & Greed Index: {fg['score']}/100 — {fg['rating']} ({fg['direction']})")
        lines.append(f"Interpretation: {fg['interpretation']}")

    # Earnings
    earnings = context.get("earnings", [])
    if earnings:
        lines.append(f"\nUpcoming Earnings (next 14 days):")
        for e in earnings:
            lines.append(f"  {e['urgency']} {e['ticker']} reports on {e['date']} ({e['days_away']} days)")

    # Macro events
    macro = context.get("macro_events", [])
    if macro:
        lines.append(f"\nKey Macro Events (next 30 days):")
        for m in macro:
            lines.append(f"  {m['urgency']} {m['emoji']} {m['event']} — {m['date_display']} ({m['days_away']} days) [{m['impact']} impact]")

    # Cross-asset macro indicators
    mi = context.get("macro_indicators", {})
    if mi:
        lines.append(f"\nCross-Asset Macro Context:")
        if "vix" in mi:
            v = mi["vix"]
            chg = f" ({v['change_pct']:+.1f}%)" if v.get("change_pct") is not None else ""
            lines.append(f"  VIX: {v['value']}{chg} — {v['interpretation']}")
        if "yield_10y" in mi:
            y = mi["yield_10y"]
            chg = f" ({y['change_pct']:+.1f}%)" if y.get("change_pct") is not None else ""
            lines.append(f"  10Y Treasury Yield: {y['value']}%{chg} — {y['interpretation']}")
        if "dxy" in mi:
            d = mi["dxy"]
            chg = f" ({d['change_pct']:+.1f}%)" if d.get("change_pct") is not None else ""
            lines.append(f"  US Dollar Index (DXY): {d['value']}{chg} — {d['interpretation']}")
        if "credit_risk_appetite" in mi:
            c = mi["credit_risk_appetite"]
            chg = f" ({c['change_pct']:+.1f}%)" if c.get("change_pct") is not None else ""
            lines.append(f"  Credit Risk Appetite (HYG/LQD): {c['value']}{chg} — {c['interpretation']}")

    return "\n".join(lines)

def print_context_summary(context):
    fg      = context.get("fear_greed", {})
    earnings= context.get("earnings", [])
    macro   = context.get("macro_events", [])

    print(f"\n{BOLD}{'─'*55}")
    print(f"  MARKET CONTEXT SUMMARY")
    print(f"{'─'*55}{RESET}")

    if fg.get("score") is not None:
        score = fg["score"]
        if score <= 25:
            col = RED
        elif score <= 45:
            col = YELLOW
        elif score <= 55:
            col = RESET
        elif score <= 75:
            col = YELLOW
        else:
            col = RED
        print(f"\n  Fear & Greed: {col}{BOLD}{score}/100 — {fg['rating']}{RESET}")
        print(f"  {fg['interpretation']}")
        print(f"  Previous close: {fg['prev_close']} | Previous week: {fg['prev_week']}")

    if earnings:
        print(f"\n  Earnings ({len(earnings)} upcoming):")
        for e in earnings:
            print(f"    {e['urgency']} {e['ticker']} — {e['date']} ({e['days_away']} days)")

    if macro:
        print(f"\n  Macro Events ({len(macro)} in 30 days):")
        for m in macro:
            print(f"    {m['urgency']} {m['emoji']} {m['event']} — {m['date_display']} ({m['days_away']} days)")

    mi = context.get("macro_indicators", {})
    if mi:
        print(f"\n  Cross-Asset Macro Context:")
        if "vix" in mi:
            v = mi["vix"]; chg = f" ({v['change_pct']:+.1f}%)" if v.get("change_pct") is not None else ""
            print(f"    VIX: {v['value']}{chg} — {v['interpretation']}")
        if "yield_10y" in mi:
            y = mi["yield_10y"]; chg = f" ({y['change_pct']:+.1f}%)" if y.get("change_pct") is not None else ""
            print(f"    10Y Yield: {y['value']}%{chg} — {y['interpretation']}")
        if "dxy" in mi:
            d = mi["dxy"]; chg = f" ({d['change_pct']:+.1f}%)" if d.get("change_pct") is not None else ""
            print(f"    DXY: {d['value']}{chg} — {d['interpretation']}")
        if "credit_risk_appetite" in mi:
            c = mi["credit_risk_appetite"]; chg = f" ({c['change_pct']:+.1f}%)" if c.get("change_pct") is not None else ""
            print(f"    HYG/LQD: {c['value']}{chg} — {c['interpretation']}")

    print()

if __name__ == "__main__":
    # run_agent.sh step 6: context for the investor's real watchlist (this
    # used to run on a hardcoded test list: MSFT, AVGO, TSLA, XOM...).
    # agent_brain.py reuses this file when it's fresh instead of refetching.
    try:
        with open(os.path.join(BASE_DIR, "data", "market_data.json")) as f:
            tickers = [s.get("ticker") for s in json.load(f).get("stocks", []) if s.get("ticker")]
    except Exception:
        tickers = []
    fetch_all_context(tickers)
