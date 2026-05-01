"""
Bulk-import GME daily OHLCV history from Yahoo Finance into daily_candles.

Default mode is idempotent: existing dates are skipped, preserving any
tick-derived VWAPs the daily_aggregator wrote.

`overwrite=True` (--overwrite) replaces existing rows in the fetched range
with yfinance values. Use when local tick capture has been incomplete
(partial webhook delivery → undercounted volumes); yfinance reports
exchange-authoritative volume.

VWAP is approximated as typical price (H+L+C)/3 since yfinance doesn't
supply intraday-VWAP.

Usage:
    python import_history.py                  # default: 2 years, skip existing
    python import_history.py 5                # 5 years, skip existing
    python import_history.py 1 --overwrite    # 1 year, replace existing rows
"""
import os
import sqlite3
import sys

DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")
SYMBOL = "GME"


def import_history(years: int = 2, db_path: str = DEFAULT_DB_PATH,
                   overwrite: bool = False) -> dict:
    import yfinance as yf

    hist = yf.Ticker(SYMBOL).history(period=f"{years}y")
    fetched = len(hist)
    if fetched == 0:
        print(f"[import_history] yfinance returned no data for {SYMBOL}")
        return {"fetched": 0, "inserted": 0, "skipped": 0, "deleted": 0}

    conn = sqlite3.connect(db_path)
    try:
        if overwrite:
            date_min = str(hist.index[0].date())
            date_max = str(hist.index[-1].date())
            cur = conn.execute(
                "DELETE FROM daily_candles WHERE symbol=? AND date BETWEEN ? AND ?",
                (SYMBOL, date_min, date_max),
            )
            deleted = cur.rowcount
            existing = set()
        else:
            deleted = 0
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
    skipped = fetched - inserted - (deleted if not overwrite else 0)
    print(
        f"[import_history] fetched={fetched} inserted={inserted} "
        f"deleted={deleted} skipped={skipped} "
        f"range={date_min}..{date_max} total={total}"
    )
    return {"fetched": fetched, "inserted": inserted,
            "skipped": skipped, "deleted": deleted}


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a not in ("--overwrite", "-o")]
    overwrite = any(a in ("--overwrite", "-o") for a in sys.argv[1:])
    years = int(args[0]) if args else 2
    import_history(years, overwrite=overwrite)
