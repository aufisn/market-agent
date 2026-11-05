# ============================================================
# MARKET AGENT V4.5 — Monthly Performance Report
# Runs on the 1st of each month at 08:00 Oman (04:00 UTC)
# Sends a comprehensive report to Telegram
# ============================================================

import json
import os
import glob
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
from common import GREEN, RED, YELLOW, BLUE, BOLD, RESET, safe_float, load_json

load_dotenv()
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(BASE_DIR, "logs", "performance_history.json")
LATEST_FILE  = os.path.join(BASE_DIR, "logs", "latest.json")
PERF_LOG     = os.path.join(BASE_DIR, "logs", "performance_log.json")
PEAK_FILE    = os.path.join(BASE_DIR, "logs", "portfolio_peak.json")
THESIS_LOG   = os.path.join(BASE_DIR, "logs", "thesis_log.json")
OUTCOME_LOG  = os.path.join(BASE_DIR, "logs", "outcome_log.json")
CORR_MATRIX  = os.path.join(BASE_DIR, "logs", "correlation_matrix.json")


def record_snapshot():
    """Record today's portfolio snapshot into performance_history.json"""
    latest = load_json(LATEST_FILE)
    if not latest:
        return

    ps    = latest.get("portfolio_summary", {})
    tv    = safe_float(ps.get("total_value")   or latest.get("total_value",   0))
    tc    = safe_float(ps.get("total_cost")    or latest.get("total_cost",    0))
    cash  = safe_float(ps.get("cash")          or latest.get("cash",          0))
    cap   = safe_float(ps.get("total_capital") or latest.get("total_capital", 0))
    pnl   = safe_float(ps.get("total_pnl_usd") or latest.get("total_pnl_usd", 0))
    pnlp  = safe_float(ps.get("total_pnl_pct") or latest.get("total_pnl_pct", 0))
    holds = len(latest.get("holdings", []))
    picks = len(latest.get("agent_picks", []))

    # Load history
    history = load_json(HISTORY_FILE) or []

    snapshot = {
        "date":           datetime.now().strftime("%Y-%m-%d"),
        "time":           datetime.now().isoformat(),
        "total_value":    round(tv, 2),
        "total_cost":     round(tc, 2),
        "total_capital":  round(cap, 2),
        "cash":           round(cash, 2),
        "pnl_usd":        round(pnl, 2),
        "pnl_pct":        round(pnlp, 2),
        "holdings_count": holds,
        "picks_count":    picks,
    }

    # Avoid duplicate dates
    today = snapshot["date"]
    history = [h for h in history if h.get("date") != today]
    history.append(snapshot)

    # Keep last 365 days only
    history = sorted(history, key=lambda x: x.get("date",""))[-365:]

    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)

    print(f"{GREEN}✓ Snapshot recorded: ${cap:,.2f} total capital{RESET}")
    return snapshot

def get_spy_return(days=30):
    """Fetch SPY return over last N days as benchmark"""
    try:
        import yfinance as yf
        spy  = yf.Ticker("SPY")
        hist = spy.history(period=f"{days+5}d")
        if len(hist) < 2:
            return None
        start = float(hist["Close"].iloc[0])
        end   = float(hist["Close"].iloc[-1])
        return round(((end - start) / start) * 100, 2)
    except Exception:
        return None

def calc_signal_accuracy(perf_log):
    """Calculate signal hit rate from performance_log.json"""
    if not perf_log:
        return None, 0, 0
    closed = [e for e in perf_log if e.get("status") in ("HIT","STOP")]
    if not closed:
        return None, 0, len([e for e in perf_log if e.get("status")=="OPEN"])
    hits    = len([e for e in closed if e.get("status")=="STOP" or
                   (e.get("status")=="HIT")])
    # Count actual hits
    hits    = len([e for e in closed if e.get("status")=="HIT"])
    rate    = round((hits / len(closed)) * 100, 1)
    open_ct = len([e for e in perf_log if e.get("status")=="OPEN"])
    return rate, len(closed), open_ct


# ── INTELLIGENCE LAYER — MONTHLY AGGREGATION ─────────────
# These pull from the memory features built this session (thesis_log,
# outcome_log, correlation_matrix) plus a scan of the month's dated
# analysis_*.json snapshots for data that isn't persisted long-term
# anywhere else (adversarial critique, gate7 tier distribution).

def load_month_snapshots(year, month):
    """Load every dated analysis_*.json snapshot saved during the given month."""
    pattern = os.path.join(BASE_DIR, "logs", f"analysis_{year}{month:02d}*.json")
    snapshots = []
    for p in sorted(glob.glob(pattern)):
        data = load_json(p)
        if data:
            snapshots.append(data)
    return snapshots


def summarize_thesis_health(thesis_log, month_start_str, month_end_str):
    """Current thesis status tally, plus any status changes that happened this month."""
    if not thesis_log:
        return {"intact": 0, "weakening": 0, "broken": 0, "changes_this_month": []}
    tally = {"intact": 0, "weakening": 0, "broken": 0}
    changes = []
    for ticker, entry in thesis_log.items():
        status = (entry.get("current_status") or "INTACT").lower()
        if status in tally:
            tally[status] += 1
        for h in entry.get("status_history", []):
            d = (h.get("date") or "")[:10]
            if month_start_str <= d <= month_end_str:
                changes.append({
                    "ticker": ticker, "from": h.get("from"), "to": h.get("to"),
                    "reasoning": h.get("reasoning", ""), "date": d,
                })
    changes.sort(key=lambda c: c["date"])
    return {"intact": tally["intact"], "weakening": tally["weakening"],
            "broken": tally["broken"], "changes_this_month": changes}


def summarize_outcome_grades(outcome_log, month_start_str, month_end_str):
    """Tally checkpoint + exit-review grades that were GRADED during this month."""
    if not outcome_log:
        return {"correct": 0, "partial": 0, "incorrect": 0, "graded": []}
    tally = {"correct": 0, "partial": 0, "incorrect": 0}
    grade_key = {"CORRECT": "correct", "PARTIALLY_CORRECT": "partial", "INCORRECT": "incorrect"}
    graded = []
    for ticker_key, entry in outcome_log.items():
        ticker = entry.get("ticker", ticker_key.split("::")[0])
        for horizon, cp in (entry.get("checkpoints") or {}).items():
            if cp.get("status") == "graded" and cp.get("graded_at"):
                d = cp["graded_at"][:10]
                if month_start_str <= d <= month_end_str and cp.get("grade") in grade_key:
                    tally[grade_key[cp["grade"]]] += 1
                    graded.append({"ticker": ticker, "horizon": f"{horizon}d",
                                    "grade": cp["grade"], "reasoning": cp.get("reasoning", ""), "date": d})
        er = entry.get("exit_review")
        if er and er.get("status") == "graded" and er.get("graded_at"):
            d = er["graded_at"][:10]
            if month_start_str <= d <= month_end_str and er.get("grade") in grade_key:
                tally[grade_key[er["grade"]]] += 1
                graded.append({"ticker": ticker, "horizon": "exit",
                                "grade": er["grade"], "reasoning": er.get("reasoning", ""), "date": d})
    graded.sort(key=lambda g: g["date"])
    return {"correct": tally["correct"], "partial": tally["partial"],
            "incorrect": tally["incorrect"], "graded": graded}


def summarize_adversarial_critique(snapshots):
    """Aggregate bear-case strength across every pick seen in this month's snapshots."""
    tally = {"WEAK": 0, "MODERATE": 0, "STRONG": 0}
    strong_flags = {}  # ticker -> most recent {argument, date} seen this month
    for snap in snapshots:
        d = (snap.get("saved_at") or "")[:10]
        for pick in snap.get("agent_picks", []):
            bc = pick.get("bear_case")
            if not bc or bc.get("strength") not in tally:
                continue
            tally[bc["strength"]] += 1
            if bc["strength"] == "STRONG":
                strong_flags[pick.get("ticker", "")] = {"argument": bc.get("argument", ""), "date": d}
    return {"tally": tally, "strong_flags": strong_flags}


def summarize_correlation(corr_matrix, threshold=0.7):
    """Current holdings-vs-holdings pairs at or above the correlation threshold."""
    pairs, seen = [], set()
    for t1, row in (corr_matrix or {}).items():
        for t2, c in (row or {}).items():
            if c is None:
                continue
            pair = tuple(sorted([t1, t2]))
            if pair in seen:
                continue
            seen.add(pair)
            if c >= threshold:
                pairs.append({"a": pair[0], "b": pair[1], "correlation": c})
    pairs.sort(key=lambda p: -p["correlation"])
    return pairs


def summarize_gate_distribution(snapshots):
    """Green (all 7 gates) vs Blue (G1-6 only) pick counts this month, deduped by ticker+day."""
    seen, green, blue = set(), 0, 0
    for snap in snapshots:
        d = (snap.get("saved_at") or "")[:10]
        for pick in snap.get("agent_picks", []):
            key = (pick.get("ticker", ""), d)
            if key in seen:
                continue
            seen.add(key)
            if pick.get("gate7_status") == "FAILED":
                blue += 1
            else:
                green += 1
    return {"green": green, "blue": blue}


def summarize_macro_trend(snapshots):
    """Start-of-month vs most-recent macro indicator snapshot, if available."""
    with_macro = [s for s in snapshots if s.get("macro_indicators")]
    if not with_macro:
        return None
    first, last = with_macro[0], with_macro[-1]
    return {
        "first_date": (first.get("saved_at") or "")[:10],
        "last_date":  (last.get("saved_at") or "")[:10],
        "first":      first.get("macro_indicators", {}),
        "last":       last.get("macro_indicators", {}),
    }


# ── PDF COLOURS & HELPERS ─────────────────────────────────────
def _c(hex_str):
    """Convert #RRGGBB to reportlab Color"""
    from reportlab.lib.colors import HexColor
    return HexColor(hex_str)

C_BG       = _c("#050810")
C_CARD     = _c("#0C1824")
C_BORDER   = _c("#0E2030")
C_BLUE     = _c("#0EA5C9")
C_LBLUE    = _c("#7DD4F8")
C_TEXT     = _c("#C4D9EF")
C_DIM      = _c("#2A4A64")
C_GREEN    = _c("#00C896")
C_RED      = _c("#FF4D6A")
C_AMBER    = _c("#F0A500")
C_WHITE    = _c("#E8F4FF")


def build_pdf(data: dict, output_path: str):
    """
    Generate a professional monthly performance PDF.
    data keys: month_nm, curr_cap, curr_pnl, curr_pnlp, cash, cash_pct,
               month_chg_usd, month_chg_pct, q_chg_usd, q_chg_pct,
               h6_chg_usd, h6_chg_pct, inception_pct, inception_chg, inception_date,
               spy_month, spy_q, spy_6m,
               peak_val, peak_date, drawdown, dd_mode,
               acc_rate, closed_ct, open_ct,
               holdings, picks, history_30,
               universe_size, picks_count,
               realloc_count
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen       import canvas
    from reportlab.lib.colors   import Color
    from reportlab.platypus     import Table, TableStyle
    from reportlab.lib          import colors

    W, H = A4   # 595 x 842 pt
    c    = canvas.Canvas(output_path, pagesize=A4)

    sign = lambda x: "+" if x >= 0 else ""
    col  = lambda x: C_GREEN if x >= 0 else C_RED

    # ── helpers ──────────────────────────────────────────────
    def set_font(face="Helvetica", size=10, color=None):
        c.setFont(face, size)
        if color:
            c.setFillColor(color)

    def draw_rect(x, y, w, h, fill=C_CARD, stroke=C_BORDER, radius=4):
        c.setFillColor(fill)
        c.setStrokeColor(stroke)
        c.setLineWidth(0.5)
        c.roundRect(x, y, w, h, radius, fill=1, stroke=1)

    def section_header(y, title):
        c.setFillColor(C_BLUE)
        c.rect(36, y - 2, W - 72, 18, fill=1, stroke=0)
        set_font("Helvetica-Bold", 9, C_WHITE)
        c.drawString(42, y + 3, title.upper())
        return y - 26

    def kpi_box(x, y, w, h, label, value, value_color=C_WHITE, sub=None):
        draw_rect(x, y, w, h)
        set_font("Helvetica", 7, C_DIM)
        c.drawString(x + 8, y + h - 13, label.upper())
        set_font("Helvetica-Bold", 13, value_color)
        c.drawString(x + 8, y + h - 30, value)
        if sub:
            set_font("Helvetica", 7, C_DIM)
            c.drawString(x + 8, y + 6, sub)

    def bar(x, y, w, h, pct_port, pct_spy, label):
        """Draw a small horizontal bar comparison"""
        max_pct = max(abs(pct_port), abs(pct_spy), 1) * 1.3
        bar_w   = (W - 72 - 80) / 2

        set_font("Helvetica-Bold", 8, C_TEXT)
        c.drawString(x, y + 4, label)

        # Portfolio bar
        port_len = max(2, int(bar_w * abs(pct_port) / max_pct))
        c.setFillColor(col(pct_port))
        c.rect(x + 60, y + 4, port_len if pct_port >= 0 else -port_len, 8, fill=1, stroke=0)
        set_font("Helvetica-Bold", 8, col(pct_port))
        c.drawString(x + 60 + port_len + 3, y + 4, f"{sign(pct_port)}{pct_port:.2f}%")

        # SPY bar
        spy_len = max(2, int(bar_w * abs(pct_spy) / max_pct)) if pct_spy is not None else 0
        if pct_spy is not None:
            c.setFillColor(C_DIM)
            c.rect(x + 60, y - 6, spy_len if pct_spy >= 0 else -spy_len, 6, fill=1, stroke=0)
            set_font("Helvetica", 7, C_DIM)
            c.drawString(x + 60 + spy_len + 3, y - 6, f"SPY {sign(pct_spy)}{pct_spy:.2f}%")

    def line_chart(x, y, w, h, values, label=""):
        """Draw a simple line chart for capital history"""
        if not values or len(values) < 2:
            return
        mn, mx = min(values), max(values)
        rng     = mx - mn if mx != mn else 1

        pts = []
        for i, v in enumerate(values):
            px = x + (i / (len(values) - 1)) * w
            py = y + ((v - mn) / rng) * h
            pts.append((px, py))

        # Fill
        fill_pts = [(x, y)] + pts + [(x + w, y)]
        path = c.beginPath()
        path.moveTo(*fill_pts[0])
        for pt in fill_pts[1:]:
            path.lineTo(*pt)
        path.close()
        c.setFillColor(Color(0.055, 0.651, 0.788, alpha=0.12))
        c.drawPath(path, fill=1, stroke=0)

        # Line
        c.setStrokeColor(C_BLUE)
        c.setLineWidth(1.5)
        path2 = c.beginPath()
        path2.moveTo(*pts[0])
        for pt in pts[1:]:
            path2.lineTo(*pt)
        c.drawPath(path2, fill=0, stroke=1)

        # Min/max labels
        set_font("Helvetica", 7, C_DIM)
        c.drawString(x, y - 10, f"${mn:,.0f}")
        c.drawRightString(x + w, y - 10, f"${mx:,.0f}")
        if label:
            set_font("Helvetica", 7, C_DIM)
            c.drawCentredString(x + w/2, y - 10, label)

    # ════════════════════════════════════════════════════════
    # PAGE 1 — COVER + KPIs + PERFORMANCE
    # ════════════════════════════════════════════════════════
    c.setFillColor(C_BG)
    c.rect(0, 0, W, H, fill=1, stroke=0)

    # Header bar
    c.setFillColor(C_BLUE)
    c.rect(0, H - 60, W, 60, fill=1, stroke=0)
    set_font("Helvetica-Bold", 18, C_WHITE)
    c.drawString(36, H - 38, "Market Agent V4.5")
    set_font("Helvetica", 11, C_WHITE)
    c.drawRightString(W - 36, H - 38, f"Monthly Report — {data['month_nm']}")
    set_font("Helvetica", 8, _c("#AACCEE"))
    c.drawString(36, H - 52, "Portfolio Performance & Analysis")
    c.drawRightString(W - 36, H - 52, f"Generated {datetime.now().strftime('%d %b %Y  %H:%M')} GST")

    # ── KPI ROW ───────────────────────────────────────────────
    y = H - 90
    bw = (W - 72 - 12) / 4
    bh = 54

    kpi_box(36,          y - bh, bw, bh, "Total Capital",
            f"${data['curr_cap']:,.0f}", C_WHITE)
    kpi_box(36 + bw + 4, y - bh, bw, bh, "Total P&L",
            f"{sign(data['curr_pnlp'])}{data['curr_pnlp']:.2f}%",
            col(data['curr_pnlp']),
            f"{sign(data['curr_pnl'])}${abs(data['curr_pnl']):,.0f}")
    kpi_box(36 + (bw+4)*2, y - bh, bw, bh, "Cash",
            f"${data['cash']:,.0f}",
            C_WHITE, f"{data['cash_pct']:.1f}% of capital")
    dd_color = C_GREEN if data['dd_mode']=='NORMAL' else C_AMBER if data['dd_mode']=='DEFENSIVE' else C_RED
    kpi_box(36 + (bw+4)*3, y - bh, bw, bh, "Drawdown",
            f"{data['drawdown']:.1f}%",
            dd_color, data['dd_mode'])

    # ── PERFORMANCE vs BENCHMARK ──────────────────────────────
    y = y - bh - 24
    y = section_header(y, "📈  Performance vs S&P 500")

    periods = [
        ("1 Month",        data['month_chg_pct'], data['spy_month']),
        ("3 Months",       data['q_chg_pct'],     data['spy_q']),
        ("6 Months",       data['h6_chg_pct'],    data['spy_6m']),
        (f"Since Inception ({data['inception_date']})", data['inception_pct'], None),
    ]
    for label, port_pct, spy_pct in periods:
        bar(36, y, W - 72, 20, port_pct, spy_pct or 0, label)
        y -= 30

    # Alpha summary box
    y -= 4
    draw_rect(36, y - 30, W - 72, 28)
    set_font("Helvetica-Bold", 8, C_LBLUE)
    c.drawString(44, y - 10, "1M ALPHA")
    alpha_1m = data['month_chg_pct'] - (data['spy_month'] or 0)
    set_font("Helvetica-Bold", 11, col(alpha_1m))
    c.drawString(110, y - 11, f"{sign(alpha_1m)}{alpha_1m:.2f}%")

    set_font("Helvetica-Bold", 8, C_LBLUE)
    c.drawString(200, y - 10, "3M ALPHA")
    alpha_3m = data['q_chg_pct'] - (data['spy_q'] or 0)
    set_font("Helvetica-Bold", 11, col(alpha_3m))
    c.drawString(266, y - 11, f"{sign(alpha_3m)}{alpha_3m:.2f}%")

    set_font("Helvetica-Bold", 8, C_LBLUE)
    c.drawString(356, y - 10, "INCEPTION GAIN")
    set_font("Helvetica-Bold", 11, col(data['inception_pct']))
    c.drawString(450, y - 11, f"{sign(data['inception_chg'])}${abs(data['inception_chg']):,.0f}")

    y -= 44

    # ── 30-DAY CHART ──────────────────────────────────────────
    y = section_header(y, "📊  30-Day Capital Trend")
    hist_vals = [safe_float(h.get("total_capital", 0)) for h in data.get("history_30", [])]
    if hist_vals:
        draw_rect(36, y - 90, W - 72, 82)
        line_chart(50, y - 82, W - 100, 64, hist_vals)
    y -= 108

    # ── DRAWDOWN ──────────────────────────────────────────────
    y = section_header(y, "🛡️  Drawdown Protection")
    draw_rect(36, y - 36, W - 72, 30)
    set_font("Helvetica", 8, C_DIM)
    c.drawString(44, y - 12, "Peak Value")
    set_font("Helvetica-Bold", 9, C_WHITE)
    c.drawString(44, y - 24, f"${data['peak_val']:,.2f}  ({data['peak_date']})")

    set_font("Helvetica", 8, C_DIM)
    c.drawString(220, y - 12, "Current Drawdown")
    set_font("Helvetica-Bold", 9, dd_color)
    c.drawString(220, y - 24, f"{data['drawdown']:.1f}%  —  {data['dd_mode']}")

    set_font("Helvetica", 8, C_DIM)
    c.drawString(390, y - 12, "Status")
    status_emoji = "NORMAL ✓" if data['dd_mode']=='NORMAL' else "DEFENSIVE ⚠" if data['dd_mode']=='DEFENSIVE' else "CRITICAL ✗"
    set_font("Helvetica-Bold", 9, dd_color)
    c.drawString(390, y - 24, status_emoji)
    y -= 52

    # Footer P1
    set_font("Helvetica", 7, C_DIM)
    c.drawCentredString(W/2, 18, "Market Agent V4.5  ·  Not financial advice  ·  Page 1 of 3")
    c.setStrokeColor(C_BORDER)
    c.setLineWidth(0.5)
    c.line(36, 28, W - 36, 28)

    # ════════════════════════════════════════════════════════
    # PAGE 2 — HOLDINGS + SIGNALS + PICKS
    # ════════════════════════════════════════════════════════
    c.showPage()
    c.setFillColor(C_BG)
    c.rect(0, 0, W, H, fill=1, stroke=0)

    # Header bar P2
    c.setFillColor(C_CARD)
    c.rect(0, H - 40, W, 40, fill=1, stroke=0)
    set_font("Helvetica-Bold", 11, C_LBLUE)
    c.drawString(36, H - 25, f"Market Agent V4.5  ·  {data['month_nm']}")
    set_font("Helvetica", 9, C_DIM)
    c.drawRightString(W - 36, H - 25, "Holdings · Signals · Picks")

    y = H - 58

    # ── HOLDINGS TABLE ────────────────────────────────────────
    y = section_header(y, f"🏦  Holdings ({len(data['holdings'])})")

    if data['holdings']:
        tbl_data = [["Ticker", "Name", "Qty", "Cost $", "Price $", "Value $", "P&L %", "P&L $", "→ Target", "→ Stop"]]
        for h in sorted(data['holdings'], key=lambda x: safe_float(x.get("pnl_pct", 0)), reverse=True):
            ticker  = h.get("ticker", "—")
            name    = (h.get("name", "") or "")[:16]
            qty     = safe_float(h.get("quantity", 0))
            cost    = safe_float(h.get("buy_price", 0))
            price   = safe_float(h.get("price", 0))
            val     = round(price * qty, 0)
            pnlp    = safe_float(h.get("pnl_pct", 0))
            pnlu    = safe_float(h.get("pnl_usd", 0))
            target  = safe_float(h.get("target", 0))
            stop    = safe_float(h.get("stop_loss", 0))
            to_tgt  = f"{sign(target-price)}{round(((target-price)/price)*100,1)}%" if target > 0 and price > 0 else "—"
            to_stop = f"{round(((stop-price)/price)*100,1)}%"                        if stop   > 0 and price > 0 else "—"
            tbl_data.append([
                ticker, name,
                f"{qty:.0f}",
                f"${cost:.2f}",
                f"${price:.2f}",
                f"${val:,.0f}",
                f"{sign(pnlp)}{pnlp:.1f}%",
                f"{sign(pnlu)}${abs(pnlu):,.0f}",
                to_tgt,
                to_stop
            ])

        col_widths = [38, 80, 26, 46, 46, 50, 40, 54, 46, 46]
        tbl = Table(tbl_data, colWidths=col_widths, repeatRows=1)

        # Build per-row P&L colouring
        style_cmds = [
            ("BACKGROUND",  (0, 0), (-1, 0),  C_BLUE),
            ("TEXTCOLOR",   (0, 0), (-1, 0),  colors.white),
            ("FONTNAME",    (0, 0), (-1, 0),  "Helvetica-Bold"),
            ("FONTSIZE",    (0, 0), (-1, -1), 7),
            ("FONTNAME",    (0, 1), (-1, -1), "Helvetica"),
            ("TEXTCOLOR",   (0, 1), (-1, -1), C_TEXT),
            ("BACKGROUND",  (0, 1), (-1, -1), C_CARD),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [C_CARD, _c("#0A1620")]),
            ("GRID",        (0, 0), (-1, -1), 0.3, C_BORDER),
            ("ALIGN",       (0, 0), (-1, -1), "CENTER"),
            ("ALIGN",       (1, 0), (1, -1),  "LEFT"),
            ("PADDING",     (0, 0), (-1, -1), 3),
            ("VALIGN",      (0, 0), (-1, -1), "MIDDLE"),
        ]
        # Colour P&L column
        for i, h in enumerate(data['holdings'], start=1):
            pnlp = safe_float(h.get("pnl_pct", 0))
            fg   = C_GREEN if pnlp >= 0 else C_RED
            style_cmds.append(("TEXTCOLOR", (6, i), (6, i), fg))
            style_cmds.append(("TEXTCOLOR", (7, i), (7, i), fg))

        tbl.setStyle(TableStyle(style_cmds))
        _, th = tbl.wrapOn(c, W - 72, 400)
        tbl.drawOn(c, 36, y - th)
        y = y - th - 18
    else:
        set_font("Helvetica", 9, C_DIM)
        c.drawString(36, y - 16, "No holdings data")
        y -= 24

    # ── SIGNAL ACCURACY ───────────────────────────────────────
    y = section_header(y, "🎯  Signal Performance")
    draw_rect(36, y - 44, W - 72, 38)

    metrics = [
        ("Open Signals",   str(data['open_ct']),   C_WHITE),
        ("Closed Signals", str(data['closed_ct']), C_WHITE),
        ("Hit Rate",       f"{data['acc_rate']}%" if data['acc_rate'] is not None else "N/A", C_GREEN if (data['acc_rate'] or 0) >= 50 else C_RED),
        ("Universe",       f"{int(data['universe_size'])} stocks" if data['universe_size'] else "—", C_WHITE),
        ("Picks Qualified",str(data['picks_count']), C_LBLUE),
        ("Gate Pass Rate", f"{round(data['picks_count']/data['universe_size']*100,1)}%" if data['universe_size'] else "—", C_WHITE),
    ]
    mx = (W - 72) / len(metrics)
    for i, (label, val, col_v) in enumerate(metrics):
        bx = 36 + i * mx
        set_font("Helvetica", 6, C_DIM)
        c.drawCentredString(bx + mx/2, y - 14, label.upper())
        set_font("Helvetica-Bold", 10, col_v)
        c.drawCentredString(bx + mx/2, y - 30, val)
    y -= 60

    # ── TOP PICKS ─────────────────────────────────────────────
    picks = data.get("picks", [])[:5]
    y = section_header(y, f"📌  Top Agent Picks ({len(data.get('picks', []))} qualified)")

    if picks:
        pick_data = [["Ticker", "Name", "Score", "Entry", "Target", "Action", "Thesis"]]
        for p in picks:
            thesis = (p.get("thesis") or p.get("reasoning") or "")[:45]
            pick_data.append([
                p.get("ticker", "—"),
                (p.get("name", "") or "")[:18],
                f"{safe_float(p.get('strategy_score', p.get('score', 0))):.0f}",
                p.get("suggested_entry", "—"),
                str(p.get("target_price", p.get("target", "—"))),
                p.get("action", "BUY"),
                thesis,
            ])
        pick_widths = [38, 90, 32, 54, 54, 36, 168]
        ptbl = Table(pick_data, colWidths=pick_widths, repeatRows=1)
        ptbl.setStyle(TableStyle([
            ("BACKGROUND",  (0, 0), (-1, 0),  C_BLUE),
            ("TEXTCOLOR",   (0, 0), (-1, 0),  colors.white),
            ("FONTNAME",    (0, 0), (-1, 0),  "Helvetica-Bold"),
            ("FONTSIZE",    (0, 0), (-1, -1), 7),
            ("FONTNAME",    (0, 1), (-1, -1), "Helvetica"),
            ("TEXTCOLOR",   (0, 1), (-1, -1), C_TEXT),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [C_CARD, _c("#0A1620")]),
            ("GRID",        (0, 0), (-1, -1), 0.3, C_BORDER),
            ("ALIGN",       (0, 0), (-1, -1), "CENTER"),
            ("ALIGN",       (6, 0), (6, -1),  "LEFT"),
            ("PADDING",     (0, 0), (-1, -1), 3),
            ("VALIGN",      (0, 0), (-1, -1), "MIDDLE"),
            ("TEXTCOLOR",   (5, 1), (5, -1),  C_GREEN),
        ]))
        _, ph = ptbl.wrapOn(c, W - 72, 300)
        ptbl.drawOn(c, 36, y - ph)
        y = y - ph - 18
    else:
        set_font("Helvetica", 9, C_DIM)
        c.drawString(36, y - 16, "No picks qualified this month")
        y -= 24

    # ── REALLOCATION SUMMARY ──────────────────────────────────
    if data.get('realloc_count', 0) > 0:
        y = section_header(y, "🔄  Reallocation Activity")
        draw_rect(36, y - 28, W - 72, 22)
        set_font("Helvetica", 8, C_TEXT)
        c.drawString(44, y - 16,
            f"{data['realloc_count']} reallocation recommendation(s) generated this month. "
            f"Review details in Telegram alerts or dashboard.")
        y -= 44

    # Footer P2
    set_font("Helvetica", 7, C_DIM)
    c.drawCentredString(W/2, 18, "Market Agent V4.5  ·  Not financial advice  ·  Page 2 of 3")
    c.setStrokeColor(C_BORDER)
    c.setLineWidth(0.5)
    c.line(36, 28, W - 36, 28)

    # ════════════════════════════════════════════════════════
    # PAGE 3 — INTELLIGENCE LAYER
    # Thesis health, outcome grading, adversarial critique,
    # correlation risk, gate distribution, macro trend — the
    # qualitative memory-driven data that page 1-2's numbers
    # don't capture on their own.
    # ════════════════════════════════════════════════════════
    c.showPage()
    c.setFillColor(C_BG)
    c.rect(0, 0, W, H, fill=1, stroke=0)

    c.setFillColor(C_CARD)
    c.rect(0, H - 40, W, 40, fill=1, stroke=0)
    set_font("Helvetica-Bold", 11, C_LBLUE)
    c.drawString(36, H - 25, f"Market Agent V4.5  ·  {data['month_nm']}")
    set_font("Helvetica", 9, C_DIM)
    c.drawRightString(W - 36, H - 25, "Intelligence Layer")

    y = H - 58

    # ── THESIS HEALTH ──────────────────────────────────────────
    th = data.get("thesis_health") or {}
    y = section_header(y, "🧠  Thesis Health")
    tally_items = [
        ("Intact",    th.get("intact", 0),    C_GREEN),
        ("Weakening", th.get("weakening", 0), C_AMBER),
        ("Broken",    th.get("broken", 0),    C_RED),
    ]
    tw3 = (W - 72) / 3
    for i, (label, val, vcol) in enumerate(tally_items):
        bx = 36 + i * tw3
        draw_rect(bx, y - 30, tw3 - 6, 26)
        set_font("Helvetica", 7, C_DIM)
        c.drawCentredString(bx + (tw3-6)/2, y - 12, label.upper())
        set_font("Helvetica-Bold", 13, vcol)
        c.drawCentredString(bx + (tw3-6)/2, y - 26, str(val))
    y -= 42

    changes = th.get("changes_this_month", [])
    if changes:
        set_font("Helvetica-Bold", 8, C_LBLUE)
        c.drawString(36, y, f"Status changes this month ({len(changes)}):")
        y -= 14
        for ch in changes[:5]:
            to_col = C_RED if ch["to"] == "BROKEN" else C_AMBER if ch["to"] == "WEAKENING" else C_GREEN
            set_font("Helvetica-Bold", 8, C_TEXT)
            c.drawString(40, y, f"{ch['ticker']}")
            set_font("Helvetica", 8, C_DIM)
            c.drawString(80, y, f"{ch['from']} →")
            set_font("Helvetica-Bold", 8, to_col)
            c.drawString(112, y, f"{ch['to']}")
            set_font("Helvetica", 7, C_DIM)
            c.drawString(195, y, (ch.get("reasoning") or "")[:82])
            y -= 12
        if len(changes) > 5:
            set_font("Helvetica-Oblique", 7, C_DIM)
            c.drawString(40, y, f"+ {len(changes)-5} more — see dashboard Delta tab")
            y -= 12
    else:
        set_font("Helvetica", 8, C_DIM)
        c.drawString(36, y, "No thesis status changes this month.")
        y -= 14
    y -= 12

    # ── OUTCOME TRACKING GRADES ────────────────────────────────
    og = data.get("outcome_grades") or {}
    y = section_header(y, "📊  Outcome Tracking — Self-Graded Post-Mortems")
    grade_items = [
        ("Correct",            og.get("correct", 0),   C_GREEN),
        ("Partially Correct",  og.get("partial", 0),   C_AMBER),
        ("Incorrect",          og.get("incorrect", 0), C_RED),
    ]
    for i, (label, val, vcol) in enumerate(grade_items):
        bx = 36 + i * tw3
        draw_rect(bx, y - 30, tw3 - 6, 26)
        set_font("Helvetica", 7, C_DIM)
        c.drawCentredString(bx + (tw3-6)/2, y - 12, label.upper())
        set_font("Helvetica-Bold", 13, vcol)
        c.drawCentredString(bx + (tw3-6)/2, y - 26, str(val))
    y -= 42

    graded = og.get("graded", [])
    if graded:
        set_font("Helvetica-Bold", 8, C_LBLUE)
        c.drawString(36, y, f"Checkpoints graded this month ({len(graded)}):")
        y -= 14
        for g in graded[:5]:
            gcol = C_GREEN if g["grade"]=="CORRECT" else C_AMBER if g["grade"]=="PARTIALLY_CORRECT" else C_RED
            set_font("Helvetica-Bold", 8, C_TEXT)
            c.drawString(40, y, f"{g['ticker']} ({g['horizon']})")
            set_font("Helvetica-Bold", 8, gcol)
            c.drawString(105, y, g["grade"].replace("_"," "))
            set_font("Helvetica", 7, C_DIM)
            c.drawString(195, y, (g.get("reasoning") or "")[:78])
            y -= 12
        if len(graded) > 5:
            set_font("Helvetica-Oblique", 7, C_DIM)
            c.drawString(40, y, f"+ {len(graded)-5} more — see logs/outcome_log.json")
            y -= 12
    else:
        set_font("Helvetica", 8, C_DIM)
        c.drawString(36, y, "No checkpoints reached their 30/60/90-day grading window this month.")
        y -= 14
    y -= 12

    # ── ADVERSARIAL CRITIQUE ───────────────────────────────────
    ac = data.get("adversarial") or {}
    ac_tally = ac.get("tally", {})
    if sum(ac_tally.values()) > 0:
        y = section_header(y, "⚖️  Adversarial Self-Critique — Bear Case Strength")
        ac_items = [
            ("Weak",     ac_tally.get("WEAK", 0),     C_GREEN),
            ("Moderate", ac_tally.get("MODERATE", 0), C_AMBER),
            ("Strong",   ac_tally.get("STRONG", 0),   C_RED),
        ]
        for i, (label, val, vcol) in enumerate(ac_items):
            bx = 36 + i * tw3
            draw_rect(bx, y - 30, tw3 - 6, 26)
            set_font("Helvetica", 7, C_DIM)
            c.drawCentredString(bx + (tw3-6)/2, y - 12, label.upper())
            set_font("Helvetica-Bold", 13, vcol)
            c.drawCentredString(bx + (tw3-6)/2, y - 26, str(val))
        y -= 42

        strong_flags = ac.get("strong_flags", {})
        if strong_flags:
            set_font("Helvetica-Bold", 8, C_LBLUE)
            c.drawString(36, y, f"Strong bear cases flagged this month ({len(strong_flags)} ticker(s)):")
            y -= 14
            for ticker, info in list(strong_flags.items())[:5]:
                set_font("Helvetica-Bold", 8, C_RED)
                c.drawString(40, y, ticker)
                set_font("Helvetica", 7, C_DIM)
                c.drawString(80, y, (info.get("argument") or "")[:100])
                y -= 12
        y -= 8

    # ── CORRELATION RISK ───────────────────────────────────────
    corr_pairs = data.get("correlation_pairs") or []
    y = section_header(y, "🔗  Correlation Risk (current holdings)")
    if corr_pairs:
        set_font("Helvetica", 8, C_TEXT)
        c.drawString(36, y, f"{len(corr_pairs)} holding pair(s) at or above 0.7 daily-return correlation:")
        y -= 14
        for p in corr_pairs[:5]:
            set_font("Helvetica-Bold", 8, C_AMBER)
            c.drawString(40, y, f"{p['a']} <-> {p['b']}")
            set_font("Helvetica", 8, C_TEXT)
            c.drawString(150, y, f"{p['correlation']:+.2f}")
            y -= 12
    else:
        set_font("Helvetica", 8, C_DIM)
        c.drawString(36, y, "No holding pairs at or above 0.7 correlation — genuinely diversified.")
        y -= 14
    y -= 12

    # ── GATE DISTRIBUTION ───────────────────────────────────────
    gd = data.get("gate_distribution") or {}
    y = section_header(y, "🎯  Pick Gate Distribution (this month)")
    draw_rect(36, y - 28, W - 72, 22)
    set_font("Helvetica", 8, C_TEXT)
    c.drawString(44, y - 16,
        f"✅ {gd.get('green',0)} passed all 7 gates    🔷 {gd.get('blue',0)} passed Gates 1-6 (Gate 7 caveat)")
    y -= 44

    # ── MACRO TREND ─────────────────────────────────────────────
    mt = data.get("macro_trend")
    if mt:
        y = section_header(y, "🌐  Macro Trend This Month")
        first, last = mt.get("first", {}), mt.get("last", {})
        macro_rows = [
            ("VIX", first.get("vix",{}).get("value"), last.get("vix",{}).get("value")),
            ("10Y Yield", first.get("yield_10y",{}).get("value"), last.get("yield_10y",{}).get("value")),
            ("DXY", first.get("dxy",{}).get("value"), last.get("dxy",{}).get("value")),
        ]
        set_font("Helvetica", 7, C_DIM)
        c.drawString(36, y, f"{mt.get('first_date','—')}  →  {mt.get('last_date','—')}")
        y -= 14
        for label, fv, lv in macro_rows:
            if fv is None or lv is None:
                continue
            set_font("Helvetica-Bold", 8, C_TEXT)
            c.drawString(40, y, label)
            set_font("Helvetica", 8, C_DIM)
            c.drawString(120, y, f"{fv}")
            c.drawString(160, y, "→")
            chg_col = C_GREEN if lv >= fv else C_RED
            set_font("Helvetica-Bold", 8, chg_col)
            c.drawString(180, y, f"{lv}")
            y -= 12
        y -= 8

    # Footer P3
    set_font("Helvetica", 7, C_DIM)
    c.drawCentredString(W/2, 18, "Market Agent V4.5  ·  Not financial advice  ·  Page 3 of 3")
    c.setStrokeColor(C_BORDER)
    c.setLineWidth(0.5)
    c.line(36, 28, W - 36, 28)

    c.save()
    print(f"{GREEN}✓ PDF saved: {output_path}{RESET}")


def send_pdf_telegram(pdf_path, caption):
    """Send PDF as document via Telegram sendDocument"""
    token   = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(f"{YELLOW}⚠ Telegram not configured{RESET}")
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendDocument"
        with open(pdf_path, "rb") as f:
            resp = requests.post(url, data={
                "chat_id":    chat_id,
                "caption":    caption,
                "parse_mode": "HTML",
            }, files={"document": f}, timeout=30)
        ok = resp.status_code == 200
        if not ok:
            print(f"{YELLOW}⚠ Telegram sendDocument failed: {resp.text}{RESET}")
        return ok
    except Exception as e:
        print(f"{YELLOW}⚠ Telegram PDF error: {e}{RESET}")
        return False


def run():
    print(f"\n{BOLD}{BLUE}{'='*55}")
    print(f"  MARKET AGENT — Monthly Performance Report (PDF)")
    print(f"  {datetime.now().strftime('%B %Y')}")
    print(f"{'='*55}{RESET}\n")

    # Always record today's snapshot first
    record_snapshot()

    # Load data sources
    history  = load_json(HISTORY_FILE) or []
    latest   = load_json(LATEST_FILE)  or {}
    perf_log = load_json(PERF_LOG)     or []
    peak     = load_json(PEAK_FILE)    or {}
    thesis_log  = load_json(THESIS_LOG)  or {}
    outcome_log = load_json(OUTCOME_LOG) or {}
    corr_matrix = (load_json(CORR_MATRIX) or {}).get("matrix", {})

    now      = datetime.now()
    month_nm = now.strftime("%B %Y")

    if not history:
        token   = os.getenv("TELEGRAM_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if token and chat_id:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id,
                      "text": f"📊 <b>Monthly Report — {month_nm}</b>\n\nNo historical data yet. PDF report will generate next month.",
                      "parse_mode": "HTML"},
                timeout=10
            )
        return

    # ── COMPUTE ALL METRICS ───────────────────────────────────
    current   = history[-1]
    curr_cap  = safe_float(current.get("total_capital", 0))
    curr_pnl  = safe_float(current.get("pnl_usd", 0))
    curr_pnlp = safe_float(current.get("pnl_pct", 0))
    cash      = safe_float(current.get("cash", 0))
    cash_pct  = round((cash / curr_cap * 100), 1) if curr_cap > 0 else 0

    def period_change(days):
        cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        snap   = next((h for h in reversed(history) if h["date"] <= cutoff), None)
        if not snap:
            return 0, 0
        prev    = safe_float(snap.get("total_capital", curr_cap))
        chg_usd = round(curr_cap - prev, 2)
        chg_pct = round((chg_usd / prev) * 100, 2) if prev > 0 else 0
        return chg_usd, chg_pct

    month_chg_usd, month_chg_pct = period_change(30)
    q_chg_usd,     q_chg_pct     = period_change(90)
    h6_chg_usd,    h6_chg_pct    = period_change(180)

    first_snap     = history[0]
    inception_cap  = safe_float(first_snap.get("total_capital", curr_cap))
    inception_date = first_snap.get("date", "—")
    inception_chg  = round(curr_cap - inception_cap, 2)
    inception_pct  = round((inception_chg / inception_cap) * 100, 2) if inception_cap > 0 else 0

    spy_month = get_spy_return(30)
    spy_q     = get_spy_return(90)
    spy_6m    = get_spy_return(180)

    peak_val  = safe_float(peak.get("peak_value", curr_cap))
    peak_date = peak.get("peak_date", "—")
    drawdown  = safe_float(peak.get("drawdown_pct", 0))
    dd_mode   = peak.get("mode", "NORMAL")

    acc_rate, closed_ct, open_ct = calc_signal_accuracy(perf_log)

    holdings      = latest.get("holdings", [])
    picks         = latest.get("agent_picks", [])
    universe_size = safe_float(latest.get("universe_size", 0))
    picks_count   = len(picks)

    one_month_ago = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    history_30    = [h for h in history if h.get("date", "") >= one_month_ago]
    realloc_count = len(latest.get("reallocation", []))

    # ── INTELLIGENCE LAYER ────────────────────────────────
    # This report runs on the 1st, so "this month" means the calendar
    # month containing YESTERDAY — the month that just ended — not the
    # brand-new month that just started an hour ago. If run manually
    # mid-month for testing, this just resolves to the current month.
    import calendar
    report_ref = now - timedelta(days=1)
    report_year, report_month = report_ref.year, report_ref.month
    month_start_str = f"{report_year}-{report_month:02d}-01"
    last_day = calendar.monthrange(report_year, report_month)[1]
    month_end_str = f"{report_year}-{report_month:02d}-{last_day:02d}"

    month_snapshots  = load_month_snapshots(report_year, report_month)
    thesis_health    = summarize_thesis_health(thesis_log, month_start_str, month_end_str)
    outcome_grades   = summarize_outcome_grades(outcome_log, month_start_str, month_end_str)
    adversarial      = summarize_adversarial_critique(month_snapshots)
    correlation_pairs= summarize_correlation(corr_matrix)
    gate_distribution= summarize_gate_distribution(month_snapshots)
    macro_trend      = summarize_macro_trend(month_snapshots)

    # ── BUILD PDF ─────────────────────────────────────────────
    pdf_dir  = os.path.join(BASE_DIR, "logs", "reports")
    os.makedirs(pdf_dir, exist_ok=True)
    pdf_path = os.path.join(pdf_dir, f"market_agent_{now.strftime('%Y_%m')}.pdf")

    pdf_data = dict(
        month_nm=month_nm, curr_cap=curr_cap, curr_pnl=curr_pnl,
        curr_pnlp=curr_pnlp, cash=cash, cash_pct=cash_pct,
        month_chg_usd=month_chg_usd, month_chg_pct=month_chg_pct,
        q_chg_usd=q_chg_usd, q_chg_pct=q_chg_pct,
        h6_chg_usd=h6_chg_usd, h6_chg_pct=h6_chg_pct,
        inception_pct=inception_pct, inception_chg=inception_chg,
        inception_date=inception_date, spy_month=spy_month,
        spy_q=spy_q, spy_6m=spy_6m, peak_val=peak_val,
        peak_date=peak_date, drawdown=drawdown, dd_mode=dd_mode,
        acc_rate=acc_rate, closed_ct=closed_ct, open_ct=open_ct,
        holdings=holdings, picks=picks, history_30=history_30,
        universe_size=universe_size, picks_count=picks_count,
        realloc_count=realloc_count,
        thesis_health=thesis_health, outcome_grades=outcome_grades,
        adversarial=adversarial, correlation_pairs=correlation_pairs,
        gate_distribution=gate_distribution, macro_trend=macro_trend,
    )

    try:
        build_pdf(pdf_data, pdf_path)
    except Exception as e:
        print(f"{RED}✗ PDF generation failed: {e}{RESET}")
        import traceback; traceback.print_exc()
        return

    # ── SEND PDF VIA TELEGRAM ─────────────────────────────────
    sign = lambda x: "+" if x >= 0 else ""

    thesis_line = ""
    if thesis_health.get("weakening", 0) or thesis_health.get("broken", 0):
        thesis_line = (f"\n🧠 Thesis: {thesis_health.get('intact',0)} intact, "
                        f"{thesis_health.get('weakening',0)} weakening, "
                        f"{thesis_health.get('broken',0)} broken")

    outcome_total = outcome_grades.get("correct",0) + outcome_grades.get("partial",0) + outcome_grades.get("incorrect",0)
    outcome_line = ""
    if outcome_total > 0:
        outcome_line = (f"\n📊 Post-mortems graded: {outcome_grades.get('correct',0)} correct, "
                         f"{outcome_grades.get('partial',0)} partial, "
                         f"{outcome_grades.get('incorrect',0)} incorrect")

    strong_count = len(adversarial.get("strong_flags", {}))
    bear_line = f"\n⚖️ {strong_count} strong bear case(s) flagged this month" if strong_count else ""

    caption = (
        f"📊 <b>Monthly Report — {month_nm}</b>\n\n"
        f"Portfolio: <b>${curr_cap:,.0f}</b>  "
        f"({sign(curr_pnlp)}{curr_pnlp:.2f}% total P&L)\n"
        f"1M vs S&P: {sign(month_chg_pct)}{month_chg_pct:.2f}% vs "
        f"{(sign(spy_month)+str(spy_month)+'%') if spy_month is not None else '—'}"
        f"{thesis_line}{outcome_line}{bear_line}\n\n"
        f"Full breakdown — including thesis health, outcome grading, "
        f"adversarial critique, and correlation risk — in the attached PDF."
    )

    success = send_pdf_telegram(pdf_path, caption)
    if success:
        print(f"\n{GREEN}✓ Monthly PDF report sent to Telegram{RESET}")
    else:
        print(f"\n{RED}✗ Monthly PDF failed to send{RESET}")


if __name__ == "__main__":
    run()
