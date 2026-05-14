"""FINRA Reg SHO daily short-volume intel.

Pulls the consolidated NMS short-volume CSV that FINRA publishes daily
(https://cdn.finra.org/equity/regsho/daily/CNMSshvol{YYYYMMDD}.txt) and
caches per-ticker (date, short_volume, total_volume, short_pct) in
agent_memory.db. The summary helper returns latest + 30-day average so the
CTO DV burst can show whether short pressure is elevating vs. baseline.

Two-tier semantics are intentional:
  - get_short_vol_summary("GME") → fetches any missing days first (cheap on a
    hot cache), then computes the rolling 30-day average from cached rows.
  - update_for_ticker("GME", days_back=35) → standalone backfill, used by the
    daily cron (CTO DV at 09:10 ET) so the cache stays warm.

FINRA publishes the prior trading day's file mid-morning ET, so the 09:10 ET
DV burst gets yesterday's number reliably. Weekends/holidays return 404 and
are skipped silently.
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
CREATE TABLE IF NOT EXISTS finra_short_vol (
    date          TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    short_volume  INTEGER NOT NULL,
    total_volume  INTEGER NOT NULL,
    short_pct     REAL NOT NULL,
    fetched_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (date, ticker)
);
CREATE INDEX IF NOT EXISTS idx_finra_sv_ticker_date ON finra_short_vol(ticker, date);
"""

_FINRA_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{yyyymmdd}.txt"


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _fetch_one_day(yyyymmdd: str) -> str | None:
    """Return the raw text of the FINRA daily file for the given YYYYMMDD,
    or None on 404 / non-200 / circuit-open / wrong-shape response.
    Wrapped in a circuit breaker so a long FINRA outage doesn't keep
    hammering the cron."""
    breaker = get_breaker("finra")
    url = _FINRA_URL.format(yyyymmdd=yyyymmdd)
    try:
        r = breaker.call(requests.get, url, timeout=15,
                          headers={"User-Agent": "GMETradingSystem/1.0"})
    except Exception as e:
        log.debug(f"[finra] {yyyymmdd} fetch error: {e}")
        return None
    if r.status_code != 200:
        return None
    # Defensive: real files start with the header line. Anything else
    # (404 HTML, S3 XML error, rate-limit page) → reject.
    if not r.text.startswith("Date|Symbol|"):
        return None
    return r.text


def _parse_for_ticker(text: str, ticker: str) -> tuple[int, int] | None:
    """Return (short_volume, total_volume) for the ticker on this day, or
    None if not present. Volumes are floats in the source — coerce to int
    for storage (sub-share fractions are noise)."""
    target = ticker.upper()
    for line in text.splitlines()[1:]:  # skip header
        parts = line.split("|")
        if len(parts) >= 5 and parts[1] == target:
            try:
                short = int(float(parts[2]))
                total = int(float(parts[4]))
            except ValueError:
                return None
            if total <= 0:
                return None
            return short, total
    return None


def update_for_ticker(ticker: str, days_back: int = 30,
                       db_path: str = DB_PATH) -> int:
    """Backfill missing rows for the last `days_back` calendar days. Returns
    the count of newly inserted rows. Idempotent — UNIQUE(date, ticker)
    plus an explicit cache-hit check skip the fetch when a row already
    exists. Weekends are skipped without an HTTP call."""
    inserted = 0
    today = date.today()
    target = ticker.upper()
    conn = sqlite3.connect(db_path)
    try:
        _ensure_schema(conn)
        for offset in range(1, days_back + 1):
            d = today - timedelta(days=offset)
            if d.weekday() >= 5:  # 5=Sat, 6=Sun
                continue
            date_str = d.isoformat()
            if conn.execute(
                "SELECT 1 FROM finra_short_vol WHERE date=? AND ticker=?",
                (date_str, target),
            ).fetchone():
                continue
            text = _fetch_one_day(d.strftime("%Y%m%d"))
            if text is None:
                continue
            parsed = _parse_for_ticker(text, target)
            if parsed is None:
                continue
            short, total = parsed
            pct = short / total
            try:
                conn.execute(
                    "INSERT INTO finra_short_vol "
                    "(date, ticker, short_volume, total_volume, short_pct) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (date_str, target, short, total, pct),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                pass  # raced with another writer
        conn.commit()
    finally:
        conn.close()
    if inserted:
        log.info(f"[finra] cached {inserted} new short-vol rows for {target}")
    return inserted


def get_short_vol_summary(ticker: str, db_path: str = DB_PATH) -> dict | None:
    """Return a small dict summarising recent short-volume for a ticker.

    Refreshes any missing days first (cheap on a hot cache), then computes
    the 30-day rolling short-pct average. Returns None when no data is
    available for the ticker (e.g. fresh install with FINRA unreachable).

    Shape:
        {
          'latest_date':  '2026-05-13',
          'latest_pct':   0.575,         # 0..1
          'avg_30d_pct':  0.482,
          'n_samples':    21,
          'delta_pp':     9.3,           # latest_pct - avg_30d_pct, in PERCENTAGE POINTS
        }
    """
    update_for_ticker(ticker, days_back=35, db_path=db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT date, short_pct FROM finra_short_vol "
            "WHERE ticker=? ORDER BY date DESC LIMIT 30",
            (ticker.upper(),),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    latest = rows[0]
    avg = sum(r["short_pct"] for r in rows) / len(rows)
    latest_pct = float(latest["short_pct"])
    return {
        "latest_date": latest["date"],
        "latest_pct":  latest_pct,
        "avg_30d_pct": float(avg),
        "n_samples":   len(rows),
        "delta_pp":    (latest_pct - avg) * 100.0,
    }


def format_brief_line(summary: dict) -> str:
    """Format a one-line summary suitable for inclusion in the CTO DV
    brief. Caller is responsible for handling the None case (no row)."""
    return (
        f"Short Vol: {summary['latest_pct']*100:.0f}% "
        f"(30d avg {summary['avg_30d_pct']*100:.0f}%, "
        f"as of {summary['latest_date']})"
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import sys
    t = sys.argv[1] if len(sys.argv) > 1 else "GME"
    s = get_short_vol_summary(t)
    if s is None:
        print(f"no data for {t}")
    else:
        print(format_brief_line(s))
        print(f"  delta vs 30d: {s['delta_pp']:+.1f} pp ({s['n_samples']} samples)")
