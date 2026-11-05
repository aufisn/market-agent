#!/usr/bin/env python3
"""
L7: market calendar without hand-maintained lists.

NYSE holidays are computed from NYSE's published rules (Rule 7.2), so they
never run out. One-off closures (national days of mourning, weather) can't be
predicted and go in SPECIAL_CLOSURES.

FOMC decision dates are read from the Federal Reserve's calendar page and
cached in data/fomc_calendar.json for a week. If the page can't be read the
cache is used; if there's no cache, market_context falls back to its hand list.
CPI and jobs dates stay hand-maintained: bls.gov refuses automated downloads.

CLI:
  python3 market_calendar.py --is-trading-day [YYYY-MM-DD]   exit 0 open, 1 closed
  python3 market_calendar.py --holidays 2027
  python3 market_calendar.py --refresh-fomc
"""
import json
import os
import re
import sys
from datetime import date, datetime, timedelta

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
FOMC_URL  = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FOMC_PATH = os.path.join(BASE_DIR, "data", "fomc_calendar.json")
FOMC_MAX_AGE_DAYS = 7

# Unscheduled full-day closures. Add new ones as NYSE announces them.
SPECIAL_CLOSURES = {
    "2025-01-09": "National Day of Mourning (President Carter)",
}


def _easter(year):
    """Gregorian Easter Sunday (anonymous algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year, month, weekday, n):
    """n-th given weekday (Mon=0) of a month; n=-1 for the last one."""
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d):
    """Saturday holidays move to Friday, Sunday ones to Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def nyse_holidays(year):
    """{iso_date: name} of full-day NYSE closures in a year."""
    h = {}
    ny = date(year, 1, 1)
    if ny.weekday() != 5:          # NYSE doesn't close the Friday before a Saturday New Year
        h[_observed(ny)] = "New Year's Day"
    h[_nth_weekday(year, 1, 0, 3)] = "Martin Luther King Jr. Day"
    h[_nth_weekday(year, 2, 0, 3)] = "Presidents' Day"
    h[_easter(year) - timedelta(days=2)] = "Good Friday"
    h[_nth_weekday(year, 5, 0, -1)] = "Memorial Day"
    if year >= 2022:
        h[_observed(date(year, 6, 19))] = "Juneteenth"
    h[_observed(date(year, 7, 4))] = "Independence Day"
    h[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    h[_nth_weekday(year, 11, 3, 4)] = "Thanksgiving"
    h[_observed(date(year, 12, 25))] = "Christmas"
    out = {d.isoformat(): n for d, n in h.items() if d.year == year}
    out.update({k: v for k, v in SPECIAL_CLOSURES.items() if k.startswith(str(year))})
    return dict(sorted(out.items()))


def holiday_list(start_year, end_year):
    out = []
    for y in range(start_year, end_year + 1):
        out += list(nyse_holidays(y))
    return out


def is_trading_day(d=None):
    d = d or date.today()
    if isinstance(d, str):
        d = datetime.strptime(d, "%Y-%m-%d").date()
    return d.weekday() < 5 and d.isoformat() not in nyse_holidays(d.year)


def trading_days_between(earlier, later):
    """Trading days from earlier (exclusive) to later (inclusive)."""
    d, n = earlier, 0
    while d < later:
        d += timedelta(days=1)
        if is_trading_day(d):
            n += 1
    return n


# ── FOMC ──────────────────────────────────────────────────────
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_fomc_page(html):
    """Decision dates (second meeting day) per year from the Fed's page.
    Unscheduled meetings and notation votes are skipped."""
    out = {}
    panels = re.split(r'<div class="panel-heading">', html)
    for p in panels:
        m = re.search(r">(\d{4}) FOMC Meetings<", p)
        if not m:
            continue
        year = int(m.group(1))
        months = re.findall(r'fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>', p)
        days   = re.findall(r'fomc-meeting__date[^>]*>([^<]+)<', p)
        dates = []
        for mon, day in zip(months, days):
            txt = day.strip().lower()
            if "unscheduled" in txt or "notation" in txt:
                continue
            dm = re.match(r"(\d{1,2})(?:-(\d{1,2}))?", txt)
            mn = mon.strip().lower().split("/")[-1][:3]   # "Jan/Feb" -> feb, "March" -> mar
            if not dm or mn not in _MONTHS:
                continue
            last_day = int(dm.group(2) or dm.group(1))
            try:
                dates.append(date(year, _MONTHS[mn], last_day).isoformat())
            except ValueError:
                continue
        if dates:
            out[str(year)] = sorted(set(dates))
    return out


def refresh_fomc(force=False):
    """Returns {"fetched_at", "source", "years": {yyyy: [iso dates]}} or None."""
    cached = None
    try:
        with open(FOMC_PATH) as f:
            cached = json.load(f)
        age = datetime.now() - datetime.fromisoformat(cached["fetched_at"])
        if not force and age < timedelta(days=FOMC_MAX_AGE_DAYS):
            return cached
    except Exception:
        pass
    try:
        import requests
        r = requests.get(FOMC_URL, timeout=15, headers={"User-Agent": "market-agent/4.5"})
        r.raise_for_status()
        years = parse_fomc_page(r.text)
        if not years:
            raise ValueError("no meetings found — page layout may have changed")
        data = {"fetched_at": datetime.now().isoformat(timespec="seconds"),
                "source": FOMC_URL, "years": years}
        os.makedirs(os.path.dirname(FOMC_PATH), exist_ok=True)
        tmp = FOMC_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, FOMC_PATH)
        return data
    except Exception as e:
        print(f"⚠ FOMC calendar refresh failed ({e}) — using {'cached copy' if cached else 'hand list'}")
        return cached


def fomc_dates():
    data = refresh_fomc()
    if not data:
        return []
    return sorted(d for ds in data.get("years", {}).values() for d in ds)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--is-trading-day"]:
        sys.exit(0 if is_trading_day(args[1] if len(args) > 1 else None) else 1)
    if args[:1] == ["--holidays"]:
        y = int(args[1]) if len(args) > 1 else date.today().year
        for d, n in nyse_holidays(y).items():
            print(d, n)
        sys.exit(0)
    if args[:1] == ["--refresh-fomc"]:
        d = refresh_fomc(force=True)
        print(json.dumps(d, indent=2) if d else "refresh failed")
        sys.exit(0 if d else 1)
    print(__doc__)
