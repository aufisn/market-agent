# ============================================================
# MARKET AGENT V4.5 — Layer 2: Live Data Fetcher
# Tier 1 enriched: price, volume, market cap, valuation,
# fundamentals, earnings dates — all from Yahoo Finance
# ============================================================

import yfinance as yf
import pandas as pd
import json
import os
from datetime import datetime
from dotenv import load_dotenv
from common import GREEN, RED, YELLOW, BLUE, BOLD, RESET, safe_float

load_dotenv()
BASE_DIR          = os.path.dirname(os.path.abspath(__file__))


# ── LOAD WATCHLIST ────────────────────────────────────────────
def load_watchlist(path=None):
    if path is None:
        path = os.path.join(BASE_DIR, "watchlist.csv")
    try:
        df = pd.read_csv(path)
        # Ensure required columns exist
        for col in ["list_type","quantity","buy_price","target","stop_loss"]:
            if col not in df.columns:
                df[col] = ""
        df["list_type"] = df["list_type"].fillna("interested").str.lower().str.strip()
        # Clean numeric columns
        for col in ["quantity","buy_price","target","stop_loss"]:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

        cash_rows    = df[df["list_type"] == "cash"]
        holding_rows = df[df["list_type"] == "holding"]
        interest_rows= df[df["list_type"] == "interested"]

        cash = float(cash_rows["quantity"].sum()) if not cash_rows.empty else 0.0

        print(f"{GREEN}✓ Watchlist loaded — "
              f"{len(holding_rows)} holdings, "
              f"{len(interest_rows)} interested, "
              f"${cash:,.0f} cash{RESET}")
        return df.to_dict("records"), cash
    except FileNotFoundError:
        print(f"{RED}✗ watchlist.csv not found{RESET}")
        return [], 0.0

# ── DATA VALIDATION ───────────────────────────────────────────
# A run must not overwrite good data with an empty or partial feed: if
# fewer than this share of holdings get a live price, fetch_all() exits
# non-zero WITHOUT writing market_data.json, so run_agent.sh stops, sends
# its failure alert, and the previous analysis stays on the dashboard.
MIN_HOLDING_COVERAGE = 0.8
STALE_AFTER_TRADING_DAYS = 2


class DataValidationError(Exception):
    pass


def trading_days_between(earlier, later):
    """Trading days from earlier (exclusive) to later (inclusive), skipping
    NYSE holidays (market_calendar)."""
    from market_calendar import trading_days_between as _tdb
    return _tdb(earlier, later)


def load_previous_prices():
    path = os.path.join(BASE_DIR, "data", "market_data.json")
    try:
        with open(path) as f:
            prev = json.load(f)
        return {s.get("ticker"): s for s in prev.get("stocks", []) if s.get("ticker") and s.get("price")}
    except Exception:
        return {}


# ── FETCH PRICE ───────────────────────────────────────────────
def fetch_price(ticker):
    try:
        stock = yf.Ticker(ticker)
        info  = stock.info

        # PRIMARY: regularMarketPrice — always current, no split distortion
        current = safe_float(
            info.get("regularMarketPrice") or
            info.get("currentPrice") or
            info.get("previousClose") or 0
        )

        # FALLBACK: history
        if current == 0:
            hist   = stock.history(period="5d", auto_adjust=False)
            closes = hist["Close"].dropna() if not hist.empty else pd.Series()
            if closes.empty:
                return None
            current = round(float(closes.iloc[-1]), 4)

        current = round(current, 4)

        # ── PRICE TIMESTAMP / STALENESS ───────────────────────
        # regularMarketTime is when Yahoo last saw a trade. A price that
        # stopped updating used to be treated as live indefinitely.
        price_as_of, price_stale = None, None
        rmt = info.get("regularMarketTime")
        if rmt:
            try:
                as_of = datetime.fromtimestamp(int(rmt))
                price_as_of = as_of.strftime("%Y-%m-%d %H:%M")
                price_stale = trading_days_between(as_of.date(), datetime.now().date()) > STALE_AFTER_TRADING_DAYS
            except Exception:
                pass

        # ── PRICE FIELDS ──────────────────────────────────────
        prev_close = safe_float(
            info.get("regularMarketPreviousClose") or
            info.get("previousClose") or current
        )
        chg = round(((current - prev_close) / prev_close) * 100, 2) if prev_close > 0 else 0.0

        open_p  = safe_float(info.get("regularMarketOpen")    or info.get("open"))
        day_hi  = safe_float(info.get("regularMarketDayHigh") or info.get("dayHigh"))
        day_lo  = safe_float(info.get("regularMarketDayLow")  or info.get("dayLow"))
        w52_hi  = safe_float(info.get("fiftyTwoWeekHigh"))
        w52_lo  = safe_float(info.get("fiftyTwoWeekLow"))

        # % distance from 52-week high/low
        pct_from_52w_high = round(((current - w52_hi) / w52_hi) * 100, 2) if w52_hi > 0 else None
        pct_from_52w_low  = round(((current - w52_lo) / w52_lo) * 100, 2) if w52_lo > 0 else None

        # ── VOLUME FIELDS ─────────────────────────────────────
        volume     = int(safe_float(info.get("regularMarketVolume") or info.get("volume") or 0))
        avg_vol_10 = int(safe_float(info.get("averageDailyVolume10Day") or 0))
        avg_vol_3m = int(safe_float(info.get("averageVolume") or avg_vol_10 or 1))
        vol_ratio  = round(volume / avg_vol_3m, 2) if avg_vol_3m > 0 else 1.0

        # ── MARKET FIELDS ─────────────────────────────────────
        market_cap = safe_float(info.get("marketCap") or 0)
        beta       = safe_float(info.get("beta") or info.get("beta3Year"))

        # Market cap classification
        if market_cap >= 200e9:   cap_class = "Mega-cap"
        elif market_cap >= 10e9:  cap_class = "Large-cap"
        elif market_cap >= 2e9:   cap_class = "Mid-cap"
        elif market_cap >= 300e6: cap_class = "Small-cap"
        else:                     cap_class = "Micro-cap"

        # ── VALUATION FIELDS ──────────────────────────────────
        pe_trailing = safe_float(info.get("trailingPE"))
        pe_forward  = safe_float(info.get("forwardPE"))
        peg_ratio   = safe_float(info.get("pegRatio"))
        price_sales = safe_float(info.get("priceToSalesTrailingTwelveMonths"))
        price_book  = safe_float(info.get("priceToBook"))
        ev_ebitda   = safe_float(info.get("enterpriseToEbitda"))
        div_yield   = round(safe_float(info.get("dividendYield")) * 100, 2) if info.get("dividendYield") else 0.0
        payout_ratio= round(safe_float(info.get("payoutRatio")) * 100, 2) if info.get("payoutRatio") else 0.0

        # ── FUNDAMENTAL FIELDS (from yfinance info) ───────────
        rev_ttm          = safe_float(info.get("totalRevenue"))
        gross_margin     = round(safe_float(info.get("grossMargins")) * 100, 2) if info.get("grossMargins") else None
        operating_margin = round(safe_float(info.get("operatingMargins")) * 100, 2) if info.get("operatingMargins") else None
        net_margin       = round(safe_float(info.get("profitMargins")) * 100, 2) if info.get("profitMargins") else None
        roe              = round(safe_float(info.get("returnOnEquity")) * 100, 2) if info.get("returnOnEquity") else None
        eps_ttm          = safe_float(info.get("trailingEps"))
        eps_forward      = safe_float(info.get("forwardEps"))
        rev_growth_yoy   = round(safe_float(info.get("revenueGrowth")) * 100, 2) if info.get("revenueGrowth") else None
        earnings_growth  = round(safe_float(info.get("earningsGrowth")) * 100, 2) if info.get("earningsGrowth") else None
        debt_to_equity   = round(safe_float(info.get("debtToEquity")), 2) if info.get("debtToEquity") else None
        free_cash_flow   = safe_float(info.get("freeCashflow"))
        fcf_yield        = round((free_cash_flow / market_cap) * 100, 2) if market_cap > 0 and free_cash_flow else None

        # ── EVENT FIELDS ──────────────────────────────────────
        next_earnings_date = None
        days_to_earnings   = None
        try:
            ne = info.get("earningsTimestamp") or info.get("earningsTimestampStart")
            if ne:
                ne_dt = datetime.fromtimestamp(ne)
                days_to_earnings = (ne_dt - datetime.now()).days
                next_earnings_date = ne_dt.strftime("%Y-%m-%d")
        except Exception:
            pass

        # ── COMPUTE TECHNICAL SCORE (Tier 1 numeric) ──────────
        # Will be enriched further with RSI/SMA from Alpha Vantage
        technical_signal = "NEUTRAL"
        if pct_from_52w_high is not None:
            if pct_from_52w_high > -5:   technical_signal = "NEAR_HIGH"
            elif pct_from_52w_high < -20: technical_signal = "PULLBACK"

        return {
            "ticker":             ticker,
            # Price
            "price":              current,
            "price_as_of":        price_as_of,
            "price_stale":        price_stale,
            "price_open":         round(open_p, 4) if open_p else None,
            "price_day_high":     round(day_hi, 4) if day_hi else None,
            "price_day_low":      round(day_lo, 4) if day_lo else None,
            "price_prev_close":   round(prev_close, 4),
            "change_pct":         chg,
            # 52-week
            "52w_high":           round(w52_hi, 4),
            "52w_low":            round(w52_lo, 4),
            "pct_from_52w_high":  pct_from_52w_high,
            "pct_from_52w_low":   pct_from_52w_low,
            # Volume
            "volume":             volume,
            "avg_volume":         avg_vol_3m,
            "avg_volume_10d":     avg_vol_10,
            "volume_ratio":       vol_ratio,
            # Market
            "market_cap":         market_cap,
            "market_cap_class":   cap_class,
            "beta":               round(beta, 2) if beta else None,
            # Valuation
            "pe_trailing":        round(pe_trailing, 2) if pe_trailing else None,
            "pe_forward":         round(pe_forward, 2) if pe_forward else None,
            "peg_ratio":          round(peg_ratio, 2) if peg_ratio else None,
            "price_to_sales":     round(price_sales, 2) if price_sales else None,
            "price_to_book":      round(price_book, 2) if price_book else None,
            "ev_to_ebitda":       round(ev_ebitda, 2) if ev_ebitda else None,
            "dividend_yield":     div_yield,
            "payout_ratio":       payout_ratio,
            # Fundamentals
            "revenue_ttm":        rev_ttm,
            "revenue_growth_yoy": rev_growth_yoy,
            "earnings_growth_yoy":earnings_growth,
            "gross_margin":       gross_margin,
            "operating_margin":   operating_margin,
            "net_margin":         net_margin,
            "roe":                roe,
            "eps_ttm":            eps_ttm,
            "eps_forward":        eps_forward,
            "debt_to_equity":     debt_to_equity,
            "free_cash_flow":     free_cash_flow,
            "fcf_yield":          fcf_yield,
            # Events
            "next_earnings_date": next_earnings_date,
            "days_to_earnings":   days_to_earnings,
            # Company / Sector — fetched directly from Yahoo Finance
            # Works for any stock automatically — no hardcoding needed
            "sector":             info.get("sector") or info.get("industry") or "",
            "company_name":       info.get("longName") or info.get("shortName") or "",
            # Signal
            "technical_signal":   technical_signal,
            "data_source":        "Yahoo Finance",
        }
    except Exception as e:
        print(f"  {YELLOW}⚠ {ticker}: {e}{RESET}")
        return None

# ── FETCH TECHNICALS ──────────────────────────────────────────
# RSI(14) / SMA50 / SMA200 computed from Yahoo daily history (technicals.py).
# The Alpha Vantage free tier this used to call (25 requests/day, 3 per
# ticker) returned nothing in practice — every RSI was None.
def fetch_technicals(ticker):
    from technicals import compute_technicals
    return compute_technicals(ticker)

# ── PRINT STOCK SUMMARY ───────────────────────────────────────
def print_summary(stock, price_data, tech_data):
    chg   = price_data.get("change_pct", 0)
    arrow = f"{GREEN}▲" if chg >= 0 else f"{RED}▼"
    rsi   = tech_data.get("rsi")
    curr  = safe_float(price_data.get("price"))
    buy   = safe_float(stock.get("buy_price"))
    qty   = safe_float(stock.get("quantity"))

    pnl_str = ""
    if buy > 0 and curr > 0 and qty > 0:
        pnl_pct = round(((curr - buy) / buy) * 100, 2)
        pnl_usd = round((curr - buy) * qty, 2)
        pnl_str = f"  P&L:      {'+' if pnl_pct>=0 else ''}{pnl_pct}% (${pnl_usd:+,.2f})"

    cap   = price_data.get("market_cap_class", "")
    mc    = price_data.get("market_cap", 0)
    mc_s  = f"${mc/1e9:.1f}B" if mc >= 1e9 else f"${mc/1e6:.0f}M" if mc > 0 else "N/A"
    pe    = price_data.get("pe_forward") or price_data.get("pe_trailing")
    pe_s  = f"Fwd P/E: {pe}" if pe else ""
    h52   = price_data.get("pct_from_52w_high")
    h52_s = f"{h52:+.1f}% from 52wH" if h52 is not None else ""
    mgn   = price_data.get("operating_margin")
    mgn_s = f"OpMgn: {mgn}%" if mgn else ""
    dte   = price_data.get("days_to_earnings")
    dte_s = f"  ⚠ Earnings in {dte}d" if dte is not None and 0 <= dte <= 14 else ""
    beta  = price_data.get("beta")
    beta_s= f"Beta: {beta}" if beta else ""

    print(f"\n  {BOLD}{stock['ticker']}{RESET} — {stock['name']}  [{cap} {mc_s}]")
    print(f"  Price:    ${curr}  {arrow} {chg}%{RESET}  |  {h52_s}")
    if qty > 0:
        print(f"  Qty:      {int(qty)} shares  |  Value: ${curr*qty:,.2f}")
    if pnl_str:
        print(pnl_str)
    print(f"  RSI:      {rsi if rsi else 'N/A'}  |  Vol ratio: {price_data.get('volume_ratio','N/A')}x  |  {pe_s}  |  {mgn_s}  |  {beta_s}")
    if dte_s:
        print(f"  {YELLOW}{dte_s}{RESET}")

# ── MAIN FETCH ────────────────────────────────────────────────
def fetch_all():
    print(f"\n{BOLD}{BLUE}{'='*55}")
    print(f"  MARKET AGENT V4.5 — Data Fetch")
    print(f"  {datetime.now().strftime('%A %d %B %Y  %H:%M')}")
    print(f"{'='*55}{RESET}\n")

    # Try tracker JSON first, fall back to watchlist.csv
    tracker_path = os.path.join(BASE_DIR, "data", "tracker_snapshot.json")
    if os.path.exists(tracker_path):
        try:
            import json as _json
            with open(tracker_path) as _f:
                snap = _json.load(_f)
            us_h = snap.get("us_holdings", [])
            cash = safe_float(snap.get("cash_usd", 0))
            if us_h:
                print(f"{GREEN}✓ Using tracker data: {len(us_h)} US holdings{RESET}")
                # Load interested stocks from watchlist.csv and merge
                wl_all, wl_cash = load_watchlist()
                interested = [s for s in wl_all if s.get("list_type") == "interested"]
                watchlist  = us_h + interested
                if cash == 0:
                    cash = wl_cash
                print(f"{GREEN}✓ Added {len(interested)} interested stocks from watchlist{RESET}")
            else:
                watchlist, cash = load_watchlist()
        except Exception as e:
            print(f"{YELLOW}⚠ Tracker read failed: {e} — using watchlist{RESET}")
            watchlist, cash = load_watchlist()
    else:
        watchlist, cash = load_watchlist()

    if not watchlist:
        return [], 0.0

    all_data   = []
    total_cost = 0.0
    total_val  = 0.0
    previous   = load_previous_prices()
    holdings_expected = sum(1 for s in watchlist if str(s.get("list_type", "")).strip().lower() == "holding")
    holdings_live = 0
    carried_forward = []

    for stock in watchlist:
        ltype = str(stock.get("list_type","")).strip().lower()
        if ltype == "cash":
            continue  # handled separately

        ticker = str(stock.get("ticker","")).strip()
        if not ticker:
            continue
        ltype  = stock.get("list_type", "interested")

        print(f"{BLUE}Fetching {ticker}...{RESET}", end=" ", flush=True)
        price_data = fetch_price(ticker)

        if not price_data:
            prev = previous.get(ticker)
            if ltype == "holding" and prev:
                # Keep the position visible at its last known price, clearly
                # flagged, instead of silently dropping it from the portfolio.
                price_data = {k: v for k, v in prev.items()
                              if k not in ("quantity", "buy_price", "cost_basis", "current_value",
                                           "pnl_usd", "pnl_pct", "fetched_at", "list_type")}
                price_data["price_stale"] = True
                price_data["price_carried_forward"] = True
                carried_forward.append(ticker)
                print(f"{YELLOW}no live price — carrying forward ${price_data.get('price')} "
                      f"(as of {price_data.get('price_as_of') or prev.get('fetched_at', '?')[:16]}){RESET}")
            else:
                print(f"{RED}skipped{RESET}")
                continue
        else:
            if ltype == "holding":
                holdings_live += 1
            flag = f" {YELLOW}(STALE — last trade {price_data.get('price_as_of')}){RESET}" if price_data.get("price_stale") else ""
            print(f"{GREEN}✓ ${price_data['price']}{RESET}{flag}")

        # Technicals for US/Gulf stocks only
        if ltype in ("holding", "interested"):
            tech_data = fetch_technicals(ticker)
        else:
            tech_data = {"rsi": None, "sma_50": None, "sma_200": None, "atr_14": None, "atr_pct": None}

        print_summary(stock, price_data, tech_data)

        # Calculate position values
        buy  = safe_float(stock.get("buy_price"))
        qty  = safe_float(stock.get("quantity"))
        curr = safe_float(price_data.get("price"))

        cost_basis    = round(buy * qty, 2)
        current_value = round(curr * qty, 2)
        pnl_usd       = round(current_value - cost_basis, 2)
        pnl_pct       = round(((curr - buy) / buy) * 100, 2) if buy > 0 and curr > 0 else 0.0

        if ltype == "holding" and qty > 0:
            total_cost += cost_basis
            total_val  += current_value

        combined = {
            **stock,
            **price_data,
            **tech_data,
            "buy_price":     buy if buy > 0 else "",
            "quantity":      qty,
            "cost_basis":    cost_basis if buy > 0 and qty > 0 else 0,
            "current_value": current_value if qty > 0 else 0,
            "pnl_usd":       pnl_usd if buy > 0 and qty > 0 else 0,
            "pnl_pct":       pnl_pct if buy > 0 and qty > 0 else 0,
            "fetched_at":    datetime.now().isoformat(),
        }
        all_data.append(combined)

    # Portfolio summary
    total_pnl     = round(total_val - total_cost, 2)
    total_pnl_pct = round((total_pnl / total_cost) * 100, 2) if total_cost > 0 else 0
    total_capital = round(total_val + cash, 2)

    print(f"\n{BOLD}{GREEN}{'─'*55}")
    print(f"  PORTFOLIO SUMMARY")
    print(f"{'─'*55}{RESET}")
    print(f"  Cost Basis:     ${total_cost:,.2f}")
    print(f"  Current Value:  ${total_val:,.2f}")
    print(f"  P&L:            ${total_pnl:+,.2f} ({total_pnl_pct:+.2f}%)")
    print(f"  Cash Available: ${cash:,.2f}")
    print(f"  Total Capital:  ${total_capital:,.2f}")

    # ── VALIDATE BEFORE WRITING ───────────────────────────────
    coverage = holdings_live / holdings_expected if holdings_expected else 1.0
    if holdings_expected and (coverage < MIN_HOLDING_COVERAGE or total_val <= 0):
        raise DataValidationError(
            f"only {holdings_live}/{holdings_expected} holdings returned a live price "
            f"(need {MIN_HOLDING_COVERAGE:.0%}), portfolio value ${total_val:,.0f} — "
            f"market data looks broken; previous data left unchanged")
    stale = [s["ticker"] for s in all_data if s.get("price_stale")]
    if stale:
        print(f"{YELLOW}⚠ Stale or carried-forward prices: {', '.join(stale)}{RESET}")

    # Save to JSON
    os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)
    output = {
        "stocks":          all_data,
        "cash":            cash,
        "total_cost":      total_cost,
        "total_value":     total_val,
        "total_pnl_usd":   total_pnl,
        "total_pnl_pct":   total_pnl_pct,
        "total_capital":   total_capital,
        "fetched_at":      datetime.now().isoformat(),
        "stale_tickers":   stale,
        "holdings_live_coverage": round(coverage, 3),
    }
    path = os.path.join(BASE_DIR, "data", "market_data.json")
    write_json_atomic(path, output)

    print(f"\n{GREEN}✓ Data saved to data/market_data.json{RESET}\n")

    # ── FETCH PERFORMANCE PRICES ──────────────────────────────
    perf_prices = fetch_performance_prices()
    if perf_prices:
        output["performance_prices"] = perf_prices
        write_json_atomic(path, output)
        print(f"{GREEN}✓ Performance prices updated: {list(perf_prices.keys())}{RESET}\n")

    return all_data, cash

def write_json_atomic(path, obj):
    """Write-then-rename so readers (dashboard, later steps) never see a half-written file."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, default=str)
    os.replace(tmp, path)


# ── FETCH PERFORMANCE PRICES ──────────────────────────────────
def fetch_performance_prices():
    perf_log_path = os.path.join(BASE_DIR, "logs", "performance_log.json")
    if not os.path.exists(perf_log_path):
        return {}
    try:
        with open(perf_log_path) as f:
            perf_log = json.load(f)
    except Exception as e:
        print(f"{YELLOW}⚠ Could not read performance_log.json: {e}{RESET}")
        return {}
    open_tickers = set()
    for entry in perf_log:
        if entry.get("status") == "OPEN" and entry.get("ticker"):
            open_tickers.add(entry["ticker"].upper())
    if not open_tickers:
        return {}
    print(f"\n{BLUE}Fetching prices for {len(open_tickers)} Performance signal(s)...{RESET}")
    perf_prices = {}
    for ticker in sorted(open_tickers):
        print(f"  {BLUE}{ticker}...{RESET}", end=" ", flush=True)
        try:
            data = fetch_price(ticker)
            if data and data.get("price"):
                perf_prices[ticker] = {
                    "price":      data["price"],
                    "change_pct": data.get("change_pct", 0),
                    "fetched_at": datetime.now().isoformat()
                }
                print(f"{GREEN}✓ ${data['price']}{RESET}")
            else:
                print(f"{YELLOW}no price{RESET}")
        except Exception as e:
            print(f"{RED}error: {e}{RESET}")
    return perf_prices

if __name__ == "__main__":
    import sys
    try:
        fetch_all()
    except DataValidationError as e:
        print(f"\n{RED}✗ DATA VALIDATION FAILED: {e}{RESET}\n")
        sys.exit(2)
