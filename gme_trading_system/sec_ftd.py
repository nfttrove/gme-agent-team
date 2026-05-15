"""SEC Fails-to-Deliver (FTD) intel.

Pulls the bi-weekly SEC fails-to-deliver files
(https://www.sec.gov/files/data/fails-deliver-data/cnsfails{YYYYMM}{a|b}.zip)
and caches per-ticker (settlement_date, fails_quantity, price) in
agent_memory.db. The summary helper returns latest settlement + rolling
14-day total so the CTO DV burst can flag elevated fails.

Cadence note: SEC publishes each half-month file roughly 30 days after
the half-month closes. So the 09:10 ET DV burst on 2026-05-15 sees the
'a' (settlement dates 1-15) file for April and may not yet see the 'b'
(16-end) file. That's the data publication lag, not a bug. Backfill
iterates the most recent ~3 months of half-month files.

Two-tier semantics mirror finra_short_vol:
  - get_ftd_summary("GME") → fetches any missing files first (cheap on
    a hot cache via file-level dedup), then computes the rolling stats.
  - update_for_ticker("GME", months_back=3) → standalone backfill, used
    by the daily cron (CTO DV at 09:10 ET) so the cache stays warm.

SEC requires a real User-Agent — uses the same SEC_USER_AGENT convention
as insider_buys.py / sec_scanner.py.
"""
from __future__ import annotations

import io
import logging
import os
import sqlite3
import zipfile
from datetime import date

import requests

from circuit_breaker import get_breaker

log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")

SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "GMETradingSystem research@example.com")
SEC_HEADERS = {"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sec_ftd (
    settlement_date  TEXT NOT NULL,
    ticker           TEXT NOT NULL,
    cusip            TEXT,
    fails_quantity   INTEGER NOT NULL,
    price            REAL,
    description      TEXT,
    fetched_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (settlement_date, ticker)
);
CREATE INDEX IF NOT EXISTS idx_sec_ftd_ticker_date ON sec_ftd(ticker, settlement_date);
CREATE TABLE IF NOT EXISTS sec_ftd_files (
    file_id    TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

_SEC_URL = "https://www.sec.gov/files/data/fails-deliver-data/cnsfails{file_id}.zip"


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _fetch_one_file(file_id: str) -> bytes | None:
    """Return raw zip bytes for cnsfails{file_id}.zip, or None on
    404 / non-200 / circuit-open / wrong-shape response. Wrapped in a
    circuit breaker so a long SEC outage doesn't keep hammering the cron."""
    breaker = get_breaker("sec_ftd")
    url = _SEC_URL.format(file_id=file_id)
    try:
        r = breaker.call(requests.get, url, timeout=30, headers=SEC_HEADERS)
    except Exception as e:
        log.debug(f"[sec_ftd] {file_id} fetch error: {e}")
        return None
    if r.status_code != 200:
        return None
    # Defensive: a real zip starts with PK\x03\x04
    if not r.content[:4].startswith(b"PK\x03\x04"):
        return None
    return r.content


def _parse_zip_for_ticker(
    content: bytes, ticker: str
) -> list[tuple[str, int, float | None, str | None, str | None]]:
    """Return list of (settlement_date_iso, fails_quantity, price, cusip,
    description) rows for the ticker. Empty list if absent or malformed.

    Source date format is YYYYMMDD; converted to YYYY-MM-DD for storage
    consistency with finra_short_vol."""
    target = ticker.upper()
    rows: list[tuple[str, int, float | None, str | None, str | None]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = zf.namelist()
            if not names:
                return []
            with zf.open(names[0]) as f:
                text = f.read().decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, KeyError, OSError) as e:
        log.debug(f"[sec_ftd] zip parse error: {e}")
        return []

    for line in text.splitlines()[1:]:  # skip header
        if line.startswith("Trailer"):
            continue
        parts = line.split("|")
        if len(parts) < 6 or parts[2].upper() != target:
            continue
        ymd = parts[0]
        if len(ymd) != 8 or not ymd.isdigit():
            continue
        iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
        try:
            qty = int(parts[3])
        except ValueError:
            continue
        try:
            price = float(parts[5])
        except (ValueError, IndexError):
            price = None
        cusip = parts[1] or None
        desc = parts[4] or None
        rows.append((iso, qty, price, cusip, desc))
    return rows


def _iter_recent_file_ids(months_back: int = 3) -> list[str]:
    """Generate the most recent half-month file IDs as 'YYYYMMa' /
    'YYYYMMb' strings, newest first. Iterates `months_back + 1` months
    to cover SEC's ~30-day publication lag."""
    today = date.today()
    year, month = today.year, today.month
    file_ids: list[str] = []
    for _ in range(months_back + 1):
        yyyymm = f"{year:04d}{month:02d}"
        file_ids.append(f"{yyyymm}b")
        file_ids.append(f"{yyyymm}a")
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return file_ids


def update_for_ticker(
    ticker: str, months_back: int = 3, db_path: str = DB_PATH
) -> int:
    """Fetch and cache FTD rows for the last `months_back` half-months.
    Returns the count of newly inserted rows. File-level dedup skips
    already-processed files without an HTTP call. Files that fail to
    fetch (404 / network) are NOT marked done — they'll be retried next
    cron."""
    inserted = 0
    target = ticker.upper()
    conn = sqlite3.connect(db_path)
    try:
        _ensure_schema(conn)
        for file_id in _iter_recent_file_ids(months_back):
            if conn.execute(
                "SELECT 1 FROM sec_ftd_files WHERE file_id=?",
                (file_id,),
            ).fetchone():
                continue
            content = _fetch_one_file(file_id)
            if content is None:
                continue
            rows = _parse_zip_for_ticker(content, target)
            for iso, qty, price, cusip, desc in rows:
                try:
                    conn.execute(
                        "INSERT INTO sec_ftd "
                        "(settlement_date, ticker, cusip, fails_quantity, "
                        " price, description) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (iso, target, cusip, qty, price, desc),
                    )
                    inserted += 1
                except sqlite3.IntegrityError:
                    pass  # already cached
            conn.execute(
                "INSERT OR IGNORE INTO sec_ftd_files (file_id) VALUES (?)",
                (file_id,),
            )
        conn.commit()
    finally:
        conn.close()
    if inserted:
        log.info(f"[sec_ftd] cached {inserted} new FTD rows for {target}")
    return inserted


def get_ftd_summary(ticker: str, db_path: str = DB_PATH) -> dict | None:
    """Return a small dict summarising recent FTD activity for a ticker.

    Refreshes any missing files first, then computes the rolling 14-day
    total of fails quantity from cached rows. Returns None when no data
    is available for the ticker.

    Shape:
        {
          'latest_date':     '2026-04-13',
          'latest_qty':       48242,
          'rolling_14d_qty':  163651,
          'n_samples':        5,
          'latest_price':     22.91,
        }
    """
    update_for_ticker(ticker, months_back=3, db_path=db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT settlement_date, fails_quantity, price FROM sec_ftd "
            "WHERE ticker=? ORDER BY settlement_date DESC LIMIT 14",
            (ticker.upper(),),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    latest = rows[0]
    total_14d = sum(r["fails_quantity"] for r in rows)
    return {
        "latest_date":     latest["settlement_date"],
        "latest_qty":      int(latest["fails_quantity"]),
        "rolling_14d_qty": int(total_14d),
        "n_samples":       len(rows),
        "latest_price":    float(latest["price"]) if latest["price"] is not None else None,
    }


def _fmt_qty(n: int) -> str:
    """Format a share quantity with K/M suffix where useful."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 10_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:,}"


def format_brief_line(summary: dict, ticker: str = "") -> str:
    """Format a one-line summary suitable for inclusion in the CTO DV
    brief. Caller handles the None case (no data)."""
    prefix = f"{ticker.upper()} " if ticker else ""
    return (
        f"{prefix}FTDs: {_fmt_qty(summary['latest_qty'])} "
        f"(settled {summary['latest_date']}, "
        f"14d total {_fmt_qty(summary['rolling_14d_qty'])})"
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import sys
    t = sys.argv[1] if len(sys.argv) > 1 else "GME"
    s = get_ftd_summary(t)
    if s is None:
        print(f"no data for {t}")
    else:
        print(format_brief_line(s, t))
        price_str = f"${s['latest_price']:.2f}" if s["latest_price"] is not None else "n/a"
        print(f"  latest price: {price_str}")
        print(f"  samples in 14d window: {s['n_samples']}")
