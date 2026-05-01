"""
Bulk-import GME daily OHLCV history from Yahoo Finance into daily_candles.

Idempotent: existing dates are skipped (the daily_candles table has no UNIQUE
constraint on (symbol, date), so we filter in Python rather than relying on
INSERT OR IGNORE).

VWAP is approximated as typical price (H+L+C)/3 since yfinance doesn't supply
it. Tick-derived VWAPs from daily_aggregator take priority because we never
overwrite an existing date.

Usage:
    python import_history.py            # default: 2 years
    python import_history.py 5          # 5 years
"""
import os
import sqlite3
import sys

DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")
SYMBOL = "GME"


def import_history(years: int = 2, db_path: str = DEFAULT_DB_PATH) -> dict:
    import yfinance as yf

    hist = yf.Ticker(SYMBOL).history(period=f"{years}y")
    fetched = len(hist)
    if fetched == 0:
        print(f"[import_history] yfinance returned no data for {SYMBOL}")
        return {"fetched": 0, "inserted": 0, "skipped": 0}

    conn = sqlite3.connect(db_path)
    try:
        existing = {
            r[0] for r in conn.execute(
                "SELECT date FROM daily_candles WHERE symbol=?", (SYMBOL,)
            )
        }

        new_rows = []
        for idx, row in hist.iterrows():
            date = str(idx.date())
            if date in existing:
                continue
            high, low, close = float(row["High"]), float(row["Low"]), float(row["Close"])
            vwap = round((high + low + close) / 3.0, 4)
            new_rows.append(
                (SYMBOL, date, float(row["Open"]), high, low, close, float(row["Volume"]), vwap)
            )

        if new_rows:
            conn.executemany(
                "INSERT INTO daily_candles "
                "(symbol, date, open, high, low, close, volume, vwap) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                new_rows,
            )
            conn.commit()

        total, date_min, date_max = conn.execute(
            "SELECT COUNT(*), MIN(date), MAX(date) FROM daily_candles WHERE symbol=?",
            (SYMBOL,),
        ).fetchone()
    finally:
        conn.close()

    inserted = len(new_rows)
    skipped = fetched - inserted
    print(
        f"[import_history] fetched={fetched} inserted={inserted} skipped={skipped} "
        f"range={date_min}..{date_max} total={total}"
    )
    return {"fetched": fetched, "inserted": inserted, "skipped": skipped}


if __name__ == "__main__":
    years = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    import_history(years)
