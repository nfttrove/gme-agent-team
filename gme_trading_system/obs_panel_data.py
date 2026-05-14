"""
Assembles the JSON payload served at GET /obs/stats.json.

Pulls from four sources, all already in this project:
  - market_fundamentals (latest row) — yfinance .info + .quarterly_financials, refreshed daily
  - options_snapshots (next-or-current expiry) — max pain, refreshed weekly
  - price_ticks (today, ET) — open, intraday high/low, summed volume
  - daily_candles (yesterday) — prev close fallback if fundamentals row missing it
  - datetime.now(ET) — formatted date header

Returns one flat dict. Numbers are returned raw; client-side JS handles formatting.
"""
import os
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")
SYMBOL  = "GME"

_FUNDAMENTALS_COLS = [
    "market_cap", "market_cap_yoy_pct",
    "revenue_ttm", "revenue_yoy_pct",
    "net_income_ttm", "net_income_yoy_pct",
    "eps_ttm", "eps_yoy_pct",
    "shares_out", "shares_out_yoy_pct",
    "pe_ratio", "beta",
    "fifty_two_week_low", "fifty_two_week_high",
    "prev_close", "next_earnings_date",
    "dark_pool_pct", "dark_pool_volume", "dark_pool_date",
]


def assemble_panel_payload() -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return {
            "as_of": datetime.now(ET).isoformat(),
            "date_header": datetime.now(ET).strftime("%A, %B %-d, %Y"),
            "symbol": SYMBOL,
            **_latest_fundamentals(conn),
            **_today_market_data(conn),
            **_max_pain(conn),
        }
    finally:
        conn.close()


def _latest_fundamentals(conn) -> dict:
    row = conn.execute(
        f"SELECT {', '.join(_FUNDAMENTALS_COLS)} "
        "FROM market_fundamentals ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return {col: None for col in _FUNDAMENTALS_COLS}
    return {col: row[col] for col in _FUNDAMENTALS_COLS}


def _today_market_data(conn) -> dict:
    today = datetime.now(ET).date().isoformat()
    row = conn.execute(
        """SELECT
              (SELECT open  FROM price_ticks
                 WHERE symbol=? AND timestamp LIKE ?
                 ORDER BY timestamp ASC  LIMIT 1) AS day_open,
              (SELECT MAX(high) FROM price_ticks
                 WHERE symbol=? AND timestamp LIKE ?) AS day_high,
              (SELECT MIN(low)  FROM price_ticks
                 WHERE symbol=? AND timestamp LIKE ?) AS day_low,
              (SELECT SUM(volume) FROM price_ticks
                 WHERE symbol=? AND timestamp LIKE ?) AS day_volume
        """,
        (SYMBOL, f"{today}%") * 4,
    ).fetchone()
    return {
        "day_open":   row["day_open"],
        "day_high":   row["day_high"],
        "day_low":    row["day_low"],
        "day_volume": int(row["day_volume"]) if row["day_volume"] is not None else None,
    }


def _max_pain(conn) -> dict:
    today = datetime.now(ET).date().isoformat()
    row = conn.execute(
        """SELECT expiration, max_pain_strike, current_price, delta_to_max_pain,
                  put_call_ratio, net_oi_bias
             FROM options_snapshots
            WHERE expiration >= ?
         ORDER BY expiration ASC LIMIT 1""",
        (today,),
    ).fetchone()
    if row is None:
        # Fall back to most recent snapshot regardless of expiry (better than nothing).
        row = conn.execute(
            """SELECT expiration, max_pain_strike, current_price, delta_to_max_pain,
                      put_call_ratio, net_oi_bias
                 FROM options_snapshots
             ORDER BY timestamp DESC LIMIT 1"""
        ).fetchone()
    if row is None:
        return {
            "max_pain_strike": None,
            "max_pain_expiration": None,
            "max_pain_delta": None,
            "put_call_ratio": None,
            "net_oi_bias": None,
        }
    return {
        "max_pain_strike":     row["max_pain_strike"],
        "max_pain_expiration": row["expiration"],
        "max_pain_delta":      row["delta_to_max_pain"],
        "put_call_ratio":      row["put_call_ratio"],
        "net_oi_bias":         row["net_oi_bias"],
    }


if __name__ == "__main__":
    import json
    print(json.dumps(assemble_panel_payload(), indent=2, default=str))
