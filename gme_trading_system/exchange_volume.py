"""Per-venue daily exchange-volume breakdown via Polygon.io.

Pulls trade-level rows from https://api.polygon.io/v3/trades/{ticker} for a
given date and aggregates them by venue:

    DARK POOL    1,153,053  $25.37M   9,280  55.77%
    NYSE           223,089   $4.90M   1,749  10.79%
    NASDAQ         170,486   $3.75M   2,577   8.24%
    EDGX           130,796   $2.87M   1,047   6.32%
    ...

"DARK POOL" is the sum of FINRA TRF / ADF prints (off-exchange trades reported
to FINRA — what people colloquially mean by "dark pool" volume). Percent of
real volume is share-count over the sum of LIT (non-TRF) venues, matching the
convention on redstripedtie.com/_/{ticker}?view=volume.

Wiring:
  - update_for_ticker("GME") → backfill last N trading days into agent_memory.db.
  - get_exchange_volume_summary("GME") → refreshes a small window first, then
    returns a dict with venue rows + totals. Consumed by the CTO DV burst.
  - format_brief_line / format_full_table → one-liner for the burst and a
    mono-spaced 15-row table for the /exchange Telegram command.

Polygon API: Stocks Starter ($29/mo) — unlimited calls, full historical trades
with `exchange` (MIC id) and `trf_id` fields. Soft-fails to None when
POLYGON_API_KEY is unset so the rest of the burst keeps running.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import date, timedelta

import requests

from circuit_breaker import get_breaker

log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS exchange_volume (
    date         TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    venue        TEXT NOT NULL,
    shares       INTEGER NOT NULL,
    notional_usd REAL    NOT NULL,
    trades       INTEGER NOT NULL,
    fetched_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (date, ticker, venue)
);
CREATE INDEX IF NOT EXISTS idx_exvol_ticker_date ON exchange_volume(ticker, date);
"""

_POLYGON_TRADES_URL = "https://api.polygon.io/v3/trades/{ticker}"

# Polygon `exchange` field → human-readable venue. TRF/ADF carriers are folded
# into a single "DARK POOL" bucket. Unmapped ids fall through to f"EXCH_{id}"
# (matches the raw-id display redstripedtie uses for venues it doesn't label).
_VENUE_MAP: dict[int, str] = {
    1:  "NYSE AMEX",
    2:  "XBOS",          # NASDAQ OMX BX
    3:  "XCIS",          # NYSE National
    4:  "DARK POOL",     # FINRA NASDAQ TRF Carteret
    5:  "EPRL",          # MIAX Pearl Equities
    6:  "IEX",
    7:  "DARK POOL",     # FINRA NYSE TRF Chicago
    8:  "EDGA",          # Cboe EDGA
    9:  "EDGX",          # Cboe EDGX
    10: "NYSE CHICAGO",
    11: "NYSE",
    12: "NYSE ARCA",
    13: "NASDAQ",
    15: "BATS",          # Cboe BZX
    16: "MEMX",
    19: "NASDAQ PHILLY", # NASDAQ PSX
    21: "DARK POOL",     # FINRA ADF
    36: "DARK POOL",     # FINRA TRF (legacy id)
}

_DARK_POOL = "DARK POOL"


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _venue_for(exchange_id: int) -> str:
    return _VENUE_MAP.get(exchange_id, f"EXCH_{exchange_id}")


def _fetch_trades_page(url: str, params: dict | None = None) -> dict | None:
    """One paged GET to Polygon. Returns parsed JSON or None on any failure.
    Wrapped in the polygon circuit breaker so an outage doesn't keep
    hammering the cron."""
    breaker = get_breaker("polygon")
    try:
        r = breaker.call(requests.get, url, params=params, timeout=20,
                         headers={"User-Agent": "GMETradingSystem/1.0"})
    except Exception as e:
        log.debug(f"[polygon] fetch error: {e}")
        return None
    if r.status_code != 200:
        log.debug(f"[polygon] HTTP {r.status_code} for {url}")
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    if not isinstance(data, dict) or "results" not in data:
        return None
    return data


def _iter_trades_for_day(ticker: str, day: date, api_key: str):
    """Yield raw trade dicts for the ticker on `day`, paginating Polygon's
    `next_url`. Each Polygon page caps at 50,000 trades; GME on a heavy day
    is typically 50k-300k, so 1-6 pages."""
    base = _POLYGON_TRADES_URL.format(ticker=ticker.upper())
    params = {
        "timestamp": day.isoformat(),
        "order": "asc",
        "limit": 50000,
        "apiKey": api_key,
    }
    url = base
    while True:
        data = _fetch_trades_page(url, params)
        if data is None:
            return
        for tr in data.get("results", []) or []:
            yield tr
        next_url = data.get("next_url")
        if not next_url:
            return
        # next_url already includes cursor/timestamp; append apiKey since
        # Polygon strips it from the echoed URL.
        sep = "&" if "?" in next_url else "?"
        url = f"{next_url}{sep}apiKey={api_key}"
        params = None  # next_url carries the query string verbatim


def _aggregate(trades) -> dict[str, list]:
    """Fold a trade iterable into {venue: [shares, notional_usd, trades]}.
    Defensive: silently skips rows missing price/size/exchange."""
    agg: dict[str, list] = {}
    for tr in trades:
        try:
            size = int(tr.get("size") or 0)
            price = float(tr.get("price") or 0.0)
            ex = int(tr.get("exchange"))
        except (TypeError, ValueError):
            continue
        if size <= 0 or price <= 0:
            continue
        venue = _venue_for(ex)
        row = agg.setdefault(venue, [0, 0.0, 0])
        row[0] += size
        row[1] += price * size
        row[2] += 1
    return agg


def _has_rows_for(conn: sqlite3.Connection, ticker: str, day: date) -> bool:
    return conn.execute(
        "SELECT 1 FROM exchange_volume WHERE date=? AND ticker=? LIMIT 1",
        (day.isoformat(), ticker.upper()),
    ).fetchone() is not None


def update_for_ticker(ticker: str, days_back: int = 5,
                      db_path: str = DB_PATH) -> int:
    """Backfill missing days for `ticker` over the last `days_back` calendar
    days. Returns count of venue-rows inserted. Idempotent — PRIMARY KEY
    (date, ticker, venue) plus an explicit cache-hit check skip the fetch
    when ANY row already exists for (date, ticker). Weekends are skipped
    without an HTTP call. Soft-fails to 0 when POLYGON_API_KEY is unset."""
    api_key = os.environ.get("POLYGON_API_KEY", "").strip()
    if not api_key:
        log.debug("[polygon] POLYGON_API_KEY unset — skipping exchange-volume backfill")
        return 0

    inserted = 0
    today = date.today()
    target = ticker.upper()
    conn = sqlite3.connect(db_path)
    try:
        _ensure_schema(conn)
        for offset in range(1, days_back + 1):
            d = today - timedelta(days=offset)
            if d.weekday() >= 5:
                continue
            if _has_rows_for(conn, target, d):
                continue
            agg = _aggregate(_iter_trades_for_day(target, d, api_key))
            if not agg:
                continue
            for venue, (shares, notional, trades) in agg.items():
                try:
                    conn.execute(
                        "INSERT INTO exchange_volume "
                        "(date, ticker, venue, shares, notional_usd, trades) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (d.isoformat(), target, venue, shares, notional, trades),
                    )
                    inserted += 1
                except sqlite3.IntegrityError:
                    pass
            conn.commit()
    finally:
        conn.close()
    if inserted:
        log.info(f"[polygon] cached {inserted} exchange-volume rows for {target}")
    return inserted


def get_exchange_volume_summary(ticker: str,
                                 db_path: str = DB_PATH) -> dict | None:
    """Return the most-recent cached day's per-venue breakdown for `ticker`.

    Refreshes the last 5 days first (cheap on a hot cache), then returns:
        {
          'date': '2026-05-13',
          'venues': [{venue, shares, notional_usd, trades, pct_of_real}, ...],
          'total_real_shares': 2067506,   # sum of LIT venues (excludes DARK POOL)
          'total_notional': 45561234.50,  # sum across ALL venues incl. DARK POOL
          'total_trades': 19483,          # sum across ALL venues incl. DARK POOL
          'dark_pool_pct': 55.77,         # DARK POOL shares / (DARK POOL + LIT)
        }

    DARK POOL is forced first in `venues`; remaining venues sorted desc by
    shares. `pct_of_real` for lit venues is shares / total_real_shares * 100.
    For DARK POOL, `pct_of_real` is shares / (dark + real) * 100 — matches
    the redstripedtie convention where the percentages sum to 100% across
    the table (lit + dark together).

    Returns None when no data is available (fresh install + Polygon down,
    or POLYGON_API_KEY missing).
    """
    update_for_ticker(ticker, days_back=5, db_path=db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_schema(conn)
        latest = conn.execute(
            "SELECT MAX(date) AS d FROM exchange_volume WHERE ticker=?",
            (ticker.upper(),),
        ).fetchone()
        if not latest or not latest["d"]:
            return None
        latest_date = latest["d"]
        rows = conn.execute(
            "SELECT venue, shares, notional_usd, trades "
            "FROM exchange_volume "
            "WHERE ticker=? AND date=?",
            (ticker.upper(), latest_date),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None

    dark_shares = sum(r["shares"] for r in rows if r["venue"] == _DARK_POOL)
    lit_shares = sum(r["shares"] for r in rows if r["venue"] != _DARK_POOL)
    total_shares = dark_shares + lit_shares
    total_notional = sum(r["notional_usd"] for r in rows)
    total_trades = sum(r["trades"] for r in rows)

    venues = []
    for r in rows:
        pct = (r["shares"] / total_shares * 100.0) if total_shares else 0.0
        venues.append({
            "venue":        r["venue"],
            "shares":       int(r["shares"]),
            "notional_usd": float(r["notional_usd"]),
            "trades":       int(r["trades"]),
            "pct_of_real":  pct,
        })

    # DARK POOL forced first; rest desc by shares.
    venues.sort(key=lambda v: (0 if v["venue"] == _DARK_POOL else 1, -v["shares"]))

    dark_pool_pct = (dark_shares / total_shares * 100.0) if total_shares else 0.0
    return {
        "date":              latest_date,
        "venues":            venues,
        "total_real_shares": int(lit_shares),
        "total_notional":    float(total_notional),
        "total_trades":      int(total_trades),
        "dark_pool_pct":     float(dark_pool_pct),
    }


def _short_name(venue: str) -> str:
    """Compact venue label for the one-line brief."""
    return {
        "DARK POOL":     "DARK",
        "NASDAQ":        "NDAQ",
        "NYSE ARCA":     "ARCA",
        "NYSE AMEX":     "AMEX",
        "NYSE CHICAGO":  "CHI",
        "NASDAQ PHILLY": "PSX",
    }.get(venue, venue)


def format_brief_line(summary: dict, ticker: str = "") -> str:
    """One-line summary suitable for the CTO DV burst:
        GME Venue Mix: DARK 55.8% · NYSE 10.8% · NDAQ 8.2% (top-3 of 15, 2026-05-13)

    When `ticker` is provided, the line is prefixed so multi-ticker bursts
    stay unambiguous.
    """
    venues = summary["venues"]
    top3 = venues[:3]
    parts = " · ".join(
        f"{_short_name(v['venue'])} {v['pct_of_real']:.1f}%" for v in top3
    )
    prefix = f"{ticker.upper()} " if ticker else ""
    return (
        f"{prefix}Venue Mix: {parts} "
        f"(top-3 of {len(venues)}, {summary['date']})"
    )


def _fmt_shares(n: int) -> str:
    return f"{n:>10,}"


def _fmt_notional(usd: float) -> str:
    if usd >= 1e9:
        return f"${usd/1e9:>5.2f}b"
    if usd >= 1e6:
        return f"${usd/1e6:>5.2f}m"
    if usd >= 1e3:
        return f"${usd/1e3:>5.1f}k"
    return f"${usd:>6.0f}"


def format_full_table(summary: dict) -> str:
    """Mono-spaced 15-row breakdown for /exchange. Wrap in <pre>…</pre>
    when sending over Telegram HTML."""
    header = f"Exchange Volume — {summary['date']}"
    cols = f"{'Exchange':<14} {'Pct':>6} {'Shares':>10} {'Notional':>8} {'Trades':>7}"
    sep = "-" * len(cols)
    lines = [header, "", cols, sep]
    for v in summary["venues"]:
        lines.append(
            f"{v['venue']:<14} "
            f"{v['pct_of_real']:>5.2f}% "
            f"{_fmt_shares(v['shares'])} "
            f"{_fmt_notional(v['notional_usd']):>8} "
            f"{v['trades']:>7,}"
        )
    lines.append(sep)
    lines.append(
        f"Dark pool: {summary['dark_pool_pct']:.1f}% · "
        f"Real volume: {summary['total_real_shares']:,} shares · "
        f"Trades: {summary['total_trades']:,}"
    )
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import sys
    t = sys.argv[1] if len(sys.argv) > 1 else "GME"
    s = get_exchange_volume_summary(t)
    if s is None:
        print(f"no data for {t} (POLYGON_API_KEY set?)")
    else:
        print(format_brief_line(s))
        print()
        print(format_full_table(s))
