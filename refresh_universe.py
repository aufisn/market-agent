#!/usr/bin/env python3
# ============================================================
# MARKET AGENT V4.5 — Stock Universe Refresh
# refresh_universe.py
#
# Manually triggered (via dashboard "Refresh Stock List" button) —
# NOT part of the regular schedule. Builds a stable, comprehensive
# 300-stock candidate pool from ALL NYSE + NASDAQ common stock above
# a market cap floor — replacing the old approach of asking Claude to
# invent ~52 tickers from memory every single run. That approach meant
# a stock passing every quality gate today could simply vanish from
# consideration tomorrow for no reason other than "Claude didn't
# happen to think of it that day" — this fixes that structurally.
#
# Originally sourced from S&P 500 constituents only, but S&P 500
# membership has a hard U.S.-domicile requirement — that structurally
# excludes large, legitimate foreign companies trading on U.S.
# exchanges (Shell, BP, TotalEnergies, etc.), in any sector, not just
# Energy. Sourcing from the full NYSE+NASDAQ listing directory instead
# includes ADRs by construction — a foreign company is eligible the
# exact same way any domestic one is, purely by clearing the same
# market-cap/quality bar. No hardcoded ticker names anywhere in this
# selection — every filter here is a rule (ETF flag, test-issue flag,
# market cap threshold), not a specific company list.
#
# Pipeline:
#   1. Fetch the live NYSE + NASDAQ listing directories (with a cached
#      fallback if the source is unreachable — a failed refresh NEVER
#      leaves you with zero candidates). Filtered to real common stock
#      only via the ETF and Test Issue flags — not by guessing from
#      ticker/name patterns.
#   2. Fetch live fundamentals for every remaining ticker via yfinance
#      (this is now several thousand tickers, not ~500 — realistically
#      30-90+ minutes, since market cap can only be checked by fetching
#      each ticker's full data, not filtered cheaply beforehand).
#   3. Drop anything below MARKET_CAP_FLOOR.
#   4. Score every stock RELATIVE TO ITS OWN SECTOR (revenue growth,
#      margins, low beta, FCF yield, low debt) — pure Python, no Claude
#      call, no added API cost. Sector now comes from yfinance's own
#      classification (the raw listing files don't include GICS sector
#      the way the old S&P-500-only source did).
#   5. Select the top 300 using PER-SECTOR CAPS (scaled from your
#      strategy.json sector weights) as a CEILING, never a floor —
#      a sector's cap only limits how many of ITS stocks can be
#      included; it never forces a weaker stock in from a sector
#      that's short of quality candidates. An adaptive floor (half the
#      day's median score) keeps a short sector from padding its own
#      cap with its own weak stragglers, so genuinely strong leftover
#      candidates from OTHER sectors get first claim on unused slots.
#   6. Save to data/universe_master.json with started_at/finished_at
#      timestamps as the definitive record of when this data is from.
#
# Trigger: python3 refresh_universe.py
# Called by trigger.py's /refresh-universe endpoint, itself called
# from a "Refresh Stock List" button on the dashboard's Run tab. The
# in-progress/completed state lives in a server-side status file, not
# browser memory — closing the dashboard or refreshing the page does
# NOT lose track of a run in progress; re-opening the page picks up
# exactly where the server actually is.
# ============================================================

import json
import os
import sys
import time
from datetime import datetime, timezone
from dotenv import load_dotenv
import requests
from common import GREEN, RED, YELLOW, BLUE, BOLD, RESET, safe_float

load_dotenv()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


CACHE_PATH_NASDAQ = os.path.join(BASE_DIR, "data", "nasdaq_listings_cache.csv")
CACHE_PATH_NYSE   = os.path.join(BASE_DIR, "data", "nyse_listings_cache.csv")
MASTER_LIST_PATH  = os.path.join(BASE_DIR, "data", "universe_master.json")
STATUS_PATH       = os.path.join(BASE_DIR, "logs", "universe_refresh_status.json")
STRATEGY_PATH     = os.path.join(BASE_DIR, "strategy.json")

TARGET_SIZE = 300

# Market-cap tiers — replaces the old single $13B hard floor. Each tier
# gets its own share of TARGET_SIZE, selected independently (same
# relative-to-sector scoring, same per-sector-cap select_top_n logic
# below — just run once per tier instead of once for the whole pool).
# Anything below the lowest tier's floor is still excluded entirely —
# this widens what's eligible, it doesn't remove the floor concept.
MARKET_CAP_TIERS = [
    {"name": "small_mid",  "min": 1_000_000_000,  "max": 5_000_000_000,  "pct": 0.05},
    {"name": "mid_large",  "min": 5_000_000_000,  "max": 13_000_000_000, "pct": 0.25},
    {"name": "large_mega", "min": 13_000_000_000, "max": None,           "pct": 0.70},
]
MARKET_CAP_FLOOR = MARKET_CAP_TIERS[0]["min"]  # $1B — the single absolute cutoff used by the initial fetch-loop filter


def get_market_cap_tier(market_cap):
    """Returns the tier name a given market cap falls into, or None if
    below the lowest tier's floor (shouldn't happen for anything that
    already passed the MARKET_CAP_FLOOR filter, but checked explicitly
    rather than assumed)."""
    for tier in MARKET_CAP_TIERS:
        lo, hi = tier["min"], tier["max"]
        if market_cap >= lo and (hi is None or market_cap < hi):
            return tier["name"]
    return None


def compute_tier_target_sizes(target_size=TARGET_SIZE):
    """Splits target_size across MARKET_CAP_TIERS by their pct, using
    largest-remainder apportionment so the tier sizes always sum to
    EXACTLY target_size regardless of rounding — same technique as
    compute_sector_caps() below, applied one level up."""
    raw = {t["name"]: t["pct"] * target_size for t in MARKET_CAP_TIERS}
    sizes = {n: int(v) for n, v in raw.items()}
    remainder = target_size - sum(sizes.values())
    remainders_sorted = sorted(raw.keys(), key=lambda n: raw[n] - sizes[n], reverse=True)
    for n in remainders_sorted[:remainder]:
        sizes[n] += 1
    return sizes
                                     # A number, not a name — this is what replaces "must be an
                                     # S&P 500 constituent" as the quality gate.

# yfinance's own sector labels (from info.get("sector")) -> internal
# bucket name. Sector now comes from yfinance rather than a CSV column,
# since the raw NYSE/NASDAQ listing files don't include GICS
# classification the way the old S&P-500-only source did. yfinance
# uses its own slightly different labels for a few sectors (e.g.
# "Financial Services" not "Financials", "Consumer Defensive" not
# "Consumer Staples") — mapped here to match this system's existing
# naming convention.
GICS_TO_BUCKET = {
    "Technology":              "Technology",
    "Healthcare":               "Healthcare",
    "Financial Services":       "Financials",
    "Consumer Defensive":       "Consumer Staples",
    "Energy":                   "Energy",
    "Industrials":              "Industrials",
    "Consumer Cyclical":        "Consumer Discretionary",
    "Utilities":                "Utilities",
    "Real Estate":              "Real Estate",
    "Basic Materials":          "Materials",
    "Communication Services":   "Communication Services",
}

# strategy.json's candidate_universe.sectors has only 10 buckets, built
# for the old smaller Claude-generated universe. Real GICS has 11 sectors
# — Materials and Communication Services aren't in that original config
# at all (even though Communication Services already shows up elsewhere
# in this system's own sector tracking, e.g. META). These two need a
# reasonable default weight to fit into the proportional cap scaling —
# worth reviewing once you see real output; not treated as final.
DEFAULT_EXTRA_WEIGHTS = {
    "Materials":              3,   # similar tier to Real Estate
    "Communication Services": 5,   # similar tier to Industrials
}


def write_status(status, started_at=None, **extra):
    """
    started_at is passed through explicitly and preserved across BOTH
    the initial "running" write and the final "done" write — this is
    what lets the dashboard show a real start time even if you check
    back long after triggering it, or reopen the page from scratch.
    """
    os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "status": status,
        "started_at": started_at or now,
        "updated_at": now,
        **extra,
    }
    if status == "done":
        payload["finished_at"] = now
    with open(STATUS_PATH, "w") as f:
        json.dump(payload, f)


# ── STEP 1: S&P 500 CONSTITUENT LIST (with failure protection) ────
# ── STEP 1: NYSE + NASDAQ LISTINGS (with failure protection) ──────
# Security-type exclusions — these are TYPE keywords (warrant, preferred,
# etc.), not company names, matching "no hardcoded stock names" the same
# way the ETF/Test-Issue flags do. "Depositary Shares" alone usually
# denotes a FRACTIONAL PREFERRED STOCK structure (e.g. "1/1,000th
# interest in a share of Preferred Stock") and gets excluded — but
# "American Depositary Shares" is the actual ADR structure for foreign
# common stock (e.g. Ambev, Shell-style listings) and is deliberately
# kept; the whole point of this rebuild.
_EXCLUDED_NAME_PATTERNS = ["Warrant", "Right", "Unit", "Preferred", " Notes", "Debenture"]

def _is_excluded_security_type(name):
    if "American Depositary" in name:
        return False
    if "Depositary Shares" in name and "American Depositary" not in name:
        return True
    return any(p in name for p in _EXCLUDED_NAME_PATTERNS)


def _parse_symbol_directory(raw_text, symbol_field, etf_field, test_field):
    """
    Shared parser for NASDAQ Trader's pipe-delimited symbol directory
    format (used for both nasdaqlisted.txt and otherlisted.txt) — a
    trailing 'File Creation Time' footer line is standard in these
    files and gets skipped automatically since it won't have enough
    pipe-delimited fields to match the header.
    """
    lines = raw_text.strip().split("\n")
    header = lines[0].split("|")
    rows = []
    for line in lines[1:]:
        parts = line.split("|")
        if len(parts) != len(header):
            continue  # footer row or malformed line — skip, don't guess
        row = dict(zip(header, parts))
        if row.get(test_field, "N") == "Y":
            continue  # test listing, not a real security
        if row.get(etf_field, "N") == "Y":
            continue  # ETF, not an individual stock
        symbol = (row.get(symbol_field) or "").strip()
        name   = (row.get("Security Name") or "").strip()
        if not symbol or "$" in symbol:
            continue  # "$" in the symbol itself is a strong preferred-share convention
        if _is_excluded_security_type(name):
            continue
        rows.append({"ticker": symbol.replace(".", "-"), "name": name})
    return rows


def _fetch_with_cache_fallback(url, cache_path, expected_header_prefix, source_label):
    """
    Same failure-protection pattern as the original S&P 500 fetch:
    live source first, sanity-checked before trusting it, falling back
    to the last successfully-cached copy on ANY failure (network error,
    bad status, or a response that doesn't look like the expected
    format — guards against my own assumptions about this source's
    exact structure being wrong, not just network failures).
    """
    raw_text = None
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        text = resp.text
        lines = text.strip().split("\n")
        if len(lines) < 100 or not lines[0].startswith(expected_header_prefix):
            raise ValueError(f"Unexpected format — {len(lines)} lines, header: {lines[0][:60]!r}")
        raw_text = text
        print(f"{GREEN}✓ Fetched live {source_label} list ({len(lines)-1} rows){RESET}")
    except Exception as e:
        print(f"{YELLOW}⚠ Live {source_label} fetch failed ({e}) — falling back to cache{RESET}")

    if raw_text is None:
        if os.path.exists(cache_path):
            with open(cache_path, "r") as f:
                raw_text = f.read()
            cache_age_days = (datetime.now() - datetime.fromtimestamp(os.path.getmtime(cache_path))).days
            print(f"{YELLOW}⚠ Using cached {source_label} list from {cache_age_days} day(s) ago{RESET}")
        else:
            raise RuntimeError(
                f"Live {source_label} fetch failed AND no cached copy exists yet — "
                f"this looks like the very first refresh attempt. Check network "
                f"connectivity on the server and try again."
            )
    else:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w") as f:
            f.write(raw_text)
    return raw_text


def fetch_us_listings():
    """
    Fetches the full NYSE + NASDAQ symbol directories directly from
    NASDAQ Trader (the authoritative source — the various GitHub
    mirrors of this data ultimately scrape the same files). Filtered
    to real, tradeable common stock only via the ETF and Test Issue
    flags that are actual fields in this data — not by guessing from
    ticker or name patterns. This is what makes foreign ADRs (Shell,
    BP, TotalEnergies, etc.) eligible: they're NYSE/NASDAQ-listed
    exactly like any domestic stock, so nothing here treats them any
    differently — no hardcoded company names anywhere in this filter.
    Returns list of {ticker, name} (no sector — comes from yfinance
    during the fundamentals fetch instead, see fetch_fundamentals()).
    """
    nasdaq_text = _fetch_with_cache_fallback(
        "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt",
        CACHE_PATH_NASDAQ, "Symbol|Security Name", "NASDAQ"
    )
    nyse_text = _fetch_with_cache_fallback(
        "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt",
        CACHE_PATH_NYSE, "ACT Symbol|Security Name", "NYSE/other"
    )

    nasdaq_rows = _parse_symbol_directory(nasdaq_text, "Symbol", "ETF", "Test Issue")
    nyse_rows   = _parse_symbol_directory(nyse_text, "ACT Symbol", "ETF", "Test Issue")

    combined = {}
    for row in nasdaq_rows + nyse_rows:
        combined[row["ticker"]] = row  # de-dupe if a symbol somehow appears in both
    result = list(combined.values())
    print(f"{GREEN}✓ Combined NASDAQ ({len(nasdaq_rows)}) + NYSE/other ({len(nyse_rows)}) "
          f"= {len(result)} unique common-stock tickers (ETFs/test issues excluded){RESET}")
    return result


# ── STEP 2: FUNDAMENTALS + QUALITY SCORE (pure Python, no Claude) ──
def fetch_fundamentals(ticker):
    """
    Returns (data_dict_or_None, failure_reason_or_None). Distinguishing WHY a
    fetch failed — rate-limited vs. genuinely no data vs. something else —
    used to be entirely invisible: every failure just returned None with no
    trace of the cause. That hid a real incident: at 5,907 tickers (up from
    the old ~500-stock universe), 98%+ of fetches were failing near-instantly
    — the whole run finished in ~5 minutes when even a conservative estimate
    put it at 30-90+ minutes — almost certainly Yahoo Finance rate-limiting
    yfinance at this volume, not a real absence of qualifying companies.
    """
    import yfinance as yf
    try:
        info = yf.Ticker(ticker).info
        price = safe_float(info.get("regularMarketPrice") or info.get("currentPrice") or info.get("previousClose"))
        if price <= 0:
            return None, "no_price_data"
        mc = safe_float(info.get("marketCap"))
        return {
            "price":            price,
            "beta":             safe_float(info.get("beta") or info.get("beta3Year")),
            "revenue_growth":   safe_float(info.get("revenueGrowth")) * 100,
            "operating_margin": safe_float(info.get("operatingMargins")) * 100,
            "debt_to_equity":   safe_float(info.get("debtToEquity")),
            "fcf_yield":        (safe_float(info.get("freeCashflow")) / mc * 100) if info.get("freeCashflow") and mc > 0 else 0,
            "market_cap":       mc,
            "sector":           info.get("sector") or "",
        }, None
    except Exception as e:
        msg = str(e).lower()
        if "429" in msg or "too many requests" in msg or "rate limit" in msg:
            return None, "rate_limited"
        if "timeout" in msg or "timed out" in msg:
            return None, "timeout"
        if "connection" in msg:
            return None, "connection_error"
        return None, "other_error"


METRIC_WEIGHTS = [
    # (field name, weight, lower_is_better)
    ("revenue_growth",   25, False),
    ("operating_margin", 20, False),
    ("beta",             20, True),
    ("fcf_yield",        20, False),
    ("debt_to_equity",   15, True),
]


def compute_relative_scores(scored_candidates):
    """
    Scores every candidate by PERCENTILE RANK WITHIN ITS OWN SECTOR for
    each metric, rather than on one absolute scale compared across the
    whole market. This replaced an earlier version that scored everyone
    on the same fixed 0-100 formula regardless of sector — which broke
    in a confirmed, real way: revenue growth is inherently cyclical for
    commodity-driven sectors (a temporary gas-vs-oil price divergence
    put EQT/EXE near 90 while XOM/CVX scored below 50, purely from that
    swing, not because they're worse companies), and banks structurally
    carry far higher leverage than industrials as a normal part of how
    banking works — an absolute debt/equity scale penalises Financials
    for something that isn't actually a quality signal in that sector.

    Ranking each stock only against its own sector peers removes
    cross-sector comparability as a variable entirely. A stock's score
    now reflects "how strong is this within its own peer group",
    computed once all fundamentals are gathered (percentiles need the
    full sector group to exist first — this can't be scored per-stock
    in isolation the way the old absolute formula could).
    """
    by_sector = {}
    for c in scored_candidates:
        by_sector.setdefault(c["bucket"], []).append(c)

    for sector, group in by_sector.items():
        n = len(group)
        for field, weight, lower_is_better in METRIC_WEIGHTS:
            def sort_key(c):
                val = c.get(field)
                if val is None:
                    # Missing data sorts to the worst end, not a random spot
                    return float("inf") if lower_is_better else float("-inf")
                return val
            ranked = sorted(group, key=sort_key, reverse=not lower_is_better)
            for rank, c in enumerate(ranked):
                percentile = (1.0 - rank / (n - 1)) if n > 1 else 1.0
                c.setdefault("_metric_percentiles", {})[field] = round(percentile, 3)

    for c in scored_candidates:
        pct = c.get("_metric_percentiles", {})
        c["quality_score"] = round(
            sum(pct.get(field, 0.5) * weight for field, weight, _ in METRIC_WEIGHTS), 2
        )

    return scored_candidates


# ── STEP 3: SECTOR-CAPPED SELECTION (ceiling, never a floor) ───────
def compute_sector_caps(target_size=TARGET_SIZE):
    """
    Scales strategy.json's existing candidate_universe sector slot
    weights proportionally to target_size. These are CEILINGS, not
    quotas — see select_top_n() for how that distinction is enforced.
    Uses largest-remainder apportionment so caps always sum to EXACTLY
    target_size regardless of rounding.
    """
    try:
        with open(STRATEGY_PATH) as f:
            strategy = json.load(f)
        sectors_cfg = strategy.get("candidate_universe", {}).get("sectors", {})
    except FileNotFoundError:
        sectors_cfg = {}

    weights = {}
    for sector, cfg in sectors_cfg.items():
        if sector == "ETFs & Commodities":
            continue  # not part of the S&P 500 constituent universe at all
        weights[sector] = cfg.get("slots", 5)
    for sector, w in DEFAULT_EXTRA_WEIGHTS.items():
        weights.setdefault(sector, w)

    total_weight = sum(weights.values())
    raw  = {s: (w / total_weight) * target_size for s, w in weights.items()}
    caps = {s: int(v) for s, v in raw.items()}
    remainder = target_size - sum(caps.values())
    remainders_sorted = sorted(raw.keys(), key=lambda s: raw[s] - caps[s], reverse=True)
    for s in remainders_sorted[:remainder]:
        caps[s] += 1
    return caps


def select_top_n(scored_candidates, caps, target_size=TARGET_SIZE):
    """
    Two-pass selection, ranking on the relative-to-sector quality_score
    computed by compute_relative_scores():

    Pass 0 — an ADAPTIVE floor (half the global median score) removes
    candidates clearly below the pack before anything else runs. This
    went through two wrong turns before landing here, worth being
    explicit about: a FIXED absolute floor (was 40) wrongly excluded a
    big chunk of any sector's genuinely reasonable stocks once scoring
    became relative-to-sector (scores now center on ~50 by construction,
    so a fixed 40 cut roughly the bottom quartile of every sector
    regardless of whether those stocks were actually bad — confirmed:
    7 of 30 solid, non-broken Energy candidates excluded for no real
    reason). Removing the floor ENTIRELY then reintroduced the ORIGINAL
    problem from a different angle: a sector with slightly more
    candidates than its cap could fill that cap entirely using its own
    weak stragglers, and because that fully satisfies its cap, Pass 2
    never even gets triggered to compare against a different sector's
    genuinely better leftover candidates. The floor's real job was
    never "filter bad companies" — it's "create enough slack in a
    mediocre sector's contribution for Pass 2 to have something to
    redistribute." A floor tied to the actual score distribution (half
    the median) does that adaptively, confirmed against both failure
    scenarios directly rather than guessing a fixed number.

    Pass 1 — rank remaining candidates globally (sector-blind), walk
    down adding each to the result unless its sector has already hit
    its cap.

    Pass 2 — if a short sector couldn't fill its cap, the remaining
    target_size slots go to the next-best leftover candidates from ANY
    sector, even one already at its nominal cap. This is the piece a
    naive single-pass cap-walk gets wrong: without it, a sector that
    happens to exhaust its candidates early can force in leftovers from
    an under-filled sector, even while a different sector's still-strong
    6th, 7th, 8th-best candidates sit unused only because that sector
    filled its cap first. Sector caps prevent over-concentration; they
    were never meant to block a sector's genuinely strong candidates
    from filling slack elsewhere left by a weaker sector.
    """
    if not scored_candidates:
        return [], {s: 0 for s in caps}

    scores = [c["quality_score"] for c in scored_candidates]
    sorted_scores = sorted(scores)
    n = len(sorted_scores)
    median = sorted_scores[n // 2] if n % 2 == 1 else (sorted_scores[n // 2 - 1] + sorted_scores[n // 2]) / 2
    floor = median * 0.5

    eligible = [c for c in scored_candidates if c["quality_score"] >= floor]
    ranked   = sorted(eligible, key=lambda c: -c["quality_score"])

    sector_counts = {s: 0 for s in caps}
    selected      = []
    selected_ids  = set()

    # Pass 1 — respect caps
    for c in ranked:
        bucket = c.get("bucket", "Other")
        cap = caps.get(bucket)
        if cap is None or sector_counts[bucket] >= cap:
            continue
        selected.append(c)
        selected_ids.add(c["ticker"])
        sector_counts[bucket] += 1
        if len(selected) >= target_size:
            break

    # Pass 2 — fill any remaining slots from the next-best leftover
    # candidates regardless of sector/cap (still subject to the floor,
    # since `ranked` was already floor-filtered above)
    if len(selected) < target_size:
        for c in ranked:
            if c["ticker"] in selected_ids:
                continue
            selected.append(c)
            selected_ids.add(c["ticker"])
            bucket = c.get("bucket", "Other")
            sector_counts[bucket] = sector_counts.get(bucket, 0) + 1
            if len(selected) >= target_size:
                break

    return selected, sector_counts


# ── MAIN ────────────────────────────────────────────────────────
def run():
    print(f"\n{BOLD}{BLUE}{'='*55}")
    print(f"  STOCK UNIVERSE REFRESH")
    print(f"  {datetime.now().strftime('%A %d %B %Y  %H:%M')}")
    print(f"{'='*55}{RESET}\n")
    started_at = datetime.now(timezone.utc).isoformat()
    write_status("running", started_at=started_at)

    try:
        constituents = fetch_us_listings()
    except Exception as e:
        print(f"{RED}✗ {e}{RESET}")
        write_status("done", started_at=started_at, success=False, reason=str(e))
        sys.exit(1)

    print(f"{BLUE}Fetching live fundamentals for {len(constituents)} stocks "
          f"(this is a large universe now — realistically 30-90+ minutes)...{RESET}")
    scored = []
    failure_counts = {}
    REQUEST_DELAY_SECONDS = 0.3  # deliberate pacing — Yahoo Finance rate-limits yfinance
                                 # aggressively at this volume (confirmed: an earlier run
                                 # against 5,907 tickers finished in ~5 minutes instead of
                                 # the expected 30-90+, with 98%+ of fetches failing
                                 # near-instantly). This trades some runtime for actually
                                 # getting real data back.
    for i, c in enumerate(constituents):
        f, fail_reason = fetch_fundamentals(c["ticker"])
        if f is None:
            failure_counts[fail_reason] = failure_counts.get(fail_reason, 0) + 1
            time.sleep(REQUEST_DELAY_SECONDS)
            continue
        if f["market_cap"] < MARKET_CAP_FLOOR:
            time.sleep(REQUEST_DELAY_SECONDS)
            continue  # below the $1B floor — the lowest of the three market-cap tiers
        bucket = GICS_TO_BUCKET.get(f["sector"], f["sector"] or "Other")
        cap_tier = get_market_cap_tier(f["market_cap"])
        scored.append({**c, **f, "bucket": bucket, "cap_tier": cap_tier})
        time.sleep(REQUEST_DELAY_SECONDS)
        if (i + 1) % 200 == 0:
            print(f"  ...{i+1}/{len(constituents)} processed, {len(scored)} above the market cap floor so far")
        if (i + 1) % 25 == 0 or (i + 1) == len(constituents):
            # Real mid-run progress, not just a start/end status — this is
            # what lets the dashboard render an actual percentage rather
            # than a generic "still working" spinner. Every 25 tickers,
            # not every single one, to avoid needless disk writes across
            # a list this size.
            write_status("running", started_at=started_at, processed=i + 1, total=len(constituents))

    print(f"\n{GREEN}✓ {len(scored)}/{len(constituents)} stocks cleared the "
          f"${MARKET_CAP_FLOOR/1e9:.0f}B market cap floor{RESET}")
    if failure_counts:
        total_failed = sum(failure_counts.values())
        print(f"{YELLOW}  {total_failed} fetch attempts did not return usable data:{RESET}")
        for reason, count in sorted(failure_counts.items(), key=lambda x: -x[1]):
            print(f"    {reason}: {count}")
        if failure_counts.get("rate_limited", 0) > len(constituents) * 0.1:
            print(f"{RED}  ⚠ Rate limiting looks significant — consider increasing "
                  f"REQUEST_DELAY_SECONDS if this recurs{RESET}")

    # Relative-to-sector scoring — must happen AFTER all fundamentals are
    # gathered, since percentiles need each stock's full sector peer
    # group to exist first (can't be computed one stock at a time the
    # way the old absolute formula could).
    scored = compute_relative_scores(scored)

    caps = compute_sector_caps(TARGET_SIZE)
    print(f"\n{BLUE}Sector caps (ceiling, not quota, at full 300-stock scale — reference only, actual selection runs per market-cap tier below):{RESET}")
    for s, cap in sorted(caps.items(), key=lambda x: -x[1]):
        print(f"  {s}: max {cap}")

    tier_sizes = compute_tier_target_sizes(TARGET_SIZE)
    print(f"\n{BLUE}Market-cap tier targets:{RESET}")
    for t in MARKET_CAP_TIERS:
        hi_label = "∞" if t["max"] is None else f"${t['max']/1e9:.0f}B"
        print(f"  {t['name']} (${t['min']/1e9:.0f}B–{hi_label}, {t['pct']*100:.0f}%): target {tier_sizes[t['name']]}")

    # Selected independently PER TIER — same relative-to-sector scores
    # (computed once, above, across the whole pool regardless of tier —
    # scoring itself is unchanged), same select_top_n cap-respecting
    # logic, just budgeted separately so e.g. a flood of qualifying
    # large-caps can never crowd out the small_mid tier's own 5% share.
    selected = []
    sector_counts = {}
    tier_breakdown = {}
    for t in MARKET_CAP_TIERS:
        tier_pool = [c for c in scored if c.get("cap_tier") == t["name"]]
        tier_target = tier_sizes[t["name"]]
        tier_caps = compute_sector_caps(tier_target)
        tier_selected, tier_sector_counts = select_top_n(tier_pool, tier_caps, tier_target)
        selected.extend(tier_selected)
        tier_breakdown[t["name"]] = {
            "target":        tier_target,
            "actual":        len(tier_selected),
            "pool_size":     len(tier_pool),
            "sector_counts": tier_sector_counts,
        }
        for s, c in tier_sector_counts.items():
            sector_counts[s] = sector_counts.get(s, 0) + c
        short_flag = "" if len(tier_selected) >= tier_target else f" — SHORT by {tier_target - len(tier_selected)} (pool only had {len(tier_pool)} qualifying candidates)"
        print(f"\n{GREEN}✓ {t['name']}: selected {len(tier_selected)}/{tier_target} from a pool of {len(tier_pool)}{short_flag}{RESET}")
        for s, count in sorted(tier_sector_counts.items(), key=lambda x: -x[1]):
            if count:
                print(f"    {s}: {count}/{tier_caps.get(s, 0)}")

    print(f"\n{GREEN}✓ Selected {len(selected)} stocks total across all tiers:{RESET}")
    for s, count in sorted(sector_counts.items(), key=lambda x: -x[1]):
        cap = caps.get(s, 0)
        flag = " (at or near 300-scale cap)" if count >= cap else ""
        print(f"  {s}: {count}{flag}")

    finished_at = datetime.now(timezone.utc).isoformat()
    output = {
        "started_at":    started_at,
        "finished_at":   finished_at,
        "refreshed_at":  finished_at,  # kept for backward compatibility with the dashboard's earlier field name
        "source_size":   len(constituents),
        "market_cap_floor": MARKET_CAP_FLOOR,  # $1B — the absolute cutoff, not a single-tier threshold; see market_cap_tiers for the actual allocation
        "market_cap_tiers": MARKET_CAP_TIERS,
        "tier_breakdown":   tier_breakdown,
        "target_size":   TARGET_SIZE,
        "actual_size":   len(selected),
        "sector_caps":   caps,
        "sector_counts": sector_counts,
        "stocks": [
            {"ticker": s["ticker"], "name": s["name"], "sector": s["bucket"],
             "quality_score": s["quality_score"], "cap_tier": s.get("cap_tier")}
            for s in selected
        ],
    }
    os.makedirs(os.path.dirname(MASTER_LIST_PATH), exist_ok=True)
    with open(MASTER_LIST_PATH, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n{GREEN}✓ Saved {len(selected)} stocks to {MASTER_LIST_PATH}{RESET}")
    print(f"{GREEN}✓ Started {started_at} — finished {finished_at}{RESET}")
    write_status("done", started_at=started_at, success=True, count=len(selected),
                 finished_at=finished_at, refreshed_at=finished_at)


if __name__ == "__main__":
    run()
