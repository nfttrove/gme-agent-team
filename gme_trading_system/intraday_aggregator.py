"""
Aggregates price_ticks into 5-minute intraday_candles with VWAP.

Mirrors daily_aggregator.py but buckets ticks into 5-minute windows so the
pattern detector can operate on intraday geometry. Re-runs are idempotent
via INSERT OR REPLACE on (symbol, bucket_start, interval).

VWAP per bucket = SUM(typical_price * volume) / SUM(volume)
typical_price = (high + low + close) / 3
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")
SYMBOL = "GME"
INTERVAL = "5m"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS intraday_candles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT    NOT NULL DEFAULT 'GME',
    bucket_start TEXT    NOT NULL,
    interval     TEXT    NOT NULL DEFAULT '5m',
    open         REAL,
    high         REAL,
    low          REAL,
    close        REAL,
    volume       REAL,
    vwap         REAL,
    created_at   TEXT    DEFAULT (datetime('now')),
    UNIQUE(symbol, bucket_start, interval)
);
"""

_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_intraday_candles_lookup "
    "ON intraday_candles(symbol, interval, bucket_start);"
)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA_SQL)
    conn.execute(_INDEX_SQL)
    conn.commit()


def _parse_ts(ts: str) -> datetime | None:
    """Parse an ISO-8601 timestamp (with or without trailing Z) into a naive
    datetime. Returns None if parsing fails. We strip Z because the only
    use here is bucket assignment — we don't need an aware datetime."""
    try:
        return datetime.fromisoformat(ts.rstrip("Z"))
    except ValueError:
        return None


def _bucket_floor(dt: datetime) -> str:
    """Floor a datetime to the nearest 5-minute boundary, formatted as
    'YYYY-MM-DD HH:MM:00' (matches daily_candles' date-style text key)."""
    minute = (dt.minute // 5) * 5
    floored = dt.replace(minute=minute, second=0, microsecond=0)
    return floored.strftime("%Y-%m-%d %H:%M:00")


def aggregate_5m_bars(lookback_minutes: int = 240, symbol: str = SYMBOL) -> int:
    """Bucket ticks from the last `lookback_minutes` into 5-minute candles
    and write them to `intraday_candles`. Returns the number of bars written.

    Idempotent: re-running over the same window updates existing bars in place.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        _ensure_schema(conn)

        # Cutoff in plain ISO (no Z) — lex-comparison vs both Z-suffixed and
        # naive timestamps in price_ticks behaves correctly for the same
        # YYYY-MM-DDTHH:MM:SS prefix.
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)) \
            .strftime("%Y-%m-%dT%H:%M:%S")

        c = conn.cursor()
        c.execute(
            "SELECT timestamp, open, high, low, close, volume "
            "FROM price_ticks WHERE symbol=? AND timestamp >= ? "
            "ORDER BY timestamp ASC",
            (symbol, cutoff),
        )
        rows = c.fetchall()
        if not rows:
            print(f"[intraday_aggregator] no ticks since {cutoff}")
            return 0

        buckets: dict[str, list[tuple]] = {}
        for ts, o, h, l, cl, v in rows:
            dt = _parse_ts(ts)
            if dt is None:
                continue
            buckets.setdefault(_bucket_floor(dt), []).append((ts, o, h, l, cl, v))

        if not buckets:
            print("[intraday_aggregator] all ticks failed timestamp parse")
            return 0

        try:
            conn.execute("BEGIN")
            written = 0
            for bucket_start, ticks in buckets.items():
                # ticks are already in chronological order from the SELECT
                opens = [t[1] for t in ticks if t[1] is not None]
                highs = [t[2] for t in ticks if t[2] is not None]
                lows  = [t[3] for t in ticks if t[3] is not None]
                closes = [t[4] for t in ticks if t[4] is not None]
                if not closes:
                    continue

                open_ = float(opens[0]) if opens else float(closes[0])
                close_ = float(closes[-1])
                high_ = float(max(highs)) if highs else max(open_, close_)
                low_ = float(min(lows)) if lows else min(open_, close_)

                vol = 0.0
                tpv = 0.0
                for _, _o, _h, _l, _c, _v in ticks:
                    if _c is None:
                        continue
                    vv = float(_v or 0.0)
                    hh = float(_h) if _h is not None else float(_c)
                    ll = float(_l) if _l is not None else float(_c)
                    typical = (hh + ll + float(_c)) / 3.0
                    tpv += typical * vv
                    vol += vv
                vwap = tpv / vol if vol else 0.0

                conn.execute(
                    "INSERT OR REPLACE INTO intraday_candles "
                    "(symbol, bucket_start, interval, open, high, low, close, volume, vwap) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (symbol, bucket_start, INTERVAL,
                     open_, high_, low_, close_, vol, round(vwap, 4)),
                )
                written += 1
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        print(f"[intraday_aggregator] wrote {written} {INTERVAL} bars for {symbol}")
        return written
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    minutes = int(sys.argv[1]) if len(sys.argv) > 1 else 240
    aggregate_5m_bars(minutes)
