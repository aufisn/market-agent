# ============================================================
# MARKET AGENT V4.5 — Portfolio Analysis Engine
# Feature 1: Correlation Analysis
# Feature 2: Rebalancing Calculator (iterative post-trade simulation)
# Feature 3: Monte Carlo Simulation
# ============================================================

import json
import os
import math
from datetime import datetime
from dotenv import load_dotenv
from common import GREEN, RED, YELLOW, BLUE, BOLD, RESET, safe_float

load_dotenv()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ── LOAD PRICE HISTORY ────────────────────────────────────────
def fetch_price_history(tickers, days=90):
    """Fetch historical daily returns for correlation analysis"""
    import yfinance as yf
    print(f"{BLUE}Fetching {days}-day price history for correlation...{RESET}")
    histories = {}
    for ticker in tickers:
        try:
            stock  = yf.Ticker(ticker)
            hist   = stock.history(period=f"{days}d", auto_adjust=False)
            closes = hist["Close"].dropna()
            if len(closes) < 20:
                continue
            # Daily returns
            returns = []
            prices  = list(closes)
            for i in range(1, len(prices)):
                if prices[i-1] > 0:
                    returns.append((prices[i] - prices[i-1]) / prices[i-1])
            histories[ticker] = returns
            print(f"  {GREEN}✓ {ticker}: {len(returns)} daily returns{RESET}")
        except Exception as e:
            print(f"  {YELLOW}⚠ {ticker}: {e}{RESET}")
    return histories

# ── FEATURE 1: CORRELATION ANALYSIS ──────────────────────────
def calculate_correlation(returns_a, returns_b):
    """Pearson correlation coefficient between two return series"""
    n = min(len(returns_a), len(returns_b))
    if n < 10:
        return None
    a = returns_a[-n:]
    b = returns_b[-n:]
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    cov = sum((a[i]-mean_a)*(b[i]-mean_b) for i in range(n)) / n
    std_a = math.sqrt(sum((x-mean_a)**2 for x in a) / n)
    std_b = math.sqrt(sum((x-mean_b)**2 for x in b) / n)
    if std_a == 0 or std_b == 0:
        return None
    return round(cov / (std_a * std_b), 3)

def run_correlation_analysis(holdings, histories):
    """Build correlation matrix for all holdings"""
    print(f"\n{BLUE}Running correlation analysis...{RESET}")
    tickers = [h.get("ticker","") for h in holdings if h.get("ticker","") in histories]
    matrix  = {}
    pairs   = []

    for i, t1 in enumerate(tickers):
        matrix[t1] = {}
        for j, t2 in enumerate(tickers):
            if t1 == t2:
                matrix[t1][t2] = 1.0
            elif j > i:
                corr = calculate_correlation(histories[t1], histories[t2])
                matrix[t1][t2] = corr
                if t2 not in matrix:
                    matrix[t2] = {}
                matrix[t2][t1] = corr
                if corr is not None:
                    pairs.append({
                        "pair":        f"{t1}/{t2}",
                        "ticker_a":    t1,
                        "ticker_b":    t2,
                        "correlation": corr,
                        "level":       corr_level(corr),
                        "risk":        corr_risk(corr),
                    })
            else:
                matrix[t1][t2] = matrix.get(t2,{}).get(t1)

    pairs.sort(key=lambda x: abs(x.get("correlation") or 0), reverse=True)

    # Overall diversification score (lower avg correlation = better)
    valid = [p["correlation"] for p in pairs if p["correlation"] is not None]
    avg_corr = sum(valid) / len(valid) if valid else 0
    div_score = round((1 - avg_corr) * 100)

    result = {
        "matrix":           matrix,
        "pairs":            pairs,
        "avg_correlation":  round(avg_corr, 3),
        "diversification_score": div_score,
        "diversification_label": div_label(div_score),
        "high_risk_pairs":  [p for p in pairs if p["correlation"] is not None and p["correlation"] > 0.7],
        "summary": build_corr_summary(pairs, div_score),
    }

    print(f"{GREEN}✓ Correlation: avg={avg_corr:.2f}, diversity score={div_score}/100{RESET}")
    return result

def adjust_diversification(correlation, holdings, data, strategy):
    """The score above is 1 - average pairwise correlation, so a 3-stock
    portfolio with 78% in one sector could read "Well Diversified" while
    the strategy's own caps were breached. Penalise what actually breaks
    diversification and say why."""
    ps      = (strategy or {}).get("position_sizing", {})
    cap_sec = ps.get("max_sector_pct", 30)
    min_pos = ps.get("min_positions", 6)
    total   = safe_float(data.get("total_capital") or (data.get("portfolio_summary") or {}).get("total_capital"))
    score   = correlation.get("diversification_score", 0)
    reasons = []
    for p in correlation.get("pairs", []):
        if p.get("correlation") is not None and p["correlation"] > 0.85:
            score -= 15
            reasons.append(f"{p['pair']} move almost together ({p['correlation']:+.2f})")
    if total > 0:
        sectors = {}
        for h in holdings:
            sectors[h.get("sector") or "Unknown"] = sectors.get(h.get("sector") or "Unknown", 0) + safe_float(h.get("current_value"))
        for sec, val in sorted(sectors.items(), key=lambda x: -x[1]):
            pct = val / total * 100
            if pct > cap_sec:
                score = min(score, 50) - round(pct - cap_sec)
                reasons.append(f"{sec} is {pct:.0f}% of capital (cap {cap_sec}%)")
    n = len([h for h in holdings if safe_float(h.get("current_value")) > 0])
    if 0 < n < min_pos:
        score -= 10
        reasons.append(f"only {n} positions (target {min_pos}+)")
    score = max(0, min(100, round(score)))
    correlation["diversification_score_correlation_only"] = correlation.get("diversification_score")
    correlation["diversification_score"] = score
    correlation["diversification_label"] = div_label(score)
    correlation["diversification_reasons"] = reasons
    if reasons:
        correlation["summary"] = correlation.get("summary", "") + "\nConcentration: " + "; ".join(reasons)
    return correlation


def corr_level(c):
    if c is None: return "Unknown"
    ac = abs(c)
    if ac >= 0.8: return "Very High"
    if ac >= 0.6: return "High"
    if ac >= 0.4: return "Moderate"
    if ac >= 0.2: return "Low"
    return "Negligible"

def corr_risk(c):
    if c is None: return "Unknown"
    if c > 0.7:  return "HIGH — positions move together, limited diversification"
    if c > 0.5:  return "MEDIUM — some overlap in market exposure"
    if c > 0.2:  return "LOW — reasonable diversification"
    if c < -0.2: return "GOOD — negative correlation, natural hedge"
    return "MINIMAL — well diversified pair"

def div_label(score):
    if score >= 75: return "Well Diversified"
    if score >= 55: return "Adequately Diversified"
    if score >= 35: return "Moderately Concentrated"
    return "Highly Concentrated"

def build_corr_summary(pairs, div_score):
    lines = [f"Diversification Score: {div_score}/100"]
    for p in pairs[:3]:
        c = p.get("correlation")
        if c is not None:
            lines.append(f"{p['pair']}: {c:.2f} correlation ({p['level']})")
    return " | ".join(lines)

# ── FEATURE 2: REBALANCING CALCULATOR ────────────────────────
def run_rebalancing(holdings, data, strategy):
    """
    Iterative rebalancing that accounts for capital destruction when
    selling at a loss. Simulates each trade, recalculates the entire
    portfolio against the reduced capital, and repeats until all
    positions are within 5% of their targets or 10 iterations pass.
    """
    print(f"\n{BLUE}Running iterative rebalancing calculator...{RESET}")

    ps           = strategy.get("position_sizing", {})
    max_new      = ps.get("max_single_stock_pct",    10) / 100   # 10% for new/priority exits
    max_existing = ps.get("max_existing_holding_pct",20) / 100   # 20% for established holdings
    min_cash_pct = ps.get("min_cash_pct",            20) / 100   # 20% minimum cash
    tolerance    = 0.05   # 5% tolerance — within this is considered balanced
    max_iter     = 50     # safety ceiling only — loop exits via convergence first

    priority_exits = strategy.get("rebalancing", {}).get("priority_exits", [])

    # ── BUILD INITIAL SIMULATION STATE ───────────────────────
    # Each position: {ticker, shares, buy_price, current_price, current_value, cost_basis}
    positions = []
    for h in holdings:
        ticker     = h.get("ticker", "")
        curr_price = safe_float(h.get("price", 0))
        curr_val   = safe_float(h.get("current_value", 0))
        cost       = safe_float(h.get("cost_basis", 0))
        qty        = safe_float(h.get("quantity", 0))
        buy_p      = safe_float(h.get("buy_price", 0))
        if curr_price <= 0 or curr_val <= 0:
            continue
        positions.append({
            "ticker":        ticker,
            "shares":        qty,
            "buy_price":     buy_p,
            "current_price": curr_price,
            "current_value": curr_val,
            "cost_basis":    cost,
            "is_priority":   ticker in priority_exits,
        })

    sim_cash        = safe_float(data.get("cash", 0))
    all_actions     = []
    iteration       = 0
    prev_capital    = None
    convergence_reason = None  # will be set when loop exits

    # ── ITERATIVE SIMULATION ──────────────────────────────────
    while iteration < max_iter:
        iteration += 1

        # Recalculate total capital after last iteration's trades
        equity       = sum(p["current_value"] for p in positions)
        sim_total    = equity + sim_cash

        if sim_total <= 0:
            convergence_reason = "ZERO_CAPITAL"
            break

        # Convergence check — if capital hasn't changed meaningfully, stop
        if prev_capital is not None and abs(sim_total - prev_capital) < 1.0:
            convergence_reason = "CONVERGED"
            print(f"  {YELLOW}Converged at iteration {iteration} — no further sells possible (positions too small to sell whole shares){RESET}")
            break
        prev_capital = sim_total

        # Check each position against its limit
        violation_found = False
        for p in sorted(positions, key=lambda x: -x["current_value"]):
            ticker    = p["ticker"]
            curr_val  = p["current_value"]
            curr_wt   = curr_val / sim_total
            max_wt    = max_new if p["is_priority"] else max_existing

            if curr_wt <= max_wt + tolerance:
                continue  # within tolerance — no action needed

            violation_found = True

            # How much to sell to bring within tolerance
            target_val  = max_wt * sim_total
            sell_val    = curr_val - target_val
            curr_price  = p["current_price"]
            shares_sell = math.floor(sell_val / curr_price) if curr_price > 0 else 0

            if shares_sell <= 0:
                continue

            # Simulate the trade — actual proceeds at CURRENT price (may be below cost)
            actual_proceeds = round(shares_sell * curr_price, 2)
            cost_of_shares  = round(shares_sell * p["buy_price"], 2) if p["buy_price"] > 0 else actual_proceeds
            realised_loss   = round(actual_proceeds - cost_of_shares, 2)

            # Update simulation state
            p["shares"]        -= shares_sell
            p["current_value"]  = round(p["shares"] * curr_price, 2)
            p["cost_basis"]     = round(p["shares"] * p["buy_price"], 2) if p["buy_price"] > 0 else 0
            sim_cash           += actual_proceeds

            # Recalculate total AFTER this trade (capital destroyed if sold at loss)
            equity    = sum(pos["current_value"] for pos in positions)
            sim_total = equity + sim_cash
            new_wt    = p["current_value"] / sim_total if sim_total > 0 else 0

            all_actions.append({
                "action":          "SELL",
                "ticker":          ticker,
                "shares":          shares_sell,
                "amount":          actual_proceeds,
                "realised_pnl":    realised_loss,
                "reason":          (f"Overweight at {curr_wt*100:.1f}% "
                                    f"(max {max_wt*100:.0f}%) — "
                                    f"sell {shares_sell} shares @ ${curr_price:.2f}"),
                "current_weight":  round(curr_wt * 100, 1),
                "target_weight":   round(max_wt * 100, 1),
                "post_trade_weight": round(new_wt * 100, 1),
                "post_trade_capital": round(sim_total, 2),
                "priority":        "HIGH" if curr_wt - max_wt > 0.10 else "MEDIUM",
                "iteration":       iteration,
            })
            break  # one trade per iteration — recalculate everything before next

        if not violation_found:
            convergence_reason = "BALANCED"
            break  # all positions within tolerance — done

    # Set reason if we hit the safety ceiling
    if convergence_reason is None:
        convergence_reason = "MAX_ITERATIONS"
        print(f"  {YELLOW}Warning: hit safety ceiling of {max_iter} iterations — some violations may remain{RESET}")

    # ── POST-SIMULATION SUMMARY ───────────────────────────────
    # Final state after all simulated trades
    final_equity  = sum(p["current_value"] for p in positions)
    final_total   = final_equity + sim_cash
    total_proceeds= sum(a["amount"] for a in all_actions if a["action"]=="SELL")
    total_pnl     = sum(a["realised_pnl"] for a in all_actions if a["action"]=="SELL")

    # Cash check — after all sells, is cash at minimum?
    target_cash    = final_total * min_cash_pct
    cash_shortfall = max(0, target_cash - sim_cash)
    deployable     = max(0, sim_cash - target_cash)

    # Final weight check — flag any remaining violations
    remaining_violations = []
    for p in positions:
        if p["current_value"] <= 0:
            continue
        wt     = p["current_value"] / final_total if final_total > 0 else 0
        max_wt = max_new if p["is_priority"] else max_existing
        if wt > max_wt + tolerance:
            remaining_violations.append({
                "ticker": p["ticker"],
                "weight": round(wt*100,1),
                "max":    round(max_wt*100,1),
            })

    # Sector suggestions for redeployment
    target_alloc = strategy.get("target_allocation", {})
    sector_map   = data.get("sector_map", {})
    suggestions  = []
    if deployable > 500:
        for sector, targets in target_alloc.items():
            current_pct = sector_map.get(sector, 0)
            min_t = targets.get("min", 0)
            if current_pct < min_t and sector not in ("Other",):
                gap = min_t - current_pct
                suggestions.append({"sector": sector, "gap": gap,
                    "amount": round(deployable * gap / max(1, sum(
                        max(0, t.get("min",0) - sector_map.get(s,0))
                        for s,t in target_alloc.items()
                        if sector_map.get(s,0) < t.get("min",0)
                    )), 2)})
        suggestions.sort(key=lambda x: -x["gap"])

    is_balanced = len(all_actions) == 0

    # Human-readable convergence label
    convergence_labels = {
        "BALANCED":       "✅ Fully balanced — all positions within target",
        "CONVERGED":      "⚠️ Partially balanced — remaining violations too small to sell whole shares",
        "MAX_ITERATIONS": "🛑 Safety limit reached — manual review recommended",
        "ZERO_CAPITAL":   "🛑 Portfolio capital reached zero during simulation",
    }
    convergence_label = convergence_labels.get(convergence_reason, "Unknown")

    result = {
        "actions":              all_actions,
        "iterations":           iteration,
        "convergence_reason":   convergence_reason,
        "convergence_label":    convergence_label,
        "total_proceeds":       round(total_proceeds, 2),
        "total_realised_pnl":   round(total_pnl, 2),
        "cash_shortfall":       round(cash_shortfall, 2),
        "deployable":           round(deployable, 2),
        "post_trade_capital":   round(final_total, 2),
        "post_trade_cash":      round(sim_cash, 2),
        "remaining_violations": remaining_violations,
        "sector_suggestions":   suggestions[:4],
        "is_balanced":          is_balanced,
        "summary":              build_rebal_summary(all_actions, total_proceeds, deployable, total_pnl, final_total),
    }

    sells = [a for a in all_actions if a["action"]=="SELL"]
    print(f"{GREEN}✓ Rebalancing: {len(sells)} trades over {iteration} iteration(s) — {convergence_reason}")
    print(f"  Proceeds: ${total_proceeds:,.0f} | P&L: ${total_pnl:+,.0f} | Post-trade capital: ${final_total:,.0f}{RESET}")
    if remaining_violations:
        for v in remaining_violations:
            print(f"  {YELLOW}⚠ {v['ticker']} still at {v['weight']}% (max {v['max']}%){RESET}")
    return result

def build_rebal_summary(actions, proceeds, deploy, pnl=0, final_capital=0):
    sells = [a for a in actions if a["action"]=="SELL"]
    if not sells:
        return "Portfolio is within target allocation — no rebalancing needed"
    iters  = actions[-1].get("iteration", 1) if actions else 1
    pnl_str = f", P&L on trades: ${pnl:+,.0f}" if pnl != 0 else ""
    cap_str = f", post-trade capital: ${final_capital:,.0f}" if final_capital > 0 else ""
    return (f"{len(sells)} trade(s) over {iters} iteration(s), "
            f"${proceeds:,.0f} total proceeds"
            f"{pnl_str}"
            f"{cap_str}, "
            f"${deploy:,.0f} available to redeploy")


# ── MAIN ─────────────────────────────────────────────────────
# ── FEATURE 3 (L4): PORTFOLIO RISK ────────────────────────────
# Replaces the 3-year Monte Carlo, whose drift came from each holding's
# own expected return (largely the agent's optimism) and whose output —
# a median 3-year value — said little about what could go wrong next month.
VAR_Z_95      = 1.645
VAR_HORIZON_D = 21      # trading days ≈ 1 month


def _mean(x):
    return sum(x) / len(x) if x else 0.0


def _stdev(x):
    if len(x) < 2:
        return 0.0
    m = _mean(x)
    return (sum((v - m) ** 2 for v in x) / (len(x) - 1)) ** 0.5


def _beta(r, m):
    n = min(len(r), len(m))
    if n < 20:
        return None
    r, m = r[-n:], m[-n:]
    mr, mm = _mean(r), _mean(m)
    var_m = sum((b - mm) ** 2 for b in m) / (n - 1)
    if var_m <= 0:
        return None
    return sum((a - mr) * (b - mm) for a, b in zip(r, m)) / (n - 1) / var_m


def compute_risk_metrics(holdings, histories, spy_returns, total_capital):
    """Portfolio beta vs SPY, 1-month 95% VaR (parametric, from the
    portfolio's own daily returns) and the loss if every stop is hit."""
    rows, weights = [], {}
    for h in holdings:
        t = h.get("ticker", "")
        val = safe_float(h.get("current_value"))
        if not t or val <= 0 or total_capital <= 0:
            continue
        w = val / total_capital
        r = histories.get(t) or []
        b = _beta(r, spy_returns) if spy_returns else None
        vol = _stdev(r) * 252 ** 0.5 * 100 if len(r) > 20 else None
        price = safe_float(h.get("price"))
        stop  = safe_float(h.get("stop") or h.get("stop_loss"))
        qty   = safe_float(h.get("quantity"))
        to_stop = round((price - stop) * qty, 2) if price > 0 and 0 < stop < price and qty > 0 else None
        rows.append({"ticker": t, "weight_pct": round(w * 100, 2),
                     "beta": round(b, 2) if b is not None else None,
                     "vol_annual_pct": round(vol, 1) if vol else None,
                     "loss_to_stop_usd": to_stop})
        if len(r) > 20:
            weights[t] = w

    if not weights:
        return {"error": "Not enough price history to measure portfolio risk"}

    # Daily portfolio returns over the common window (cash returns 0).
    n = min(len(histories[t]) for t in weights)
    port = [sum(weights[t] * histories[t][-n:][i] for t in weights) for i in range(n)]
    sd = _stdev(port)
    var_pct = VAR_Z_95 * sd * VAR_HORIZON_D ** 0.5 * 100
    betas = [(r["weight_pct"] / 100, r["beta"]) for r in rows if r["beta"] is not None]
    to_stops = [r["loss_to_stop_usd"] for r in rows if r["loss_to_stop_usd"] is not None]
    out = {
        "portfolio_beta":     round(sum(w * b for w, b in betas), 2) if betas else None,
        "var_1m_95_pct":      round(var_pct, 2),
        "var_1m_95_usd":      round(var_pct / 100 * total_capital, 2),
        "vol_annual_pct":     round(sd * 252 ** 0.5 * 100, 1),
        "spy_vol_annual_pct": round(_stdev(spy_returns) * 252 ** 0.5 * 100, 1) if len(spy_returns or []) > 20 else None,
        "worst_day_pct":      round(min(port) * 100, 2) if port else None,
        "loss_if_all_stops_usd": round(sum(to_stops), 2) if to_stops else None,
        "stops_known":        f"{len(to_stops)}/{len(rows)}",
        "lookback_days":      n,
        "holdings":           sorted(rows, key=lambda r: -r["weight_pct"]),
        "method": (f"Beta: {n}-day regression of daily returns on SPY, weighted by position (cash = 0). "
                   f"VaR: 1.645 x daily volatility x sqrt(21) — a normal month should lose less than this "
                   f"19 times in 20. Real losses cluster, so treat it as a floor, not a ceiling."),
    }
    out["summary"] = (f"Beta {out['portfolio_beta']} · 1-month 95% VaR ${out['var_1m_95_usd']:,.0f} "
                      f"({out['var_1m_95_pct']:.1f}% of capital)")
    return out


def run():
    print(f"\n{BOLD}{BLUE}{'='*55}")
    print(f"  PORTFOLIO ANALYSIS ENGINE")
    print(f"  {datetime.now().strftime('%A %d %B %Y  %H:%M')}")
    print(f"{'='*55}{RESET}\n")

    # Load latest analysis
    latest_path = os.path.join(BASE_DIR, "logs", "latest.json")
    if not os.path.exists(latest_path):
        print(f"{RED}✗ No latest.json found. Run agent first.{RESET}")
        return None

    with open(latest_path) as f:
        data = json.load(f)

    holdings = data.get("holdings", [])
    if not holdings:
        print(f"{YELLOW}⚠ No holdings data found{RESET}")
        return None

    # Load market data for position values
    market_path = os.path.join(BASE_DIR, "data", "market_data.json")
    if os.path.exists(market_path):
        with open(market_path) as f:
            market = json.load(f)
        # Merge current values into holdings
        stock_map = {s.get("ticker"):s for s in market.get("stocks",[])}
        for h in holdings:
            t = h.get("ticker","")
            if t in stock_map:
                h["current_value"] = stock_map[t].get("current_value", 0)
                h["price"]         = stock_map[t].get("price", 0)

    # Load strategy
    strategy_path = os.path.join(BASE_DIR, "strategy.json")
    strategy = {}
    if os.path.exists(strategy_path):
        with open(strategy_path) as f:
            strategy = json.load(f)

    tickers = [h.get("ticker","") for h in holdings if h.get("ticker","")]

    # Feature 1: Correlation
    histories    = fetch_price_history(tickers + ["SPY"], days=365)
    spy_returns  = histories.pop("SPY", [])
    # Correlation keeps its original 90-day window.
    correlation  = run_correlation_analysis(holdings, {t: r[-90:] for t, r in histories.items()})
    adjust_diversification(correlation, holdings, data, strategy)

    # Feature 2: Rebalancing
    # Attach sector map from latest to data dict
    data["sector_map"] = data.get("sector_map", {})
    rebalancing = run_rebalancing(holdings, data, strategy)

    # Feature 3 (L4): portfolio risk — replaces the Monte Carlo
    total_capital = safe_float(data.get("total_capital")) or sum(safe_float(h.get("current_value")) for h in holdings)
    risk = compute_risk_metrics(holdings, histories, spy_returns, total_capital)
    if not risk.get("error"):
        print(f"{GREEN}✓ Risk: {risk['summary']}{RESET}")

    # Combine into one analysis object
    analysis = {
        "generated_at":  datetime.now().isoformat(),
        "correlation":   correlation,
        "rebalancing":   rebalancing,
        "risk":          risk,
    }

    # Save
    out_path = os.path.join(BASE_DIR, "logs", "portfolio_analysis.json")
    with open(out_path, "w") as f:
        json.dump(analysis, f, indent=2, default=str)

    # Also merge into latest.json for dashboard
    data["portfolio_analysis"] = analysis
    with open(latest_path + ".tmp", "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(latest_path + ".tmp", latest_path)  # atomic

    print(f"\n{GREEN}✓ Portfolio analysis saved{RESET}")
    print(f"{GREEN}✓ Merged into latest.json{RESET}\n")

    # Print summary
    print(f"{BOLD}{'─'*50}")
    print(f"  SUMMARY")
    print(f"{'─'*50}{RESET}")
    print(f"  Diversification: {correlation['diversification_label']} ({correlation['diversification_score']}/100)")
    print(f"  Rebalancing:     {rebalancing['summary']}")
    print(f"  Risk:            {risk.get('summary') or risk.get('error')}")
    print()

    return analysis

if __name__ == "__main__":
    run()
