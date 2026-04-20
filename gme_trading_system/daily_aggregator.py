"""
Aggregates price_ticks into daily_candles with correct VWAP.

VWAP = SUM((high+low+close)/3 * volume) / SUM(volume)
All writes are atomic — a crash cannot leave a partial candle.
"""
import sqlite3
import os
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")
SYMBOL = "GME"


def aggregate_day(date_str: str | None = None):
    """Aggregate ticks for a given date (default: today) into daily_candles."""
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()

        # Correct VWAP: SUM(typical_price * volume) / SUM(volume)
        # typical_price = (high + low + close) / 3
        c.execute(
            """
            SELECT
                COUNT(*)                                          AS tick_count,
                SUM((high + low + close) / 3.0 * volume)         AS tp_vol_sum,
                SUM(volume)                                       AS vol_sum,
                MIN(low)                                          AS day_low,
                MAX(high)                                         AS day_high
            FROM price_ticks
            WHERE symbol=? AND timestamp LIKE ?
            """,
            (SYMBOL, f"{date_str}%"),
        )
        row = c.fetchone()

        if not row or row[0] == 0:
            print(f"[aggregator] No tick data for {date_str}")
            return

        tick_count, tp_vol_sum, vol_sum, day_low, day_high = row

        vwap = tp_vol_sum / vol_sum if vol_sum else 0.0

        # First tick open
        c.execute(
            "SELECT open FROM price_ticks WHERE symbol=? AND timestamp LIKE ? ORDER BY timestamp ASC LIMIT 1",
            (SYMBOL, f"{date_str}%"),
        )
        first = c.fetchone()
        open_ = float(first[0]) if first else 0.0

        # Last tick close
        c.execute(
            "SELECT close FROM price_ticks WHERE symbol=? AND timestamp LIKE ? ORDER BY timestamp DESC LIMIT 1",
            (SYMBOL, f"{date_str}%"),
        )
        last = c.fetchone()
        close = float(last[0]) if last else 0.0

        # Atomic replace: both DELETE and INSERT in one transaction
        conn.execute("BEGIN")
        conn.execute("DELETE FROM daily_candles WHERE symbol=? AND date=?", (SYMBOL, date_str))
        conn.execute(
            """
            INSERT INTO daily_candles (symbol, date, open, high, low, close, volume, vwap)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (SYMBOL, date_str, open_, day_high, day_low, close, vol_sum, round(vwap, 4)),
        )
        conn.execute("COMMIT")
        print(
            f"[aggregator] {date_str} — "
            f"O:{open_:.2f} H:{day_high:.2f} L:{day_low:.2f} C:{close:.2f} "
            f"V:{int(vol_sum)} VWAP:{vwap:.4f} ({tick_count} ticks)"
        )
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"[aggregator] ERROR for {date_str}: {e}")
        raise
    finally:
        conn.close()


def backfill(days: int = 30):
    """Backfill the last N days from price_ticks."""
    for i in range(days):
        date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        aggregate_day(date)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        backfill(int(sys.argv[2]) if len(sys.argv) > 2 else 30)
    else:
        aggregate_day()
