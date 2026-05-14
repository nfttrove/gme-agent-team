"""
Assembles the JSON payload served at GET /obs/stats.json.

Pulls from:
  - market_fundamentals (latest row) — yfinance .info + .quarterly_financials, refreshed daily
  - options_snapshots (next-or-current expiry) — max pain, refreshed weekly
  - yfinance Ticker.info (60s TTL cache) — today's authoritative OHLCV
    NOTE: we used to sum price_ticks for today's volume, but TradingView's
    free webhook misses bars, so SUM(volume) was an order of magnitude low.
    yfinance.info["regularMarketVolume"] is the official cumulative day vol.
  - datetime.now(ET) — formatted date header

Returns one flat dict. Numbers are returned raw; client-side JS handles formatting.
"""
import logging
import os
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")
SYMBOL  = "GME"

log = logging.getLogger(__name__)

# 60-second TTL cache around yfinance.info. The OBS panel polls /obs/stats.json
# every 30s; this collapses that down to one upstream yfinance call per minute,
# well under yfinance's rate limits. Fields stay <=60s stale on stream.
_INFO_TTL_S = 60
_info_cache = {"ts": 0.0, "data": {}}

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
    """Authoritative live OHLCV from yfinance.info (60s cached).

    Falls back to a price_ticks aggregation if yfinance is unavailable,
    but flags the gap — the SUM(volume) here is known-low because the
    TradingView webhook misses bars."""
    info = _yfinance_info()
    if info:
        return {
            "day_open":   _f(info.get("regularMarketOpen") or info.get("open")),
            "day_high":   _f(info.get("regularMarketDayHigh") or info.get("dayHigh")),
            "day_low":    _f(info.get("regularMarketDayLow")  or info.get("dayLow")),
            "day_volume": int(info["regularMarketVolume"]) if info.get("regularMarketVolume") else (int(info["volume"]) if info.get("volume") else None),
        }
    # Degraded path — webhook-only aggregation. day_volume will under-report.
    today = datetime.now(ET).date().isoformat()
    row = conn.execute(
        """SELECT
              (SELECT open FROM price_ticks
                 WHERE symbol=? AND timestamp LIKE ?
                 ORDER BY timestamp ASC LIMIT 1) AS day_open,
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


def _yfinance_info() -> dict:
    """yfinance.Ticker(GME).info with a 60s TTL cache."""
    now = time.monotonic()
    if now - _info_cache["ts"] < _INFO_TTL_S and _info_cache["data"]:
        return _info_cache["data"]
    try:
        import yfinance as yf
        data = yf.Ticker(SYMBOL).info or {}
        _info_cache["ts"] = now
        _info_cache["data"] = data
        return data
    except Exception as e:
        log.warning(f"[obs_panel] yfinance.info failed, falling back to DB: {e}")
        return _info_cache["data"]  # last good (may be stale) — better than empty


def _f(v):
    """Round price-like floats to 2dp; pass None through."""
    try:
        return round(float(v), 2) if v is not None else None
    except (TypeError, ValueError):
        return None


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
