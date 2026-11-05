# ============================================================
# MARKET AGENT V4.5 — Capital Reallocation Engine
# reallocation_engine.py
#
# For each qualified pick (passed all 6 gates), evaluates:
#   Layer 1 — Can available cash fund a meaningful position?
#             (meaningful = ≥ 3% of total capital)
#   Layer 2 — Even with cash available, is there a holding
#             that should be swapped? (new pick ≥ 85% better)
#   Layer 3 — If cash insufficient or position too small,
#             find the best divestment candidate to generate cash
#   Layer 4 — No viable path found — flag for future monitoring
#
# Reads:  analysis["agent_picks"]   — qualified picks
#         analysis["holdings"]      — current holdings
#         data["stocks"]            — market data incl. watchlist fields
#         data["total_capital"]     — total portfolio value
#         data["cash"]              — available cash
#
# Writes: analysis["reallocation"]  — list of recommendations
#         logs/reallocation.json    — persisted output
# ============================================================

import json
import os
from datetime import datetime
from common import GREEN, RED, YELLOW, BLUE, BOLD, RESET, safe_float, save_json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


MEANINGFUL_POSITION_PCT    = 3.0   # minimum % of total capital for a position to matter
HIGH_CONVICTION_THRESHOLD  = 80.0  # attractiveness score required to trigger floor exception
SWAP_IMPROVEMENT_THRESHOLD = 85.0  # new pick must score this % better to justify swap


REGIME_SIZE_MULTIPLIER = {"normal": 1.0, "selective": 0.75, "defensive": 0.5, "halt": 0.0}

# L4 risk-based sizing: a position is sized so that being stopped out loses
# risk_per_trade_pct of capital, scaled by conviction. The stop used for
# sizing is never tighter than ATR_STOP_MULTIPLE x the stock's daily range.
CONVICTION_FACTOR = {"High": 1.0, "Medium": 0.75, "Low": 0.5}
ATR_STOP_MULTIPLE = 2.0
DEFAULT_STOP_PCT  = 8.0


def risk_based_cap(pick, total_capital, risk_per_trade_pct):
    """(max position value, explanation dict) for one pick."""
    price = safe_float(pick.get("price") or pick.get("current_price"))
    stop  = safe_float(pick.get("stop_price"))
    stop_pct = (price - stop) / price * 100 if price > 0 and 0 < stop < price else None
    atr_pct  = safe_float(pick.get("atr_pct"))
    atr_floor = ATR_STOP_MULTIPLE * atr_pct if atr_pct > 0 else 0.0
    sizing_stop = max(stop_pct or 0.0, atr_floor) or DEFAULT_STOP_PCT
    conf = pick.get("confidence") or "Medium"
    conviction = CONVICTION_FACTOR.get(conf, 0.75)
    bear = (pick.get("bear_case") or {}).get("strength") if isinstance(pick.get("bear_case"), dict) else None
    if str(bear).lower() == "strong":
        conviction *= 0.75
    budget = total_capital * risk_per_trade_pct / 100
    cap = budget / (sizing_stop / 100) * conviction
    return cap, {
        "risk_budget_usd":   round(budget, 2),
        "sizing_stop_pct":   round(sizing_stop, 2),
        "stop_pct":          round(stop_pct, 2) if stop_pct else None,
        "atr_pct":           atr_pct or None,
        "stop_widened_by_atr": bool(atr_floor and (stop_pct or 0) < atr_floor),
        "conviction_factor": round(conviction, 3),
        "risk_cap_value":    round(cap, 2),
        "risk_cap_pct":      round(cap / total_capital * 100, 2) if total_capital > 0 else None,
    }


def load_sizing_policy():
    """One source for the cash floor, regime scaling and minimum order.

    strategy.json says "Cash below 15% — no new buys under any
    circumstances", but this engine used its own hardcoded 10% floor
    (7% for "high conviction"), so it recommended buys the strategy
    forbids. Now: floor = strategy min_cash_pct, raised by the regime
    detector's cash_floor_override (25% in a bear trend, 40% in a
    crisis); sizes are scaled by the regime's pick_aggressiveness; and
    orders below min_order_usd are dropped (a $25 commission on a $134
    order is 19%). The old high-conviction exception only applies if
    strategy.json sets position_sizing.high_conviction_floor_pct."""
    try:
        with open(os.path.join(BASE_DIR, "strategy.json")) as f:
            strategy = json.load(f)
    except Exception:
        strategy = {}
    try:
        with open(os.path.join(BASE_DIR, "data", "market_regime.json")) as f:
            regime = json.load(f)
    except Exception:
        regime = {}
    ps = strategy.get("position_sizing", {})
    floor = safe_float(ps.get("min_cash_pct"), 15.0)
    adj = regime.get("adjustments") or {}
    override = adj.get("cash_floor_override")
    if isinstance(override, (int, float)) and override > floor:
        floor = float(override)
    aggr = adj.get("pick_aggressiveness", "normal")
    return {
        "floor_pct":       floor,
        "exception_pct":   safe_float(ps.get("high_conviction_floor_pct"), 0) or None,
        "min_order_usd":   safe_float(ps.get("min_order_usd"), 2000.0),
        "size_multiplier": REGIME_SIZE_MULTIPLIER.get(aggr, 1.0),
        "risk_per_trade_pct": safe_float(ps.get("risk_per_trade_pct"), 1.0),
        "regime":          regime.get("regime"),
        "aggressiveness":  aggr,
    }


def scale_detail(detail, mult):
    """Apply the regime multiplier to every size field of a recommendation."""
    if mult >= 1.0:
        return detail
    for sh, val, pct in (("shares", "position_value", "position_pct"),
                         ("suggested_shares", "suggested_position", "suggested_pct"),
                         ("correlation_adjusted_shares", "correlation_adjusted_position", "correlation_adjusted_pct")):
        if detail.get(sh) and detail.get(val):
            new_sh = int(detail[sh] * mult)
            ratio = new_sh / detail[sh]
            detail[sh] = new_sh
            detail[val] = round(detail[val] * ratio, 2)
            if detail.get(pct):
                detail[pct] = round(detail[pct] * ratio, 2)
    return detail


def load_max_new_position_pct():
    """
    Reads max_single_stock_pct from strategy.json (10% by default, per the
    file's own note: "10% max for new positions, 20% max for existing proven
    winners"). Every candidate compute_suggested_shares() sizes here is a
    NEW position (never an add-to-existing-holding), so this is always the
    correct cap to apply — not max_existing_holding_pct.

    This was previously MISSING entirely: sizing only checked the cash
    floor (don't deploy below 10%/7% remaining cash) with no cap on how
    large the resulting position could get. With a large excess-cash
    pool, that let a single new-position suggestion balloon to 40%+ of
    total capital — a real, severe violation of the strategy's own
    configured concentration limit, not just a display quirk.
    """
    try:
        with open(os.path.join(BASE_DIR, "strategy.json")) as f:
            strategy = json.load(f)
        return safe_float(strategy.get("position_sizing", {}).get("max_single_stock_pct"), 10.0)
    except FileNotFoundError:
        return 10.0


# MUST come after safe_float() is defined above — this runs immediately at
# import time (top-level statement), and load_max_new_position_pct() calls
# safe_float() internally. Having this above safe_float's definition caused
# a NameError on every single import, silently killing the reallocation
# engine before it could ever run for any pick, on every run since the
# position-cap fix was first introduced.
MAX_NEW_POSITION_PCT = load_max_new_position_pct()


# ── RETENTION SCORE ───────────────────────────────────────────
def compute_retention_score(holding, stock_data_map, total_capital):
    """
    Score how compelling it is to KEEP this holding (0-100).
    Higher = stronger case to hold. Lower = better divestment candidate.

    Components:
      30% — remaining upside to target
      25% — momentum (RSI direction, score)
      20% — stop proximity (distance from stop loss)
      15% — thesis progress (gain vs time)
      10% — sector uniqueness
    """
    ticker     = holding.get("ticker", "").upper()
    score_data = stock_data_map.get(ticker, {})

    buy_price  = safe_float(holding.get("buy_price") or holding.get("entryPrice") or
                            score_data.get("buy_price"))
    target     = safe_float(holding.get("target") or score_data.get("target"))
    stop       = safe_float(holding.get("stop") or holding.get("stop_loss") or
                            score_data.get("stop_loss"))
    current    = safe_float(holding.get("current_price") or score_data.get("price"))
    rsi        = safe_float(holding.get("rsi") or score_data.get("rsi"), default=50)
    agg_score  = safe_float(holding.get("aggregate_score") or score_data.get("aggregate_score"), default=50)

    components = {}

    # ── 1. Remaining upside to target (30%) ──────────────────
    if target > 0 and buy_price > 0 and current > 0:
        total_upside    = target - buy_price
        remaining       = target - current
        pct_remaining   = (remaining / total_upside) * 100 if total_upside > 0 else 0
        pct_remaining   = max(0, min(100, pct_remaining))
        # High remaining upside = strong reason to hold
        upside_score = pct_remaining
    else:
        upside_score = 50  # neutral if no target set
    components["remaining_upside"] = round(upside_score, 1)

    # ── 2. Momentum (25%) ─────────────────────────────────────
    # RSI: 40-60 is neutral, above 60 = strong momentum, below 40 = weakening
    if rsi >= 60:
        momentum_score = 80
    elif rsi >= 50:
        momentum_score = 60
    elif rsi >= 40:
        momentum_score = 40
    else:
        momentum_score = 20
    # Blend with aggregate score
    momentum_score = (momentum_score * 0.5) + (min(agg_score, 100) * 0.5)
    components["momentum"] = round(momentum_score, 1)

    # ── 3. Stop proximity (20%) ───────────────────────────────
    # How far is current price from stop? More cushion = stronger hold
    if stop > 0 and current > 0:
        distance_to_stop_pct = ((current - stop) / current) * 100
        # >15% cushion = very safe, <5% = dangerous
        if distance_to_stop_pct >= 15:
            stop_score = 90
        elif distance_to_stop_pct >= 10:
            stop_score = 70
        elif distance_to_stop_pct >= 5:
            stop_score = 45
        else:
            stop_score = 15  # near stop = weak hold
    else:
        stop_score = 50
    components["stop_proximity"] = round(stop_score, 1)

    # ── 4. Thesis progress (15%) ──────────────────────────────
    # Are we gaining toward target or stuck?
    if buy_price > 0 and target > 0 and current > 0:
        total_move_needed = target - buy_price
        move_achieved     = current - buy_price
        progress_pct      = (move_achieved / total_move_needed * 100) if total_move_needed > 0 else 0
        progress_pct      = max(-50, min(150, progress_pct))

        if progress_pct >= 80:
            # Very close to target — limited upside remaining, weak hold
            thesis_score = 30
        elif progress_pct >= 50:
            thesis_score = 60
        elif progress_pct >= 20:
            thesis_score = 75
        elif progress_pct >= 0:
            thesis_score = 55  # early stage, thesis playing out
        else:
            thesis_score = 25  # losing ground
    else:
        thesis_score = 50
    components["thesis_progress"] = round(thesis_score, 1)

    # ── 5. Sector uniqueness (10%) ────────────────────────────
    # Placeholder — set to neutral; sector comparison done at engine level
    components["sector_uniqueness"] = 50

    # ── Weighted total ────────────────────────────────────────
    weighted = (
        components["remaining_upside"] * 0.30 +
        components["momentum"]         * 0.25 +
        components["stop_proximity"]   * 0.20 +
        components["thesis_progress"]  * 0.15 +
        components["sector_uniqueness"]* 0.10
    )
    return round(weighted, 1), components


# ── DIVESTMENT SCORE ──────────────────────────────────────────
def compute_divestment_score(holding, stock_data_map, total_capital, new_pick_sector=None):
    """
    Score how attractive this holding is to SELL (0-100).
    Higher = better divestment candidate.
    Inverse of retention score with extra factors.
    """
    retention, components = compute_retention_score(holding, stock_data_map, total_capital)

    ticker    = holding.get("ticker", "").upper()
    buy_price = safe_float(holding.get("buy_price") or holding.get("entryPrice") or
                           stock_data_map.get(ticker, {}).get("buy_price"))
    current   = safe_float(holding.get("current_price") or
                           stock_data_map.get(ticker, {}).get("price"))
    pnl_pct   = safe_float(holding.get("pnl_pct"))
    target    = safe_float(holding.get("target") or
                           stock_data_map.get(ticker, {}).get("target"))

    # Base: inverse of retention
    divestment = 100 - retention

    # Bonus: position is already profitable — selling locks in gain cleanly
    if pnl_pct > 10:
        divestment += 10
    elif pnl_pct > 5:
        divestment += 5

    # Bonus: sector overlap with new pick — concentration risk if we add more
    holding_sector = holding.get("sector", "")
    if new_pick_sector and holding_sector and new_pick_sector.lower() == holding_sector.lower():
        divestment += 8

    # Bonus: very close to target (>85% of the way) — most gain already captured
    if buy_price > 0 and target > 0 and current > 0:
        total_move = target - buy_price
        achieved   = current - buy_price
        progress   = (achieved / total_move * 100) if total_move > 0 else 0
        if progress >= 85:
            divestment += 12

    return round(min(divestment, 100), 1), components


# ── PICK ATTRACTIVENESS SCORE ─────────────────────────────────
def compute_pick_attractiveness(pick):
    """
    Convert pick's existing aggregate_score and gate results into
    a 0-100 attractiveness score for comparison with retention score.
    """
    agg     = safe_float(pick.get("aggregate_score"), default=50)
    conf    = pick.get("confidence", "")
    risk    = pick.get("risk_level", "")
    signal  = pick.get("signal", "")

    score = agg

    # Confidence boost
    if conf == "High":
        score += 10
    elif conf == "Medium":
        score += 0
    else:
        score -= 10

    # Risk adjustment
    if risk == "Low":
        score += 5
    elif risk == "High":
        score -= 10

    # Signal boost
    if signal == "STRONG BUY":
        score += 8
    elif signal == "BUY":
        score += 3

    return round(min(score, 100), 1)


# ── LIMIT PRICE + SUGGESTED SHARES ───────────────────────────
def compute_limit_price(pick):
    """
    Derive a limit price from the pick's suggested_entry range (midpoint),
    falling back to 0.25% below current price if no range is available.
    Returns (limit_price, limit_note)
    """
    price = safe_float(pick.get("price") or pick.get("current_price"))
    entry = pick.get("suggested_entry", "") or ""

    # Try to parse a range like "$124-$128" or "$124–$128" or "124 - 128"
    import re
    nums = re.findall(r"\d+(?:\.\d+)?", entry.replace(",", ""))
    if len(nums) >= 2:
        lo   = float(nums[0])
        hi   = float(nums[1])
        mid  = round((lo + hi) / 2, 2)
        note = f"Midpoint of entry range ${lo:.2f}–${hi:.2f}"
        return mid, note
    elif len(nums) == 1:
        # Single price given in suggested_entry — use it directly
        pt   = float(nums[0])
        note = f"Entry price from range: ${pt:.2f}"
        return pt, note
    else:
        # Fallback: 0.25% below current price
        limit = round(price * 0.9975, 2)
        note  = f"0.25% below current price (${price:.2f})"
        return limit, note


def compute_suggested_shares(limit_price, available_cash, total_capital, cash_floor_pct, risk_cap=None):
    """
    Calculate share count based on limit price (not current price),
    so the dollar cost reflects what you'd actually pay.

    Sizing is constrained by BOTH:
      1. The cash floor (don't deploy below cash_floor_pct remaining), and
      2. MAX_NEW_POSITION_PCT of total capital — this second check was
         previously missing, which let a single suggested position size
         to 40%+ of the portfolio whenever a lot of cash happened to be
         sitting idle. Cash availability was never sufficient sizing logic
         on its own.
    """
    if limit_price <= 0 or total_capital <= 0:
        return 0, 0
    cash_floor_value   = total_capital * (cash_floor_pct / 100)
    deployable_cash    = max(0, available_cash - cash_floor_value)

    max_position_value = total_capital * (MAX_NEW_POSITION_PCT / 100)
    if risk_cap is not None:
        max_position_value = min(max_position_value, risk_cap)
    position_cap_cash  = min(deployable_cash, max_position_value)

    shares             = int(position_cap_cash / limit_price)
    position_value     = round(shares * limit_price, 2)
    return shares, position_value


def apply_correlation_adjustment(shares, position_value, pct, limit_price, multiplier):
    """
    V4.5 — correlation_engine.py runs earlier in the pipeline and attaches
    pick["correlation"]["size_multiplier"] when a pick's daily-return
    correlation to an existing holding is high enough to matter. This
    scales the SUGGESTED (limit-order) sizing only — informational, not
    a floor/gate. The underlying can_buy/shares/position math elsewhere
    is untouched, so this never blocks a trade.
    """
    if multiplier >= 1.0 or limit_price <= 0 or shares <= 0:
        return shares, position_value, pct
    adj_shares   = int(shares * multiplier)
    adj_value    = round(adj_shares * limit_price, 2)
    adj_pct      = round(pct * (adj_value / position_value), 2) if position_value > 0 else pct
    return adj_shares, adj_value, adj_pct


# ── LAYER 1: MEANINGFUL POSITION CHECK ───────────────────────
def check_meaningful_position(pick_price, available_cash, total_capital, cash_floor_pct, risk_cap=None):
    """
    Can available cash fund a meaningful position (≥ 3% of total capital)
    while keeping cash above the floor AND respecting MAX_NEW_POSITION_PCT?
    Returns: (can_buy, shares, position_value, position_pct, cash_after, reason)
    """
    if pick_price <= 0 or total_capital <= 0:
        return False, 0, 0, 0, available_cash, "Invalid price or capital data"

    cash_floor_value   = total_capital * (cash_floor_pct / 100)
    deployable_cash    = max(0, available_cash - cash_floor_value)
    min_position_value = total_capital * (MEANINGFUL_POSITION_PCT / 100)

    # Same cap as compute_suggested_shares() — this function computes the
    # ACTUAL can_buy/shares used for the real recommendation, independently
    # of the "suggested" sizing display. It had the identical missing-cap
    # bug: cash availability alone (uncapped) was determining position size.
    max_position_value = total_capital * (MAX_NEW_POSITION_PCT / 100)
    risk_binds = risk_cap is not None and risk_cap < max_position_value
    if risk_binds:
        # L4: a small position on a volatile stock is the point of risk
        # sizing, so the 3% "meaningful" minimum doesn't apply when the risk
        # budget is what limits the size (min_order_usd still does).
        max_position_value = risk_cap
        # 0.9: whole-share rounding always lands a little under the cap
        min_position_value = min(min_position_value, risk_cap * 0.9)
    deployable_cash     = min(deployable_cash, max_position_value)

    if deployable_cash <= 0:
        return False, 0, 0, 0, available_cash, f"Cash at or below {cash_floor_pct}% floor — no deployable capital"

    shares         = int(deployable_cash / pick_price)
    position_value = shares * pick_price
    position_pct   = (position_value / total_capital) * 100
    cash_after     = available_cash - position_value

    if shares <= 0:
        return False, 0, 0, 0, available_cash, f"Cannot afford even 1 share at ${pick_price:.2f}"

    if position_value < min_position_value:
        return False, shares, position_value, round(position_pct, 2), cash_after, (
            f"Position would be only {position_pct:.1f}% of portfolio "
            f"(${position_value:,.0f} / {shares} shares) — below {MEANINGFUL_POSITION_PCT}% meaningful threshold"
        )

    return True, shares, position_value, round(position_pct, 2), cash_after, "Meaningful position achievable"


# ── LAYER 2: SWAP EVALUATION ──────────────────────────────────
def find_swap_candidates(pick, holdings, stock_data_map, total_capital, attractiveness_score):
    """
    Even if cash is available, check if any holding should be swapped.
    Returns list of swap candidates where new pick is ≥ 85% better.
    """
    candidates = []

    for h in holdings:
        ticker    = h.get("ticker", "").upper()
        retention, components = compute_retention_score(h, stock_data_map, total_capital)

        if retention <= 0:
            continue

        improvement = ((attractiveness_score - retention) / retention) * 100

        if improvement >= SWAP_IMPROVEMENT_THRESHOLD:
            current    = safe_float(h.get("current_price") or stock_data_map.get(ticker, {}).get("price"))
            quantity   = safe_float(h.get("quantity") or stock_data_map.get(ticker, {}).get("quantity"))
            sell_value = current * quantity if current > 0 and quantity > 0 else 0
            pnl_pct    = safe_float(h.get("pnl_pct"))

            candidates.append({
                "ticker":           ticker,
                "name":             h.get("name", ticker),
                "retention_score":  retention,
                "attractiveness":   attractiveness_score,
                "improvement_pct":  round(improvement, 1),
                "sell_value":       round(sell_value, 2),
                "pnl_pct":          pnl_pct,
                "components":       components,
                "reason":           build_swap_reason(h, components, improvement, pnl_pct),
            })

    # Sort by improvement descending
    candidates.sort(key=lambda x: x["improvement_pct"], reverse=True)
    return candidates


def build_swap_reason(holding, components, improvement, pnl_pct):
    reasons = []
    if components.get("remaining_upside", 100) < 40:
        reasons.append("limited upside remaining to target")
    if components.get("momentum", 100) < 45:
        reasons.append("weakening momentum")
    if components.get("stop_proximity", 100) < 40:
        reasons.append("approaching stop loss")
    if components.get("thesis_progress", 100) < 35:
        reasons.append("thesis making slow progress")
    if pnl_pct > 5:
        reasons.append(f"locking in +{pnl_pct:.1f}% gain")
    if not reasons:
        reasons.append("significantly outscored by new opportunity")
    return "; ".join(reasons)


# ── LAYER 3: DIVESTMENT TO GENERATE CASH ─────────────────────
def find_divestment_candidate(pick, holdings, stock_data_map, total_capital,
                               available_cash, cash_floor_pct):
    """
    Find the best holding to sell to generate enough cash for a meaningful
    position in the new pick. Returns best candidate or None.
    """
    pick_price       = safe_float(pick.get("price") or pick.get("current_price"))
    cash_floor_value = total_capital * (cash_floor_pct / 100)
    pick_sector      = pick.get("sector", "")
    min_position_val = total_capital * (MEANINGFUL_POSITION_PCT / 100)

    scored = []
    for h in holdings:
        ticker   = h.get("ticker", "").upper()
        current  = safe_float(h.get("current_price") or stock_data_map.get(ticker, {}).get("price"))
        quantity = safe_float(h.get("quantity") or stock_data_map.get(ticker, {}).get("quantity"))

        if current <= 0 or quantity <= 0:
            continue

        sell_value   = current * quantity
        total_cash   = available_cash + sell_value
        deployable   = max(0, total_cash - cash_floor_value)
        shares_buy   = int(deployable / pick_price) if pick_price > 0 else 0
        pos_value    = shares_buy * pick_price
        pos_pct      = (pos_value / total_capital * 100) if total_capital > 0 else 0

        if pos_value < min_position_val or shares_buy <= 0:
            continue  # selling this still doesn't create a meaningful position

        div_score, components = compute_divestment_score(
            h, stock_data_map, total_capital, pick_sector)

        pnl_pct  = safe_float(h.get("pnl_pct"))
        buy_price= safe_float(h.get("buy_price") or h.get("entryPrice") or
                              stock_data_map.get(ticker, {}).get("buy_price"))
        gain_usd = (current - buy_price) * quantity if buy_price > 0 else 0

        scored.append({
            "ticker":           ticker,
            "name":             h.get("name", ticker),
            "divestment_score": div_score,
            "sell_value":       round(sell_value, 2),
            "gain_usd":         round(gain_usd, 2),
            "pnl_pct":          pnl_pct,
            "shares_to_buy":    shares_buy,
            "new_position_value": round(pos_value, 2),
            "new_position_pct": round(pos_pct, 2),
            "cash_after":       round(total_cash - pos_value, 2),
            "components":       components,
        })

    if not scored:
        return None

    scored.sort(key=lambda x: x["divestment_score"], reverse=True)
    return scored[0]


# ── FORMAT TELEGRAM MESSAGE ───────────────────────────────────
def format_telegram(rec):
    pick    = rec["pick"]
    layer   = rec["layer"]
    ticker  = pick.get("ticker", "")
    price   = safe_float(pick.get("price") or pick.get("current_price"))
    target  = safe_float(pick.get("target"))
    signal  = pick.get("signal", "BUY")
    reason  = pick.get("reason", "")[:120]

    header = f"{'🟢' if signal == 'STRONG BUY' else '🔵'} <b>{signal} — {ticker}</b>\n"
    header += f"📌 Price: ${price:.2f}  |  Target: ${target:.2f}\n"
    header += f"💬 <i>{reason}</i>\n\n"

    if layer == 1:
        d = rec["detail"]
        return (
            header +
            f"💰 <b>BUY with available cash</b>\n"
            f"Shares: {d['shares']}  |  Position: ${d['position_value']:,.0f} ({d['position_pct']:.1f}%)\n"
            f"Cash after: ${d['cash_after']:,.0f}\n"
            f"🎯 Limit order: <b>{d['suggested_shares']} shares @ ${d['limit_price']:.2f}</b> "
            f"= ${d['suggested_position']:,.0f} ({d['suggested_pct']:.1f}%)\n"
            f"<i>{d['limit_note']}</i>"
        )

    elif layer == "1+2":
        d  = rec["detail"]
        sw = rec["swap"]
        return (
            header +
            f"💰 <b>BUY with available cash</b>\n"
            f"Shares: {d['shares']}  |  Position: ${d['position_value']:,.0f} ({d['position_pct']:.1f}%)\n"
            f"🎯 Limit order: <b>{d['suggested_shares']} shares @ ${d['limit_price']:.2f}</b> "
            f"= ${d['suggested_position']:,.0f} ({d['suggested_pct']:.1f}%)\n"
            f"<i>{d['limit_note']}</i>\n\n"
            f"⚡ <b>Also consider swapping {sw['ticker']}</b>\n"
            f"{ticker} scores {sw['improvement_pct']:.0f}% stronger — {sw['reason']}\n"
            f"Swapping would release ${sw['sell_value']:,.0f} for a larger position"
        )

    elif layer == 3:
        dv = rec["divest"]
        d  = rec.get("detail", {})
        pnl_str  = f"+{dv['pnl_pct']:.1f}%" if dv['pnl_pct'] >= 0 else f"{dv['pnl_pct']:.1f}%"
        gain_str = f"+${dv['gain_usd']:,.0f}" if dv['gain_usd'] >= 0 else f"-${abs(dv['gain_usd']):,.0f}"
        return (
            header +
            f"🔄 <b>SWAP RECOMMENDED</b>\n"
            f"Divest <b>{dv['ticker']}</b> ({pnl_str} | {gain_str}) → Fund {ticker}\n\n"
            f"New position: {dv['shares_to_buy']} shares = ${dv['new_position_value']:,.0f} "
            f"({dv['new_position_pct']:.1f}% of portfolio)\n"
            f"Cash after: ${dv['cash_after']:,.0f}\n"
            f"🎯 Limit order: <b>{d.get('suggested_shares', dv['shares_to_buy'])} shares @ "
            f"${d.get('limit_price', safe_float(rec['pick'].get('current_price'))):.2f}</b>\n"
            f"<i>{d.get('limit_note', '')}</i>"
        )

    elif layer == "1+3":
        dv = rec["divest"]
        d  = rec["detail"]
        return (
            header +
            f"⚠️ <b>Cash available but position too small</b> ({d['position_pct']:.1f}% — below 3% threshold)\n\n"
            f"🔄 <b>SWAP RECOMMENDED instead</b>\n"
            f"Divest <b>{dv['ticker']}</b> → Fund meaningful {ticker} position\n"
            f"New position: {dv['shares_to_buy']} shares = ${dv['new_position_value']:,.0f} "
            f"({dv['new_position_pct']:.1f}%)"
        )

    elif layer == 4:
        return (
            header +
            f"📋 <b>Opportunity noted — insufficient capital</b>\n"
            f"No suitable divestment candidate at this time.\n"
            f"Will reassess when capital becomes available."
        )

    return header


# ── MAIN ENGINE ───────────────────────────────────────────────
def run(analysis, data):
    """
    Main entry point. Called from agent_brain.py after picks are ranked.
    Mutates analysis in place — adds analysis["reallocation"].
    """
    print(f"\n{BOLD}{BLUE}{'='*55}")
    print(f"  MARKET AGENT V4.5 — Capital Reallocation Engine")
    print(f"{'='*55}{RESET}\n")

    picks         = analysis.get("agent_picks", [])
    holdings      = analysis.get("holdings", [])
    total_capital = safe_float(data.get("total_capital"))
    available_cash= safe_float(data.get("cash"))

    if not picks:
        print(f"{YELLOW}⚠ No qualified picks to evaluate for reallocation{RESET}")
        analysis["reallocation"] = []
        return analysis

    if total_capital <= 0:
        print(f"{YELLOW}⚠ Total capital not available — skipping reallocation{RESET}")
        analysis["reallocation"] = []
        return analysis

    cash_pct = (available_cash / total_capital * 100) if total_capital > 0 else 0
    policy     = load_sizing_policy()
    CASH_FLOOR = policy["floor_pct"]
    regime_txt = f" (regime {policy['regime']}, {policy['aggressiveness']})" if policy["regime"] else ""
    print(f"{BLUE}Cash floor {CASH_FLOOR:.0f}%{regime_txt} · size ×{policy['size_multiplier']:g} · "
          f"min order ${policy['min_order_usd']:,.0f}{RESET}")
    print(f"{BLUE}Total capital: ${total_capital:,.2f}  |  "
          f"Cash: ${available_cash:,.2f} ({cash_pct:.1f}%){RESET}")
    print(f"{BLUE}Holdings: {len(holdings)}  |  Qualified picks: {len(picks)}{RESET}\n")

    # Build stock data map for fast lookup (market_data stocks list)
    stock_data_map = {}
    for s in data.get("stocks", []):
        t = str(s.get("ticker", "")).upper()
        if t:
            stock_data_map[t] = s

    # Also enrich holdings with buy_price from stock_data_map if missing
    for h in holdings:
        t = h.get("ticker", "").upper()
        if t in stock_data_map:
            if not h.get("buy_price"):
                h["buy_price"] = stock_data_map[t].get("buy_price")
            if not h.get("quantity"):
                h["quantity"] = stock_data_map[t].get("quantity")
            if not h.get("stop") and not h.get("stop_loss"):
                h["stop"] = stock_data_map[t].get("stop_loss")

    recommendations = []

    for pick in picks:
        ticker     = pick.get("ticker", "").upper()
        pick_price = safe_float(pick.get("price") or pick.get("current_price"))

        if pick_price <= 0:
            print(f"  {YELLOW}⚠ {ticker} — no price available, skipping reallocation{RESET}")
            continue

        print(f"  {BOLD}{ticker}{RESET}  @ ${pick_price:.2f}", end="  ")

        attractiveness = compute_pick_attractiveness(pick)
        print(f"attractiveness={attractiveness:.0f}", end="  ")

        # ── CORRELATION-AWARE SIZING ────────────────────
        corr_info       = pick.get("correlation") or {}
        corr_multiplier = corr_info.get("size_multiplier", 1.0)
        corr_note       = corr_info.get("note")

        # ── LIMIT PRICE + SIZING ──────────────────────────────
        risk_cap, risk_info = risk_based_cap(pick, total_capital, policy["risk_per_trade_pct"])
        pick["risk_sizing"] = risk_info
        limit_price, limit_note = compute_limit_price(pick)
        limit_shares, limit_pos_value = compute_suggested_shares(
            limit_price, available_cash, total_capital, CASH_FLOOR, risk_cap)
        limit_pct = round((limit_pos_value / total_capital) * 100, 2) if total_capital > 0 else 0
        corr_shares, corr_position, corr_pct = apply_correlation_adjustment(
            limit_shares, limit_pos_value, limit_pct, limit_price, corr_multiplier)

        # ── LAYER 1: Can cash fund a meaningful position? ──────
        # First try standard floor
        can_buy, shares, pos_value, pos_pct, cash_after, l1_reason = check_meaningful_position(
            pick_price, available_cash, total_capital, CASH_FLOOR, risk_cap)

        floor_exception_applied = False

        # If standard floor fails, check if high-conviction exception applies
        if not can_buy and policy["exception_pct"] and attractiveness >= HIGH_CONVICTION_THRESHOLD:
            can_buy_ex, shares_ex, pos_value_ex, pos_pct_ex, cash_after_ex, l1_reason_ex = \
                check_meaningful_position(pick_price, available_cash, total_capital, policy["exception_pct"], risk_cap)
            if can_buy_ex:
                can_buy              = True
                shares               = shares_ex
                pos_value            = pos_value_ex
                pos_pct              = pos_pct_ex
                cash_after           = cash_after_ex
                l1_reason            = l1_reason_ex
                floor_exception_applied = True
                print(f"{YELLOW}⚡ HIGH CONVICTION — floor exception applied ({CASH_FLOOR}% → {policy['exception_pct']}%){RESET}", end="  ")
                # Recalculate limit shares with exception floor
                limit_shares, limit_pos_value = compute_suggested_shares(
                    limit_price, available_cash, total_capital, policy["exception_pct"], risk_cap)
                limit_pct = round((limit_pos_value / total_capital) * 100, 2) if total_capital > 0 else 0
                corr_shares, corr_position, corr_pct = apply_correlation_adjustment(
                    limit_shares, limit_pos_value, limit_pct, limit_price, corr_multiplier)

        if can_buy:
            print(f"{GREEN}L1: BUY ({pos_pct:.1f}%){RESET}")
            rec = {
                "pick":   pick,
                "layer":  1,
                "detail": {
                    "shares":                 shares,
                    "position_value":         round(pos_value, 2),
                    "position_pct":           pos_pct,
                    "cash_after":             round(cash_after, 2),
                    "limit_price":            limit_price,
                    "limit_note":             limit_note,
                    "suggested_shares":       limit_shares,
                    "suggested_position":     limit_pos_value,
                    "suggested_pct":          limit_pct,
                    "correlation_adjusted_shares":   corr_shares,
                    "correlation_adjusted_position": corr_position,
                    "correlation_adjusted_pct":      corr_pct,
                    "correlation_note":              corr_note,
                    "risk_sizing":            risk_info,
                    "floor_exception":        floor_exception_applied,
                    "floor_exception_note":   (f"⚡ High conviction pick (score {attractiveness:.0f}) — "
                                               f"cash floor reduced from {CASH_FLOOR}% to {policy['exception_pct']}% "
                                               f"to capture this opportunity") if floor_exception_applied else None,
                },
                "scenario":    "BUY_WITH_CASH",
                "summary":     f"Buy {shares} shares of {ticker} (${pos_value:,.0f} / {pos_pct:.1f}% of portfolio)",
                "telegram_msg": None,
            }

            # ── LAYER 2: Even with cash, is there a swap? ─────
            swaps = find_swap_candidates(
                pick, holdings, stock_data_map, total_capital, attractiveness)

            if swaps:
                best_swap = swaps[0]
                print(f"    {YELLOW}→ L2 swap candidate: {best_swap['ticker']} "
                      f"(+{best_swap['improvement_pct']:.0f}% better){RESET}")
                rec["layer"]    = "1+2"
                rec["swap"]     = best_swap
                rec["scenario"] = "BUY_WITH_CASH_PLUS_SWAP"
                rec["summary"]  += (f" | Also consider swapping {best_swap['ticker']} "
                                    f"— {ticker} scores {best_swap['improvement_pct']:.0f}% stronger")

            # Regime scaling, minimum order, and sequential cash: each buy
            # spends cash the next pick can no longer count on.
            scale_detail(rec["detail"], policy["size_multiplier"])
            d = rec["detail"]
            spend = safe_float(d.get("correlation_adjusted_position") or d.get("suggested_position") or d.get("position_value"))
            if policy["size_multiplier"] == 0:
                print(f"    {YELLOW}→ skipped: regime {policy['regime']} halts new buys{RESET}")
                continue
            if spend < policy["min_order_usd"]:
                print(f"    {YELLOW}→ skipped: ${spend:,.0f} is below the ${policy['min_order_usd']:,.0f} minimum order{RESET}")
                continue
            available_cash -= spend
            d["cash_after"]     = round(available_cash, 2)
            d["position_value"] = round(spend, 2)
            d["position_pct"]   = round(spend / total_capital * 100, 2)
            d["shares"]         = int(d.get("correlation_adjusted_shares") or d.get("suggested_shares") or d.get("shares") or 0)
            swap_note = rec["summary"][rec["summary"].find(" | "):] if " | " in rec["summary"] else ""
            rec["summary"] = f"Buy {d['shares']} shares of {ticker} (${spend:,.0f} / {d['position_pct']:.1f}% of portfolio){swap_note}"
            rec["telegram_msg"] = format_telegram(rec)
            recommendations.append(rec)

        else:
            print(f"{YELLOW}L1: {l1_reason[:50]}{RESET}")

            # ── LAYER 3: Find divestment candidate ────────────
            div_candidate = find_divestment_candidate(
                pick, holdings, stock_data_map, total_capital,
                available_cash, CASH_FLOOR)

            if div_candidate:
                layer = "1+3" if shares > 0 else 3
                print(f"    {BLUE}→ L3 divest candidate: {div_candidate['ticker']} "
                      f"(score {div_candidate['divestment_score']:.0f}) → "
                      f"{div_candidate['shares_to_buy']} shares of {ticker}{RESET}")
                rec = {
                    "pick":    pick,
                    "layer":   layer,
                    "detail":  {"shares": shares, "position_value": round(pos_value, 2),
                                "position_pct": pos_pct, "reason": l1_reason,
                                "limit_price":      limit_price,
                                "limit_note":       limit_note,
                                "suggested_shares": limit_shares,
                                "suggested_position": limit_pos_value,
                                "suggested_pct":    limit_pct,
                                "correlation_adjusted_shares":   corr_shares,
                                "correlation_adjusted_position": corr_position,
                                "correlation_adjusted_pct":      corr_pct,
                                "correlation_note":              corr_note},
                    "divest":  div_candidate,
                    "scenario": "DIVEST_TO_FUND",
                    "summary": (f"Divest {div_candidate['ticker']} → buy "
                                f"{div_candidate['shares_to_buy']} shares of {ticker} "
                                f"({div_candidate['new_position_pct']:.1f}% of portfolio)"),
                    "telegram_msg": None,
                }
                rec["telegram_msg"] = format_telegram(rec)
                recommendations.append(rec)

            else:
                # ── LAYER 4: No viable path ───────────────────
                print(f"    {RED}→ L4: No viable path — opportunity noted{RESET}")
                rec = {
                    "pick":         pick,
                    "layer":        4,
                    "scenario":     "NO_PATH",
                    "summary":      f"{ticker} is a strong pick but no capital path available",
                    "telegram_msg": format_telegram({"pick": pick, "layer": 4, "detail": {}}),
                }
                recommendations.append(rec)

    # Save results
    result = {
        "recommendations": recommendations,
        "evaluated_at":    datetime.now().isoformat(),
        "picks_evaluated": len(picks),
        "total_capital":   total_capital,
        "available_cash":  available_cash,
        "cash_pct":        round(cash_pct, 2),
    }
    save_json(os.path.join(BASE_DIR, "logs", "reallocation.json"), result)

    analysis["reallocation"] = recommendations

    # Summary
    scenarios = {}
    for r in recommendations:
        s = r.get("scenario", "UNKNOWN")
        scenarios[s] = scenarios.get(s, 0) + 1

    print(f"\n{GREEN}✓ Reallocation complete — {len(recommendations)} recommendation(s){RESET}")
    for s, c in scenarios.items():
        label = {
            "BUY_WITH_CASH":            "💰 Buy with cash",
            "BUY_WITH_CASH_PLUS_SWAP":  "💰⚡ Buy + swap opportunity",
            "DIVEST_TO_FUND":           "🔄 Divest to fund",
            "NO_PATH":                  "📋 No path available",
        }.get(s, s)
        print(f"    {label}: {c}")
    print()

    return analysis
