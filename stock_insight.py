# ============================================================
# MARKET AGENT V4.5 — On-Demand Stock Insight
# stock_insight.py <TICKER>
#
# Answers: "what is shaking this stock, its sector, or the broader
# market — hitting the price hard, positive or negative — today?"
#
# Free-tier version — no paid web search tool. Uses yfinance's
# built-in news feed (already used elsewhere in this stack) plus
# intraday 5-minute price reconstruction for the ticker, its sector
# ETF, and SPY. The key move beyond a basic "here are some headlines"
# summary: headline PUBLISH TIMES are matched against the exact
# TIMING of price inflection points (when the stock hit its high,
# when it started reversing), and the same is done in parallel for
# the sector ETF and SPY — so the verdict is STOCK_SPECIFIC vs
# SECTOR_WIDE vs MARKET_WIDE vs UNCLEAR, not just a headline dump.
#
# Upgrade path (not built yet): if Yahoo's news coverage is thin for
# a given ticker, the Claude API web_search tool ($10/1000 searches)
# is a natural drop-in upgrade for this same prompt — noted here so
# the next iteration doesn't have to re-derive this from scratch.
#
# Trigger: python3 stock_insight.py TICKER
# Called by trigger.py's /stock-insight endpoint, itself called from
# a ticker input + button on the dashboard's Run tab.
#
# Sends result directly to Telegram — this is an on-demand, real-time
# tool, not something that waits for the next scheduled batch.
# ============================================================

import json
import os
import sys
import time
from datetime import datetime, timezone
from dotenv import load_dotenv
import anthropic
from claude_json import ask_json, StructuredReplyError
from common import GREEN, RED, YELLOW, BLUE, BOLD, RESET, safe_float, send_telegram

load_dotenv()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
client   = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

INSIGHT_SCHEMA = {
    "type": "object",
    "properties": {
        "classification":  {"type": "string", "enum": ["STOCK_SPECIFIC", "SECTOR_WIDE", "MARKET_WIDE", "UNCLEAR"]},
        "likely_catalyst": {"type": "string"},
        "reversal_read":   {"type": "string"},
        "synthesis":       {"type": "string"},
    },
    "required": ["classification", "likely_catalyst", "reversal_read", "synthesis"],
}


SECTOR_ETF_MAP = {
    "Technology":             "XLK",
    "Healthcare":              "XLV",
    "Financial Services":      "XLF",
    "Financials":              "XLF",
    "Energy":                  "XLE",
    "Utilities":                "XLU",
    "Industrials":              "XLI",
    "Consumer Defensive":       "XLP",
    "Consumer Staples":         "XLP",
    "Consumer Cyclical":        "XLY",
    "Consumer Discretionary":   "XLY",
    "Basic Materials":          "XLB",
    "Materials":                "XLB",
    "Real Estate":              "XLRE",
    "Communication Services":   "XLC",
}

BUCKET_MINUTES = 30  # intraday resolution for the price-shape comparison


# ── SECTOR RESOLUTION ───────────────────────────────────────────
def resolve_sector_etf(ticker, info):
    sector = (info.get("sector") or "").strip()
    etf = SECTOR_ETF_MAP.get(sector)
    if etf:
        return sector, etf
    # Fallback heuristic on name/industry, matching agent_brain.py's spirit
    industry = (info.get("industry") or "").lower()
    name     = (info.get("longName") or info.get("shortName") or "").lower()
    blob = f"{industry} {name}"
    if any(w in blob for w in ["software","semiconductor","chip","cloud","cyber","internet"]):
        return "Technology", "XLK"
    if any(w in blob for w in ["oil","gas","petroleum","energy"]):
        return "Energy", "XLE"
    if any(w in blob for w in ["bank","insurance","financial","invest"]):
        return "Financial Services", "XLF"
    if any(w in blob for w in ["health","pharma","biotech","medical"]):
        return "Healthcare", "XLV"
    return sector or "Unknown", "SPY"  # fall back to SPY itself if truly unknown


# ── INTRADAY PRICE RECONSTRUCTION ───────────────────────────────
def fetch_intraday(ticker):
    """
    Returns dict with open, current, day_high (+time), day_low (+time),
    pct_from_high, and a bucketed % change series for shape comparison.
    Returns None if no intraday data available (market never opened, bad ticker, etc.)
    """
    import yfinance as yf

    hist = yf.Ticker(ticker).history(period="1d", interval="5m")
    if hist is None or hist.empty:
        return None

    open_price = safe_float(hist["Open"].iloc[0])
    current    = safe_float(hist["Close"].iloc[-1])
    high_idx   = hist["High"].idxmax()
    low_idx    = hist["Low"].idxmin()
    day_high   = safe_float(hist.loc[high_idx, "High"])
    day_low    = safe_float(hist.loc[low_idx, "Low"])

    pct_from_high = round(((current - day_high) / day_high) * 100, 2) if day_high > 0 else None

    # Bucket into BUCKET_MINUTES-minute windows, % change from day's open
    resampled = hist["Close"].resample(f"{BUCKET_MINUTES}min").last().dropna()
    buckets = []
    for ts, price in resampled.items():
        pct = round(((price - open_price) / open_price) * 100, 2) if open_price > 0 else None
        buckets.append({"time": ts.strftime("%H:%M"), "pct_change": pct})

    return {
        "open": open_price,
        "current": current,
        "day_high": day_high,
        "day_high_time": high_idx.strftime("%H:%M"),
        "day_low": day_low,
        "day_low_time": low_idx.strftime("%H:%M"),
        "pct_from_high": pct_from_high,
        "pct_change_today": round(((current - open_price) / open_price) * 100, 2) if open_price > 0 else None,
        "buckets": buckets,
    }


def fetch_vix():
    try:
        import yfinance as yf
        info = yf.Ticker("^VIX").info
        price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
        prev  = info.get("previousClose")
        if price is None:
            return None
        chg = round(((price - prev) / prev) * 100, 1) if prev else None
        return {"value": round(safe_float(price), 2), "change_pct": chg}
    except Exception:
        return None


# ── NEWS FETCH ───────────────────────────────────────────────────
def fetch_news(ticker, limit=8):
    """
    yfinance's news feed shape has changed across versions — handles
    both the newer nested {"content": {...}} shape and the older flat
    {"title", "providerPublishTime", "publisher"} shape defensively.
    """
    import yfinance as yf
    try:
        raw = yf.Ticker(ticker).news or []
    except Exception as e:
        print(f"  {YELLOW}⚠ Could not fetch news for {ticker}: {e}{RESET}")
        return []

    items = []
    for entry in raw[:limit]:
        content = entry.get("content") if isinstance(entry.get("content"), dict) else None
        if content:
            title = content.get("title", "")
            publisher = (content.get("provider") or {}).get("displayName", "")
            pub_date = content.get("pubDate") or content.get("displayTime")
            ts = _parse_news_time(pub_date)
        else:
            title = entry.get("title", "")
            publisher = entry.get("publisher", "")
            epoch = entry.get("providerPublishTime")
            ts = datetime.fromtimestamp(epoch).strftime("%H:%M") if epoch else None
        if title:
            items.append({"title": title, "publisher": publisher, "time": ts or "unknown time"})
    return items


def _parse_news_time(iso_str):
    if not iso_str:
        return None
    try:
        # Handles both "...Z" and offset-aware ISO strings
        cleaned = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        return dt.strftime("%H:%M")
    except Exception:
        return None


# ── CLAUDE SYNTHESIS ─────────────────────────────────────────────
def build_prompt(ticker, name, sector, sector_etf, stock_data, sector_data, spy_data, vix, stock_news, sector_news):
    def fmt_buckets(label, data):
        if not data:
            return f"{label}: no intraday data available"
        rows = "  ".join(f"{b['time']}: {b['pct_change']:+.1f}%" for b in data["buckets"] if b["pct_change"] is not None)
        off_high = ""
        if data.get("pct_from_high") is not None:
            off_high = f" — now {data['pct_from_high']:+.1f}% off that high"
        return (f"{label} — Open ${data['open']:.2f}, Now ${data['current']:.2f} "
                f"({data['pct_change_today']:+.1f}% today)\n"
                f"  Day high ${data['day_high']:.2f} at {data['day_high_time']}{off_high}\n"
                f"  Day low ${data['day_low']:.2f} at {data['day_low_time']}\n"
                f"  Shape ({BUCKET_MINUTES}-min buckets, % from day open): {rows}")

    news_block = ""
    if stock_news:
        news_block += f"\n{ticker} headlines today:\n"
        for n in stock_news:
            news_block += f"  [{n['time']}] {n['title']} ({n['publisher']})\n"
    else:
        news_block += f"\nNo {ticker}-specific headlines found today.\n"

    if sector_news:
        news_block += f"\n{sector_etf} ({sector} sector) headlines today:\n"
        for n in sector_news:
            news_block += f"  [{n['time']}] {n['title']} ({n['publisher']})\n"

    vix_block = f"VIX: {vix['value']} ({vix['change_pct']:+.1f}%)" if vix else "VIX: unavailable"

    return f"""You are explaining what's driving today's price action for {ticker} ({name}), for someone who watched the stock spike and then start giving back the gain before they could act on it. Be direct and specific — this is a real-time check, not a general market comment.

{fmt_buckets(f"{ticker}", stock_data)}

{fmt_buckets(f"{sector_etf} (sector ETF)", sector_data)}

{fmt_buckets("SPY (broad market)", spy_data)}

{vix_block}
{news_block}
Your job:
1. Classify today's {ticker} move as one of: STOCK_SPECIFIC, SECTOR_WIDE, MARKET_WIDE, or UNCLEAR — based on whether the sector ETF and SPY moved similarly at the same time, or whether {ticker} moved alone.
2. If a headline's publish time lines up closely with a price inflection point (the day-high time or a sharp move in the bucket series), name it as the likely catalyst. If nothing lines up, say so plainly rather than forcing a connection — a real "no clear catalyst, likely technical/profit-taking" verdict is more useful than a fabricated one.
3. Address the reversal specifically: does the pullback from the day's high look like normal profit-taking after a spike, or does it look like the move is failing / reversing into a real downtrend? Reference relative volume/shape if the data supports it.
4. Write a short, direct synthesis (4-6 sentences) suitable for a Telegram message — no headers, no bullet points, plain prose.

Return ONLY this JSON, no markdown fences, no other text:
{{
  "classification": "STOCK_SPECIFIC|SECTOR_WIDE|MARKET_WIDE|UNCLEAR",
  "likely_catalyst": "specific headline reference or 'No clear catalyst identified'",
  "reversal_read": "1-2 sentences on whether this looks like profit-taking or a real reversal",
  "synthesis": "4-6 sentence plain-prose summary for Telegram"
}}"""


def get_insight(ticker, name, sector, sector_etf, stock_data, sector_data, spy_data, vix, stock_news, sector_news):
    prompt = build_prompt(ticker, name, sector, sector_etf, stock_data, sector_data, spy_data, vix, stock_news, sector_news)
    for attempt in range(2):
        try:
            return ask_json(client, prompt, max_tokens=1200, label=f"stock_insight_{ticker}", schema=INSIGHT_SCHEMA)
        except StructuredReplyError as err:
            print(f"{RED}✗ Insight reply error: {err}{RESET}")
            return None
        except Exception as e:
            if "overloaded" in str(e).lower() and attempt < 1:
                time.sleep(15)
                continue
            print(f"{RED}✗ Insight API error: {e}{RESET}")
            return None
    return None


# ── TELEGRAM FORMATTING ───────────────────────────────────────────
def format_telegram(ticker, name, sector, sector_etf, stock_data, insight):
    if not stock_data:
        return (f"🔍 <b>Stock Insight — {ticker}</b>\n\n"
                f"No intraday data available (market may be closed, or ticker not found).")

    cls_emoji = {
        "STOCK_SPECIFIC": "🎯", "SECTOR_WIDE": "🏭",
        "MARKET_WIDE": "🌐", "UNCLEAR": "❓",
    }
    cls = insight.get("classification", "UNCLEAR") if insight else "UNCLEAR"
    emoji = cls_emoji.get(cls, "❓")

    msg = f"🔍 <b>Stock Insight — {ticker}</b>  ({name})\n\n"
    msg += (f"💵 ${stock_data['current']:.2f}  "
            f"({stock_data['pct_change_today']:+.1f}% today)\n"
            f"📈 Day high ${stock_data['day_high']:.2f} @ {stock_data['day_high_time']}")
    if stock_data.get("pct_from_high") is not None:
        msg += f"  ({stock_data['pct_from_high']:+.1f}% off high now)"
    msg += f"\n📉 Day low ${stock_data['day_low']:.2f} @ {stock_data['day_low_time']}\n\n"

    if insight:
        msg += f"{emoji} <b>{cls.replace('_',' ').title()}</b>\n"
        msg += f"🗞 Catalyst: {insight.get('likely_catalyst','—')}\n"
        msg += f"↩️ {insight.get('reversal_read','')}\n\n"
        msg += f"{insight.get('synthesis','')}"
    else:
        msg += "⚠️ Could not generate synthesis — raw price data only, see above."

    return msg


STATUS_PATH = os.path.join(BASE_DIR, "logs", "stock_insight_status.json")


def write_status(ticker, status, **extra):
    os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
    # Timezone-aware UTC timestamp (with +00:00 offset) — a naive timestamp
    # here gets misread by the browser's Date parser as LOCAL time, which
    # is wrong when server (UTC) and browser (Oman, UTC+4) differ. That
    # mismatch made the dashboard's "is this status fresh?" check almost
    # always fail, forcing every poll to run the full 80s timeout even
    # when the backend had already finished — looked like the button was
    # stuck, when it had actually completed.
    payload = {"ticker": ticker, "status": status,
               "updated_at": datetime.now(timezone.utc).isoformat(), **extra}
    with open(STATUS_PATH, "w") as f:
        json.dump(payload, f)


# ── MAIN ──────────────────────────────────────────────────────────
def run(ticker):
    ticker = ticker.upper().strip()
    print(f"\n{BOLD}{BLUE}{'='*55}")
    print(f"  STOCK INSIGHT — {ticker}")
    print(f"  {datetime.now().strftime('%A %d %B %Y  %H:%M')}")
    print(f"{'='*55}{RESET}\n")

    write_status(ticker, "running")

    import yfinance as yf
    try:
        info = yf.Ticker(ticker).info
    except Exception as e:
        print(f"{RED}✗ Could not fetch info for {ticker}: {e}{RESET}")
        send_telegram(f"🔍 <b>Stock Insight — {ticker}</b>\n\nCould not fetch data for this ticker. Check the symbol and try again.")
        write_status(ticker, "done", success=False, reason="ticker not found")
        return

    name = info.get("longName") or info.get("shortName") or ticker
    sector, sector_etf = resolve_sector_etf(ticker, info)
    print(f"{BLUE}  {ticker} — {name} — Sector: {sector} (ETF: {sector_etf}){RESET}")

    # ── Everything below can hit real-world yfinance flakiness (network
    # hiccups, rate limits, malformed responses) — none of it was
    # previously wrapped, so any crash here left stock_insight_status.json
    # stuck at "running" forever: no Telegram message, no dashboard reset,
    # nothing until a manual page refresh. This safety net guarantees a
    # "done" status and a Telegram notice get sent either way. ──
    try:
        print(f"{BLUE}  Fetching intraday price data...{RESET}")
        stock_data  = fetch_intraday(ticker)
        sector_data = fetch_intraday(sector_etf) if sector_etf != ticker else stock_data
        spy_data    = fetch_intraday("SPY")

        if not stock_data:
            print(f"{YELLOW}⚠ No intraday data for {ticker} — market may be closed{RESET}")
            send_telegram(format_telegram(ticker, name, sector, sector_etf, None, None))
            write_status(ticker, "done", success=False, reason="no intraday data")
            return

        print(f"{BLUE}  Fetching VIX...{RESET}")
        vix = fetch_vix()

        print(f"{BLUE}  Fetching news for {ticker} and {sector_etf}...{RESET}")
        stock_news  = fetch_news(ticker)
        sector_news = fetch_news(sector_etf) if sector_etf != ticker else []
        print(f"  {GREEN}✓ {len(stock_news)} {ticker} headlines, {len(sector_news)} {sector_etf} headlines{RESET}")

        print(f"{BLUE}  Generating synthesis...{RESET}")
        insight = get_insight(ticker, name, sector, sector_etf, stock_data, sector_data, spy_data, vix, stock_news, sector_news)

        if insight:
            print(f"  {GREEN}✓ Classification: {insight.get('classification')}{RESET}")
        else:
            print(f"  {YELLOW}⚠ Synthesis failed — sending raw price data only{RESET}")

        message = format_telegram(ticker, name, sector, sector_etf, stock_data, insight)
        success = send_telegram(message)
        if success:
            print(f"\n{GREEN}✓ Stock insight sent to Telegram{RESET}")
        else:
            print(f"\n{RED}✗ Failed to send to Telegram{RESET}")

        write_status(ticker, "done", success=success,
                     classification=(insight.get("classification") if insight else None))

    except Exception as e:
        print(f"{RED}✗ Unexpected error while building insight for {ticker}: {e}{RESET}")
        import traceback; traceback.print_exc()
        try:
            send_telegram(f"🔍 <b>Stock Insight — {ticker}</b>\n\n"
                           f"Something went wrong generating this insight ({type(e).__name__}). Try again in a moment.")
        except Exception:
            pass
        write_status(ticker, "done", success=False, reason=f"error: {type(e).__name__}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"{RED}Usage: python3 stock_insight.py TICKER{RESET}")
        sys.exit(1)
    run(sys.argv[1])
